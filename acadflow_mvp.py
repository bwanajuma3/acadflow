import os
import re
import hmac
import threading
import smtplib
from email.message import EmailMessage
import base64
import hashlib
import random
from datetime import datetime, timedelta, timezone

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
import requests
import streamlit as st
import pandas as pd
from fpdf import FPDF
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, ForeignKey, Text, UniqueConstraint, text, func
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ==========================================
# 1. CONFIGURATION, SECURITY & HELPERS
# ==========================================
from dotenv import load_dotenv
load_dotenv()

def get_secret(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default

DATABASE_URL = get_secret("DATABASE_URL", "sqlite:///acadflow_mvp.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# Strip channel_binding parameter if present (prevents psycopg3 SSL errors on Linux)
DATABASE_URL = DATABASE_URL.replace("&channel_binding=require", "").replace("?channel_binding=require", "")

DEMO_MODE = get_secret("DEMO_MODE", "false").strip().lower() == "true"
STORAGE_ROOT = "acadflow_storage"
os.makedirs(STORAGE_ROOT, exist_ok=True)

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

COMPLETED_STATUSES = ["10. Completed", "11. Completed", "11. Closed", "12. Closed"]
CLOSURE_STATUSES = ["10. Completed", "11. Completed", "11. Closed", "12. Closed"]

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# --- Bcrypt Password Hashing with Legacy SHA-256 Auto-Upgrade ---
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_and_upgrade_password(db, user, plain_password: str) -> bool:
    if not user or not plain_password:
        return False
    stored = user.password_hash or ""
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False

    legacy_sha256 = hashlib.sha256(plain_password.encode()).hexdigest()
    if hmac.compare_digest(stored, legacy_sha256):
        user.password_hash = hash_password(plain_password)
        db.commit()
        return True
    return False

# --- Fernet Encryption for Sensitive Tenant API Credentials ---
def _get_cipher() -> Fernet | None:
    raw_key = get_secret("FERNET_SECRET_KEY").strip()
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode("utf-8"))
    except Exception:
        return None

def encrypt_secret(plaintext: str) -> str:
    clean = (plaintext or "").strip()
    if not clean or clean.startswith("enc::"):
        return clean
    cipher = _get_cipher()
    if not cipher:
        return clean
    token = cipher.encrypt(clean.encode("utf-8")).decode("utf-8")
    return f"enc::{token}"

def decrypt_secret(ciphertext: str) -> str:
    val = (ciphertext or "").strip()
    if not val.startswith("enc::"):
        return val
    cipher = _get_cipher()
    if not cipher:
        return ""
    try:
        return cipher.decrypt(val[5:].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""

def normalize_email(email_str: str) -> str:
    return (email_str or "").strip().lower()

def find_user_by_email(db, email_str: str):
    clean = normalize_email(email_str)
    if not clean:
        return None
    return db.query(User).filter(func.lower(User.email) == clean).first()

def compute_file_sha256(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()

def save_uploaded_file(tenant_id: int, assignment_id: int, uploaded_file) -> tuple[str, str]:
    raw_bytes = uploaded_file.getbuffer()
    file_hash = compute_file_sha256(raw_bytes)
    folder = os.path.join(STORAGE_ROOT, f"tenant_{tenant_id}", f"assignment_{assignment_id}")
    os.makedirs(folder, exist_ok=True)
    clean_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(uploaded_file.name))
    safe_name = f"{int(utc_now().timestamp())}_{clean_filename}"
    file_path = os.path.join(folder, safe_name)
    with open(file_path, "wb") as f:
        f.write(raw_bytes)
    return file_path, file_hash

# ==========================================
# 2. MULTI-TENANT SQLALCHEMY MODELS
# ==========================================
class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)  # Starter, Professional, Enterprise
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
    status = Column(String, default="Active")  # Active, Trial, Suspended, Expired
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
    role = Column(String, nullable=False)  # SUPER_ADMIN, TENANT_ADMIN, STAFF, CLIENT
    commission_rate = Column(Float, default=50.0)  # Default % share for STAFF

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
    staff_payout_status = Column(String, default="Unpaid")  # Unpaid, Paid
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
    rating = Column(Integer, nullable=False)  # 1 to 5
    feedback = Column(Text)
    created_at = Column(DateTime, default=utc_now)

class AssignmentDocument(Base):
    __tablename__ = "assignment_documents"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    uploaded_by_name = Column(String, nullable=False)
    doc_category = Column(String, nullable=False)  # CLIENT_BRIEF, STAFF_DRAFT, DELIVERABLE
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
    channel = Column(String, default="CLIENT_CHAT")  # CLIENT_CHAT or INTERNAL_STAFF_ADMIN
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
    """LEDGER 2: Individual client payments to subscribing businesses"""
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
    """LEDGER 1: SaaS Subscription payments paid by businesses to Super Admin"""
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
    status = Column(String, default="Open")  # Open, Resolved
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

# ==========================================
# 3. SUPER ADMIN CASCADE DELETION HELPERS
# ==========================================
def delete_assignment_cascade(db, assignment_id: int):
    """Deletes an assignment and all its child records and uploaded files."""
    docs = db.query(AssignmentDocument).filter_by(assignment_id=assignment_id).all()
    for doc in docs:
        try:
            if doc.file_path and os.path.exists(doc.file_path):
                os.remove(doc.file_path)
        except Exception:
            pass
    db.query(AssignmentDocument).filter_by(assignment_id=assignment_id).delete(synchronize_session=False)
    db.query(AssignmentMessage).filter_by(assignment_id=assignment_id).delete(synchronize_session=False)
    db.query(AssignmentLog).filter_by(assignment_id=assignment_id).delete(synchronize_session=False)
    db.query(ClientPaymentTransaction).filter_by(assignment_id=assignment_id).delete(synchronize_session=False)
    db.query(ClientReview).filter_by(assignment_id=assignment_id).delete(synchronize_session=False)
    db.query(Assignment).filter_by(id=assignment_id).delete(synchronize_session=False)

def delete_client_account(db, client_user_id: int) -> tuple[bool, str]:
    """Deletes a CLIENT user and all assignments/records belonging to that client."""
    client_u = db.query(User).filter_by(id=client_user_id, role="CLIENT").first()
    if not client_u:
        return False, "Client account not found."
    client_assignments = db.query(Assignment).filter_by(client_id=client_user_id).all()
    for a in client_assignments:
        delete_assignment_cascade(db, a.id)
    db.delete(client_u)
    db.commit()
    return True, f"Deleted client '{client_u.full_name}' ({client_u.email}) and {len(client_assignments)} associated project(s)."

def delete_tenant_account(db, tenant_id: int) -> tuple[bool, str]:
    """Deletes a subscribing Tenant workspace, all its projects, services, staff/admins, and detaches clients."""
    t_obj = db.query(Tenant).filter_by(id=tenant_id).first()
    if not t_obj:
        return False, "Tenant workspace not found."
    b_name = t_obj.business_name

    t_assignments = db.query(Assignment).filter_by(tenant_id=tenant_id).all()
    for a in t_assignments:
        delete_assignment_cascade(db, a.id)

    db.query(Service).filter_by(tenant_id=tenant_id).delete(synchronize_session=False)
    db.query(PlatformSubscriptionPayment).filter_by(tenant_id=tenant_id).delete(synchronize_session=False)
    db.query(SupportTicket).filter_by(tenant_id=tenant_id).delete(synchronize_session=False)
    db.query(EmailNotificationLog).filter_by(tenant_id=tenant_id).delete(synchronize_session=False)

    db.query(User).filter(User.tenant_id == tenant_id, User.role == "CLIENT").update({"tenant_id": None}, synchronize_session=False)
    db.query(User).filter(User.tenant_id == tenant_id, User.role.in_(["TENANT_ADMIN", "STAFF"])).delete(synchronize_session=False)

    db.delete(t_obj)
    db.commit()
    return True, f"Deleted tenant workspace '{b_name}' and all associated staff, services, and projects."

# ==========================================
# 4. SEED & CACHED STARTUP INITIALIZATION
# ==========================================
def seed_database():
    db = SessionLocal()
    try:
        if db.query(SubscriptionPlan).count() == 0:
            plans = [
                SubscriptionPlan(name="Starter", monthly_price_kes=3500, annual_price_kes=35000,
                                 max_staff=2, max_active_projects=15,
                                 features_summary="Solo/small practice: Client portal, PDF invoices, 15 active projects."),
                SubscriptionPlan(name="Professional", monthly_price_kes=6500, annual_price_kes=65000,
                                 max_staff=10, max_active_projects=75,
                                 features_summary="Growing consultancy: Team allocation, analytics, 10 staff, 75 active projects."),
                SubscriptionPlan(name="Enterprise", monthly_price_kes=12000, annual_price_kes=120000,
                                 max_staff=100, max_active_projects=1000,
                                 features_summary="Full scale: Unlimited teams, Daraja API automation, priority support."),
            ]
            db.add_all(plans)
            db.commit()

        if db.query(User).count() > 0:
            return

        super_admin_email = normalize_email(get_secret("SUPER_ADMIN_EMAIL", "super@acadflow.co.ke"))
        super_admin_init_pw = get_secret("SUPER_ADMIN_PASSWORD", "admin123")
        super_admin = User(
            tenant_id=None, full_name="Platform Owner", email=super_admin_email,
            password_hash=hash_password(super_admin_init_pw), role="SUPER_ADMIN"
        )
        db.add(super_admin)
        db.commit()

        if not DEMO_MODE:
            return

        t1 = Tenant(
            business_name="Nairobi Research & Thesis Hub (Your Business)", tier="Enterprise", status="Active",
            subscription_ends_at=utc_now() + timedelta(days=365),
            mpesa_paybill="400200", mpesa_account="NRTH-01"
        )
        t2 = Tenant(
            business_name="ScholarAssist Kenya Ltd", tier="Starter", status="Active",
            subscription_ends_at=utc_now() + timedelta(days=30),
            mpesa_paybill="247247", mpesa_account="SCHOLAR-KE"
        )
        db.add_all([t1, t2])
        db.commit()

        users = [
            User(tenant_id=t1.id, full_name="Dr. Kamau Njoroge (Admin)", email="admin@nairobihub.co.ke",
                 password_hash=hash_password("admin123"), role="TENANT_ADMIN", commission_rate=0.0),
            User(tenant_id=t1.id, full_name="Mercy Wanjiku (Senior Statistician)", email="mercy@nairobihub.co.ke",
                 password_hash=hash_password("staff123"), role="STAFF", commission_rate=55.0),
            User(tenant_id=t1.id, full_name="Brian Ochieng (MSc Client)", email="brian@student.ac.ke",
                 password_hash=hash_password("client123"), role="CLIENT"),
            User(tenant_id=t2.id, full_name="Grace Mutua (Admin)", email="admin@scholarassist.co.ke",
                 password_hash=hash_password("admin123"), role="TENANT_ADMIN", commission_rate=0.0),
            User(tenant_id=t2.id, full_name="David Kiprop (PhD Client)", email="david@university.ac.ke",
                 password_hash=hash_password("client123"), role="CLIENT"),
        ]
        db.add_all(users)
        db.commit()

        s1 = Service(tenant_id=t1.id, title="SPSS / R Statistical Data Analysis",
                     description="Quantitative analysis, regression modeling, and APA tables.", base_price_kes=15000, turnaround_days=4)
        s2 = Service(tenant_id=t1.id, title="Research Proposal Development",
                     description="Chapters 1-3 structuring, methodology design, and literature synthesis.", base_price_kes=25000, turnaround_days=7)
        s3 = Service(tenant_id=t2.id, title="Academic Editing & Proofreading",
                     description="Language polishing, citation verification, and formatting.", base_price_kes=8000, turnaround_days=2)
        db.add_all([s1, s2, s3])
        db.commit()

        brian = find_user_by_email(db, "brian@student.ac.ke")
        mercy = find_user_by_email(db, "mercy@nairobihub.co.ke")
        david = find_user_by_email(db, "david@university.ac.ke")

        a1 = Assignment(
            tenant_id=t1.id, ref_number="AF-2026-1001", invoice_number="INV-2026-1001",
            title="Panel Data Regression for SME Credit Access",
            description="Need STATA/R regression analysis on 5-year CBK dataset (300 firms).",
            service_id=s1.id, client_id=brian.id, staff_id=mercy.id,
            status="7. In progress", priority="High",
            quoted_cost_kes=18000, amount_paid_kes=10000,
            staff_commission_pct=55.0,
            deadline=utc_now() + timedelta(days=3)
        )
        a2 = Assignment(
            tenant_id=t2.id, ref_number="AF-2026-2001", invoice_number="INV-2026-2001",
            title="PhD Thesis Chapter 4 Formatting & Citation Audit",
            description="APA 7th edition compliance check for 120-page manuscript.",
            service_id=s3.id, client_id=david.id, staff_id=None,
            status="1. Submitted", priority="Normal",
            quoted_cost_kes=9500, amount_paid_kes=0,
            deadline=utc_now() + timedelta(days=5)
        )
        db.add_all([a1, a2])
        db.commit()

        db.add(AssignmentLog(tenant_id=t1.id, assignment_id=a1.id, actor_name="System",
                             action_note="Assignment submitted and assigned to Mercy Wanjiku."))
        db.add(ClientPaymentTransaction(tenant_id=t1.id, assignment_id=a1.id,
                                        amount_kes=10000, mpesa_code="SK901ABC22",
                                        phone_number="254712345678", status="Verified"))
        db.add_all([
            PlatformSubscriptionPayment(tenant_id=t1.id, plan_name="Enterprise",
                                        amount_kes=120000, mpesa_code="SK892JKL10"),
            PlatformSubscriptionPayment(tenant_id=t2.id, plan_name="Starter",
                                        amount_kes=3500, mpesa_code="SL104MNP92"),
        ])
        db.add(PlatformAnnouncement(
            title="Welcome to AcadFlow",
            message="Automated PDF Quotations/Invoices, M-Pesa STK Push, and Staff Commission Tracking are live!"
        ))
        db.commit()
    finally:
        db.close()

@st.cache_resource
def init_database_once():
    """Runs schema creation, lightweight migrations, and initial seeding once per container startup."""
    Base.metadata.create_all(bind=engine)
    try:
        with engine.begin() as conn:
            if DATABASE_URL.startswith("sqlite"):
                t_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(tenants)")).fetchall()]
                if "subscription_ends_at" not in t_cols:
                    conn.execute(text("ALTER TABLE tenants ADD COLUMN subscription_ends_at DATETIME"))
                cp_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(client_payments)")).fetchall()]
                if "checkout_request_id" not in cp_cols:
                    conn.execute(text("ALTER TABLE client_payments ADD COLUMN checkout_request_id VARCHAR"))
                ps_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(platform_subscriptions)")).fetchall()]
                if "checkout_request_id" not in ps_cols:
                    conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN checkout_request_id VARCHAR"))
                if "phone_number" not in ps_cols:
                    conn.execute(text("ALTER TABLE platform_subscriptions ADD COLUMN phone_number VARCHAR"))
                am_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(assignment_messages)")).fetchall()]
                if "channel" not in am_cols:
                    conn.execute(text("ALTER TABLE assignment_messages ADD COLUMN channel VARCHAR DEFAULT 'CLIENT_CHAT'"))
                u_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
                if "commission_rate" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN commission_rate FLOAT DEFAULT 50.0"))
                a_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(assignments)")).fetchall()]
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
    except Exception:
        pass
    seed_database()
    return True

init_database_once()

# ==========================================
# 5. COMMISSION, TURNAROUND & BACKGROUND EMAIL SERVICES
# ==========================================
def compute_assignment_commission(db, assignment: Assignment) -> dict:
    """Calculates Staff Earnings, Agency Net Profit, and payout eligibility (Paid + Deliverable Approved)."""
    quoted = float(assignment.quoted_cost_kes or 0.0)
    paid = float(assignment.amount_paid_kes or 0.0)
    pct = float(getattr(assignment, "staff_commission_pct", 50.0) or 50.0)
    staff_earnings = round(quoted * (pct / 100.0), 2) if assignment.staff_id else 0.0
    agency_net = round(quoted - staff_earnings, 2)

    is_paid = (quoted > 0) and (paid >= quoted)
    has_deliverable = db.query(AssignmentDocument).filter_by(
        tenant_id=assignment.tenant_id,
        assignment_id=assignment.id,
        doc_category="DELIVERABLE"
    ).count() > 0

    payout_status = getattr(assignment, "staff_payout_status", "Unpaid") or "Unpaid"
    is_payable = bool(assignment.staff_id and is_paid and has_deliverable and payout_status != "Paid")

    return {
        "commission_pct": pct,
        "staff_earnings": staff_earnings,
        "agency_net": agency_net,
        "is_paid": is_paid,
        "has_deliverable": has_deliverable,
        "is_payable": is_payable,
        "payout_status": payout_status,
    }

def get_tenant_public_metrics(db, tenant_id: int) -> dict:
    """Computes public rating, review count, and turnaround time (actual completed or catalogue average)."""
    reviews = db.query(ClientReview).filter_by(tenant_id=tenant_id).all()
    review_count = len(reviews)
    avg_rating = round(sum(r.rating for r in reviews) / review_count, 1) if review_count else 0.0

    completed = db.query(Assignment).filter(
        Assignment.tenant_id == tenant_id,
        Assignment.completed_at.isnot(None),
        Assignment.created_at.isnot(None)
    ).all()

    if completed:
        total_days = sum(max(0.2, (a.completed_at - a.created_at).total_seconds() / 86400.0) for a in completed)
        avg_turnaround_days = round(total_days / len(completed), 1)
        turnaround_label = f"{avg_turnaround_days} days (Verified Avg across {len(completed)} orders)"
    else:
        srvs = db.query(Service).filter_by(tenant_id=tenant_id).all()
        if srvs:
            avg_turnaround_days = round(sum(s.turnaround_days for s in srvs) / len(srvs), 1)
            turnaround_label = f"~{avg_turnaround_days} days (Service Catalogue Est.)"
        else:
            avg_turnaround_days = None
            turnaround_label = "Not yet listed"

    return {
        "avg_rating": avg_rating,
        "review_count": review_count,
        "reviews": reviews,
        "avg_turnaround_days": avg_turnaround_days,
        "turnaround_label": turnaround_label,
        "completed_count": len(completed),
    }

def _send_smtp_background(log_id: int, smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, recipient: str, subject: str, body: str):
    """Runs SMTP network I/O off the Streamlit UI thread and updates the audit log row."""
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"AcadFlow <{smtp_user}>"
        msg["To"] = recipient
        msg.set_content(body)

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, 465, timeout=10) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        final_status = "Sent via SMTP"
    except Exception as exc:
        final_status = f"SMTP Error: {exc}"

    bg_db = SessionLocal()
    try:
        log_row = bg_db.query(EmailNotificationLog).filter_by(id=log_id).first()
        if log_row:
            log_row.delivery_status = final_status
            bg_db.commit()
    except Exception:
        bg_db.rollback()
    finally:
        bg_db.close()

def dispatch_email_notification(db, tenant_id: int | None, recipient: str, subject: str, body: str):
    """Logs the email immediately and dispatches SMTP in a background daemon thread."""
    clean_recipient = (recipient or "").strip()
    if not clean_recipient or "@" not in clean_recipient:
        return

    smtp_host = get_secret("SMTP_HOST").strip()
    smtp_port = int(get_secret("SMTP_PORT", "587"))
    smtp_user = get_secret("SMTP_USER").strip()
    smtp_pass = (get_secret("SMTP_PASSWORD") or get_secret("SMTP_PASS")).strip()

    has_smtp = bool(smtp_host and smtp_user and smtp_pass)
    initial_status = (
        "Sending in background..."
        if has_smtp
        else "Logged (Configure SMTP_HOST, SMTP_USER & SMTP_PASSWORD secrets for live email)"
    )

    log_entry = EmailNotificationLog(
        tenant_id=tenant_id,
        recipient_email=clean_recipient,
        subject=subject,
        body=body,
        delivery_status=initial_status,
    )
    db.add(log_entry)
    db.flush()

    if has_smtp:
        threading.Thread(
            target=_send_smtp_background,
            args=(log_entry.id, smtp_host, smtp_port, smtp_user, smtp_pass, clean_recipient, subject, body),
            daemon=True,
        ).start()

def notify_all_stakeholders(db, tenant_id: int | None, assignment: Assignment | None, subject: str, body: str, include_client: bool = True):
    """Broadcasts notifications to Client (optional for internal chats), Assigned Staff, Tenant Admins, and Super Admin."""
    recipients = set()

    configured_super = get_secret("SUPER_ADMIN_EMAIL")
    if configured_super and "@" in configured_super:
        recipients.add(normalize_email(configured_super))
    for sa in db.query(User).filter_by(role="SUPER_ADMIN").all():
        if sa.email and "@" in sa.email:
            recipients.add(normalize_email(sa.email))

    if tenant_id:
        for admin in db.query(User).filter_by(tenant_id=tenant_id, role="TENANT_ADMIN").all():
            if admin.email and "@" in admin.email:
                recipients.add(normalize_email(admin.email))

    if assignment is not None:
        if include_client and assignment.client_id:
            client_u = db.query(User).filter_by(id=assignment.client_id).first()
            if client_u and client_u.email:
                recipients.add(normalize_email(client_u.email))
        if assignment.staff_id:
            staff_u = db.query(User).filter_by(id=assignment.staff_id).first()
            if staff_u and staff_u.email:
                recipients.add(normalize_email(staff_u.email))

    for email_addr in sorted(recipients):
        dispatch_email_notification(db, tenant_id, email_addr, subject, body)

def generate_financial_pdf(tenant: Tenant, assignment: Assignment, client: User, service_title: str) -> bytes:
    is_quote = assignment.status in ["1. Submitted", "2. Under review", "3. Quotation sent"]
    doc_type = "QUOTATION" if is_quote else "INVOICE / RECEIPT"
    balance = max(0.0, assignment.quoted_cost_kes - assignment.amount_paid_kes)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_fill_color(24, 54, 97)
    pdf.rect(0, 0, 210, 36, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_xy(12, 10)
    pdf.cell(120, 8, tenant.business_name[:42], ln=False)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(65, 8, doc_type, align="R", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(12)
    pdf.cell(120, 6, "Powered by AcadFlow Academic Project Management", ln=False)
    pdf.cell(65, 6, f"Date: {utc_now().strftime('%Y-%m-%d')}", align="R", ln=True)

    pdf.set_text_color(30, 30, 30)
    pdf.set_y(44)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 6, "BILLED TO:", ln=False)
    pdf.cell(95, 6, "DOCUMENT DETAILS:", ln=True)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(95, 6, f"Client: {client.full_name}", ln=False)
    pdf.cell(95, 6, f"Invoice / Quote No: {assignment.invoice_number}", ln=True)
    pdf.cell(95, 6, f"Email: {client.email}", ln=False)
    pdf.cell(95, 6, f"Project Ref: {assignment.ref_number}", ln=True)
    pdf.cell(95, 6, f"Status: {assignment.status}", ln=False)
    pdf.cell(95, 6, f"Due Date: {assignment.deadline.strftime('%Y-%m-%d')}", ln=True)

    pdf.ln(10)
    pdf.set_fill_color(235, 240, 248)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(110, 9, "  Service & Project Description", border=1, fill=True)
    pdf.cell(40, 9, "Category", border=1, align="C", fill=True)
    pdf.cell(40, 9, "Amount (KES)", border=1, align="R", fill=True, ln=True)

    pdf.set_font("Helvetica", "", 10)
    desc_text = f"{assignment.title} ({service_title})"
    pdf.cell(110, 10, f"  {desc_text[:52]}", border=1)
    pdf.cell(40, 10, "Research Support", border=1, align="C")
    pdf.cell(40, 10, f"{assignment.quoted_cost_kes:,.2f}", border=1, align="R", ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(150, 7, "Total Quoted Amount:", align="R")
    pdf.cell(40, 7, f"KES {assignment.quoted_cost_kes:,.2f}", align="R", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(150, 7, "Amount Paid to Date:", align="R")
    pdf.cell(40, 7, f"KES {assignment.amount_paid_kes:,.2f}", align="R", ln=True)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(150, 8, "Balance Due:", align="R")
    pdf.cell(40, 8, f"KES {balance:,.2f}", align="R", ln=True)

    pdf.ln(10)
    pdf.set_fill_color(245, 247, 250)
    pdf.set_draw_color(180, 190, 210)
    pdf.rect(10, pdf.get_y(), 190, 26, "DF")
    pdf.set_xy(14, pdf.get_y() + 4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(180, 6, "M-PESA PAYMENT INSTRUCTIONS:", ln=True)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(180, 6, f"Paybill / Till Number: {tenant.mpesa_paybill}   |   Account Number: {tenant.mpesa_account} (or {assignment.ref_number})", ln=True)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(180, 6, "Submit your M-Pesa confirmation code inside your AcadFlow Client Portal after payment.", ln=True)

    return bytes(pdf.output())

def _execute_daraja_stk(c_key: str, c_sec: str, passkey: str, shortcode: str, phone: str, amount: int, account_ref: str, desc: str, sim_prefix: str = "ws_CO") -> dict:
    clean_phone = phone.strip().replace("+", "")
    if clean_phone.startswith("0") and len(clean_phone) == 10:
        clean_phone = "254" + clean_phone[1:]

    daraja_env = get_secret("DARAJA_ENV", "sandbox").strip().lower()
    if not (c_key and c_sec and passkey) or c_key.startswith("YOUR_"):
        sim_id = f"{sim_prefix}_{utc_now().strftime('%d%m%Y%H%M%S')}_{random.randint(1000,9999)}"
        return {
            "mode": "SANDBOX_SIMULATION",
            "CheckoutRequestID": sim_id,
            "CustomerMessage": f"Simulated STK Push sent to {clean_phone} for KES {amount:,.0f} (CheckoutID: {sim_id})."
        }
    try:
        base_api = "https://api.safaricom.co.ke" if daraja_env == "production" else "https://sandbox.safaricom.co.ke"
        auth_url = f"{base_api}/oauth/v1/generate?grant_type=client_credentials"
        auth_res = requests.get(auth_url, auth=(c_key, c_sec), timeout=10)
        access_token = auth_res.json().get("access_token")
        timestamp = utc_now().strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        stk_url = f"{base_api}/mpesa/stkpush/v1/processrequest"

        callback_url = get_secret("DARAJA_CALLBACK_URL", "https://acadflow-webhook.onrender.com/api/mpesa/callback").strip()
        webhook_token = get_secret("DARAJA_WEBHOOK_TOKEN").strip()
        if webhook_token and "token=" not in callback_url:
            sep = "&" if "?" in callback_url else "?"
            callback_url = f"{callback_url}{sep}token={webhook_token}"

        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": clean_phone,
            "PartyB": shortcode,
            "PhoneNumber": clean_phone,
            "CallBackURL": callback_url,
            "AccountReference": account_ref[:12],
            "TransactionDesc": desc
        }
        res = requests.post(stk_url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        return {"mode": "DARAJA_API", **res.json()}
    except Exception as exc:
        return {"mode": "DARAJA_API", "CustomerMessage": f"Daraja Error: {exc}"}

def trigger_daraja_stk_push(tenant: Tenant, phone: str, amount: int, account_ref: str) -> dict:
    c_key = decrypt_secret(tenant.daraja_consumer_key) or get_secret("DARAJA_CONSUMER_KEY")
    c_sec = decrypt_secret(tenant.daraja_consumer_secret) or get_secret("DARAJA_CONSUMER_SECRET")
    passkey = decrypt_secret(tenant.daraja_passkey) or get_secret("DARAJA_PASSKEY")
    shortcode = get_secret("DARAJA_SHORTCODE") or tenant.mpesa_paybill
    return _execute_daraja_stk(c_key, c_sec, passkey, shortcode, phone, amount, account_ref, "AcadFlow Payment", "ws_CO")

def trigger_platform_subscription_stk(phone: str, amount: int, account_ref: str) -> dict:
    c_key = get_secret("DARAJA_CONSUMER_KEY").strip()
    c_sec = get_secret("DARAJA_CONSUMER_SECRET").strip()
    passkey = get_secret("DARAJA_PASSKEY").strip()
    shortcode = get_secret("DARAJA_SHORTCODE", "174379").strip()
    return _execute_daraja_stk(c_key, c_sec, passkey, shortcode, phone, amount, account_ref, "AcadFlow Subscription", "ws_CO_SUB")

# ==========================================
# 6. STREAMLIT APP & SESSION LIFECYCLE
# ==========================================
st.set_page_config(page_title="AcadFlow | Academic Project SaaS", page_icon="🎓", layout="wide")

if "user_id" not in st.session_state:
    st.session_state.user_id = None

db = SessionLocal()

try:
    def get_current_user():
        if st.session_state.user_id:
            return db.query(User).filter(User.id == st.session_state.user_id).first()
        return None

    current_user = get_current_user()

    # ==========================================
    # 7. PUBLIC PORTAL (LOGIN + REGISTRATION)
    # ==========================================
    if not current_user:
        st.title("🎓 AcadFlow")
        st.caption("From Submission to Completion — Multi-Tenant Academic & Research Project Management SaaS")
        st.divider()

        if DEMO_MODE:
            col_auth, col_demo = st.columns([1.3, 1])
        else:
            col_auth = st.container()

        with col_auth:
            auth_tab1, auth_tab2, auth_tab3 = st.tabs(["🔐 Sign In", "🎓 Client Sign-Up", "🏢 Register a Business"])

            with auth_tab1:
                email = st.text_input("Email Address", key="login_email")
                password = st.text_input("Password", type="password", key="login_pass")
                if st.button("Log In", type="primary", use_container_width=True):
                    user = find_user_by_email(db, email)
                    if user and verify_and_upgrade_password(db, user, password):
                        st.session_state.user_id = user.id
                        st.rerun()
                    else:
                        st.error("Invalid email or password.")

            with auth_tab2:
                st.subheader("Create a Client Account")
                st.caption("Once registered, you can submit projects to any verified consultancy on AcadFlow.")
                active_tenants = db.query(Tenant).filter(Tenant.status.in_(["Active", "Trial"])).all()
                with st.form("client_reg_form"):
                    default_tid = active_tenants[0].id if active_tenants else None
                    if active_tenants:
                        t_map = {t.business_name: t.id for t in active_tenants}
                        chosen_biz = st.selectbox("Primary Consultancy (You can switch anytime per project)", list(t_map.keys()))
                        default_tid = t_map[chosen_biz]
                    c_name = st.text_input("Your Full Name")
                    c_email = st.text_input("Email Address")
                    c_pass = st.text_input("Create Password (min 6 chars)", type="password")
                    if st.form_submit_button("Register as Client", type="primary"):
                        clean_c_email = normalize_email(c_email)
                        if not (c_name.strip() and clean_c_email and c_pass):
                            st.error("Please fill in your full name, email address, and password.")
                        elif len(c_pass) < 6:
                            st.error("Password must be at least 6 characters.")
                        elif find_user_by_email(db, clean_c_email):
                            st.error("An account with this email already exists.")
                        else:
                            new_client = User(
                                tenant_id=default_tid,
                                full_name=c_name.strip(),
                                email=clean_c_email,
                                password_hash=hash_password(c_pass),
                                role="CLIENT"
                            )
                            db.add(new_client)
                            db.commit()
                            notify_all_stakeholders(
                                db, default_tid, None,
                                f"[AcadFlow] New Client Registered: {new_client.full_name}",
                                f"Client {new_client.full_name} ({new_client.email}) registered on AcadFlow."
                            )
                            dispatch_email_notification(
                                db, default_tid, new_client.email,
                                "Welcome to AcadFlow",
                                f"Hello {new_client.full_name}, your AcadFlow client account is active. You can compare consultancies and submit projects anytime."
                            )
                            db.commit()
                            st.session_state.user_id = new_client.id
                            st.rerun()

            with auth_tab3:
                st.subheader("Start Your 14-Day Free Trial")
                plans = db.query(SubscriptionPlan).all()
                with st.form("biz_reg_form"):
                    biz_name = st.text_input("Business / Consultancy Name")
                    biz_tier = st.selectbox("Select Plan", [p.name for p in plans])
                    adm_name = st.text_input("Administrator Full Name")
                    adm_email = st.text_input("Work Email")
                    adm_pass = st.text_input("Password (min 6 chars)", type="password")
                    if st.form_submit_button("Create Business Workspace", type="primary"):
                        clean_adm_email = normalize_email(adm_email)
                        if not (biz_name.strip() and adm_name.strip() and clean_adm_email and adm_pass):
                            st.error("Please fill in all fields to register your workspace.")
                        elif len(adm_pass) < 6:
                            st.error("Password must be at least 6 characters.")
                        elif db.query(Tenant).filter(func.lower(Tenant.business_name) == biz_name.strip().lower()).first():
                            st.error("Business name already registered.")
                        elif find_user_by_email(db, clean_adm_email):
                            st.error("Email already registered.")
                        else:
                            new_tenant = Tenant(business_name=biz_name.strip(), tier=biz_tier, status="Trial")
                            db.add(new_tenant)
                            db.commit()
                            new_admin = User(
                                tenant_id=new_tenant.id,
                                full_name=adm_name.strip(),
                                email=clean_adm_email,
                                password_hash=hash_password(adm_pass),
                                role="TENANT_ADMIN",
                                commission_rate=0.0
                            )
                            db.add(new_admin)
                            db.commit()
                            notify_all_stakeholders(
                                db, new_tenant.id, None,
                                f"[AcadFlow] New Business Registered: {new_tenant.business_name} ({biz_tier})",
                                f"Workspace '{new_tenant.business_name}' was created by {new_admin.full_name} ({new_admin.email}) on the {biz_tier} tier."
                            )
                            db.commit()
                            st.session_state.user_id = new_admin.id
                            st.rerun()

        if DEMO_MODE:
            with col_demo:
                st.info("⚡ **Prototype Quick-Login (Test Multi-Tenancy & Roles)**")
                demo_accounts = [
                    ("👑 Super Admin (Platform Owner)", "super@acadflow.co.ke"),
                    ("🏢 Tenant 1 Admin (Nairobi Research Hub)", "admin@nairobihub.co.ke"),
                    ("🔬 Tenant 1 Staff (Mercy - Statistician)", "mercy@nairobihub.co.ke"),
                    ("🎓 Tenant 1 Client (Brian - MSc Student)", "brian@student.ac.ke"),
                    ("🏢 Tenant 2 Admin (ScholarAssist Kenya)", "admin@scholarassist.co.ke"),
                ]
                for label, demo_email in demo_accounts:
                    if st.button(label, use_container_width=True):
                        u = find_user_by_email(db, demo_email)
                        if u:
                            st.session_state.user_id = u.id
                            st.rerun()
        st.stop()

    # ==========================================
    # 8. AUTHENTICATED SIDEBAR, SUBSCRIPTION EXPIRY CHECK & ACCOUNT SECURITY
    # ==========================================
    tenant = db.query(Tenant).filter_by(id=current_user.tenant_id).first() if current_user.tenant_id else None

    # Automatic Trial & Paid Subscription Expiration Enforcement
    if tenant and tenant.status in ["Active", "Trial"]:
        now = utc_now()
        if tenant.status == "Trial" and tenant.trial_ends_at and tenant.trial_ends_at < now:
            tenant.status = "Expired"
            db.commit()
        elif tenant.status == "Active" and getattr(tenant, "subscription_ends_at", None) and tenant.subscription_ends_at < now:
            tenant.status = "Expired"
            db.commit()

    with st.sidebar:
        st.markdown("## 🎓 AcadFlow")
        if current_user.role == "CLIENT":
            st.info("**Mode:** Client Marketplace Portal\n\nSubmit projects to any active consultancy.")
        elif tenant:
            expiry_str = ""
            if tenant.status == "Trial" and tenant.trial_ends_at:
                expiry_str = f"\n\n**Trial Ends:** {tenant.trial_ends_at.strftime('%Y-%m-%d')}"
            elif tenant.status == "Active" and getattr(tenant, "subscription_ends_at", None):
                expiry_str = f"\n\n**Renews:** {tenant.subscription_ends_at.strftime('%Y-%m-%d')}"
            st.success(f"**Workspace:** {tenant.business_name}\n\n**Plan:** {tenant.tier} ({tenant.status}){expiry_str}")
        else:
            st.warning("**Mode:** Central Platform Control (Super Admin)")

        role_extra = f" | Default Rate: `{getattr(current_user, 'commission_rate', 50.0):.0f}%`" if current_user.role == "STAFF" else ""
        st.markdown(f"**User:** {current_user.full_name}  \n**Email:** `{current_user.email}`  \n**Role:** `{current_user.role}`{role_extra}")
        st.divider()

        with st.expander("🔑 Account & Password Security"):
            with st.form("change_own_pass_form"):
                new_email_val = st.text_input("Login Email", value=current_user.email)
                curr_pw = st.text_input("Current Password", type="password")
                new_pw = st.text_input("New Password (min 6 chars)", type="password")
                if st.form_submit_button("Update Credentials"):
                    if not verify_and_upgrade_password(db, current_user, curr_pw):
                        st.error("Current password is incorrect.")
                    elif new_pw and len(new_pw) < 6:
                        st.error("New password must be at least 6 characters.")
                    else:
                        clean_em = normalize_email(new_email_val)
                        existing_em_user = find_user_by_email(db, clean_em)
                        if clean_em != normalize_email(current_user.email) and existing_em_user:
                            st.error("That email is already in use.")
                        else:
                            current_user.email = clean_em
                            if new_pw:
                                current_user.password_hash = hash_password(new_pw)
                            db.commit()
                            st.success("Account credentials updated!")

        if st.button("🚪 Sign Out", use_container_width=True):
            st.session_state.user_id = None
            st.rerun()

    # ==========================================
    # 9. SUPER ADMINISTRATOR DASHBOARD
    # ==========================================
    if current_user.role == "SUPER_ADMIN":
        st.title("👑 Super Administrator Control Center")
        tenants = db.query(Tenant).all()
        all_clients = db.query(User).filter_by(role="CLIENT").all()
        sub_payments = db.query(PlatformSubscriptionPayment).all()
        plans = db.query(SubscriptionPlan).all()
        total_saas_rev = sum(p.amount_kes for p in sub_payments if p.status == "Verified")

        all_completed_global = db.query(Assignment).filter(Assignment.completed_at.isnot(None)).all()
        if all_completed_global:
            global_ta = round(sum(max(0.2, (a.completed_at - a.created_at).total_seconds() / 86400.0) for a in all_completed_global) / len(all_completed_global), 1)
            global_ta_str = f"{global_ta} days"
        else:
            all_srvs = db.query(Service).all()
            global_ta_str = f"~{round(sum(s.turnaround_days for s in all_srvs) / len(all_srvs), 1)} days" if all_srvs else "N/A"

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Businesses", len(tenants))
        m2.metric("Active / Trial", sum(1 for t in tenants if t.status in ["Active", "Trial"]))
        m3.metric("Registered Clients", len(all_clients))
        m4.metric("Verified SaaS Revenue", f"KES {total_saas_rev:,.0f}")
        m5.metric("Total Projects", db.query(Assignment).count())
        m6.metric("Avg Turnaround", global_ta_str)

        sa_tabs = st.tabs([
            "🏢 Subscribing Businesses",
            "🎓 Client Accounts",
            "📦 Subscription Packages",
            "💳 SaaS Revenue (Ledger 1)",
            "📢 Announcements & Support",
            "📧 Platform Email Log"
        ])

        with sa_tabs[0]:
            if not tenants:
                st.info("No subscribing businesses registered yet.")
            for t in tenants:
                t_assign_count = db.query(Assignment).filter_by(tenant_id=t.id).count()
                t_staff_count = db.query(User).filter(User.tenant_id == t.id, User.role.in_(["TENANT_ADMIN", "STAFF"])).count()
                t_metrics = get_tenant_public_metrics(db, t.id)
                rating_str = f"{t_metrics['avg_rating']} ⭐ ({t_metrics['review_count']})" if t_metrics["review_count"] else "No reviews"
                sub_exp_lbl = t.subscription_ends_at.strftime("%Y-%m-%d") if getattr(t, "subscription_ends_at", None) else "Not set"
                with st.expander(f"{t.business_name} — [{t.tier}] — Status: {t.status} | ⏱️ {t_metrics['turnaround_label']} | {rating_str}", expanded=True):
                    c1, c2, c3, c4 = st.columns([2, 1.3, 1.1, 1.1])
                    c1.write(f"**Paybill/Till:** {t.mpesa_paybill} ({t.mpesa_account})  \n**Public Turnaround:** {t_metrics['turnaround_label']}  \n**Subscription Valid Until:** {sub_exp_lbl}")
                    c2.write(f"**Staff:** {t_staff_count} | **Projects:** {t_assign_count}  \n**Rating:** {rating_str}")
                    tier_names = [p.name for p in plans]
                    tier_idx = tier_names.index(t.tier) if t.tier in tier_names else 0
                    new_tier = c3.selectbox("Subscription Tier", tier_names, index=tier_idx, key=f"tr_{t.id}")
                    status_opts = ["Active", "Trial", "Suspended", "Expired"]
                    st_idx = status_opts.index(t.status) if t.status in status_opts else 0
                    new_status = c4.selectbox("Account Status", status_opts, index=st_idx, key=f"st_{t.id}")
                    if new_status != t.status or new_tier != t.tier:
                        t.status = new_status
                        t.tier = new_tier
                        if new_status == "Active" and (not t.subscription_ends_at or t.subscription_ends_at < utc_now()):
                            t.subscription_ends_at = utc_now() + timedelta(days=30)
                        notify_all_stakeholders(
                            db, t.id, None,
                            f"[AcadFlow] Workspace Updated: {t.business_name} ({new_tier} - {new_status})",
                            f"Workspace '{t.business_name}' subscription tier is now {new_tier} and status is {new_status}."
                        )
                        db.commit()
                        st.rerun()

                    t_users = db.query(User).filter_by(tenant_id=t.id).all()
                    if t_users:
                        u_col1, u_col2, u_col3 = st.columns([2, 1.5, 1])
                        u_map = {f"{u.full_name} ({u.email} - {u.role})": u for u in t_users}
                        sel_u_lbl = u_col1.selectbox("Workspace User", list(u_map.keys()), key=f"su_sel_{t.id}")
                        reset_pw_val = u_col2.text_input("New Password (min 6 chars)", type="password", key=f"su_pw_{t.id}")
                        if u_col3.button("Reset User Password", key=f"su_btn_{t.id}"):
                            if len(reset_pw_val) < 6:
                                st.error("Password must be at least 6 characters.")
                            else:
                                u_map[sel_u_lbl].password_hash = hash_password(reset_pw_val)
                                db.commit()
                                st.success("User password reset!")

                    st.divider()
                    del_c1, del_c2 = st.columns([2.5, 1])
                    confirm_del_t = del_c1.checkbox(
                        f"Confirm permanent deletion of '{t.business_name}' (removes {t_staff_count} staff/admin user(s) & {t_assign_count} project(s))",
                        key=f"chk_del_t_{t.id}"
                    )
                    if del_c2.button("🗑️ Delete Tenant Workspace", key=f"btn_del_t_{t.id}", disabled=not confirm_del_t):
                        ok, msg = delete_tenant_account(db, t.id)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

        with sa_tabs[1]:
            st.subheader("Manage Registered Client Accounts")
            if not all_clients:
                st.info("No client accounts registered yet.")
            else:
                tenant_lookup = {t.id: t.business_name for t in tenants}
                client_rows = []
                for cu in all_clients:
                    c_proj_cnt = db.query(Assignment).filter_by(client_id=cu.id).count()
                    c_total_paid = sum(a.amount_paid_kes for a in db.query(Assignment).filter_by(client_id=cu.id).all())
                    client_rows.append({
                        "ID": cu.id,
                        "Full Name": cu.full_name,
                        "Email": cu.email,
                        "Primary Consultancy": tenant_lookup.get(cu.tenant_id, "Marketplace / Unassigned"),
                        "Projects": c_proj_cnt,
                        "Total Paid (KES)": c_total_paid
                    })
                st.dataframe(pd.DataFrame(client_rows), use_container_width=True)

                st.divider()
                st.markdown("### 🔑 Reset Password or Delete Client Account")
                c_map = {f"{cu.full_name} ({cu.email}) [ID #{cu.id}]": cu for cu in all_clients}
                sel_client_lbl = st.selectbox("Select Client Account", list(c_map.keys()), key="sa_sel_client")
                target_client = c_map[sel_client_lbl]
                c_proj_count = db.query(Assignment).filter_by(client_id=target_client.id).count()

                cc1, cc2 = st.columns(2)
                with cc1:
                    st.markdown("**Reset Client Password**")
                    new_c_pw = st.text_input("New Client Password (min 6 chars)", type="password", key="sa_client_pw")
                    if st.button("Update Client Password", key="sa_client_pw_btn"):
                        if len(new_c_pw) < 6:
                            st.error("Password must be at least 6 characters.")
                        else:
                            target_client.password_hash = hash_password(new_c_pw)
                            db.commit()
                            st.success(f"Password updated for {target_client.full_name}!")

                with cc2:
                    st.markdown("**Permanently Delete Client Account**")
                    confirm_del_c = st.checkbox(
                        f"Confirm deletion of '{target_client.full_name}' and {c_proj_count} associated project(s)",
                        key="sa_chk_del_client"
                    )
                    if st.button("🗑️ Delete Client Account", type="primary", key="sa_btn_del_client", disabled=not confirm_del_c):
                        ok, msg = delete_client_account(db, target_client.id)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

        with sa_tabs[2]:
            st.subheader("Configure AcadFlow SaaS Tiers & Usage Limits")
            for p in plans:
                with st.form(f"plan_form_{p.id}"):
                    st.markdown(f"### {p.name} Tier")
                    pc1, pc2, pc3, pc4 = st.columns(4)
                    m_pr = pc1.number_input("Monthly (KES)", value=float(p.monthly_price_kes), step=500.0)
                    a_pr = pc2.number_input("Annual (KES)", value=float(p.annual_price_kes), step=1000.0)
                    mx_st = pc3.number_input("Max Staff", value=int(p.max_staff), step=1)
                    mx_pj = pc4.number_input("Max Active Projects", value=int(p.max_active_projects), step=5)
                    if st.form_submit_button(f"Update {p.name}"):
                        p.monthly_price_kes = m_pr
                        p.annual_price_kes = a_pr
                        p.max_staff = mx_st
                        p.max_active_projects = mx_pj
                        db.commit()
                        st.success(f"{p.name} package updated!")

        with sa_tabs[3]:
            df_subs = pd.DataFrame([{
                "Tenant ID": p.tenant_id, "Plan": p.plan_name,
                "Amount (KES)": p.amount_kes, "M-Pesa Receipt": p.mpesa_code,
                "Checkout ID": getattr(p, "checkout_request_id", ""),
                "Status": p.status, "Date": p.paid_at.strftime("%Y-%m-%d %H:%M")
            } for p in sub_payments])
            st.dataframe(df_subs, use_container_width=True)

            pending_subs = [p for p in sub_payments if p.status == "Pending Verification"]
            if pending_subs:
                st.markdown("**Pending Subscription Payments**")
                for ps in pending_subs:
                    sc1, sc2 = st.columns([3, 1])
                    sc1.write(f"• Tenant #{ps.tenant_id} | Plan: **{ps.plan_name}** | KES {ps.amount_kes:,.0f} | Ref: `{ps.mpesa_code}`")
                    if sc2.button("✅ Approve & Activate", key=f"sa_sub_ver_{ps.id}"):
                        ps.status = "Verified"
                        t_obj = db.query(Tenant).filter_by(id=ps.tenant_id).first()
                        plan_obj = db.query(SubscriptionPlan).filter_by(name=ps.plan_name).first()
                        days_to_add = 365 if (plan_obj and ps.amount_kes >= float(plan_obj.annual_price_kes)) else 30
                        if t_obj:
                            base_dt = t_obj.subscription_ends_at if (t_obj.subscription_ends_at and t_obj.subscription_ends_at > utc_now()) else utc_now()
                            t_obj.tier = ps.plan_name
                            t_obj.status = "Active"
                            t_obj.subscription_ends_at = base_dt + timedelta(days=days_to_add)
                            notify_all_stakeholders(
                                db, t_obj.id, None,
                                f"[AcadFlow] Subscription Activated: {t_obj.business_name} ({ps.plan_name})",
                                f"Your {ps.plan_name} subscription (KES {ps.amount_kes:,.0f}) has been verified and activated until {t_obj.subscription_ends_at.strftime('%Y-%m-%d')}."
                            )
                        db.commit()
                        st.rerun()

        with sa_tabs[4]:
            col_ann, col_sup = st.columns(2)
            with col_ann:
                st.subheader("Broadcast Platform Announcement")
                with st.form("ann_form"):
                    a_title = st.text_input("Announcement Title")
                    a_msg = st.text_area("Message to All Subscribing Businesses")
                    if st.form_submit_button("Publish Announcement", type="primary") and a_title and a_msg:
                        db.add(PlatformAnnouncement(title=a_title.strip(), message=a_msg.strip(), is_active=True))
                        db.commit()
                        st.success("Announcement published!")
                        st.rerun()
                for ann in db.query(PlatformAnnouncement).order_by(PlatformAnnouncement.created_at.desc()).all():
                    st.info(f"**{ann.title}** ({'Active' if ann.is_active else 'Archived'}) — {ann.message}")

            with col_sup:
                st.subheader("Subscriber Support Tickets")
                tickets = db.query(SupportTicket).order_by(SupportTicket.created_at.desc()).all()
                if not tickets:
                    st.caption("No support tickets submitted yet.")
                for tk in tickets:
                    with st.expander(f"[{tk.status}] {tk.business_name}: {tk.subject}", expanded=(tk.status == "Open")):
                        st.write(tk.description)
                        rep = st.text_input("Super Admin Response", value=tk.admin_reply, key=f"tk_rep_{tk.id}")
                        if st.button("Mark Resolved & Reply", key=f"tk_btn_{tk.id}"):
                            tk.admin_reply = rep
                            tk.status = "Resolved"
                            notify_all_stakeholders(
                                db, tk.tenant_id, None,
                                f"[AcadFlow Support] Ticket Resolved: {tk.subject}",
                                f"Your support ticket '{tk.subject}' has been resolved.\n\nReply: {rep}"
                            )
                            db.commit()
                            st.rerun()

        with sa_tabs[5]:
            st.subheader("System Email Notification Audit Log")
            logs = db.query(EmailNotificationLog).order_by(EmailNotificationLog.sent_at.desc()).limit(100).all()
            st.dataframe(pd.DataFrame([{
                "To": l.recipient_email, "Subject": l.subject, "Status": l.delivery_status,
                "Sent At": l.sent_at.strftime("%Y-%m-%d %H:%M")
            } for l in logs]), use_container_width=True)

    # ==========================================
    # 10. WORKSPACE / CLIENT MARKETPLACE (ADMIN / STAFF / CLIENT)
    # ==========================================
    else:
        tid = current_user.tenant_id

        def render_subscription_stk_box(target_tenant: Tenant):
            st.subheader("📲 Upgrade / Renew AcadFlow Subscription (M-Pesa STK Push)")
            all_plans = db.query(SubscriptionPlan).all()
            billing_cycle = st.radio("Billing Cycle", ["Monthly", "Annual (Save ~17%)"], horizontal=True, key="sub_billing_cycle")
            is_annual = billing_cycle.startswith("Annual")

            plan_opts = {
                f"{p.name} — KES {(p.annual_price_kes if is_annual else p.monthly_price_kes):,.0f}/{'yr' if is_annual else 'mo'} ({p.max_staff} staff, {p.max_active_projects} projects)": p
                for p in all_plans
            }
            chosen_p_lbl = st.selectbox("Select Subscription Plan", list(plan_opts.keys()), key="sub_plan_sel")
            sub_phone = st.text_input("M-Pesa Phone Number (2547XXXXXXXX or 07XXXXXXXX)", placeholder="0712345678", key="sub_stk_ph")

            col_btn1, col_btn2 = st.columns([1.4, 1])
            with col_btn1:
                if st.button("📲 Send M-Pesa STK Push", type="primary", key="sub_stk_btn", use_container_width=True):
                    if not sub_phone.strip():
                        st.error("Please enter a valid M-Pesa phone number.")
                    else:
                        chosen_p = plan_opts[chosen_p_lbl]
                        charge_amount = chosen_p.annual_price_kes if is_annual else chosen_p.monthly_price_kes
                        res = trigger_platform_subscription_stk(sub_phone, int(charge_amount), f"SUB-{target_tenant.id}")
                        chk_id = res.get("CheckoutRequestID", "")
                        if chk_id:
                            db.add(PlatformSubscriptionPayment(
                                tenant_id=target_tenant.id,
                                checkout_request_id=chk_id,
                                plan_name=chosen_p.name,
                                amount_kes=charge_amount,
                                mpesa_code=f"PENDING-{chk_id[-8:]}",
                                phone_number=sub_phone.strip(),
                                status="Pending Verification"
                            ))
                            notify_all_stakeholders(
                                db, target_tenant.id, None,
                                f"[AcadFlow] Subscription STK Push Initiated: {target_tenant.business_name} ({chosen_p.name})",
                                f"{target_tenant.business_name} initiated an M-Pesa STK Push of KES {charge_amount:,.0f} ({billing_cycle}) for the {chosen_p.name} plan (CheckoutID: {chk_id})."
                            )
                            db.commit()
                        st.info(res.get("CustomerMessage", "STK Push sent! Enter your M-Pesa PIN to activate your plan automatically."))
            with col_btn2:
                if st.button("🔄 Refresh Plan Status", key="sub_refresh_btn", use_container_width=True):
                    st.rerun()

            recent_subs = db.query(PlatformSubscriptionPayment).filter_by(tenant_id=target_tenant.id).order_by(PlatformSubscriptionPayment.paid_at.desc()).limit(4).all()
            if recent_subs:
                st.markdown("**Recent Subscription Transactions:**")
                for sp in recent_subs:
                    chk_display = f" | CheckoutID: `{sp.checkout_request_id}`" if getattr(sp, "checkout_request_id", "") else ""
                    st.caption(f"• **{sp.plan_name}** | KES {sp.amount_kes:,.0f} | Status: `{sp.status}` | Ref: `{sp.mpesa_code}`{chk_display}")

        if current_user.role in ["TENANT_ADMIN", "STAFF"] and tenant and tenant.status in ["Suspended", "Expired"]:
            st.error(f"⛔ This workspace is currently **{tenant.status}**. Complete a subscription payment below to reactivate automatically.")
            if current_user.role == "TENANT_ADMIN":
                render_subscription_stk_box(tenant)
            else:
                st.info("Please ask your Workspace Administrator to renew the AcadFlow subscription.")
            st.stop()

        active_anns = db.query(PlatformAnnouncement).filter_by(is_active=True).order_by(PlatformAnnouncement.created_at.desc()).all()
        for ann in active_anns[:1]:
            st.info(f"📢 **{ann.title}:** {ann.message}")

        if current_user.role == "CLIENT":
            st.title("🎓 Client Project Hub & Consultancy Directory")
            assignments_q = db.query(Assignment).filter(Assignment.client_id == current_user.id)
        else:
            st.title(f"🏢 {tenant.business_name}")
            assignments_q = db.query(Assignment).filter(Assignment.tenant_id == tid)
            if current_user.role == "STAFF":
                assignments_q = assignments_q.filter(Assignment.staff_id == current_user.id)

        assignments = assignments_q.order_by(Assignment.created_at.desc()).all()

        current_plan = db.query(SubscriptionPlan).filter_by(name=tenant.tier).first() if tenant else None
        all_tenant_assignments = db.query(Assignment).filter(Assignment.tenant_id == tid).all() if tid else []
        active_tenant_projects_count = sum(1 for a in all_tenant_assignments if a.status not in COMPLETED_STATUSES)
        staff_members = db.query(User).filter(User.tenant_id == tid, User.role.in_(["TENANT_ADMIN", "STAFF"])).all() if tid else []
        services = db.query(Service).filter(Service.tenant_id == tid).all() if tid else []

        if current_user.role == "TENANT_ADMIN":
            tabs = st.tabs([
                "📊 Projects & Delivery",
                "📈 Analytics & Reviews",
                "📚 Service Catalogue",
                "💰 Financials & M-Pesa",
                "👥 Team, Plan & Support"
            ])
        elif current_user.role == "STAFF":
            tabs = st.tabs(["📋 My Assigned Projects"])
        else:
            tabs = st.tabs(["📂 My Projects (All Consultancies)", "➕ Submit New Request & Compare Consultancies"])

        # --- TAB 1: ASSIGNMENTS, PDF INVOICES, M-PESA, FILES, MESSAGING & REVIEWS ---
        with tabs[0]:
            if current_user.role == "TENANT_ADMIN":
                total_rev = sum(a.amount_paid_kes for a in assignments)
                outstanding = sum(max(0, a.quoted_cost_kes - a.amount_paid_kes) for a in assignments)
                overdue_cnt = sum(1 for a in assignments if a.deadline < utc_now() and a.status not in COMPLETED_STATUSES)
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Total Projects", len(assignments))
                c2.metric("Active Projects", f"{active_tenant_projects_count} / {current_plan.max_active_projects if current_plan else '∞'}")
                c3.metric("Overdue", overdue_cnt)
                c4.metric("Collected Revenue", f"KES {total_rev:,.0f}")
                c5.metric("Outstanding", f"KES {outstanding:,.0f}")
                st.divider()

            elif current_user.role == "STAFF":
                earned_payable = 0.0
                pending_pipeline = 0.0
                total_paid_out = 0.0
                for sa_item in assignments:
                    c_info = compute_assignment_commission(db, sa_item)
                    if c_info["payout_status"] == "Paid":
                        total_paid_out += c_info["staff_earnings"]
                    elif c_info["is_payable"]:
                        earned_payable += c_info["staff_earnings"]
                    else:
                        pending_pipeline += c_info["staff_earnings"]

                with st.container(border=True):
                    st.markdown("#### 💵 My Earnings & Payouts")
                    sc1, sc2, sc3 = st.columns(3)
                    sc1.metric("Earned & Ready for Payout", f"KES {earned_payable:,.0f}")
                    sc2.metric("In-Progress / Awaiting Client Pay", f"KES {pending_pipeline:,.0f}")
                    sc3.metric("Total Paid Out", f"KES {total_paid_out:,.0f}")
                st.divider()

            # Filter & Search Bar for Scalability
            if assignments:
                f_col1, f_col2 = st.columns([2, 1.2])
                search_q = f_col1.text_input("🔍 Search by Ref #, Invoice #, or Title", placeholder="e.g. AF-2026 or Regression", key="proj_search").strip().lower()
                filter_mode = f_col2.selectbox("Filter View", ["All Projects", "Active Only", "Awaiting Payment", "Completed / Closed"], key="proj_filter")

                filtered_assignments = []
                for item in assignments:
                    if search_q and search_q not in item.ref_number.lower() and search_q not in item.invoice_number.lower() and search_q not in item.title.lower():
                        continue
                    if filter_mode == "Active Only" and item.status in COMPLETED_STATUSES:
                        continue
                    elif filter_mode == "Awaiting Payment" and (item.amount_paid_kes >= item.quoted_cost_kes or item.quoted_cost_kes <= 0):
                        continue
                    elif filter_mode == "Completed / Closed" and item.status not in COMPLETED_STATUSES:
                        continue
                    filtered_assignments.append(item)
            else:
                filtered_assignments = []

            if not filtered_assignments:
                st.info("No projects match your current view.")
            else:
                for a in filtered_assignments:
                    proj_tenant = db.query(Tenant).filter_by(id=a.tenant_id).first()
                    client_obj = db.query(User).filter_by(id=a.client_id).first()
                    staff_obj = db.query(User).filter_by(id=a.staff_id).first() if a.staff_id else None
                    srv_obj = db.query(Service).filter_by(id=a.service_id).first()
                    balance = max(0.0, a.quoted_cost_kes - a.amount_paid_kes)

                    all_proj_docs = db.query(AssignmentDocument).filter_by(tenant_id=a.tenant_id, assignment_id=a.id).all()
                    client_deliverables = [d for d in all_proj_docs if d.doc_category == "DELIVERABLE"]
                    staff_drafts = [d for d in all_proj_docs if d.doc_category == "STAFF_DRAFT"]
                    comm_info = compute_assignment_commission(db, a)
                    is_fully_paid = comm_info["is_paid"]
                    has_final_deliverable = comm_info["has_deliverable"]

                    biz_badge = f" [{proj_tenant.business_name}]" if current_user.role == "CLIENT" and proj_tenant else ""
                    is_open_default = a.status not in COMPLETED_STATUSES
                    with st.expander(f"**{a.ref_number}**{biz_badge} | {a.title} — `{a.status}` (Due: {a.deadline.strftime('%Y-%m-%d')})", expanded=is_open_default):
                        sub_t1, sub_t2, sub_t3, sub_t4, sub_t5 = st.tabs([
                            "📌 Overview & Controls",
                            "🧾 Quotation / Invoice & M-Pesa",
                            "📎 Files & Deliverables",
                            "💬 Discussion",
                            "📜 Audit Trail"
                        ])

                        with sub_t1:
                            col_info, col_manage = st.columns([1.4, 1])
                            with col_info:
                                if proj_tenant:
                                    st.markdown(f"**Consultancy:** {proj_tenant.business_name}")
                                st.markdown(f"**Client:** {client_obj.full_name if client_obj else 'Unknown'}  \n"
                                            f"**Assigned Staff:** {staff_obj.full_name if staff_obj else '_Unassigned_'}  \n"
                                            f"**Priority:** {a.priority}  \n"
                                            f"**Requirements:** {a.description}")
                                st.markdown(f"**Financials ({a.invoice_number}):** Quoted `KES {a.quoted_cost_kes:,.0f}` | "
                                            f"Paid `KES {a.amount_paid_kes:,.0f}` | "
                                            f"Balance `KES {balance:,.0f}`")

                                if current_user.role in ["TENANT_ADMIN", "STAFF"]:
                                    if a.staff_id:
                                        pay_tag = (
                                            f"✅ Paid (`{a.staff_payout_mpesa}`)" if comm_info["payout_status"] == "Paid"
                                            else ("🟢 Earned & Payable" if comm_info["is_payable"] else "⏳ Pending Client Pay & Deliverable")
                                        )
                                        net_str = f" | Agency Net: `KES {comm_info['agency_net']:,.0f}`" if current_user.role == "TENANT_ADMIN" else ""
                                        st.markdown(
                                            f"**Staff Commission ({comm_info['commission_pct']:.0f}%):** "
                                            f"`KES {comm_info['staff_earnings']:,.0f}` ({pay_tag}){net_str}"
                                        )
                                    pay_icon = "✅" if is_fully_paid else "⏳"
                                    deliv_icon = "✅" if has_final_deliverable else "⏳"
                                    st.caption(f"**Closure Checklist:** {pay_icon} Full Payment Confirmed | {deliv_icon} Final Deliverable Released to Client ({len(client_deliverables)}) | Staff Drafts ({len(staff_drafts)})")

                                existing_rev = db.query(ClientReview).filter_by(assignment_id=a.id).first()
                                if existing_rev:
                                    st.success(f"⭐ **Client Rating:** {'⭐' * existing_rev.rating} ({existing_rev.rating}/5) — _{existing_rev.feedback}_")

                            with col_manage:
                                if current_user.role in ["TENANT_ADMIN", "STAFF"]:
                                    status_idx = ASSIGNMENT_STATUSES.index(a.status) if a.status in ASSIGNMENT_STATUSES else 0
                                    new_st = st.selectbox("Stage (1–12)", ASSIGNMENT_STATUSES,
                                                          index=status_idx, key=f"st_{a.id}")
                                    if current_user.role == "TENANT_ADMIN":
                                        staff_names = ["Unassigned"] + [s.full_name for s in staff_members]
                                        curr_idx = staff_names.index(staff_obj.full_name) if staff_obj else 0
                                        sel_staff = st.selectbox("Assign Staff", staff_names, index=curr_idx, key=f"sf_{a.id}")
                                        default_pct_val = float(getattr(a, "staff_commission_pct", 50.0) or 50.0)
                                        c_q1, c_q2 = st.columns(2)
                                        new_q = c_q1.number_input("Quoted Cost (KES)", value=float(a.quoted_cost_kes), step=500.0, key=f"q_{a.id}")
                                        new_comm_pct = c_q2.number_input("Staff Share (%)", min_value=0.0, max_value=100.0, value=default_pct_val, step=5.0, key=f"cp_{a.id}")

                                    if st.button("Save Project Changes", key=f"upd_{a.id}", type="primary"):
                                        if new_st in CLOSURE_STATUSES and (not is_fully_paid or not has_final_deliverable):
                                            missing_reasons = []
                                            if not is_fully_paid:
                                                missing_reasons.append(f"full payment is not confirmed (Balance: KES {balance:,.0f})")
                                            if not has_final_deliverable:
                                                missing_reasons.append("no final deliverable has been submitted to the client yet")
                                            st.error(f"⛔ Cannot mark project as **{new_st}** because " + " and ".join(missing_reasons) + ".")
                                        else:
                                            changes = []
                                            if new_st != a.status:
                                                changes.append(f"Status: '{a.status}' -> '{new_st}'")
                                                a.status = new_st
                                                if new_st in COMPLETED_STATUSES and not a.completed_at:
                                                    a.completed_at = utc_now()
                                            if current_user.role == "TENANT_ADMIN":
                                                chosen = next((s for s in staff_members if s.full_name == sel_staff), None)
                                                new_sid = chosen.id if chosen else None
                                                if new_sid != a.staff_id:
                                                    changes.append(f"Assigned to {sel_staff}")
                                                    a.staff_id = new_sid
                                                    if chosen:
                                                        staff_def_rate = float(getattr(chosen, "commission_rate", 50.0) or 50.0)
                                                        a.staff_commission_pct = staff_def_rate if new_comm_pct == default_pct_val else new_comm_pct
                                                    if a.status in ["1. Submitted", "5. Payment confirmed"] and new_sid is not None:
                                                        a.status = "6. Assigned"
                                                elif new_comm_pct != default_pct_val:
                                                    a.staff_commission_pct = new_comm_pct
                                                    changes.append(f"Staff commission set to {new_comm_pct:.0f}%")

                                                if new_q != a.quoted_cost_kes:
                                                    changes.append(f"Quote updated to KES {new_q:,.0f}")
                                                    a.quoted_cost_kes = new_q

                                            if changes:
                                                summary_note = "; ".join(changes)
                                                db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                                     actor_name=current_user.full_name, action_note=summary_note))
                                                notify_all_stakeholders(
                                                    db, a.tenant_id, a,
                                                    f"[{proj_tenant.business_name}] Progress Update ({a.ref_number}): {a.status}",
                                                    f"Project: {a.title} ({a.ref_number})\n"
                                                    f"Updated by: {current_user.full_name}\n"
                                                    f"Changes: {summary_note}\n"
                                                    f"Current Stage: {a.status} | Quoted: KES {a.quoted_cost_kes:,.0f} | Paid: KES {a.amount_paid_kes:,.0f}"
                                                )
                                                db.commit()
                                                st.success("Project updated and all stakeholders notified!")
                                                st.rerun()
                                            else:
                                                st.info("No new changes detected.")
                                else:
                                    if a.status in ["8. Awaiting client feedback", "9. Awaiting client feedback"]:
                                        c_b1, c_b2 = st.columns(2)
                                        if c_b1.button("✅ Approve Work", key=f"app_{a.id}", type="primary"):
                                            if not is_fully_paid or not has_final_deliverable:
                                                st.error("Project cannot be completed until full payment is confirmed and final deliverable is uploaded.")
                                            else:
                                                a.status = "11. Completed"
                                                a.completed_at = utc_now()
                                                db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                                     actor_name=current_user.full_name, action_note="Client approved final work."))
                                                notify_all_stakeholders(
                                                    db, a.tenant_id, a,
                                                    f"[{proj_tenant.business_name}] Work Approved ({a.ref_number}): 11. Completed",
                                                    f"Client {current_user.full_name} approved final deliverables for '{a.title}' ({a.ref_number})."
                                                )
                                                db.commit()
                                                st.rerun()
                                        if c_b2.button("🔄 Request Revision", key=f"rev_{a.id}"):
                                            a.status = "10. Revision requested"
                                            db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                                 actor_name=current_user.full_name, action_note="Client requested revision."))
                                            notify_all_stakeholders(
                                                db, a.tenant_id, a,
                                                f"[{proj_tenant.business_name}] Revision Requested ({a.ref_number})",
                                                f"Client {current_user.full_name} requested a revision on '{a.title}' ({a.ref_number})."
                                            )
                                            db.commit()
                                            st.rerun()

                                    if a.status in COMPLETED_STATUSES and not existing_rev:
                                        with st.form(f"rev_form_{a.id}"):
                                            st.markdown(f"**Rate {proj_tenant.business_name}**")
                                            stars = st.slider("Satisfaction Rating (1–5)", 1, 5, 5)
                                            fb_text = st.text_input("Public Review Comment")
                                            if st.form_submit_button("Publish Public Review"):
                                                db.add(ClientReview(
                                                    tenant_id=a.tenant_id, assignment_id=a.id,
                                                    client_name=current_user.full_name, rating=stars, feedback=fb_text
                                                ))
                                                notify_all_stakeholders(
                                                    db, a.tenant_id, a,
                                                    f"[{proj_tenant.business_name}] New Client Review ({stars}/5) - {a.ref_number}",
                                                    f"{current_user.full_name} rated '{proj_tenant.business_name}' {stars}/5 stars: {fb_text}"
                                                )
                                                db.commit()
                                                st.rerun()

                        with sub_t2:
                            pdf_bytes = generate_financial_pdf(proj_tenant, a, client_obj, srv_obj.title if srv_obj else "Academic Service")
                            pdf_label = "📄 Download PDF Quotation" if a.status in ["1. Submitted", "2. Under review", "3. Quotation sent"] else "🧾 Download PDF Invoice / Receipt"
                            st.download_button(
                                label=pdf_label, data=pdf_bytes,
                                file_name=f"{a.invoice_number}_{a.ref_number}.pdf",
                                mime="application/pdf", key=f"pdf_{a.id}"
                            )
                            st.divider()
                            txns = db.query(ClientPaymentTransaction).filter_by(tenant_id=a.tenant_id, assignment_id=a.id).all()
                            for tx in txns:
                                tc1, tc2 = st.columns([3, 1])
                                chk_note = f" | CheckoutID: `{tx.checkout_request_id}`" if getattr(tx, "checkout_request_id", "") else ""
                                tc1.write(f"• M-Pesa Code: **`{tx.mpesa_code}`** | KES `{tx.amount_kes:,.0f}` | Status: `{tx.status}`{chk_note}")
                                if current_user.role == "TENANT_ADMIN" and tx.status == "Pending Verification":
                                    if tc2.button("✅ Verify Payment", key=f"ver_{tx.id}"):
                                        tx.status = "Verified"
                                        a.amount_paid_kes += tx.amount_kes
                                        if a.amount_paid_kes >= a.quoted_cost_kes and a.status in ["1. Submitted", "2. Under review", "3. Quotation sent", "4. Awaiting payment"]:
                                            a.status = "5. Payment confirmed"
                                        db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                             actor_name=current_user.full_name,
                                                             action_note=f"Verified M-Pesa {tx.mpesa_code} (KES {tx.amount_kes:,.0f})."))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Payment Confirmed: KES {tx.amount_kes:,.0f} ({tx.mpesa_code})",
                                            f"M-Pesa payment of KES {tx.amount_kes:,.0f} (Receipt: {tx.mpesa_code}) for '{a.title}' ({a.ref_number}) has been verified.\n"
                                            f"Total Paid: KES {a.amount_paid_kes:,.0f} / KES {a.quoted_cost_kes:,.0f}\n"
                                            f"Current Stage: {a.status}"
                                        )
                                        db.commit()
                                        st.rerun()

                            if current_user.role == "CLIENT" and balance > 0:
                                p_col1, p_col2 = st.columns(2)
                                with p_col1:
                                    stk_phone = st.text_input("M-Pesa Phone (2547XXXXXXXX)", value="254712345678", key=f"stk_ph_{a.id}")
                                    stk_amt = st.number_input("Amount to Pay (KES)", min_value=10.0, value=float(balance), key=f"stk_am_{a.id}")
                                    if st.button("📲 Send M-Pesa STK Push", key=f"stk_btn_{a.id}", type="primary"):
                                        res = trigger_daraja_stk_push(proj_tenant, stk_phone, int(stk_amt), a.ref_number)
                                        chk_id = res.get("CheckoutRequestID", "")
                                        if chk_id:
                                            db.add(ClientPaymentTransaction(
                                                tenant_id=a.tenant_id, assignment_id=a.id,
                                                checkout_request_id=chk_id,
                                                amount_kes=float(stk_amt),
                                                mpesa_code=f"STK-{chk_id[-8:]}",
                                                phone_number=stk_phone.strip(),
                                                status="Pending Verification"
                                            ))
                                            db.add(AssignmentLog(
                                                tenant_id=a.tenant_id, assignment_id=a.id,
                                                actor_name=current_user.full_name,
                                                action_note=f"Initiated M-Pesa STK Push ({chk_id}) for KES {stk_amt:,.0f}."
                                            ))
                                            notify_all_stakeholders(
                                                db, a.tenant_id, a,
                                                f"[{proj_tenant.business_name}] M-Pesa STK Push Initiated ({a.ref_number})",
                                                f"Client {current_user.full_name} initiated an M-Pesa STK Push of KES {stk_amt:,.0f} from {stk_phone} for '{a.title}' ({a.ref_number}).\nCheckoutRequestID: {chk_id}"
                                            )
                                            db.commit()
                                        st.info(res.get("CustomerMessage", "STK Push request processed."))
                                with p_col2:
                                    m_code = st.text_input("Or Enter M-Pesa Confirmation Code", placeholder="e.g. SL84K92P10", key=f"mc_{a.id}")
                                    m_amt = st.number_input("Paid Amount (KES)", min_value=10.0, value=float(balance), key=f"ma_{a.id}")
                                    if st.button("Submit M-Pesa Code", key=f"sub_mc_{a.id}") and m_code.strip():
                                        clean_code = m_code.strip().upper()
                                        if db.query(ClientPaymentTransaction).filter_by(tenant_id=a.tenant_id, mpesa_code=clean_code).first():
                                            st.error("This M-Pesa confirmation code has already been submitted!")
                                        else:
                                            db.add(ClientPaymentTransaction(
                                                tenant_id=a.tenant_id, assignment_id=a.id,
                                                amount_kes=m_amt, mpesa_code=clean_code,
                                                phone_number=stk_phone, status="Pending Verification"
                                            ))
                                            db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                                 actor_name=current_user.full_name,
                                                                 action_note=f"Client submitted M-Pesa code {clean_code}."))
                                            notify_all_stakeholders(
                                                db, a.tenant_id, a,
                                                f"[{proj_tenant.business_name}] M-Pesa Receipt Submitted: {clean_code} ({a.ref_number})",
                                                f"Client {current_user.full_name} submitted M-Pesa receipt {clean_code} for KES {m_amt:,.0f} on '{a.title}' ({a.ref_number})."
                                            )
                                            db.commit()
                                            st.success("Payment submitted and all stakeholders notified!")
                                            st.rerun()

                        with sub_t3:
                            visible_docs = [
                                d for d in all_proj_docs
                                if current_user.role in ["TENANT_ADMIN", "STAFF"] or d.doc_category != "STAFF_DRAFT"
                            ]
                            if not visible_docs:
                                st.caption("No files uploaded yet.")
                            for d in visible_docs:
                                dc1, dc2, dc3 = st.columns([2.6, 1, 1.4])
                                cat_label = {
                                    "CLIENT_BRIEF": "📥 Client Brief",
                                    "STAFF_DRAFT": "🔒 Staff Draft (For Admin Review)",
                                    "DELIVERABLE": "✅ Final Client Deliverable"
                                }.get(d.doc_category, d.doc_category)
                                dc1.markdown(f"📄 **{d.original_filename}** (`{cat_label}`) — _by {d.uploaded_by_name}_")
                                if os.path.exists(d.file_path):
                                    with open(d.file_path, "rb") as fp:
                                        dc2.download_button("⬇️ Download", data=fp.read(),
                                                            file_name=d.original_filename, key=f"dl_{d.id}")
                                if current_user.role == "TENANT_ADMIN" and d.doc_category == "STAFF_DRAFT":
                                    if dc3.button("🚀 Approve & Send to Client", key=f"prom_{d.id}", type="primary"):
                                        d.doc_category = "DELIVERABLE"
                                        a.status = "9. Awaiting client feedback"
                                        db.add(AssignmentLog(
                                            tenant_id=a.tenant_id, assignment_id=a.id,
                                            actor_name=current_user.full_name,
                                            action_note=f"Approved staff draft '{d.original_filename}' and released as Final Deliverable to client."
                                        ))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Final Deliverable Ready ({a.ref_number}): {d.original_filename}",
                                            f"Your deliverable '{d.original_filename}' for '{a.title}' ({a.ref_number}) has been reviewed and released by {current_user.full_name}. Please log in to download and review."
                                        )
                                        db.commit()
                                        st.success("Deliverable released to client!")
                                        st.rerun()

                            st.divider()
                            if current_user.role == "STAFF":
                                doc_cat = "STAFF_DRAFT"
                                st.info("📤 **Staff Submission:** Upload your completed work below. It goes directly to your **Tenant Admin** for quality review before release to the client.")
                            elif current_user.role == "TENANT_ADMIN":
                                doc_cat = st.radio(
                                    "Upload File As:",
                                    ["DELIVERABLE", "STAFF_DRAFT"],
                                    format_func=lambda x: "✅ Final Deliverable (Visible to Client)" if x == "DELIVERABLE" else "🔒 Internal Working Draft (Staff & Admin Only)",
                                    horizontal=True,
                                    key=f"adm_cat_{a.id}"
                                )
                            else:
                                doc_cat = "CLIENT_BRIEF"

                            new_file = st.file_uploader(f"Select File ({doc_cat})", key=f"up_{a.id}")
                            if new_file and st.button("Save Uploaded File", key=f"save_f_{a.id}"):
                                incoming_hash = compute_file_sha256(new_file.getbuffer())
                                dup = db.query(AssignmentDocument).filter_by(tenant_id=a.tenant_id, assignment_id=a.id, file_hash=incoming_hash).first()
                                if dup:
                                    st.error(f"Duplicate file blocked: '{dup.original_filename}' already exists on this project.")
                                else:
                                    saved_path, f_hash = save_uploaded_file(a.tenant_id, a.id, new_file)
                                    db.add(AssignmentDocument(
                                        tenant_id=a.tenant_id, assignment_id=a.id,
                                        uploaded_by_name=current_user.full_name, doc_category=doc_cat,
                                        original_filename=new_file.name, file_hash=f_hash, file_path=saved_path
                                    ))
                                    if doc_cat == "STAFF_DRAFT":
                                        a.status = "8. Internal review (Staff -> Admin)"
                                        db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                             actor_name=current_user.full_name,
                                                             action_note=f"Staff uploaded draft '{new_file.name}' to Admin for review."))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Staff Draft Submitted for Admin Review ({a.ref_number})",
                                            f"Staff {current_user.full_name} uploaded draft '{new_file.name}' on '{a.title}' ({a.ref_number}) for Admin approval.",
                                            include_client=False
                                        )
                                    elif doc_cat == "DELIVERABLE":
                                        if a.status in ["6. Assigned", "7. In progress", "8. Internal review (Staff -> Admin)", "10. Revision requested"]:
                                            a.status = "9. Awaiting client feedback"
                                        db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                             actor_name=current_user.full_name,
                                                             action_note=f"Released Final Deliverable to client: {new_file.name}"))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Final Deliverable Uploaded ({a.ref_number}): {new_file.name}",
                                            f"{current_user.full_name} released final deliverable '{new_file.name}' for '{a.title}' ({a.ref_number})."
                                        )
                                    else:
                                        db.add(AssignmentLog(tenant_id=a.tenant_id, assignment_id=a.id,
                                                             actor_name=current_user.full_name,
                                                             action_note=f"Client uploaded brief: {new_file.name}"))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Client File Uploaded ({a.ref_number}): {new_file.name}",
                                            f"Client {current_user.full_name} uploaded '{new_file.name}' to '{a.title}' ({a.ref_number})."
                                        )
                                    db.commit()
                                    st.rerun()

                        with sub_t4:
                            all_msgs = db.query(AssignmentMessage).filter_by(tenant_id=a.tenant_id, assignment_id=a.id).order_by(AssignmentMessage.sent_at.asc()).all()
                            client_msgs = [m for m in all_msgs if getattr(m, "channel", "CLIENT_CHAT") != "INTERNAL_STAFF_ADMIN"]
                            internal_msgs = [m for m in all_msgs if getattr(m, "channel", "CLIENT_CHAT") == "INTERNAL_STAFF_ADMIN"]

                            if current_user.role in ["TENANT_ADMIN", "STAFF"]:
                                chat_c1, chat_c2 = st.columns(2)
                                with chat_c1:
                                    st.markdown("#### 💬 Client & Team Thread")
                                    for m in client_msgs:
                                        st.markdown(f"**{m.sender_name}** (`{m.sender_role}`) • _{m.sent_at.strftime('%Y-%m-%d %H:%M')}_  \n> {m.message_text}")
                                    pub_input = st.text_input("Message client & team...", key=f"msg_in_{a.id}")
                                    if st.button("Send to Client Thread", key=f"msg_btn_{a.id}") and pub_input.strip():
                                        clean_msg = pub_input.strip()
                                        db.add(AssignmentMessage(
                                            tenant_id=a.tenant_id, assignment_id=a.id,
                                            sender_name=current_user.full_name, sender_role=current_user.role,
                                            channel="CLIENT_CHAT", message_text=clean_msg
                                        ))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] New Message on {a.ref_number} from {current_user.full_name}",
                                            f"{current_user.full_name} ({current_user.role}) posted on '{a.title}' ({a.ref_number}):\n\n\"{clean_msg}\""
                                        )
                                        db.commit()
                                        st.rerun()

                                with chat_c2:
                                    st.markdown("#### 🔒 Internal Staff ↔ Admin Chat")
                                    st.caption("Private coordination between Staff & Admin (not visible to Client).")
                                    for m in internal_msgs:
                                        st.markdown(f"**{m.sender_name}** (`{m.sender_role}`) • _{m.sent_at.strftime('%Y-%m-%d %H:%M')}_  \n> {m.message_text}")
                                    int_input = st.text_input("Private note to Staff / Admin...", key=f"int_msg_in_{a.id}")
                                    if st.button("Send Internal Note", key=f"int_msg_btn_{a.id}", type="primary") and int_input.strip():
                                        clean_int = int_input.strip()
                                        db.add(AssignmentMessage(
                                            tenant_id=a.tenant_id, assignment_id=a.id,
                                            sender_name=current_user.full_name, sender_role=current_user.role,
                                            channel="INTERNAL_STAFF_ADMIN", message_text=clean_int
                                        ))
                                        notify_all_stakeholders(
                                            db, a.tenant_id, a,
                                            f"[{proj_tenant.business_name}] Internal Staff-Admin Note ({a.ref_number})",
                                            f"{current_user.full_name} ({current_user.role}) posted an internal note on '{a.title}' ({a.ref_number}):\n\n\"{clean_int}\"",
                                            include_client=False
                                        )
                                        db.commit()
                                        st.rerun()
                            else:
                                for m in client_msgs:
                                    st.markdown(f"**{m.sender_name}** (`{m.sender_role}`) • _{m.sent_at.strftime('%Y-%m-%d %H:%M')}_  \n> {m.message_text}")
                                msg_input = st.text_input("Write a message...", key=f"msg_in_{a.id}")
                                if st.button("Send Message", key=f"msg_btn_{a.id}") and msg_input.strip():
                                    clean_msg = msg_input.strip()
                                    db.add(AssignmentMessage(
                                        tenant_id=a.tenant_id, assignment_id=a.id,
                                        sender_name=current_user.full_name, sender_role=current_user.role,
                                        channel="CLIENT_CHAT", message_text=clean_msg
                                    ))
                                    notify_all_stakeholders(
                                        db, a.tenant_id, a,
                                        f"[{proj_tenant.business_name}] New Message on {a.ref_number} from {current_user.full_name}",
                                        f"{current_user.full_name} ({current_user.role}) posted on '{a.title}' ({a.ref_number}):\n\n\"{clean_msg}\""
                                    )
                                    db.commit()
                                    st.rerun()

                        with sub_t5:
                            logs = db.query(AssignmentLog).filter_by(tenant_id=a.tenant_id, assignment_id=a.id).order_by(AssignmentLog.timestamp.desc()).all()
                            for lg in logs:
                                st.caption(f"[{lg.timestamp.strftime('%Y-%m-%d %H:%M')}] **{lg.actor_name}**: {lg.action_note}")

        # --- CLIENT TAB 2: CHOOSE ANY TENANT, VIEW PUBLIC REVIEWS & TURNAROUND TIME ---
        if current_user.role == "CLIENT":
            with tabs[1]:
                st.subheader("Compare Consultancies & Submit a New Request")
                active_tenants = db.query(Tenant).filter(Tenant.status.in_(["Active", "Trial"])).all()
                if not active_tenants:
                    st.warning("No active consultancies are available right now.")
                else:
                    tenant_choices = {}
                    for t_item in active_tenants:
                        tm = get_tenant_public_metrics(db, t_item.id)
                        r_badge = f"⭐ {tm['avg_rating']}/5 ({tm['review_count']} reviews)" if tm["review_count"] else "⭐ New (No reviews yet)"
                        lbl = f"{t_item.business_name} — {r_badge} | ⏱️ Turnaround: {tm['turnaround_label']}"
                        tenant_choices[lbl] = (t_item, tm)

                    selected_t_lbl = st.selectbox("1. Choose Academic / Research Consultancy for This Project", list(tenant_choices.keys()))
                    chosen_tenant, chosen_metrics = tenant_choices[selected_t_lbl]

                    with st.container(border=True):
                        p1, p2, p3 = st.columns(3)
                        p1.metric("Consultancy", chosen_tenant.business_name)
                        p2.metric(
                            "Public Client Rating",
                            f"{chosen_metrics['avg_rating']} / 5.0 ⭐ ({chosen_metrics['review_count']})" if chosen_metrics["review_count"] else "New Workspace"
                        )
                        p3.metric("Public Turnaround Time", chosen_metrics["turnaround_label"])

                        if chosen_metrics["reviews"]:
                            with st.expander(f"📣 Read Public Client Reviews for {chosen_tenant.business_name} ({chosen_metrics['review_count']})", expanded=True):
                                for rev in sorted(chosen_metrics["reviews"], key=lambda r: r.created_at, reverse=True)[:5]:
                                    st.markdown(f"- {'⭐' * rev.rating} **{rev.client_name}** ({rev.created_at.strftime('%Y-%m-%d')}): _{rev.feedback or 'Great service!'}_")
                        else:
                            st.caption("No public client reviews yet for this consultancy — be the first to review after project completion!")

                    chosen_plan = db.query(SubscriptionPlan).filter_by(name=chosen_tenant.tier).first()
                    chosen_active_cnt = db.query(Assignment).filter(
                        Assignment.tenant_id == chosen_tenant.id,
                        ~Assignment.status.in_(COMPLETED_STATUSES)
                    ).count()
                    chosen_services = db.query(Service).filter_by(tenant_id=chosen_tenant.id).all()

                    if chosen_plan and chosen_active_cnt >= chosen_plan.max_active_projects:
                        st.warning(f"**{chosen_tenant.business_name}** has reached its active project capacity. Please select another consultancy above.")
                    elif not chosen_services:
                        st.warning(f"**{chosen_tenant.business_name}** has not published any services yet. Please select another consultancy above.")
                    else:
                        with st.form("client_submit_form"):
                            service_map = {f"{s.title} (From KES {s.base_price_kes:,.0f} • Public Turnaround: ~{s.turnaround_days} days)": s for s in chosen_services}
                            chosen_srv_label = st.selectbox("2. Select Service", list(service_map.keys()))
                            task_title = st.text_input("Project / Assignment Title")
                            task_desc = st.text_area("Detailed Requirements, Methodology & Expected Deliverables")
                            pref_deadline = st.date_input("Preferred Deadline", value=utc_now() + timedelta(days=5))
                            uploaded_doc = st.file_uploader("Attach Supporting Documents (PDF, DOCX, CSV, XLSX, ZIP)")

                            if st.form_submit_button(f"Submit Request to {chosen_tenant.business_name}", type="primary"):
                                clean_title = task_title.strip()
                                if not clean_title:
                                    st.error("Please enter a project title.")
                                else:
                                    srv = service_map[chosen_srv_label]
                                    existing_active = db.query(Assignment).filter(
                                        Assignment.tenant_id == chosen_tenant.id,
                                        Assignment.client_id == current_user.id,
                                        ~Assignment.status.in_(["11. Closed", "12. Closed"])
                                    ).all()
                                    if any(ex.title.strip().lower() == clean_title.lower() for ex in existing_active):
                                        st.error(f"Duplicate project blocked: You already have an active project titled '{clean_title}' with {chosen_tenant.business_name}.")
                                    else:
                                        rand_num = random.randint(1000, 9999)
                                        ref_no = f"AF-{utc_now().year}-{rand_num}"
                                        inv_no = f"INV-{utc_now().year}-{rand_num}"
                                        new_a = Assignment(
                                            tenant_id=chosen_tenant.id, ref_number=ref_no, invoice_number=inv_no,
                                            title=clean_title, description=task_desc,
                                            service_id=srv.id, client_id=current_user.id,
                                            status=ASSIGNMENT_STATUSES[0], quoted_cost_kes=srv.base_price_kes,
                                            deadline=datetime.combine(pref_deadline, datetime.min.time())
                                        )
                                        db.add(new_a)
                                        db.commit()

                                        if uploaded_doc:
                                            saved_path, f_hash = save_uploaded_file(chosen_tenant.id, new_a.id, uploaded_doc)
                                            db.add(AssignmentDocument(
                                                tenant_id=chosen_tenant.id, assignment_id=new_a.id,
                                                uploaded_by_name=current_user.full_name, doc_category="CLIENT_BRIEF",
                                                original_filename=uploaded_doc.name, file_hash=f_hash, file_path=saved_path
                                            ))

                                        db.add(AssignmentLog(tenant_id=chosen_tenant.id, assignment_id=new_a.id,
                                                             actor_name=current_user.full_name,
                                                             action_note=f"Submitted request {ref_no} ({inv_no}) to {chosen_tenant.business_name}."))
                                        notify_all_stakeholders(
                                            db, chosen_tenant.id, new_a,
                                            f"[{chosen_tenant.business_name}] New Work Posted ({ref_no}): {clean_title}",
                                            f"New assignment posted on {chosen_tenant.business_name}!\n"
                                            f"Reference: {ref_no} | Invoice: {inv_no}\n"
                                            f"Client: {current_user.full_name} ({current_user.email})\n"
                                            f"Service: {srv.title} | Quoted: KES {srv.base_price_kes:,.0f}\n"
                                            f"Deadline: {pref_deadline}\n"
                                            f"Requirements: {task_desc}"
                                        )
                                        db.commit()
                                        st.success(f"Request submitted to **{chosen_tenant.business_name}**! Reference: **{ref_no}** | Invoice: **{inv_no}**")
                                        st.rerun()

        # --- TENANT ADMIN EXTRA TABS ---
        if current_user.role == "TENANT_ADMIN":
            with tabs[1]:
                st.subheader("Workspace Performance, Turnaround & Client Satisfaction")
                t_pub = get_tenant_public_metrics(db, tid)
                reviews = t_pub["reviews"]
                total_clients = db.query(Assignment.client_id).filter_by(tenant_id=tid).distinct().count()

                ac1, ac2, ac3, ac4 = st.columns(4)
                ac1.metric("Active / Past Clients", total_clients)
                ac2.metric("Completed Projects", sum(1 for a in assignments if a.status in COMPLETED_STATUSES))
                ac3.metric("Public Client Rating", f"{t_pub['avg_rating']:.1f} / 5.0 ⭐ ({t_pub['review_count']})" if reviews else "No reviews yet")
                ac4.metric("Public Turnaround Time", t_pub["turnaround_label"])

                if assignments:
                    st.markdown("**Assignments by Workflow Stage**")
                    stage_counts = pd.Series([a.status for a in assignments]).value_counts()
                    st.bar_chart(stage_counts)

                if reviews:
                    st.markdown("**Public Client Reviews**")
                    for r in reviews:
                        st.write(f"• {'⭐' * r.rating} **{r.client_name}** ({r.created_at.strftime('%Y-%m-%d')}): _{r.feedback}_")

            with tabs[2]:
                st.subheader("Business Service Catalogue (Public Turnaround & Pricing)")
                for s in services:
                    st.markdown(f"- **{s.title}** — KES {s.base_price_kes:,.0f} (Public Turnaround: **{s.turnaround_days} days**)  \n  _{s.description}_")
                st.divider()
                with st.form("add_service_form"):
                    st.markdown("**Add New Service**")
                    s_title = st.text_input("Service Name")
                    s_desc = st.text_input("Description")
                    s_price = st.number_input("Base Price (KES)", min_value=500.0, step=500.0)
                    s_days = st.number_input("Public Turnaround (Days)", min_value=1, value=3)
                    if st.form_submit_button("Add Service") and s_title:
                        if any(existing.title.strip().lower() == s_title.strip().lower() for existing in services):
                            st.error("A service with this name already exists.")
                        else:
                            db.add(Service(tenant_id=tid, title=s_title.strip(), description=s_desc,
                                           base_price_kes=s_price, turnaround_days=s_days))
                            db.commit()
                            st.rerun()

            with tabs[3]:
                st.subheader("Client Financials, Staff Payroll & Daraja Configuration (Ledger 2)")
                with st.form("mpesa_cfg_form"):
                    c1, c2 = st.columns(2)
                    c_pb = c1.text_input("Your Business M-Pesa Paybill / Till", value=tenant.mpesa_paybill)
                    c_acc = c2.text_input("Account Reference / Name", value=tenant.mpesa_account)
                    d_key = st.text_input("Daraja Consumer Key", value=decrypt_secret(tenant.daraja_consumer_key), type="password")
                    d_sec = st.text_input("Daraja Consumer Secret", value=decrypt_secret(tenant.daraja_consumer_secret), type="password")
                    d_pass = st.text_input("Daraja Lipa Na M-Pesa Passkey", value=decrypt_secret(tenant.daraja_passkey), type="password")
                    if st.form_submit_button("Save Payment & Daraja Settings"):
                        tenant.mpesa_paybill = c_pb
                        tenant.mpesa_account = c_acc
                        tenant.daraja_consumer_key = encrypt_secret(d_key)
                        tenant.daraja_consumer_secret = encrypt_secret(d_sec)
                        tenant.daraja_passkey = encrypt_secret(d_pass)
                        db.commit()
                        st.success("Payment & encrypted Daraja settings updated!")

                st.divider()
                st.markdown("### 💸 Staff Payroll / M-Pesa Payouts")
                staff_assignments = [a for a in assignments if a.staff_id is not None]
                if not staff_assignments:
                    st.caption("No projects have been assigned to staff yet.")
                else:
                    payroll_rows = []
                    payable_list = []
                    for sa_a in staff_assignments:
                        st_u = db.query(User).filter_by(id=sa_a.staff_id).first()
                        ci = compute_assignment_commission(db, sa_a)
                        eligibility = (
                            "✅ Paid" if ci["payout_status"] == "Paid"
                            else ("🟢 Ready for Payout" if ci["is_payable"] else "⏳ Awaiting Client Pay / Deliverable")
                        )
                        payroll_rows.append({
                            "Ref #": sa_a.ref_number,
                            "Staff Member": st_u.full_name if st_u else "Unknown",
                            "Quoted (KES)": sa_a.quoted_cost_kes,
                            "Share (%)": f"{ci['commission_pct']:.0f}%",
                            "Staff Earnings (KES)": ci["staff_earnings"],
                            "Agency Net (KES)": ci["agency_net"],
                            "Payout Status": eligibility,
                            "Payout M-Pesa Ref": getattr(sa_a, "staff_payout_mpesa", "") or "-"
                        })
                        if ci["is_payable"]:
                            payable_list.append((sa_a, st_u, ci))

                    st.dataframe(pd.DataFrame(payroll_rows), use_container_width=True)

                    if payable_list:
                        st.markdown("**Disburse Earned Staff Payouts:**")
                        for pay_a, pay_u, pay_ci in payable_list:
                            pc1, pc2, pc3 = st.columns([2.2, 1.3, 1])
                            pc1.write(
                                f"• **{pay_u.full_name if pay_u else 'Staff'}** — `{pay_a.ref_number}` ({pay_a.title[:35]}) | "
                                f"**KES {pay_ci['staff_earnings']:,.0f}** ({pay_ci['commission_pct']:.0f}%)"
                            )
                            mp_ref = pc2.text_input("M-Pesa Payout Receipt", placeholder="e.g. SL99PAY01", key=f"sp_ref_{pay_a.id}")
                            if pc3.button("💸 Mark Payout as Paid", key=f"sp_btn_{pay_a.id}", type="primary"):
                                receipt_str = mp_ref.strip().upper() or f"MANUAL-{utc_now().strftime('%Y%m%d%H%M')}"
                                pay_a.staff_payout_status = "Paid"
                                pay_a.staff_payout_mpesa = receipt_str
                                pay_a.staff_paid_at = utc_now()
                                db.add(AssignmentLog(
                                    tenant_id=tid, assignment_id=pay_a.id,
                                    actor_name=current_user.full_name,
                                    action_note=f"Marked staff payout of KES {pay_ci['staff_earnings']:,.0f} to {pay_u.full_name if pay_u else 'Staff'} as Paid (Ref: {receipt_str})."
                                ))
                                if pay_u and pay_u.email:
                                    dispatch_email_notification(
                                        db, tid, pay_u.email,
                                        f"[{tenant.business_name}] Staff Payout Sent: KES {pay_ci['staff_earnings']:,.0f} ({pay_a.ref_number})",
                                        f"Hello {pay_u.full_name},\n\nYour commission payout of KES {pay_ci['staff_earnings']:,.0f} ({pay_ci['commission_pct']:.0f}%) for '{pay_a.title}' ({pay_a.ref_number}) has been marked as Paid (Ref: {receipt_str})."
                                    )
                                db.commit()
                                st.success(f"Payout recorded ({receipt_str}) and staff notified!")
                                st.rerun()

                st.divider()
                st.markdown("### 📊 Project Revenue & Agency Net Margin Ledger")
                fin_rows = []
                for a in assignments:
                    ci = compute_assignment_commission(db, a)
                    fin_rows.append({
                        "Ref #": a.ref_number, "Invoice #": a.invoice_number, "Project": a.title,
                        "Quoted (KES)": a.quoted_cost_kes, "Paid (KES)": a.amount_paid_kes,
                        "Balance (KES)": max(0.0, a.quoted_cost_kes - a.amount_paid_kes),
                        "Staff Share (KES)": ci["staff_earnings"],
                        "Agency Net (KES)": ci["agency_net"],
                        "Status": a.status
                    })
                st.dataframe(pd.DataFrame(fin_rows), use_container_width=True)

            with tabs[4]:
                st.subheader("Team Members, Subscription Plan & AcadFlow Support")
                if current_plan:
                    st.info(f"**Current Tier:** {current_plan.name} ({tenant.status}) — Staff Used: **{len(staff_members)} / {current_plan.max_staff}** | "
                            f"Active Projects: **{active_tenant_projects_count} / {current_plan.max_active_projects}**")

                t_users = db.query(User).filter_by(tenant_id=tid).all()
                st.dataframe(pd.DataFrame([{
                    "Name": u.full_name,
                    "Email": u.email,
                    "Role": u.role,
                    "Default Commission (%)": f"{getattr(u, 'commission_rate', 50.0):.0f}%" if u.role == "STAFF" else "-"
                } for u in t_users]), use_container_width=True)

                only_staff = [u for u in t_users if u.role == "STAFF"]
                if only_staff:
                    with st.expander("⚙️ Adjust Default Commission Rate for Existing Staff"):
                        ec1, ec2, ec3 = st.columns([2, 1.2, 1])
                        s_map = {f"{u.full_name} ({getattr(u, 'commission_rate', 50.0):.0f}%)": u for u in only_staff}
                        chosen_s_lbl = ec1.selectbox("Select Staff Member", list(s_map.keys()), key="edit_st_rate_sel")
                        target_st_u = s_map[chosen_s_lbl]
                        updated_rate = ec2.number_input("New Default Rate (%)", min_value=0.0, max_value=100.0, value=float(getattr(target_st_u, "commission_rate", 50.0) or 50.0), step=5.0, key="edit_st_rate_val")
                        if ec3.button("Save Rate", key="edit_st_rate_btn"):
                            target_st_u.commission_rate = updated_rate
                            db.commit()
                            st.success(f"Updated {target_st_u.full_name}'s default rate to {updated_rate:.0f}%!")
                            st.rerun()

                col_staff, col_saas, col_sup = st.columns([1, 1.3, 1.1])
                with col_staff:
                    with st.form("invite_staff_form"):
                        st.markdown("**Add Staff / Researcher**")
                        st_name = st.text_input("Staff Full Name")
                        st_email = st.text_input("Staff Email")
                        st_rate = st.number_input("Default Commission Share (%)", min_value=0.0, max_value=100.0, value=50.0, step=5.0)
                        st_pass = st.text_input("Temporary Password (min 6 chars)", type="password")
                        if st.form_submit_button("Add Staff Member"):
                            clean_st_email = normalize_email(st_email)
                            if not (st_name.strip() and clean_st_email and st_pass):
                                st.error("Please fill in staff name, email, and temporary password.")
                            elif len(st_pass) < 6:
                                st.error("Temporary password must be at least 6 characters.")
                            elif current_plan and len(staff_members) >= current_plan.max_staff:
                                st.error(f"Staff limit reached ({current_plan.max_staff}) for {current_plan.name} plan. Please upgrade!")
                            elif find_user_by_email(db, clean_st_email):
                                st.error("User with this email already exists.")
                            else:
                                new_staff = User(
                                    tenant_id=tid, full_name=st_name.strip(), email=clean_st_email,
                                    password_hash=hash_password(st_pass), role="STAFF", commission_rate=st_rate
                                )
                                db.add(new_staff)
                                notify_all_stakeholders(
                                    db, tid, None,
                                    f"[{tenant.business_name}] New Staff Added: {new_staff.full_name}",
                                    f"Staff member {new_staff.full_name} ({new_staff.email}, {st_rate:.0f}% commission) was added to {tenant.business_name}."
                                )
                                dispatch_email_notification(
                                    db, tid, new_staff.email,
                                    f"Welcome to {tenant.business_name} on AcadFlow",
                                    f"Hello {new_staff.full_name}, you have been added as Staff on {tenant.business_name} with a {st_rate:.0f}% project commission rate. Log in with your email and temporary password."
                                )
                                db.commit()
                                st.rerun()

                with col_saas:
                    render_subscription_stk_box(tenant)

                with col_sup:
                    with st.form("support_ticket_form"):
                        st.markdown("**Contact AcadFlow Super Admin**")
                        tk_subj = st.text_input("Subject")
                        tk_desc = st.text_area("Describe Issue or Request")
                        if st.form_submit_button("Submit Support Ticket") and tk_subj and tk_desc:
                            db.add(SupportTicket(
                                tenant_id=tid, business_name=tenant.business_name,
                                subject=tk_subj.strip(), description=tk_desc.strip()
                            ))
                            notify_all_stakeholders(
                                db, tid, None,
                                f"[AcadFlow Support] New Ticket from {tenant.business_name}: {tk_subj.strip()}",
                                f"Business: {tenant.business_name}\nSubject: {tk_subj.strip()}\n\n{tk_desc.strip()}"
                            )
                            db.commit()
                            st.success("Support ticket sent to Super Admin!")
                            st.rerun()

                    my_tickets = db.query(SupportTicket).filter_by(tenant_id=tid).order_by(SupportTicket.created_at.desc()).all()
                    if my_tickets:
                        st.markdown("**My Support Tickets & Super Admin Replies:**")
                        for mtk in my_tickets:
                            with st.expander(f"[{mtk.status}] {mtk.subject} ({mtk.created_at.strftime('%Y-%m-%d')})", expanded=(mtk.status == "Resolved")):
                                st.write(f"**Issue:** {mtk.description}")
                                if mtk.admin_reply:
                                    st.success(f"**Super Admin Response:** {mtk.admin_reply}")
                                else:
                                    st.info("⏳ Awaiting response from Super Admin.")
finally:
    db.close()
