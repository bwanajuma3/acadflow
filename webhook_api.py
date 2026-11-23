from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from database import (
    Base, engine, SessionLocal, Tenant, User,
    Assignment, ClientPaymentTransaction, PlatformSubscriptionPayment
)

# Ensure tables and checkout_request_id columns exist on both ledgers
Base.metadata.create_all(bind=engine)
with engine.begin() as conn:
    conn.execute(text("ALTER TABLE client_payments ADD COLUMN IF NOT EXISTS checkout_request_id VARCHAR;"))
    conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN IF NOT EXISTS checkout_request_id VARCHAR;"))
    conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN IF NOT EXISTS phone_number VARCHAR;"))

app = FastAPI(title="AcadFlow M-Pesa Daraja Webhook Service")

@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "AcadFlow Daraja Callback API"}

@app.post("/api/mpesa/callback")
async def mpesa_stk_callback(request: Request):
    """Receives Safaricom Daraja STK Push callback for both Client Projects and Tenant SaaS Subscriptions."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ResultCode": 1, "ResultDesc": "Invalid JSON payload"})

    stk_callback = payload.get("Body", {}).get("stkCallback", {})
    result_code = stk_callback.get("ResultCode")
    checkout_id = stk_callback.get("CheckoutRequestID")

    if result_code != 0:
        return {"ResultCode": 0, "ResultDesc": "Callback received (Transaction cancelled/failed)"}

    items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
    meta = {item.get("Name"): item.get("Value") for item in items if isinstance(item, dict)}

    amount = float(meta.get("Amount", 0) or 0)
    mpesa_receipt = str(meta.get("MpesaReceiptNumber", "")).strip()
    phone = str(meta.get("PhoneNumber", "")).strip()

    db = SessionLocal()
    try:
        # 1. Check Ledger 1: Tenant SaaS Subscription Payment
        sub_tx = db.query(PlatformSubscriptionPayment).filter_by(checkout_request_id=checkout_id).first()
        if sub_tx:
            if sub_tx.status != "Verified":
                sub_tx.mpesa_code = mpesa_receipt
                sub_tx.amount_kes = amount
                sub_tx.phone_number = phone
                sub_tx.status = "Verified"

                tenant = db.query(Tenant).filter_by(id=sub_tx.tenant_id).first()
                if tenant:
                    tenant.tier = sub_tx.plan_name
                    tenant.status = "Active"
                db.commit()
            return {"ResultCode": 0, "ResultDesc": "Subscription Activated"}

        # 2. Check Ledger 2: Client Assignment Payment
        if mpesa_receipt and db.query(ClientPaymentTransaction).filter_by(mpesa_code=mpesa_receipt).first():
            return {"ResultCode": 0, "ResultDesc": "Already processed"}

        pending_tx = db.query(ClientPaymentTransaction).filter_by(checkout_request_id=checkout_id).first()
        if pending_tx:
            pending_tx.mpesa_code = mpesa_receipt
            pending_tx.amount_kes = amount
            pending_tx.phone_number = phone
            pending_tx.status = "Verified"

            assignment = db.query(Assignment).filter_by(id=pending_tx.assignment_id).first()
            if assignment:
                assignment.amount_paid_kes = (assignment.amount_paid_kes or 0.0) + amount
                if assignment.amount_paid_kes >= (assignment.quoted_cost_kes or 0.0):
                    assignment.status = "5. Payment confirmed"
            db.commit()
    except Exception as exc:
        db.rollback()
        return JSONResponse(status_code=500, content={"ResultCode": 1, "ResultDesc": f"Database error: {str(exc)}"})
    finally:
        db.close()

    return {"ResultCode": 0, "ResultDesc": "Accepted"}