"""Private licensing API for FileSync Pro.

The desktop application never receives a payment-provider secret or a complete
license list.  Licenses are activated with a one-time code sent after payment
and are bound to a limited number of device identifiers.

Este es el backend (servidor) de licencias, separado de la app de escritorio
(Organizador.py). Es una API hecha con FastAPI que:

1. Emite códigos de activación para un correo y un plan, cuando lo pide un
   administrador autenticado (`/v1/licenses/issue`).
2. Permite que la app de escritorio active una licencia en un equipo
   concreto usando ese código (`/v1/licenses/activate`).
3. Permite validar si una licencia sigue activa en ese equipo
   (`/v1/licenses/validate`), por ejemplo cada vez que arranca la app.
4. Recibe notificaciones del proveedor de pagos (Wompi) por webhook, firmadas
   con una clave secreta compartida (`/v1/webhooks/payment`).

Toda la información se guarda en una base de datos SQLite muy simple con dos
tablas: `licenses` (una fila por licencia comprada) y `activations` (qué
equipos han activado cada licencia).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field


APP_NAME = "FileSync Pro Licensing API"
DEFAULT_MAX_ACTIVATIONS = 2  # Cuántos equipos distintos pueden usar una misma licencia.
VALID_PLANS = {"basica", "pro", "premium"}
# Ruta del archivo SQLite; se puede sobrescribir con la variable de entorno
# FILESYNC_PRO_LICENSE_DB (útil para pruebas o para separar entornos).
DB_PATH = Path(os.getenv("FILESYNC_PRO_LICENSE_DB", "filesync_licenses.db"))
# Clave que debe enviar un administrador para poder emitir licencias.
ADMIN_API_KEY = os.getenv("FILESYNC_PRO_ADMIN_API_KEY", "")
# Clave secreta compartida con el proveedor de pagos para firmar los webhooks.
WEBHOOK_SECRET = os.getenv("FILESYNC_PRO_WEBHOOK_SECRET", "")


def utcnow() -> datetime:
    """Hora actual en UTC, con zona horaria explícita (evita ambigüedades
    al comparar fechas guardadas en la base de datos)."""
    return datetime.now(UTC)


def hash_code(code: str) -> str:
    """Convierte un código de activación en su huella SHA-256.

    Nunca se guarda el código de activación en texto plano en la base de
    datos: solo se guarda este hash. Así, aunque alguien accediera a la
    base de datos, no podría recuperar los códigos originales.
    """
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    """Abre una conexión a SQLite para usar con `with database() as conn:`.

    `row_factory = sqlite3.Row` permite acceder a las columnas por nombre
    (`row["email"]`) en vez de por posición. Al salir del bloque `with` sin
    errores se hace `commit()` automáticamente, y la conexión siempre se
    cierra al final (haya o no error), gracias al `finally`.
    """
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_database() -> None:
    """Crea las tablas si todavía no existen (no borra datos si ya están).

    - `licenses`: una fila por licencia vendida, con su plan, el hash del
      código de activación, si está activa/cancelada y cuándo expira.
    - `activations`: registra en qué dispositivos se ha activado cada
      licencia, para poder aplicar el límite `max_activations`.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS licenses (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                plan_code TEXT NOT NULL,
                activation_code_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                max_activations INTEGER NOT NULL DEFAULT 2,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS licenses_email_idx ON licenses(email);
            CREATE TABLE IF NOT EXISTS activations (
                license_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (license_id, device_id),
                FOREIGN KEY (license_id) REFERENCES licenses(id)
            );
            """
        )


# --- Modelos de entrada (Pydantic valida automáticamente el JSON recibido) ---

class ActivationRequest(BaseModel):
    """Cuerpo esperado en POST /v1/licenses/activate."""

    email: EmailStr
    activation_code: str = Field(min_length=12, max_length=200)
    device_id: str = Field(min_length=32, max_length=128)


class ValidationRequest(BaseModel):
    """Cuerpo esperado en POST /v1/licenses/validate."""

    email: EmailStr
    license_key: str = Field(min_length=12, max_length=200)
    device_id: str = Field(min_length=32, max_length=128)


class IssueLicenseRequest(BaseModel):
    """Cuerpo esperado en POST /v1/licenses/issue (solo administrador)."""

    email: EmailStr
    plan_code: str
    expires_at: datetime | None = None
    max_activations: int = Field(default=DEFAULT_MAX_ACTIVATIONS, ge=1, le=10)


def serialize_license(row: sqlite3.Row) -> dict[str, str]:
    """Convierte una fila de la tabla `licenses` en el formato que espera
    la app de escritorio (la misma forma que usan las respuestas de
    activate/validate)."""
    return {
        "status": "active",
        "email": row["email"],
        "plan_code": row["plan_code"],
        "subscription_id": row["id"],
        "next_payment_date": row["expires_at"] or "",
    }


def active_license(email: str, activation_code: str) -> sqlite3.Row | None:
    """Busca una licencia que coincida con el correo y el código dados, y
    que además esté activa y no expirada. Devuelve `None` si no aplica
    ninguna de esas condiciones, para que quien llama solo tenga que
    comprobar si el resultado es `None`."""
    with database() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE email = ? AND activation_code_hash = ?",
            (email.lower(), hash_code(activation_code)),
        ).fetchone()
    if row is None or row["status"] != "active":
        return None
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= utcnow():
        return None
    return row


def require_admin(x_admin_key: str = Header(default="")) -> None:
    """Dependencia de FastAPI que protege los endpoints de administrador.

    Compara la cabecera `X-Admin-Key` recibida con `ADMIN_API_KEY` usando
    `hmac.compare_digest`, que compara en tiempo constante para evitar que
    un atacante pueda adivinar la clave midiendo cuánto tarda la respuesta
    (ataque de temporización).
    """
    if not ADMIN_API_KEY or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


app = FastAPI(title=APP_NAME, version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    """Se ejecuta una vez cuando arranca el servidor: asegura que la base
    de datos y sus tablas existan antes de recibir peticiones."""
    init_database()


@app.get("/health")
def health() -> dict[str, str]:
    """Endpoint simple para comprobar que el servidor está vivo (útil para
    monitoreo o para que la plataforma de despliegue verifique el servicio)."""
    return {"status": "ok"}


@app.post("/v1/licenses/issue", dependencies=[Depends(require_admin)])
def issue_license(payload: IssueLicenseRequest) -> dict[str, str]:
    """Crea una licencia nueva (solo accesible con la clave de administrador).

    Genera un identificador de licencia y un código de activación aleatorios
    y seguros (`secrets.token_urlsafe`), guarda solo el *hash* del código en
    la base de datos, y devuelve el código en texto plano una única vez para
    que el flujo de compra se lo envíe al comprador.
    """
    plan_code = payload.plan_code.strip().lower()
    if plan_code not in VALID_PLANS:
        raise HTTPException(status_code=422, detail="Unknown plan")
    license_id = secrets.token_urlsafe(18)
    activation_code = "FS-" + secrets.token_urlsafe(24)
    expires_at = payload.expires_at.astimezone(UTC).isoformat() if payload.expires_at else ""
    with database() as conn:
        conn.execute(
            "INSERT INTO licenses VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (license_id, payload.email.lower(), plan_code, hash_code(activation_code), expires_at, payload.max_activations, utcnow().isoformat()),
        )
    # Send this code to the purchaser through the payment fulfilment workflow;
    # it is intentionally returned only to the authenticated administrator.
    return {"license_id": license_id, "activation_code": activation_code}


@app.post("/v1/licenses/activate")
def activate_license(payload: ActivationRequest) -> dict[str, str]:
    """Activa una licencia en un dispositivo concreto.

    Si el dispositivo ya estaba activado, solo se actualiza `last_seen_at`
    (para saber que sigue en uso). Si es un dispositivo nuevo, se comprueba
    que no se haya superado el límite `max_activations` antes de registrarlo.
    """
    row = active_license(payload.email, payload.activation_code)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired activation code")
    with database() as conn:
        existing = conn.execute(
            "SELECT 1 FROM activations WHERE license_id = ? AND device_id = ?", (row["id"], payload.device_id)
        ).fetchone()
        if existing is None:
            count = conn.execute("SELECT COUNT(*) FROM activations WHERE license_id = ?", (row["id"],)).fetchone()[0]
            if count >= row["max_activations"]:
                raise HTTPException(status_code=409, detail="Activation limit reached")
            conn.execute(
                "INSERT INTO activations VALUES (?, ?, ?, ?)", (row["id"], payload.device_id, utcnow().isoformat(), utcnow().isoformat())
            )
        else:
            conn.execute("UPDATE activations SET last_seen_at = ? WHERE license_id = ? AND device_id = ?", (utcnow().isoformat(), row["id"], payload.device_id))
    result = serialize_license(row)
    result["license_key"] = payload.activation_code
    return result


@app.post("/v1/licenses/validate")
def validate_license(payload: ValidationRequest) -> dict[str, str]:
    """Comprueba si una licencia sigue activa en un dispositivo dado.

    A diferencia de `activate_license`, aquí NO se registra un dispositivo
    nuevo: si el dispositivo no aparece en `activations`, se responde
    `inactive`. Esto es lo que la app de escritorio llama periódicamente
    para confirmar que la licencia sigue vigente.
    """
    row = active_license(payload.email, payload.license_key)
    if row is None:
        return {"status": "inactive"}
    with database() as conn:
        activation = conn.execute(
            "SELECT 1 FROM activations WHERE license_id = ? AND device_id = ?", (row["id"], payload.device_id)
        ).fetchone()
        if activation is None:
            return {"status": "inactive", "message": "License is not activated on this device"}
        conn.execute("UPDATE activations SET last_seen_at = ? WHERE license_id = ? AND device_id = ?", (utcnow().isoformat(), row["id"], payload.device_id))
    result = serialize_license(row)
    result["license_key"] = payload.license_key
    return result


@app.post("/v1/webhooks/payment")
async def payment_webhook(request: Request, x_webhook_signature: str = Header(default="")) -> dict[str, str]:
    """Verify the provider's HMAC before handing the event to fulfilment code."""
    body = await request.body()
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest() if WEBHOOK_SECRET else ""
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_webhook_signature, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    # Provider-specific fulfilment belongs here: validate the event with the
    # provider API, then issue the code through the authenticated admin flow.
    return {"status": "accepted"}
