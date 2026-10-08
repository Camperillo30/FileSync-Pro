# Pruebas del cliente de licencias por código (`MakeCodeLicenseClient` en Organizador.py).
# Levantan un servidor HTTP local que imita el escenario "CipherVault - Canje de codigo" de Make
# (mismo contrato: hash del código, un código = un equipo, respuesta firmada con sha256).
import datetime as dt
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SECRET = "secreto-de-prueba"
EMAIL = "cliente@example.com"
CODE = "ABCD1234EFGH5678"
CODE_SHOWN = "abcd-1234-efgh-5678"  # Como lo copia el cliente del correo.
MACHINE_A = "a" * 64
MACHINE_B = "b" * 64


def _hash(code, email):
    return hashlib.sha256(f"{code}|{email}".encode()).hexdigest()


class FakeMake:
    """Imita el escenario de canje de Make con un diccionario como data store."""

    def __init__(self):
        self.store = {}
        self.requests = 0
        self.tamper_signature = False
        self.server = None

    def add_license(self, code, email, plan="fs-basica", created_at="2026-10-02T18:00:00", meses=1):
        self.store[_hash(code, email)] = {
            "plan": plan, "reference": "i8Erwh-TEST", "meses": meses,
            "created_at": created_at, "machine": "", "used_at": "",
        }

    def handle(self, payload):
        self.requests += 1
        key, machine, nonce = payload.get("code_hash"), payload.get("machine", ""), payload.get("nonce", "")
        record = self.store.get(key)
        if record is None or not machine:
            return {"ok": False, "error": "invalid"}
        if record["machine"] == "":
            record["machine"] = machine
        elif record["machine"] != machine:
            return {"ok": False, "error": "used"}
        fields = [key, machine, nonce, record["plan"], record["reference"], str(record["meses"]), record["created_at"]]
        sig = hashlib.sha256(("|".join(fields) + "|" + SECRET).encode()).hexdigest()
        if self.tamper_signature:
            sig = "0" * 64
        return {
            "ok": True, "plan": record["plan"], "reference": record["reference"],
            "meses": str(record["meses"]), "created_at": record["created_at"], "sig": sig,
        }

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.dumps(outer.handle(json.loads(self.rfile.read(length)))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}/"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def make():
    fake = FakeMake()
    fake.url = fake.start()
    fake.add_license(CODE, EMAIL)
    yield fake
    fake.stop()


@pytest.fixture()
def licenses():
    pytest.importorskip("tkinter")
    import Organizador

    return Organizador


def _client(licenses, make, state=None):
    provider = (lambda: state) if state is not None else None
    return licenses.MakeCodeLicenseClient(make.url, SECRET, state_provider=provider)


def test_activate_binds_code_and_returns_expiration(licenses, make):
    result = _client(licenses, make).activate_license(EMAIL, CODE_SHOWN, MACHINE_A)
    assert result["status"] == "active"
    assert result["plan_code"] == "basica"
    assert result["license_key"] == CODE
    # created_at (hora de Bogotá, UTC-5) + 30 días.
    assert result["next_payment_date"] == "2026-11-01T18:00:00-05:00"
    assert make.store[_hash(CODE, EMAIL)]["machine"] == MACHINE_A


def test_email_is_case_and_space_insensitive(licenses, make):
    assert _client(licenses, make).activate_license("  Cliente@Example.COM ", CODE, MACHINE_A)["status"] == "active"


def test_same_machine_can_revalidate_but_other_machine_cannot(licenses, make):
    client = _client(licenses, make)
    assert client.activate_license(EMAIL, CODE, MACHINE_A)["status"] == "active"
    assert client.validate_license(EMAIL, CODE, MACHINE_A, force=True)["status"] == "active"
    other = client.activate_license(EMAIL, CODE, MACHINE_B)
    assert other["status"] == "inactive"
    assert "otro equipo" in other["message"]


def test_wrong_email_or_wrong_code_is_invalid(licenses, make):
    client = _client(licenses, make)
    assert client.activate_license("otro@example.com", CODE, MACHINE_A)["status"] == "inactive"
    assert "no es válido" in client.activate_license(EMAIL, "ZZZZ1234EFGH5678", MACHINE_A)["message"]


def test_malformed_code_does_not_hit_make(licenses, make):
    client = _client(licenses, make)
    assert client.activate_license(EMAIL, "123", MACHINE_A)["status"] == "inactive"
    assert make.requests == 0


def test_tampered_signature_is_rejected(licenses, make):
    make.tamper_signature = True
    with pytest.raises(licenses.LicensingError):
        _client(licenses, make).activate_license(EMAIL, CODE, MACHINE_A)


def test_wrong_signing_secret_is_rejected(licenses, make):
    client = licenses.MakeCodeLicenseClient(make.url, "otro-secreto")
    with pytest.raises(licenses.LicensingError):
        client.activate_license(EMAIL, CODE, MACHINE_A)


def test_cipher_vault_code_is_not_valid_for_filesync(licenses, make):
    cv_code = "1111222233334444"
    make.add_license(cv_code, EMAIL, plan="basic")
    result = _client(licenses, make).activate_license(EMAIL, cv_code, MACHINE_A)
    assert result["status"] == "inactive"
    assert "no corresponde a FileSync Pro" in result["message"]


def test_not_configured_raises(licenses):
    with pytest.raises(licenses.LicensingError):
        licenses.MakeCodeLicenseClient("", "").activate_license(EMAIL, CODE, MACHINE_A)


def test_resolve_subscription_always_asks_for_code(licenses, make):
    assert _client(licenses, make).resolve_subscription(EMAIL, "pro")["status"] == "activation_required"


def _state(days_left, checked_hours_ago=None):
    now = dt.datetime.now(dt.UTC)
    state = {
        "subscription_expires_at": (now + dt.timedelta(days=days_left)).isoformat(),
        "subscription_plan_code": "pro",
        "subscription_id": "code:abc",
    }
    if checked_hours_ago is not None:
        state["subscription_checked_at"] = (
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=checked_hours_ago)
        ).replace(tzinfo=None).isoformat()
    return state


def test_far_from_expiry_does_not_call_make(licenses, make):
    client = _client(licenses, make, _state(days_left=20))
    result = client.validate_license(EMAIL, CODE, MACHINE_A)
    assert result["status"] == "active" and result["plan_code"] == "pro"
    assert make.requests == 0


def test_force_always_calls_make(licenses, make):
    client = _client(licenses, make, _state(days_left=20))
    client.validate_license(EMAIL, CODE, MACHINE_A, force=True)
    assert make.requests == 1


def test_near_expiry_checks_make_but_not_more_than_twice_a_day(licenses, make):
    state = _state(days_left=1)
    client = _client(licenses, make, state)
    assert client.validate_license(EMAIL, CODE, MACHINE_A)["status"] == "active"
    assert make.requests == 1
    # Se anotó la consulta: la siguiente (al volver a la ventana) ya no sale a Make.
    assert client.validate_license(EMAIL, CODE, MACHINE_A)["status"] == "active"
    assert make.requests == 1
    # Pasadas 12 horas vuelve a consultar.
    state["subscription_checked_at"] = (dt.datetime.utcnow() - dt.timedelta(hours=13)).isoformat()
    client.validate_license(EMAIL, CODE, MACHINE_A)
    assert make.requests == 2


def test_expired_license_checks_make_and_picks_up_renewal(licenses, make):
    state = _state(days_left=-3, checked_hours_ago=24)
    client = _client(licenses, make, state)
    first = client.validate_license(EMAIL, CODE, MACHINE_A)
    assert first["status"] == "active"
    assert first["next_payment_date"] == "2026-11-01T18:00:00-05:00"
    # Make recibe un pago nuevo: actualiza created_at del mismo código.
    make.store[_hash(CODE, EMAIL)]["created_at"] = "2026-12-01T09:30:00"
    state["subscription_checked_at"] = (dt.datetime.utcnow() - dt.timedelta(hours=13)).isoformat()
    renewed = client.validate_license(EMAIL, CODE, MACHINE_A)
    assert renewed["next_payment_date"] == "2026-12-31T09:30:00-05:00"


def test_expiration_handles_bad_input(licenses):
    cls = licenses.MakeCodeLicenseClient
    assert cls.expiration_from("", 1) == ""
    assert cls.expiration_from("no es fecha", 1) == ""
    assert cls.expiration_from("2026-10-02T18:00:00", "x") == "2026-11-01T18:00:00-05:00"
    assert cls.expiration_from("2026-10-02T18:00:00", 2) == "2026-12-01T18:00:00-05:00"
