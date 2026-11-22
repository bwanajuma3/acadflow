from fastapi import FastAPI, Request
from database import SessionLocal, Assignment, ClientPaymentTransaction

app = FastAPI(title="AcadFlow M-Pesa Daraja Webhook Service")

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "AcadFlow Daraja Callback API"}

@app.post("/api/mpesa/callback")
async def mpesa_stk_callback(request: Request):
    """Receives Safaricom Daraja STK Push JSON payload and auto-confirms payment."""
    payload = await request.json()
    stk_callback = payload.get("Body", {}).get("stkCallback", {})
    result_code = stk_callback.get("ResultCode")
    checkout_id = stk_callback.get("CheckoutRequestID")

    if result_code != 0:
        return {"ResultCode": 0, "ResultDesc": "Callback received (Transaction cancelled/failed)"}

    items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
    meta = {item["Name"]: item.get("Value") for item in items}

    amount = float(meta.get("Amount", 0))
    mpesa_receipt = str(meta.get("MpesaReceiptNumber", ""))
    phone = str(meta.get("PhoneNumber", ""))

    db = SessionLocal()
    try:
        # Idempotency check: prevent duplicate webhook processing
        if db.query(ClientPaymentTransaction).filter_by(mpesa_code=mpesa_receipt).first():
            return {"ResultCode": 0, "ResultDesc": "Already processed"}

        pending_tx = db.query(ClientPaymentTransaction).filter_by(checkout_request_id=checkout_id).first()
        if pending_tx:
            pending_tx.mpesa_code = mpesa_receipt
            pending_tx.amount_kes = amount
            pending_tx.phone_number = phone
            pending_tx.status = "Verified"

            assignment = db.query(Assignment).filter_by(id=pending_tx.assignment_id).first()
            if assignment:
                assignment.amount_paid_kes += amount
                if assignment.amount_paid_kes >= assignment.quoted_cost_kes:
                    assignment.status = "5. Payment confirmed"
            db.commit()
    finally:
        db.close()

    return {"ResultCode": 0, "ResultDesc": "Accepted"}