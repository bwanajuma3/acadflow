import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from database import User, Assignment

def _get_setting(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val
    try:
        import streamlit as st
        return str(st.secrets.get(key, default))
    except Exception:
        return default

def notify_stakeholders(db, assignment: Assignment, subject: str, body_html: str) -> bool:
    """Automatically emails Client, Assigned Staff, Tenant Admins, and Super Admin."""
    smtp_host = _get_setting("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(_get_setting("SMTP_PORT", "587"))
    smtp_user = _get_setting("SMTP_USER")
    smtp_pass = _get_setting("SMTP_PASSWORD")
    super_admin = _get_setting("SUPER_ADMIN_EMAIL")

    if not smtp_user or not smtp_pass:
        return False

    recipients = set()
    if super_admin and "@" in super_admin:
        recipients.add(super_admin.strip().lower())

    # 1. Client email
    if getattr(assignment, "client_id", None):
        client = db.query(User).filter_by(id=assignment.client_id).first()
        if client and client.email:
            recipients.add(client.email.strip().lower())

    # 2. Assigned Staff / Writer email
    if getattr(assignment, "staff_id", None):
        staff = db.query(User).filter_by(id=assignment.staff_id).first()
        if staff and staff.email:
            recipients.add(staff.email.strip().lower())

    # 3. All Tenant Admins for this workspace
    if getattr(assignment, "tenant_id", None):
        admins = db.query(User).filter_by(tenant_id=assignment.tenant_id, role="TENANT_ADMIN").all()
        for admin in admins:
            if admin.email:
                recipients.add(admin.email.strip().lower())

    if not recipients:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"AcadFlow <{smtp_user}>"
    msg["To"] = smtp_user
    msg["Bcc"] = ", ".join(sorted(recipients))  # BCC keeps everyone's email address private
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, list(recipients), msg.as_string())
        return True
    except Exception as exc:
        print(f"[Notification Error] {exc}")
        return False