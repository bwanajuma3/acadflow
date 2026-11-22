import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///acadflow_prod.db")

# Handle Render/Heroku postgres:// prefix compatibility
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,   # Automatically recycles dropped cloud DB connections
    pool_size=10,
    max_overflow=20
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(Integer, primary_key=True, index=True)
    business_name = Column(String, unique=True, nullable=False)
    tier = Column(String, default="Professional")
    status = Column(String, default="Active")
    mpesa_paybill = Column(String, default="522522")
    mpesa_account = Column(String, default="ACADFLOW")
    daraja_consumer_key_enc = Column(String, default="")
    daraja_consumer_secret_enc = Column(String, default="")
    daraja_passkey_enc = Column(String, default="")
    created_at = Column(DateTime, default=utc_now)

class Assignment(Base):
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    ref_number = Column(String, unique=True, nullable=False, index=True)
    invoice_number = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    status = Column(String, default="1. Submitted")
    quoted_cost_kes = Column(Float, default=0.0)
    amount_paid_kes = Column(Float, default=0.0)

class ClientPaymentTransaction(Base):
    __tablename__ = "client_payments"
    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    checkout_request_id = Column(String, unique=True, nullable=True, index=True)
    amount_kes = Column(Float, nullable=False)
    mpesa_code = Column(String, unique=True, nullable=False)
    phone_number = Column(String, default="")
    status = Column(String, default="Verified")
    recorded_at = Column(DateTime, default=utc_now)