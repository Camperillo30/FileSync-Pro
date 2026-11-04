# Pruebas de la demo única de FileSync Pro:
# - pedir la demo a Make (respuesta firmada, "granted"/"used", sin conexión no arranca),
# - el estado local de la demo y su copia de respaldo (no se recupera borrando una copia),
# - el corte limpio al organizar (solo cuentan los archivos organizados con éxito),
# - qué facturas gastan la demo.
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SECRET = "secreto-de-prueba"
MACHINE = "c" * 64


@pytest.fixture()
def org(tmp_path, monkeypatch):
    pytest.importorskip("tkinter")
    # Datos de la app y copia de respaldo de la demo en una carpeta temporal.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    import Organizador

    monkeypatch.setattr(Organizador.LicenseManager, "is_developer_access_enabled", lambda self: False)
    return Organizador


class FakeDemoMake:
    """Imita la ruta "Demo FileSync Pro" del escenario de canje en Make."""

    def __init__(self):
        self.store = {}
        self.tamper = False
        self.requests = []

    def handle(self, payload):
        self.requests.append(payload)
        key, machine, nonce = payload["code_hash"], payload["machine"], payload["nonce"]
        if payload.get("action") != "demo" or not machine or not key.startswith("fsdemo-"):
            return {"ok": False, "error": "invalid"}
        status = "used" if key in self.store else "granted"
        self.store.setdefault(key, {"machine": machine})
        sig = hashlib.sha256(f"{key}|{machine}|{nonce}|{status}|{SECRET}".encode()).hexdigest()
        return {"ok": True, "demo": status, "sig": "0" * 64 if self.tamper else sig}

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.dumps(outer.handle(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))).encode()
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


@pytest.fixture()
def make():
    fake = FakeDemoMake()
    fake.url = fake.start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


# --- Make -------------------------------------------------------------------------


def test_demo_is_granted_once_per_machine(org, make):
    client = org.MakeCodeLicenseClient(make.url, SECRET)
    assert client.request_demo(MACHINE) == "granted"
    assert client.request_demo(MACHINE) == "used"
    assert client.request_demo("d" * 64) == "granted"
    assert make.requests[0]["code_hash"] == "fsdemo-" + MACHINE
    assert make.requests[0]["action"] == "demo"


def test_demo_rejects_bad_signature(org, make):
    make.tamper = True
    with pytest.raises(org.LicensingError):
        org.MakeCodeLicenseClient(make.url, SECRET).request_demo(MACHINE)


def test_demo_needs_connection(org):
    with pytest.raises(org.LicensingError, match="internet"):
        org.MakeCodeLicenseClient("http://127.0.0.1:9/", SECRET).request_demo(MACHINE, timeout=2)


# --- Estado local -----------------------------------------------------------------


def test_demo_counters_and_exhaustion(org):
    lm = org.LicenseManager()
    assert lm.demo_disponible() and not lm.is_demo_active()
    lm.start_demo_local()
    assert lm.is_demo_active()
    assert lm.consume_demo("files", org.DEMO_ARCHIVOS - 1) == 1
    assert lm.consume_demo("files", 5) == 0  # Nunca pasa del tope.
    assert lm.is_demo_active()  # Todavía le quedan facturas.
    for _ in range(org.DEMO_FACTURAS):
        lm.consume_demo("invoices")
    assert not lm.is_demo_active()
    assert lm.demo_info()["status"] == "exhausted"


def test_deleting_app_data_does_not_reset_demo(org, tmp_path):
    lm = org.LicenseManager()
    lm.start_demo_local()
    lm.consume_demo("files", 30)
    lm.license_file.unlink()  # El usuario borra la carpeta de datos de la app.
    info = org.LicenseManager().demo_info()
    assert info["status"] == "active" and info["files_left"] == org.DEMO_ARCHIVOS - 30


def test_restoring_an_old_copy_keeps_the_most_used(org):
    lm = org.LicenseManager()
    lm.start_demo_local()
    lm.consume_demo("files", 10)
    viejo = lm.license_file.read_bytes()  # Copia de license.json con 10 usados.
    lm.consume_demo("files", 30)
    lm.license_file.write_bytes(viejo)  # La restaura para "recuperar" archivos.
    assert org.LicenseManager().demo_info()["files_left"] == org.DEMO_ARCHIVOS - 40


def test_demo_not_offered_with_paid_license(org):
    lm = org.LicenseManager()
    lm.activate_local("a@b.co", "X" * 16, subscription_plan_code="basica")
    assert not lm.demo_disponible()
    lm.start_demo_local()
    assert not lm.is_demo_active()  # Con licencia, la demo no aplica.


# --- Facturas ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("estado", "confianza", "cuenta"),
    [
        ("ok", "86%", True),
        ("parcial", "43%", True),
        ("parcial", "14%", False),
        ("sin_texto", "0%", False),
        ("error: no se pudo leer", "0%", False),
    ],
)
def test_only_readable_invoices_count(org, estado, confianza, cuenta):
    result = {"estado": estado, "confianza": confianza}
    assert org.ModernOrganizadorGUI._factura_cuenta_para_demo(result) is cuenta


# --- Organizar: corte limpio --------------------------------------------------------


def _gui_sin_ventana(org, lm):
    """Una ventana "de mentira" con solo lo que usa `_ejecutar_organizacion`."""
    gui = org.ModernOrganizadorGUI.__new__(org.ModernOrganizadorGUI)
    gui.license_manager = lm
    gui.process_recovery = org.ProcessRecoveryManager()
    gui.organizador = org.ModernOrganizadorArchivos()
    gui._cancel_requested = False
    gui._recovery_batch_size = 50
    gui._progress_batch_size = 100
    gui._log_batch_size = 250
    gui._last_recovery_update = 0.0
    gui._demo_corte_info = None
    gui.after = lambda _ms, *_args: None  # Sin interfaz: se ignoran las actualizaciones.
    return gui


def _run(org, gui, origen, destino, files, eliminar_duplicados=False):
    run_state = gui.process_recovery.create_run(
        {
            "origen": str(origen), "destino": str(destino), "organizar_por_categoria": True,
            "organizar_por_fecha": False, "detectar_recibos": False,
            "eliminar_duplicados": eliminar_duplicados, "mover_en_vez_de_copiar": True,
            "organizar_todos": True, "tipo_archivo": "", "limite_ejecucion": None, "demo": True,
        },
        files,
    )
    gui._corrida_demo = True
    gui.organizador.estadisticas["total_archivos"] = len(files)
    gui._ejecutar_organizacion(files, run_state, 0)
    return run_state


def test_demo_cuts_cleanly_at_limit(org, tmp_path):
    lm = org.LicenseManager()
    lm.start_demo_local()
    origen, destino = tmp_path / "origen", tmp_path / "destino"
    origen.mkdir()
    files = []
    for i in range(org.DEMO_ARCHIVOS + 12):
        path = origen / f"doc_{i:03d}.txt"
        path.write_text(f"contenido {i}")
        files.append(str(path))

    gui = _gui_sin_ventana(org, lm)
    _run(org, gui, origen, destino, files)

    movidos = [p for p in destino.rglob("*") if p.is_file()]
    assert len(movidos) == org.DEMO_ARCHIVOS
    assert len(list(origen.glob("*.txt"))) == 12  # El resto no se tocó.
    assert gui._demo_corte_info == {"revisados": org.DEMO_ARCHIVOS, "sin_tocar": 12}
    assert lm.demo_info()["files_left"] == 0
    journal = gui.process_recovery.load_state()  # La corrida quedó lista para cerrarse.
    assert journal["next_index"] == len(files)


def test_duplicates_do_not_spend_demo(org, tmp_path):
    lm = org.LicenseManager()
    lm.start_demo_local()
    origen, destino = tmp_path / "origen", tmp_path / "destino"
    origen.mkdir()
    files = []
    for i in range(6):
        path = origen / f"foto_{i}.txt"
        path.write_text("mismo contenido" if i < 4 else f"distinto {i}")
        files.append(str(path))

    gui = _gui_sin_ventana(org, lm)
    _run(org, gui, origen, destino, files, eliminar_duplicados=True)

    # 1 original + 2 distintos organizados; 3 duplicados a cuarentena (no cuentan).
    assert gui.organizador.estadisticas["duplicados"] == 3
    assert lm.demo_info()["files_left"] == org.DEMO_ARCHIVOS - 3
    assert gui._demo_corte_info is None


# --- Duración de 24 horas -----------------------------------------------------------


def _mover_inicio(lm, horas_atras):
    import datetime as dt

    inicio = dt.datetime.utcnow() - dt.timedelta(hours=horas_atras)
    lm.state["demo"]["started_at"] = inicio.isoformat()
    lm.state["demo"]["last_seen_at"] = inicio.isoformat()
    lm._save_state()


def test_demo_lasts_24_hours(org):
    lm = org.LicenseManager()
    lm.start_demo_local()
    _mover_inicio(lm, 23)
    info = lm.demo_info()
    assert lm.is_demo_active() and 0 < info["seconds_left"] <= 3600
    _mover_inicio(lm, 24.1)
    assert not lm.is_demo_active()
    info = lm.demo_info()
    assert info["status"] == "exhausted" and info["expired"]
    assert not lm.demo_disponible()  # Vencida no vuelve a ofrecerse.


def test_turning_clock_back_ends_demo(org):
    import datetime as dt

    lm = org.LicenseManager()
    lm.start_demo_local()
    futuro = (dt.datetime.utcnow() + dt.timedelta(hours=3)).isoformat()
    lm.state["demo"]["last_seen_at"] = futuro  # La app ya vio una hora "más adelante".
    assert not lm.is_demo_active()


def test_restoring_old_copy_does_not_extend_time(org):
    lm = org.LicenseManager()
    lm.start_demo_local()
    _mover_inicio(lm, 25)
    viejo = lm.license_file.read_bytes()
    lm.state["demo"]["started_at"] = __import__("datetime").datetime.utcnow().isoformat()
    lm.license_file.write_bytes(viejo)
    # La copia de respaldo guarda el inicio más antiguo: sigue vencida.
    assert not org.LicenseManager().is_demo_active()
