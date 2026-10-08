"""Prueba de punta a punta del flujo de licencias por código (Wompi -> Make -> correo -> canje).

Qué hace, en orden:
1. Simula un pago APROBADO de Wompi (firmado con tu secreto de eventos) hacia el webhook de pagos de Make.
   Make crea la licencia y te manda un correo con el código.
2. Te pide ese código y lo canjea en un "equipo A" (debe salir bien y la firma debe verificar).
3. Intenta canjearlo en un "equipo B" (debe rechazarse: un código = un equipo).
4. Simula un SEGUNDO pago del mismo correo (renovación) y vuelve a consultar: la fecha debe avanzar y NO
   debe llegar un código nuevo.
5. Imprime las llaves de prueba que quedan en el data store de Make para que las borres.

Uso (PowerShell, desde la carpeta del proyecto):

    $env:WOMPI_EVENTS_SECRET = "prod_events_..."            # Secreto de eventos de Wompi
    $env:FILESYNC_PRO_LICENSE_SIGNING_SECRET = "..."         # El mismo que pones en .env
    python tools\\probar_licencias_make.py --email tu_correo@gmail.com --plan pro

Usa un correo tuyo: Make le enviará el correo del código (y el de renovación).
"""

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from urllib import error, request

PAYMENT_HOOK = os.getenv(
    "FILESYNC_PRO_PAYMENT_HOOK_URL", "https://hook.us2.make.com/jni71fawwtsudjo58qm82jy8l3hlr5un"
)
REDEEM_HOOK = os.getenv(
    "FILESYNC_PRO_LICENSE_REDEEM_URL", "https://hook.us2.make.com/vm9homuf7aq7s6fbve9hdm55xub8o6gt"
)
# Identificadores de los enlaces de pago de Wompi (los mismos que usa el escenario de Make para elegir plan).
PLAN_LINK_IDS = {"basica": "i8Erwh", "pro": "SYVQ0M", "premium": "HQB1kI"}


def post_json(url, payload, timeout=40):
    req = request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "FileSync-Pro-test/1"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, body
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def send_payment(email, plan, events_secret):
    """Simula el webhook de Wompi de una transacción aprobada, firmado como lo exige Make."""
    tx_id = f"TEST-FS-{uuid.uuid4().hex[:10]}"
    reference = f"{PLAN_LINK_IDS[plan]}-TEST-{uuid.uuid4().hex[:6]}"
    status, amount, timestamp = "APPROVED", 1890000, int(time.time())
    checksum = hashlib.sha256(f"{tx_id}{status}{amount}{timestamp}{events_secret}".encode()).hexdigest()
    payload = {
        "event": "transaction.updated",
        "data": {"transaction": {
            "id": tx_id, "status": status, "amount_in_cents": amount, "reference": reference,
            "customer_email": email, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        }},
        "timestamp": timestamp,
        "signature": {"properties": ["transaction.id", "transaction.status", "transaction.amount_in_cents"],
                      "checksum": checksum},
    }
    code, body = post_json(PAYMENT_HOOK, payload)
    print(f"  -> webhook de pagos respondió {code} {body.strip()[:60]!r} (referencia {reference})")
    return reference


def code_hash(email, code):
    clean = "".join(ch for ch in code if ch.isalnum()).upper()
    return hashlib.sha256(f"{clean}|{email.strip().lower()}".encode()).hexdigest()


def redeem(email, code, machine, signing_secret):
    chash = code_hash(email, code)
    nonce = uuid.uuid4().hex
    _, body = post_json(REDEEM_HOOK, {"v": 1, "code_hash": chash, "machine": machine, "nonce": nonce})
    data = json.loads(body)
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error")}, chash
    signed = "|".join([chash, machine, nonce, str(data["plan"]), str(data["reference"]),
                       str(data["meses"]), str(data["created_at"]), signing_secret])
    data["firma_ok"] = hashlib.sha256(signed.encode()).hexdigest() == data.get("sig")
    return data, chash


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Correo tuyo donde Make enviará el código")
    parser.add_argument("--plan", choices=sorted(PLAN_LINK_IDS), default="basica")
    args = parser.parse_args()

    events_secret = os.getenv("WOMPI_EVENTS_SECRET", "").strip()
    signing_secret = os.getenv("FILESYNC_PRO_LICENSE_SIGNING_SECRET", "").strip()
    if not events_secret or not signing_secret:
        sys.exit("Faltan WOMPI_EVENTS_SECRET y/o FILESYNC_PRO_LICENSE_SIGNING_SECRET en el entorno (ver el inicio del archivo).")

    email = args.email.strip().lower()
    machine_a, machine_b = "A" * 8 + uuid.uuid4().hex, "B" * 8 + uuid.uuid4().hex

    print("1) Pago simulado (cliente nuevo)")
    ref1 = send_payment(email, args.plan, events_secret)
    print("   Espera ~1 minuto y revisa tu correo (también spam): debe llegar 'Tu codigo de activacion de FileSync Pro'.")
    code = input("   Pega el código recibido: ").strip()

    print("2) Canje en el equipo A")
    data, chash = redeem(email, code, machine_a, signing_secret)
    print("  ", {k: v for k, v in data.items() if k != "sig"})
    assert data.get("ok") and data.get("firma_ok") and str(data["plan"]).startswith("fs-"), "El canje debía salir bien"
    first_created = data["created_at"]

    print("3) Mismo código en el equipo B (debe rechazarse)")
    data_b, _ = redeem(email, code, machine_b, signing_secret)
    print("  ", data_b)
    assert not data_b.get("ok") and data_b.get("error") == "used", "El equipo B debía ser rechazado"

    print("4) Segundo pago del mismo correo (renovación)")
    time.sleep(2)
    ref2 = send_payment(email, args.plan, events_secret)
    print("   Esperando 20 s a que Make procese...")
    time.sleep(20)
    data2, _ = redeem(email, code, machine_a, signing_secret)
    print("  ", {k: v for k, v in data2.items() if k != "sig"})
    assert data2.get("ok") and data2.get("firma_ok")
    assert data2["created_at"] != first_created, "created_at debía avanzar tras el segundo pago"
    print("   Debe haber llegado el correo 'Renovamos tu licencia de FileSync Pro' (y NINGÚN código nuevo).")

    email_key = hashlib.sha256(email.encode()).hexdigest()
    print("\nTODO OK. Llaves de prueba que quedaron en el data store 'CipherVault_codigos' (bórralas):")
    for key in (chash, f"fsmail-{email_key}", f"pay-{ref1}", f"pay-{ref2}"):
        print("  ", key)


if __name__ == "__main__":
    main()
