import hashlib
import hmac
import html
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


def load_dotenv_file(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv_file()


def normalize_base_url(value: str, default: str):
    clean = (value or "").strip().rstrip("/")
    if not clean:
        return default.rstrip("/")
    if clean.startswith("//"):
        return f"https:{clean}".rstrip("/")
    if "://" not in clean:
        return f"https://{clean}".rstrip("/")
    return clean


def load_json_object_env(name: str):
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key).strip().lower(): str(value).strip() for key, value in data.items() if str(value).strip()}


def load_float_env(name: str, default: float):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_int_env(name: str, default: int):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


APP_NAME = "FileSync Pro"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")
DB_PATH = Path(os.getenv("FILESYNC_PRO_DB", "filesync_pro.db"))
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "")
PRICE_AMOUNT = load_float_env("FILESYNC_PRO_PRICE", 49.99)
PRICE_CURRENCY = os.getenv("FILESYNC_PRO_CURRENCY", "COP")
LICENSE_ACTIVATIONS = load_int_env("FILESYNC_PRO_MAX_ACTIVATIONS", 2)
MP_SUBSCRIPTION_PLAN_MAP = load_json_object_env("MP_SUBSCRIPTION_PLAN_MAP")
MP_SUBSCRIPTION_LINK_MAP = load_json_object_env("MP_SUBSCRIPTION_LINK_MAP")
DEFAULT_SUBSCRIPTION_PLAN_CODE = os.getenv("FILESYNC_PRO_DEFAULT_PLAN_CODE", "basica").strip().lower() or "basica"
SUBSCRIPTION_ACTIVE_STATUSES = {"authorized", "active"}


class CheckoutRequest(BaseModel):
    email: EmailStr
    plan_code: str = DEFAULT_SUBSCRIPTION_PLAN_CODE


class LicenseRequest(BaseModel):
    email: EmailStr
    license_key: str
    device_id: str


class SubscriptionCheckoutRequest(BaseModel):
    email: EmailStr
    plan_code: str = DEFAULT_SUBSCRIPTION_PLAN_CODE
    external_reference: str = ""


class SubscriptionLinkRequest(BaseModel):
    email: EmailStr
    plan_code: str = DEFAULT_SUBSCRIPTION_PLAN_CODE


class SubscriptionLookupRequest(BaseModel):
    email: EmailStr
    plan_code: str = DEFAULT_SUBSCRIPTION_PLAN_CODE


app = FastAPI(title="FileSync Pro Licensing API")


def sql(query: str):
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


def db():
    if USE_POSTGRES:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL esta configurada, pero falta instalar psycopg.")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table_name: str):
    if USE_POSTGRES:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        ).fetchall()
        return {row["column_name"] if isinstance(row, dict) else row[0] for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def ensure_column(conn, table_name: str, column_name: str, definition: str):
    if column_name in table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db():
    conn = db()
    statements = [
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            preference_id TEXT,
            payment_id TEXT,
            status TEXT NOT NULL,
            license_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            order_id TEXT NOT NULL,
            status TEXT NOT NULL,
            activations_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            subscription_id TEXT PRIMARY KEY,
            external_reference TEXT NOT NULL,
            email TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            init_point TEXT,
            payer_id TEXT,
            next_payment_date TEXT,
            last_payment_id TEXT,
            license_key TEXT,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]
    for statement in statements:
        conn.execute(statement)
    ensure_column(conn, "licenses", "subscription_id", "TEXT")
    conn.commit()
    conn.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def escape(value):
    return html.escape(str(value), quote=True)


def new_license_key():
    return f"FSP-{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:8].upper()}"


def subscription_grants_access(status: str):
    return (status or "").strip().lower() in SUBSCRIPTION_ACTIVE_STATUSES


def render_page(title: str, body: str):
    page_title = escape(title)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <style>
    :root {{
      --bg: #f3efe7;
      --surface: #fffdf8;
      --ink: #17263c;
      --muted: #5e6b7a;
      --line: #d8d0c1;
      --accent: #0f5f8c;
      --accent-2: #ef8f00;
      --ok: #18794e;
      --warn: #9a6700;
      --danger: #b42318;
      --shadow: 0 18px 50px rgba(23, 38, 60, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(239, 143, 0, 0.12), transparent 28%),
        linear-gradient(180deg, #efe8db 0%, var(--bg) 35%, #f7f4ee 100%);
    }}
    .shell {{
      min-height: 100vh;
      padding: 32px 18px 56px;
    }}
    .wrap {{
      max-width: 1040px;
      margin: 0 auto;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 24px;
      align-items: stretch;
      margin-bottom: 24px;
    }}
    .card {{
      background: rgba(255, 253, 248, 0.94);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 28px;
      backdrop-filter: blur(8px);
    }}
    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--accent);
      font-size: 14px;
      margin-bottom: 16px;
    }}
    .dot {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 0 0 6px rgba(15, 95, 140, 0.08);
    }}
    h1, h2, h3 {{
      margin: 0 0 12px;
      line-height: 1.05;
    }}
    h1 {{
      font-size: clamp(42px, 8vw, 72px);
      letter-spacing: -0.05em;
      max-width: 11ch;
    }}
    h2 {{ font-size: 28px; }}
    p {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.6;
      font-size: 16px;
    }}
    .price {{
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin: 18px 0 10px;
    }}
    .price strong {{
      font-size: 42px;
      letter-spacing: -0.05em;
    }}
    .badge {{
      display: inline-block;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(15, 95, 140, 0.1);
      color: var(--accent);
      font-weight: 600;
      font-size: 13px;
      margin-bottom: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin: 22px 0 24px;
    }}
    .feature {{
      padding: 18px;
      border-radius: 18px;
      background: rgba(15, 95, 140, 0.05);
      border: 1px solid rgba(15, 95, 140, 0.1);
    }}
    .feature h3 {{
      font-size: 16px;
      margin-bottom: 8px;
    }}
    .feature p {{
      font-size: 14px;
      margin: 0;
    }}
    .form {{
      display: grid;
      gap: 14px;
      margin-top: 12px;
    }}
    label {{
      display: block;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.02em;
      margin-bottom: 6px;
    }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px 16px;
      font-size: 16px;
      background: #fff;
      color: var(--ink);
    }}
    input:focus {{
      outline: 2px solid rgba(15, 95, 140, 0.2);
      border-color: var(--accent);
    }}
    button, .button {{
      display: inline-flex;
      justify-content: center;
      align-items: center;
      gap: 8px;
      width: 100%;
      border: 0;
      border-radius: 14px;
      padding: 15px 18px;
      background: linear-gradient(135deg, var(--accent), #1b789f);
      color: #fff;
      font-size: 16px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }}
    .button.secondary {{
      background: transparent;
      color: var(--accent);
      border: 1px solid rgba(15, 95, 140, 0.2);
    }}
    .meta {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
      font-size: 14px;
      color: var(--muted);
    }}
    .status {{
      display: inline-block;
      margin-bottom: 16px;
      padding: 7px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
    }}
    .status.ok {{ background: rgba(24, 121, 78, 0.12); color: var(--ok); }}
    .status.warn {{ background: rgba(154, 103, 0, 0.12); color: var(--warn); }}
    .status.danger {{ background: rgba(180, 35, 24, 0.12); color: var(--danger); }}
    .panel {{
      max-width: 760px;
      margin: 0 auto;
    }}
    .code {{
      font-family: Consolas, monospace;
      background: #f5f1e8;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      word-break: break-word;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 22px;
    }}
    .actions .button {{
      width: auto;
      min-width: 210px;
    }}
    @media (max-width: 840px) {{
      .hero, .grid {{
        grid-template-columns: 1fr;
      }}
      .card {{
        padding: 22px;
      }}
      h1 {{
        max-width: none;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="wrap">
      {body}
    </div>
  </div>
</body>
</html>"""
    )


def mp_headers():
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Configura MP_ACCESS_TOKEN en el backend.")
    return {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}


def mp_request(method: str, url: str, payload=None, params=None):
    headers = mp_headers()
    if method.upper() in {"POST", "PUT", "PATCH"}:
        headers = {**headers, "X-Idempotency-Key": uuid.uuid4().hex}
    response = requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        data=json.dumps(payload) if payload is not None else None,
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Mercado Pago devolvio {response.status_code}: {response.text}")
    if not response.text.strip():
        return {}
    return response.json()


def create_mp_preference(order_id: str, email: str, plan_code: str):
    payload = {
        "items": [
            {
                "id": plan_code,
                "title": f"{APP_NAME} Licencia vitalicia",
                "description": "Acceso completo a la app de escritorio",
                "quantity": 1,
                "currency_id": PRICE_CURRENCY,
                "unit_price": PRICE_AMOUNT,
            }
        ],
        "payer": {"email": email},
        "back_urls": {
            "success": f"{SITE_BASE_URL}/checkout/return/success",
            "pending": f"{SITE_BASE_URL}/checkout/return/pending",
            "failure": f"{SITE_BASE_URL}/checkout/return/failure",
        },
        "notification_url": f"{PUBLIC_BASE_URL}/webhooks/mercadopago",
        "auto_return": "approved",
        "external_reference": order_id,
    }
    return mp_request("POST", "https://api.mercadopago.com/checkout/preferences", payload)


def fetch_order(order_id: str):
    conn = db()
    row = conn.execute(sql("SELECT * FROM orders WHERE order_id = ?"), (order_id,)).fetchone()
    conn.close()
    return row


def fetch_subscription(subscription_id: str):
    conn = db()
    row = conn.execute(sql("SELECT * FROM subscriptions WHERE subscription_id = ?"), (subscription_id,)).fetchone()
    conn.close()
    return row


def fetch_subscription_by_external_reference(external_reference: str):
    conn = db()
    row = conn.execute(
        sql("SELECT * FROM subscriptions WHERE external_reference = ?"),
        (external_reference,),
    ).fetchone()
    conn.close()
    return row


def fetch_order_for_license(row):
    order_id = row["order_id"] if row and row["order_id"] else ""
    if not order_id:
        return None
    return fetch_order(order_id)


def resolve_subscription_plan(plan_code: str):
    normalized = (plan_code or DEFAULT_SUBSCRIPTION_PLAN_CODE).strip().lower()
    env_plan_id = os.getenv(f"MP_SUBSCRIPTION_PLAN_ID_{normalized.upper()}", "").strip()
    plan_id = MP_SUBSCRIPTION_PLAN_MAP.get(normalized) or env_plan_id
    if not plan_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No existe un plan configurado para '{normalized}'. "
                "Define MP_SUBSCRIPTION_PLAN_MAP o MP_SUBSCRIPTION_PLAN_ID_<PLAN_CODE>."
            ),
        )
    return normalized, plan_id


def resolve_subscription_link(plan_code: str):
    normalized = (plan_code or DEFAULT_SUBSCRIPTION_PLAN_CODE).strip().lower()
    env_link = os.getenv(f"MP_SUBSCRIPTION_LINK_{normalized.upper()}", "").strip()
    checkout_url = MP_SUBSCRIPTION_LINK_MAP.get(normalized) or env_link
    if not checkout_url:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No existe un link configurado para '{normalized}'. "
                "Define MP_SUBSCRIPTION_LINK_MAP o MP_SUBSCRIPTION_LINK_<PLAN_CODE>."
            ),
        )
    return normalized, checkout_url


def resolve_subscription_checkout_config(plan_code: str):
    normalized, plan_id = resolve_subscription_plan(plan_code)
    _, checkout_url = resolve_subscription_link(normalized)
    return normalized, plan_id, checkout_url


def build_subscription_reason(plan_code: str):
    return f"{APP_NAME} suscripcion {plan_code.replace('_', ' ')}".strip()


def create_mp_subscription(email: str, plan_code: str, external_reference: str):
    normalized_plan_code, plan_id = resolve_subscription_plan(plan_code)
    payload = {
        "preapproval_plan_id": plan_id,
        "reason": build_subscription_reason(normalized_plan_code),
        "external_reference": external_reference,
        "payer_email": email,
        "back_url": f"{SITE_BASE_URL}/subscriptions/return",
        "status": "pending",
    }
    return normalized_plan_code, plan_id, mp_request("POST", "https://api.mercadopago.com/preapproval", payload)


def create_checkout_order(email: str, plan_code: str = DEFAULT_SUBSCRIPTION_PLAN_CODE):
    order_id = uuid.uuid4().hex
    now = utc_now()
    conn = db()
    conn.execute(
        sql("""
        INSERT INTO orders (order_id, email, plan_code, amount, currency, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """),
        (order_id, email, plan_code, PRICE_AMOUNT, PRICE_CURRENCY, now, now),
    )
    conn.commit()
    conn.close()

    preference = create_mp_preference(order_id, email, plan_code)
    conn = db()
    conn.execute(
        sql("UPDATE orders SET preference_id = ?, updated_at = ? WHERE order_id = ?"),
        (preference.get("id"), utc_now(), order_id),
    )
    conn.commit()
    conn.close()
    return {
        "order_id": order_id,
        "checkout_url": preference.get("init_point"),
        "sandbox_checkout_url": preference.get("sandbox_init_point"),
        "preference_id": preference.get("id"),
    }


def get_payment(payment_id: str):
    return mp_request("GET", f"https://api.mercadopago.com/v1/payments/{payment_id}")


def get_subscription(subscription_id: str):
    return mp_request("GET", f"https://api.mercadopago.com/preapproval/{subscription_id}")


def get_authorized_payment(authorized_payment_id: str):
    return mp_request("GET", f"https://api.mercadopago.com/authorized_payments/{authorized_payment_id}")


def search_subscriptions(payer_email: str, plan_id: str = ""):
    params = {"payer_email": payer_email}
    if plan_id:
        params["preapproval_plan_id"] = plan_id
    return mp_request("GET", "https://api.mercadopago.com/preapproval/search", params=params)


def validate_webhook_signature(x_signature: str, x_request_id: str, data_id: str):
    if not MP_WEBHOOK_SECRET:
        return True
    if not x_signature or not x_request_id or not data_id:
        return False

    parts = {}
    for item in x_signature.split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            parts[key.strip()] = value.strip()
    ts = parts.get("ts")
    received = parts.get("v1")
    if not ts or not received:
        return False

    manifest = f"id:{str(data_id).lower()};request-id:{x_request_id};ts:{ts};"
    generated = hmac.new(MP_WEBHOOK_SECRET.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(generated, received)


def ensure_license_for_paid_order(order_id: str, email: str):
    conn = db()
    row = conn.execute(sql("SELECT license_key FROM orders WHERE order_id = ?"), (order_id,)).fetchone()
    if row and row["license_key"]:
        conn.close()
        return row["license_key"]

    license_key = new_license_key()
    now = utc_now()
    conn.execute(
        sql("""
        INSERT INTO licenses (license_key, email, order_id, status, activations_json, created_at, updated_at)
        VALUES (?, ?, ?, 'active', '[]', ?, ?)
        """),
        (license_key, email, order_id, now, now),
    )
    conn.execute(sql("UPDATE orders SET license_key = ?, updated_at = ? WHERE order_id = ?"), (license_key, now, order_id))
    conn.commit()
    conn.close()
    return license_key


def sync_license_with_subscription(subscription_id: str, email: str, status: str):
    if not subscription_id or not email:
        return None

    conn = db()
    row = conn.execute(sql("SELECT * FROM licenses WHERE subscription_id = ?"), (subscription_id,)).fetchone()
    now = utc_now()
    desired_status = "active" if subscription_grants_access(status) else "inactive"

    if row:
        conn.execute(
            sql("UPDATE licenses SET status = ?, updated_at = ? WHERE subscription_id = ?"),
            (desired_status, now, subscription_id),
        )
        if desired_status == "active" and not row["email"]:
            conn.execute(
                sql("UPDATE licenses SET email = ?, updated_at = ? WHERE subscription_id = ?"),
                (email, now, subscription_id),
            )
        conn.commit()
        conn.close()
        return row["license_key"]

    if desired_status != "active":
        conn.close()
        return None

    license_key = new_license_key()
    conn.execute(
        sql("""
        INSERT INTO licenses (license_key, email, order_id, status, activations_json, created_at, updated_at, subscription_id)
        VALUES (?, ?, ?, 'active', '[]', ?, ?, ?)
        """),
        (license_key, email, subscription_id, now, now, subscription_id),
    )
    conn.execute(
        sql("UPDATE subscriptions SET license_key = ?, updated_at = ? WHERE subscription_id = ?"),
        (license_key, now, subscription_id),
    )
    conn.commit()
    conn.close()
    return license_key


def upsert_subscription_record(subscription_data, fallback_email: str = "", fallback_plan_code: str = ""):
    subscription_id = str(subscription_data.get("id") or "").strip()
    if not subscription_id:
        raise HTTPException(status_code=502, detail="Mercado Pago no devolvio un ID de suscripcion.")

    conn = db()
    existing = conn.execute(sql("SELECT * FROM subscriptions WHERE subscription_id = ?"), (subscription_id,)).fetchone()
    email = (
        str(subscription_data.get("payer_email") or "").strip()
        or fallback_email
        or (existing["email"] if existing else "")
    )
    plan_id = (
        str(subscription_data.get("preapproval_plan_id") or "").strip()
        or (existing["plan_id"] if existing else "")
    )
    plan_code = fallback_plan_code or (existing["plan_code"] if existing else "")
    reason = (
        str(subscription_data.get("reason") or "").strip()
        or (existing["reason"] if existing else build_subscription_reason(plan_code or "plan"))
    )
    status = str(subscription_data.get("status") or "pending").strip().lower()
    external_reference = (
        str(subscription_data.get("external_reference") or "").strip()
        or (existing["external_reference"] if existing else "")
    )
    init_point = (
        str(subscription_data.get("init_point") or "").strip()
        or (existing["init_point"] if existing else "")
    )
    payer_id = str(subscription_data.get("payer_id") or "").strip()
    next_payment_date = str(subscription_data.get("next_payment_date") or "").strip()
    license_key = existing["license_key"] if existing else None
    details_json = json.dumps(subscription_data)
    now = utc_now()

    if existing:
        conn.execute(
            sql("""
            UPDATE subscriptions
            SET external_reference = ?, email = ?, plan_code = ?, plan_id = ?, reason = ?, status = ?,
                init_point = ?, payer_id = ?, next_payment_date = ?, details_json = ?, updated_at = ?
            WHERE subscription_id = ?
            """),
            (
                external_reference,
                email,
                plan_code,
                plan_id,
                reason,
                status,
                init_point,
                payer_id,
                next_payment_date,
                details_json,
                now,
                subscription_id,
            ),
        )
    else:
        conn.execute(
            sql("""
            INSERT INTO subscriptions (
                subscription_id, external_reference, email, plan_code, plan_id, reason, status, init_point,
                payer_id, next_payment_date, last_payment_id, license_key, details_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)
            """),
            (
                subscription_id,
                external_reference,
                email,
                plan_code,
                plan_id,
                reason,
                status,
                init_point,
                payer_id,
                next_payment_date,
                details_json,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()

    license_key = sync_license_with_subscription(subscription_id, email, status) or license_key
    if license_key:
        conn = db()
        conn.execute(
            sql("UPDATE subscriptions SET license_key = ?, updated_at = ? WHERE subscription_id = ?"),
            (license_key, utc_now(), subscription_id),
        )
        conn.commit()
        conn.close()

    return fetch_subscription(subscription_id)


def create_subscription_checkout(email: str, plan_code: str, external_reference: str = ""):
    local_reference = external_reference.strip() or uuid.uuid4().hex
    normalized_plan_code, _, subscription = create_mp_subscription(email, plan_code, local_reference)
    row = upsert_subscription_record(subscription, fallback_email=email, fallback_plan_code=normalized_plan_code)
    checkout_url = row["init_point"] if row else subscription.get("init_point")
    if not checkout_url:
        raise HTTPException(status_code=502, detail="Mercado Pago no devolvio una URL de suscripcion.")
    return {
        "subscription_id": row["subscription_id"],
        "external_reference": row["external_reference"],
        "plan_code": row["plan_code"],
        "status": row["status"],
        "checkout_url": checkout_url,
        "license_key": row["license_key"],
    }


def sync_subscription_state(subscription_id: str):
    subscription = get_subscription(subscription_id)
    return upsert_subscription_record(subscription)


def choose_latest_subscription(results):
    if not results:
        return None

    def sort_key(item):
        return (
            str(item.get("last_modified") or ""),
            str(item.get("date_last_updated") or ""),
            str(item.get("date_created") or ""),
            str(item.get("id") or ""),
        )

    return sorted(results, key=sort_key, reverse=True)[0]


def resolve_subscription_from_plan_link(email: str, plan_code: str):
    normalized_plan_code, plan_id = resolve_subscription_plan(plan_code)
    result = search_subscriptions(email, plan_id)
    subscription = choose_latest_subscription(result.get("results", []))
    if not subscription:
        return {
            "found": False,
            "subscription_id": "",
            "email": email,
            "plan_code": normalized_plan_code,
            "status": "not_found",
            "license_key": None,
            "message": "Todavia no aparece una suscripcion para ese correo y plan.",
        }

    row = upsert_subscription_record(subscription, fallback_email=email, fallback_plan_code=normalized_plan_code)
    current_status = row["status"]
    return {
        "found": True,
        "subscription_id": row["subscription_id"],
        "email": row["email"],
        "plan_code": row["plan_code"],
        "status": current_status,
        "license_key": row["license_key"] if subscription_grants_access(current_status) else None,
        "next_payment_date": row["next_payment_date"],
    }


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": utc_now()}


@app.get("/", response_class=HTMLResponse)
def storefront_home():
    body = f"""
      <section class="hero">
        <div class="card">
          <div class="brand"><span class="dot"></span>{escape(APP_NAME)}</div>
          <div class="badge">Licencia vitalicia y activacion inmediata</div>
          <h1>Organiza tus archivos sin perder tiempo.</h1>
          <p>Compra la licencia de {escape(APP_NAME)}, paga en Mercado Pago y activa la app en tu equipo con tu correo y tu clave.</p>
          <div class="price">
            <strong>{escape(PRICE_AMOUNT)} {escape(PRICE_CURRENCY)}</strong>
            <span>pago unico</span>
          </div>
          <p>Ideal para vender tu app de escritorio sin meter credenciales sensibles dentro del ejecutable.</p>
          <div class="grid">
            <div class="feature">
              <h3>Compra guiada</h3>
              <p>El cliente escribe su correo, va al checkout y vuelve con una confirmacion clara.</p>
            </div>
            <div class="feature">
              <h3>Licencia automatica</h3>
              <p>Cuando Mercado Pago confirma el pago, el backend emite la licencia y la deja lista para activar.</p>
            </div>
            <div class="feature">
              <h3>Validacion por equipo</h3>
              <p>La licencia se activa por dispositivo y el escritorio puede verificar el estado cuando lo necesite.</p>
            </div>
          </div>
        </div>
        <div class="card">
          <h2>Comprar ahora</h2>
          <p>Ingresa el correo del comprador para abrir el checkout de Mercado Pago.</p>
          <form class="form" method="post" action="/buy">
            <div>
              <label for="email">Correo del comprador</label>
              <input id="email" type="email" name="email" placeholder="nombre@correo.com" required>
            </div>
            <button type="submit">Ir a Mercado Pago</button>
          </form>
          <hr style="border:0;border-top:1px solid var(--line);margin:22px 0;">
          <h3>Suscripcion recurrente</h3>
          <p>Usa el mismo <strong>plan_code</strong> que configures en <strong>MP_SUBSCRIPTION_PLAN_MAP</strong>.</p>
          <form class="form" method="post" action="/subscribe">
            <div>
              <label for="subscription_email">Correo del suscriptor</label>
              <input id="subscription_email" type="email" name="email" placeholder="nombre@correo.com" required>
            </div>
            <div>
              <label for="plan_code">Codigo del plan</label>
              <input id="plan_code" type="text" name="plan_code" placeholder="monthly" value="monthly" required>
            </div>
            <button type="submit">Ir a suscripcion</button>
          </form>
          <div class="meta">
            <div>Webhook del backend: <strong>{escape(PUBLIC_BASE_URL)}/webhooks/mercadopago</strong></div>
            <div>Activaciones permitidas por licencia: <strong>{escape(LICENSE_ACTIVATIONS)}</strong></div>
            <div>Mapea tus planes con <strong>MP_SUBSCRIPTION_PLAN_MAP</strong> o <strong>MP_SUBSCRIPTION_PLAN_ID_*</strong>.</div>
            <div>Estado recomendado despues del pago: abrir la app y pulsar <strong>Verificar licencia</strong>.</div>
          </div>
        </div>
      </section>
    """
    return render_page(f"{APP_NAME} | Licencia", body)


@app.post("/buy")
def buy_redirect(email: EmailStr = Form(...)):
    checkout = create_checkout_order(str(email))
    checkout_url = checkout.get("checkout_url") or checkout.get("sandbox_checkout_url")
    if not checkout_url:
        raise HTTPException(status_code=502, detail="Mercado Pago no devolvio una URL de checkout.")
    return RedirectResponse(checkout_url, status_code=303)


@app.post("/subscribe")
def subscribe_redirect(email: EmailStr = Form(...), plan_code: str = Form(DEFAULT_SUBSCRIPTION_PLAN_CODE)):
    subscription = create_subscription_checkout(str(email), plan_code)
    return RedirectResponse(subscription["checkout_url"], status_code=303)


@app.post("/checkout/create")
def checkout_create(payload: CheckoutRequest):
    return create_checkout_order(payload.email, payload.plan_code)


@app.post("/subscriptions/create")
def subscriptions_create(payload: SubscriptionCheckoutRequest):
    return create_subscription_checkout(payload.email, payload.plan_code, payload.external_reference)


@app.get("/subscription-plans")
def subscription_plans():
    plan_codes = sorted(set(MP_SUBSCRIPTION_LINK_MAP.keys()) | set(MP_SUBSCRIPTION_PLAN_MAP.keys()))
    plans = []
    for plan_code in plan_codes:
        try:
            normalized, plan_id, checkout_url = resolve_subscription_checkout_config(plan_code)
        except HTTPException:
            continue
        plans.append(
            {
                "plan_code": normalized,
                "checkout_url": checkout_url,
                "plan_id": plan_id,
            }
        )
    default_plan_code = DEFAULT_SUBSCRIPTION_PLAN_CODE if any(
        plan["plan_code"] == DEFAULT_SUBSCRIPTION_PLAN_CODE for plan in plans
    ) else (plans[0]["plan_code"] if plans else DEFAULT_SUBSCRIPTION_PLAN_CODE)
    return {
        "default_plan_code": default_plan_code,
        "plans": plans,
    }


@app.post("/subscriptions/link")
def subscriptions_link(payload: SubscriptionLinkRequest):
    normalized_plan_code, _, checkout_url = resolve_subscription_checkout_config(payload.plan_code)
    return {
        "email": payload.email,
        "plan_code": normalized_plan_code,
        "checkout_url": checkout_url,
        "status": "pending",
    }


@app.get("/checkout/status/{order_id}")
def checkout_status(order_id: str):
    row = fetch_order(order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    return {"order_id": row["order_id"], "status": row["status"], "license_key": row["license_key"], "email": row["email"]}


@app.get("/subscriptions/status/{subscription_id}")
def subscription_status(subscription_id: str, sync: bool = True):
    row = sync_subscription_state(subscription_id) if sync else fetch_subscription(subscription_id)
    if not row:
        raise HTTPException(status_code=404, detail="Suscripcion no encontrada.")
    return {
        "subscription_id": row["subscription_id"],
        "status": row["status"],
        "email": row["email"],
        "plan_code": row["plan_code"],
        "license_key": row["license_key"],
        "next_payment_date": row["next_payment_date"],
        "external_reference": row["external_reference"],
    }


@app.post("/subscriptions/resolve")
def subscription_resolve(payload: SubscriptionLookupRequest):
    return resolve_subscription_from_plan_link(payload.email, payload.plan_code)


@app.post("/licenses/activate")
def activate_license(payload: LicenseRequest):
    conn = db()
    row = conn.execute(
        sql("SELECT * FROM licenses WHERE license_key = ? AND email = ?"),
        (payload.license_key, payload.email),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Licencia no encontrada para ese correo.")
    subscription = None
    if row["subscription_id"]:
        subscription = fetch_subscription(row["subscription_id"])
        if not subscription_grants_access(subscription["status"] if subscription else ""):
            conn.close()
            return {"status": "inactive", "message": "La suscripcion asociada no esta activa."}
    if row["status"] != "active":
        conn.close()
        return {"status": "inactive", "message": "La licencia no esta activa."}

    order = fetch_order_for_license(row)
    activations = json.loads(row["activations_json"])
    if payload.device_id not in activations:
        if len(activations) >= LICENSE_ACTIVATIONS:
            conn.close()
            return {"status": "inactive", "message": "La licencia ya alcanzo el limite de activaciones."}
        activations.append(payload.device_id)
        conn.execute(
            sql("UPDATE licenses SET activations_json = ?, updated_at = ? WHERE license_key = ?"),
            (json.dumps(activations), utc_now(), payload.license_key),
        )
        conn.commit()
    conn.close()
    return {
        "status": "active",
        "message": "Licencia activada correctamente.",
        "next_payment_date": subscription["next_payment_date"] if subscription else "",
        "subscription_id": subscription["subscription_id"] if subscription else "",
        "plan_code": subscription["plan_code"] if subscription else (order["plan_code"] if order else ""),
    }


@app.post("/licenses/validate")
def validate_license(payload: LicenseRequest):
    conn = db()
    row = conn.execute(
        sql("SELECT * FROM licenses WHERE license_key = ? AND email = ?"),
        (payload.license_key, payload.email),
    ).fetchone()
    conn.close()
    if not row:
        return {"status": "inactive", "message": "No existe una licencia para esos datos."}

    subscription = None
    if row["subscription_id"]:
        subscription = fetch_subscription(row["subscription_id"])
        if not subscription_grants_access(subscription["status"] if subscription else ""):
            return {"status": "inactive", "message": "La suscripcion asociada no esta activa."}

    activations = json.loads(row["activations_json"])
    if row["status"] == "active" and payload.device_id in activations:
        order = fetch_order_for_license(row)
        return {
            "status": "active",
            "message": "Licencia valida.",
            "next_payment_date": subscription["next_payment_date"] if subscription else "",
            "subscription_id": subscription["subscription_id"] if subscription else "",
            "plan_code": subscription["plan_code"] if subscription else (order["plan_code"] if order else ""),
        }
    return {"status": "inactive", "message": "Este dispositivo no esta activado."}


@app.post("/webhooks/mercadopago")
async def mercadopago_webhook(request: Request):
    body = await request.json()
    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")
    data_id = request.query_params.get("data.id") or str(body.get("data", {}).get("id", ""))

    if not validate_webhook_signature(x_signature, x_request_id, data_id):
        raise HTTPException(status_code=401, detail="Firma de webhook invalida.")

    topic = (
        request.query_params.get("topic")
        or request.query_params.get("type")
        or body.get("topic")
        or body.get("type")
        or ""
    ).strip()
    resource_id = str(body.get("data", {}).get("id") or data_id or "").strip()

    if topic == "payment":
        payment = get_payment(resource_id)
        order_id = payment.get("external_reference")
        email = payment.get("payer", {}).get("email")
        status = payment.get("status", "pending")
        payment_id = str(payment.get("id"))

        conn = db()
        order_row = fetch_order(str(order_id)) if order_id else None
        if order_row:
            conn.execute(
                sql("UPDATE orders SET payment_id = ?, status = ?, updated_at = ? WHERE order_id = ?"),
                (payment_id, status, utc_now(), order_id),
            )
        subscription_row = None
        if payment.get("metadata", {}).get("subscription_id"):
            subscription_row = fetch_subscription(str(payment["metadata"]["subscription_id"]))
        elif order_id:
            subscription_row = fetch_subscription_by_external_reference(str(order_id))
        if subscription_row:
            conn.execute(
                sql("UPDATE subscriptions SET last_payment_id = ?, updated_at = ? WHERE subscription_id = ?"),
                (payment_id, utc_now(), subscription_row["subscription_id"]),
            )
        conn.commit()
        conn.close()

        license_key = None
        if order_row and status == "approved" and order_id and email:
            license_key = ensure_license_for_paid_order(order_id, email)

        return {"received": True, "topic": topic, "status": status, "order_id": order_id, "license_key": license_key}

    if topic == "subscription_preapproval":
        subscription_row = sync_subscription_state(resource_id)
        return {
            "received": True,
            "topic": topic,
            "subscription_id": subscription_row["subscription_id"],
            "status": subscription_row["status"],
            "license_key": subscription_row["license_key"],
        }

    if topic == "subscription_authorized_payment":
        authorized_payment = get_authorized_payment(resource_id)
        subscription_id = str(authorized_payment.get("preapproval_id") or "").strip()
        subscription_row = sync_subscription_state(subscription_id) if subscription_id else None

        if subscription_row:
            conn = db()
            conn.execute(
                sql("UPDATE subscriptions SET last_payment_id = ?, updated_at = ? WHERE subscription_id = ?"),
                (str(authorized_payment.get("payment", {}).get("id") or authorized_payment.get("id") or ""), utc_now(), subscription_id),
            )
            conn.commit()
            conn.close()

        return {
            "received": True,
            "topic": topic,
            "subscription_id": subscription_id,
            "status": subscription_row["status"] if subscription_row else authorized_payment.get("status", ""),
            "payment_status": authorized_payment.get("payment", {}).get("status", ""),
            "license_key": subscription_row["license_key"] if subscription_row else None,
        }

    if topic == "subscription_preapproval_plan":
        return {"received": True, "topic": topic, "ignored": True}

    return {"received": True, "topic": topic or "unknown", "ignored": True}


def render_checkout_result(kind: str, title: str, message: str, order_id: str = "", payment_id: str = ""):
    row = fetch_order(order_id) if order_id else None
    license_key = row["license_key"] if row and row["license_key"] else ""
    email = row["email"] if row and row["email"] else ""
    status = row["status"] if row and row["status"] else kind
    status_class = {"approved": "ok", "pending": "warn", "failure": "danger"}.get(kind, "warn")

    details = []
    if order_id:
        details.append(f"<div><strong>Pedido:</strong> {escape(order_id)}</div>")
    if payment_id:
        details.append(f"<div><strong>Pago:</strong> {escape(payment_id)}</div>")
    if email:
        details.append(f"<div><strong>Correo:</strong> {escape(email)}</div>")
    details.append(f"<div><strong>Estado registrado:</strong> {escape(status)}</div>")
    if license_key:
        details.append(f"<div><strong>Licencia emitida:</strong> {escape(license_key)}</div>")

    body = f"""
      <section class="panel card">
        <div class="brand"><span class="dot"></span>{escape(APP_NAME)}</div>
        <div class="status {status_class}">{escape(title)}</div>
        <h2>{escape(title)}</h2>
        <p>{escape(message)}</p>
        <div class="code">
          {''.join(details)}
        </div>
        <div class="actions">
          <a class="button" href="{escape(SITE_BASE_URL)}/">Volver al sitio</a>
          <a class="button secondary" href="{escape(PUBLIC_BASE_URL)}/docs">Ver API</a>
        </div>
      </section>
    """
    return render_page(title, body)


def render_subscription_result(kind: str, title: str, message: str, subscription_id: str = ""):
    row = fetch_subscription(subscription_id) if subscription_id else None
    status = row["status"] if row else kind
    status_class = {"authorized": "ok", "active": "ok", "pending": "warn", "paused": "warn", "cancelled": "danger", "canceled": "danger"}.get(status, "warn")

    details = []
    if row:
        details.append(f"<div><strong>Suscripcion:</strong> {escape(row['subscription_id'])}</div>")
        details.append(f"<div><strong>Correo:</strong> {escape(row['email'])}</div>")
        details.append(f"<div><strong>Plan:</strong> {escape(row['plan_code'])}</div>")
        details.append(f"<div><strong>Estado registrado:</strong> {escape(row['status'])}</div>")
        if row["license_key"]:
            details.append(f"<div><strong>Licencia activa:</strong> {escape(row['license_key'])}</div>")
        if row["next_payment_date"]:
            details.append(f"<div><strong>Proximo cobro:</strong> {escape(row['next_payment_date'])}</div>")

    body = f"""
      <section class="panel card">
        <div class="brand"><span class="dot"></span>{escape(APP_NAME)}</div>
        <div class="status {status_class}">{escape(title)}</div>
        <h2>{escape(title)}</h2>
        <p>{escape(message)}</p>
        <div class="code">
          {''.join(details) or '<div>La suscripcion se esta sincronizando.</div>'}
        </div>
        <div class="actions">
          <a class="button" href="{escape(SITE_BASE_URL)}/">Volver al sitio</a>
          <a class="button secondary" href="{escape(PUBLIC_BASE_URL)}/docs">Ver API</a>
        </div>
      </section>
    """
    return render_page(title, body)


@app.get("/checkout/return/success", response_class=HTMLResponse)
def checkout_return_success(external_reference: str = "", payment_id: str = "", status: str = ""):
    order_id = external_reference.strip()
    message = "Pago aprobado. Ya puedes volver a la app y usar 'Verificar licencia' para completar la activacion."
    if status and status.lower() != "approved":
        message = "Mercado Pago te redirigio aqui, pero el estado final aun puede estar sincronizandose."
    return render_checkout_result("approved", "Pago aprobado", message, order_id=order_id, payment_id=payment_id)


@app.get("/checkout/return/pending", response_class=HTMLResponse)
def checkout_return_pending(external_reference: str = "", payment_id: str = ""):
    return render_checkout_result(
        "pending",
        "Pago pendiente",
        "Tu pago sigue en proceso. Vuelve mas tarde a la app y usa 'Verificar licencia'.",
        order_id=external_reference.strip(),
        payment_id=payment_id,
    )


@app.get("/checkout/return/failure", response_class=HTMLResponse)
def checkout_return_failure(external_reference: str = "", payment_id: str = ""):
    return render_checkout_result(
        "failure",
        "Pago no completado",
        "El cobro no se completo. Puedes intentarlo otra vez desde la app o desde esta pagina.",
        order_id=external_reference.strip(),
        payment_id=payment_id,
    )


@app.get("/subscriptions/return", response_class=HTMLResponse)
def subscriptions_return(preapproval_id: str = "", status: str = ""):
    subscription_id = preapproval_id.strip()
    if subscription_id:
        try:
            sync_subscription_state(subscription_id)
        except HTTPException:
            pass

    normalized_status = status.strip().lower() or "pending"
    if normalized_status in SUBSCRIPTION_ACTIVE_STATUSES:
        return render_subscription_result(
            normalized_status,
            "Suscripcion activa",
            "La suscripcion quedo autorizada. Ya puedes volver a la app y validar tu licencia.",
            subscription_id=subscription_id,
        )
    if normalized_status in {"cancelled", "canceled"}:
        return render_subscription_result(
            normalized_status,
            "Suscripcion cancelada",
            "La suscripcion fue cancelada. Si necesitas acceso otra vez, inicia una nueva suscripcion.",
            subscription_id=subscription_id,
        )
    return render_subscription_result(
        normalized_status,
        "Suscripcion en proceso",
        "Mercado Pago esta terminando de sincronizar el estado. Vuelve a la app y consulta el estado en unos segundos.",
        subscription_id=subscription_id,
    )
