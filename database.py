import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

def get_db_secret(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default

DATABASE_URL = get_db_secret("DATABASE_URL", "sqlite:///acadflow_mvp.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

DATABASE_URL = DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")

connect_args = (
    {"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {"connect_timeout": 10}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=300
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

ASSIGNMENT_STATUSES = [
    "1. Submitted",
    "2. Under review",
    "3. Quotation sent",
    "4. Awaiting payment",
    "5. Payment confirmed",
    "6. Assigned",
    "7. In progress",
    "8. Internal review (Staff -> Admin)",
    "9. Awaiting client feedback",
    "10. Revision requested",
    "11. Completed",
    "12. Closed"
]

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ==========================================
# MULTI-TENANT SQLALCHEMY MODELS
# ==========================================
class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    monthly_price_kes = Column(Float, nullable=False)
    annual_price_kes = Column(Float, nullable=False)
    max_staff = Column(Integer, nullable=False)
    max_active_projects = Column(Integer, nullable=False)
    features_summary = Column(String, nullable=False)

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    business_name = Column(String, unique=True, nullable=False)
    tier = Column(String, default="Professional")
    status = Column(String, default="Active")
    trial_ends_at = Column(DateTime, default=lambda: utc_now() + timedelta(days=14))
    subscription_ends_at = Column(DateTime, nullable=True)
    mpesa_paybill = Column(String, default="522522")
    mpesa_account = Column(String, default="ACADFLOW")
    daraja_consumer_key = Column(String, default="")
    daraja_consumer_secret = Column(String, default="")
    daraja_passkey = Column(String, default="")
    created_at = Column(DateTime, default=utc_now)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    commission_rate = Column(Float, default=50.0)

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text)
    base_price_kes = Column(Float, nullable=False)
    turnaround_days = Column(Integer, default=3)

class Assignment(Base):
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    ref_number = Column(String, unique=True, nullable=False)
    invoice_number = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text)
    service_id = Column(Integer, ForeignKey("services.id"))
    client_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    staff_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    status = Column(String, default=ASSIGNMENT_STATUSES[0])
    priority = Column(String, default="Normal")
    quoted_cost_kes = Column(Float, default=0.0)
    amount_paid_kes = Column(Float, default=0.0)
    staff_commission_pct = Column(Float, default=50.0)
    staff_payout_status = Column(String, default="Unpaid")
    staff_payout_mpesa = Column(String, default="")
    staff_paid_at = Column(DateTime, nullable=True)
    deadline = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)

class ClientReview(Base):
    __tablename__ = "client_reviews"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), unique=True, nullable=False)
    client_name = Column(String, nullable=False)
    rating = Column(Integer, nullable=False)
    feedback = Column(Text)
    created_at = Column(DateTime, default=utc_now)

class AssignmentDocument(Base):
    __tablename__ = "assignment_documents"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    uploaded_by_name = Column(String, nullable=False)
    doc_category = Column(String, nullable=False)
    original_filename = Column(String, nullable=False)
    file_hash = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=utc_now)

class AssignmentMessage(Base):
    __tablename__ = "assignment_messages"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    sender_name = Column(String, nullable=False)
    sender_role = Column(String, nullable=False)
    channel = Column(String, default="CLIENT_CHAT")
    message_text = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=utc_now)

class AssignmentLog(Base):
    __tablename__ = "assignment_logs"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    actor_name = Column(String, nullable=False)
    action_note = Column(String, nullable=False)
    timestamp = Column(DateTime, default=utc_now)

class ClientPaymentTransaction(Base):
    __tablename__ = "client_payments"
    __table_args__ = (UniqueConstraint("tenant_id", "mpesa_code", name="uq_tenant_client_mpesa_code"),)
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    checkout_request_id = Column(String, default="")
    amount_kes = Column(Float, nullable=False)
    mpesa_code = Column(String, nullable=False)
    phone_number = Column(String, default="")
    status = Column(String, default="Pending Verification")
    recorded_at = Column(DateTime, default=utc_now)

class PlatformSubscriptionPayment(Base):
    __tablename__ = "platform_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    checkout_request_id = Column(String, default="")
    plan_name = Column(String, nullable=False)
    amount_kes = Column(Float, nullable=False)
    mpesa_code = Column(String, unique=True, nullable=False)
    phone_number = Column(String, default="")
    status = Column(String, default="Verified")
    paid_at = Column(DateTime, default=utc_now)

class PlatformAnnouncement(Base):
    __tablename__ = "platform_announcements"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now)

class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    business_name = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String, default="Open")
    admin_reply = Column(Text, default="")
    created_at = Column(DateTime, default=utc_now)

class EmailNotificationLog(Base):
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    delivery_status = Column(String, default="Queued / Logged")
    sent_at = Column(DateTime, default=utc_now)

def init_db_schema():
    """Creates all tables and ensures required columns exist across SQLite and PostgreSQL."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            t_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(tenants)")).fetchall()]
            if "subscription_ends_at" not in t_cols:
                conn.execute(text("ALTER TABLE tenants ADD COLUMN subscription_ends_at DATETIME"))
            cp_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(client_payments)")).fetchall()]
            if "checkout_request_id" not in cp_cols:
                conn.execute(text("ALTER TABLE client_payments ADD COLUMN checkout_request_id VARCHAR"))
            ps_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(platform_subscriptions)")).fetchall()]
            if "checkout_request_id" not in ps_cols:
                conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN checkout_request_id VARCHAR"))
            if "phone_number" not in ps_cols:
                conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN phone_number VARCHAR"))
            am_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(assignment_messages)")).fetchall()]
            if "channel" not in am_cols:
                conn.execute(text("ALTER TABLE assignment_messages ADD COLUMN channel VARCHAR DEFAULT 'CLIENT_CHAT'"))
            u_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if "commission_rate" not in u_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN commission_rate FLOAT DEFAULT 50.0"))
            a_cols = [r[1] for r in conn.execute(text("PRAGMA table_info(assignments)")).fetchall()]
            if "staff_commission_pct" not in a_cols:
                conn.execute(text("ALTER TABLE assignments ADD COLUMN staff_commission_pct FLOAT DEFAULT 50.0"))
            if "staff_payout_status" not in a_cols:
                conn.execute(text("ALTER TABLE assignments ADD COLUMN staff_payout_status VARCHAR DEFAULT 'Unpaid'"))
            if "staff_payout_mpesa" not in a_cols:
                conn.execute(text("ALTER TABLE assignments ADD COLUMN staff_payout_mpesa VARCHAR DEFAULT ''"))
            if "staff_paid_at" not in a_cols:
                conn.execute(text("ALTER TABLE assignments ADD COLUMN staff_paid_at DATETIME"))
        else:
            conn.execute(text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subscription_ends_at TIMESTAMP;"))
            conn.execute(text("ALTER TABLE client_payments ADD COLUMN IF NOT EXISTS checkout_request_id VARCHAR;"))
            conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN IF NOT EXISTS checkout_request_id VARCHAR;"))
            conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN IF NOT EXISTS phone_number VARCHAR;"))
            conn.execute(text("ALTER TABLE assignment_messages ADD COLUMN IF NOT EXISTS channel VARCHAR DEFAULT 'CLIENT_CHAT';"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS commission_rate FLOAT DEFAULT 50.0;"))
            conn.execute(text("ALTER TABLE assignments ADD COLUMN IF NOT EXISTS staff_commission_pct FLOAT DEFAULT 50.0;"))
            conn.execute(text("ALTER TABLE assignments ADD COLUMN IF NOT EXISTS staff_payout_status VARCHAR DEFAULT 'Unpaid';"))
            conn.execute(text("ALTER TABLE assignments ADD COLUMN IF NOT EXISTS staff_payout_mpesa VARCHAR DEFAULT '';"))
            conn.execute(text("ALTER TABLE assignments ADD COLUMN IF NOT EXISTS staff_paid_at TIMESTAMP;"))