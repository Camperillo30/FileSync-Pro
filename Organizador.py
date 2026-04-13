import ctypes
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import threading
import sys
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog
import tkinter as tk
from tkinter import ttk
from urllib import error, request


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


def load_local_env():
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().with_name(".env"),
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")

    seen = set()
    for candidate in candidates:
        normalized = str(candidate.resolve()).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        load_dotenv_file(candidate)


load_local_env()


APP_NAME = "FileSync Pro"
APP_VERSION = "3.0"
APP_ID = "miempresa.filesyncpro.desktop"
DEFAULT_API_BASE_URL = os.getenv("FILESYNC_PRO_API_URL", "http://127.0.0.1:8000")
TRIAL_DAYS = 0
DEV_MODE_ENABLED = os.getenv("FILESYNC_PRO_DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEV_DEVICE_IDS = {
    device_id.strip()
    for device_id in os.getenv("FILESYNC_PRO_DEV_DEVICE_IDS", "").split(",")
    if device_id.strip()
}


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
except Exception:
    pass


class LicensingError(Exception):
    pass


class ModernOrganizadorArchivos:
    """Logica de organizacion de archivos."""

    def __init__(self):
        self.categorias = {
            "Imagenes": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".svg", ".webp", ".ico"],
            "Documentos": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"],
            "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".mid", ".midi"],
            "Video": [".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".mpeg", ".mpg", ".webm"],
            "Comprimidos": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"],
            "Ejecutables": [".exe", ".msi", ".app", ".sh", ".bat", ".cmd"],
            "Codigo": [".py", ".java", ".cpp", ".c", ".js", ".html", ".css", ".php", ".rb", ".pl", ".json", ".xml"],
            "Sistema": [".dll", ".sys", ".ini", ".cfg", ".log", ".bak"],
            "Datos": [".db", ".sqlite", ".mdb", ".accdb", ".csv"],
            "Diseno": [".psd", ".ai", ".eps", ".indd", ".xd", ".fig", ".sketch"],
            "eBooks": [".epub", ".mobi", ".azw", ".azw3"],
            "Otros": [],
        }
        self.hashes_archivos = {}
        self.estadisticas = self._nuevas_estadisticas()

    @staticmethod
    def _nuevas_estadisticas():
        return {
            "total_archivos": 0,
            "procesados": 0,
            "duplicados": 0,
            "errores": 0,
            "categorias_usadas": set(),
        }

    def reset(self):
        self.hashes_archivos = {}
        self.estadisticas = self._nuevas_estadisticas()

    def calcular_hash_archivo(self, ruta_archivo):
        hash_obj = hashlib.sha256()
        try:
            with open(ruta_archivo, "rb") as archivo:
                for bloque in iter(lambda: archivo.read(4096), b""):
                    hash_obj.update(bloque)
            return hash_obj.hexdigest()
        except OSError:
            return None

    def obtener_categoria(self, extension, nombre_archivo=""):
        extension = extension.lower()
        for categoria, extensiones in self.categorias.items():
            if extension in extensiones:
                return categoria

        nombre_lower = nombre_archivo.lower()
        if any(word in nombre_lower for word in ["setup", "install", "installer"]):
            return "Ejecutables"
        if any(word in nombre_lower for word in ["readme", "license", "changelog"]):
            return "Documentos"
        if any(word in nombre_lower for word in ["backup", "old", "copy"]):
            return "Sistema"
        return "Otros"

    def generar_nombre_unico(self, ruta_destino, nombre_original):
        nombre_base = Path(nombre_original).stem
        extension = Path(nombre_original).suffix
        candidato = ruta_destino / nombre_original
        if not candidato.exists():
            return candidato

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        contador = 0
        while True:
            sufijo = f"_{timestamp}" if contador == 0 else f"_{timestamp}_{contador}"
            candidato = ruta_destino / f"{nombre_base}{sufijo}{extension}"
            if not candidato.exists():
                return candidato
            contador += 1

    @staticmethod
    def clave_orden_personalizada(nombre_archivo):
        nombre_sin_ext = Path(nombre_archivo).stem.lower()
        if nombre_sin_ext and nombre_sin_ext[0].isdigit():
            match = re.match(r"^(\d+)", nombre_sin_ext)
            return (0, int(match.group(1)) if match else 0, nombre_sin_ext)
        return (1, nombre_sin_ext, nombre_sin_ext)

    def obtener_todas_extensiones(self):
        extensiones = set()
        for lista in self.categorias.values():
            extensiones.update(lista)
        return sorted(extensiones)


class LicenseManager:
    """Gestiona licencia local."""

    def __init__(self):
        base_dir = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
        base_dir.mkdir(parents=True, exist_ok=True)
        self.license_file = base_dir / "license.json"
        self.state = self._load_state()

    def _load_state(self):
        if self.license_file.exists():
            try:
                return json.loads(self.license_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        state = {
            "installed_at": dt.datetime.utcnow().isoformat(),
            "license_key": "",
            "email": "",
            "status": "inactive",
            "last_validation": "",
            "device_id": self.get_device_id(),
        }
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        if state is not None:
            self.state = state
        self.license_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    @staticmethod
    def get_device_id():
        raw = f"{platform.system()}|{platform.node()}|{uuid.getnode()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_developer_access_enabled(self):
        if DEV_MODE_ENABLED:
            return True
        return self.get_device_id() in DEV_DEVICE_IDS

    def days_left_in_trial(self):
        if TRIAL_DAYS <= 0:
            return 0
        installed_at = dt.datetime.fromisoformat(self.state["installed_at"])
        elapsed = (dt.datetime.utcnow() - installed_at).days
        return max(TRIAL_DAYS - elapsed, 0)

    def is_feature_unlocked(self):
        return self.is_developer_access_enabled() or self.state.get("status") == "active"

    def activate_local(self, email, license_key):
        self.state.update(
            {
                "email": email,
                "license_key": license_key,
                "status": "active",
                "last_validation": dt.datetime.utcnow().isoformat(),
                "device_id": self.get_device_id(),
            }
        )
        self._save_state()

    def set_validation_status(self, status):
        self.state["status"] = status
        self.state["last_validation"] = dt.datetime.utcnow().isoformat()
        self._save_state()


class PaymentAPIClient:
    """Cliente HTTP minimo para comunicarse con el backend."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _humanize_http_error(self, status_code, detail):
        parsed_detail = detail.strip()
        try:
            payload = json.loads(parsed_detail)
            if isinstance(payload, dict) and payload.get("detail"):
                parsed_detail = str(payload["detail"])
        except json.JSONDecodeError:
            pass

        return f"Error HTTP {status_code}: {parsed_detail}"

    def _connection_help_message(self):
        if self.base_url.startswith("http://127.0.0.1") or self.base_url.startswith("http://localhost"):
            return (
                "No se pudo conectar con el servidor de licencias local.\n\n"
                f"URL configurada: {self.base_url}\n\n"
                "Pasos para resolverlo:\n"
                "1. Inicia el backend con: uvicorn backend_app:app --reload\n"
                "2. Verifica que responda en /health\n"
                "3. Si usas otra URL, configura FILESYNC_PRO_API_URL en tu .env"
            )

        return (
            "No se pudo conectar con el servidor de licencias.\n\n"
            f"URL configurada: {self.base_url}\n\n"
            "Revisa que FILESYNC_PRO_API_URL apunte a una API activa y accesible."
        )

    def health_check(self):
        return self._request_json("GET", "/health")

    def _request_json(self, method, path, payload=None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LicensingError(self._humanize_http_error(exc.code, detail)) from exc
        except error.URLError as exc:
            raise LicensingError(self._connection_help_message()) from exc

    def create_checkout(self, email, plan_code="pro_lifetime"):
        return self._request_json("POST", "/checkout/create", {"email": email, "plan_code": plan_code})

    def activate_license(self, email, license_key, device_id):
        return self._request_json(
            "POST",
            "/licenses/activate",
            {"email": email, "license_key": license_key, "device_id": device_id},
        )

    def validate_license(self, email, license_key, device_id):
        return self._request_json(
            "POST",
            "/licenses/validate",
            {"email": email, "license_key": license_key, "device_id": device_id},
        )

    def get_checkout_status(self, order_id):
        return self._request_json("GET", f"/checkout/status/{order_id}")


class ModernOrganizadorGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} - Organizador de Archivos")
        self.state("zoomed")
        self.configure(bg="#f4f6f8")
        self.minsize(1200, 760)

        self.license_manager = LicenseManager()
        self.api_client = PaymentAPIClient(DEFAULT_API_BASE_URL)
        self.organizador = ModernOrganizadorArchivos()

        self.directorio_origen = tk.StringVar()
        self.directorio_destino = tk.StringVar()
        self.organizar_por_categoria = tk.BooleanVar(value=True)
        self.eliminar_duplicados = tk.BooleanVar(value=True)
        self.mover_en_vez_de_copiar = tk.BooleanVar(value=True)
        self.organizar_todos = tk.BooleanVar(value=True)
        self.tipo_archivo_seleccionado = tk.StringVar()
        self.status_text = tk.StringVar()
        self.checkout_order_id = tk.StringVar(value="")
        self.checkout_email = tk.StringVar(value="")

        self.estadisticas = {
            "total": tk.StringVar(value="0"),
            "procesados": tk.StringVar(value="0"),
            "duplicados": tk.StringVar(value="0"),
            "categorias": tk.StringVar(value="0"),
        }
        self._buttons = []

        self._configurar_icono()
        self.configurar_estilos()
        self.crear_widgets()
        self.actualizar_estado_licencia()
        self._configurar_atajos()

    def _configurar_icono(self):
        for icono in ("icono.png", "icono.ico"):
            try:
                if icono.endswith(".png"):
                    self.iconphoto(False, tk.PhotoImage(file=icono))
                else:
                    self.iconbitmap(icono)
                return
            except Exception:
                continue

    def _configurar_atajos(self):
        self.bind("<Control-o>", lambda _: self.seleccionar_origen())
        self.bind("<Control-d>", lambda _: self.seleccionar_destino())
        self.bind("<F5>", lambda _: self.iniciar_organizacion())
        self.bind("<Control-l>", lambda _: self.verificar_licencia())
        self.bind("<Control-q>", lambda _: self.salir())

    def configurar_estilos(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Titulo.TLabel", font=("Segoe UI", 26, "bold"), background="#f4f6f8", foreground="#10233b")
        style.configure("Subtitulo.TLabel", font=("Segoe UI", 11), background="#f4f6f8", foreground="#607086")
        style.configure("Card.TLabelframe", background="#ffffff", borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#10233b", font=("Segoe UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=10)
        style.configure("Secondary.TButton", font=("Segoe UI", 10), padding=8)
        style.configure("TCheckbutton", background="#ffffff", font=("Segoe UI", 10))

    def crear_widgets(self):
        container = ttk.Frame(self, padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        self._crear_header(container)

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        self._crear_panel_izquierdo(content)
        self._crear_panel_derecho(content)

    def _crear_header(self, parent):
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill=tk.X)

        title_frame = ttk.Frame(wrapper)
        title_frame.pack(fill=tk.X)
        ttk.Label(title_frame, text=APP_NAME, style="Titulo.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_frame,
            text="App de escritorio lista para distribuir, con licencias y cobro online.",
            style="Subtitulo.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        license_bar = tk.Frame(wrapper, bg="#10233b", padx=16, pady=12)
        license_bar.pack(fill=tk.X, pady=(14, 0))
        tk.Label(
            license_bar,
            textvariable=self.status_text,
            bg="#10233b",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT)

        for text, command in [
            ("Comprar licencia", self.comprar_licencia),
            ("Activar licencia", self.activar_licencia),
            ("Verificar licencia", self.verificar_licencia),
        ]:
            btn = ttk.Button(license_bar, text=text, command=command, style="Secondary.TButton")
            btn.pack(side=tk.RIGHT, padx=(8, 0))
            self._buttons.append(btn)

    def _crear_panel_izquierdo(self, parent):
        left_panel = ttk.LabelFrame(parent, text="Configuracion", padding=18, style="Card.TLabelframe")
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        dir_frame = ttk.Frame(left_panel)
        dir_frame.pack(fill=tk.X, pady=(0, 18))
        self._crear_selector_directorio(dir_frame, "Directorio de origen", self.directorio_origen, self.seleccionar_origen)
        self._crear_selector_directorio(dir_frame, "Directorio de destino", self.directorio_destino, self.seleccionar_destino)

        opts_frame = ttk.LabelFrame(left_panel, text="Opciones", padding=14, style="Card.TLabelframe")
        opts_frame.pack(fill=tk.X, pady=(0, 18))
        for text, variable in [
            ("Organizar por categorias", self.organizar_por_categoria),
            ("Eliminar archivos duplicados", self.eliminar_duplicados),
            ("Mover archivos en lugar de copiar", self.mover_en_vez_de_copiar),
        ]:
            ttk.Checkbutton(opts_frame, text=text, variable=variable).pack(anchor=tk.W, pady=3)

        tipo_frame = ttk.LabelFrame(opts_frame, text="Filtro de archivos", padding=10, style="Card.TLabelframe")
        tipo_frame.pack(fill=tk.X, pady=(12, 0))
        ttk.Checkbutton(
            tipo_frame,
            text="Organizar todos los tipos de archivo",
            variable=self.organizar_todos,
            command=self.actualizar_disponibilidad_tipo,
        ).pack(anchor=tk.W)

        selector = ttk.Frame(tipo_frame)
        selector.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(selector, text="Solo esta extension:").pack(side=tk.LEFT, padx=(18, 8))
        self.combo_tipo = ttk.Combobox(selector, textvariable=self.tipo_archivo_seleccionado, state="readonly", width=14)
        self.combo_tipo["values"] = self.organizador.obtener_todas_extensiones()
        if self.combo_tipo["values"]:
            self.combo_tipo.set(self.combo_tipo["values"][0])
        self.combo_tipo.pack(side=tk.LEFT)
        self.actualizar_disponibilidad_tipo()

        stats_frame = ttk.LabelFrame(left_panel, text="Estadisticas", padding=14, style="Card.TLabelframe")
        stats_frame.pack(fill=tk.X)
        self._crear_fila_stat(stats_frame, 0, "Archivos totales", self.estadisticas["total"], "Procesados", self.estadisticas["procesados"])
        self._crear_fila_stat(stats_frame, 1, "Duplicados", self.estadisticas["duplicados"], "Categorias", self.estadisticas["categorias"])

    def _crear_panel_derecho(self, parent):
        right_panel = ttk.Frame(parent)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(8, 0))

        checkout_frame = ttk.LabelFrame(right_panel, text="Ventas y activacion", padding=14, style="Card.TLabelframe")
        checkout_frame.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(
            checkout_frame,
            text="Vende la app con una licencia vitalicia. El cobro ocurre en Mercado Pago y la activacion se entrega desde tu backend.",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        info = ttk.Frame(checkout_frame)
        info.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(info, text="Pedido actual:").grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Label(info, textvariable=self.checkout_order_id).grid(row=0, column=1, sticky=tk.W, padx=(8, 0), pady=2)
        ttk.Label(info, text="Email comprador:").grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Label(info, textvariable=self.checkout_email).grid(row=1, column=1, sticky=tk.W, padx=(8, 0), pady=2)

        log_frame = ttk.LabelFrame(right_panel, text="Registro de actividad", padding=14, style="Card.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.texto_log = scrolledtext.ScrolledText(log_frame, height=20, font=("Consolas", 9), wrap=tk.WORD, bg="#f8fafc")
        self.texto_log.pack(fill=tk.BOTH, expand=True)
        self.texto_log.tag_config("exito", foreground="#117a37")
        self.texto_log.tag_config("error", foreground="#b42318")
        self.texto_log.tag_config("advertencia", foreground="#b54708")
        self.texto_log.tag_config("info", foreground="#175cd3")
        self.texto_log.tag_config("subtitulo", font=("Consolas", 9, "bold"))

        self.progreso = ttk.Progressbar(right_panel, mode="determinate")
        self.progreso.pack(fill=tk.X, pady=(14, 14))

        btn_frame = ttk.Frame(right_panel)
        btn_frame.pack(fill=tk.X)
        for text, command, style_name, side in [
            ("Iniciar organizacion", self.iniciar_organizacion, "Primary.TButton", tk.LEFT),
            ("Analizar directorio", self.analizar_directorio, "Secondary.TButton", tk.LEFT),
            ("Limpiar registro", self.limpiar_log, "Secondary.TButton", tk.LEFT),
            ("Salir", self.salir, "Secondary.TButton", tk.RIGHT),
        ]:
            btn = ttk.Button(btn_frame, text=text, command=command, style=style_name)
            btn.pack(side=side, padx=8)
            self._buttons.append(btn)

    def _crear_selector_directorio(self, parent, label, variable, command):
        ttk.Label(parent, text=label, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 6))
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row, text="Examinar", command=command, style="Secondary.TButton").pack(side=tk.RIGHT)

    @staticmethod
    def _crear_fila_stat(parent, row, label_a, var_a, label_b, var_b):
        ttk.Label(parent, text=label_a).grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Label(parent, textvariable=var_a, font=("Segoe UI", 9, "bold")).grid(row=row, column=1, sticky=tk.W, padx=(8, 24), pady=4)
        ttk.Label(parent, text=label_b).grid(row=row, column=2, sticky=tk.W, pady=4)
        ttk.Label(parent, textvariable=var_b, font=("Segoe UI", 9, "bold")).grid(row=row, column=3, sticky=tk.W, padx=(8, 0), pady=4)

    def actualizar_estado_licencia(self):
        state = self.license_manager.state
        if self.license_manager.is_developer_access_enabled():
            text = "Acceso de desarrollador activo | Equipo sin restricciones"
        elif state.get("status") == "active":
            text = f"Licencia activa para {state.get('email', 'usuario')} | Dispositivo validado"
        else:
            text = f"Licencia requerida | Backend: {DEFAULT_API_BASE_URL}"
        self.status_text.set(text)

    def actualizar_disponibilidad_tipo(self):
        self.combo_tipo.configure(state="disabled" if self.organizar_todos.get() else "readonly")

    def seleccionar_origen(self):
        seleccionado = filedialog.askdirectory(title="Seleccionar directorio de origen")
        if seleccionado:
            self.directorio_origen.set(seleccionado)
            self.log("Directorio de origen seleccionado.", "info")

    def seleccionar_destino(self):
        seleccionado = filedialog.askdirectory(title="Seleccionar directorio de destino")
        if seleccionado:
            self.directorio_destino.set(seleccionado)
            self.log("Directorio de destino seleccionado.", "info")

    def log(self, mensaje, tipo="normal"):
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        prefijos = {"exito": "[OK] ", "error": "[ERROR] ", "advertencia": "[WARN] ", "info": "[INFO] ", "subtitulo": ""}
        linea = f"[{timestamp}] {prefijos.get(tipo, '')}{mensaje}\n"
        self.texto_log.insert(tk.END, linea)
        if tipo != "normal":
            self.texto_log.tag_add(tipo, "end-2l linestart", "end-2l lineend")
        self.texto_log.see(tk.END)
        self.texto_log.update_idletasks()

    def limpiar_log(self):
        self.texto_log.delete("1.0", tk.END)
        self.log("Registro limpiado.", "info")

    def comprar_licencia(self):
        try:
            self.api_client.health_check()
        except LicensingError as exc:
            messagebox.showerror("Servidor de licencias no disponible", str(exc))
            self.log("No se pudo contactar el backend de licencias.", "error")
            return

        email = simpledialog.askstring("Comprar licencia", "Correo del comprador:", parent=self)
        if not email:
            return
        try:
            checkout = self.api_client.create_checkout(email=email)
        except LicensingError as exc:
            messagebox.showerror("No fue posible iniciar el cobro", str(exc))
            return

        self.checkout_order_id.set(checkout.get("order_id", ""))
        self.checkout_email.set(email)
        checkout_url = checkout.get("checkout_url") or checkout.get("sandbox_checkout_url")
        self.log(f"Checkout generado para {email}. Pedido {self.checkout_order_id.get()}.", "info")
        if checkout_url:
            webbrowser.open(checkout_url)
            messagebox.showinfo(
                "Pago iniciado",
                "Se abrio Mercado Pago en tu navegador.\n\n"
                "Despues del pago aprobado, usa 'Verificar licencia' o activa la clave recibida.",
            )

    def activar_licencia(self):
        email = simpledialog.askstring("Activar licencia", "Correo del comprador:", parent=self)
        if not email:
            return
        license_key = simpledialog.askstring("Activar licencia", "Clave de licencia:", parent=self)
        if not license_key:
            return
        try:
            response = self.api_client.activate_license(email, license_key, self.license_manager.get_device_id())
        except LicensingError as exc:
            messagebox.showerror("No fue posible activar", str(exc))
            return

        if response.get("status") == "active":
            self.license_manager.activate_local(email, license_key)
            self.actualizar_estado_licencia()
            self.log("Licencia activada correctamente.", "exito")
            messagebox.showinfo("Licencia activada", "La app quedo activada en este equipo.")
            return

        messagebox.showwarning("Licencia no valida", response.get("message", "No se pudo activar la licencia."))

    def verificar_licencia(self):
        if self.license_manager.is_developer_access_enabled():
            self.actualizar_estado_licencia()
            self.log("Acceso de desarrollador habilitado en este equipo.", "exito")
            messagebox.showinfo(
                "Acceso de desarrollador",
                "Este equipo tiene acceso de desarrollador activo.\n\nLa app funciona sin limitaciones de licencia.",
            )
            return

        state = self.license_manager.state
        if not state.get("license_key") or not state.get("email"):
            order_id = self.checkout_order_id.get().strip()
            if order_id:
                self._verificar_checkout(order_id)
            else:
                messagebox.showinfo("Sin licencia local", "Todavia no hay una licencia local guardada en esta app.")
            return

        try:
            response = self.api_client.validate_license(
                state["email"], state["license_key"], self.license_manager.get_device_id()
            )
        except LicensingError as exc:
            messagebox.showerror("No fue posible validar", str(exc))
            return

        if response.get("status") == "active":
            self.license_manager.set_validation_status("active")
            self.actualizar_estado_licencia()
            self.log("Licencia validada con el servidor.", "exito")
            messagebox.showinfo("Licencia valida", "La licencia sigue activa en este equipo.")
        else:
            self.license_manager.set_validation_status("inactive")
            self.actualizar_estado_licencia()
            self.log("La licencia no fue validada.", "advertencia")
            messagebox.showwarning("Licencia invalida", response.get("message", "La licencia ya no es valida."))

    def _verificar_checkout(self, order_id):
        try:
            response = self.api_client.get_checkout_status(order_id)
        except LicensingError as exc:
            messagebox.showerror("No fue posible consultar el pago", str(exc))
            return

        self.log(f"Estado de pedido {order_id}: {response.get('status', 'desconocido')}.", "info")
        license_key = response.get("license_key")
        email = response.get("email") or self.checkout_email.get().strip()
        if license_key and email:
            self.license_manager.activate_local(email, license_key)
            self.actualizar_estado_licencia()
            messagebox.showinfo("Pago aprobado", f"Tu licencia fue emitida y activada.\n\nClave: {license_key}")
        else:
            messagebox.showinfo("Pago en proceso", "Mercado Pago aun no confirma el cobro o la licencia no ha sido emitida.")

    def _asegurar_acceso(self):
        if self.license_manager.is_feature_unlocked():
            return True
        messagebox.showwarning(
            "Licencia requerida",
            "Esta funcion requiere una licencia activa.\n\nCompra o activa una licencia para continuar.",
        )
        return False

    def analizar_directorio(self):
        origen = self.directorio_origen.get()
        if not origen or not os.path.exists(origen):
            messagebox.showwarning("Directorio invalido", "Selecciona un directorio de origen valido.")
            return

        self.log("Analizando directorio...", "info")
        categorias_count = {}
        total_archivos = 0
        for root, _, files in os.walk(origen):
            for nombre_archivo in sorted(files, key=self.organizador.clave_orden_personalizada):
                total_archivos += 1
                categoria = self.organizador.obtener_categoria(Path(nombre_archivo).suffix, nombre_archivo)
                categorias_count[categoria] = categorias_count.get(categoria, 0) + 1

        self.estadisticas["total"].set(str(total_archivos))
        self.estadisticas["categorias"].set(str(len(categorias_count)))
        self.log("=" * 50, "subtitulo")
        self.log("Analisis completado", "subtitulo")
        self.log(f"Archivos encontrados: {total_archivos}", "info")
        self.log(f"Categorias detectadas: {len(categorias_count)}", "info")
        for categoria, cantidad in sorted(categorias_count.items()):
            porcentaje = (cantidad / total_archivos * 100) if total_archivos else 0
            self.log(f"{categoria}: {cantidad} archivo(s) ({porcentaje:.1f}%)")
        self.log("=" * 50, "subtitulo")

    def iniciar_organizacion(self):
        if not self._asegurar_acceso():
            return

        origen = self.directorio_origen.get()
        destino = self.directorio_destino.get()
        if not origen or not os.path.exists(origen):
            messagebox.showerror("Error", "Selecciona un directorio de origen valido.")
            return
        if not destino:
            messagebox.showerror("Error", "Selecciona un directorio de destino.")
            return
        if not self.organizar_todos.get() and not self.tipo_archivo_seleccionado.get():
            messagebox.showerror("Error", "Selecciona una extension para filtrar.")
            return

        resumen = [
            f"Origen: {origen}",
            f"Destino: {destino}",
            f"Tipo: {'Todos' if self.organizar_todos.get() else self.tipo_archivo_seleccionado.get()}",
            f"Eliminar duplicados: {'Si' if self.eliminar_duplicados.get() else 'No'}",
            f"Mover archivos: {'Si' if self.mover_en_vez_de_copiar.get() else 'No'}",
        ]
        if not messagebox.askyesno("Confirmar organizacion", "\n".join(resumen)):
            return

        self.organizador.reset()
        self._cambiar_estado_botones("disabled")
        hilo = threading.Thread(target=self._ejecutar_organizacion, daemon=True)
        hilo.start()

    def _ejecutar_organizacion(self):
        origen = self.directorio_origen.get()
        destino = self.directorio_destino.get()
        extension_filtrada = self.tipo_archivo_seleccionado.get().lower()

        try:
            total_archivos = 0
            for _, _, files in os.walk(origen):
                for nombre_archivo in files:
                    extension = Path(nombre_archivo).suffix.lower()
                    if not self.organizar_todos.get() and extension != extension_filtrada:
                        continue
                    total_archivos += 1

            self.organizador.estadisticas["total_archivos"] = total_archivos
            self.after(0, lambda: self.progreso.configure(maximum=max(total_archivos, 1), value=0))

            archivos_procesados = 0
            for root, _, files in os.walk(origen):
                for nombre_archivo in sorted(files, key=self.organizador.clave_orden_personalizada):
                    extension = Path(nombre_archivo).suffix.lower()
                    if not self.organizar_todos.get() and extension != extension_filtrada:
                        continue

                    ruta_completa = Path(root) / nombre_archivo
                    hash_archivo = self.organizador.calcular_hash_archivo(ruta_completa)
                    if hash_archivo is None:
                        self.organizador.estadisticas["errores"] += 1
                        self.after(0, self.log, f"No se pudo leer {nombre_archivo}.", "error")
                        continue

                    if self.eliminar_duplicados.get() and hash_archivo in self.organizador.hashes_archivos:
                        self.organizador.estadisticas["duplicados"] += 1
                        self.after(0, self.log, f"Duplicado detectado: {nombre_archivo}.", "advertencia")
                        if self.mover_en_vez_de_copiar.get():
                            try:
                                os.remove(ruta_completa)
                            except OSError as exc:
                                self.organizador.estadisticas["errores"] += 1
                                self.after(0, self.log, f"No se pudo eliminar duplicado: {exc}", "error")
                        continue

                    categoria = self.organizador.obtener_categoria(extension, nombre_archivo)
                    ruta_destino = Path(destino) / categoria if self.organizar_por_categoria.get() else Path(destino)
                    ruta_destino.mkdir(parents=True, exist_ok=True)
                    ruta_final = self.organizador.generar_nombre_unico(ruta_destino, nombre_archivo)

                    try:
                        if self.mover_en_vez_de_copiar.get():
                            shutil.move(str(ruta_completa), str(ruta_final))
                            accion = "Movido"
                        else:
                            shutil.copy2(str(ruta_completa), str(ruta_final))
                            accion = "Copiado"
                        self.organizador.hashes_archivos[hash_archivo] = str(ruta_final)
                        self.organizador.estadisticas["categorias_usadas"].add(categoria)
                    except OSError as exc:
                        self.organizador.estadisticas["errores"] += 1
                        self.after(0, self.log, f"Error con {nombre_archivo}: {exc}", "error")
                        continue

                    archivos_procesados += 1
                    self.organizador.estadisticas["procesados"] += 1
                    detalle = ruta_final.relative_to(Path(destino)) if self.organizar_por_categoria.get() else ruta_final.name
                    self.after(0, self.log, f"{accion}: {nombre_archivo} -> {detalle}")
                    self.after(0, self._actualizar_progreso, archivos_procesados)

            self.after(0, self._finalizar_proceso)
        except Exception as exc:
            self.after(0, self.log, f"Error critico en el proceso: {exc}", "error")
            self.after(0, self._cambiar_estado_botones, "normal")

    def _actualizar_progreso(self, valor):
        self.progreso["value"] = valor
        self.estadisticas["procesados"].set(str(valor))

    def _finalizar_proceso(self):
        stats = self.organizador.estadisticas
        self.estadisticas["procesados"].set(str(stats["procesados"]))
        self.estadisticas["duplicados"].set(str(stats["duplicados"]))
        self.estadisticas["categorias"].set(str(len(stats["categorias_usadas"])))

        self.log("=" * 50, "subtitulo")
        self.log("Organizacion completada", "subtitulo")
        self.log(f"Archivos encontrados: {stats['total_archivos']}")
        self.log(f"Archivos procesados: {stats['procesados']}")
        self.log(f"Duplicados eliminados: {stats['duplicados']}")
        self.log(f"Errores: {stats['errores']}")
        self.log(f"Categorias usadas: {len(stats['categorias_usadas'])}")
        self.progreso["value"] = 0
        self._cambiar_estado_botones("normal")

        messagebox.showinfo(
            "Proceso finalizado",
            "La organizacion ha terminado.\n\n"
            f"Procesados: {stats['procesados']}\n"
            f"Duplicados: {stats['duplicados']}\n"
            f"Categorias: {len(stats['categorias_usadas'])}",
        )

    def _cambiar_estado_botones(self, estado):
        for boton in self._buttons:
            boton.state(["disabled"] if estado == "disabled" else ["!disabled"])

    def salir(self):
        if messagebox.askyesno("Salir", "Deseas cerrar la aplicacion?"):
            self.destroy()


def main():
    app = ModernOrganizadorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
