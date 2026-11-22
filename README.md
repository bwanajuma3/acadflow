# 🎓 AcadFlow

**From Submission to Completion — Multi-Tenant Academic & Research Project Management SaaS**

AcadFlow is a web-based, multi-tenant Software-as-a-Service (SaaS) platform built in Python for academic consultancies, research hubs, and freelance statistical/editorial practices. It manages the full client project lifecycle—from request submission and automated PDF quotation/invoicing to M-Pesa Daraja payment verification, staff assignment, deliverable tracking, and client satisfaction reviews.

---

## ✨ Key Features

- **Strict Multi-Tenant Isolation:** Shared-database, shared-schema architecture enforced via `tenant_id` partitioning across workspaces, users, services, files, messages, and financial records.
- **Role-Based Access Control (RBAC):**
  - **Super Admin:** Platform-wide governance, subscription tier pricing/limit configuration, workspace status control (`Active`, `Trial`, `Suspended`, `Expired`), SaaS subscription ledger, broadcast announcements, support desk, and email audit logs.
  - **Tenant Admin:** Custom service catalogue management, project triage, staff allocation, M-Pesa Daraja API configuration, client payment verification, analytics dashboards, and SaaS subscription renewal.
  - **Staff / Researcher:** Dedicated queue of assigned projects, status progression, deliverable uploads, and client discussion threads.
  - **Client:** Self-service workspace registration, service request submission, PDF quotation/invoice downloads, M-Pesa STK Push / receipt submission, revision requests, and 1–5 star satisfaction reviews.
- **11-Stage Project Lifecycle:** Tracks assignments sequentially from `1. Submitted` through `6. Assigned`, `8. Awaiting client feedback`, `10. Completed`, and `11. Closed` with an immutable audit trail.
- **Dual Financial Ledgers:**
  - **Ledger 1 (Platform SaaS Revenue):** Subscription fees paid by consultancies to the AcadFlow platform owner.
  - **Ledger 2 (Client Project Payments):** Project fees paid by individual clients directly to each consultancy's M-Pesa Paybill/Till.
- **Automated Branded PDFs (`fpdf2`):** Dynamically generates downloadable PDF Quotations and Invoices/Receipts stamped with the business's branding, invoice number, balance due, and Paybill instructions.
- **Built-In Governance & Deduplication Guards:**
  - Blocks duplicate active project submissions with matching titles.
  - Enforces SHA-256 checksum deduplication on uploaded files.
  - Prevents duplicate M-Pesa receipt code submissions.
  - Enforces subscription tier limits (`Max Staff` and `Max Active Projects`).

---

## 🛠️ Tech Stack

- **Frontend / Web UI:** Python, Streamlit
- **Database & ORM:** PostgreSQL (Neon Cloud) / SQLite (Local Fallback), SQLAlchemy (`psycopg` v3)
- **Payments & Webhooks:** Safaricom M-Pesa Daraja API (STK Push + FastAPI C2B Callback Receiver)
- **PDF Generation:** `fpdf2`
- **Security:** `bcrypt` password hashing, `cryptography` (Fernet) secret encryption, `.env` environment isolation

---

## 📂 Project Structure

```text
acadflow/
├── acadflow_mvp.py      # Main multi-tenant Streamlit application
├── database.py          # Production SQLAlchemy engine & models
├── security.py          # Bcrypt password & Fernet API secret helpers
├── webhook_api.py       # FastAPI server for M-Pesa Daraja STK callbacks
├── requirements.txt     # Python dependencies
├── .gitignore           # Excludes .env, venv/, local SQLite DBs, and storage
└── README.md            # Project documentation

🚀 Local Setup & Installation
1. Clone the Repository & Create a Virtual Environment
Bash
git clone [https://github.com/YOUR_GITHUB_USERNAME/acadflow.git](https://github.com/YOUR_GITHUB_USERNAME/acadflow.git)
cd acadflow
python -m venv venv
Activate the virtual environment:

Windows (Git Bash): source venv/Scripts/activate

Windows (PowerShell): venv\Scripts\Activate.ps1

macOS / Linux: source venv/bin/activate

2. Install Dependencies
Bash
pip install -r requirements.txt
3. Configure Environment Variables
Create a .env file in the project root:

Code snippet
DATABASE_URL="postgresql://USER:PASSWORD@HOST/neondb?sslmode=require"
FERNET_KEY="your_generated_fernet_key"
# Optional SMTP settings for live email notifications:
# SMTP_HOST="smtp.gmail.com"
# SMTP_USER="notifications@yourdomain.com"
# SMTP_PASS="your_app_password"
(If DATABASE_URL is omitted, AcadFlow automatically falls back to a local acadflow_mvp.db SQLite database.)

4. Run the Application
Bash
python -m streamlit run acadflow_mvp.py
Open http://localhost:8501 in your browser. On first launch, AcadFlow automatically creates all database tables and seeds demo accounts for testing every role.

☁️ Cloud Deployment (Streamlit Community Cloud + Neon PostgreSQL)
Provision a PostgreSQL database on Neon.tech and copy the pooled postgresql:// connection string.

Push this repository to GitHub (ensure .env is listed in .gitignore).

Deploy via share.streamlit.io pointing to acadflow_mvp.py (Python 3.12).

Under Advanced settings → Secrets, add:

Ini, TOML
DATABASE_URL = "postgresql://USER:PASSWORD@HOST/neondb?sslmode=require"
(Optional) Deploy webhook_api.py on Render or Railway (uvicorn webhook_api:app --host 0.0.0.0 --port $PORT) to receive live Safaricom Daraja STK Push callbacks over HTTPS.

🔐 Default Demo Credentials (Seeded on First Run)
Role	Workspace	Email	Password
Super Admin	Platform Owner	super@acadflow.co.ke	admin123
Tenant Admin	Nairobi Research & Thesis Hub	admin@nairobihub.co.ke	admin123
Staff	Nairobi Research & Thesis Hub	mercy@nairobihub.co.ke	staff123
Client	Nairobi Research & Thesis Hub	brian@student.ac.ke	client123
Tenant Admin	ScholarAssist Kenya Ltd	admin@scholarassist.co.ke	admin123
📄 License
Proprietary — All rights reserved.