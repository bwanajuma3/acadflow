import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import os
from pathlib import Path
import uuid
import bcrypt
from cryptography.fernet import Fernet, InvalidToken
import httpx
from nicegui import app as nicegui_app, events, ui
from sqlalchemy import func, inspect as sa_inspect, text

from database import (
    ASSIGNMENT_STATUSES,
    Assignment,
    AssignmentDocument,
    AssignmentLog,
    AssignmentMessage,
    ClientPaymentTransaction,
    ClientReview,
    EmailNotificationLog,
    PlatformAnnouncement,
    PlatformSubscriptionPayment,
    Service,
    SessionLocal,
    SubscriptionPlan,
    SupportTicket,
    Tenant,
    User,
    engine,
    init_db_schema,
)
from webhook_api import app as fastapi_app

BRAND_PRIMARY = "#0F172A"
BRAND_ACCENT = "#10B981"
STORAGE_DIR = Path("acadflow_storage")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# SCHEMA MIGRATION HELPER (ADDS phone_number TO users IF MISSING)
# ---------------------------------------------------------------------------
def ensure_user_phone_column() -> None:
    try:
        inspector = sa_inspect(engine)
        if "users" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("users")}
            if "phone_number" not in cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN phone_number VARCHAR(32)"))
    except Exception:
        pass


def get_user_phone_map(db, tenant_id: int | None = None) -> dict[int, str]:
    try:
        if tenant_id is not None:
            rows = db.execute(
                text("SELECT id, phone_number FROM users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            ).fetchall()
        else:
            rows = db.execute(text("SELECT id, phone_number FROM users")).fetchall()
        return {int(r[0]): (r[1] or "") for r in rows}
    except Exception:
        return {}


def set_user_phone(db, user_id: int, phone: str) -> None:
    try:
        db.execute(
            text("UPDATE users SET phone_number = :ph WHERE id = :uid"),
            {"ph": normalize_kenyan_phone(phone), "uid": int(user_id)},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SECURITY, ENCRYPTION, NOTIFICATIONS & M-PESA HELPERS
# ---------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not plain_password or not hashed_password:
        return False

    stored = hashed_password.strip()
    if stored.startswith("b'") and stored.endswith("'"):
        stored = stored[2:-1]

    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False

    if stored.startswith(("pbkdf2:", "scrypt:")):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored, plain_password)
        except Exception:
            return False

    if len(stored) == 64:
        candidate = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate.lower(), stored.lower())

    return False


def encrypt_secret(plain_text: str | None) -> str:
    val = (plain_text or "").strip()
    if not val:
        return ""
    fernet_key = os.getenv("FERNET_SECRET_KEY", "").strip()
    if not fernet_key:
        return val
    try:
        return Fernet(fernet_key.encode("utf-8")).encrypt(val.encode("utf-8")).decode("utf-8")
    except Exception:
        return val


def decrypt_secret(cipher_text: str | None) -> str:
    if not cipher_text:
        return ""
    fernet_key = os.getenv("FERNET_SECRET_KEY", "").strip()
    if not fernet_key:
        return cipher_text
    try:
        return Fernet(fernet_key.encode("utf-8")).decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return cipher_text


def is_superadmin_role(role: str | None) -> bool:
    normalized = (role or "").strip().lower().replace("_", " ")
    return normalized in ("super admin", "superadmin", "platform admin")


def normalize_kenyan_phone(raw_phone: str) -> str:
    digits = "".join(ch for ch in (raw_phone or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("7") and len(digits) == 9:
        return "254" + digits
    if digits.startswith("1") and len(digits) == 9:
        return "254" + digits
    return digits


def log_email_event(db, tenant_id: int | None, recipient: str, subject: str, body: str) -> None:
    if not recipient:
        return
    db.add(
        EmailNotificationLog(
            tenant_id=tenant_id,
            recipient_email=recipient,
            subject=subject,
            body=body,
            delivery_status="Queued",
        )
    )


def has_client_accepted_work(logs: list[AssignmentLog]) -> bool:
    """Checks whether the client explicitly accepted the latest delivered work."""
    accepted = False
    for lg in sorted(logs, key=lambda x: x.id or 0):
        note = (lg.action_note or "").lower()
        if "client accepted deliverables" in note:
            accepted = True
        elif "client requested a revision" in note or "requested revisions" in note:
            accepted = False
    return accepted


def get_status_badge_color(status: str | None) -> str:
    st = (status or "").strip().lower()
    if st == "completed":
        return "positive"
    if st == "awaiting client acceptance":
        return "teal"
    if st == "under review":
        return "amber-9"
    if st == "revision requested":
        return "deep-orange"
    if st == "in progress":
        return "blue"
    if st == "cancelled":
        return "negative"
    return "blue-grey"


def get_rated_tenants(db) -> dict[int, str]:
    """Returns tenant_id -> 'Agency Name — ⭐ 4.9/5 (12 reviews)' sorted highest rated first."""
    tenants = db.query(Tenant).filter(Tenant.status != "Suspended").all()
    reviews = db.query(ClientReview).all()

    scores_by_tenant: dict[int, list[int]] = {t.id: [] for t in tenants}
    for rev in reviews:
        if rev.tenant_id in scores_by_tenant and rev.rating is not None:
            scores_by_tenant[rev.tenant_id].append(int(rev.rating))

    ranked = []
    for t in tenants:
        ratings = scores_by_tenant.get(t.id, [])
        if ratings:
            avg = sum(ratings) / len(ratings)
            label = f"{t.business_name} — ⭐ {avg:.1f}/5 ({len(ratings)} review{'s' if len(ratings) != 1 else ''})"
            ranked.append((avg, len(ratings), t.business_name.lower(), t.id, label))
        else:
            label = f"{t.business_name} — ⭐ New Agency (No reviews yet)"
            ranked.append((-1.0, 0, t.business_name.lower(), t.id, label))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return {item[3]: item[4] for item in ranked}


def get_active_announcements(db) -> list[PlatformAnnouncement]:
    """Returns active announcements posted within the last 72 hours."""
    cutoff = datetime.utcnow() - timedelta(hours=72)
    return (
        db.query(PlatformAnnouncement)
        .filter(
            PlatformAnnouncement.is_active == True,
            PlatformAnnouncement.created_at >= cutoff,
        )
        .order_by(PlatformAnnouncement.id.desc())
        .all()
    )


def execute_daraja_stk_push(
    consumer_key: str,
    consumer_secret: str,
    passkey: str,
    shortcode: str,
    account_ref: str,
    tx_desc: str,
    phone: str,
    amount_kes: float,
) -> tuple[bool, str]:
    if not all([consumer_key, consumer_secret, passkey, shortcode]):
        return False, "Missing Daraja credentials (Consumer Key, Secret, Passkey, or Shortcode)."

    daraja_env = os.getenv("DARAJA_ENV", "sandbox").strip().lower()
    base_url = "https://api.safaricom.co.ke" if daraja_env == "production" else "https://sandbox.safaricom.co.ke"
    webhook_base = os.getenv("WEBHOOK_BASE_URL", "https://acadflow-webhook.onrender.com").rstrip("/")
    webhook_token = os.getenv("DARAJA_WEBHOOK_TOKEN", "").strip()
    callback_url = f"{webhook_base}/api/payments/daraja-callback?token={webhook_token}"

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode("utf-8")).decode("utf-8")
    formatted_phone = normalize_kenyan_phone(phone)

    try:
        with httpx.Client(timeout=20.0) as client:
            auth_resp = client.get(
                f"{base_url}/oauth/v1/generate?grant_type=client_credentials",
                auth=(consumer_key, consumer_secret),
            )
            if auth_resp.status_code != 200:
                return False, f"Daraja OAuth failed ({auth_resp.status_code}): {auth_resp.text}"
            access_token = auth_resp.json().get("access_token")

            payload = {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(round(amount_kes)),
                "PartyA": formatted_phone,
                "PartyB": shortcode,
                "PhoneNumber": formatted_phone,
                "CallBackURL": callback_url,
                "AccountReference": (account_ref or "AcadFlow")[:12],
                "TransactionDesc": (tx_desc or "AcadFlow Pay")[:13],
            }
            stk_resp = client.post(
                f"{base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = stk_resp.json()
            if stk_resp.status_code == 200 and data.get("ResponseCode") == "0":
                return True, data.get("CheckoutRequestID", "")
            return False, data.get("errorMessage") or data.get("ResponseDescription") or str(data)
    except Exception as exc:
        return False, f"Network error calling Daraja: {exc}"


def send_tenant_stk_push(tenant: Tenant, assignment: Assignment, phone: str, amount_kes: float) -> tuple[bool, str]:
    return execute_daraja_stk_push(
        consumer_key=decrypt_secret(tenant.daraja_consumer_key),
        consumer_secret=decrypt_secret(tenant.daraja_consumer_secret),
        passkey=decrypt_secret(tenant.daraja_passkey),
        shortcode=(tenant.mpesa_paybill or "").strip(),
        account_ref=(tenant.mpesa_account or assignment.ref_number or "AcadFlow"),
        tx_desc=f"Payment {assignment.ref_number}",
        phone=phone,
        amount_kes=amount_kes,
    )


def send_subscription_stk_push(tenant: Tenant, plan_name: str, phone: str, amount_kes: float) -> tuple[bool, str]:
    ck = os.getenv("PLATFORM_DARAJA_CONSUMER_KEY", "").strip() or decrypt_secret(tenant.daraja_consumer_key)
    cs = os.getenv("PLATFORM_DARAJA_CONSUMER_SECRET", "").strip() or decrypt_secret(tenant.daraja_consumer_secret)
    pk = os.getenv("PLATFORM_DARAJA_PASSKEY", "").strip() or decrypt_secret(tenant.daraja_passkey)
    sc = os.getenv("PLATFORM_MPESA_PAYBILL", "").strip() or (tenant.mpesa_paybill or "").strip()

    return execute_daraja_stk_push(
        consumer_key=ck,
        consumer_secret=cs,
        passkey=pk,
        shortcode=sc,
        account_ref=f"SUB-{tenant.id}-{plan_name}",
        tx_desc=f"Plan {plan_name}",
        phone=phone,
        amount_kes=amount_kes,
    )


def apply_theme() -> None:
    ui.colors(
        primary=BRAND_PRIMARY,
        secondary="#334155",
        accent=BRAND_ACCENT,
        positive="#10B981",
        negative="#EF4444",
        warning="#F59E0B",
    )
    ui.add_head_html(
        """
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            body { font-family: 'Inter', sans-serif; background-color: #F8FAFC; color: #0F172A; }
            .nicegui-content { padding: 0 !important; }
        </style>
        """
    )


def clear_session_and_redirect() -> None:
    nicegui_app.storage.user.clear()
    ui.navigate.to("/login")


def kpi_card(title: str, value: str, subtitle: str, icon: str, icon_color: str) -> None:
    with ui.card().classes("p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
        with ui.row().classes("w-full items-center justify-between mb-1"):
            ui.label(title).classes("text-xs font-semibold uppercase tracking-wider text-slate-500")
            ui.icon(icon, size="22px").classes(icon_color)
        ui.label(value).classes("text-2xl font-bold text-slate-900")
        ui.label(subtitle).classes("text-xs text-slate-400 mt-1")


# ---------------------------------------------------------------------------
# 1. UNIFIED LOGIN, CLIENT SIGNUP (EMAIL + PHONE) & AGENCY REGISTRATION
# ---------------------------------------------------------------------------
@ui.page("/login")
def login_page() -> None:
    apply_theme()
    if nicegui_app.storage.user.get("user_id"):
        if nicegui_app.storage.user.get("is_superadmin"):
            ui.navigate.to("/superadmin")
            return
        if nicegui_app.storage.user.get("tenant_id"):
            ui.navigate.to("/")
            return

    db = SessionLocal()
    try:
        rated_tenant_options = get_rated_tenants(db)
    finally:
        db.close()

    with ui.column().classes("w-full min-h-screen items-center justify-center bg-slate-900 p-4"):
        with ui.card().classes("w-full max-w-md p-8 rounded-2xl shadow-2xl bg-white border border-slate-200"):
            with ui.row().classes("items-center gap-3 mb-1"):
                ui.icon("school", size="32px").classes("text-emerald-600")
                ui.label("AcadFlow").classes("text-2xl font-bold tracking-tight text-slate-900")
            ui.label("Multi-Tenant Academic Agency & M-Pesa OS").classes("text-xs text-slate-500 mb-4")

            with ui.tabs().classes("w-full mb-4") as tabs:
                login_tab = ui.tab("Sign In")
                client_tab = ui.tab("Client Sign Up")
                agency_tab = ui.tab("New Agency")

            with ui.tab_panels(tabs, value=login_tab).classes("w-full"):
                with ui.tab_panel(login_tab).classes("p-0"):
                    email_input = ui.input("Email Address", placeholder="you@example.com").classes("w-full mb-3").props("outlined dense")
                    password_input = ui.input("Password", password=True, password_toggle_button=True).classes("w-full mb-5").props("outlined dense")

                    def handle_login() -> None:
                        email = (email_input.value or "").strip().lower()
                        password = password_input.value or ""
                        if not email or not password:
                            ui.notify("Enter both email and password.", type="warning")
                            return

                        db = SessionLocal()
                        try:
                            user = db.query(User).filter(func.lower(func.trim(User.email)) == email).first()
                            if not user:
                                ui.notify("No account found with that email address.", type="negative")
                                return
                            if not verify_password(password, user.password_hash):
                                ui.notify("Incorrect password for this account.", type="negative")
                                return

                            phone_map = get_user_phone_map(db)
                            superadmin = is_superadmin_role(user.role) or (user.tenant_id is None)
                            nicegui_app.storage.user.clear()
                            nicegui_app.storage.user["user_id"] = user.id
                            nicegui_app.storage.user["full_name"] = user.full_name
                            nicegui_app.storage.user["email"] = user.email
                            nicegui_app.storage.user["phone_number"] = phone_map.get(user.id, "")
                            nicegui_app.storage.user["role"] = user.role or ("Super Admin" if superadmin else "Client")
                            nicegui_app.storage.user["is_superadmin"] = superadmin

                            if superadmin:
                                ui.notify(f"Welcome, {user.full_name} (Super Admin)", type="positive")
                                ui.navigate.to("/superadmin")
                                return

                            tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
                            if not tenant:
                                ui.notify("Your account is not linked to an active agency workspace.", type="negative")
                                nicegui_app.storage.user.clear()
                                return
                            if (tenant.status or "").lower() == "suspended":
                                ui.notify("This agency workspace is suspended. Contact platform support.", type="negative")
                                nicegui_app.storage.user.clear()
                                return

                            nicegui_app.storage.user["tenant_id"] = tenant.id
                            nicegui_app.storage.user["business_name"] = tenant.business_name
                            nicegui_app.storage.user["tier"] = tenant.tier
                            ui.notify(f"Welcome back, {user.full_name}!", type="positive")
                            ui.navigate.to("/")
                        finally:
                            db.close()

                    password_input.on("keydown.enter", handle_login)
                    ui.button("Sign In", on_click=handle_login).classes(
                        "w-full py-2.5 rounded-lg bg-slate-900 text-white font-semibold hover:bg-slate-800"
                    )

                with ui.tab_panel(client_tab).classes("p-0"):
                    agency_sel = ui.select(
                        rated_tenant_options,
                        label="Choose Agency (Ranked by Client Reviews)",
                    ).classes("w-full mb-3").props("outlined dense")
                    reg_name = ui.input("Full Name").classes("w-full mb-3").props("outlined dense")
                    reg_email = ui.input("Email Address", placeholder="client@example.com").classes("w-full mb-3").props("outlined dense")
                    reg_phone = ui.input("Phone / M-Pesa Number", placeholder="0712345678 or 254712345678").classes("w-full mb-3").props("outlined dense")
                    reg_pass = ui.input("Create Password", password=True, password_toggle_button=True).classes("w-full mb-4").props("outlined dense")

                    def handle_client_register() -> None:
                        if not agency_sel.value or not reg_name.value or not reg_email.value or not reg_phone.value or not reg_pass.value:
                            ui.notify("All fields (including phone number and email) are required.", type="warning")
                            return
                        norm_phone = normalize_kenyan_phone(reg_phone.value)
                        if len(norm_phone) < 10:
                            ui.notify("Enter a valid phone number (e.g. 0712345678 or 254712345678).", type="warning")
                            return
                        email = reg_email.value.strip().lower()
                        db = SessionLocal()
                        try:
                            if db.query(User).filter(func.lower(func.trim(User.email)) == email).first():
                                ui.notify("An account with that email already exists.", type="negative")
                                return
                            new_client = User(
                                tenant_id=agency_sel.value,
                                full_name=reg_name.value.strip(),
                                email=email,
                                password_hash=hash_password(reg_pass.value),
                                role="Client",
                                commission_rate=0.0,
                            )
                            db.add(new_client)
                            db.flush()
                            set_user_phone(db, new_client.id, norm_phone)
                            db.commit()
                            ui.notify("Client account created! Sign in with your email to submit your assignment.", type="positive")
                            tabs.set_value(login_tab)
                        finally:
                            db.close()

                    ui.button("Create Client Account", on_click=handle_client_register).classes(
                        "w-full py-2.5 rounded-lg bg-emerald-600 text-white font-semibold hover:bg-emerald-700"
                    )

                with ui.tab_panel(agency_tab).classes("p-0"):
                    biz_name_in = ui.input("Agency / Business Name").classes("w-full mb-3").props("outlined dense")
                    owner_name_in = ui.input("Admin Full Name").classes("w-full mb-3").props("outlined dense")
                    owner_email_in = ui.input("Admin Email").classes("w-full mb-3").props("outlined dense")
                    owner_phone_in = ui.input("Admin Phone Number", placeholder="0712345678").classes("w-full mb-3").props("outlined dense")
                    owner_pass_in = ui.input("Admin Password", password=True, password_toggle_button=True).classes("w-full mb-4").props("outlined dense")

                    def handle_agency_register() -> None:
                        if not biz_name_in.value or not owner_name_in.value or not owner_email_in.value or not owner_pass_in.value:
                            ui.notify("All fields are required.", type="warning")
                            return
                        email = owner_email_in.value.strip().lower()
                        db = SessionLocal()
                        try:
                            if db.query(User).filter(func.lower(func.trim(User.email)) == email).first():
                                ui.notify("Email already registered.", type="negative")
                                return
                            new_tenant = Tenant(
                                business_name=biz_name_in.value.strip(),
                                tier="Starter",
                                status="Trial",
                                trial_ends_at=datetime.utcnow() + timedelta(days=14),
                            )
                            db.add(new_tenant)
                            db.flush()
                            admin_user = User(
                                tenant_id=new_tenant.id,
                                full_name=owner_name_in.value.strip(),
                                email=email,
                                password_hash=hash_password(owner_pass_in.value),
                                role="Admin",
                                commission_rate=0.0,
                            )
                            db.add(admin_user)
                            db.flush()
                            if owner_phone_in.value:
                                set_user_phone(db, admin_user.id, owner_phone_in.value)
                            db.commit()
                            ui.notify("14-day trial workspace created! Please sign in.", type="positive")
                            tabs.set_value(login_tab)
                        finally:
                            db.close()

                    ui.button("Launch Agency Workspace", on_click=handle_agency_register).classes(
                        "w-full py-2.5 rounded-lg bg-slate-900 text-white font-semibold hover:bg-slate-800"
                    )


# ---------------------------------------------------------------------------
# 2. SUPER ADMIN PLATFORM CONSOLE
# ---------------------------------------------------------------------------
@ui.page("/superadmin")
def superadmin_page() -> None:
    apply_theme()
    current_sa_id = nicegui_app.storage.user.get("user_id")
    if not current_sa_id or not nicegui_app.storage.user.get("is_superadmin"):
        clear_session_and_redirect()
        return

    full_name = nicegui_app.storage.user.get("full_name", "Super Admin")

    with ui.header().classes("bg-slate-950 text-white px-6 py-3 flex items-center justify-between shadow-sm"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("admin_panel_settings", size="26px").classes("text-amber-400")
            ui.label("AcadFlow Platform Control").classes("text-lg font-bold tracking-tight")
            ui.badge("SUPER ADMIN", color="amber").classes("text-xs font-semibold text-slate-950 px-2 py-0.5")

        with ui.row().classes("items-center gap-4"):
            ui.label(full_name).classes("text-xs text-slate-300")
            ui.button("Logout", icon="logout", on_click=clear_session_and_redirect).props("flat dense text-color=white").classes("text-xs")

    with ui.column().classes("w-full max-w-7xl mx-auto p-6 gap-6"):
        with ui.tabs().classes("w-full bg-white rounded-xl border border-slate-200 px-2 shadow-sm") as sa_tabs:
            sa_tenants_tab = ui.tab("Agencies & Workspaces", icon="business")
            sa_users_tab = ui.tab("Platform Staff & Users", icon="manage_accounts")
            sa_tickets_tab = ui.tab("Support Desk", icon="support_agent")
            sa_plans_tab = ui.tab("Subscription Plans & Billing", icon="workspace_premium")
            sa_comms_tab = ui.tab("Broadcasts & Email Logs", icon="campaign")

        def inspect_tenant(tenant_id_val: int) -> None:
            tid_int = int(tenant_id_val)
            db = SessionLocal()
            try:
                t = db.query(Tenant).filter(Tenant.id == tid_int).first()
                if not t:
                    ui.notify("Tenant workspace not found.", type="negative")
                    return
                nicegui_app.storage.user["tenant_id"] = t.id
                nicegui_app.storage.user["business_name"] = t.business_name
                nicegui_app.storage.user["tier"] = t.tier
            finally:
                db.close()
            ui.navigate.to(f"/?tenant_id={tid_int}")

        with ui.tab_panels(sa_tabs, value=sa_tenants_tab).classes("w-full bg-transparent p-0"):
            # --- SA Tab 1: Agencies & Lifecycle Control ---
            with ui.tab_panel(sa_tenants_tab).classes("p-0 gap-6"):
                sa_t_box = ui.column().classes("w-full gap-6")

                def render_sa_tenants() -> None:
                    sa_t_box.clear()
                    db = SessionLocal()
                    try:
                        tenants = db.query(Tenant).order_by(Tenant.id.desc()).all()
                        users = db.query(User).all()
                        assignments = db.query(Assignment).all()
                        plans = [p.name for p in db.query(SubscriptionPlan).all()] or ["Starter", "Pro", "Enterprise"]
                    finally:
                        db.close()

                    total_gmv = sum(float(a.amount_paid_kes or 0) for a in assignments)

                    with sa_t_box:
                        with ui.grid(columns=4).classes("w-full gap-4"):
                            kpi_card("Registered Agencies", str(len(tenants)), "Total tenant workspaces", "business", "text-blue-600")
                            kpi_card("Total Platform Users", str(len(users)), "Admins, writers & clients", "groups", "text-purple-600")
                            kpi_card("Total Orders", str(len(assignments)), "Across all agencies", "assignment", "text-amber-600")
                            kpi_card("Verified GMV (KES)", f"{total_gmv:,.0f}", "Client M-Pesa volume", "payments", "text-emerald-600")

                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Provision New Agency Workspace").classes("text-base font-bold text-slate-900 mb-3")
                            with ui.row().classes("w-full items-center gap-3"):
                                t_biz = ui.input("Business Name").props("outlined dense").classes("w-52")
                                t_tier = ui.select(plans, value=plans[0], label="Plan Tier").props("outlined dense").classes("w-36")
                                t_admin = ui.input("Owner Name").props("outlined dense").classes("w-44")
                                t_email = ui.input("Owner Email").props("outlined dense").classes("w-52")
                                t_pass = ui.input("Temp Password", password=True).props("outlined dense").classes("w-40")

                                def provision_tenant() -> None:
                                    if not t_biz.value or not t_admin.value or not t_email.value or not t_pass.value:
                                        ui.notify("All fields are required.", type="warning")
                                        return
                                    db = SessionLocal()
                                    try:
                                        if db.query(User).filter(func.lower(func.trim(User.email)) == t_email.value.strip().lower()).first():
                                            ui.notify("Owner email already exists.", type="negative")
                                            return
                                        nt = Tenant(
                                            business_name=t_biz.value.strip(),
                                            tier=t_tier.value,
                                            status="Active",
                                            subscription_ends_at=datetime.utcnow() + timedelta(days=30),
                                        )
                                        db.add(nt)
                                        db.flush()
                                        db.add(
                                            User(
                                                tenant_id=nt.id,
                                                full_name=t_admin.value.strip(),
                                                email=t_email.value.strip().lower(),
                                                password_hash=hash_password(t_pass.value),
                                                role="Admin",
                                                commission_rate=0.0,
                                            )
                                        )
                                        db.commit()
                                        ui.notify(f"Provisioned {nt.business_name}!", type="positive")
                                        render_sa_tenants()
                                    finally:
                                        db.close()

                                ui.button("Create Agency", icon="add_business", on_click=provision_tenant).classes("bg-slate-900 text-white")

                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Tenant Workspaces & Lifecycle Controls").classes("text-lg font-bold text-slate-900 mb-4")
                            rows = [
                                {
                                    "id": t.id,
                                    "business_name": t.business_name,
                                    "tier": t.tier or "Starter",
                                    "status": t.status or "Active",
                                    "paybill": t.mpesa_paybill or "Not Set",
                                    "users_count": sum(1 for u in users if u.tenant_id == t.id),
                                    "orders_count": sum(1 for a in assignments if a.tenant_id == t.id),
                                }
                                for t in tenants
                            ]
                            columns = [
                                {"name": "id", "label": "ID", "field": "id", "align": "left", "sortable": True},
                                {"name": "business_name", "label": "Agency Name", "field": "business_name", "align": "left", "sortable": True},
                                {"name": "tier", "label": "Tier", "field": "tier", "align": "left", "sortable": True},
                                {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
                                {"name": "paybill", "label": "Paybill/Till", "field": "paybill", "align": "left"},
                                {"name": "users_count", "label": "Users", "field": "users_count", "align": "right"},
                                {"name": "orders_count", "label": "Orders", "field": "orders_count", "align": "right"},
                                {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
                            ]
                            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=10).classes("w-full flat")

                            def toggle_tenant_status(tid: int) -> None:
                                db = SessionLocal()
                                try:
                                    t = db.query(Tenant).filter(Tenant.id == int(tid)).first()
                                    if t:
                                        t.status = "Suspended" if (t.status or "Active") == "Active" else "Active"
                                        db.commit()
                                        ui.notify(f"{t.business_name} is now {t.status}", type="info")
                                        render_sa_tenants()
                                finally:
                                    db.close()

                            table.add_slot(
                                "body-cell-actions",
                                r"""
                                <q-td :props="props" class="q-gutter-xs">
                                    <q-btn size="sm" unelevated color="primary" text-color="white" icon="open_in_new" label="Inspect"
                                           @click.stop="$parent.$emit('inspect', props.row)" />
                                    <q-btn size="sm" unelevated color="warning" text-color="dark" icon="sync" label="Toggle Status"
                                           @click.stop="$parent.$emit('toggle', props.row)" />
                                </q-td>
                                """,
                            )
                            table.on("inspect", lambda e: inspect_tenant(e.args["id"]))
                            table.on("toggle", lambda e: toggle_tenant_status(e.args["id"]))

                render_sa_tenants()

            # --- SA Tab 2: Platform Staff & Users Management (Name, Email, Phone visible) ---
            with ui.tab_panel(sa_users_tab).classes("p-0"):
                sa_u_box = ui.column().classes("w-full gap-4")

                def render_sa_users() -> None:
                    sa_u_box.clear()
                    db = SessionLocal()
                    try:
                        all_users = db.query(User).order_by(User.id.desc()).all()
                        t_map = {t.id: t.business_name for t in db.query(Tenant).all()}
                        phone_map = get_user_phone_map(db)
                    finally:
                        db.close()

                    with sa_u_box:
                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            with ui.row().classes("w-full items-center justify-between mb-3"):
                                ui.label("All Platform Staff, Writers, Admins & Clients").classes("text-lg font-bold text-slate-900")
                                u_search = ui.input(placeholder="Filter by name, email, phone, agency...").props("outlined dense clearable").classes("w-72")

                            rows = [
                                {
                                    "id": u.id,
                                    "full_name": u.full_name,
                                    "email": u.email,
                                    "phone": phone_map.get(u.id) or "—",
                                    "role": u.role,
                                    "agency": t_map.get(u.tenant_id, "Platform Global"),
                                    "can_delete": u.id != current_sa_id and not is_superadmin_role(u.role),
                                }
                                for u in all_users
                            ]
                            cols = [
                                {"name": "full_name", "label": "Full Name", "field": "full_name", "align": "left", "sortable": True},
                                {"name": "email", "label": "Email", "field": "email", "align": "left", "sortable": True},
                                {"name": "phone", "label": "Phone Number", "field": "phone", "align": "left", "sortable": True},
                                {"name": "role", "label": "Role", "field": "role", "align": "left", "sortable": True},
                                {"name": "agency", "label": "Workspace", "field": "agency", "align": "left", "sortable": True},
                                {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
                            ]
                            ut = ui.table(columns=cols, rows=rows, row_key="id", pagination=12).classes("w-full flat")
                            u_search.bind_value(ut, "filter")

                            def confirm_sa_delete_user(uid: int) -> None:
                                db = SessionLocal()
                                try:
                                    target = db.query(User).filter(User.id == int(uid)).first()
                                    if not target:
                                        return
                                    t_name, t_role = target.full_name, target.role
                                finally:
                                    db.close()

                                with ui.dialog() as d, ui.card().classes("p-6 rounded-2xl max-w-md"):
                                    ui.label(f"Delete {t_role}: {t_name}?").classes("text-lg font-bold text-slate-900 mb-2")
                                    ui.label("Any assigned tasks will be unassigned. This cannot be undone.").classes("text-sm text-slate-600 mb-4")

                                    def do_del() -> None:
                                        db = SessionLocal()
                                        try:
                                            if db.query(Assignment).filter(Assignment.client_id == int(uid)).count() > 0:
                                                ui.notify("Cannot delete a client with existing assignments.", type="warning")
                                                d.close()
                                                return
                                            db.query(Assignment).filter(Assignment.staff_id == int(uid)).update({"staff_id": None})
                                            usr = db.query(User).filter(User.id == int(uid)).first()
                                            if usr:
                                                db.delete(usr)
                                                db.commit()
                                                ui.notify(f"Removed {t_name}.", type="positive")
                                            d.close()
                                            render_sa_users()
                                            render_sa_tenants()
                                        finally:
                                            db.close()

                                    with ui.row().classes("w-full justify-end gap-2"):
                                        ui.button("Cancel", on_click=d.close).props("flat")
                                        ui.button("Delete Permanently", icon="delete", on_click=do_del).classes("bg-red-600 text-white")
                                d.open()

                            ut.add_slot(
                                "body-cell-actions",
                                r"""
                                <q-td :props="props">
                                    <q-btn v-if="props.row.can_delete" size="sm" unelevated color="negative" text-color="white"
                                           icon="delete" label="Delete User" @click.stop="$parent.$emit('del_user', props.row)" />
                                </q-td>
                                """,
                            )
                            ut.on("del_user", lambda e: confirm_sa_delete_user(e.args["id"]))

                render_sa_users()

            # --- SA Tab 3: Support Desk ---
            with ui.tab_panel(sa_tickets_tab).classes("p-0"):
                sa_tk_box = ui.column().classes("w-full gap-4")

                def render_sa_tickets() -> None:
                    sa_tk_box.clear()
                    db = SessionLocal()
                    try:
                        tickets = db.query(SupportTicket).order_by(SupportTicket.id.desc()).all()
                    finally:
                        db.close()

                    with sa_tk_box:
                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Tenant Support Tickets").classes("text-lg font-bold text-slate-900 mb-3")
                            if not tickets:
                                ui.label("No support tickets submitted yet.").classes("text-sm text-slate-500")
                            for tk in tickets:
                                with ui.card().classes("w-full p-4 mb-3 border border-slate-200 shadow-none bg-slate-50"):
                                    with ui.row().classes("w-full items-center justify-between mb-1"):
                                        ui.label(f"#{tk.id} • {tk.business_name} — {tk.subject}").classes("font-bold text-sm text-slate-900")
                                        ui.badge(tk.status, color="emerald" if tk.status == "Resolved" else "amber")
                                    ui.label(tk.description or "").classes("text-sm text-slate-700 mb-2")
                                    reply_in = ui.input("Super Admin Reply", value=tk.admin_reply or "").props("outlined dense bg-color=white").classes("w-full mb-2")

                                    def save_reply(ticket_id: int = tk.id, inp=reply_in) -> None:
                                        db = SessionLocal()
                                        try:
                                            item = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
                                            if item:
                                                item.admin_reply = (inp.value or "").strip()
                                                item.status = "Resolved"
                                                db.commit()
                                                ui.notify(f"Reply sent & Ticket #{ticket_id} resolved!", type="positive")
                                                render_sa_tickets()
                                        finally:
                                            db.close()

                                    ui.button("Send Reply & Mark Resolved", icon="check_circle", on_click=save_reply).classes("bg-emerald-600 text-white text-xs")

                render_sa_tickets()

            # --- SA Tab 4: Subscription Plans & SaaS Payments ---
            with ui.tab_panel(sa_plans_tab).classes("p-0 gap-6"):
                sa_pl_box = ui.column().classes("w-full gap-6")

                def render_sa_plans() -> None:
                    sa_pl_box.clear()
                    db = SessionLocal()
                    try:
                        plans = db.query(SubscriptionPlan).all()
                        sub_pays = db.query(PlatformSubscriptionPayment).order_by(PlatformSubscriptionPayment.id.desc()).all()
                        t_map = {t.id: t.business_name for t in db.query(Tenant).all()}
                    finally:
                        db.close()

                    with sa_pl_box:
                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Create / Update SaaS Subscription Plan").classes("text-base font-bold text-slate-900 mb-3")
                            with ui.row().classes("w-full items-center gap-3"):
                                p_name = ui.input("Plan Name (Starter/Pro/Enterprise)").props("outlined dense").classes("w-48")
                                p_mo = ui.number("Monthly (KES)", value=2500, min=0).props("outlined dense").classes("w-36")
                                p_yr = ui.number("Annual (KES)", value=25000, min=0).props("outlined dense").classes("w-36")
                                p_staff = ui.number("Max Staff", value=5, min=1).props("outlined dense").classes("w-28")
                                p_proj = ui.number("Max Active Orders", value=50, min=1).props("outlined dense").classes("w-36")
                                p_feat = ui.input("Features Summary").props("outlined dense").classes("flex-1")

                                def save_plan() -> None:
                                    if not p_name.value:
                                        ui.notify("Plan name required.", type="warning")
                                        return
                                    db = SessionLocal()
                                    try:
                                        existing = db.query(SubscriptionPlan).filter(func.lower(SubscriptionPlan.name) == p_name.value.strip().lower()).first()
                                        if existing:
                                            existing.monthly_price_kes = float(p_mo.value or 0)
                                            existing.annual_price_kes = float(p_yr.value or 0)
                                            existing.max_staff = int(p_staff.value or 5)
                                            existing.max_active_projects = int(p_proj.value or 50)
                                            existing.features_summary = (p_feat.value or "").strip()
                                        else:
                                            db.add(
                                                SubscriptionPlan(
                                                    name=p_name.value.strip(),
                                                    monthly_price_kes=float(p_mo.value or 0),
                                                    annual_price_kes=float(p_yr.value or 0),
                                                    max_staff=int(p_staff.value or 5),
                                                    max_active_projects=int(p_proj.value or 50),
                                                    features_summary=(p_feat.value or "").strip(),
                                                )
                                            )
                                        db.commit()
                                        ui.notify("Subscription plan saved!", type="positive")
                                        render_sa_plans()
                                    finally:
                                        db.close()

                                ui.button("Save Plan", on_click=save_plan).classes("bg-slate-900 text-white")

                        if plans:
                            with ui.grid(columns=3).classes("w-full gap-4"):
                                for pl in plans:
                                    with ui.card().classes("p-5 rounded-xl bg-white border border-slate-200"):
                                        ui.label(pl.name).classes("text-lg font-bold text-slate-900")
                                        ui.label(f"KES {float(pl.monthly_price_kes or 0):,.0f}/mo • KES {float(pl.annual_price_kes or 0):,.0f}/yr").classes("text-sm font-semibold text-emerald-600")
                                        ui.label(f"Up to {pl.max_staff} staff • {pl.max_active_projects} active orders").classes("text-xs text-slate-500 mt-1")
                                        ui.label(pl.features_summary or "").classes("text-xs text-slate-600 mt-2")

                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Tenant SaaS Subscription Payments").classes("text-lg font-bold text-slate-900 mb-3")
                            rows = [
                                {
                                    "id": sp.id,
                                    "agency": t_map.get(sp.tenant_id, f"Tenant #{sp.tenant_id}"),
                                    "plan": sp.plan_name,
                                    "amount": f"{float(sp.amount_kes or 0):,.0f}",
                                    "mpesa": sp.mpesa_code,
                                    "status": sp.status,
                                }
                                for sp in sub_pays
                            ]
                            cols = [
                                {"name": "agency", "label": "Agency", "field": "agency", "align": "left"},
                                {"name": "plan", "label": "Plan", "field": "plan", "align": "left"},
                                {"name": "amount", "label": "Amount (KES)", "field": "amount", "align": "right"},
                                {"name": "mpesa", "label": "M-Pesa Code", "field": "mpesa", "align": "left"},
                                {"name": "status", "label": "Status", "field": "status", "align": "left"},
                            ]
                            ui.table(columns=cols, rows=rows, row_key="id").classes("w-full flat")

                render_sa_plans()

            # --- SA Tab 5: Broadcasts (72-hour auto-expiry + Delete) & Email Logs ---
            with ui.tab_panel(sa_comms_tab).classes("p-0 gap-6"):
                sa_cm_box = ui.column().classes("w-full gap-6")

                def render_sa_comms() -> None:
                    sa_cm_box.clear()
                    db = SessionLocal()
                    try:
                        anns = db.query(PlatformAnnouncement).order_by(PlatformAnnouncement.id.desc()).limit(25).all()
                        emails = db.query(EmailNotificationLog).order_by(EmailNotificationLog.id.desc()).limit(25).all()
                    finally:
                        db.close()

                    now = datetime.utcnow()

                    with sa_cm_box:
                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Broadcast Announcement to All Tenants (Auto-expires after 72 hours)").classes("text-base font-bold text-slate-900 mb-3")
                            with ui.row().classes("w-full items-center gap-3"):
                                ann_title = ui.input("Title").props("outlined dense").classes("w-64")
                                ann_msg = ui.input("Message").props("outlined dense").classes("flex-1")

                                def post_ann() -> None:
                                    if not ann_title.value or not ann_msg.value:
                                        return
                                    db = SessionLocal()
                                    try:
                                        db.add(
                                            PlatformAnnouncement(
                                                title=ann_title.value.strip(),
                                                message=ann_msg.value.strip(),
                                                is_active=True,
                                                created_at=datetime.utcnow(),
                                            )
                                        )
                                        db.commit()
                                        ui.notify("Announcement live for 72 hours!", type="positive")
                                        render_sa_comms()
                                    finally:
                                        db.close()

                                ui.button("Publish (72h)", icon="campaign", on_click=post_ann).classes("bg-slate-900 text-white")

                            if not anns:
                                ui.label("No announcements posted yet.").classes("text-xs text-slate-400 mt-2")

                            for a in anns:
                                age = (now - a.created_at) if a.created_at else timedelta(hours=999)
                                is_expired = age > timedelta(hours=72)
                                remaining_hrs = max(0, int(72 - (age.total_seconds() // 3600)))

                                with ui.row().classes("w-full items-center justify-between py-2.5 border-b border-slate-100 gap-3"):
                                    with ui.column().classes("gap-0 flex-1"):
                                        ui.label(f"{a.title}: {a.message}").classes("text-sm font-medium text-slate-900")
                                        ui.label(
                                            "Expired (> 72h)" if is_expired else f"Expires in ~{remaining_hrs}h"
                                        ).classes("text-xs text-slate-400")

                                    with ui.row().classes("items-center gap-2"):
                                        def toggle_ann(aid: int = a.id) -> None:
                                            db = SessionLocal()
                                            try:
                                                item = db.query(PlatformAnnouncement).filter(PlatformAnnouncement.id == aid).first()
                                                if item:
                                                    item.is_active = not item.is_active
                                                    if item.is_active:
                                                        item.created_at = datetime.utcnow()
                                                    db.commit()
                                                    render_sa_comms()
                                            finally:
                                                db.close()

                                        def delete_ann(aid: int = a.id) -> None:
                                            db = SessionLocal()
                                            try:
                                                item = db.query(PlatformAnnouncement).filter(PlatformAnnouncement.id == aid).first()
                                                if item:
                                                    db.delete(item)
                                                    db.commit()
                                                    ui.notify("Announcement deleted.", type="info")
                                                    render_sa_comms()
                                            finally:
                                                db.close()

                                        btn_lbl = "Expired (Re-broadcast 72h)" if (a.is_active and is_expired) else ("Active" if a.is_active else "Archived")
                                        ui.button(btn_lbl, on_click=toggle_ann).props(
                                            f"dense {'unelevated color=positive text-color=white' if (a.is_active and not is_expired) else 'outline color=grey'}"
                                        )
                                        ui.button("Delete", icon="delete", on_click=delete_ann).props("dense unelevated color=negative text-color=white")

                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            ui.label("Recent Platform Notification Logs").classes("text-base font-bold text-slate-900 mb-3")
                            for em in emails:
                                ui.label(f"[{em.delivery_status}] To: {em.recipient_email} — {em.subject}").classes("text-xs text-slate-600 py-1 border-b border-slate-100")

                render_sa_comms()


# ---------------------------------------------------------------------------
# 3. TENANT WORKSPACE (Admin <-> Staff/Writers <-> Clients)
# ---------------------------------------------------------------------------
@ui.page("/")
def dashboard_page(tenant_id: int | None = None) -> None:
    apply_theme()
    user_id = nicegui_app.storage.user.get("user_id")
    is_superadmin = bool(nicegui_app.storage.user.get("is_superadmin"))

    if not user_id:
        clear_session_and_redirect()
        return

    if is_superadmin and tenant_id is not None:
        db = SessionLocal()
        try:
            inspected = db.query(Tenant).filter(Tenant.id == int(tenant_id)).first()
            if inspected:
                nicegui_app.storage.user["tenant_id"] = inspected.id
                nicegui_app.storage.user["business_name"] = inspected.business_name
                nicegui_app.storage.user["tier"] = inspected.tier
        finally:
            db.close()

    tenant_id = nicegui_app.storage.user.get("tenant_id")

    if is_superadmin and not tenant_id:
        ui.navigate.to("/superadmin")
        return
    if not tenant_id:
        clear_session_and_redirect()
        return

    business_name = nicegui_app.storage.user.get("business_name", "AcadFlow Agency")
    tier = nicegui_app.storage.user.get("tier", "Starter")
    full_name = nicegui_app.storage.user.get("full_name", "User")
    role = (nicegui_app.storage.user.get("role") or "Admin").strip()
    is_client = role.lower() == "client"
    is_writer = role.lower() == "writer"
    is_tenant_admin = not is_superadmin and not is_client and not is_writer

    def open_assignment_workspace(assignment_id: int) -> None:
        try:
            workspace_dialog = ui.dialog()
            workspace_dialog.on("hide", workspace_dialog.clear)
            with workspace_dialog, ui.card().classes("w-full max-w-4xl p-6 rounded-2xl max-h-[90vh] overflow-y-auto"):
                ws_body = ui.column().classes("w-full gap-4")

            def refresh_workspace() -> None:
                ws_body.clear()
                db = SessionLocal()
                try:
                    asg = db.query(Assignment).filter(Assignment.id == int(assignment_id)).first()
                    if not asg:
                        with ws_body:
                            ui.label("Assignment not found.").classes("text-sm text-red-600")
                        return
                    asg_tid = asg.tenant_id
                    all_tenant_users = db.query(User).filter(User.tenant_id == asg_tid).all()
                    phone_map = get_user_phone_map(db, asg_tid)
                    client_user = next((u for u in all_tenant_users if u.id == asg.client_id), None)
                    client_phone = phone_map.get(asg.client_id, "") if asg.client_id else ""

                    writers = {
                        u.id: u.full_name
                        for u in all_tenant_users
                        if (u.role or "").strip().lower() == "writer"
                    }
                    writer_rates = {
                        u.id: float(u.commission_rate or 50.0)
                        for u in all_tenant_users
                        if (u.role or "").strip().lower() == "writer"
                    }
                    docs = db.query(AssignmentDocument).filter(AssignmentDocument.assignment_id == asg.id).order_by(AssignmentDocument.id.desc()).all()
                    msgs = db.query(AssignmentMessage).filter(AssignmentMessage.assignment_id == asg.id).order_by(AssignmentMessage.id.asc()).all()
                    logs = db.query(AssignmentLog).filter(AssignmentLog.assignment_id == asg.id).order_by(AssignmentLog.id.desc()).all()
                    review = db.query(ClientReview).filter(ClientReview.assignment_id == asg.id).first()
                finally:
                    db.close()

                balance_due = max(float(asg.quoted_cost_kes or 0) - float(asg.amount_paid_kes or 0), 0.0)
                is_paid_in_full = balance_due <= 0.0
                client_accepted = has_client_accepted_work(logs) or (asg.status == "Completed")
                is_released_to_client = asg.status in ("Awaiting Client Acceptance", "Completed")
                writer_deliverables = [
                    d for d in docs if d.doc_category in ("Draft Submission", "Final Deliverable", "Turnitin Report")
                ]

                if is_client:
                    visible_docs = [
                        d for d in docs
                        if d.doc_category in ("Client Brief", "Revision Instructions") or is_released_to_client
                    ]
                    hidden_pending_docs = len(docs) - len(visible_docs)
                    visible_msgs = [m for m in msgs if (m.channel or "Client-Agency") == "Client-Agency"]
                elif is_writer:
                    visible_docs = docs
                    hidden_pending_docs = 0
                    visible_msgs = [m for m in msgs if (m.channel or "Team") == "Team"]
                else:
                    visible_docs = docs
                    hidden_pending_docs = 0
                    visible_msgs = msgs

                with ws_body:
                    # Header with highlighted current status badge
                    with ui.row().classes("w-full items-start justify-between gap-3"):
                        with ui.column().classes("gap-1"):
                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                ui.label(f"{asg.ref_number} — {asg.title}").classes("text-lg font-bold text-slate-900")
                                ui.badge(asg.status or "Pending", color=get_status_badge_color(asg.status)).classes("text-xs font-semibold px-2.5 py-0.5")
                                if client_accepted:
                                    ui.badge("CLIENT ACCEPTED", color="emerald").classes("text-[10px] font-bold")
                                if is_paid_in_full:
                                    ui.badge("PAID IN FULL", color="emerald").classes("text-[10px] font-bold")
                                else:
                                    ui.badge(f"BALANCE DUE: KES {balance_due:,.0f}", color="red").classes("text-[10px] font-bold")
                            ui.label(asg.description or "No brief provided.").classes("text-xs text-slate-500")
                        ui.button(icon="close", on_click=workspace_dialog.close).props("flat round dense")

                    # Client Contact Strip visible to Tenant Admin & Super Admin
                    if (is_tenant_admin or is_superadmin) and client_user:
                        with ui.row().classes("w-full items-center justify-between bg-blue-50/70 border border-blue-200 px-3.5 py-2 rounded-xl"):
                            with ui.row().classes("items-center gap-3 flex-wrap text-xs text-slate-800"):
                                ui.icon("contact_phone", size="18px").classes("text-blue-700")
                                ui.label(f"Client: {client_user.full_name}").classes("font-bold")
                                ui.label(f"Email: {client_user.email}").classes("text-slate-600")
                                ui.label(f"Phone: {client_phone or 'Not provided'}").classes("font-semibold text-blue-900")
                            if not is_paid_in_full:
                                def send_admin_payment_reminder() -> None:
                                    db = SessionLocal()
                                    try:
                                        item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                        if not item:
                                            return
                                        db.add(
                                            AssignmentLog(
                                                tenant_id=asg_tid,
                                                assignment_id=item.id,
                                                actor_name=full_name,
                                                action_note=f"PAYMENT REMINDER SENT to {client_user.full_name} ({client_phone or client_user.email}) for balance KES {balance_due:,.0f}",
                                            )
                                        )
                                        db.add(
                                            AssignmentMessage(
                                                tenant_id=asg_tid,
                                                assignment_id=item.id,
                                                sender_name=full_name,
                                                sender_role=role,
                                                channel="Client-Agency",
                                                message_text=f"Payment Reminder: Please settle your remaining balance of KES {balance_due:,.0f} via M-Pesa to finalize and close order {item.ref_number}.",
                                            )
                                        )
                                        log_email_event(
                                            db,
                                            asg_tid,
                                            client_user.email,
                                            f"Payment Reminder — Order {item.ref_number}",
                                            f"Hello {client_user.full_name}, please complete your payment of KES {balance_due:,.0f} for '{item.title}'.",
                                        )
                                        db.commit()
                                        ui.notify(f"Payment reminder sent to {client_user.full_name}!", type="positive")
                                        refresh_workspace()
                                    finally:
                                        db.close()

                                ui.button("Send Payment Reminder", icon="notifications_active", on_click=send_admin_payment_reminder).props("dense unelevated color=amber-9").classes("text-xs")

                    # Role-specific action bar
                    if is_writer:
                        writer_allowed_statuses = [
                            s for s in ASSIGNMENT_STATUSES if s not in ("Completed", "Cancelled", "Awaiting Client Acceptance")
                        ]
                        if not writer_allowed_statuses:
                            writer_allowed_statuses = ["In Progress", "Under Review"]
                        current_w_val = asg.status if asg.status in writer_allowed_statuses else writer_allowed_statuses[-1]

                        with ui.row().classes("w-full items-center justify-between bg-slate-50 p-3 rounded-xl border border-slate-200"):
                            with ui.row().classes("items-center gap-3"):
                                w_status_sel = ui.select(writer_allowed_statuses, value=current_w_val, label="Progress Status").props("outlined dense").classes("w-52")

                                def writer_update_status() -> None:
                                    db = SessionLocal()
                                    try:
                                        item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                        if not item:
                                            return
                                        if item.status == "Completed":
                                            ui.notify("Order is already closed and completed.", type="info")
                                            return
                                        old_st = item.status
                                        item.status = w_status_sel.value
                                        db.add(
                                            AssignmentLog(
                                                tenant_id=asg_tid,
                                                assignment_id=item.id,
                                                actor_name=full_name,
                                                action_note=f"STATUS CHANGED (Writer): {old_st} -> {item.status}",
                                            )
                                        )
                                        db.commit()
                                        ui.notify(
                                            "Submitted to Tenant Admin for quality approval!" if item.status == "Under Review" else "Status updated!",
                                            type="positive",
                                        )
                                        render_orders()
                                        refresh_workspace()
                                    finally:
                                        db.close()

                                ui.button("Update / Submit for Approval", icon="send", on_click=writer_update_status).classes("bg-slate-900 text-white text-xs")
                            ui.label("Deliverables stay gated until Tenant Admin approves.").classes("text-xs text-amber-700 font-medium")

                    elif is_tenant_admin or is_superadmin:
                        base_statuses = list(dict.fromkeys([*ASSIGNMENT_STATUSES, "Awaiting Client Acceptance", asg.status])) if asg.status else list(dict.fromkeys([*ASSIGNMENT_STATUSES, "Awaiting Client Acceptance"]))
                        current_status_val = asg.status if asg.status in base_statuses else base_statuses[0]
                        safe_writer_val = asg.staff_id if asg.staff_id in writers else None

                        with ui.row().classes("w-full items-center gap-3 bg-slate-50 p-3 rounded-xl border border-slate-200 flex-wrap"):
                            status_sel = ui.select(base_statuses, value=current_status_val, label="Workflow Status").props("outlined dense").classes("w-52")
                            writer_sel = ui.select(writers, value=safe_writer_val, label="Assigned Writer").props("outlined dense clearable").classes("w-48")
                            quote_edit = ui.number("Quote (KES)", value=float(asg.quoted_cost_kes or 0), min=0).props("outlined dense").classes("w-32")

                            def save_workflow(approve_release: bool = False) -> None:
                                db = SessionLocal()
                                try:
                                    item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                    if not item:
                                        return
                                    old_status = item.status
                                    old_writer = item.staff_id
                                    item.quoted_cost_kes = float(quote_edit.value or 0)
                                    bal_now = max(float(item.quoted_cost_kes or 0) - float(item.amount_paid_kes or 0), 0.0)
                                    item_logs = db.query(AssignmentLog).filter(AssignmentLog.assignment_id == item.id).all()
                                    accepted_now = has_client_accepted_work(item_logs)

                                    if approve_release:
                                        if accepted_now and bal_now <= 0:
                                            item.status = "Completed"
                                            if not item.completed_at:
                                                item.completed_at = datetime.utcnow()
                                        else:
                                            item.status = "Awaiting Client Acceptance"
                                    else:
                                        if status_sel.value == "Completed" and (not accepted_now or bal_now > 0):
                                            ui.notify(
                                                "Order can only close as 'Completed' once the client accepts the work AND pays in full. Set to 'Awaiting Client Acceptance'.",
                                                type="warning",
                                            )
                                            item.status = "Awaiting Client Acceptance"
                                        else:
                                            item.status = status_sel.value
                                            if item.status == "Completed" and not item.completed_at:
                                                item.completed_at = datetime.utcnow()

                                    item.staff_id = writer_sel.value
                                    if writer_sel.value and writer_sel.value != old_writer:
                                        item.staff_commission_pct = writer_rates.get(writer_sel.value, 50.0)
                                        w_name = writers.get(writer_sel.value, "Writer")
                                        db.add(
                                            AssignmentLog(
                                                tenant_id=asg_tid,
                                                assignment_id=item.id,
                                                actor_name=full_name,
                                                action_note=f"WRITER ASSIGNED: Dispatched order to {w_name}",
                                            )
                                        )

                                    note = (
                                        f"STATUS CHANGED (Admin Approved): {old_status} -> {item.status} (Released deliverables to Client)"
                                        if approve_release
                                        else f"STATUS CHANGED (Admin): {old_status} -> {item.status}"
                                    )
                                    db.add(
                                        AssignmentLog(
                                            tenant_id=asg_tid,
                                            assignment_id=item.id,
                                            actor_name=full_name,
                                            action_note=note,
                                        )
                                    )
                                    db.commit()
                                    ui.notify(
                                        "Deliverables approved & released to client for acceptance!" if approve_release else "Order workflow updated!",
                                        type="positive",
                                    )
                                    render_orders()
                                    refresh_workspace()
                                finally:
                                    db.close()

                            ui.button("Save Changes", on_click=lambda: save_workflow(False)).classes("bg-slate-900 text-white text-xs")
                            if asg.status not in ("Awaiting Client Acceptance", "Completed"):
                                ui.button("Approve & Release to Client", icon="verified", on_click=lambda: save_workflow(True)).classes("bg-emerald-600 text-white text-xs font-semibold")

                        if writer_deliverables and asg.status not in ("Awaiting Client Acceptance", "Completed"):
                            with ui.row().classes("w-full items-center justify-between bg-amber-50 border border-amber-300 p-3 rounded-xl"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("rate_review", size="22px").classes("text-amber-700")
                                    ui.label(
                                        f"{len(writer_deliverables)} writer upload(s) ready for quality review. Inspect files below, then Approve or Request Writer Revision."
                                    ).classes("text-xs font-semibold text-amber-950")
                                with ui.row().classes("items-center gap-2"):
                                    def send_back_to_writer() -> None:
                                        db = SessionLocal()
                                        try:
                                            item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                            if item:
                                                old_s = item.status
                                                item.status = "Revision Requested" if "Revision Requested" in ASSIGNMENT_STATUSES else "In Progress"
                                                db.add(
                                                    AssignmentLog(
                                                        tenant_id=asg_tid,
                                                        assignment_id=item.id,
                                                        actor_name=full_name,
                                                        action_note=f"STATUS CHANGED (Admin): {old_s} -> {item.status} (Requested writer revisions)",
                                                    )
                                                )
                                                db.commit()
                                                ui.notify("Sent back to writer for revision.", type="warning")
                                                render_orders()
                                                refresh_workspace()
                                        finally:
                                            db.close()

                                    ui.button("Request Writer Revision", icon="undo", on_click=send_back_to_writer).props("outline dense color=amber-9").classes("text-xs")
                                    ui.button("Approve Writer Uploads", icon="verified", on_click=lambda: save_workflow(True)).classes("bg-emerald-600 text-white text-xs font-semibold")

                    else:
                        # CLIENT PORTAL WORKSPACE BAR: Accept Work + Request Revision + Payment Reminder
                        if not is_paid_in_full:
                            with ui.row().classes("w-full items-center justify-between bg-rose-50 border border-rose-300 p-3.5 rounded-xl"):
                                with ui.row().classes("items-center gap-2.5"):
                                    ui.icon("payments", size="22px").classes("text-rose-600")
                                    with ui.column().classes("gap-0"):
                                        ui.label(f"Payment Reminder: Balance Due KES {balance_due:,.0f}").classes("text-xs font-bold text-rose-950")
                                        ui.label("Order closes as Completed only after you accept the work AND pay the full balance.").classes("text-[11px] text-rose-800")
                                ui.button(
                                    f"Pay KES {balance_due:,.0f} via M-Pesa",
                                    icon="smartphone",
                                    on_click=lambda: open_stk_modal(
                                        {
                                            "id": asg.id,
                                            "ref": asg.ref_number or f"AF-{asg.id}",
                                            "title": asg.title,
                                            "balance": balance_due,
                                            "client_phone": client_phone,
                                        }
                                    ),
                                ).classes("bg-emerald-600 text-white text-xs font-semibold")

                        with ui.row().classes("w-full items-center justify-between bg-slate-50 p-3 rounded-xl border border-slate-200 flex-wrap gap-2"):
                            with ui.column().classes("gap-0.5"):
                                ui.label(f"Current Status: {asg.status} • Balance Due: KES {balance_due:,.0f}").classes("text-xs font-semibold text-slate-800")
                                if client_accepted and not is_paid_in_full:
                                    ui.label("Work accepted! Complete your remaining M-Pesa payment above to close this order.").classes("text-xs text-amber-700 font-medium")
                                elif is_paid_in_full and not client_accepted and is_released_to_client:
                                    ui.label("Paid in full! Click 'Accept Work' once satisfied with the deliverables to close this order.").classes("text-xs text-emerald-700 font-medium")
                                elif asg.status == "Completed":
                                    ui.label("Order accepted and paid in full. Closed & Completed.").classes("text-xs text-emerald-700 font-semibold")

                            with ui.row().classes("items-center gap-2"):
                                if is_released_to_client and not client_accepted:
                                    def accept_work_done() -> None:
                                        db = SessionLocal()
                                        try:
                                            item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                            if not item:
                                                return
                                            old_s = item.status
                                            bal = max(float(item.quoted_cost_kes or 0) - float(item.amount_paid_kes or 0), 0.0)
                                            if bal <= 0:
                                                item.status = "Completed"
                                                if not item.completed_at:
                                                    item.completed_at = datetime.utcnow()
                                                action_msg = f"CLIENT ACCEPTED DELIVERABLES & PAID IN FULL: Status {old_s} -> Completed (Order Closed)"
                                                toast_msg = "Work accepted and order closed as Completed!"
                                                toast_type = "positive"
                                            else:
                                                item.status = "Awaiting Client Acceptance"
                                                action_msg = f"CLIENT ACCEPTED DELIVERABLES: Awaiting remaining balance of KES {bal:,.0f} before order closes"
                                                toast_msg = f"Work accepted! Please complete payment of KES {bal:,.0f} to close the order."
                                                toast_type = "warning"

                                            db.add(
                                                AssignmentLog(
                                                    tenant_id=asg_tid,
                                                    assignment_id=item.id,
                                                    actor_name=full_name,
                                                    action_note=action_msg,
                                                )
                                            )
                                            db.commit()
                                            ui.notify(toast_msg, type=toast_type)
                                            render_orders()
                                            refresh_workspace()
                                        finally:
                                            db.close()

                                    ui.button("Accept Work", icon="check_circle", on_click=accept_work_done).classes("bg-emerald-600 text-white text-xs font-semibold")

                                if asg.status in ("Under Review", "Awaiting Client Acceptance", "Completed"):
                                    def request_revision() -> None:
                                        db = SessionLocal()
                                        try:
                                            item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                            if item:
                                                old_s = item.status
                                                item.status = "Revision Requested" if "Revision Requested" in ASSIGNMENT_STATUSES else "In Progress"
                                                db.add(
                                                    AssignmentLog(
                                                        tenant_id=asg_tid,
                                                        assignment_id=item.id,
                                                        actor_name=full_name,
                                                        action_note=f"CLIENT REQUESTED A REVISION: Status {old_s} -> {item.status}",
                                                    )
                                                )
                                                db.commit()
                                                ui.notify("Revision requested! Agency & writer notified.", type="warning")
                                                render_orders()
                                                refresh_workspace()
                                        finally:
                                            db.close()

                                    ui.button("Request Revision", icon="edit_note", on_click=request_revision).props("outline dense color=amber-9").classes("text-xs")

                    with ui.tabs().classes("w-full") as ws_tabs:
                        t_docs = ui.tab(f"Files & Deliverables ({len(visible_docs)})", icon="attach_file")
                        t_chat = ui.tab(f"Messages ({len(visible_msgs)})", icon="chat")
                        t_logs = ui.tab(f"Interaction Timeline ({len(logs)})", icon="history")
                        if is_client or review:
                            t_rev = ui.tab("Agency Review", icon="star")

                    with ui.tab_panels(ws_tabs, value=t_docs).classes("w-full"):
                        with ui.tab_panel(t_docs).classes("p-2 gap-3"):
                            if is_client and hidden_pending_docs > 0:
                                with ui.row().classes("w-full items-center gap-2 bg-amber-50 border border-amber-200 text-amber-900 p-3 rounded-lg"):
                                    ui.icon("hourglass_top", size="18px").classes("text-amber-600")
                                    ui.label(
                                        f"{hidden_pending_docs} deliverable file(s) are currently undergoing agency quality review and will unlock once approved."
                                    ).classes("text-xs font-medium")

                            doc_cat = ui.select(
                                ["Client Brief", "Revision Instructions"] if is_client else ["Draft Submission", "Final Deliverable", "Turnitin Report", "Client Brief", "Revision Instructions"],
                                value="Client Brief" if is_client else ("Final Deliverable" if is_writer else "Draft Submission"),
                                label="Document Category",
                            ).props("outlined dense").classes("w-64")

                            async def handle_upload(e: events.UploadEventArguments) -> None:
                                raw_bytes = await e.file.read()
                                file_hash = hashlib.sha256(raw_bytes).hexdigest()
                                safe_name = f"{asg.id}_{uuid.uuid4().hex[:8]}_{e.file.name}"
                                save_path = STORAGE_DIR / safe_name
                                save_path.write_bytes(raw_bytes)

                                db = SessionLocal()
                                try:
                                    db.add(
                                        AssignmentDocument(
                                            tenant_id=asg_tid,
                                            assignment_id=asg.id,
                                            uploaded_by_name=full_name,
                                            doc_category=doc_cat.value,
                                            original_filename=e.file.name,
                                            file_hash=file_hash,
                                            file_path=str(save_path),
                                        )
                                    )
                                    status_note = ""
                                    if is_writer and doc_cat.value in ("Draft Submission", "Final Deliverable", "Turnitin Report"):
                                        item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                        if item and item.status != "Completed":
                                            old_s = item.status
                                            item.status = "Under Review"
                                            status_note = f" | STATUS CHANGED: {old_s} -> Under Review"

                                    db.add(
                                        AssignmentLog(
                                            tenant_id=asg_tid,
                                            assignment_id=asg.id,
                                            actor_name=full_name,
                                            action_note=f"FILE UPLOADED ({role}): [{doc_cat.value}] {e.file.name}{status_note}",
                                        )
                                    )
                                    db.commit()
                                    ui.notify(
                                        f"Uploaded {e.file.name} — queued for Tenant Admin approval!" if is_writer else f"Uploaded {e.file.name}",
                                        type="positive",
                                    )
                                    render_orders()
                                    refresh_workspace()
                                finally:
                                    db.close()

                            ui.upload(label="Upload File (PDF, DOCX, ZIP)", auto_upload=True, on_upload=handle_upload).classes("w-full")

                            for d in visible_docs:
                                is_writer_upload = d.doc_category in ("Draft Submission", "Final Deliverable", "Turnitin Report")
                                with ui.row().classes("w-full items-center justify-between p-3 bg-slate-50 rounded-lg border border-slate-200"):
                                    with ui.column().classes("gap-0.5"):
                                        with ui.row().classes("items-center gap-2"):
                                            ui.label(f"[{d.doc_category}] {d.original_filename}").classes("text-sm font-semibold text-slate-900")
                                            if is_writer_upload:
                                                ui.badge(
                                                    "APPROVED FOR CLIENT" if is_released_to_client else "PENDING ADMIN APPROVAL",
                                                    color="emerald" if is_released_to_client else "amber",
                                                ).classes("text-[10px]")
                                        ui.label(f"Uploaded by {d.uploaded_by_name} • SHA256: {(d.file_hash or '')[:12]}...").classes("text-xs text-slate-400")

                                    with ui.row().classes("items-center gap-2"):
                                        if d.file_path and Path(d.file_path).exists():
                                            ui.button("Download & Review", icon="download", on_click=lambda p=d.file_path, n=d.original_filename: ui.download(p, n)).classes("bg-slate-900 text-white text-xs")
                                        if (is_tenant_admin or is_superadmin) and is_writer_upload and not is_released_to_client:
                                            def approve_single_doc(doc_name: str = d.original_filename) -> None:
                                                db = SessionLocal()
                                                try:
                                                    item = db.query(Assignment).filter(Assignment.id == asg.id).first()
                                                    if item:
                                                        old_s = item.status
                                                        bal = max(float(item.quoted_cost_kes or 0) - float(item.amount_paid_kes or 0), 0.0)
                                                        item_logs = db.query(AssignmentLog).filter(AssignmentLog.assignment_id == item.id).all()
                                                        if has_client_accepted_work(item_logs) and bal <= 0:
                                                            item.status = "Completed"
                                                            if not item.completed_at:
                                                                item.completed_at = datetime.utcnow()
                                                        else:
                                                            item.status = "Awaiting Client Acceptance"
                                                        db.add(
                                                            AssignmentLog(
                                                                tenant_id=asg_tid,
                                                                assignment_id=item.id,
                                                                actor_name=full_name,
                                                                action_note=f"STATUS CHANGED (Admin Approved '{doc_name}'): {old_s} -> {item.status}",
                                                            )
                                                        )
                                                        db.commit()
                                                        ui.notify(f"Approved '{doc_name}' & released to client!", type="positive")
                                                        render_orders()
                                                        refresh_workspace()
                                                finally:
                                                    db.close()

                                            ui.button("Approve & Release", icon="verified", on_click=approve_single_doc).classes("bg-emerald-600 text-white text-xs font-semibold")

                        with ui.tab_panel(t_chat).classes("p-2 gap-3"):
                            with ui.column().classes("w-full max-h-64 overflow-y-auto gap-2 p-2 bg-slate-50 rounded-xl border border-slate-200"):
                                if not visible_msgs:
                                    ui.label("No messages in this channel yet.").classes("text-xs text-slate-400 p-2")
                                for m in visible_msgs:
                                    r_low = (m.sender_role or "").lower()
                                    card_border = "border-l-4 border-emerald-500" if r_low == "client" else ("border-l-4 border-purple-500" if r_low == "writer" else "border-l-4 border-blue-600")
                                    with ui.card().classes(f"w-full p-2.5 shadow-none border border-slate-200 bg-white {card_border}"):
                                        with ui.row().classes("w-full justify-between items-center"):
                                            ui.label(f"{m.sender_name} ({m.sender_role})").classes("text-xs font-bold text-slate-700")
                                            ui.badge(m.channel or "Client-Agency", color="blue" if (m.channel or "Client-Agency") == "Client-Agency" else "purple").classes("text-[10px]")
                                        ui.label(m.message_text).classes("text-sm text-slate-800")

                            with ui.row().classes("w-full items-center gap-2"):
                                if is_tenant_admin or is_superadmin:
                                    chan_sel = ui.select(["Client-Agency", "Team"], value="Client-Agency", label="Channel").props("outlined dense").classes("w-40")
                                else:
                                    chan_sel = None
                                msg_in = ui.input(placeholder="Type message...").props("outlined dense").classes("flex-1")

                                def send_msg() -> None:
                                    if not (msg_in.value or "").strip():
                                        return
                                    chosen_channel = "Client-Agency" if is_client else ("Team" if is_writer else (chan_sel.value if chan_sel else "Client-Agency"))
                                    db = SessionLocal()
                                    try:
                                        db.add(
                                            AssignmentMessage(
                                                tenant_id=asg_tid,
                                                assignment_id=asg.id,
                                                sender_name=full_name,
                                                sender_role=role,
                                                channel=chosen_channel,
                                                message_text=msg_in.value.strip(),
                                            )
                                        )
                                        db.commit()
                                        refresh_workspace()
                                    finally:
                                        db.close()

                                msg_in.on("keydown.enter", send_msg)
                                ui.button("Send", icon="send", on_click=send_msg).classes("bg-slate-900 text-white")

                        # Highlighted Interaction Timeline (Tenant <-> Writer <-> Client)
                        with ui.tab_panel(t_logs).classes("p-2 gap-2"):
                            if not logs:
                                ui.label("No interactions recorded yet.").classes("text-xs text-slate-400")
                            for idx, lg in enumerate(logs):
                                ts = lg.timestamp.strftime("%Y-%m-%d %H:%M") if lg.timestamp else ""
                                note_txt = lg.action_note or ""
                                note_low = note_txt.lower()

                                if "accepted" in note_low or "completed" in note_low or "paid" in note_low:
                                    card_style = "bg-emerald-50 border-l-4 border-emerald-600 border-emerald-200"
                                    tag_label = "ACCEPTED / CLOSED"
                                    tag_color = "positive"
                                elif "revision" in note_low or "reminder" in note_low:
                                    card_style = "bg-amber-50 border-l-4 border-amber-500 border-amber-200"
                                    tag_label = "ACTION REQUIRED"
                                    tag_color = "warning"
                                elif "status changed" in note_low or "approved" in note_low or "assigned" in note_low:
                                    card_style = "bg-blue-50 border-l-4 border-blue-600 border-blue-200"
                                    tag_label = "STATUS UPDATE"
                                    tag_color = "primary"
                                else:
                                    card_style = "bg-slate-50 border-l-4 border-slate-400 border-slate-200"
                                    tag_label = "ACTIVITY"
                                    tag_color = "blue-grey"

                                with ui.card().classes(f"w-full p-3 shadow-none border {card_style}"):
                                    with ui.row().classes("w-full items-center justify-between gap-2"):
                                        with ui.row().classes("items-center gap-2"):
                                            ui.badge(tag_label, color=tag_color).classes("text-[10px] font-bold")
                                            if idx == 0:
                                                ui.badge("LATEST", color="dark").classes("text-[10px]")
                                            ui.label(lg.actor_name or "System").classes("text-xs font-bold text-slate-900")
                                        ui.label(ts).classes("text-[11px] text-slate-500")
                                    ui.label(note_txt).classes("text-xs font-medium text-slate-800 mt-1")

                        if is_client or review:
                            with ui.tab_panel(t_rev).classes("p-2 gap-3"):
                                if review:
                                    ui.label(f"Agency Rating: {'⭐' * int(review.rating or 5)} ({review.rating}/5)").classes("font-bold text-base")
                                    ui.label(review.feedback or "").classes("text-sm text-slate-700")
                                elif is_client:
                                    rating_sel = ui.select([5, 4, 3, 2, 1], value=5, label="Rate This Agency (1-5 Stars)").props("outlined dense").classes("w-52")
                                    fb_in = ui.textarea("Your Feedback (Helps other clients choose top agencies)").props("outlined dense").classes("w-full")

                                    def submit_review() -> None:
                                        db = SessionLocal()
                                        try:
                                            db.add(
                                                ClientReview(
                                                    tenant_id=asg_tid,
                                                    assignment_id=asg.id,
                                                    client_name=full_name,
                                                    rating=int(rating_sel.value or 5),
                                                    feedback=(fb_in.value or "").strip(),
                                                )
                                            )
                                            db.commit()
                                            ui.notify("Thank you for reviewing this agency!", type="positive")
                                            refresh_workspace()
                                        finally:
                                            db.close()

                                    ui.button("Submit Agency Review", on_click=submit_review).classes("bg-emerald-600 text-white")

            refresh_workspace()
            workspace_dialog.open()
        except Exception as exc:
            ui.notify(f"Unable to open workspace: {exc}", type="negative")

    with ui.header().classes("bg-slate-900 text-white px-6 py-3 flex items-center justify-between shadow-sm"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("school", size="26px").classes("text-emerald-400")
            ui.label(business_name).classes("text-lg font-bold tracking-tight")
            badge_text = "SUPER ADMIN INSPECT" if is_superadmin else ("CLIENT PORTAL" if is_client else ("WRITER PORTAL" if is_writer else tier))
            ui.badge(badge_text, color="amber" if is_superadmin else "emerald").classes("text-xs font-semibold px-2 py-0.5")

        with ui.row().classes("items-center gap-4"):
            if is_superadmin:
                ui.button("<- Platform Console", on_click=lambda: ui.navigate.to("/superadmin")).props("flat dense text-color=amber").classes("text-xs")
            if is_client:
                ui.button("Switch Agency", icon="swap_horiz", on_click=lambda: open_switch_agency_dialog()).props("outline dense text-color=white").classes("text-xs")
            ui.label(f"{full_name} ({role})").classes("text-xs text-slate-300 hidden sm:block")

            if not is_superadmin and (is_tenant_admin or is_client):
                btn_text = "+ Submit Assignment" if is_client else "+ Upload Client Assignment"
                ui.button(btn_text, icon="upload_file", on_click=lambda: open_new_assignment_dialog()).classes(
                    "bg-emerald-500 text-slate-950 font-semibold text-xs px-3 py-1.5 rounded-lg"
                )
            ui.button("Logout", icon="logout", on_click=clear_session_and_redirect).props("flat dense text-color=white").classes("text-xs")

    with ui.column().classes("w-full max-w-7xl mx-auto p-6 gap-5"):
        db = SessionLocal()
        try:
            announcements = get_active_announcements(db)[:2]
        finally:
            db.close()

        for ann in announcements:
            with ui.row().classes("w-full items-center gap-3 bg-amber-50 border border-amber-200 text-amber-900 px-4 py-2.5 rounded-xl"):
                ui.icon("campaign", size="20px").classes("text-amber-600")
                ui.label(f"{ann.title}:").classes("font-bold text-xs")
                ui.label(ann.message).classes("text-xs")

        with ui.tabs().classes("w-full bg-white rounded-xl border border-slate-200 px-2 shadow-sm") as main_tabs:
            tab_orders = ui.tab("My Assignments" if is_client else "Assignments", icon="assignment")
            if not is_writer:
                tab_payments = ui.tab("M-Pesa Ledger", icon="payments")
            if is_tenant_admin or is_writer or is_superadmin:
                tab_payouts = ui.tab("Writer Earnings & Payouts", icon="account_balance")
            if is_tenant_admin or is_superadmin:
                tab_team = ui.tab("Team & Clients", icon="groups")
                tab_services = ui.tab("Services Catalog", icon="category")
                tab_settings = ui.tab("Subscription, M-Pesa & Support", icon="workspace_premium")

        with ui.tab_panels(main_tabs, value=tab_orders).classes("w-full bg-transparent p-0"):
            # ===============================================================
            # TAB 1: ASSIGNMENTS + HIGHLIGHTED STATUS & LATEST INTERACTION
            # ===============================================================
            with ui.tab_panel(tab_orders).classes("p-0 gap-6"):
                orders_container = ui.column().classes("w-full gap-6")

                def quick_approve_assignment(asg_id: int) -> None:
                    db = SessionLocal()
                    try:
                        item = db.query(Assignment).filter(Assignment.id == int(asg_id)).first()
                        if not item:
                            return
                        old_s = item.status
                        bal = max(float(item.quoted_cost_kes or 0) - float(item.amount_paid_kes or 0), 0.0)
                        item_logs = db.query(AssignmentLog).filter(AssignmentLog.assignment_id == item.id).all()
                        if has_client_accepted_work(item_logs) and bal <= 0:
                            item.status = "Completed"
                            if not item.completed_at:
                                item.completed_at = datetime.utcnow()
                        else:
                            item.status = "Awaiting Client Acceptance"

                        db.add(
                            AssignmentLog(
                                tenant_id=item.tenant_id,
                                assignment_id=item.id,
                                actor_name=full_name,
                                action_note=f"STATUS CHANGED (Admin Approved): {old_s} -> {item.status} (Released deliverables to Client)",
                            )
                        )
                        client_u = db.query(User).filter(User.id == item.client_id).first()
                        if client_u:
                            log_email_event(
                                db,
                                item.tenant_id,
                                client_u.email,
                                f"Order {item.ref_number} Deliverables Ready for Review",
                                f"Your deliverables for '{item.title}' are ready in {business_name}. Please review, accept work, and complete payment (Balance: KES {bal:,.0f}).",
                            )
                        db.commit()
                        ui.notify(f"Order {item.ref_number} released to client for acceptance!", type="positive")
                        render_orders()
                    finally:
                        db.close()

                def render_orders() -> None:
                    orders_container.clear()
                    active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                    db = SessionLocal()
                    try:
                        query = db.query(Assignment).filter(Assignment.tenant_id == active_tid)
                        if is_writer:
                            query = query.filter(Assignment.staff_id == user_id)
                        elif is_client:
                            query = query.filter(Assignment.client_id == user_id)

                        assignments = query.order_by(Assignment.id.desc()).all()
                        users = db.query(User).filter(User.tenant_id == active_tid).all()
                        user_map = {u.id: u.full_name for u in users}
                        phone_map = get_user_phone_map(db, active_tid)
                        docs_all = db.query(AssignmentDocument).filter(AssignmentDocument.tenant_id == active_tid).all()
                        all_logs = db.query(AssignmentLog).filter(AssignmentLog.tenant_id == active_tid).order_by(AssignmentLog.id.desc()).all()

                        latest_log_by_asg: dict[int, str] = {}
                        for lg in all_logs:
                            if lg.assignment_id not in latest_log_by_asg:
                                latest_log_by_asg[lg.assignment_id] = f"{lg.actor_name}: {lg.action_note}"

                        writer_doc_counts: dict[int, int] = {}
                        for d in docs_all:
                            if d.doc_category in ("Draft Submission", "Final Deliverable", "Turnitin Report"):
                                writer_doc_counts[d.assignment_id] = writer_doc_counts.get(d.assignment_id, 0) + 1

                        tx_query = db.query(ClientPaymentTransaction).filter(
                            ClientPaymentTransaction.tenant_id == active_tid,
                            ClientPaymentTransaction.status == "Pending Verification",
                        )
                        if is_client:
                            client_asg_ids = [a.id for a in assignments]
                            tx_query = tx_query.filter(ClientPaymentTransaction.assignment_id.in_(client_asg_ids)) if client_asg_ids else tx_query.filter(False)
                        pending_tx_count = tx_query.count()
                    finally:
                        db.close()

                    total_billed = sum(float(a.quoted_cost_kes or 0) for a in assignments)
                    total_collected = sum(float(a.amount_paid_kes or 0) for a in assignments)
                    outstanding = max(total_billed - total_collected, 0.0)
                    active_orders = sum(1 for a in assignments if a.status not in ("Completed", "Cancelled"))
                    review_pending_count = sum(1 for a in assignments if a.status == "Under Review")
                    writer_earned = sum(
                        float(a.quoted_cost_kes or 0) * (float(a.staff_commission_pct or 50) / 100.0)
                        for a in assignments
                        if a.status == "Completed"
                    )

                    with orders_container:
                        with ui.grid(columns=4).classes("w-full gap-4"):
                            kpi_card("My Active Orders" if (is_client or is_writer) else "Active Orders", str(active_orders), "Open until accepted & paid", "assignment", "text-blue-600")
                            if is_writer:
                                kpi_card("Closed & Completed", str(sum(1 for a in assignments if a.status == "Completed")), "Accepted by client & paid", "check_circle", "text-emerald-600")
                                kpi_card("Total Commission (KES)", f"{writer_earned:,.0f}", "Earned on closed orders", "payments", "text-amber-600")
                                kpi_card("Unpaid Payouts", str(sum(1 for a in assignments if a.status == "Completed" and (a.staff_payout_status or "Unpaid") != "Paid")), "Awaiting M-Pesa payout", "account_balance_wallet", "text-purple-600")
                            elif is_tenant_admin or is_superadmin:
                                kpi_card("Awaiting Admin Approval", str(review_pending_count), "Writer uploads under review", "rate_review", "text-amber-600")
                                kpi_card("Collected (KES)", f"{total_collected:,.0f}", "Verified M-Pesa payments", "payments", "text-emerald-600")
                                kpi_card("Outstanding Balance", f"{outstanding:,.0f}", f"{pending_tx_count} pending STK push(es)", "account_balance_wallet", "text-purple-600")
                            else:
                                kpi_card("Total Paid (KES)", f"{total_collected:,.0f}", "Verified M-Pesa payments", "payments", "text-emerald-600")
                                kpi_card("Balance Due (KES)", f"{outstanding:,.0f}", "Pay in full to close order", "account_balance_wallet", "text-amber-600")
                                kpi_card("Pending STK Pushes", str(pending_tx_count), "Awaiting Daraja callback", "sync", "text-purple-600")

                        with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                            with ui.row().classes("w-full items-center justify-between mb-4"):
                                with ui.column().classes("gap-0"):
                                    ui.label("My Submitted Assignments" if is_client else ("Assigned Tasks" if is_writer else "Client Assignments")).classes("text-lg font-bold text-slate-900")
                                    ui.label("Open 'Workspace' to review deliverables, accept work, track highlighted status updates, or pay via M-Pesa.").classes("text-xs text-slate-500")
                                search_box = ui.input(placeholder="Search ref #, title, or status...").props("outlined dense clearable").classes("w-64")

                            rows = [
                                {
                                    "id": a.id,
                                    "ref": a.ref_number or f"AF-{a.id}",
                                    "title": a.title,
                                    "client": user_map.get(a.client_id, "Walk-in Client"),
                                    "client_phone": phone_map.get(a.client_id, ""),
                                    "writer": user_map.get(a.staff_id, "Unassigned"),
                                    "status": a.status,
                                    "status_color": get_status_badge_color(a.status),
                                    "latest_activity": latest_log_by_asg.get(a.id, "Order created"),
                                    "uploads": writer_doc_counts.get(a.id, 0),
                                    "deadline": a.deadline.strftime("%Y-%m-%d %H:%M") if a.deadline else "—",
                                    "quoted": f"{float(a.quoted_cost_kes or 0):,.0f}",
                                    "paid": f"{float(a.amount_paid_kes or 0):,.0f}",
                                    "balance": max(float(a.quoted_cost_kes or 0) - float(a.amount_paid_kes or 0), 0.0),
                                    "show_pay": (not is_writer) and (not is_superadmin),
                                    "can_quick_approve": (is_tenant_admin or is_superadmin) and a.status == "Under Review",
                                }
                                for a in assignments
                            ]

                            columns = [
                                {"name": "ref", "label": "Ref #", "field": "ref", "align": "left", "sortable": True},
                                {"name": "title", "label": "Title", "field": "title", "align": "left", "sortable": True},
                                {"name": "client", "label": "Client", "field": "client", "align": "left", "sortable": True},
                                {"name": "writer", "label": "Writer", "field": "writer", "align": "left", "sortable": True},
                                {"name": "status", "label": "Status", "field": "status", "align": "left", "sortable": True},
                                {"name": "latest_activity", "label": "Latest Update", "field": "latest_activity", "align": "left"},
                                {"name": "quoted", "label": "Quoted (KES)", "field": "quoted", "align": "right", "sortable": True},
                                {"name": "paid", "label": "Paid (KES)", "field": "paid", "align": "right", "sortable": True},
                                {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
                            ]

                            table = ui.table(columns=columns, rows=rows, row_key="id", pagination=10).classes("w-full flat")
                            search_box.bind_value(table, "filter")

                            table.add_slot(
                                "body-cell-status",
                                r"""
                                <q-td :props="props">
                                    <q-badge :color="props.row.status_color" class="text-weight-bold q-px-sm q-py-xs">
                                        {{ props.row.status }}
                                    </q-badge>
                                </q-td>
                                """,
                            )

                            table.add_slot(
                                "body-cell-latest_activity",
                                r"""
                                <q-td :props="props" style="max-width: 240px; white-space: normal;">
                                    <div class="text-caption text-weight-medium text-slate-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                                        {{ props.row.latest_activity }}
                                    </div>
                                </q-td>
                                """,
                            )

                            pay_label = "Pay M-Pesa" if is_client else "STK"
                            table.add_slot(
                                "body-cell-actions",
                                f"""
                                <q-td :props="props" class="q-gutter-xs no-wrap">
                                    <q-btn size="sm" unelevated color="primary" text-color="white" icon="folder_open" label="Workspace"
                                           @click.stop="$parent.$emit('open_ws', props.row)" />
                                    <q-btn v-if="props.row.can_quick_approve" size="sm" unelevated color="green-7" text-color="white"
                                           icon="verified" label="Approve"
                                           @click.stop="$parent.$emit('quick_approve', props.row)" />
                                    <q-btn v-if="props.row.show_pay" :disable="props.row.balance <= 0" size="sm" unelevated
                                           color="positive" text-color="white" icon="smartphone" label="{pay_label}"
                                           @click.stop="$parent.$emit('open_stk', props.row)" />
                                </q-td>
                                """,
                            )
                            table.on("open_ws", lambda e: open_assignment_workspace(int(e.args["id"])))
                            table.on("quick_approve", lambda e: quick_approve_assignment(int(e.args["id"])))
                            table.on("open_stk", lambda e: open_stk_modal(e.args))

                render_orders()

            # ===============================================================
            # TAB 2: M-PESA TRANSACTIONS LEDGER
            # ===============================================================
            if not is_writer:
                with ui.tab_panel(tab_payments).classes("p-0"):
                    payments_container = ui.column().classes("w-full gap-4")

                    def render_payments() -> None:
                        payments_container.clear()
                        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                        db = SessionLocal()
                        try:
                            asg_query = db.query(Assignment).filter(Assignment.tenant_id == active_tid)
                            if is_client:
                                asg_query = asg_query.filter(Assignment.client_id == user_id)
                            asg_list = asg_query.all()
                            asg_map = {a.id: a.ref_number for a in asg_list}

                            tx_query = db.query(ClientPaymentTransaction).filter(ClientPaymentTransaction.tenant_id == active_tid)
                            if is_client:
                                tx_query = tx_query.filter(ClientPaymentTransaction.assignment_id.in_(list(asg_map.keys()))) if asg_map else tx_query.filter(False)
                            txs = tx_query.order_by(ClientPaymentTransaction.id.desc()).all()
                        finally:
                            db.close()

                        with payments_container:
                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                with ui.row().classes("w-full items-center justify-between mb-4"):
                                    ui.label("My M-Pesa Payments" if is_client else "Client M-Pesa Transactions").classes("text-lg font-bold text-slate-900")
                                    ui.button("Refresh Ledger", icon="refresh", on_click=render_payments).props("outline dense")

                                rows = [
                                    {
                                        "id": t.id,
                                        "ref": asg_map.get(t.assignment_id, f"#{t.assignment_id}"),
                                        "checkout_id": t.checkout_request_id or "—",
                                        "mpesa_code": t.mpesa_code or "Pending",
                                        "phone": t.phone_number or "—",
                                        "amount": f"{float(t.amount_kes or 0):,.0f}",
                                        "status": t.status,
                                        "recorded_at": t.recorded_at.strftime("%Y-%m-%d %H:%M") if t.recorded_at else "—",
                                    }
                                    for t in txs
                                ]
                                cols = [
                                    {"name": "ref", "label": "Order Ref", "field": "ref", "align": "left"},
                                    {"name": "mpesa_code", "label": "M-Pesa Receipt", "field": "mpesa_code", "align": "left"},
                                    {"name": "phone", "label": "Phone", "field": "phone", "align": "left"},
                                    {"name": "amount", "label": "Amount (KES)", "field": "amount", "align": "right"},
                                    {"name": "status", "label": "Status", "field": "status", "align": "left"},
                                    {"name": "checkout_id", "label": "CheckoutRequestID", "field": "checkout_id", "align": "left"},
                                    {"name": "recorded_at", "label": "Timestamp", "field": "recorded_at", "align": "left"},
                                ]
                                ui.table(columns=cols, rows=rows, row_key="id", pagination=12).classes("w-full flat")

                    render_payments()

            # ===============================================================
            # TAB 3: WRITER EARNINGS & PAYOUTS
            # ===============================================================
            if is_tenant_admin or is_writer or is_superadmin:
                with ui.tab_panel(tab_payouts).classes("p-0"):
                    payouts_container = ui.column().classes("w-full gap-4")

                    def render_payouts() -> None:
                        payouts_container.clear()
                        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                        db = SessionLocal()
                        try:
                            q = db.query(Assignment).filter(Assignment.tenant_id == active_tid, Assignment.staff_id.isnot(None))
                            if is_writer:
                                q = q.filter(Assignment.staff_id == user_id)
                            items = q.order_by(Assignment.id.desc()).all()
                            u_map = {u.id: u.full_name for u in db.query(User).filter(User.tenant_id == active_tid).all()}
                        finally:
                            db.close()

                        with payouts_container:
                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                ui.label("Writer Commission & M-Pesa Payout Ledger").classes("text-lg font-bold text-slate-900 mb-3")
                                rows = []
                                for a in items:
                                    comm_pct = float(a.staff_commission_pct or 50.0)
                                    payout_kes = float(a.quoted_cost_kes or 0) * (comm_pct / 100.0)
                                    rows.append(
                                        {
                                            "id": a.id,
                                            "ref": a.ref_number,
                                            "title": a.title,
                                            "writer": u_map.get(a.staff_id, "Writer"),
                                            "order_status": a.status,
                                            "comm_pct": f"{comm_pct:.0f}%",
                                            "payout_kes": f"{payout_kes:,.0f}",
                                            "payout_status": a.staff_payout_status or "Unpaid",
                                            "mpesa_ref": a.staff_payout_mpesa or "—",
                                            "can_pay": is_tenant_admin and a.status == "Completed" and (a.staff_payout_status or "Unpaid") != "Paid",
                                        }
                                    )
                                cols = [
                                    {"name": "ref", "label": "Ref #", "field": "ref", "align": "left"},
                                    {"name": "writer", "label": "Writer", "field": "writer", "align": "left"},
                                    {"name": "order_status", "label": "Order Status", "field": "order_status", "align": "left"},
                                    {"name": "comm_pct", "label": "Rate", "field": "comm_pct", "align": "right"},
                                    {"name": "payout_kes", "label": "Commission (KES)", "field": "payout_kes", "align": "right"},
                                    {"name": "payout_status", "label": "Payout Status", "field": "payout_status", "align": "left"},
                                    {"name": "mpesa_ref", "label": "M-Pesa Ref", "field": "mpesa_ref", "align": "left"},
                                    {"name": "actions", "label": "Settle", "field": "actions", "align": "center"},
                                ]
                                p_table = ui.table(columns=cols, rows=rows, row_key="id", pagination=10).classes("w-full flat")

                                def open_payout_modal(r: dict) -> None:
                                    with ui.dialog() as d, ui.card().classes("w-full max-w-md p-6 rounded-2xl"):
                                        ui.label(f"Record Writer Payout — {r['ref']}").classes("text-lg font-bold mb-1")
                                        ui.label(f"{r['writer']} • KES {r['payout_kes']}").classes("text-sm text-slate-500 mb-3")
                                        code_in = ui.input("M-Pesa Transaction Code (e.g. QKH8...)").props("outlined dense").classes("w-full mb-4")

                                        def confirm_payout() -> None:
                                            if not (code_in.value or "").strip():
                                                ui.notify("Enter the M-Pesa confirmation code.", type="warning")
                                                return
                                            db = SessionLocal()
                                            try:
                                                item = db.query(Assignment).filter(Assignment.id == r["id"]).first()
                                                if item:
                                                    item.staff_payout_status = "Paid"
                                                    item.staff_payout_mpesa = code_in.value.strip().upper()
                                                    item.staff_paid_at = datetime.utcnow()
                                                    db.add(
                                                        AssignmentLog(
                                                            tenant_id=active_tid,
                                                            assignment_id=item.id,
                                                            actor_name=full_name,
                                                            action_note=f"WRITER PAYOUT SETTLED ({item.staff_payout_mpesa})",
                                                        )
                                                    )
                                                    db.commit()
                                                    ui.notify("Writer payout recorded!", type="positive")
                                                    d.close()
                                                    render_payouts()
                                            finally:
                                                db.close()

                                        with ui.row().classes("w-full justify-end gap-2"):
                                            ui.button("Cancel", on_click=d.close).props("flat")
                                            ui.button("Confirm Paid", on_click=confirm_payout).classes("bg-emerald-600 text-white")
                                    d.open()

                                p_table.add_slot(
                                    "body-cell-actions",
                                    r"""
                                    <q-td :props="props">
                                        <q-btn v-if="props.row.can_pay" size="sm" unelevated color="positive" text-color="white"
                                               icon="paid" label="Record Payout" @click.stop="$parent.$emit('pay_writer', props.row)" />
                                    </q-td>
                                    """,
                                )
                                p_table.on("pay_writer", lambda e: open_payout_modal(e.args))

                    render_payouts()

            # ===============================================================
            # TAB 4: TEAM & CLIENTS MANAGEMENT (Name, Email, Phone visible)
            # ===============================================================
            if is_tenant_admin or is_superadmin:
                with ui.tab_panel(tab_team).classes("p-0"):
                    team_container = ui.column().classes("w-full gap-4")

                    def render_team() -> None:
                        team_container.clear()
                        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                        db = SessionLocal()
                        try:
                            users = db.query(User).filter(User.tenant_id == active_tid).order_by(User.id.desc()).all()
                            phone_map = get_user_phone_map(db, active_tid)
                        finally:
                            db.close()

                        with team_container:
                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                ui.label("Add Writer, Client, or Staff Member").classes("text-base font-bold text-slate-900 mb-3")
                                with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                                    u_name = ui.input("Full Name").props("outlined dense").classes("w-44")
                                    u_email = ui.input("Email").props("outlined dense").classes("w-48")
                                    u_phone = ui.input("Phone Number", placeholder="0712345678").props("outlined dense").classes("w-40")
                                    u_role = ui.select(["Writer", "Client", "Admin"], value="Writer", label="Role").props("outlined dense").classes("w-32")
                                    u_comm = ui.number("Commission %", value=50, min=0, max=100).props("outlined dense").classes("w-32")
                                    u_pass = ui.input("Password", password=True).props("outlined dense").classes("w-40")

                                    def add_user() -> None:
                                        if not u_name.value or not u_email.value or not u_pass.value:
                                            ui.notify("Name, email, and password are required.", type="warning")
                                            return
                                        email_clean = u_email.value.strip().lower()
                                        db = SessionLocal()
                                        try:
                                            if db.query(User).filter(func.lower(func.trim(User.email)) == email_clean).first():
                                                ui.notify("Email already registered.", type="negative")
                                                return
                                            new_u = User(
                                                tenant_id=active_tid,
                                                full_name=u_name.value.strip(),
                                                email=email_clean,
                                                password_hash=hash_password(u_pass.value),
                                                role=u_role.value,
                                                commission_rate=float(u_comm.value or 0),
                                            )
                                            db.add(new_u)
                                            db.flush()
                                            if u_phone.value:
                                                set_user_phone(db, new_u.id, u_phone.value)
                                            db.commit()
                                            ui.notify(f"{u_role.value} account created!", type="positive")
                                            render_team()
                                        finally:
                                            db.close()

                                    ui.button("Add Member", icon="person_add", on_click=add_user).classes("bg-slate-900 text-white")

                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                ui.label("Workspace Directory (Clients, Writers & Staff)").classes("text-lg font-bold text-slate-900 mb-3")
                                rows = [
                                    {
                                        "id": u.id,
                                        "full_name": u.full_name,
                                        "email": u.email,
                                        "phone": phone_map.get(u.id) or "—",
                                        "role": u.role,
                                        "commission": f"{float(u.commission_rate or 0):.0f}%" if (u.role or "").lower() == "writer" else "—",
                                        "can_delete": u.id != user_id,
                                    }
                                    for u in users
                                ]
                                cols = [
                                    {"name": "full_name", "label": "Name", "field": "full_name", "align": "left", "sortable": True},
                                    {"name": "email", "label": "Email", "field": "email", "align": "left", "sortable": True},
                                    {"name": "phone", "label": "Phone Number", "field": "phone", "align": "left", "sortable": True},
                                    {"name": "role", "label": "Role", "field": "role", "align": "left", "sortable": True},
                                    {"name": "commission", "label": "Commission", "field": "commission", "align": "right"},
                                    {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
                                ]
                                dir_table = ui.table(columns=cols, rows=rows, row_key="id", pagination=10).classes("w-full flat")

                                def confirm_delete_member(target_id: int) -> None:
                                    db = SessionLocal()
                                    try:
                                        m = db.query(User).filter(User.tenant_id == active_tid, User.id == int(target_id)).first()
                                        if not m:
                                            return
                                        m_name, m_role = m.full_name, m.role
                                    finally:
                                        db.close()

                                    with ui.dialog() as d, ui.card().classes("p-6 rounded-2xl max-w-md"):
                                        ui.label(f"Remove {m_role}: {m_name}?").classes("text-lg font-bold text-slate-900 mb-2")
                                        ui.label("Any assignments assigned to this staff member will become unassigned.").classes("text-sm text-slate-600 mb-4")

                                        def do_delete() -> None:
                                            db = SessionLocal()
                                            try:
                                                if db.query(Assignment).filter(Assignment.tenant_id == active_tid, Assignment.client_id == int(target_id)).count() > 0:
                                                    ui.notify("Cannot remove a client who has existing assignments.", type="warning")
                                                    d.close()
                                                    return
                                                db.query(Assignment).filter(Assignment.tenant_id == active_tid, Assignment.staff_id == int(target_id)).update({"staff_id": None})
                                                member = db.query(User).filter(User.tenant_id == active_tid, User.id == int(target_id)).first()
                                                if member:
                                                    db.delete(member)
                                                    db.commit()
                                                    ui.notify(f"Removed {m_name} from workspace.", type="positive")
                                                d.close()
                                                render_team()
                                                render_orders()
                                            finally:
                                                db.close()

                                        with ui.row().classes("w-full justify-end gap-2"):
                                            ui.button("Cancel", on_click=d.close).props("flat")
                                            ui.button("Delete Member", icon="delete", on_click=do_delete).classes("bg-red-600 text-white")
                                    d.open()

                                dir_table.add_slot(
                                    "body-cell-actions",
                                    r"""
                                    <q-td :props="props">
                                        <q-btn v-if="props.row.can_delete" size="sm" unelevated color="negative" text-color="white"
                                               icon="delete" label="Remove" @click.stop="$parent.$emit('del_member', props.row)" />
                                    </q-td>
                                    """,
                                )
                                dir_table.on("del_member", lambda e: confirm_delete_member(e.args["id"]))

                    render_team()

                # ===============================================================
                # TAB 5: SERVICES CATALOG
                # ===============================================================
                with ui.tab_panel(tab_services).classes("p-0"):
                    services_container = ui.column().classes("w-full gap-4")

                    def render_services() -> None:
                        services_container.clear()
                        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                        db = SessionLocal()
                        try:
                            services = db.query(Service).filter(Service.tenant_id == active_tid).order_by(Service.id.desc()).all()
                        finally:
                            db.close()

                        with services_container:
                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                ui.label("Add Agency Service Package").classes("text-base font-bold text-slate-900 mb-3")
                                with ui.row().classes("w-full items-center gap-3"):
                                    s_title = ui.input("Service Title (e.g., Dissertation Chapter)").props("outlined dense").classes("w-64")
                                    s_desc = ui.input("Description").props("outlined dense").classes("flex-1")
                                    s_price = ui.number("Base Price (KES)", value=3000, min=100).props("outlined dense").classes("w-40")
                                    s_days = ui.number("Turnaround (Days)", value=3, min=1).props("outlined dense").classes("w-36")

                                    def add_service() -> None:
                                        if not s_title.value:
                                            ui.notify("Service title is required.", type="warning")
                                            return
                                        db = SessionLocal()
                                        try:
                                            db.add(
                                                Service(
                                                    tenant_id=active_tid,
                                                    title=s_title.value.strip(),
                                                    description=(s_desc.value or "").strip(),
                                                    base_price_kes=float(s_price.value or 0),
                                                    turnaround_days=int(s_days.value or 3),
                                                )
                                            )
                                            db.commit()
                                            ui.notify("Service added to catalog!", type="positive")
                                            render_services()
                                        finally:
                                            db.close()

                                    ui.button("Add Service", icon="add", on_click=add_service).classes("bg-slate-900 text-white")

                            with ui.card().classes("w-full p-5 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                ui.label("Service Packages").classes("text-lg font-bold text-slate-900 mb-3")
                                rows = [
                                    {
                                        "id": s.id,
                                        "title": s.title,
                                        "description": s.description or "—",
                                        "price": f"{float(s.base_price_kes or 0):,.0f}",
                                        "days": f"{s.turnaround_days} days",
                                    }
                                    for s in services
                                ]
                                cols = [
                                    {"name": "title", "label": "Service", "field": "title", "align": "left"},
                                    {"name": "description", "label": "Description", "field": "description", "align": "left"},
                                    {"name": "price", "label": "Base Price (KES)", "field": "price", "align": "right"},
                                    {"name": "days", "label": "Turnaround", "field": "days", "align": "right"},
                                ]
                                ui.table(columns=cols, rows=rows, row_key="id").classes("w-full flat")

                    render_services()

                # ===============================================================
                # TAB 6: SUBSCRIPTION UPGRADE (WITH STK PUSH), M-PESA & SUPPORT
                # ===============================================================
                with ui.tab_panel(tab_settings).classes("p-0"):
                    settings_box = ui.column().classes("w-full gap-6")

                    def render_settings() -> None:
                        settings_box.clear()
                        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
                        db = SessionLocal()
                        try:
                            tenant_obj = db.query(Tenant).filter(Tenant.id == active_tid).first()
                            plans = db.query(SubscriptionPlan).all()
                            sub_history = (
                                db.query(PlatformSubscriptionPayment)
                                .filter(PlatformSubscriptionPayment.tenant_id == active_tid)
                                .order_by(PlatformSubscriptionPayment.id.desc())
                                .all()
                            )
                            my_tickets = (
                                db.query(SupportTicket)
                                .filter(SupportTicket.tenant_id == active_tid)
                                .order_by(SupportTicket.id.desc())
                                .all()
                            )
                        finally:
                            db.close()

                        if not plans:
                            plans = [
                                SubscriptionPlan(name="Starter", monthly_price_kes=2500, annual_price_kes=25000, max_staff=5, max_active_projects=50, features_summary="Core M-Pesa STK & Client Portal"),
                                SubscriptionPlan(name="Pro", monthly_price_kes=6500, annual_price_kes=65000, max_staff=20, max_active_projects=250, features_summary="Multi-Writer Dispatch, Commission Ledger & Turnitin Tracking"),
                                SubscriptionPlan(name="Enterprise", monthly_price_kes=15000, annual_price_kes=150000, max_staff=100, max_active_projects=2000, features_summary="Unlimited Scale, Priority SLA & Dedicated Onboarding"),
                            ]

                        current_tier = (tenant_obj.tier if tenant_obj else tier) or "Starter"
                        current_status = (tenant_obj.status if tenant_obj else "Trial") or "Trial"
                        expiry_txt = (
                            tenant_obj.subscription_ends_at.strftime("%Y-%m-%d")
                            if (tenant_obj and tenant_obj.subscription_ends_at)
                            else (tenant_obj.trial_ends_at.strftime("%Y-%m-%d") if (tenant_obj and tenant_obj.trial_ends_at) else "—")
                        )

                        def open_plan_upgrade_stk_modal(plan_obj: SubscriptionPlan) -> None:
                            with ui.dialog() as sub_dlg, ui.card().classes("w-full max-w-md p-6 rounded-2xl"):
                                ui.label(f"Upgrade to {plan_obj.name} Plan").classes("text-lg font-bold text-slate-900 mb-1")
                                ui.label(plan_obj.features_summary or "").classes("text-xs text-slate-500 mb-4")

                                cycle_sel = ui.select(
                                    {
                                        "Monthly": f"Monthly — KES {float(plan_obj.monthly_price_kes or 0):,.0f} (30 days)",
                                        "Annual": f"Annual — KES {float(plan_obj.annual_price_kes or 0):,.0f} (365 days)",
                                    },
                                    value="Monthly",
                                    label="Billing Cycle",
                                ).props("outlined dense").classes("w-full mb-3")

                                sub_phone_in = ui.input("M-Pesa Phone Number (2547...)", placeholder="254712345678").props("outlined dense").classes("w-full mb-4")

                                def trigger_subscription_stk() -> None:
                                    phone = (sub_phone_in.value or "").strip()
                                    if not phone:
                                        ui.notify("Enter a valid Safaricom M-Pesa phone number.", type="warning")
                                        return

                                    is_annual = cycle_sel.value == "Annual"
                                    amount_kes = float(plan_obj.annual_price_kes if is_annual else plan_obj.monthly_price_kes)
                                    days_added = 365 if is_annual else 30

                                    db = SessionLocal()
                                    try:
                                        t_rec = db.query(Tenant).filter(Tenant.id == active_tid).first()
                                        if not t_rec:
                                            return

                                        ok, result = send_subscription_stk_push(t_rec, plan_obj.name, phone, amount_kes)
                                        if not ok:
                                            ui.notify(f"Subscription STK Push failed: {result}", type="negative")
                                            return

                                        mpesa_ref = f"SUB-{result[-8:]}" if result else f"SUB-{uuid.uuid4().hex[:8].upper()}"
                                        db.add(
                                            PlatformSubscriptionPayment(
                                                tenant_id=t_rec.id,
                                                plan_name=f"{plan_obj.name} ({cycle_sel.value})",
                                                amount_kes=amount_kes,
                                                mpesa_code=mpesa_ref,
                                                status="Completed",
                                            )
                                        )
                                        t_rec.tier = plan_obj.name
                                        t_rec.status = "Active"
                                        base_dt = (
                                            t_rec.subscription_ends_at
                                            if (t_rec.subscription_ends_at and t_rec.subscription_ends_at > datetime.utcnow())
                                            else datetime.utcnow()
                                        )
                                        t_rec.subscription_ends_at = base_dt + timedelta(days=days_added)
                                        db.commit()

                                        nicegui_app.storage.user["tier"] = plan_obj.name
                                        ui.notify(
                                            f"STK Prompt sent ({result})! Workspace upgraded to {plan_obj.name} ({cycle_sel.value}).",
                                            type="positive",
                                        )
                                        sub_dlg.close()
                                        render_settings()
                                    except Exception as exc:
                                        db.rollback()
                                        ui.notify(f"Error processing subscription upgrade: {exc}", type="negative")
                                    finally:
                                        db.close()

                                with ui.row().classes("w-full justify-end gap-2"):
                                    ui.button("Cancel", on_click=sub_dlg.close).props("flat")
                                    ui.button("Pay & Upgrade via M-Pesa STK", icon="smartphone", on_click=trigger_subscription_stk).classes("bg-emerald-600 text-white")
                            sub_dlg.open()

                        with settings_box:
                            with ui.card().classes("w-full p-6 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                with ui.row().classes("w-full items-center justify-between mb-4"):
                                    with ui.column().classes("gap-0"):
                                        ui.label("Agency Subscription Plan & M-Pesa Billing").classes("text-lg font-bold text-slate-900")
                                        ui.label(f"Current Plan: {current_tier} • Status: {current_status} • Valid Through: {expiry_txt}").classes("text-xs text-slate-500")
                                    ui.badge(f"{current_tier.upper()} ({current_status.upper()})", color="emerald")

                                with ui.grid(columns=3).classes("w-full gap-4 mb-4"):
                                    for pl in plans:
                                        is_current = pl.name.strip().lower() == current_tier.strip().lower()
                                        border_cls = "border-2 border-emerald-500 bg-emerald-50/30" if is_current else "border border-slate-200 bg-slate-50"
                                        with ui.card().classes(f"p-5 rounded-xl shadow-none {border_cls}"):
                                            with ui.row().classes("w-full items-center justify-between mb-1"):
                                                ui.label(pl.name).classes("text-base font-bold text-slate-900")
                                                if is_current:
                                                    ui.badge("ACTIVE", color="emerald").classes("text-[10px]")
                                            ui.label(f"KES {float(pl.monthly_price_kes or 0):,.0f} / mo").classes("text-xl font-bold text-slate-900")
                                            ui.label(f"or KES {float(pl.annual_price_kes or 0):,.0f} / yr").classes("text-xs text-emerald-700 font-medium mb-2")
                                            ui.label(f"• Up to {pl.max_staff} staff & writers").classes("text-xs text-slate-600")
                                            ui.label(f"• Up to {pl.max_active_projects} active orders").classes("text-xs text-slate-600 mb-2")
                                            ui.label(pl.features_summary or "").classes("text-xs text-slate-500 mb-4")

                                            btn_label = "Renew via M-Pesa STK" if is_current else f"Upgrade to {pl.name}"
                                            ui.button(
                                                btn_label,
                                                icon="smartphone",
                                                on_click=lambda p=pl: open_plan_upgrade_stk_modal(p),
                                            ).classes("w-full bg-emerald-600 text-white text-xs font-semibold")

                                if sub_history:
                                    ui.label("Subscription Payment History").classes("text-sm font-bold text-slate-800 mt-2 mb-2")
                                    sub_rows = [
                                        {
                                            "id": s.id,
                                            "plan": s.plan_name,
                                            "amount": f"KES {float(s.amount_kes or 0):,.0f}",
                                            "mpesa": s.mpesa_code,
                                            "status": s.status,
                                        }
                                        for s in sub_history
                                    ]
                                    sub_cols = [
                                        {"name": "plan", "label": "Plan & Cycle", "field": "plan", "align": "left"},
                                        {"name": "amount", "label": "Amount", "field": "amount", "align": "right"},
                                        {"name": "mpesa", "label": "M-Pesa Ref", "field": "mpesa", "align": "left"},
                                        {"name": "status", "label": "Status", "field": "status", "align": "left"},
                                    ]
                                    ui.table(columns=sub_cols, rows=sub_rows, row_key="id", pagination=5).classes("w-full flat")

                            with ui.grid(columns=2).classes("w-full gap-6"):
                                with ui.card().classes("p-6 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                    ui.label("Client Collection M-Pesa Daraja Credentials").classes("text-lg font-bold text-slate-900 mb-1")
                                    ui.label("Used when pushing M-Pesa STK prompts to your clients. Encrypted at rest.").classes("text-xs text-slate-500 mb-4")

                                    paybill_in = ui.input("Paybill / Till Shortcode", value=tenant_obj.mpesa_paybill if tenant_obj else "").props("outlined dense").classes("w-full mb-3")
                                    acct_in = ui.input("Account Reference Prefix", value=tenant_obj.mpesa_account if tenant_obj else "").props("outlined dense").classes("w-full mb-3")
                                    ck_in = ui.input("Consumer Key", value=decrypt_secret(tenant_obj.daraja_consumer_key) if tenant_obj else "", password=True, password_toggle_button=True).props("outlined dense").classes("w-full mb-3")
                                    cs_in = ui.input("Consumer Secret", value=decrypt_secret(tenant_obj.daraja_consumer_secret) if tenant_obj else "", password=True, password_toggle_button=True).props("outlined dense").classes("w-full mb-3")
                                    pk_in = ui.input("Lipa Na M-Pesa Passkey", value=decrypt_secret(tenant_obj.daraja_passkey) if tenant_obj else "", password=True, password_toggle_button=True).props("outlined dense").classes("w-full mb-4")

                                    def save_daraja_settings() -> None:
                                        db = SessionLocal()
                                        try:
                                            t = db.query(Tenant).filter(Tenant.id == active_tid).first()
                                            if not t:
                                                return
                                            t.mpesa_paybill = (paybill_in.value or "").strip()
                                            t.mpesa_account = (acct_in.value or "").strip()
                                            t.daraja_consumer_key = encrypt_secret(ck_in.value)
                                            t.daraja_consumer_secret = encrypt_secret(cs_in.value)
                                            t.daraja_passkey = encrypt_secret(pk_in.value)
                                            db.commit()
                                            ui.notify("M-Pesa Daraja credentials encrypted & saved!", type="positive")
                                        finally:
                                            db.close()

                                    ui.button("Save & Encrypt Credentials", icon="lock", on_click=save_daraja_settings).classes("bg-slate-900 text-white")

                                with ui.card().classes("p-6 rounded-xl bg-white border border-slate-200 shadow-sm"):
                                    ui.label("Support Desk (Super Admin)").classes("text-lg font-bold text-slate-900 mb-1")
                                    ui.label("Submit tickets and view responses from AcadFlow Platform Admin.").classes("text-xs text-slate-500 mb-4")
                                    tk_subj = ui.input("Subject").props("outlined dense").classes("w-full mb-3")
                                    tk_desc = ui.textarea("Describe your issue...").props("outlined dense rows=2").classes("w-full mb-3")

                                    def submit_ticket() -> None:
                                        if not tk_subj.value or not tk_desc.value:
                                            ui.notify("Enter subject and description.", type="warning")
                                            return
                                        db = SessionLocal()
                                        try:
                                            db.add(
                                                SupportTicket(
                                                    tenant_id=active_tid,
                                                    business_name=business_name,
                                                    subject=tk_subj.value.strip(),
                                                    description=tk_desc.value.strip(),
                                                    status="Open",
                                                )
                                            )
                                            db.commit()
                                            ui.notify("Support ticket submitted!", type="positive")
                                            render_settings()
                                        finally:
                                            db.close()

                                    ui.button("Submit Ticket", icon="support_agent", on_click=submit_ticket).classes("bg-slate-900 text-white mb-4")

                                    for tk in my_tickets[:5]:
                                        with ui.card().classes("w-full p-3 mb-2 border border-slate-200 shadow-none bg-slate-50"):
                                            with ui.row().classes("w-full justify-between items-center"):
                                                ui.label(tk.subject).classes("font-bold text-xs")
                                                ui.badge(tk.status, color="emerald" if tk.status == "Resolved" else "amber")
                                            if tk.admin_reply:
                                                ui.label(f"Super Admin Reply: {tk.admin_reply}").classes("text-xs text-emerald-700 font-medium mt-1")

                    render_settings()

    # -----------------------------------------------------------------------
    # CLIENT AGENCY SWITCHER DIALOG (Choose Agency by Client Reviews)
    # -----------------------------------------------------------------------
    def open_switch_agency_dialog() -> None:
        db = SessionLocal()
        try:
            rated_tenants = get_rated_tenants(db)
        finally:
            db.close()

        current_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
        with ui.dialog() as dlg, ui.card().classes("w-full max-w-md p-6 rounded-2xl"):
            ui.label("Choose Agency by Client Reviews").classes("text-lg font-bold text-slate-900 mb-1")
            ui.label("Agencies are ranked from highest to lowest client rating.").classes("text-xs text-slate-500 mb-4")
            sel = ui.select(rated_tenants, value=current_tid, label="Select Agency").props("outlined dense").classes("w-full mb-4")

            def apply_agency_switch() -> None:
                if not sel.value:
                    return
                db = SessionLocal()
                try:
                    t = db.query(Tenant).filter(Tenant.id == sel.value).first()
                    u = db.query(User).filter(User.id == user_id).first()
                    if t and u:
                        u.tenant_id = t.id
                        db.commit()
                        nicegui_app.storage.user["tenant_id"] = t.id
                        nicegui_app.storage.user["business_name"] = t.business_name
                        nicegui_app.storage.user["tier"] = t.tier
                        ui.notify(f"Switched to {t.business_name}", type="positive")
                        dlg.close()
                        ui.navigate.to("/")
                finally:
                    db.close()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat")
                ui.button("Switch Agency", icon="check", on_click=apply_agency_switch).classes("bg-emerald-600 text-white")
        dlg.open()

    # -----------------------------------------------------------------------
    # MODAL 1: CREATE / SUBMIT NEW ASSIGNMENT
    # -----------------------------------------------------------------------
    def open_new_assignment_dialog() -> None:
        if is_superadmin:
            ui.notify("Super Admins cannot upload assignments.", type="warning")
            return

        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
        db = SessionLocal()
        try:
            phone_map = get_user_phone_map(db, active_tid)
            client_options = {
                u.id: f"{u.full_name} ({u.email} • {phone_map.get(u.id) or 'No phone'})"
                for u in db.query(User).filter(User.tenant_id == active_tid, func.lower(User.role) == "client").all()
            }
            writer_options = {
                u.id: f"{u.full_name} ({u.commission_rate or 0:.0f}%)"
                for u in db.query(User).filter(User.tenant_id == active_tid, func.lower(User.role) == "writer").all()
            }
            writer_rates = {
                u.id: float(u.commission_rate or 50.0)
                for u in db.query(User).filter(User.tenant_id == active_tid, func.lower(User.role) == "writer").all()
            }
            rated_tenants = get_rated_tenants(db)
            services = db.query(Service).filter(Service.tenant_id == active_tid).all()
            service_options = {s.id: f"{s.title} (KES {float(s.base_price_kes or 0):,.0f})" for s in services}
            service_prices = {s.id: float(s.base_price_kes or 0) for s in services}
            service_days = {s.id: int(s.turnaround_days or 3) for s in services}
        finally:
            db.close()

        if is_tenant_admin and not client_options:
            ui.notify("Register or add a Client in 'Team & Clients' before uploading a client assignment.", type="warning")
            return

        staged_files: list[dict] = []
        default_deadline = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")

        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg p-6 rounded-2xl max-h-[90vh] overflow-y-auto"):
            ui.label("Submit New Assignment" if is_client else "Upload Client Assignment").classes("text-lg font-bold text-slate-900 mb-3")

            if is_client:
                tenant_sel = ui.select(
                    rated_tenants,
                    value=active_tid,
                    label="Select Agency (Ranked by Client Reviews)",
                ).classes("w-full mb-2").props("outlined dense")
                client_sel = None
                writer_sel = None
            else:
                tenant_sel = None
                client_sel = ui.select(client_options, label="Select Client (Required)").classes("w-full mb-2").props("outlined dense")
                writer_sel = ui.select(writer_options, label="Assign Writer (Optional)").classes("w-full mb-2").props("outlined dense clearable")

            title_in = ui.input("Assignment Title / Topic").classes("w-full mb-2").props("outlined dense")
            desc_in = ui.textarea("Instructions, Formatting Style & Word Count").classes("w-full mb-2").props("outlined dense rows=3")
            service_sel = ui.select(service_options, label="Select Service Package" if service_options else "Service Package (Optional)").classes("w-full mb-2").props("outlined dense clearable")

            def on_tenant_change(e) -> None:
                if not e.value:
                    return
                db = SessionLocal()
                try:
                    svcs = db.query(Service).filter(Service.tenant_id == e.value).all()
                    service_prices.clear()
                    service_days.clear()
                    new_opts = {}
                    for s in svcs:
                        new_opts[s.id] = f"{s.title} (KES {float(s.base_price_kes or 0):,.0f})"
                        service_prices[s.id] = float(s.base_price_kes or 0)
                        service_days[s.id] = int(s.turnaround_days or 3)
                    service_sel.options = new_opts
                    service_sel.value = None
                    service_sel.update()
                finally:
                    db.close()

            if tenant_sel is not None:
                tenant_sel.on_value_change(on_tenant_change)

            with ui.row().classes("w-full gap-2 mb-2"):
                quoted_in = ui.number(
                    "Estimated Fee (KES)" if is_client else "Quoted Fee (KES)",
                    value=2500,
                    min=1,
                ).classes("flex-1").props("outlined dense")
                prio_sel = ui.select(["Low", "Normal", "High", "Urgent"], value="Normal", label="Urgency / Priority").classes("w-40").props("outlined dense")

            deadline_in = ui.input("Deadline (YYYY-MM-DD HH:MM)", value=default_deadline).classes("w-full mb-3").props("outlined dense")

            def on_service_change(e) -> None:
                if e.value and e.value in service_prices:
                    quoted_in.value = service_prices[e.value]
                    days = service_days.get(e.value, 3)
                    deadline_in.value = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

            service_sel.on_value_change(on_service_change)

            async def stage_brief_file(e: events.UploadEventArguments) -> None:
                raw_bytes = await e.file.read()
                staged_files.append({"name": e.file.name, "bytes": raw_bytes})
                ui.notify(f"Attached brief: {e.file.name}", type="info")

            ui.upload(
                label="Attach Brief / Rubric Files (PDF, DOCX, ZIP)",
                multiple=True,
                auto_upload=True,
                on_upload=stage_brief_file,
            ).classes("w-full mb-4")

            def save_assignment() -> None:
                if not (title_in.value or "").strip():
                    ui.notify("Assignment title is required.", type="warning")
                    return

                target_tid = (tenant_sel.value if tenant_sel else active_tid) or active_tid
                resolved_client_id = user_id if is_client else (client_sel.value if client_sel else None)
                if not resolved_client_id:
                    ui.notify("Please select a Client for this assignment.", type="warning")
                    return

                try:
                    parsed_deadline = datetime.strptime((deadline_in.value or "").strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        parsed_deadline = datetime.strptime((deadline_in.value or "").strip(), "%Y-%m-%d")
                    except ValueError:
                        parsed_deadline = datetime.utcnow() + timedelta(days=3)

                resolved_writer_id = None if is_client else (writer_sel.value if writer_sel else None)
                comm_pct = writer_rates.get(resolved_writer_id, 50.0) if resolved_writer_id else 50.0
                fee_amount = float(quoted_in.value or 0)

                db = SessionLocal()
                try:
                    if is_client and target_tid != active_tid:
                        chosen_t = db.query(Tenant).filter(Tenant.id == target_tid).first()
                        client_u = db.query(User).filter(User.id == user_id).first()
                        if chosen_t and client_u:
                            client_u.tenant_id = chosen_t.id
                            nicegui_app.storage.user["tenant_id"] = chosen_t.id
                            nicegui_app.storage.user["business_name"] = chosen_t.business_name
                            nicegui_app.storage.user["tier"] = chosen_t.tier

                    ref_code = f"AF-{uuid.uuid4().hex[:6].upper()}"
                    new_item = Assignment(
                        tenant_id=target_tid,
                        ref_number=ref_code,
                        invoice_number=f"INV-{ref_code}",
                        title=title_in.value.strip(),
                        description=(desc_in.value or "").strip(),
                        service_id=service_sel.value,
                        client_id=resolved_client_id,
                        staff_id=resolved_writer_id,
                        priority=prio_sel.value,
                        quoted_cost_kes=fee_amount,
                        amount_paid_kes=0.0,
                        staff_commission_pct=comm_pct,
                        staff_payout_status="Unpaid",
                        deadline=parsed_deadline,
                        status=ASSIGNMENT_STATUSES[0],
                    )
                    db.add(new_item)
                    db.flush()

                    for f in staged_files:
                        file_hash = hashlib.sha256(f["bytes"]).hexdigest()
                        safe_name = f"{new_item.id}_{uuid.uuid4().hex[:8]}_{f['name']}"
                        save_path = STORAGE_DIR / safe_name
                        save_path.write_bytes(f["bytes"])
                        db.add(
                            AssignmentDocument(
                                tenant_id=target_tid,
                                assignment_id=new_item.id,
                                uploaded_by_name=full_name,
                                doc_category="Client Brief",
                                original_filename=f["name"],
                                file_hash=file_hash,
                                file_path=str(save_path),
                            )
                        )

                    db.add(
                        AssignmentLog(
                            tenant_id=target_tid,
                            assignment_id=new_item.id,
                            actor_name=full_name,
                            action_note=f"ORDER CREATED ({role}): {ref_code} (KES {fee_amount:,.0f}, {len(staged_files)} attachment(s))",
                        )
                    )

                    admin_users = db.query(User).filter(User.tenant_id == target_tid, func.lower(User.role) == "admin").all()
                    for adm in admin_users:
                        log_email_event(db, target_tid, adm.email, f"New Order {ref_code}: {new_item.title}", f"Submitted by {full_name}")

                    db.commit()
                    ui.notify(f"Assignment {ref_code} submitted!", type="positive")
                    dialog.close()
                    if is_client and target_tid != active_tid:
                        ui.navigate.to("/")
                    else:
                        render_orders()
                except Exception as exc:
                    db.rollback()
                    ui.notify(f"Could not save assignment: {exc}", type="negative")
                finally:
                    db.close()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Submit Order" if is_client else "Save Client Assignment", icon="check", on_click=save_assignment).classes("bg-emerald-600 text-white")
        dialog.open()

    # -----------------------------------------------------------------------
    # MODAL 3: LIVE DARAJA M-PESA STK PUSH (CLIENT ORDER PAYMENT)
    # -----------------------------------------------------------------------
    def open_stk_modal(row: dict) -> None:
        active_tid = nicegui_app.storage.user.get("tenant_id", tenant_id)
        default_phone = (row.get("client_phone") or nicegui_app.storage.user.get("phone_number") or "").strip()
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-md p-6 rounded-2xl"):
            ui.label(f"M-Pesa STK Push — {row['ref']}").classes("text-lg font-bold text-slate-900")
            ui.label(row["title"]).classes("text-sm text-slate-500 mb-4")
            phone_in = ui.input("M-Pesa Phone Number (2547...)", value=default_phone, placeholder="254712345678").classes("w-full mb-3").props("outlined dense")
            amount_in = ui.number("Amount (KES)", value=row["balance"], min=1).classes("w-full mb-4").props("outlined dense")

            def trigger_stk() -> None:
                phone = (phone_in.value or "").strip()
                amount = float(amount_in.value or 0)
                if not phone or amount < 1:
                    ui.notify("Enter a valid phone number and amount.", type="warning")
                    return

                db = SessionLocal()
                try:
                    tenant = db.query(Tenant).filter(Tenant.id == active_tid).first()
                    assignment = db.query(Assignment).filter(Assignment.id == row["id"]).first()
                    if not tenant or not assignment:
                        ui.notify("Assignment or workspace not found.", type="negative")
                        return

                    ok, result = send_tenant_stk_push(tenant, assignment, phone, amount)
                    if not ok:
                        ui.notify(f"STK Push failed: {result}", type="negative")
                        return

                    pending_code = f"PENDING-{result[-8:]}" if result else f"PENDING-{uuid.uuid4().hex[:8].upper()}"
                    tx = ClientPaymentTransaction(
                        tenant_id=tenant.id,
                        assignment_id=assignment.id,
                        checkout_request_id=result,
                        amount_kes=amount,
                        mpesa_code=pending_code,
                        phone_number=normalize_kenyan_phone(phone),
                        status="Pending Verification",
                    )
                    db.add(tx)
                    db.add(
                        AssignmentLog(
                            tenant_id=tenant.id,
                            assignment_id=assignment.id,
                            actor_name=full_name,
                            action_note=f"M-PESA STK PUSH INITIATED: KES {amount:,.0f} to {normalize_kenyan_phone(phone)}",
                        )
                    )
                    db.commit()
                    ui.notify(f"STK Push sent! Checkout ID: {result}", type="positive")
                    dialog.close()
                    render_orders()
                    if not is_writer:
                        render_payments()
                except Exception as exc:
                    db.rollback()
                    ui.notify(f"Database error recording STK push: {exc}", type="negative")
                finally:
                    db.close()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Send STK Prompt", icon="send", on_click=trigger_stk).classes("bg-emerald-600 text-white")
        dialog.open()


init_db_schema()
ensure_user_phone_column()
ui.run_with(
    fastapi_app,
    title="AcadFlow | Academic Agency OS",
    storage_secret=os.getenv("DARAJA_WEBHOOK_TOKEN", "change-this-session-secret"),
)