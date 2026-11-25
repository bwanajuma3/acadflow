
# AcadFlow — Multi-Tenant Academic Agency & M-Pesa OS

**AcadFlow** is a full-stack, multi-tenant SaaS platform built with **Python**, **NiceGUI**, **FastAPI**, and **SQLAlchemy** for managing academic writing agencies, freelance writers, and clients. It combines role-isolated agency workspaces, quality-gated deliverable approvals, real-time interaction timelines, and automated **Safaricom M-Pesa Daraja STK Push** collections.

---

## Key Features

### 1. Multi-Tenant Role Architecture
* **Super Admin (`/superadmin`)**:
  * Provision, inspect, or suspend agency workspaces across **Starter**, **Pro**, and **Enterprise** subscription tiers.
  * View full directory of platform staff, writers, agency admins, and clients (including **Name**, **Email**, and **Phone Number**).
  * Manage SaaS subscription pricing plans, review tenant subscription payment history, resolve support desk tickets, and publish 72-hour auto-expiring platform announcements.
* **Agency Admin (`/`)**:
  * Manage client assignments, quote fees in KES, assign writers with custom commission rates (`%`), and review writer submissions before releasing them to clients.
  * View full client contact details (**Name**, **Email**, **Phone Number**) and send one-click **Payment Reminders** on unpaid orders.
  * Configure encrypted per-tenant **Safaricom Daraja API** credentials (`Consumer Key`, `Consumer Secret`, `Passkey`, `Paybill/Till Shortcode`).
  * Track writer commission balances and settle writer M-Pesa payouts once orders are closed.
  * Upgrade or renew agency SaaS subscriptions via M-Pesa STK Push.
* **Writer Portal (`/`)**:
  * View assigned orders, collaborate with agency admins on internal `Team` channels, upload drafts/final deliverables/Turnitin reports, and submit orders for quality review.
  * Track earned commissions and M-Pesa payout settlement status.
* **Client Portal (`/`)**:
  * Register with **Email** and **M-Pesa Phone Number**, browse agencies ranked by verified client reviews, and switch agencies anytime.
  * Submit assignment briefs with attached files (`PDF`, `DOCX`, `ZIP`), chat directly with the agency (`Client-Agency` channel), and pay via pre-filled **M-Pesa STK Push**.
  * Review approved deliverables, click **Accept Work** or **Request Revision**, and leave 1–5 star agency reviews.

---

### 2. Quality-Gated Order Lifecycle & Strict Closure Rules
1. **Order Creation (`Pending`)**: Client or Agency Admin submits an assignment brief with deadline and quoted fee (KES).
2. **Writer Dispatch & Execution (`In Progress`)**: Agency Admin assigns a writer; deliverables uploaded by writers remain hidden from the client until approved.
3. **Quality Assurance (`Under Review` / `Revision Requested`)**: Writer submits work for admin review. The Agency Admin inspects the files and either requests writer revisions or clicks **Approve & Release to Client**.
4. **Client Review & Payment (`Awaiting Client Acceptance`)**:
   * Approved files unlock for the client.
   * Clients see an **Accept Work** button, a **Request Revision** button, and a **Payment Reminder** banner whenever a balance remains.
5. **Strict Order Closure (`Completed`)**:
   * An order transitions to **`Completed`** (and unlocks writer commission payout settlement) **only** when **both** conditions are satisfied:
     1. The client has explicitly clicked **Accept Work** on the delivered files.
     2. The remaining balance due is **`KES 0`** (paid in full via M-Pesa).

---

### 3. Highlighted Interaction Timeline & Audit Trail
* Color-coded status badges (`Pending`, `In Progress`, `Under Review`, `Revision Requested`, `Awaiting Client Acceptance`, `Completed`) across the assignments table and workspace headers.
* Every status shift, file upload, writer assignment, payment reminder, STK push, and client acceptance is logged in a color-highlighted **Interaction Timeline** with actor names, role context, and timestamps.

---

### 4. M-Pesa Daraja STK Push & Webhook Reconciliation
* **Per-Tenant Client Collections**: Each agency stores its own Daraja credentials encrypted at rest using Fernet (`FERNET_SECRET_KEY`). STK prompts automatically pre-fill the client's saved phone number (`2547...`).
* **Platform SaaS Subscriptions**: Agencies upgrade or renew subscription plans via STK Push routed to platform Daraja credentials.
* **FastAPI Callback Webhook (`webhook_api.py`)**: Receives asynchronous Safaricom Daraja STK callbacks at `/api/payments/daraja-callback`, verifies the webhook token, records the M-Pesa receipt code, updates `amount_paid_kes`, and logs the transaction in the M-Pesa Ledger.

---

## Project Structure

```text
├── nicegui_app.py        # Main NiceGUI web application & UI routes (/login, /, /superadmin)
├── webhook_api.py        # FastAPI server & Safaricom Daraja M-Pesa callback endpoints
├── database.py           # SQLAlchemy models, engine setup & schema initialization
├── acadflow_storage/     # Local file storage for uploaded briefs & deliverables (SHA-256 hashed)
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation

```

---

## Environment Variables

Create a `.env` file or configure the following environment variables in your deployment environment (e.g., Render, Railway, Docker):

| Variable | Description | Example |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL or SQLite connection URI | `postgresql://user:pass@host:5432/acadflow` |
| `FERNET_SECRET_KEY` | 32-byte URL-safe base64 key for encrypting tenant Daraja keys | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DARAJA_ENV` | Safaricom API environment (`sandbox` or `production`) | `sandbox` |
| `WEBHOOK_BASE_URL` | Public HTTPS URL where Daraja posts STK callbacks | `https://acadflow-webhook.onrender.com` |
| `DARAJA_WEBHOOK_TOKEN` | Secret query token & NiceGUI session storage secret | `your-random-secret-token` |
| `PLATFORM_DARAJA_CONSUMER_KEY` | *(Optional)* Platform consumer key for agency SaaS billing | `...` |
| `PLATFORM_DARAJA_CONSUMER_SECRET` | *(Optional)* Platform consumer secret for agency SaaS billing | `...` |
| `PLATFORM_DARAJA_PASSKEY` | *(Optional)* Platform Lipa Na M-Pesa passkey | `...` |
| `PLATFORM_MPESA_PAYBILL` | *(Optional)* Platform Paybill/Till shortcode | `174379` |

---

## Local Setup & Running

1. **Clone the repository & install dependencies:**
```bash
pip install -r requirements.txt

```


2. **Configure environment variables:**
```bash
export DATABASE_URL="sqlite:///./acadflow.db"
export DARAJA_ENV="sandbox"
export DARAJA_WEBHOOK_TOKEN="dev-secret-key"

```


3. **Start the application:**
Because `nicegui_app.py` mounts directly onto the FastAPI instance (`ui.run_with(fastapi_app, ...)`), running the Uvicorn/NiceGUI entrypoint serves both the web UI and the M-Pesa webhook endpoints on the same port:
```bash
uvicorn nicegui_app:fastapi_app --host 0.0.0.0 --port 8000 --reload

```


4. **Access the portals:**
* **Login / Sign Up / New Agency**: `http://localhost:8000/login`
* **Agency / Writer / Client Workspace**: `http://localhost:8000/`
* **Super Admin Platform Console**: `http://localhost:8000/superadmin`



---

## Database Schema Notes

* On startup, `init_db_schema()` initializes all core tables (`tenants`, `users`, `services`, `assignments`, `assignment_documents`, `assignment_messages`, `assignment_logs`, `client_payment_transactions`, `client_reviews`, `subscription_plans`, `platform_subscription_payments`, `support_tickets`, `platform_announcements`, `email_notification_logs`).
* `ensure_user_phone_column()` automatically verifies and adds `phone_number` to `users` if upgrading an existing database without requiring a manual reset.

```

```