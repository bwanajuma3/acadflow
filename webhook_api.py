import base64
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from database import (
    ASSIGNMENT_STATUSES,
    Assignment,
    AssignmentLog,
    ClientPaymentTransaction,
    PlatformSubscriptionPayment,
    SessionLocal,
    SubscriptionPlan,
    Tenant,
    init_db_schema,
    utc_now,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("acadflow.webhook")

DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox").lower()
DARAJA_BASE_URL = (
    "https://api.safaricom.co.ke"
    if DARAJA_ENV == "production"
    else "https://sandbox.safaricom.co.ke"
)
DARAJA_WEBHOOK_TOKEN = os.getenv("DARAJA_WEBHOOK_TOKEN", "")
FERNET_SECRET_KEY = os.getenv("FERNET_SECRET_KEY", "")

# Platform-level Daraja credentials (used for SaaS subscription STK queries)
PLATFORM_CONSUMER_KEY = os.getenv("PLATFORM_DARAJA_CONSUMER_KEY", "")
PLATFORM_CONSUMER_SECRET = os.getenv("PLATFORM_DARAJA_CONSUMER_SECRET", "")
PLATFORM_PASSKEY = os.getenv("PLATFORM_DARAJA_PASSKEY", "")
PLATFORM_SHORTCODE = os.getenv("PLATFORM_DARAJA_SHORTCODE", "174379")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_schema()
    yield


app = FastAPI(
    title="AcadFlow M-Pesa Daraja Webhook & Reconciliation Service",
    lifespan=lifespan,
)


# ==========================================
# DEPENDENCIES & SECURITY HELPERS
# ==========================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_webhook_token(token: Optional[str] = Query(default=None)):
    """Validates the shared secret token passed in the callback query string."""
    if not DARAJA_WEBHOOK_TOKEN:
        logger.error("DARAJA_WEBHOOK_TOKEN is not configured on the server.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook authentication is not configured.",
        )
    if not token or not hmac.compare_digest(token, DARAJA_WEBHOOK_TOKEN):
        logger.warning("Rejected unauthorized Daraja callback attempt.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook token.",
        )


def decrypt_secret(cipher_text: str) -> str:
    """Decrypts Fernet-encrypted tenant Daraja credentials stored in the database."""
    if not cipher_text:
        return ""
    if not FERNET_SECRET_KEY:
        return cipher_text
    try:
        f = Fernet(FERNET_SECRET_KEY.encode("utf-8"))
        return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Fallback if value is still plaintext during migration
        return cipher_text


def extract_callback_metadata(items: list[dict]) -> Dict[str, Any]:
    """Converts Daraja CallbackMetadata.Item list into a flat dictionary."""
    parsed: Dict[str, Any] = {}
    for entry in items or []:
        name = entry.get("Name")
        if name:
            parsed[name] = entry.get("Value")
    return parsed


# ==========================================
# CORE IDEMPOTENT SETTLEMENT LOGIC
# ==========================================
def settle_client_payment(
    db: Session,
    checkout_request_id: str,
    result_code: int,
    result_desc: str,
    mpesa_receipt: str = "",
    amount: Optional[float] = None,
    phone: str = "",
) -> str:
    """Idempotently settles or marks failed a client assignment STK Push transaction."""
    tx = (
        db.query(ClientPaymentTransaction)
        .filter(ClientPaymentTransaction.checkout_request_id == checkout_request_id)
        .first()
    )
    if not tx and mpesa_receipt:
        tx = (
            db.query(ClientPaymentTransaction)
            .filter(ClientPaymentTransaction.mpesa_code == mpesa_receipt)
            .first()
        )

    if not tx:
        return "not_found"

    # Idempotency guard: never apply the same payment twice
    if tx.status == "Verified":
        logger.info("Duplicate callback ignored for Verified client tx %s", checkout_request_id)
        return "already_verified"

    if result_code != 0:
        if tx.status == "Pending Verification":
            tx.status = f"Failed ({result_desc[:60]})"
            db.commit()
        return "failed"

    # Prevent duplicate receipt collisions across retries
    if mpesa_receipt:
        existing_receipt = (
            db.query(ClientPaymentTransaction)
            .filter(
                ClientPaymentTransaction.tenant_id == tx.tenant_id,
                ClientPaymentTransaction.mpesa_code == mpesa_receipt,
                ClientPaymentTransaction.id != tx.id,
            )
            .first()
        )
        if existing_receipt and existing_receipt.status == "Verified":
            tx.status = "Duplicate Ignored"
            db.commit()
            return "already_verified"
        tx.mpesa_code = mpesa_receipt

    paid_amount = float(amount) if amount is not None else float(tx.amount_kes)
    tx.amount_kes = paid_amount
    if phone:
        tx.phone_number = str(phone)
    tx.status = "Verified"

    assignment = db.query(Assignment).filter(Assignment.id == tx.assignment_id).first()
    if assignment:
        assignment.amount_paid_kes = float(assignment.amount_paid_kes or 0.0) + paid_amount
        if (
            assignment.amount_paid_kes >= float(assignment.quoted_cost_kes or 0.0)
            and assignment.status in {
                ASSIGNMENT_STATUSES[0],
                ASSIGNMENT_STATUSES[1],
                ASSIGNMENT_STATUSES[2],
                ASSIGNMENT_STATUSES[3],
            }
        ):
            assignment.status = ASSIGNMENT_STATUSES[4]  # "5. Payment confirmed"

        db.add(
            AssignmentLog(
                tenant_id=tx.tenant_id,
                assignment_id=assignment.id,
                actor_name="M-Pesa Daraja Webhook",
                action_note=(
                    f"Verified KES {paid_amount:,.2f} via M-Pesa "
                    f"(Receipt: {tx.mpesa_code or checkout_request_id})."
                ),
            )
        )

    db.commit()
    return "verified"


def settle_subscription_payment(
    db: Session,
    checkout_request_id: str,
    result_code: int,
    result_desc: str,
    mpesa_receipt: str = "",
    amount: Optional[float] = None,
    phone: str = "",
) -> str:
    """Idempotently settles a tenant's SaaS subscription payment and extends access."""
    sub_tx = (
        db.query(PlatformSubscriptionPayment)
        .filter(PlatformSubscriptionPayment.checkout_request_id == checkout_request_id)
        .first()
    )
    if not sub_tx and mpesa_receipt:
        sub_tx = (
            db.query(PlatformSubscriptionPayment)
            .filter(PlatformSubscriptionPayment.mpesa_code == mpesa_receipt)
            .first()
        )

    if not sub_tx:
        return "not_found"

    if sub_tx.status == "Verified":
        logger.info("Duplicate callback ignored for Verified subscription tx %s", checkout_request_id)
        return "already_verified"

    if result_code != 0:
        if sub_tx.status == "Pending Verification":
            sub_tx.status = f"Failed ({result_desc[:60]})"
            db.commit()
        return "failed"

    if mpesa_receipt:
        sub_tx.mpesa_code = mpesa_receipt
    paid_amount = float(amount) if amount is not None else float(sub_tx.amount_kes)
    sub_tx.amount_kes = paid_amount
    if phone:
        sub_tx.phone_number = str(phone)
    sub_tx.status = "Verified"
    sub_tx.paid_at = utc_now()

    tenant = db.query(Tenant).filter(Tenant.id == sub_tx.tenant_id).first()
    if tenant:
        plan = (
            db.query(SubscriptionPlan)
            .filter(SubscriptionPlan.name == sub_tx.plan_name)
            .first()
        )
        days_to_add = 365 if (plan and paid_amount >= float(plan.annual_price_kes)) else 30
        now = utc_now()
        base_date = (
            tenant.subscription_ends_at
            if (tenant.subscription_ends_at and tenant.subscription_ends_at > now)
            else now
        )
        tenant.subscription_ends_at = base_date + timedelta(days=days_to_add)
        tenant.tier = sub_tx.plan_name
        tenant.status = "Active"

    db.commit()
    return "verified"


# ==========================================
# DARAJA STK PUSH QUERY RECONCILIATION
# ==========================================
def fetch_daraja_token(consumer_key: str, consumer_secret: str) -> str:
    auth_str = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode("utf-8")).decode("utf-8")
    resp = requests.get(
        f"{DARAJA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth_str}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def query_daraja_stk_status(
    shortcode: str,
    passkey: str,
    consumer_key: str,
    consumer_secret: str,
    checkout_request_id: str,
) -> Dict[str, Any]:
    """Queries Safaricom's STK Push Query API for transactions missing a webhook."""
    access_token = fetch_daraja_token(consumer_key, consumer_secret)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw_password = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw_password.encode("utf-8")).decode("utf-8")

    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    resp = requests.post(
        f"{DARAJA_BASE_URL}/mpesa/stkpushquery/v1/query",
        json=payload,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    return resp.json()


# ==========================================
# FASTAPI ENDPOINTS
# ==========================================
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "acadflow-daraja-webhook", "timestamp": utc_now().isoformat()}


@app.post("/api/payments/daraja-callback")
async def daraja_stk_callback(
    request: Request,
    _: None = Depends(verify_webhook_token),
    db: Session = Depends(get_db),
):
    """
    Receives Safaricom Daraja STK Push callbacks.
    Always returns HTTP 200 after valid auth so Safaricom stops retrying.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    stk_callback = payload.get("Body", {}).get("stkCallback", {})
    checkout_request_id = stk_callback.get("CheckoutRequestID", "")
    result_code = int(stk_callback.get("ResultCode", -1))
    result_desc = str(stk_callback.get("ResultDesc", ""))

    if not checkout_request_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted (missing CheckoutRequestID)"}

    meta_items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
    meta = extract_callback_metadata(meta_items)

    mpesa_receipt = str(meta.get("MpesaReceiptNumber", "") or "")
    amount = meta.get("Amount")
    phone = str(meta.get("PhoneNumber", "") or "")

    # 1. Try settling as a client assignment payment
    outcome = settle_client_payment(
        db=db,
        checkout_request_id=checkout_request_id,
        result_code=result_code,
        result_desc=result_desc,
        mpesa_receipt=mpesa_receipt,
        amount=amount,
        phone=phone,
    )

    # 2. If not found in client payments, check platform SaaS subscription payments
    if outcome == "not_found":
        outcome = settle_subscription_payment(
            db=db,
            checkout_request_id=checkout_request_id,
            result_code=result_code,
            result_desc=result_desc,
            mpesa_receipt=mpesa_receipt,
            amount=amount,
            phone=phone,
        )

    logger.info(
        "Processed Daraja callback | CheckoutRequestID=%s | ResultCode=%s | Outcome=%s",
        checkout_request_id,
        result_code,
        outcome,
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/api/payments/reconcile-pending")
def reconcile_pending_stk_pushes(
    min_age_seconds: int = Query(default=90, ge=30, le=86400),
    _: None = Depends(verify_webhook_token),
    db: Session = Depends(get_db),
):
    """
    Polls Daraja STK Push Query for any 'Pending Verification' transactions older
    than min_age_seconds whose callback was delayed or dropped.
    """
    cutoff = utc_now() - timedelta(seconds=min_age_seconds)
    reconciled = []
    errors = []

    # 1. Reconcile pending client payments using each tenant's Daraja credentials
    pending_client_txs = (
        db.query(ClientPaymentTransaction)
        .filter(
            ClientPaymentTransaction.status == "Pending Verification",
            ClientPaymentTransaction.checkout_request_id != "",
            ClientPaymentTransaction.recorded_at <= cutoff,
        )
        .all()
    )

    for tx in pending_client_txs:
        tenant = db.query(Tenant).filter(Tenant.id == tx.tenant_id).first()
        if not tenant:
            continue
        c_key = decrypt_secret(tenant.daraja_consumer_key)
        c_secret = decrypt_secret(tenant.daraja_consumer_secret)
        passkey = decrypt_secret(tenant.daraja_passkey)
        shortcode = tenant.mpesa_paybill

        if not (c_key and c_secret and passkey and shortcode):
            continue

        try:
            res = query_daraja_stk_status(
                shortcode=shortcode,
                passkey=passkey,
                consumer_key=c_key,
                consumer_secret=c_secret,
                checkout_request_id=tx.checkout_request_id,
            )
            if "ResultCode" in res:
                r_code = int(res["ResultCode"])
                r_desc = str(res.get("ResultDesc", ""))
                outcome = settle_client_payment(
                    db=db,
                    checkout_request_id=tx.checkout_request_id,
                    result_code=r_code,
                    result_desc=r_desc,
                )
                reconciled.append(
                    {"type": "client", "checkout_request_id": tx.checkout_request_id, "outcome": outcome}
                )
        except Exception as exc:
            errors.append({"checkout_request_id": tx.checkout_request_id, "error": str(exc)})

    # 2. Reconcile pending platform SaaS subscription payments
    if PLATFORM_CONSUMER_KEY and PLATFORM_CONSUMER_SECRET and PLATFORM_PASSKEY:
        pending_sub_txs = (
            db.query(PlatformSubscriptionPayment)
            .filter(
                PlatformSubscriptionPayment.status == "Pending Verification",
                PlatformSubscriptionPayment.checkout_request_id != "",
                PlatformSubscriptionPayment.paid_at <= cutoff,
            )
            .all()
        )
        for sub_tx in pending_sub_txs:
            try:
                res = query_daraja_stk_status(
                    shortcode=PLATFORM_SHORTCODE,
                    passkey=PLATFORM_PASSKEY,
                    consumer_key=PLATFORM_CONSUMER_KEY,
                    consumer_secret=PLATFORM_CONSUMER_SECRET,
                    checkout_request_id=sub_tx.checkout_request_id,
                )
                if "ResultCode" in res:
                    r_code = int(res["ResultCode"])
                    r_desc = str(res.get("ResultDesc", ""))
                    outcome = settle_subscription_payment(
                        db=db,
                        checkout_request_id=sub_tx.checkout_request_id,
                        result_code=r_code,
                        result_desc=r_desc,
                    )
                    reconciled.append(
                        {"type": "subscription", "checkout_request_id": sub_tx.checkout_request_id, "outcome": outcome}
                    )
            except Exception as exc:
                errors.append({"checkout_request_id": sub_tx.checkout_request_id, "error": str(exc)})

    return {
        "checked_client_count": len(pending_client_txs),
        "reconciled": reconciled,
        "errors": errors,
    }