"""FileSync Pro - Aplicación de escritorio (Tkinter).

Este es el archivo principal de la app: contiene TODO lo que ve y usa el
usuario final. Es un archivo grande, así que aquí va un mapa rápido de qué
hace cada parte (buscar la clase/función por nombre para ir directo):

- Funciones al inicio del archivo (load_dotenv_file, load_runtime_config...):
  cargan configuración desde `.env` y `desktop_runtime_config.json` antes de
  que arranque cualquier otra cosa (URLs del backend, links de pago, etc.).
- `ModernOrganizadorArchivos`: la lógica "pura" de organizar archivos
  (decidir la categoría de cada archivo, calcular su hash para detectar
  duplicados, elegir la carpeta de destino por fecha, etc.). No dibuja nada
  en pantalla, solo trabaja con rutas de archivos.
- `InvoiceExtractor`: lee facturas (imágenes o PDF) y trata de extraer datos
  como proveedor, número de factura, fecha, NIT/RUT, correo y total, usando
  OCR (pytesseract) y/o texto de PDF (pypdf).
- `LicenseManager`: guarda en disco (cifrado) el estado de la licencia del
  usuario en este equipo: si está activada, el plan contratado, cuántos usos
  lleva este mes, si está en periodo de prueba, etc.
- `ProcessRecoveryManager`: permite que, si la app se cierra a mitad de una
  organización de archivos, la próxima vez se pueda continuar donde quedó
  (guarda un "manifiesto" y un "diario" de lo ya procesado).
- `LegacySheetsLicenseClient` y `LicenseApiClient`: dos formas de consultar
  si una licencia es válida — la primera lee un CSV publicado de Google
  Sheets (flujo antiguo con Make), la segunda habla con el backend propio
  (`backend_app.py`) por HTTP. Cuál se usa depende de la configuración.
- `ModernOrganizadorGUI`: la ventana principal (hereda de `tk.Tk`). Aquí
  vive toda la interfaz: botones, pestañas, el tutorial guiado, el flujo de
  compra/activación de licencia, y el hilo en segundo plano que organiza los
  archivos mientras la interfaz sigue respondiendo.
- `main()`: punto de entrada; crea la ventana principal y arranca el bucle
  de eventos de Tkinter.

La app corre en Windows y en macOS. Todo lo que depende del sistema
operativo (tipografías, carpeta de datos, recorrido de carpetas en Mac,
identificador del equipo, Tesseract) vive en `filesync_core/platform_compat.py`.
"""

import ctypes
import csv
import datetime as dt
import hashlib
import io
import json
import os
import platform
import re
import shutil
import threading
import sys
import time
import uuid
import webbrowser
import base64
import logging
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog
import tkinter as tk
from tkinter import ttk
from urllib import error, request

from filesync_core.platform_compat import (
    FONT_MONO,
    FONT_UI,
    IS_MAC,
    IS_WINDOWS,
    MAC_SCROLL_UNIT_PX,
    app_data_root,
    contains_only_ds_store,
    find_bundled_tesseract,
    find_tesseract_cmd,
    is_inside_macos_package,
    mac_hardware_uuid,
    scale_font_size,
    walk_files,
)
from filesync_core.safety import (
    PathSafetyError,
    atomic_write_bytes,
    atomic_write_json,
    is_within,
    purge_quarantine,
    quarantine_file,
    quarantine_summary,
    safe_unlink,
    validate_source_destination,
)

try:
    from PIL import ExifTags, Image, ImageTk, UnidentifiedImageError
except ImportError:
    ExifTags = None
    Image = None
    ImageTk = None
    UnidentifiedImageError = OSError

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


DESKTOP_RUNTIME_CONFIG_NAME = "desktop_runtime_config.json"
DESKTOP_RUNTIME_CONFIG_OVERRIDE_NAME = "desktop_runtime_config.local.json"
ICON_ASSET_DIR = "assets"
ICON_PNG_NAME = "icono.png"
ICON_PNG_VARIANTS = ("icono_16.png", "icono_32.png", "icono_48.png", ICON_PNG_NAME)
ICON_ICO_NAME = "icono.ico"
ICON_HEADER_NAME = "icono_56.png"
LOGO_MARCA_FOOTER_NAME = "logo_marca_28.png"


def load_dotenv_file(path: str = ".env"):
    """Lee un archivo `.env` (líneas `CLAVE=valor`) y carga cada valor en
    las variables de entorno del proceso, sin pisar las que ya existan
    (`setdefault`). Ignora líneas vacías o que empiezan con `#`."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_runtime_config_file(path: Path, override_existing: bool = False):
    """Carga un archivo JSON de configuración (por ejemplo
    `desktop_runtime_config.json`) y vuelca sus claves como variables de
    entorno. Si un valor es un objeto/dict, se guarda como texto JSON (por
    ejemplo, el mapa de links de pago por plan). `override_existing`
    controla si debe pisar variables que ya estén definidas (se usa en
    `True` para el archivo `.local.json`, que tiene prioridad)."""
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return

    for key, value in payload.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        if isinstance(value, dict):
            serialized = json.dumps(value, ensure_ascii=False)
            if override_existing:
                os.environ[normalized_key] = serialized
            else:
                os.environ.setdefault(normalized_key, serialized)
            continue
        elif value is not None:
            serialized = str(value).strip()
            if override_existing:
                os.environ[normalized_key] = serialized
            else:
                os.environ.setdefault(normalized_key, serialized)


def load_runtime_config():
    """Busca y carga la configuración del ejecutable en varias ubicaciones
    posibles (carpeta temporal de PyInstaller, carpeta actual, junto al
    script, junto al .exe), en ese orden, sin duplicar rutas ya vistas.
    Al final también busca `desktop_runtime_config.local.json`, que sí
    puede sobrescribir valores (para pruebas en el equipo del desarrollador,
    ver `is_owner_device` más abajo)."""
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / DESKTOP_RUNTIME_CONFIG_NAME)

    candidates.extend(
        [
            Path.cwd() / DESKTOP_RUNTIME_CONFIG_NAME,
            Path(__file__).resolve().with_name(DESKTOP_RUNTIME_CONFIG_NAME),
        ]
    )
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / DESKTOP_RUNTIME_CONFIG_NAME)

    seen = set()
    for candidate in candidates:
        normalized = str(candidate.resolve()).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        load_runtime_config_file(candidate)

    override_candidates = [
        Path.cwd() / DESKTOP_RUNTIME_CONFIG_OVERRIDE_NAME,
        Path(__file__).resolve().with_name(DESKTOP_RUNTIME_CONFIG_OVERRIDE_NAME),
    ]
    if getattr(sys, "frozen", False):
        override_candidates.append(Path(sys.executable).resolve().parent / DESKTOP_RUNTIME_CONFIG_OVERRIDE_NAME)

    seen.clear()
    for candidate in override_candidates:
        normalized = str(candidate.resolve()).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        load_runtime_config_file(candidate, override_existing=True)


load_runtime_config()  # Se ejecuta apenas se importa el módulo, antes de leer las constantes de abajo.


def load_local_env():
    """Igual que `load_runtime_config`, pero para el archivo `.env`
    clásico (variable=valor por línea), buscándolo en las mismas
    ubicaciones típicas de una app empaquetada con PyInstaller."""
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


def resolve_app_resource(*relative_parts: str):
    """Encuentra un recurso empaquetado con la app (por ejemplo un ícono en
    `assets/`) probando varias carpetas base posibles, y devuelve la
    primera ruta que realmente existe (o `None` si no se encuentra en
    ninguna)."""
    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    roots.extend(
        [
            Path.cwd(),
            Path(__file__).resolve().parent,
        ]
    )
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)

    seen = set()
    for root in roots:
        normalized = str(root.resolve()).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidate = root.joinpath(*relative_parts)
        if candidate.exists():
            return candidate
    return None


def configure_ocr():
    """Le indica a `pytesseract` qué Tesseract usar y devuelve la ruta elegida
    (o `None` si se deja el del sistema).

    Orden: 1) el Tesseract EMPAQUETADO dentro de la app (carpeta `tesseract/`
    en el .exe/.app, o `vendor/tesseract/` al correr desde el código), con sus
    idiomas (`tessdata`), para que el OCR funcione en un PC que no lo tenga
    instalado; 2) en macOS, el de Homebrew/MacPorts (una app abierta desde
    Finder no ve el PATH); 3) el que esté en el PATH del sistema.
    """
    if pytesseract is None:
        return None
    bundled = find_bundled_tesseract(resolve_app_resource)
    if bundled is not None:
        comando, tessdata = bundled
        pytesseract.pytesseract.tesseract_cmd = comando
        if tessdata:
            # Se fuerza (no `setdefault`): un TESSDATA_PREFIX del sistema apuntaría
            # a idiomas de otra versión de Tesseract.
            os.environ["TESSDATA_PREFIX"] = tessdata
        return comando
    comando = find_tesseract_cmd()
    if comando:
        pytesseract.pytesseract.tesseract_cmd = comando
    return comando


configure_ocr()  # Al importar el módulo, antes de que cualquier pantalla use el OCR.


def ocr_selftest(report_path: str | None = None) -> int:
    """Diagnóstico del OCR, sin abrir la ventana: `FileSync Pro --ocr-selftest [archivo]`.

    Comprueba qué Tesseract usa la app, que arranca, que tiene los idiomas `spa` y `eng`
    y que lee una imagen de prueba. Escribe el informe en `archivo` (útil en el .exe de
    Windows, que no tiene consola) y en la salida estándar si existe. Devuelve 0 si todo
    está bien. Sirve para comprobar en otro equipo que el OCR empaquetado funciona.
    """
    lines = []
    ok = False
    try:
        if pytesseract is None or Image is None:
            raise RuntimeError("pytesseract o Pillow no están disponibles en este build")
        from PIL import ImageDraw, ImageFont

        lines.append(f"tesseract_cmd={pytesseract.pytesseract.tesseract_cmd}")
        lines.append(f"TESSDATA_PREFIX={os.environ.get('TESSDATA_PREFIX', '')}")
        lines.append(f"version={pytesseract.get_tesseract_version()}")
        idiomas = sorted(pytesseract.get_languages(config=""))
        lines.append("idiomas=" + ",".join(idiomas))
        faltan = [idioma for idioma in ("spa", "eng") if idioma not in idiomas]
        if faltan:
            raise RuntimeError("faltan los idiomas: " + ", ".join(faltan))
        imagen = Image.new("RGB", (900, 140), "white")
        ImageDraw.Draw(imagen).text((30, 30), "FACTURA 12345", fill="black", font=ImageFont.load_default(size=56))
        texto = pytesseract.image_to_string(imagen, lang="spa+eng")
        lines.append(f"lectura={texto.strip()!r}")
        ok = "12345" in "".join(caracter for caracter in texto if caracter.isdigit())
    except Exception as exc:  # noqa: BLE001 - es un diagnóstico: cualquier fallo se informa, no se propaga
        lines.append(f"error={exc}")
    lines.append("resultado=" + ("OK" if ok else "FALLO"))
    informe = "\n".join(lines)
    if report_path:
        try:
            Path(report_path).write_text(informe + "\n", encoding="utf-8")
        except OSError:
            pass
    if sys.stdout is not None:
        print(informe)
    return 0 if ok else 1


def load_json_object_env(name: str):
    """Lee una variable de entorno que contiene un JSON con forma de
    objeto (por ejemplo `WOMPI_SUBSCRIPTION_LINK_MAP`) y la convierte en un
    diccionario normal de Python, con las claves en minúscula. Si el valor
    no está definido, no es JSON válido, o no es un objeto, devuelve un
    diccionario vacío en vez de lanzar un error."""
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


# --- Constantes globales de la app (nombre, planes, precios, regex, etc.) ---

APP_NAME = "FileSync Pro"
APP_VERSION = "3.1.0"
APP_AUTHOR = "S.C.A"
LOGGER = logging.getLogger("filesync_pro")
APP_ID = "miempresa.filesyncpro.desktop"
# Los duplicados enviados a cuarentena que llevan MÁS de estos días se borran
# solos al abrir la app (si la casilla de borrado automático está activa).
CUARENTENA_DIAS_MAX = 30


def obtener_aviso_derechos() -> str:
    """Texto de copyright que se muestra en el pie de la ventana, con el
    año actual calculado en cada arranque para que nunca quede desfasado."""
    anio = dt.datetime.now().year
    return (
        f"© {anio} {APP_AUTHOR} - {APP_NAME}. Todos los derechos reservados. "
        "Software con licencia comercial: queda prohibida su copia, distribución "
        "o modificación sin autorización expresa del autor."
    )
MONTH_FOLDER_NAMES = {
    1: "01 - Enero",
    2: "02 - Febrero",
    3: "03 - Marzo",
    4: "04 - Abril",
    5: "05 - Mayo",
    6: "06 - Junio",
    7: "07 - Julio",
    8: "08 - Agosto",
    9: "09 - Septiembre",
    10: "10 - Octubre",
    11: "11 - Noviembre",
    12: "12 - Diciembre",
}

# --- Paletas de color para el modo claro y el modo oscuro ---
# Un solo diccionario por tema: `configurar_estilos` y los pocos widgets
# tk "crudos" (canvas, barra de licencia, registro) leen sus colores de
# aquí, así que agregar o ajustar un color se hace en un solo lugar y se
# aplica igual en ambos temas.
TEMA_CLARO = {
    "bg_app": "#f4f6f8",
    "bg_card": "#ffffff",
    "bg_alt": "#f7fafc",
    "bg_header": "#10233b",
    "fg_header": "#ffffff",
    "text_primary": "#10233b",
    "text_secondary": "#607086",
    "text_muted": "#2f4258",
    "accent": "#0f5f8c",
    "accent_hover": "#1b789f",
    "accent_pressed": "#0b4c70",
    "accent_disabled_bg": "#8ea8b8",
    "accent_disabled_fg": "#eef4f7",
    "btn_bg": "#ffffff",
    "btn_bg_hover": "#f7fafc",
    "btn_bg_pressed": "#e8eef2",
    "btn_bg_disabled": "#dde4ea",
    "btn_fg_disabled": "#6f7c8c",
    "border": "#c6d0d8",
    "border_hover": "#a6b8c4",
    "entry_bg": "#ffffff",
    "entry_fg": "#10233b",
    "tree_bg": "#ffffff",
    "tree_fg": "#10233b",
    "tree_heading_bg": "#eef2f6",
    "log_bg": "#f8fafc",
    "log_exito": "#117a37",
    "log_error": "#b42318",
    "log_advertencia": "#b54708",
    "log_info": "#175cd3",
    "tutorial_bg": "#dff5ff",
    "tutorial_fg": "#0f5f8c",
    "scrollbar_trough": "#e7ebef",
}

TEMA_OSCURO = {
    "bg_app": "#141b23",
    "bg_card": "#1c2530",
    "bg_alt": "#212b36",
    "bg_header": "#0a121a",
    "fg_header": "#eef2f6",
    "text_primary": "#eef2f6",
    "text_secondary": "#93a3b5",
    "text_muted": "#c3ccd6",
    "accent": "#4098d7",
    "accent_hover": "#5cb1e8",
    "accent_pressed": "#2f7fb3",
    "accent_disabled_bg": "#334454",
    "accent_disabled_fg": "#6c7c8c",
    "btn_bg": "#1c2530",
    "btn_bg_hover": "#243040",
    "btn_bg_pressed": "#141b23",
    "btn_bg_disabled": "#202932",
    "btn_fg_disabled": "#5c6b7a",
    "border": "#2c3a49",
    "border_hover": "#3d4f61",
    "entry_bg": "#212b36",
    "entry_fg": "#eef2f6",
    "tree_bg": "#1c2530",
    "tree_fg": "#eef2f6",
    "tree_heading_bg": "#212b36",
    "log_bg": "#10161d",
    "log_exito": "#3ecf7e",
    "log_error": "#ff6b5e",
    "log_advertencia": "#f5a55a",
    "log_info": "#5cb1e8",
    "tutorial_bg": "#17324a",
    "tutorial_fg": "#5cb1e8",
    "scrollbar_trough": "#1a232c",
}
DEFAULT_API_BASE_URL = os.getenv("FILESYNC_PRO_API_URL", "https://hook.us2.make.com/jni71fawwtsudjo58qm82jy8l3hlr5un").strip()
SHEETS_LICENSE_URL = os.getenv("FILESYNC_PRO_LICENSE_CSV_URL", "").strip()
DEFAULT_PLAN_CODE = "basica"
DEFAULT_SUBSCRIPTION_PLAN_CODE = os.getenv("FILESYNC_PRO_DEFAULT_PLAN_CODE", DEFAULT_PLAN_CODE).strip().lower() or DEFAULT_PLAN_CODE
DEV_MODE_ENABLED = os.getenv("FILESYNC_PRO_DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEVELOPER_HOSTNAME = "DESKTOP-335GNPS"
# Cada plan puede tener un link mensual y/o anual. La clave compuesta es "<plan>_<frecuencia>".
DEFAULT_SUBSCRIPTION_LINK_MAP = {}
SUBSCRIPTION_LINK_MAP = {**DEFAULT_SUBSCRIPTION_LINK_MAP, **load_json_object_env("WOMPI_SUBSCRIPTION_LINK_MAP")}
# Precios de referencia solo para mostrar en pantalla (no se cobran desde aquí; el cobro real lo define Wompi).
PLAN_PRICING_COP = {
    "basica": {"mensual": 18900},
    "pro": {"mensual": 33900},
    "premium": {"mensual": 59900},
}
TRIAL_DAYS = 0  # Días de prueba gratuita antes de exigir licencia (0 = sin periodo de prueba).
EMAIL_REGEX = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
# Dominios de correo mal escritos comunes -> su corrección, para sugerirle
# al usuario "¿quisiste decir gmail.com?" en vez de solo rechazar el correo.
COMMON_EMAIL_DOMAIN_FIXES = {
    "gmal.com": "gmail.com",
    "gmail.con": "gmail.com",
    "gmai.com": "gmail.com",
    "gmial.com": "gmail.com",
    "hotnail.com": "hotmail.com",
    "hotmai.com": "hotmail.com",
    "hotmail.con": "hotmail.com",
    "outlok.com": "outlook.com",
    "outlook.con": "outlook.com",
    "yaho.com": "yahoo.com",
    "yahoo.con": "yahoo.com",
}
# Qué puede hacer cada plan: quitar duplicados, mover archivos, filtrar por
# tipo, cuántos usos mensuales tiene (None = ilimitado) y si puede deshacer
# la última organización. `ModernOrganizadorGUI._aplicar_restricciones_plan`
# usa este diccionario para habilitar/deshabilitar controles en la interfaz.
PLAN_FEATURES = {
    "basica": {
        "remove_duplicates": False,
        "move_files": True,
        "type_filter": False,
        "monthly_use_limit": 3,
        "undo_organization": False,
    },
    "pro": {
        "remove_duplicates": True,
        "move_files": True,
        "type_filter": True,
        "monthly_use_limit": 10,
        "undo_organization": True,
    },
    "premium": {
        "remove_duplicates": True,
        "move_files": True,
        "type_filter": True,
        "monthly_use_limit": None,
        "undo_organization": True,
    },
}
# Acceso "sin restricciones" que se usa para el equipo del desarrollador
# (ver `is_owner_device`) y para el modo desarrollador local.
FULL_FEATURE_ACCESS = {
    "remove_duplicates": True,
    "move_files": True,
    "type_filter": True,
    "monthly_use_limit": None,
    "undo_organization": True,
}

# Nombres de archivos usados por ProcessRecoveryManager para poder reanudar
# una organización interrumpida (ver esa clase más abajo).
PROCESS_STATE_FILE = "process_state.json"
PROCESS_MANIFEST_PREFIX = "process_manifest_"
PROCESS_JOURNAL_PREFIX = "process_journal_"


_fs = scale_font_size  # Tamaño de fuente ajustado a la plataforma (en Windows no cambia nada).

if IS_WINDOWS:
    try:
        # En Windows, esto hace que la app tenga su propio ícono en la barra de
        # tareas en vez de agruparse bajo el ícono genérico de Python.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


class LicensingError(Exception):
    """Error genérico para problemas relacionados con la licencia (activación,
    validación, límites de uso, etc.)."""


def get_app_storage_dir():
    """Carpeta donde la app guarda sus datos privados (licencia, estado de
    procesos, logs): `%APPDATA%\\FileSync Pro` en Windows,
    `~/Library/Application Support/FileSync Pro` en macOS, o la carpeta
    personal del usuario si `APPDATA` no está definida. También configura,
    la primera vez, el logging a archivo (`filesync_pro.log`)."""
    base_dir = app_data_root() / APP_NAME
    base_dir.mkdir(parents=True, exist_ok=True)
    if not LOGGER.handlers:
        handler = logging.FileHandler(base_dir / "filesync_pro.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
    return base_dir


def _archivo_preferencias():
    """Ruta del archivo donde se guardan preferencias de interfaz (por
    ahora, solo si el modo oscuro está activado). Es un JSON aparte de
    `license.json` porque no tiene nada de sensible y no necesita cifrado."""
    return get_app_storage_dir() / "preferencias.json"


def cargar_preferencias():
    """Lee las preferencias de interfaz guardadas en disco. Si el archivo
    no existe o está dañado, devuelve un diccionario vacío (se usan los
    valores por defecto) en vez de fallar."""
    try:
        with open(_archivo_preferencias(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def guardar_preferencias(data):
    """Guarda el diccionario de preferencias en disco. Si falla (permisos,
    disco lleno...), no interrumpe la app: la preferencia simplemente no
    persiste hasta la próxima vez que se logre guardar."""
    try:
        with open(_archivo_preferencias(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except Exception:
        pass


class ModernOrganizadorArchivos:
    """Lógica de organización de archivos.

    Esta clase no toca la interfaz gráfica ni mueve archivos por sí misma:
    solo decide cosas (a qué categoría pertenece un archivo, en qué carpeta
    de fecha debería ir, si dos archivos son duplicados por su hash, si una
    imagen parece un recibo/factura, qué nombre único usar si ya existe un
    archivo con ese nombre en el destino...). Quien realmente copia/mueve
    los archivos usando esta información es `ModernOrganizadorGUI`.
    """

    def __init__(self):
        # Mapa de categoría -> lista de extensiones que pertenecen a ella.
        # "Recibos" se deja vacío a propósito: no se detecta por extensión,
        # sino por contenido (ver `es_recibo`) cuando la detección de
        # recibos está activada.
        self.categorias = {
            "Recibos": [],
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
        self.categorias_ui = {
            "Imagenes": "Imágenes",
            "Video": "Vídeo",
            "Codigo": "Código",
            "Diseno": "Diseño",
        }
        self.image_extensions = set(self.categorias["Imagenes"])
        # Palabras que, si aparecen en el nombre del archivo o su carpeta,
        # bastan por sí solas para marcarlo como recibo/factura.
        self.receipt_keywords = (
            "recibo",
            "receipt",
            "factura",
            "invoice",
            "ticket",
            "boleta",
            "comprobante",
            "pago",
            "payment",
        )
        # Palabras "fuertes": si el OCR encuentra varias de estas en el
        # texto de la imagen, es buena señal de que es un recibo.
        self.receipt_primary_keywords = (
            "recibo",
            "receipt",
            "factura",
            "invoice",
            "ticket",
            "boleta",
            "comprobante",
            "subtotal",
            "total",
            "iva",
            "nit",
        )
        # Palabras "débiles": por sí solas no bastan, pero suman puntos
        # junto con las palabras primarias (ver la fórmula en `es_recibo`).
        self.receipt_secondary_keywords = (
            "fecha",
            "hora",
            "transaccion",
            "autorizacion",
            "cliente",
            "cajero",
            "efectivo",
            "tarjeta",
            "debito",
            "credito",
            "cantidad",
            "precio",
            "pagado",
            "gracias por su compra",
        )
        self.receipt_amount_pattern = re.compile(r"(cop|usd|eur|mxn|ars|clp|\$)\s*\d", re.IGNORECASE)  # ¿hay un monto con moneda?
        self.receipt_date_pattern = re.compile(r"\b\d{1,4}[\/\-:]\d{1,2}[\/\-:]\d{1,4}\b")  # ¿hay algo con forma de fecha?
        # Dos formas de fecha que se buscan dentro del NOMBRE del archivo:
        # AAAA-MM-DD (y variantes con "_" o ".") o DD-MM-AAAA.
        self.filename_date_patterns = (
            re.compile(r"(?P<year>19\d{2}|20\d{2})[-_\.]?(?P<month>0[1-9]|1[0-2])[-_\.]?(?P<day>0[1-9]|[12]\d|3[01])"),
            re.compile(r"(?P<day>0[1-9]|[12]\d|3[01])[-_\.](?P<month>0[1-9]|1[0-2])[-_\.](?P<year>19\d{2}|20\d{2})"),
        )
        # Tabla para quitar tildes/ñ al comparar texto (ver `_normalizar_texto`).
        self._text_translation = str.maketrans(
            {
                "á": "a",
                "é": "e",
                "í": "i",
                "ó": "o",
                "ú": "u",
                "ü": "u",
                "ñ": "n",
            }
        )
        self._exif_tag_ids = {}
        if ExifTags is not None:
            self._exif_tag_ids = {str(name): tag_id for tag_id, name in ExifTags.TAGS.items()}
        # Cachés en memoria (por ruta de archivo) para no repetir trabajo
        # costoso (OCR, lectura EXIF) si se consulta el mismo archivo varias
        # veces durante una misma organización.
        self._receipt_detection_cache = {}
        self._date_reference_cache = {}
        self._ocr_text_cache = {}
        self.hashes_archivos = {}  # hash SHA-256 -> ruta del primer archivo visto con ese contenido (detecta duplicados).
        self.estadisticas = self._nuevas_estadisticas()

    @staticmethod
    def _nuevas_estadisticas():
        """Diccionario "en blanco" para llevar la cuenta de una corrida:
        cuántos archivos había, cuántos se procesaron, cuántos duplicados y
        errores hubo, y qué categorías se usaron."""
        return {
            "total_archivos": 0,
            "procesados": 0,
            "duplicados": 0,
            "errores": 0,
            "carpetas_vacias_eliminadas": 0,
            "archivos_vacios": [],  # Rutas de archivos de 0 bytes encontrados (se avisa al final).
            "categorias_usadas": set(),
        }

    def reset(self):
        """Limpia cachés y estadísticas para empezar una organización nueva
        desde cero (se llama antes de cada corrida)."""
        self._receipt_detection_cache = {}
        self._date_reference_cache = {}
        self._ocr_text_cache = {}
        self.hashes_archivos = {}
        self.estadisticas = self._nuevas_estadisticas()

    def calcular_hash_archivo(self, ruta_archivo):
        """Calcula el hash SHA-256 del contenido de un archivo, leyéndolo
        en bloques de 4 KB (para no cargar archivos grandes enteros en
        memoria). Dos archivos con el mismo hash tienen el mismo contenido
        byte a byte; así es como se detectan duplicados. Devuelve `None`
        si el archivo no se puede leer."""
        hash_obj = hashlib.sha256()
        try:
            with open(ruta_archivo, "rb") as archivo:
                for bloque in iter(lambda: archivo.read(4096), b""):
                    hash_obj.update(bloque)
            return hash_obj.hexdigest()
        except OSError:
            return None

    def es_imagen(self, extension):
        """True si la extensión (por ejemplo `.jpg`) corresponde a una
        imagen conocida."""
        return extension.lower() in self.image_extensions

    def obtener_categoria(self, extension, nombre_archivo="", ruta_archivo=None, detectar_recibos=False):
        """Decide a qué categoría pertenece un archivo.

        Orden de decisión:
        1. Si la detección de recibos está activada y es una imagen que
           parece un recibo/factura -> "Recibos".
        2. Si su extensión está en el mapa `self.categorias` -> esa
           categoría.
        3. Si no, se intenta adivinar por palabras clave en el nombre
           (instaladores, licencias/readme, copias de seguridad...).
        4. Si nada aplica -> "Otros".
        """
        extension = extension.lower()
        if detectar_recibos and ruta_archivo is not None and self.es_imagen(extension) and self.es_recibo(ruta_archivo, nombre_archivo):
            return "Recibos"
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

    def obtener_nombre_categoria_ui(self, categoria):
        """Nombre "bonito" de la categoría para mostrar en pantalla (por
        ejemplo "Codigo" -> "Código"); si no hay traducción, devuelve el
        nombre interno tal cual."""
        return self.categorias_ui.get(categoria, categoria)

    def construir_ruta_destino(
        self,
        destino_base,
        ruta_archivo,
        organizar_por_categoria=True,
        organizar_por_fecha=True,
        detectar_recibos=False,
    ):
        """Calcula la carpeta de destino final para un archivo, combinando
        (según lo que esté activado) año/mes de la fecha de referencia y
        categoría. Por ejemplo: `destino/2024/03 - Marzo/Imagenes/`.
        Devuelve tanto la categoría detectada como la ruta completa."""
        categoria = self.obtener_categoria(
            ruta_archivo.suffix,
            ruta_archivo.name,
            ruta_archivo=ruta_archivo,
            detectar_recibos=detectar_recibos,
        )
        ruta_destino = Path(destino_base)
        if organizar_por_fecha:
            fecha_referencia = self.obtener_fecha_referencia(ruta_archivo)
            ruta_destino = ruta_destino / str(fecha_referencia.year) / self.formatear_carpeta_mes(fecha_referencia)
        if organizar_por_categoria:
            ruta_destino = ruta_destino / categoria
        return categoria, ruta_destino

    @staticmethod
    def formatear_carpeta_mes(fecha_referencia):
        """Nombre de la subcarpeta del mes, por ejemplo "03 - Marzo"."""
        return MONTH_FOLDER_NAMES.get(fecha_referencia.month, f"{fecha_referencia.month:02d}")

    def obtener_fecha_referencia(self, ruta_archivo):
        """Fecha que se usa para decidir en qué carpeta año/mes va el
        archivo. Se intenta en este orden (y se cachea el resultado):
        1. Fecha EXIF de la foto (si es una imagen con esos metadatos).
        2. Fecha detectada en el propio nombre del archivo.
        3. Fecha de última modificación del archivo en disco.
        4. Si todo falla, la fecha/hora actual.
        """
        cache_key = str(ruta_archivo)
        if cache_key in self._date_reference_cache:
            return self._date_reference_cache[cache_key]

        fecha_referencia = None
        if self.es_imagen(ruta_archivo.suffix):
            fecha_referencia = self._extraer_fecha_exif(ruta_archivo)
        if fecha_referencia is None:
            fecha_referencia = self._extraer_fecha_desde_nombre(ruta_archivo.stem)
        if fecha_referencia is None:
            try:
                fecha_referencia = dt.datetime.fromtimestamp(ruta_archivo.stat().st_mtime)
            except OSError:
                fecha_referencia = dt.datetime.now()
        self._date_reference_cache[cache_key] = fecha_referencia
        return fecha_referencia

    def _extraer_fecha_exif(self, ruta_archivo):
        """Intenta leer la fecha en que se tomó la foto desde sus metadatos
        EXIF (`DateTimeOriginal`, luego `DateTimeDigitized`, luego
        `DateTime`, en ese orden de preferencia). Devuelve `None` si no hay
        Pillow instalado, no es imagen, o no tiene esos metadatos."""
        if Image is None or not self.es_imagen(ruta_archivo.suffix):
            return None
        try:
            with Image.open(ruta_archivo) as imagen:
                exif_data = imagen.getexif()
        except (OSError, ValueError, UnidentifiedImageError):
            return None
        if not exif_data:
            return None

        for tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            tag_id = self._exif_tag_ids.get(tag_name)
            if tag_id is None:
                continue
            fecha = self._parsear_fecha_candidata(exif_data.get(tag_id))
            if fecha is not None:
                return fecha
        return None

    def _extraer_fecha_desde_nombre(self, nombre_archivo):
        """Busca una fecha con forma AAAA-MM-DD o DD-MM-AAAA dentro del
        nombre del archivo (por ejemplo `factura_2024-03-15.pdf`)."""
        for patron in self.filename_date_patterns:
            match = patron.search(nombre_archivo)
            if not match:
                continue
            try:
                return dt.datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
        return None

    @staticmethod
    def _parsear_fecha_candidata(raw_value):
        """Convierte un valor EXIF crudo (texto o bytes, en varios formatos
        posibles de fecha/hora) en un `datetime` de Python, o `None` si no
        se pudo interpretar de ninguna forma conocida."""
        if not raw_value:
            return None
        if isinstance(raw_value, bytes):
            for encoding in ("utf-8", "latin-1"):
                try:
                    raw_value = raw_value.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                return None

        texto = str(raw_value).strip()
        if not texto:
            return None
        texto = texto.replace("T", " ")
        if len(texto) >= 19:
            candidato = texto[:19]
            for formato in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return dt.datetime.strptime(candidato, formato)
                except ValueError:
                    continue
        for formato in ("%Y:%m:%d", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(texto[:10], formato)
            except ValueError:
                continue
        return None

    def es_recibo(self, ruta_archivo, nombre_archivo=""):
        """Decide si una imagen es probablemente un recibo/factura.

        Primero revisa si el nombre del archivo o de su carpeta ya lo
        delata (por ejemplo "factura_super.jpg"). Si no, y hay OCR
        disponible, extrae el texto de la imagen y aplica una fórmula de
        puntaje: 2 o más palabras "primarias" (total, IVA, NIT...), o 1
        primaria más 2 secundarias / un monto con moneda / algo con forma
        de fecha. El resultado se guarda en caché por archivo.
        """
        cache_key = str(ruta_archivo)
        if cache_key in self._receipt_detection_cache:
            return self._receipt_detection_cache[cache_key]

        if not self.es_imagen(ruta_archivo.suffix):
            self._receipt_detection_cache[cache_key] = False
            return False

        texto_base = self._normalizar_texto(f"{ruta_archivo.parent.name} {nombre_archivo or ruta_archivo.name}")
        if any(keyword in texto_base for keyword in self.receipt_keywords):
            self._receipt_detection_cache[cache_key] = True
            return True

        texto_ocr = self._extraer_texto_imagen(ruta_archivo)
        if not texto_ocr:
            self._receipt_detection_cache[cache_key] = False
            return False

        primarias = sum(1 for keyword in self.receipt_primary_keywords if keyword in texto_ocr)
        secundarias = sum(1 for keyword in self.receipt_secondary_keywords if keyword in texto_ocr)
        tiene_importe = bool(self.receipt_amount_pattern.search(texto_ocr))
        tiene_fecha = bool(self.receipt_date_pattern.search(texto_ocr))
        es_recibo = primarias >= 2 or (primarias >= 1 and (secundarias >= 2 or tiene_importe or tiene_fecha))
        self._receipt_detection_cache[cache_key] = es_recibo
        return es_recibo

    def _extraer_texto_imagen(self, ruta_archivo):
        """Ejecuta OCR (Tesseract, español+inglés) sobre una imagen y
        devuelve el texto reconocido, normalizado. Antes de reconocer,
        convierte la imagen a escala de grises y la agranda si es pequeña
        (el OCR funciona mejor con imágenes de al menos ~1600px de lado),
        lo cual mejora la precisión sin modificar el archivo original.
        Cualquier error (imagen corrupta, Tesseract no instalado, etc.) se
        traduce simplemente en texto vacío. Resultado cacheado por archivo.
        """
        cache_key = str(ruta_archivo)
        if cache_key in self._ocr_text_cache:
            return self._ocr_text_cache[cache_key]
        if Image is None or pytesseract is None:
            self._ocr_text_cache[cache_key] = ""
            return ""

        try:
            with Image.open(ruta_archivo) as imagen:
                procesada = imagen.convert("L")
                ancho, alto = procesada.size
                lado_mayor = max(ancho, alto, 1)
                if lado_mayor < 1600:
                    factor = max(1, round(1600 / lado_mayor))
                    if factor > 1:
                        procesada = procesada.resize((ancho * factor, alto * factor))
                texto = pytesseract.image_to_string(procesada, lang="spa+eng")
        except Exception:
            texto = ""

        texto_normalizado = self._normalizar_texto(texto)
        self._ocr_text_cache[cache_key] = texto_normalizado
        return texto_normalizado

    def _normalizar_texto(self, valor):
        """Pasa un texto a minúsculas, sin espacios sobrantes y sin
        tildes/ñ, para que las comparaciones de palabras clave no dependan
        de cómo estén escritas exactamente."""
        return str(valor or "").strip().lower().translate(self._text_translation)

    def generar_nombre_unico(self, ruta_destino, nombre_original):
        """Si `nombre_original` ya existe en `ruta_destino`, genera un
        nombre alternativo agregando una marca de tiempo (y un contador si
        hiciera falta), para nunca sobrescribir un archivo existente."""
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
        """Clave de ordenamiento para listar archivos: los que empiezan
        con número van primero y se ordenan numéricamente (para que
        "2.txt" quede antes que "10.txt", en vez de orden alfabético puro);
        el resto se ordena alfabéticamente después."""
        nombre_sin_ext = Path(nombre_archivo).stem.lower()
        if nombre_sin_ext and nombre_sin_ext[0].isdigit():
            match = re.match(r"^(\d+)", nombre_sin_ext)
            return (0, int(match.group(1)) if match else 0, nombre_sin_ext)
        return (1, nombre_sin_ext, nombre_sin_ext)

    def obtener_todas_extensiones(self):
        """Lista ordenada de todas las extensiones conocidas por la app
        (unión de todas las categorías); se usa para poblar el filtro de
        "tipo de archivo" en la interfaz."""
        extensiones = set()
        for lista in self.categorias.values():
            extensiones.update(lista)
        return sorted(extensiones)


class InvoiceExtractor:
    """Extrae campos comunes desde facturas en imagen, PDF digital o texto.

    Es la clase detrás de la pestaña "Facturas" de la interfaz: recibe la
    ruta de una factura (foto, PDF o texto plano), obtiene su texto (con
    OCR si es imagen, o con `pypdf` si es un PDF con texto digital) y luego
    usa expresiones regulares + palabras clave para intentar rellenar
    proveedor, número de factura, fecha, NIT, correo, subtotal, IVA y
    total. Todo esto es una heurística (no hay garantía de acierto), por
    eso cada resultado incluye un porcentaje de "confianza" calculado en
    `_confidence_score` según cuántos campos se lograron completar.
    """

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
    PDF_EXTENSIONS = {".pdf"}
    TEXT_EXTENSIONS = {".txt", ".csv", ".xml"}
    SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | TEXT_EXTENSIONS

    # Palabras clave (en minúscula, sin tildes) que ayudan a ubicar cada
    # campo dentro del texto extraído de la factura.
    INVOICE_LABELS = (
        "factura electronica de venta",
        "factura de venta",
        "factura",
        "invoice",
        "comprobante",
    )
    NUMBER_LABELS = (
        "numero",
        "nro",
        "no",
        "num",
        "factura",
        "invoice",
        "consecutivo",
    )
    TOTAL_LABELS = (
        "total a pagar",
        "valor total",
        "total factura",
        "total",
        "importe total",
        "amount due",
    )
    SUBTOTAL_LABELS = (
        "subtotal",
        "sub total",
        "base gravable",
        "base imponible",
        "valor bruto",
    )
    TAX_LABELS = (
        "iva",
        "impuesto",
        "impuestos",
        "tax",
        "vat",
    )

    # Patrón de fecha: DD/MM/AAAA (o con "-"/".") o AAAA/MM/DD.
    date_pattern = re.compile(
        r"\b("
        r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}"
        r"|"
        r"\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}"
        r")\b"
    )
    # Patrón de "monto de dinero": número con separadores de miles/decimales,
    # opcionalmente con un código de moneda o el símbolo "$" antes o después.
    amount_pattern = re.compile(r"(?:COP|USD|EUR|MXN|ARS|CLP|\$)?\s*[-+]?\d[\d.,]*(?:\s*(?:COP|USD|EUR|MXN|ARS|CLP))?", re.IGNORECASE)
    # Identificación tributaria: NIT, RUC, CUIT, RFC, CIF... seguida del número.
    id_pattern = re.compile(r"\b(?:NIT|RUC|CUIT|RFC|CIF|ID|Identificacion)\s*[:#\-]?\s*([0-9A-Z.\-]{5,})", re.IGNORECASE)
    # Número de factura: "Factura No. 12345", "Invoice #ABC-99", etc.
    invoice_number_pattern = re.compile(
        r"\b(?:Factura|Invoice|No\.?|Nro\.?|Numero|Num\.?|Consecutivo)\s*(?:No\.?|Nro\.?|Numero|#|:|-)?\s*([A-Z0-9][A-Z0-9\-_.]{2,})",
        re.IGNORECASE,
    )
    email_pattern = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

    def __init__(self):
        self._ocr_cache = {}  # texto OCR ya calculado, por ruta de archivo.

    def is_supported(self, path):
        """True si la extensión del archivo es una de las que esta clase
        sabe leer (imagen, PDF o texto plano)."""
        return Path(path).suffix.lower() in self.SUPPORTED_EXTENSIONS

    def extract_many(self, paths):
        """Aplica `extract` a una lista de rutas y devuelve la lista de
        resultados en el mismo orden (se usa al seleccionar varias
        facturas a la vez en la interfaz)."""
        results = []
        for path in paths:
            results.append(self.extract(path))
        return results

    def extract(self, path):
        """Procesa una factura y devuelve un diccionario con todos los
        campos encontrados (o vacíos si no se pudieron detectar), más
        `confianza` (0-100%) y `estado` ("ok", "parcial", "sin_texto" o
        un mensaje de error)."""
        invoice_path = Path(path)
        result = {
            "archivo": invoice_path.name,
            "ruta": str(invoice_path),
            "proveedor": "",
            "numero": "",
            "fecha": "",
            "nit": "",
            "email": "",
            "subtotal": "",
            "iva": "",
            "total": "",
            "confianza": "0%",
            "estado": "sin_texto",
            "texto": "",
        }

        try:
            text = self.extract_text(invoice_path)
        except Exception as exc:
            result["estado"] = f"error: {exc}"
            return result

        normalized_text = self._normalize_text(text)
        result["texto"] = normalized_text[:4000]
        if not normalized_text:
            return result

        lines = self._clean_lines(normalized_text)
        result.update(
            {
                "proveedor": self._extract_supplier(lines),
                "numero": self._extract_invoice_number(normalized_text, lines),
                "fecha": self._extract_date(normalized_text, lines),
                "nit": self._extract_tax_id(normalized_text),
                "email": self._extract_email(normalized_text),
                "subtotal": self._find_labeled_amount(lines, self.SUBTOTAL_LABELS),
                "iva": self._find_labeled_amount(lines, self.TAX_LABELS),
                "total": self._extract_total(lines),
            }
        )
        score = self._confidence_score(result)
        result["confianza"] = f"{score}%"
        result["estado"] = "ok" if score >= 65 else "parcial"
        return result

    def extract_text(self, path):
        """Obtiene el texto crudo de la factura según su tipo de archivo:
        OCR para imágenes, extracción de texto de PDF, o lectura directa
        si ya es un archivo de texto. Lanza `ValueError` si la extensión
        no está soportada."""
        suffix = path.suffix.lower()
        if suffix in self.IMAGE_EXTENSIONS:
            return self._extract_image_text(path)
        if suffix in self.PDF_EXTENSIONS:
            return self._extract_pdf_text(path)
        if suffix in self.TEXT_EXTENSIONS:
            return path.read_text(encoding="utf-8", errors="ignore")
        raise ValueError("Formato no soportado")

    def _extract_image_text(self, path):
        """OCR de una imagen de factura (más agresivo que el de
        `ModernOrganizadorArchivos`: agranda hasta 1800px de lado en vez de
        1600, porque aquí se necesita leer números y textos pequeños con
        precisión, no solo detectar palabras clave)."""
        if Image is None or pytesseract is None:
            raise RuntimeError("Falta instalar Pillow o Tesseract OCR para leer imagenes.")
        cache_key = str(path)
        if cache_key in self._ocr_cache:
            return self._ocr_cache[cache_key]
        with Image.open(path) as image:
            processed = image.convert("L")
            width, height = processed.size
            longest_side = max(width, height, 1)
            if longest_side < 1800:
                scale = max(1, round(1800 / longest_side))
                if scale > 1:
                    processed = processed.resize((width * scale, height * scale))
            text = pytesseract.image_to_string(processed, lang="spa+eng")
        self._ocr_cache[cache_key] = text
        return text

    def _extract_pdf_text(self, path):
        """Extrae el texto "digital" (no escaneado) de un PDF usando
        `pypdf`, limitado a las primeras 5 páginas (las facturas casi
        siempre tienen toda la información relevante al inicio, y así se
        evita procesar PDFs largos innecesariamente)."""
        if PdfReader is None:
            raise RuntimeError("Falta instalar pypdf para leer PDFs digitales.")
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages[:5]:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)

    @staticmethod
    def _normalize_text(text):
        """Colapsa espacios/tabs repetidos y unifica saltos de línea."""
        return re.sub(r"[ \t]+", " ", str(text or "")).replace("\r", "\n").strip()

    @staticmethod
    def _clean_lines(text):
        """Divide el texto en líneas, descartando las que quedan vacías."""
        return [line.strip() for line in text.splitlines() if line.strip()]

    @staticmethod
    def _fold(text):
        """Versión en minúsculas y sin tildes de un texto, solo para
        comparar contra palabras clave (el texto original no se modifica)."""
        translation = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
        return str(text or "").translate(translation).lower()

    def _extract_supplier(self, lines):
        """Adivina el nombre del proveedor: recorre las primeras 12 líneas
        y devuelve la primera que no sea una etiqueta conocida (factura,
        NIT, fecha...), no sea muy corta, y no parezca en sí misma un monto
        o una fecha. Es una heurística simple: en la mayoría de facturas el
        nombre del negocio aparece cerca del encabezado."""
        ignored = ("factura", "invoice", "nit", "ruc", "fecha", "total", "subtotal", "iva", "www.", "http")
        for line in lines[:12]:
            folded = self._fold(line)
            if len(line) < 4 or any(token in folded for token in ignored):
                continue
            if self.amount_pattern.search(line) or self.date_pattern.search(line):
                continue
            return line[:120]
        return ""

    def _extract_invoice_number(self, text, lines):
        """Busca el número de factura: primero en las líneas que contienen
        una etiqueta relevante ("número", "factura", "consecutivo"...), y
        si no aparece ahí, como último recurso en todo el texto."""
        for line in lines[:30]:
            folded = self._fold(line)
            if not any(label in folded for label in self.NUMBER_LABELS):
                continue
            match = self.invoice_number_pattern.search(line)
            if match:
                return match.group(1).strip(" .:-#")
        match = self.invoice_number_pattern.search(text)
        if match:
            return match.group(1).strip(" .:-#")
        return ""

    def _extract_date(self, text, lines):
        """Busca la fecha de la factura: prioriza líneas que mencionan
        "fecha"/"date"/"emisión"; si ninguna tiene una fecha reconocible,
        busca cualquier fecha en todo el texto."""
        for line in lines[:40]:
            folded = self._fold(line)
            if "fecha" in folded or "date" in folded or "emision" in folded:
                match = self.date_pattern.search(line)
                if match:
                    return match.group(1)
        match = self.date_pattern.search(text)
        return match.group(1) if match else ""

    def _extract_tax_id(self, text):
        """Busca un NIT/RUC/RFC/CUIT/CIF en el texto completo."""
        match = self.id_pattern.search(text)
        return match.group(1).strip(" .:-#") if match else ""

    def _extract_email(self, text):
        """Busca la primera dirección de correo que aparezca en el texto."""
        match = self.email_pattern.search(text)
        return match.group(0) if match else ""

    def _extract_total(self, lines):
        """Busca el total de la factura: primero cerca de una etiqueta
        como "total a pagar" (tomando el último monto encontrado, porque
        el total real suele ser el último número de esa línea); si no hay
        ninguna etiqueta de total, usa el último monto de dinero que
        aparezca en todo el documento como aproximación."""
        total = self._find_labeled_amount(lines, self.TOTAL_LABELS, prefer_last=True)
        if total:
            return total
        all_amounts = []
        for line in lines:
            all_amounts.extend(self._extract_amounts(line))
        if not all_amounts:
            return ""
        return all_amounts[-1]

    def _find_labeled_amount(self, lines, labels, prefer_last=False):
        """Busca montos de dinero solo en las líneas que contienen alguna
        de las `labels` dadas (por ejemplo, las etiquetas de subtotal o de
        IVA), y devuelve el primero o el último encontrado según
        `prefer_last`."""
        candidates = []
        for line in lines:
            folded = self._fold(line)
            if any(label in folded for label in labels):
                amounts = self._extract_amounts(line)
                if amounts:
                    candidates.extend(amounts)
        if not candidates:
            return ""
        return candidates[-1] if prefer_last else candidates[0]

    def _extract_amounts(self, line):
        """Encuentra todos los montos de dinero en una línea de texto y
        los normaliza a un formato numérico consistente."""
        amounts = []
        for raw in self.amount_pattern.findall(line):
            normalized = self._normalize_money(raw)
            if normalized:
                amounts.append(normalized)
        return amounts

    @staticmethod
    def _normalize_money(raw):
        """Convierte un texto de monto (que puede venir en formato
        colombiano "1.234.567,89" o en formato inglés "1,234,567.89",
        entre otros) a un número con formato consistente "1,234,567.89".

        La parte delicada es decidir cuál símbolo (`,` o `.`) es el
        separador decimal cuando aparecen los dos: se asume que el que
        aparece MÁS A LA DERECHA es el decimal (así "1.234,56" y
        "1,234.56" se interpretan correctamente). Si el texto no resulta
        ser un número válido al final, se devuelve tal cual llegó.
        """
        text = re.sub(r"(?i)\b(COP|USD|EUR|MXN|ARS|CLP)\b", "", str(raw or ""))
        text = text.replace("$", "").strip()
        text = re.sub(r"[^0-9,.\-]", "", text)
        if not re.search(r"\d", text):
            return ""
        if "," in text and "." in text:
            decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
            thousand_separator = "." if decimal_separator == "," else ","
            text = text.replace(thousand_separator, "").replace(decimal_separator, ".")
        elif "," in text:
            parts = text.split(",")
            text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) <= 2 else "".join(parts)
        elif "." in text:
            parts = text.split(".")
            text = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) <= 2 else "".join(parts)
        try:
            return f"{float(text):,.2f}"
        except ValueError:
            return raw.strip()

    @staticmethod
    def _confidence_score(result):
        """Porcentaje de "confianza" del resultado: qué proporción de los
        7 campos importantes se logró completar. No mide si los valores
        son correctos, solo si se encontró algo para cada campo."""
        important_fields = ("proveedor", "numero", "fecha", "nit", "subtotal", "iva", "total")
        found = sum(1 for field in important_fields if result.get(field))
        return round((found / len(important_fields)) * 100)


class LicenseManager:
    """Gestiona la licencia local, guardada en `license.json` dentro de la
    carpeta de datos de la app (ver `get_app_storage_dir`).

    Guarda cosas como: si hay una licencia activa, a qué correo y plan
    corresponde, cuándo vence la suscripción, y cuántos usos mensuales
    lleva consumidos (para los planes con límite). En Windows, el archivo
    se cifra con la API DPAPI de Windows (`CryptProtectData`), que solo el
    mismo usuario/equipo puede descifrar; así el archivo no sirve si se
    copia a otro computador (ver `_invalidate_copied_state`). En macOS no
    existe DPAPI: el archivo se guarda sin cifrar (solo legible por el
    usuario, permisos 0600) y la copia a otro Mac se detecta igualmente con
    `get_device_id`.
    """

    def __init__(self):
        base_dir = get_app_storage_dir()
        self.license_file = base_dir / "license.json"
        self.state = self._load_state()

    @staticmethod
    def _encrypt_local_state(payload: bytes) -> bytes:
        """Cifra los bytes del estado de licencia usando DPAPI de Windows
        (atada al usuario de Windows actual). Fuera de Windows, o si algo
        falla, se guarda el contenido sin cifrar en vez de fallar."""
        if os.name != "nt":
            return payload
        try:
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            buffer = ctypes.create_string_buffer(payload)
            blob_in = DATA_BLOB(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
            blob_out = DATA_BLOB()
            if not crypt32.CryptProtectData(
                ctypes.byref(blob_in),
                APP_NAME,
                None,
                None,
                None,
                0x01,
                ctypes.byref(blob_out),
            ):
                return payload
            try:
                encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                kernel32.LocalFree(blob_out.pbData)
            wrapper = {"__protected__": True, "encoding": "base64", "data": base64.b64encode(encrypted).decode("ascii")}
            return json.dumps(wrapper, indent=2).encode("utf-8")
        except Exception:
            return payload

    @staticmethod
    def _decrypt_local_state(payload: bytes) -> bytes:
        """Inverso de `_encrypt_local_state`: si el contenido está marcado
        como protegido (`__protected__`), lo descifra con DPAPI; si no,
        asume que ya es JSON en texto plano (compatibilidad con archivos
        antiguos o de otros sistemas operativos) y lo devuelve tal cual."""
        try:
            decoded = json.loads(payload.decode("utf-8"))
            if not (isinstance(decoded, dict) and decoded.get("__protected__") and decoded.get("data")):
                return payload
            if os.name != "nt":
                return payload
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

            encrypted = base64.b64decode(decoded["data"])
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            buffer = ctypes.create_string_buffer(encrypted)
            blob_in = DATA_BLOB(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
            blob_out = DATA_BLOB()
            if not crypt32.CryptUnprotectData(
                ctypes.byref(blob_in),
                None,
                None,
                None,
                None,
                0x01,
                ctypes.byref(blob_out),
            ):
                return payload
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                kernel32.LocalFree(blob_out.pbData)
        except Exception:
            return payload

    def _invalidate_copied_state(self, state):
        """Si el archivo de licencia fue copiado desde otro equipo (el
        `device_id` guardado no coincide con el de este equipo), se
        invalida la licencia: no tendría sentido que copiar un archivo
        activara la app en un computador distinto al que la compró."""
        stored_device_id = state.get("device_id", "")
        current_device_id = self.get_device_id()
        if stored_device_id and stored_device_id != current_device_id:
            state.update(
                {
                    "license_key": "",
                    "email": "",
                    "status": "inactive",
                    "subscription_status": "inactive",
                    "subscription_expires_at": "",
                    "subscription_warning_7_for": "",
                    "subscription_warning_3_for": "",
                    "device_id": current_device_id,
                }
            )
        elif not stored_device_id:
            state["device_id"] = current_device_id
        return state

    def _load_state(self):
        """Lee (y descifra) el estado de licencia guardado en disco. Si no
        existe o está corrupto, crea un estado nuevo "inactivo". Los
        `setdefault` permiten que, si se actualiza la app y se agregan
        campos nuevos, un archivo de licencia viejo se complete con
        valores por defecto en vez de fallar."""
        if self.license_file.exists():
            try:
                raw = self.license_file.read_bytes()
                state = json.loads(self._decrypt_local_state(raw).decode("utf-8"))
                state.setdefault("subscription_id", "")
                state.setdefault("subscription_email", "")
                state.setdefault("subscription_plan_code", "")
                state.setdefault("subscription_status", "inactive")
                state.setdefault("subscription_expires_at", "")
                state.setdefault("subscription_warning_7_for", "")
                state.setdefault("subscription_warning_3_for", "")
                state.setdefault("monthly_usage", {})
                state.setdefault("monthly_uses", {})
                state = self._invalidate_copied_state(state)
                self._save_state(state)
                return state
            except (json.JSONDecodeError, OSError):
                pass
        state = {
            "installed_at": dt.datetime.utcnow().isoformat(),
            "license_key": "",
            "email": "",
            "status": "inactive",
            "last_validation": "",
            "device_id": self.get_device_id(),
            "subscription_id": "",
            "subscription_email": "",
            "subscription_plan_code": "",
            "subscription_status": "inactive",
            "subscription_expires_at": "",
            "subscription_warning_7_for": "",
            "subscription_warning_3_for": "",
            "monthly_usage": {},
            "monthly_uses": {},
        }
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        """Guarda (cifrado y de forma atómica) el estado de licencia
        actual en disco."""
        if state is not None:
            self.state = state
        payload = json.dumps(self.state, indent=2).encode("utf-8")
        atomic_write_bytes(self.license_file, self._encrypt_local_state(payload))

    @staticmethod
    def get_device_id():
        """Identificador estable de este equipo, derivado del sistema
        operativo, el nombre de la máquina y la dirección MAC (`uuid.getnode`),
        reducido a un hash. Se usa para el límite de "activaciones por
        licencia" y para detectar si un archivo de licencia fue copiado de
        otro equipo.

        En macOS se usa el UUID de hardware del Mac: `platform.node()` (el
        nombre de red) y la MAC (`uuid.getnode`) cambian según el Wi-Fi o la
        VPN, y eso invalidaría la licencia cada vez que el usuario cambie de
        red. En Windows la fórmula original no cambia (para no invalidar las
        licencias ya activadas)."""
        raw = f"{platform.system()}|{platform.node()}|{uuid.getnode()}"
        if IS_MAC:
            hardware_uuid = mac_hardware_uuid()
            if hardware_uuid:
                raw = f"{platform.system()}|{hardware_uuid}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def get_machine_name():
        """Nombre del equipo en la red (usado para detectar el modo
        desarrollador, ver `is_developer_access_enabled`)."""
        return (os.getenv("COMPUTERNAME") or platform.node() or "").strip()

    def is_owner_device(self):
        """Deshabilitado a propósito: la app distribuida nunca debe traer
        embebida una identidad de equipo con acceso privilegiado. Siempre
        devuelve `False` en el cliente que reciben los usuarios."""
        # Never embed privileged device identities in a distributable client.
        return False

    @staticmethod
    def _parse_datetime(value):
        """Convierte un texto ISO-8601 (posiblemente con "Z" al final) en
        un `datetime`, o `None` si está vacío o no es válido."""
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def is_developer_access_enabled(self):
        """True si `FILESYNC_PRO_DEV_MODE` está activado en el entorno, o
        si el nombre del equipo coincide con el del equipo de desarrollo
        (`DEVELOPER_HOSTNAME`). Sirve para probar la app sin licencia
        durante el desarrollo."""
        return DEV_MODE_ENABLED or self.get_machine_name().lower() == DEVELOPER_HOSTNAME.lower()

    def local_unrestricted_access_mode(self):
        """Devuelve "owner", "developer" o cadena vacía, según por qué
        razón (si aplica) este equipo tiene acceso sin restricciones sin
        necesidad de una licencia real."""
        if self.is_owner_device():
            return "owner"
        if self.is_developer_access_enabled():
            return "developer"
        return ""

    def has_local_unrestricted_access(self):
        """Atajo booleano sobre `local_unrestricted_access_mode`."""
        return bool(self.local_unrestricted_access_mode())

    def days_left_in_trial(self):
        """Días restantes del periodo de prueba gratuito (actualmente
        `TRIAL_DAYS = 0`, así que siempre devuelve 0: no hay prueba)."""
        if TRIAL_DAYS <= 0:
            return 0
        installed_at = dt.datetime.fromisoformat(self.state["installed_at"])
        elapsed = (dt.datetime.utcnow() - installed_at).days
        return max(TRIAL_DAYS - elapsed, 0)

    def is_feature_unlocked(self):
        """¿Puede el usuario usar las funciones de la app ahora mismo?
        Sí, si tiene acceso sin restricciones (developer/owner), o si su
        licencia está activa y no vencida. Si estaba activa pero la
        suscripción ya venció, se marca como inactiva en este mismo
        momento (efecto colateral: además de responder, actualiza y
        guarda el estado)."""
        if self.has_local_unrestricted_access():
            return True
        if self.state.get("status") != "active":
            return False
        if self.is_subscription_expired():
            self.state["status"] = "inactive"
            self.state["subscription_status"] = "inactive"
            self._save_state()
            return False
        return True

    def activate_local(self, email, license_key, subscription_id="", subscription_plan_code="", subscription_expires_at=""):
        """Guarda localmente los datos de una licencia recién validada
        (correo, clave, plan, fecha de vencimiento). Si la fecha de
        vencimiento cambió, se resetean las banderas de aviso de "faltan
        7/3 días" para que vuelvan a mostrarse si corresponde más
        adelante."""
        previous_expiration = self.state.get("subscription_expires_at", "")
        next_expiration = subscription_expires_at or previous_expiration
        self.state.update(
            {
                "email": email,
                "license_key": license_key,
                "status": "active",
                "last_validation": dt.datetime.utcnow().isoformat(),
                "device_id": self.get_device_id(),
                "subscription_id": subscription_id or self.state.get("subscription_id", ""),
                "subscription_email": email,
                "subscription_plan_code": subscription_plan_code or self.state.get("subscription_plan_code", ""),
                "subscription_status": "active",
                "subscription_expires_at": next_expiration,
            }
        )
        if next_expiration and next_expiration != previous_expiration:
            self.state["subscription_warning_7_for"] = ""
            self.state["subscription_warning_3_for"] = ""
        self._save_state()

    def set_validation_status(self, status):
        """Actualiza el estado ("active"/"inactive") tras una validación
        remota de la licencia, junto con la marca de tiempo."""
        self.state["status"] = status
        self.state["last_validation"] = dt.datetime.utcnow().isoformat()
        if self.state.get("subscription_id"):
            self.state["subscription_status"] = status
        self._save_state()

    def update_subscription_expiration(self, subscription_expires_at):
        """Actualiza solo la fecha de vencimiento guardada localmente
        (por ejemplo, tras refrescar el estado desde el servidor)."""
        normalized = (subscription_expires_at or "").strip()
        previous = self.state.get("subscription_expires_at", "")
        self.state["subscription_expires_at"] = normalized
        if normalized and normalized != previous:
            self.state["subscription_warning_7_for"] = ""
            self.state["subscription_warning_3_for"] = ""
        self._save_state()

    def is_subscription_expired(self):
        """True si hay una fecha de vencimiento guardada y ya pasó. Sin
        fecha guardada, se asume que no está vencida (por ejemplo, planes
        sin fecha de corte definida todavía)."""
        expires_at = self._parse_datetime(self.state.get("subscription_expires_at", ""))
        if not expires_at:
            return False
        now = dt.datetime.now(expires_at.tzinfo) if expires_at.tzinfo else dt.datetime.utcnow()
        return now >= expires_at

    def days_until_block(self):
        """Días que faltan para que la suscripción venza (puede ser
        negativo si ya venció), o `None` si no hay fecha de vencimiento
        guardada. Se usa para mostrar avisos de "tu plan vence en N días"."""
        expires_at = self._parse_datetime(self.state.get("subscription_expires_at", ""))
        if not expires_at:
            return None
        now = dt.datetime.now(expires_at.tzinfo) if expires_at.tzinfo else dt.datetime.utcnow()
        return (expires_at.date() - now.date()).days

    @staticmethod
    def _current_usage_period():
        """Clave del "mes actual" en formato AAAA-MM, usada para llevar el
        contador de usos mensuales por periodo."""
        return dt.datetime.utcnow().strftime("%Y-%m")

    def get_monthly_usage_info(self, plan_code, features):
        """Cuántos usos se han consumido este mes y cuántos quedan, según
        el límite (`monthly_use_limit`) del plan actual. Si el plan no
        tiene límite (Premium), devuelve `limit`/`remaining` en `None`."""
        limit = features.get("monthly_use_limit")
        if limit is None:
            return {
                "period": self._current_usage_period(),
                "used": 0,
                "remaining": None,
                "limit": None,
            }

        usage = self.state.setdefault("monthly_uses", {})
        period = self._current_usage_period()
        used = int(usage.get(period, 0) or 0)
        remaining = max(limit - used, 0)
        return {
            "period": period,
            "used": used,
            "remaining": remaining,
            "limit": limit,
        }

    def register_monthly_use(self, plan_code, features, use_count=1):
        """Suma `use_count` al contador de usos del mes actual (sin pasar
        del límite del plan), y guarda el estado. No hace nada si el plan
        no tiene límite mensual."""
        if use_count <= 0:
            return

        limit = features.get("monthly_use_limit")
        if limit is None:
            return

        usage = self.state.setdefault("monthly_uses", {})
        period = self._current_usage_period()
        used = int(usage.get(period, 0) or 0)
        usage[period] = min(used + use_count, limit)
        self._save_state()

    def remember_pending_subscription(self, email, plan_code, subscription_id=""):
        """Guarda localmente que el usuario inició una compra (correo y
        plan elegidos) mientras se espera la confirmación del pago, para
        poder retomar la verificación aunque cierre y reabra la app."""
        self.state.update(
            {
                "subscription_email": email,
                "subscription_plan_code": plan_code,
                "subscription_id": subscription_id,
                "subscription_status": "pending",
                "subscription_expires_at": "",
                "subscription_warning_7_for": "",
                "subscription_warning_3_for": "",
            }
        )
        self._save_state()

    def clear_pending_subscription(self):
        """Olvida cualquier compra pendiente que se estuviera rastreando
        (por ejemplo, tras confirmar el pago o si el usuario cancela)."""
        self.state.update(
            {
                "subscription_id": "",
                "subscription_email": "",
                "subscription_plan_code": "",
                "subscription_status": "inactive",
                "subscription_expires_at": "",
                "subscription_warning_7_for": "",
                "subscription_warning_3_for": "",
            }
        )
        self._save_state()


class ProcessRecoveryManager:
    """Persiste el estado de una organización para poder retomarla.

    Cuando la app organiza muchos archivos, el proceso puede tardar y
    podría interrumpirse (se cierra la app, se va la luz, etc.). Para no
    perder el progreso, esta clase guarda en disco, dentro de la carpeta de
    datos de la app:

    - Un "manifiesto" (`process_manifest_<id>.json`): la lista completa de
      archivos que se planeaba procesar en esa corrida.
    - Un "diario" (`process_journal_<id>.jsonl`): una línea JSON por cada
      archivo ya procesado (copiado/movido/duplicado/error), en el orden en
      que se fueron procesando.
    - Un archivo de estado (`process_state.json`) con la configuración de
      la corrida (origen, destino, opciones) y su estado general.

    Al reabrir la app, `rebuild_runtime_state` puede leer el diario y saber
    exactamente por dónde iba, para continuar sin repetir trabajo. Además,
    al terminar una corrida con éxito, se guarda una copia aparte
    ("last_run_...") que alimenta la función de "Deshacer última
    organización".
    """

    def __init__(self):
        self.base_dir = get_app_storage_dir()
        self.state_file = self.base_dir / PROCESS_STATE_FILE

    @staticmethod
    def _utc_now():
        return dt.datetime.utcnow().isoformat()

    def _load_json_file(self, path):
        """Lee un archivo JSON y devuelve `{}` si no existe o está
        corrupto, en vez de lanzar una excepción."""
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_json_file(self, path, payload):
        atomic_write_json(path, payload)

    def load_state(self):
        """Estado guardado de la corrida actual (vacío si no hay ninguna
        en curso o guardada)."""
        state = self._load_json_file(self.state_file)
        return state if isinstance(state, dict) else {}

    def _manifest_path_for(self, run_id):
        return self.base_dir / f"{PROCESS_MANIFEST_PREFIX}{run_id}.json"

    def _journal_path_for(self, run_id):
        return self.base_dir / f"{PROCESS_JOURNAL_PREFIX}{run_id}.jsonl"

    def clear_run_files(self, state=None):
        """Borra el manifiesto y el diario de una corrida (pero no el
        propio `process_state.json`; para eso está `clear()`)."""
        current_state = state if isinstance(state, dict) and state else self.load_state()
        for key in ("manifest_path", "journal_path"):
            raw_path = str(current_state.get(key, "")).strip()
            if not raw_path:
                continue
            try:
                safe_unlink(raw_path, self.base_dir)
            except (OSError, PathSafetyError):
                pass

    def clear(self):
        """Borra por completo el rastro de la corrida actual (manifiesto,
        diario y el archivo de estado). Se usa cuando ya no hace falta
        poder reanudarla (terminó bien, o el usuario decidió no reanudar)."""
        state = self.load_state()
        self.clear_run_files(state)
        try:
            safe_unlink(self.state_file, self.base_dir)
        except (OSError, PathSafetyError):
            pass

    def create_run(self, config, files):
        """Empieza a rastrear una nueva corrida: borra cualquier rastro de
        una corrida anterior, genera un `run_id` único, escribe el
        manifiesto con la lista completa de archivos a procesar, crea un
        diario vacío, y guarda el estado inicial (configuración elegida +
        `status: "running"`). Devuelve ese estado."""
        self.clear()
        run_id = uuid.uuid4().hex
        manifest_path = self._manifest_path_for(run_id)
        journal_path = self._journal_path_for(run_id)
        created_at = self._utc_now()

        manifest = {
            "run_id": run_id,
            "created_at": created_at,
            "files": [str(Path(file_path)) for file_path in files],
        }
        self._save_json_file(manifest_path, manifest)
        journal_path.write_text("", encoding="utf-8")

        state = {
            "run_id": run_id,
            "status": "running",
            "created_at": created_at,
            "updated_at": created_at,
            "manifest_path": str(manifest_path),
            "journal_path": str(journal_path),
            "origen": config["origen"],
            "destino": config["destino"],
            "organizar_por_categoria": bool(config["organizar_por_categoria"]),
            "organizar_por_fecha": bool(config.get("organizar_por_fecha", True)),
            "detectar_recibos": bool(config.get("detectar_recibos", True)),
            "eliminar_duplicados": bool(config["eliminar_duplicados"]),
            "mover_en_vez_de_copiar": bool(config["mover_en_vez_de_copiar"]),
            "eliminar_carpetas_vacias": bool(config.get("eliminar_carpetas_vacias", False)),
            "organizar_todos": bool(config["organizar_todos"]),
            "tipo_archivo": config["tipo_archivo"],
            "limite_ejecucion": config["limite_ejecucion"],
            "total_archivos": len(files),
        }
        self._save_json_file(self.state_file, state)
        return state

    def update_state(self, **fields):
        """Actualiza campos sueltos del estado de la corrida actual (por
        ejemplo el progreso: cuántos archivos van procesados) y lo guarda."""
        state = self.load_state()
        if not state:
            return {}
        state.update(fields)
        state["updated_at"] = self._utc_now()
        self._save_json_file(self.state_file, state)
        return state

    def mark_interrupted(self):
        """Marca la corrida como "interrumpida" (no llegó a completarse),
        a menos que ya estuviera marcada como completada. Se llama, por
        ejemplo, si el usuario cancela o cierra la app a mitad de proceso,
        para que la próxima vez se le ofrezca reanudarla."""
        state = self.load_state()
        if not state:
            return {}
        if state.get("status") == "completed":
            return state
        state["status"] = "interrupted"
        state["updated_at"] = self._utc_now()
        self._save_json_file(self.state_file, state)
        return state

    def mark_completed(self, summary=None):
        """Marca la corrida como terminada con éxito, guarda un resumen
        opcional, preserva una copia para poder deshacerla después
        (`_preservar_ultima_ejecucion`) y finalmente limpia los archivos
        temporales de recuperación (ya no hace falta poder "reanudarla")."""
        state = self.load_state()
        if not state:
            return
        if summary:
            state["summary"] = summary
        state["status"] = "completed"
        state["updated_at"] = self._utc_now()
        self._save_json_file(self.state_file, state)
        self._preservar_ultima_ejecucion(state)
        self.clear()

    def _last_run_manifest_path(self):
        return self.base_dir / "last_run_manifest.json"

    def _last_run_journal_path(self):
        return self.base_dir / "last_run_journal.jsonl"

    def _last_run_summary_path(self):
        return self.base_dir / "last_run_summary.json"

    def _preservar_ultima_ejecucion(self, state):
        """Copia el manifest y journal de la ejecución que acaba de terminar,
        para que 'Deshacer última organización' pueda usarlos incluso después
        de que clear() borre los archivos temporales de recuperación."""
        try:
            journal_path = Path(str(state.get("journal_path", "")).strip())
            manifest_path = Path(str(state.get("manifest_path", "")).strip())
            if journal_path.exists():
                shutil.copyfile(journal_path, self._last_run_journal_path())
            if manifest_path.exists():
                shutil.copyfile(manifest_path, self._last_run_manifest_path())
            resumen = {
                "run_id": state.get("run_id", ""),
                "completed_at": self._utc_now(),
                "destino": state.get("destino", ""),
                "origen": state.get("origen", ""),
                "mover_en_vez_de_copiar": bool(state.get("mover_en_vez_de_copiar", False)),
                "deshecho": False,
            }
            self._save_json_file(self._last_run_summary_path(), resumen)
        except OSError:
            pass

    def get_last_run_summary(self):
        """Resumen de la última organización completada (origen, destino,
        si se movieron o copiaron archivos, si ya se deshizo)."""
        summary = self._load_json_file(self._last_run_summary_path())
        return summary if isinstance(summary, dict) else {}

    def get_last_run_journal_entries(self):
        """Lee línea por línea el diario de la última corrida completada y
        devuelve la lista de eventos (uno por archivo procesado), usados
        por "Deshacer última organización" para saber qué mover de
        vuelta."""
        journal_path = self._last_run_journal_path()
        if not journal_path.exists():
            return []
        entries = []
        try:
            with journal_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return entries

    def mark_last_run_undone(self):
        """Marca en el resumen guardado que la última organización ya fue
        deshecha (para no permitir deshacerla dos veces)."""
        resumen = self.get_last_run_summary()
        if not resumen:
            return
        resumen["deshecho"] = True
        self._save_json_file(self._last_run_summary_path(), resumen)

    def get_manifest_files(self, state):
        """Lista completa de archivos que se planeaba procesar en la
        corrida guardada en `state` (leída de su manifiesto)."""
        manifest_path = Path(str(state.get("manifest_path", "")).strip())
        manifest = self._load_json_file(manifest_path)
        files = manifest.get("files", []) if isinstance(manifest, dict) else []
        return [str(Path(file_path)) for file_path in files if str(file_path).strip()]

    def rebuild_runtime_state(self, state):
        """Reconstruye, a partir del diario de una corrida interrumpida,
        por dónde se debe continuar: el índice del siguiente archivo a
        procesar, los contadores (procesados/duplicados/errores), las
        categorías ya usadas y el mapa de hashes ya vistos (para seguir
        detectando duplicados correctamente al reanudar). Esto es lo que
        permite continuar una organización larga justo donde se quedó."""
        snapshot = {
            "next_index": 0,
            "procesados": 0,
            "duplicados": 0,
            "errores": 0,
            "categorias_usadas": set(),
            "hashes": {},
        }
        journal_path = Path(str(state.get("journal_path", "")).strip())
        if not journal_path.exists():
            return snapshot

        try:
            with journal_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    index = int(entry.get("index", -1))
                    if index >= 0:
                        snapshot["next_index"] = max(snapshot["next_index"], index + 1)

                    status = entry.get("status")
                    categoria = str(entry.get("categoria", "")).strip()
                    if categoria:
                        snapshot["categorias_usadas"].add(categoria)
                    if status in {"copied", "moved"}:
                        snapshot["procesados"] += 1
                        file_hash = str(entry.get("hash", "")).strip()
                        dest = str(entry.get("destino", "")).strip()
                        if file_hash and dest:
                            snapshot["hashes"][file_hash] = dest
                    elif status == "duplicate":
                        snapshot["duplicados"] += 1
                    elif status in {"error", "missing"}:
                        snapshot["errores"] += 1
        except OSError:
            return snapshot

        return snapshot


class LegacySheetsLicenseClient:
    """
    Cliente de licencias sin backend propio.
    Lee un Google Sheet publicado como CSV para verificar suscripciones sincronizadas por Make.
    No requiere servidor, Render, ni base de datos propia.

    El Sheet debe tener estas columnas (fila 1 = encabezados):
        email | plan | estado | fecha_pago

    Para obtener la URL CSV de tu Sheet:
        Archivo → Compartir → Publicar en la web → CSV → Copiar enlace
    Luego ponla en FILESYNC_PRO_SHEETS_URL dentro de desktop_runtime_config.json.
    """

    _CONNECTION_MSG = (
        "No pudimos conectar con el servicio de licencias en este momento.\n\n"
        "Verifica tu conexión a internet e inténtalo nuevamente."
    )
    _NO_URL_MSG = (
        "El servicio de licencias no está configurado en esta instalación.\n\n"
        "Contacta al soporte para obtener asistencia."
    )

    def __init__(self, sheets_url):
        self.sheets_url = (sheets_url or "").strip()

    def _fetch_rows(self, timeout=15):
        """Descarga el CSV del Sheet y devuelve una lista de dicts."""
        if not self.sheets_url:
            raise LicensingError(self._NO_URL_MSG)
        req = request.Request(self.sheets_url, headers={"User-Agent": "FileSync-Pro/3"})
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8-sig")
        except error.URLError as exc:
            raise LicensingError(self._CONNECTION_MSG) from exc

        reader = csv.DictReader(io.StringIO(raw))
        return [
            {k.strip().lower(): v.strip() for k, v in row.items()}
            for row in reader
        ]

    def _find_row(self, email, plan_code=None):
        """Busca la primera fila activa que coincida con el email (y opcionalmente el plan)."""
        email_norm = email.strip().lower()
        rows = self._fetch_rows()
        for row in rows:
            if row.get("email", "").lower() != email_norm:
                continue
            if plan_code and row.get("plan", "").lower() != plan_code.lower():
                continue
            if row.get("estado", "").lower() in {"activo", "active", "authorized"}:
                return row
        return None

    def warm_up(self):
        """No-op: sin servidor que despertar."""
        pass

    def health_check(self, timeout=15):
        """Verifica que el Sheet sea accesible."""
        try:
            self._fetch_rows(timeout=timeout)
            return {"status": "ok"}
        except LicensingError:
            return {"status": "error"}

    def resolve_subscription(self, email, plan_code):
        """
        Consulta el Sheet para verificar si el email tiene una suscripción activa.
        Devuelve un dict compatible con el formato que espera la app.
        """
        row = self._find_row(email, plan_code)
        if row is None:
            # Intentar sin filtro de plan (por si el plan guardado difiere levemente)
            row = self._find_row(email)
        if row is None:
            return {"status": "not_found"}

        plan = row.get("plan", plan_code).lower()
        fecha = row.get("fecha_pago", "").strip()

        # Calcular fecha de vencimiento = fecha_pago + 30 días
        expires_at = self._calcular_vencimiento(fecha, dias=30)

        # Clave determinística: no requiere BD
        raw_key = f"{email.lower()}:{plan}:filesync"
        license_key = "FS-" + hashlib.sha256(raw_key.encode()).hexdigest()[:20].upper()

        return {
            "status": "active",
            "email": email,
            "plan_code": plan,
            "license_key": license_key,
            "subscription_id": f"sheets:{email.lower()}",
            "next_payment_date": expires_at,
        }

    def activate_license(self, email, license_key, device_id):
        """
        Sin servidor propio no hay activación remota.
        La verificación de dispositivos se maneja localmente en LicenseManager.
        Siempre aprueba si la clave es válida (generada por resolve_subscription).
        """
        return {
            "status": "active",
            "email": email,
            "license_key": license_key,
            "subscription_id": f"sheets:{email.lower()}",
        }

    def validate_license(self, email, license_key, device_id):
        """Re-verifica en el Sheet que la suscripción siga activa."""
        row = self._find_row(email)
        if row is None:
            return {"status": "inactive"}
        plan = row.get("plan", "").lower()
        fecha = row.get("fecha_pago", "").strip()
        expires_at = self._calcular_vencimiento(fecha, dias=30)
        return {
            "status": "active",
            "email": email,
            "plan_code": plan,
            "license_key": license_key,
            "next_payment_date": expires_at,
        }

    @staticmethod
    def _calcular_vencimiento(fecha_pago, dias=30):
        """
        Calcula la fecha de vencimiento sumando `dias` a la fecha_pago.
        Soporta formatos ISO 8601 y DD/MM/YYYY.
        Devuelve string ISO 8601 o vacío si no puede parsear.
        """
        if not fecha_pago:
            return ""
        formatos = [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
        ]
        for fmt in formatos:
            try:
                base = dt.datetime.strptime(fecha_pago[:len(fmt) + 5].strip(), fmt)
                vencimiento = base + dt.timedelta(days=dias)
                return vencimiento.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
        return ""


class LicenseApiClient:
    """Cliente HTTP para el backend propio de licencias (`backend_app.py`).

    Es la alternativa "moderna" a `LegacySheetsLicenseClient`: en vez de
    leer un CSV de Google Sheets, habla directamente con la API REST vía
    `/v1/licenses/activate` y `/v1/licenses/validate`. Cuál de los dos
    clientes usa la app depende de qué URL esté configurada (backend
    propio vs. hoja de cálculo) — ver cómo se construye el cliente en
    `ModernOrganizadorGUI`.
    """

    _CONNECTION_MSG = (
        "No pudimos conectar con el servicio de licencias en este momento.\n\n"
        "Verifica tu conexiÃ³n a internet e intÃ©ntalo nuevamente."
    )
    _NO_URL_MSG = (
        "El servicio de licencias no estÃ¡ configurado en esta instalaciÃ³n.\n\n"
        "Contacta al soporte para obtener asistencia."
    )

    def __init__(self, api_url):
        self.api_url = (api_url or "").strip().rstrip("/")

    def _request(self, method, endpoint, payload=None, timeout=15):
        """Hace una petición HTTP genérica (GET/POST) al backend y
        devuelve el JSON de la respuesta ya decodificado. Cualquier
        problema de red, HTTP o de formato se traduce en `LicensingError`
        con un mensaje amigable para mostrar al usuario."""
        if not self.api_url:
            raise LicensingError(self._NO_URL_MSG)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.api_url}{endpoint}", data=body, method=method,
            headers={"User-Agent": "FileSync-Pro/3", "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, json.JSONDecodeError) as exc:
            raise LicensingError(self._CONNECTION_MSG) from exc

    def warm_up(self):
        """Hace un ping silencioso a `/health` para "despertar" el
        servidor (útil si está alojado en un plan gratuito que se duerme
        tras inactividad), sin molestar al usuario si falla."""
        try:
            self._request("GET", "/health", timeout=5)
        except LicensingError:
            pass

    def health_check(self, timeout=15):
        """Comprueba si el backend está respondiendo."""
        try:
            return self._request("GET", "/health", timeout=timeout)
        except LicensingError:
            return {"status": "error"}

    def activate_license(self, email, activation_code, device_id):
        """Activa una licencia en este dispositivo usando el código de
        activación (ver `backend_app.activate_license`)."""
        return self._request("POST", "/v1/licenses/activate", {
            "email": email, "activation_code": activation_code, "device_id": device_id,
        })

    def validate_license(self, email, license_key, device_id):
        """Verifica que la licencia siga activa en este dispositivo."""
        return self._request("POST", "/v1/licenses/validate", {
            "email": email, "license_key": license_key, "device_id": device_id,
        })

    def resolve_subscription(self, email, plan_code):
        """Este cliente no puede "adivinar" una suscripción a partir del
        correo (a diferencia del cliente de Sheets): siempre exige pasar
        por el flujo explícito de activación con código."""
        # Activation is deliberately never inferred from an email address.
        return {"status": "activation_required", "email": email, "plan_code": plan_code}


class ModernOrganizadorGUI(tk.Tk):
    """La ventana principal de la aplicación (hereda de `tk.Tk`, así que ES
    la ventana raíz de Tkinter).

    Aquí vive todo lo que el usuario ve y toca: construcción de la interfaz
    (`crear_widgets` y los métodos `_crear_*`), el tutorial guiado paso a
    paso, el flujo completo de compra/activación/verificación de licencia,
    la pestaña de extracción de facturas, y el proceso de organizar
    archivos en sí (que corre en un hilo aparte para no congelar la
    ventana mientras trabaja, ver `_ejecutar_organizacion`).

    Convención de nombres de métodos: los que empiezan con `_` son
    "privados" (detalles internos de la ventana); los que no, son acciones
    que casi siempre están conectadas directamente a un botón de la
    interfaz (por ejemplo `iniciar_organizacion`, `seleccionar_origen`).
    """

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION} - Organizador de Archivos")
        # Preferencias de interfaz (por ahora solo el modo oscuro), guardadas
        # en disco para recordar la elección la próxima vez que se abra la app.
        self._preferencias = cargar_preferencias()
        self.modo_oscuro = tk.BooleanVar(value=bool(self._preferencias.get("modo_oscuro", False)))
        self.configure(bg=self._paleta()["bg_app"])
        self.minsize(1040, 680)
        self._configurar_tamano_inicial()

        # --- Los "servicios" de los que depende la ventana ---
        self.license_manager = LicenseManager()  # Estado de licencia guardado localmente.
        self.process_recovery = ProcessRecoveryManager()  # Reanudar organizaciones interrumpidas.
        self.api_client = LegacySheetsLicenseClient(SHEETS_LICENSE_URL)  # Verificación de licencia contra Google Sheets.
        self.organizador = ModernOrganizadorArchivos()  # Lógica de categorías/fechas/duplicados.
        self.invoice_extractor = InvoiceExtractor()  # Lectura de facturas (pestaña "Facturas").

        # --- Variables de Tkinter: ligan el estado de la app a los widgets ---
        self.directorio_origen = tk.StringVar()
        self.directorio_destino = tk.StringVar()
        self.organizar_por_categoria = tk.BooleanVar(value=True)
        self.organizar_por_fecha = tk.BooleanVar(value=True)
        self.detectar_recibos = tk.BooleanVar(value=True)
        self.eliminar_duplicados = tk.BooleanVar(value=True)
        self.mover_en_vez_de_copiar = tk.BooleanVar(value=True)
        # Opción para borrar, al terminar, las carpetas que hayan quedado vacías en el
        # origen. Apagada por defecto: solo actúa si además se está MOVIENDO archivos.
        self.eliminar_carpetas_vacias = tk.BooleanVar(value=False)
        # Borrado automático de la cuarentena (más de CUARENTENA_DIAS_MAX días). Es una
        # preferencia de la app (no de una corrida), por eso se guarda en preferencias.json.
        self.purga_automatica_cuarentena = tk.BooleanVar(
            value=bool(self._preferencias.get("purga_automatica_cuarentena", True))
        )
        self.organizar_todos = tk.BooleanVar(value=True)
        self.tipo_archivo_seleccionado = tk.StringVar()
        self.status_text = tk.StringVar()
        self.subscription_id = tk.StringVar(value=self.license_manager.state.get("subscription_id", ""))
        self.subscription_email = tk.StringVar(value=self.license_manager.state.get("subscription_email", ""))
        self.subscription_plan_code = tk.StringVar(value=self.license_manager.state.get("subscription_plan_code", ""))

        self.estadisticas = {
            "total": tk.StringVar(value="0"),
            "procesados": tk.StringVar(value="0"),
            "duplicados": tk.StringVar(value="0"),
            "categorias": tk.StringVar(value="0"),
        }
        # --- Estado interno de la ventana (no son widgets, son "banderas" y cachés) ---
        self._buttons = []
        self._invoice_buttons = []
        self.invoice_results = []
        self.invoice_tree = None
        self._feature_controls = {}
        self._current_run_limit = None
        self._run_in_progress = False
        self._cancel_requested = False
        self._last_recovery_update = 0.0
        self._progress_batch_size = 100
        self._log_batch_size = 250
        self._recovery_batch_size = 50
        self._left_scroll_canvas = None
        self._left_scroll_window = None
        self._right_scroll_canvas = None
        self._right_scroll_window = None
        self._scroll_activo_canvas = None
        self._tutorial_targets = {}
        self._tutorial_overlay = None
        self._tutorial_step_index = 0
        self._tutorial_target_style = None
        self._license_bar = None
        self._license_label = None
        self._logo_marca_images = {}

        # Construcción de la interfaz, en orden: ícono, estilos visuales,
        # widgets, y ajustar el tamaño de la ventana a lo que realmente
        # ocupa el contenido ya dibujado.
        self._configurar_icono()
        self.aplicar_tema()
        self.crear_widgets()
        self.update_idletasks()
        self._ajustar_geometria_a_contenido()
        self.actualizar_estado_licencia()
        self._configurar_atajos()
        self.bind("<F1>", lambda _: self.iniciar_tutorial())
        self.protocol("WM_DELETE_WINDOW", self.salir)
        if IS_MAC:
            # ⌘Q y "Salir de FileSync Pro" del menú de la app pasan por el mismo
            # `salir` que el botón "Salir" (así se respeta el aviso de "hay una
            # organización en curso"), en vez de cerrar la app de golpe.
            self.createcommand("::tk::mac::Quit", self.salir)
        # `after(ms, fn)` programa `fn` para ejecutarse una sola vez pasados
        # esos milisegundos, sin bloquear la interfaz: así la ventana
        # aparece de inmediato y estas comprobaciones (avisos de licencia,
        # sincronizar con el servidor, ofrecer reanudar un proceso viejo)
        # se hacen un momento después, en segundo plano.
        self.after(700, self._mostrar_avisos_licencia)
        self.after(1200, self._sincronizar_licencia_silenciosa)
        self.after(1400, self._ofrecer_reanudar_proceso)
        self.after(3000, self._purgar_cuarentena_automatica)
        self.bind("<FocusIn>", lambda _: self._sincronizar_licencia_silenciosa())
        # Despierta el backend de Render en segundo plano para evitar "servicio lento"
        threading.Thread(target=self.api_client.warm_up, daemon=True).start()

    def _configurar_tamano_inicial(self):
        """Calcula un tamaño de ventana inicial razonable según la
        resolución de pantalla (90% del ancho, 88% del alto, con mínimos y
        máximos), y la centra en la pantalla."""
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(max(1080, int(screen_width * 0.9)), max(screen_width - 40, 900))
        height = min(max(700, int(screen_height * 0.88)), max(screen_height - 60, 680))
        width = min(width, screen_width)
        height = min(height, screen_height)
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _ajustar_geometria_a_contenido(self):
        """Vuelve a ajustar el tamaño de la ventana, esta vez teniendo en
        cuenta cuánto espacio piden realmente los widgets ya creados
        (`winfo_reqwidth/height`), para que no queden cortados ni sobre
        demasiado espacio vacío."""
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        requested_width = self.winfo_reqwidth() + 24
        requested_height = self.winfo_reqheight() + 24

        width = min(max(requested_width, self.winfo_width(), 1080), max(screen_width - 40, 900))
        height = min(max(requested_height, self.winfo_height(), 700), max(screen_height - 60, 680))
        width = min(width, screen_width)
        height = min(height, screen_height)

        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _plan_links():
        """Links de pago (Wompi) configurados y no vacíos, por clave
        compuesta "<plan>_<frecuencia>" (ver `_dividir_plan_frecuencia`)."""
        return {key: value for key, value in SUBSCRIPTION_LINK_MAP.items() if value}

    @staticmethod
    def _dividir_plan_frecuencia(plan_freq_code):
        """Separa una clave compuesta como "pro_anual" en ("pro", "anual").
        Si no trae sufijo de frecuencia, se asume "mensual"."""
        if plan_freq_code.endswith("_anual"):
            return plan_freq_code[: -len("_anual")], "anual"
        if plan_freq_code.endswith("_mensual"):
            return plan_freq_code[: -len("_mensual")], "mensual"
        return plan_freq_code, "mensual"

    @staticmethod
    def _formatear_cop(valor):
        """Formatea un número como pesos colombianos: "$18.900 COP"."""
        return f"${valor:,.0f}".replace(",", ".") + " COP"

    @staticmethod
    def _default_plan_freq_code(plan_links, plans):
        """Resuelve la clave compuesta por defecto, prefiriendo la variante mensual del plan configurado."""
        preferred_mensual = f"{DEFAULT_SUBSCRIPTION_PLAN_CODE}_mensual"
        if preferred_mensual in plan_links:
            return preferred_mensual
        preferred_anual = f"{DEFAULT_SUBSCRIPTION_PLAN_CODE}_anual"
        if preferred_anual in plan_links:
            return preferred_anual
        return plans[0]

    @staticmethod
    def _plan_labels():
        """Nombres "bonitos" de cada plan para mostrar en pantalla."""
        return {
            "basica": "Licencia Básica",
            "pro": "Licencia Pro",
            "premium": "Licencia Premium",
        }

    def _plan_descriptions(self):
        """Texto descriptivo de cada plan/frecuencia disponible (qué
        incluye + precio, con el ahorro calculado si es plan anual), para
        mostrar en el selector de planes al comprar una licencia."""
        base_descriptions = {
            "basica": "Organización esencial por categorías. Incluye mover archivos. No incluye filtro por extensión ni eliminación de duplicados.",
            "pro": "Agrega filtro por extensión, eliminación de duplicados y mover archivos.",
            "premium": "Desbloquea todas las funciones, incluido mover archivos en lugar de copiarlos.",
        }
        descriptions = {}
        for plan_freq_code in self._plan_links():
            plan, freq = self._dividir_plan_frecuencia(plan_freq_code)
            base = base_descriptions.get(plan, "Suscripción con bloqueo automático si no se renueva.")
            precios = PLAN_PRICING_COP.get(plan, {})
            if freq == "anual" and "anual" in precios and "mensual" in precios:
                ahorro = precios["mensual"] * 12 - precios["anual"]
                precio_txt = (
                    f"{self._formatear_cop(precios['anual'])}/año "
                    f"(ahorras {self._formatear_cop(ahorro)} vs. mensual)"
                )
            elif freq == "mensual" and "mensual" in precios:
                precio_txt = f"{self._formatear_cop(precios['mensual'])}/mes"
            else:
                precio_txt = ""
            descriptions[plan_freq_code] = f"{base}\n{precio_txt}" if precio_txt else base
        return descriptions

    @staticmethod
    def _sort_plan_codes(plan_codes):
        """Ordena las claves de plan/frecuencia en el orden en que deben
        aparecer en la interfaz: básica, pro, premium (y dentro de cada
        una, mensual antes que anual)."""
        preferred_plan_order = {"basica": 0, "pro": 1, "premium": 2}
        preferred_freq_order = {"mensual": 0, "anual": 1}

        def sort_key(code):
            plan, freq = ModernOrganizadorGUI._dividir_plan_frecuencia(code)
            return (preferred_plan_order.get(plan, 99), preferred_freq_order.get(freq, 9), code)

        return sorted(plan_codes, key=sort_key)

    def _nombre_plan(self, plan_code):
        """Nombre para mostrar de un código de plan; si no es uno
        conocido, lo capitaliza como respaldo en vez de fallar."""
        normalized = (plan_code or "").strip().lower()
        return self._plan_labels().get(normalized, normalized.replace("_", " ").title() or "Licencia")

    @staticmethod
    def _normalizar_plan_code(plan_code):
        """Limpia un código de plan (minúsculas, sin espacios); si queda
        vacío, usa el plan por defecto en vez de un código vacío."""
        normalized = (plan_code or "").strip().lower()
        return normalized or DEFAULT_PLAN_CODE

    def _plan_features(self, plan_code):
        """Diccionario de funciones habilitadas (`PLAN_FEATURES`) para un
        plan dado; si el plan no se reconoce, se da acceso completo por
        seguridad (mejor sobrestimar que bloquear a alguien que sí pagó)."""
        normalized = self._normalizar_plan_code(plan_code)
        return PLAN_FEATURES.get(normalized, FULL_FEATURE_ACCESS)

    def _plan_code_activo(self):
        """Determina qué plan aplica AHORA MISMO al usuario:
        - Acceso developer/owner -> se trata como "premium" (todo desbloqueado).
        - Licencia activa pero sin plan guardado (licencias antiguas) -> "legacy".
        - Licencia activa con un plan guardado -> ese plan.
        - Sin licencia activa -> el plan por defecto (básica), que es el
          más restrictivo, así que sin licencia el usuario ve las
          limitaciones del plan básico."""
        if self.license_manager.has_local_unrestricted_access():
            return "premium"

        state = self.license_manager.state
        raw_plan_code = state.get("subscription_plan_code", "").strip()
        if state.get("status") == "active" and not raw_plan_code:
            return "legacy"
        plan_code = self._normalizar_plan_code(raw_plan_code)
        if state.get("status") != "active" and not raw_plan_code:
            return DEFAULT_PLAN_CODE
        return plan_code

    def _set_feature_widget_state(self, feature_key, enabled):
        """Habilita o deshabilita (visualmente, en gris) el control de la
        interfaz asociado a una función del plan (por ejemplo el checkbox
        de "eliminar duplicados")."""
        widget = self._feature_controls.get(feature_key)
        if widget is None:
            return
        if enabled:
            widget.state(["!disabled"])
        else:
            widget.state(["disabled"])

    def _aplicar_restricciones_plan(self):
        """Aplica en la interfaz las limitaciones del plan actual: si una
        función no está incluida en el plan, se apaga su casilla y se
        deshabilita el control para que no se pueda volver a activar."""
        features = self._plan_features(self._plan_code_activo())

        if not features["remove_duplicates"]:
            self.eliminar_duplicados.set(False)
        if not features["move_files"]:
            self.mover_en_vez_de_copiar.set(False)
        if not features["type_filter"]:
            self.organizar_todos.set(True)

        self._set_feature_widget_state("remove_duplicates", features["remove_duplicates"])
        self._set_feature_widget_state("move_files", features["move_files"])
        self._set_feature_widget_state("type_filter", features["type_filter"])
        self._set_feature_widget_state("undo_organization", features.get("undo_organization", False))
        self.actualizar_disponibilidad_tipo()

    def _monthly_quota_text(self):
        """Texto corto para mostrar en la barra de estado con los usos
        mensuales restantes (o vacío si el usuario tiene acceso sin
        restricciones)."""
        if self.license_manager.has_local_unrestricted_access():
            return ""

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None:
            return " | Usos mensuales ilimitados"
        return f" | Usos mensuales: {usage_info['remaining']} de {usage_info['limit']} disponibles"

    def _asegurar_uso_mensual_disponible(self):
        """Comprueba, antes de dejar organizar archivos, si al usuario le
        queda al menos un uso este mes. Si no le queda, muestra el diálogo
        de "usos agotados" (`_ofrecer_compra_al_agotar_usos`) y devuelve
        `False` para que quien llama cancele la operación."""
        if self.license_manager.has_local_unrestricted_access():
            return True

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None or usage_info["remaining"] > 0:
            return True

        self._ofrecer_compra_al_agotar_usos(plan_code, usage_info)
        return False

    def _ofrecer_compra_al_agotar_usos(self, plan_code, usage_info):
        """Muestra un diálogo cuando se agotan los usos, ofreciendo renovar o cambiar de plan."""
        plan_name = self._nombre_plan(plan_code)
        plan_links = self._plan_links()

        dialogo = tk.Toplevel(self)
        dialogo.title("Usos agotados")
        dialogo.resizable(False, False)
        dialogo.configure(bg=self._paleta()["bg_app"])
        dialogo.grab_set()

        ancho, alto = 420, 240
        dialogo.update_idletasks()
        x = (dialogo.winfo_screenwidth() - ancho) // 2
        y = (dialogo.winfo_screenheight() - alto) // 2
        dialogo.geometry(f"{ancho}x{alto}+{x}+{y}")

        frame = ttk.Frame(dialogo, padding=24)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="⚠️  Usos mensuales agotados",
            font=(FONT_UI, _fs(12), "bold"),
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            frame,
            text=(
                f"Tu plan {plan_name} ya usó los {usage_info['limit']} usos disponibles este mes.\n\n"
                "¿Quieres renovar tu plan actual o cambiar a uno con más usos?"
            ),
            wraplength=370,
            justify="left",
        ).pack(anchor="w", pady=(0, 16))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X)

        def _renovar():
            dialogo.destroy()
            self._iniciar_compra_plan(plan_code)

        def _cambiar():
            dialogo.destroy()
            self.comprar_licencia()

        def _cancelar():
            dialogo.destroy()

        ttk.Button(btn_frame, text=f"Renovar {plan_name}", command=_renovar, style="Accent.TButton").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Ver otros planes", command=_cambiar).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancelar", command=_cancelar).pack(side=tk.RIGHT)

    def _iniciar_compra_plan(self, plan_code):
        """Inicia el flujo de compra para un plan específico, saltando el selector."""
        plan_links = self._plan_links()
        clave = next(
            (k for k in plan_links if self._dividir_plan_frecuencia(k)[0] == plan_code),
            None,
        )
        if not clave:
            self.comprar_licencia()
            return
        email = self._pedir_correo_confirmado(
            "Renovar licencia",
            f"Escribe el correo con el que quieres renovar el plan {self._nombre_plan(plan_code)}.",
        )
        if not email:
            return
        checkout_url = plan_links[clave]
        plan_base, _ = self._dividir_plan_frecuencia(clave)
        self.license_manager.remember_pending_subscription(email, plan_base)
        self.actualizar_estado_licencia()
        self.log(f"Link de plan local listo para {email}. Plan {clave}.", "info")
        webbrowser.open(checkout_url)

    def _registrar_uso_mensual(self, etiqueta_uso):
        """Suma un uso al contador mensual del plan actual y lo anota en
        el log de la app (con cuántos usos quedan). `etiqueta_uso` es solo
        texto descriptivo para el log (por ejemplo "organizar archivos")."""
        if self.license_manager.has_local_unrestricted_access():
            return

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        self.license_manager.register_monthly_use(plan_code, features, 1)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None:
            self.log(f"Uso registrado: {etiqueta_uso}. Usos mensuales ilimitados.", "info")
        else:
            self.log(
                f"Uso registrado: {etiqueta_uso}. Usos restantes: {usage_info['remaining']} de {usage_info['limit']}.",
                "info",
            )

    def _iterar_archivos_elegibles(self, origen, extension_filtrada):
        """Recorre `origen` (y todas sus subcarpetas) y va entregando, uno
        por uno, los archivos que corresponde procesar: si el filtro por
        tipo está activo (`organizar_todos` en `False`), solo entrega los
        que coincidan con `extension_filtrada`. Es un generador (`yield`)
        para poder recorrer carpetas enormes sin cargar toda la lista en
        memoria de una vez."""
        for root, dirnames, files in walk_files(origen):
            dirnames.sort(key=str.lower)
            for nombre_archivo in sorted(files, key=self.organizador.clave_orden_personalizada):
                extension = Path(nombre_archivo).suffix.lower()
                if not self.organizar_todos.get() and extension != extension_filtrada:
                    continue
                yield str(Path(root) / nombre_archivo)

    def _construir_lista_archivos_elegibles(self, origen, extension_filtrada, limite=None):
        """Igual que `_iterar_archivos_elegibles`, pero devuelve una lista
        completa (parando en `limite` archivos si se indica uno)."""
        archivos = []
        for ruta_archivo in self._iterar_archivos_elegibles(origen, extension_filtrada):
            archivos.append(ruta_archivo)
            if limite is not None and len(archivos) >= limite:
                break
        return archivos

    def _contar_archivos_elegibles(self, origen, extension_filtrada):
        """Cuenta cuántos archivos serían elegibles, sin guardar la lista
        completa (se usa para mostrar el total antes de organizar)."""
        return sum(1 for _ in self._iterar_archivos_elegibles(origen, extension_filtrada))

    @staticmethod
    def _normalizar_email(email):
        return (email or "").strip().lower()

    def _validar_email(self, email):
        """Valida el formato de un correo y, si el dominio es un error de
        tipeo común (gmial.com, hotnail.com...), en vez de aceptarlo o
        rechazarlo sin más, sugiere la corrección probable. Devuelve
        `(correo_normalizado, "")` si es válido, o `("", mensaje_error)`
        si no."""
        normalized = self._normalizar_email(email)
        if not normalized:
            return "", "Debes ingresar un correo electrónico."
        if not EMAIL_REGEX.match(normalized):
            return "", "El correo no tiene un formato válido."
        domain = normalized.split("@", 1)[1]
        suggested_domain = COMMON_EMAIL_DOMAIN_FIXES.get(domain)
        if suggested_domain:
            suggestion = f"{normalized.split('@', 1)[0]}@{suggested_domain}"
            return "", f"Revisa el correo. Quizá quisiste escribir: {suggestion}"
        return normalized, ""

    def _pedir_correo_confirmado(self, title, subtitle):
        """Abre una ventana emergente que pide el correo dos veces (para
        evitar errores de tipeo al comprar/activar una licencia), valida
        el formato y que ambas veces coincidan, y no se cierra hasta que
        el usuario confirme o cancele (`self.wait_window`). Devuelve el
        correo validado, o cadena vacía si se canceló."""
        paleta = self._paleta()
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg=paleta["bg_app"])

        email_var = tk.StringVar()
        confirm_var = tk.StringVar()
        error_var = tk.StringVar()
        result = {"email": ""}

        wrapper = ttk.Frame(dialog, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrapper, text=subtitle, font=(FONT_UI, _fs(10), "bold"), wraplength=380, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(0, 12)
        )

        ttk.Label(wrapper, text="Correo electrónico").pack(anchor=tk.W)
        email_entry = ttk.Entry(wrapper, textvariable=email_var, width=42)
        email_entry.pack(fill=tk.X, pady=(4, 10))

        ttk.Label(wrapper, text="Confirmar correo").pack(anchor=tk.W)
        confirm_entry = ttk.Entry(wrapper, textvariable=confirm_var, width=42)
        confirm_entry.pack(fill=tk.X, pady=(4, 8))

        ttk.Label(wrapper, textvariable=error_var, foreground=paleta["log_error"], wraplength=380, justify=tk.LEFT).pack(
            anchor=tk.W
        )

        actions = ttk.Frame(wrapper)
        actions.pack(fill=tk.X, pady=(14, 0))

        def confirm():
            email = self._normalizar_email(email_var.get())
            confirm_email = self._normalizar_email(confirm_var.get())
            validated_email, error_message = self._validar_email(email)
            if error_message:
                error_var.set(error_message)
                return
            if validated_email != confirm_email:
                error_var.set("Los correos no coinciden. Revisa que ambos sean iguales.")
                return
            result["email"] = validated_email
            dialog.destroy()

        def cancel():
            dialog.destroy()

        ttk.Button(actions, text="Cancelar", command=cancel, style="Secondary.TButton").pack(side=tk.RIGHT)
        ttk.Button(actions, text="Continuar", command=confirm, style="Primary.TButton").pack(side=tk.RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _: confirm())
        dialog.bind("<Escape>", lambda _: cancel())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.winfo_rootx() + 80}+{self.winfo_rooty() + 80}")
        email_entry.focus_set()
        self.wait_window(dialog)
        return result["email"]

    def _elegir_plan(self, plan_codes, default_plan):
        """Abre una ventana emergente con botones de opción (uno por
        plan/frecuencia disponible, con su descripción y precio) para que
        el usuario elija qué quiere comprar. Devuelve el código elegido, o
        cadena vacía si se canceló."""
        dialog = tk.Toplevel(self)
        dialog.title("Elegir plan")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg=self._paleta()["bg_app"])

        selected_plan = tk.StringVar(value=default_plan)
        result = {"plan_code": ""}
        base_labels = self._plan_labels()
        descriptions = self._plan_descriptions()
        freq_suffix = {"mensual": "Mensual", "anual": "Anual"}

        def etiqueta_completa(plan_freq_code):
            plan, freq = self._dividir_plan_frecuencia(plan_freq_code)
            base = base_labels.get(plan, plan.replace("_", " ").title())
            return f"{base} — {freq_suffix.get(freq, freq.title())}"

        wrapper = ttk.Frame(dialog, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            wrapper,
            text="Selecciona el plan que quieres pagar:",
            font=(FONT_UI, _fs(10), "bold"),
        ).pack(anchor=tk.W, pady=(0, 10))

        for plan_code in plan_codes:
            label = etiqueta_completa(plan_code)
            description = descriptions.get(plan_code, "Suscripción con bloqueo automático si no se renueva.")
            option = ttk.Frame(wrapper, padding=(0, 4, 0, 8))
            option.pack(fill=tk.X, anchor=tk.W)
            ttk.Radiobutton(
                option,
                text=label,
                value=plan_code,
                variable=selected_plan,
            ).pack(anchor=tk.W)
            ttk.Label(
                option,
                text=description,
                style="Subtitulo.TLabel",
                wraplength=360,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=(24, 0), pady=(2, 0))

        actions = ttk.Frame(wrapper)
        actions.pack(fill=tk.X, pady=(14, 0))

        def confirm():
            result["plan_code"] = selected_plan.get().strip().lower()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        ttk.Button(actions, text="Cancelar", command=cancel, style="Secondary.TButton").pack(side=tk.RIGHT)
        ttk.Button(actions, text="Continuar", command=confirm, style="Primary.TButton").pack(side=tk.RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _: confirm())
        dialog.bind("<Escape>", lambda _: cancel())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.winfo_rootx() + 80}+{self.winfo_rooty() + 80}")
        self.wait_window(dialog)
        return result["plan_code"]

    def _configurar_icono(self):
        """Configura el ícono de la ventana: intenta cargar los PNG de
        varios tamaños (mejor para la mayoría de plataformas) y, si hay un
        .ico disponible, también lo usa como respaldo (necesario en
        Windows para la barra de tareas). Cualquier ícono que falle al
        cargar simplemente se omite en vez de interrumpir el arranque."""
        self._icon_images = []

        for icono_png_name in ICON_PNG_VARIANTS:
            icono_png = resolve_app_resource(ICON_ASSET_DIR, icono_png_name) or resolve_app_resource(icono_png_name)
            if icono_png is None:
                continue
            cargado = None
            # Igual que en `_cargar_logo_marca`: Pillow primero (decodifica
            # el PNG por su cuenta, más tolerante que el lector nativo de
            # Tk en algunas instalaciones de Windows) y `tk.PhotoImage`
            # como respaldo si Pillow no está disponible.
            if Image is not None and ImageTk is not None:
                try:
                    with Image.open(icono_png) as img:
                        cargado = ImageTk.PhotoImage(img.convert("RGBA"))
                except Exception:
                    cargado = None
            if cargado is None:
                try:
                    cargado = tk.PhotoImage(file=str(icono_png))
                except Exception:
                    continue
            self._icon_images.append(cargado)

        if self._icon_images:
            try:
                self.iconphoto(True, *self._icon_images)
            except Exception:
                self._icon_images = []

        # El .ico es solo para Windows; en macOS el ícono de la app viene del
        # paquete .app (icono.icns) y `iconphoto` de arriba ya cubre el resto.
        icono_ico = None if IS_MAC else (
            resolve_app_resource(ICON_ASSET_DIR, ICON_ICO_NAME) or resolve_app_resource(ICON_ICO_NAME)
        )
        if icono_ico is not None:
            try:
                self.iconbitmap(default=str(icono_ico))
            except Exception:
                pass

    def _cargar_logo_marca(self, nombre_archivo: str):
        """Carga (con caché) uno de los PNG del logo de marca ya
        pre-escalados en `assets/` para usarlo como imagen de un
        `ttk.Label`. Devuelve `None` sin interrumpir la interfaz si el
        archivo no existe o no se puede leer (por ejemplo, en un
        empaquetado que no incluyó el asset).

        Se intenta primero con Pillow (`ImageTk`), que decodifica el PNG
        por su cuenta y es más tolerante que el lector de PNG que trae Tk
        de fábrica; si Pillow no está disponible, se usa `tk.PhotoImage`
        directamente como respaldo."""
        if nombre_archivo in self._logo_marca_images:
            return self._logo_marca_images[nombre_archivo]

        ruta = resolve_app_resource(ICON_ASSET_DIR, nombre_archivo) or resolve_app_resource(nombre_archivo)
        imagen = None
        if ruta is not None:
            if Image is not None and ImageTk is not None:
                try:
                    with Image.open(ruta) as img:
                        imagen = ImageTk.PhotoImage(img.convert("RGBA"))
                except Exception:
                    imagen = None
            if imagen is None:
                try:
                    imagen = tk.PhotoImage(file=str(ruta))
                except Exception:
                    imagen = None

        self._logo_marca_images[nombre_archivo] = imagen
        return imagen

    def _configurar_atajos(self):
        """Atajos de teclado globales: Ctrl+O elegir origen, Ctrl+D elegir
        destino, F5 iniciar organización, Ctrl+Q salir (F1, el tutorial,
        se registra aparte en `__init__`). En macOS se agregan además los
        equivalentes con la tecla Comando (⌘O, ⌘D); ⌘Q lo maneja el menú de
        la app (ver `__init__`)."""
        modificadores = ("Control", "Command") if IS_MAC else ("Control",)
        for modificador in modificadores:
            self.bind(f"<{modificador}-o>", lambda _: self.seleccionar_origen())
            self.bind(f"<{modificador}-d>", lambda _: self.seleccionar_destino())
        self.bind("<F5>", lambda _: self.iniciar_organizacion())
        self.bind("<Control-q>", lambda _: self.salir())

    def _paleta(self):
        """Devuelve el diccionario de colores del tema actual (oscuro si
        `self.modo_oscuro` está activo, claro en caso contrario)."""
        return TEMA_OSCURO if self.modo_oscuro.get() else TEMA_CLARO

    def configurar_estilos(self):
        """Define la apariencia visual de todos los widgets ttk usados en
        la app (colores, tipografías, bordes...) según la paleta actual: un
        solo lugar centraliza el "tema" para que toda la interfaz se vea
        consistente, y para poder recolorearla completa al cambiar entre
        modo claro y modo oscuro (ver `aplicar_tema`)."""
        paleta = self._paleta()
        style = ttk.Style()
        style.theme_use("clam")

        # --- Estilos base: cualquier widget ttk sin un `style=` propio usa
        # estos (TFrame, TLabel...), así que fijarlos es lo que hace que los
        # contenedores y textos "sueltos" también cambien de tema en vez de
        # quedarse con el gris por defecto de Tk. ---
        style.configure("TFrame", background=paleta["bg_app"])
        style.configure("TLabel", background=paleta["bg_app"], foreground=paleta["text_primary"])
        style.configure("TLabelframe", background=paleta["bg_app"], bordercolor=paleta["border"])
        style.configure("TLabelframe.Label", background=paleta["bg_app"], foreground=paleta["text_primary"])
        style.configure("TCheckbutton", background=paleta["bg_card"], foreground=paleta["text_primary"], font=(FONT_UI, _fs(10)))
        style.map(
            "TCheckbutton",
            background=[("active", paleta["bg_card"])],
            foreground=[("active", paleta["text_primary"])],
        )
        style.configure("TRadiobutton", background=paleta["bg_app"], foreground=paleta["text_primary"], font=(FONT_UI, _fs(10)))
        style.map(
            "TRadiobutton",
            background=[("active", paleta["bg_app"])],
            foreground=[("active", paleta["text_primary"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=paleta["entry_bg"],
            foreground=paleta["entry_fg"],
            insertcolor=paleta["text_primary"],
            bordercolor=paleta["border"],
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", paleta["btn_bg_disabled"])],
            bordercolor=[("focus", paleta["accent"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=paleta["entry_bg"],
            background=paleta["entry_bg"],
            foreground=paleta["entry_fg"],
            arrowcolor=paleta["text_secondary"],
            bordercolor=paleta["border"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", paleta["entry_bg"]), ("disabled", paleta["btn_bg_disabled"])],
            foreground=[("readonly", paleta["entry_fg"])],
            background=[("readonly", paleta["entry_bg"])],
        )
        style.configure(
            "Treeview",
            background=paleta["tree_bg"],
            fieldbackground=paleta["tree_bg"],
            foreground=paleta["tree_fg"],
            bordercolor=paleta["border"],
        )
        style.map(
            "Treeview",
            background=[("selected", paleta["accent"])],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            background=paleta["tree_heading_bg"],
            foreground=paleta["text_primary"],
            font=(FONT_UI, _fs(9), "bold"),
        )
        style.map("Treeview.Heading", background=[("active", paleta["btn_bg_hover"])])
        for orientacion in ("Vertical", "Horizontal"):
            style.configure(
                f"{orientacion}.TScrollbar",
                background=paleta["btn_bg"],
                troughcolor=paleta["scrollbar_trough"],
                bordercolor=paleta["border"],
                arrowcolor=paleta["text_secondary"],
            )
            style.map(f"{orientacion}.TScrollbar", background=[("active", paleta["btn_bg_hover"])])
        style.configure(
            "TProgressbar",
            troughcolor=paleta["scrollbar_trough"],
            background=paleta["accent"],
            bordercolor=paleta["border"],
        )
        style.configure(
            "TButton",
            font=(FONT_UI, _fs(10), "bold"),
            padding=8,
            foreground=paleta["text_primary"],
            background=paleta["btn_bg"],
            borderwidth=1,
            relief="solid",
            focusthickness=0,
        )
        style.map(
            "TButton",
            foreground=[("disabled", paleta["btn_fg_disabled"]), ("pressed", paleta["text_primary"]), ("active", paleta["text_primary"])],
            background=[("disabled", paleta["btn_bg_disabled"]), ("pressed", paleta["btn_bg_pressed"]), ("active", paleta["btn_bg_hover"])],
            bordercolor=[("disabled", paleta["border"]), ("pressed", paleta["border_hover"]), ("active", paleta["border_hover"])],
        )

        # --- Estilos con nombre propio, usados explícitamente por partes
        # concretas de la interfaz (tarjetas, botones primarios, tutorial...) ---
        style.configure("Titulo.TLabel", font=(FONT_UI, _fs(26), "bold"), background=paleta["bg_app"], foreground=paleta["text_primary"])
        style.configure("Subtitulo.TLabel", font=(FONT_UI, _fs(11)), background=paleta["bg_app"], foreground=paleta["text_secondary"])
        style.configure("Footer.TLabel", font=(FONT_UI, _fs(8)), background=paleta["bg_app"], foreground=paleta["text_secondary"])
        style.configure("Card.TLabelframe", background=paleta["bg_card"], bordercolor=paleta["border"], borderwidth=1, relief="solid")
        style.configure(
            "Card.TLabelframe.Label", background=paleta["bg_card"], foreground=paleta["text_primary"], font=(FONT_UI, _fs(11), "bold")
        )
        style.configure(
            "Primary.TButton",
            font=(FONT_UI, _fs(10), "bold"),
            padding=10,
            foreground="#ffffff",
            background=paleta["accent"],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            foreground=[("disabled", paleta["accent_disabled_fg"]), ("pressed", "#ffffff"), ("active", "#ffffff")],
            background=[("disabled", paleta["accent_disabled_bg"]), ("pressed", paleta["accent_pressed"]), ("active", paleta["accent_hover"])],
        )
        style.configure(
            "Secondary.TButton",
            font=(FONT_UI, _fs(10), "bold"),
            padding=8,
            foreground=paleta["text_primary"],
            background=paleta["btn_bg"],
            borderwidth=1,
            relief="solid",
            focusthickness=0,
        )
        style.map(
            "Secondary.TButton",
            foreground=[("disabled", paleta["btn_fg_disabled"]), ("pressed", paleta["text_primary"]), ("active", paleta["text_primary"])],
            background=[("disabled", paleta["btn_bg_disabled"]), ("pressed", paleta["btn_bg_pressed"]), ("active", paleta["btn_bg_hover"])],
            bordercolor=[("disabled", paleta["border"]), ("pressed", paleta["border_hover"]), ("active", paleta["border_hover"])],
        )
        # "Accent.TButton" se usa en el diálogo de "usos agotados" (botón
        # "Renovar plan"); no tenía un estilo propio definido antes, así
        # que heredaba el look por defecto de ttk (no se adaptaba al tema).
        style.configure(
            "Accent.TButton",
            font=(FONT_UI, _fs(10), "bold"),
            padding=8,
            foreground="#ffffff",
            background=paleta["accent"],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Accent.TButton",
            foreground=[("disabled", paleta["accent_disabled_fg"]), ("pressed", "#ffffff"), ("active", "#ffffff")],
            background=[("disabled", paleta["accent_disabled_bg"]), ("pressed", paleta["accent_pressed"]), ("active", paleta["accent_hover"])],
        )
        style.configure("StatCard.TFrame", background=paleta["bg_alt"], borderwidth=1, relief="solid", bordercolor=paleta["border"])
        style.configure("StatLabel.TLabel", background=paleta["bg_alt"], foreground=paleta["text_secondary"], font=(FONT_UI, _fs(9), "bold"))
        style.configure("StatValue.TLabel", background=paleta["bg_alt"], foreground=paleta["text_primary"], font=(FONT_UI, _fs(18), "bold"))
        style.configure("ScrollHost.TFrame", background=paleta["bg_app"])
        style.configure("TutorialFocus.TFrame", background=paleta["tutorial_bg"], borderwidth=3, relief="solid")
        style.configure(
            "TutorialFocus.TLabelframe",
            background=paleta["tutorial_bg"],
            foreground=paleta["tutorial_fg"],
            borderwidth=3,
            relief="solid",
        )
        style.configure(
            "TutorialFocus.TLabelframe.Label",
            background=paleta["tutorial_bg"],
            foreground=paleta["tutorial_fg"],
            font=(FONT_UI, _fs(11), "bold"),
        )
        style.configure(
            "TutorialFocus.TButton",
            font=(FONT_UI, _fs(10), "bold"),
            padding=10,
            foreground="#ffffff",
            background=paleta["accent"],
            borderwidth=3,
            relief="solid",
        )
        # Checkbutton usado como interruptor de modo oscuro en la barra
        # oscura del encabezado: necesita su propio estilo porque, a
        # diferencia del resto de casillas, va sobre `bg_header` y no
        # sobre una tarjeta clara.
        style.configure(
            "HeaderCheck.TCheckbutton",
            background=paleta["bg_header"],
            foreground=paleta["fg_header"],
            font=(FONT_UI, _fs(10), "bold"),
        )
        style.map(
            "HeaderCheck.TCheckbutton",
            background=[("active", paleta["bg_header"])],
            foreground=[("active", paleta["fg_header"])],
        )

    def aplicar_tema(self):
        """Vuelve a aplicar la paleta de color actual a TODA la ventana: los
        estilos ttk (que ya se actualizan solos en los widgets existentes en
        cuanto se reconfiguran, por referenciar el estilo por nombre) y los
        pocos widgets tk "crudos" que no usan un estilo ttk y por eso no se
        recolorean automáticamente (fondo de la ventana, los dos canvas con
        scroll, la barra de licencia y el registro de actividad). Se llama
        una vez al construir la ventana y cada vez que se activa/desactiva
        el modo oscuro."""
        paleta = self._paleta()
        self.configure(bg=paleta["bg_app"])
        self.configurar_estilos()

        if self._left_scroll_canvas is not None:
            self._left_scroll_canvas.configure(background=paleta["bg_app"])
        if self._right_scroll_canvas is not None:
            self._right_scroll_canvas.configure(background=paleta["bg_app"])
        if self._license_bar is not None:
            self._license_bar.configure(bg=paleta["bg_header"])
        if self._license_label is not None:
            self._license_label.configure(bg=paleta["bg_header"], fg=paleta["fg_header"])
        if getattr(self, "texto_log", None) is not None:
            self.texto_log.configure(bg=paleta["log_bg"], fg=paleta["text_primary"], insertbackground=paleta["text_primary"])
            self.texto_log.tag_config("exito", foreground=paleta["log_exito"])
            self.texto_log.tag_config("error", foreground=paleta["log_error"])
            self.texto_log.tag_config("advertencia", foreground=paleta["log_advertencia"])
            self.texto_log.tag_config("info", foreground=paleta["log_info"])

        # El menú desplegable de un Combobox es, por dentro, un Listbox de
        # Tk normal (no ttk), así que no sigue los estilos de arriba: se
        # configura aparte, por clase, para toda la app.
        self.option_add("*TCombobox*Listbox.background", paleta["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", paleta["entry_fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", paleta["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _cambiar_tema(self):
        """Handler del interruptor de modo oscuro del encabezado: aplica la
        paleta nueva a toda la ventana y guarda la preferencia en disco para
        que la próxima vez que se abra la app recuerde la elección."""
        self._preferencias["modo_oscuro"] = bool(self.modo_oscuro.get())
        guardar_preferencias(self._preferencias)
        self.aplicar_tema()

    def crear_widgets(self):
        """Punto de entrada que arma toda la ventana: la cabecera arriba, y
        debajo dos columnas (panel izquierdo de configuración, panel
        derecho de ventas/facturas/registro), delegando cada parte a los
        métodos `_crear_*`."""
        container = ttk.Frame(self, padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        self._crear_header(container)
        # El pie se empaqueta ANTES que el contenido central y con
        # side=BOTTOM: así reserva su franja fija en la parte inferior de
        # la ventana y el `content` de abajo (que sí se expande) solo
        # ocupa el espacio que queda en medio, sin taparlo.
        self._crear_footer(container)

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        content.columnconfigure(0, weight=4, minsize=380)
        content.columnconfigure(1, weight=5, minsize=460)
        content.rowconfigure(0, weight=1)
        self._crear_panel_izquierdo(content)
        self._crear_panel_derecho(content)

    def _crear_footer(self, parent):
        """Pie de página fijo al fondo de la ventana: logo de marca en
        miniatura junto al aviso de derechos reservados, para dejar claro
        que la app tiene dueño y licencia y no se puede redistribuir."""
        footer = ttk.Frame(parent)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        ttk.Separator(footer, orient="horizontal").pack(fill=tk.X, pady=(0, 8))

        footer_row = ttk.Frame(footer)
        footer_row.pack(fill=tk.X)

        logo_footer = self._cargar_logo_marca(LOGO_MARCA_FOOTER_NAME)
        if logo_footer is not None:
            ttk.Label(footer_row, image=logo_footer, style="TLabel").pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(
            footer_row,
            text=obtener_aviso_derechos(),
            style="Footer.TLabel",
            wraplength=920,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, anchor=tk.W)

    def _crear_header(self, parent):
        """Cabecera de la ventana: título de la app, subtítulo, y la barra
        oscura con el estado de la licencia, el interruptor de modo oscuro
        y los botones "Verificar licencia" / "Comprar licencia" / "Cómo
        usar" (este último abre el tutorial y también se registra como
        objetivo del propio tutorial)."""
        paleta = self._paleta()
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill=tk.X)

        title_frame = ttk.Frame(wrapper)
        title_frame.pack(fill=tk.X)

        title_row = ttk.Frame(title_frame)
        title_row.pack(fill=tk.X, anchor=tk.W)

        logo_header = self._cargar_logo_marca(ICON_HEADER_NAME)
        if logo_header is not None:
            ttk.Label(title_row, image=logo_header, style="TLabel").pack(side=tk.LEFT, padx=(0, 12))

        title_textos = ttk.Frame(title_row)
        title_textos.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(title_textos, text=f"{APP_NAME} {APP_VERSION}", style="Titulo.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_textos,
            text="Organiza tus archivos por categorías, fechas y reglas inteligentes.",
            style="Subtitulo.TLabel",
        ).pack(anchor=tk.W, pady=(4, 0))

        license_bar = tk.Frame(wrapper, bg=paleta["bg_header"], padx=16, pady=12)
        license_bar.pack(fill=tk.X, pady=(14, 0))
        self._license_bar = license_bar
        self._license_label = tk.Label(
            license_bar,
            textvariable=self.status_text,
            bg=paleta["bg_header"],
            fg=paleta["fg_header"],
            font=(FONT_UI, _fs(10), "bold"),
        )
        self._license_label.pack(side=tk.LEFT)

        for text, command in [
            ("Verificar licencia", self.verificar_licencia),
            ("Comprar licencia", self.comprar_licencia),
            ("Cómo usar", self.iniciar_tutorial),
        ]:
            btn = ttk.Button(license_bar, text=text, command=command, style="Secondary.TButton")
            btn.pack(side=tk.RIGHT, padx=(8, 0))
            if text == "Cómo usar":
                self._tutorial_targets["ayuda"] = (btn, None)
            else:
                self._buttons.append(btn)

        modo_oscuro_check = ttk.Checkbutton(
            license_bar,
            text="🌙 Modo oscuro",
            variable=self.modo_oscuro,
            command=self._cambiar_tema,
            style="HeaderCheck.TCheckbutton",
        )
        modo_oscuro_check.pack(side=tk.RIGHT, padx=(8, 0))

    def _crear_panel_izquierdo(self, parent):
        """Columna izquierda: selectores de carpeta origen/destino, las
        casillas de opciones (por categoría, por fecha, detectar recibos,
        eliminar duplicados, mover en vez de copiar), el filtro por tipo
        de archivo y las tarjetas de estadísticas. Todo va dentro de un
        `Canvas` con scroll propio, porque en pantallas pequeñas el
        contenido puede no caber completo."""
        paleta = self._paleta()
        left_host = ttk.Frame(parent, style="ScrollHost.TFrame")
        left_host.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left_host.columnconfigure(0, weight=1)
        left_host.rowconfigure(0, weight=1)

        self._left_scroll_canvas = tk.Canvas(
            left_host, highlightthickness=0, background=paleta["bg_app"], yscrollincrement=self._scroll_unit_px()
        )
        left_scrollbar = ttk.Scrollbar(left_host, orient="vertical", command=self._left_scroll_canvas.yview)
        self._left_scroll_canvas.configure(yscrollcommand=left_scrollbar.set)
        self._left_scroll_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))

        scroll_body = ttk.Frame(self._left_scroll_canvas)
        self._left_scroll_window = self._left_scroll_canvas.create_window((0, 0), window=scroll_body, anchor="nw")
        scroll_body.bind("<Configure>", self._actualizar_scroll_panel_izquierdo)
        self._left_scroll_canvas.bind("<Configure>", self._ajustar_ancho_panel_izquierdo)
        self._left_scroll_canvas.bind("<Enter>", self._activar_scroll_panel_izquierdo)
        self._left_scroll_canvas.bind("<Leave>", self._desactivar_scroll_panel_izquierdo)

        left_panel = ttk.LabelFrame(scroll_body, text="Configuración", padding=18, style="Card.TLabelframe")
        left_panel.pack(fill=tk.X, expand=True)

        dir_frame = ttk.Frame(left_panel)
        dir_frame.pack(fill=tk.X, pady=(0, 18))
        self._crear_selector_directorio(
            dir_frame, "Directorio de origen", self.directorio_origen, self.seleccionar_origen, "origen"
        )
        self._crear_selector_directorio(
            dir_frame, "Directorio de destino", self.directorio_destino, self.seleccionar_destino, "destino"
        )

        opts_frame = ttk.LabelFrame(left_panel, text="Opciones", padding=14, style="Card.TLabelframe")
        opts_frame.pack(fill=tk.X, pady=(0, 18))
        self._tutorial_targets["opciones"] = (opts_frame, "left")
        for text, variable in [
            ("Organizar por categorías", self.organizar_por_categoria),
            ("Crear carpetas por a\u00f1o y mes", self.organizar_por_fecha),
            ("Detectar recibos en imágenes", self.detectar_recibos),
            ("Eliminar archivos duplicados", self.eliminar_duplicados),
            ("Mover archivos en lugar de copiar", self.mover_en_vez_de_copiar),
            ("Eliminar carpetas vacías del origen (solo al mover)", self.eliminar_carpetas_vacias),
        ]:
            widget = ttk.Checkbutton(opts_frame, text=text, variable=variable)
            widget.pack(anchor=tk.W, pady=3)
            if variable is self.eliminar_duplicados:
                self._feature_controls["remove_duplicates"] = widget
            elif variable is self.mover_en_vez_de_copiar:
                self._feature_controls["move_files"] = widget

        tipo_frame = ttk.LabelFrame(opts_frame, text="Filtro de archivos", padding=10, style="Card.TLabelframe")
        tipo_frame.pack(fill=tk.X, pady=(12, 0))
        self._tutorial_targets["filtro"] = (tipo_frame, "left")
        self._feature_controls["type_filter"] = ttk.Checkbutton(
            tipo_frame,
            text="Organizar todos los tipos de archivo",
            variable=self.organizar_todos,
            command=self.actualizar_disponibilidad_tipo,
        )
        self._feature_controls["type_filter"].pack(anchor=tk.W)

        selector = ttk.Frame(tipo_frame)
        selector.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(selector, text="Solo esta extensión:").pack(side=tk.LEFT, padx=(18, 8))
        self.combo_tipo = ttk.Combobox(selector, textvariable=self.tipo_archivo_seleccionado, state="readonly", width=14)
        self.combo_tipo["values"] = self.organizador.obtener_todas_extensiones()
        if self.combo_tipo["values"]:
            self.combo_tipo.set(self.combo_tipo["values"][0])
        self.combo_tipo.pack(side=tk.LEFT)
        self.actualizar_disponibilidad_tipo()

        # --- Cuarentena de duplicados: borrado automático + botón para vaciarla ---
        cuarentena_frame = ttk.LabelFrame(
            left_panel, text="Cuarentena de duplicados", padding=14, style="Card.TLabelframe"
        )
        cuarentena_frame.pack(fill=tk.X, pady=(0, 18))
        ttk.Label(
            cuarentena_frame,
            text="Los duplicados que la app aparta al mover archivos se guardan aquí y ocupan espacio en disco.",
            wraplength=380,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))
        ttk.Checkbutton(
            cuarentena_frame,
            text=f"Borrar automáticamente lo de más de {CUARENTENA_DIAS_MAX} días",
            variable=self.purga_automatica_cuarentena,
            command=self._guardar_preferencia_purga_cuarentena,
        ).pack(anchor=tk.W, pady=(0, 8))
        btn_vaciar_cuarentena = ttk.Button(
            cuarentena_frame,
            text="Vaciar cuarentena ahora",
            command=self.vaciar_cuarentena,
            style="Secondary.TButton",
        )
        btn_vaciar_cuarentena.pack(anchor=tk.W)
        self._buttons.append(btn_vaciar_cuarentena)

        stats_frame = ttk.LabelFrame(left_panel, text="Estadísticas", padding=14, style="Card.TLabelframe")
        stats_frame.pack(fill=tk.X)
        stats_grid = ttk.Frame(stats_frame)
        stats_grid.pack(fill=tk.X)
        stats_grid.columnconfigure(0, weight=1)
        stats_grid.columnconfigure(1, weight=1)
        self._crear_stat_card(stats_grid, 0, 0, "Archivos totales", self.estadisticas["total"])
        self._crear_stat_card(stats_grid, 0, 1, "Procesados", self.estadisticas["procesados"])
        self._crear_stat_card(stats_grid, 1, 0, "Duplicados", self.estadisticas["duplicados"])
        self._crear_stat_card(stats_grid, 1, 1, "Categorías", self.estadisticas["categorias"])

    def _crear_panel_derecho(self, parent):
        """Columna derecha: aviso de ventas/activación, el panel de
        facturas (`_crear_panel_facturas`), el registro de actividad con
        colores por tipo de mensaje, la barra de progreso, y los botones
        de acción principales (iniciar, analizar, cancelar, deshacer,
        limpiar registro, salir). También con scroll propio."""
        paleta = self._paleta()
        right_host = ttk.Frame(parent, style="ScrollHost.TFrame")
        right_host.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right_host.columnconfigure(0, weight=1)
        right_host.rowconfigure(0, weight=1)

        self._right_scroll_canvas = tk.Canvas(
            right_host, highlightthickness=0, background=paleta["bg_app"], yscrollincrement=self._scroll_unit_px()
        )
        right_scrollbar = ttk.Scrollbar(right_host, orient="vertical", command=self._right_scroll_canvas.yview)
        self._right_scroll_canvas.configure(yscrollcommand=right_scrollbar.set)
        self._right_scroll_canvas.grid(row=0, column=0, sticky="nsew")
        right_scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))

        scroll_body = ttk.Frame(self._right_scroll_canvas)
        self._right_scroll_window = self._right_scroll_canvas.create_window((0, 0), window=scroll_body, anchor="nw")
        scroll_body.bind("<Configure>", self._actualizar_scroll_panel_derecho)
        self._right_scroll_canvas.bind("<Configure>", self._ajustar_ancho_panel_derecho)
        self._right_scroll_canvas.bind("<Enter>", self._activar_scroll_panel_derecho)
        self._right_scroll_canvas.bind("<Leave>", self._desactivar_scroll_panel_derecho)

        right_panel = scroll_body

        checkout_frame = ttk.LabelFrame(right_panel, text="Ventas y activación", padding=14, style="Card.TLabelframe")
        checkout_frame.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(
            checkout_frame,
            text="Selecciona el plan que prefieras y completa tu compra para desbloquear la app.",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        self._crear_panel_facturas(right_panel)

        log_frame = ttk.LabelFrame(right_panel, text="Registro de actividad", padding=14, style="Card.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.texto_log = scrolledtext.ScrolledText(
            log_frame,
            height=14,
            font=(FONT_MONO, _fs(9)),
            wrap=tk.WORD,
            bg=paleta["log_bg"],
            fg=paleta["text_primary"],
            insertbackground=paleta["text_primary"],
        )
        self.texto_log.pack(fill=tk.BOTH, expand=True)
        self.texto_log.tag_config("exito", foreground=paleta["log_exito"])
        self.texto_log.tag_config("error", foreground=paleta["log_error"])
        self.texto_log.tag_config("advertencia", foreground=paleta["log_advertencia"])
        self.texto_log.tag_config("info", foreground=paleta["log_info"])
        self.texto_log.tag_config("subtitulo", font=(FONT_MONO, _fs(9), "bold"))
        self._tutorial_targets["registro"] = (log_frame, "right")

        self.progreso = ttk.Progressbar(right_panel, mode="determinate")
        self.progreso.pack(fill=tk.X, pady=(14, 14))

        btn_frame = ttk.Frame(right_panel)
        btn_frame.pack(fill=tk.X, pady=(0, 6))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        button_specs = [
            ("Iniciar organización", self.iniciar_organizacion, "Primary.TButton", 0, 0),
            ("Analizar directorio", self.analizar_directorio, "Secondary.TButton", 0, 1),
            ("Limpiar registro", self.limpiar_log, "Secondary.TButton", 1, 0),
            ("Salir", self.salir, "Secondary.TButton", 1, 1),
        ]
        for text, command, style_name, row, column in button_specs:
            btn = ttk.Button(btn_frame, text=text, command=command, style=style_name, width=24)
            btn.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            self._buttons.append(btn)
            if text == "Analizar directorio":
                self._tutorial_targets["analizar"] = (btn, "right")
            elif text == "Iniciar organización":
                self._tutorial_targets["iniciar"] = (btn, "right")

        self._cancel_button = ttk.Button(
            btn_frame, text="Cancelar proceso", command=self.cancelar_organizacion,
            style="Secondary.TButton", width=24,
        )
        self._cancel_button.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self._cancel_button.state(["disabled"])

        btn_deshacer = ttk.Button(
            btn_frame,
            text="Deshacer última organización",
            command=self.deshacer_ultima_organizacion,
            style="Secondary.TButton",
            width=24,
        )
        btn_deshacer.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self._buttons.append(btn_deshacer)
        self._feature_controls["undo_organization"] = btn_deshacer

    def _crear_panel_facturas(self, parent):
        """Panel "Extractor de facturas": botones para elegir facturas
        sueltas o una carpeta completa, exportar a CSV y limpiar
        resultados, más una tabla (`Treeview`) que muestra cada factura
        procesada con sus campos extraídos. Doble clic en una fila abre el
        detalle completo (`_mostrar_detalle_factura`)."""
        invoice_frame = ttk.LabelFrame(parent, text="Extractor de facturas", padding=14, style="Card.TLabelframe")
        invoice_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 14))
        invoice_frame.columnconfigure(0, weight=1)
        invoice_frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(invoice_frame)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for text, command in [
            ("Seleccionar facturas", self.seleccionar_facturas),
            ("Analizar carpeta", self.seleccionar_carpeta_facturas),
            ("Exportar CSV", self.exportar_facturas_csv),
            ("Limpiar", self.limpiar_facturas_extraidas),
        ]:
            button = ttk.Button(actions, text=text, command=command, style="Secondary.TButton")
            button.pack(side=tk.LEFT, padx=(0, 8))
            self._buttons.append(button)
            self._invoice_buttons.append(button)

        columns = ("archivo", "proveedor", "numero", "fecha", "nit", "total", "estado")
        table_frame = ttk.Frame(invoice_frame)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        self.invoice_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=7)
        headings = {
            "archivo": "Archivo",
            "proveedor": "Proveedor",
            "numero": "Factura",
            "fecha": "Fecha",
            "nit": "NIT/ID",
            "total": "Total",
            "estado": "Estado",
        }
        widths = {
            "archivo": 150,
            "proveedor": 160,
            "numero": 90,
            "fecha": 80,
            "nit": 100,
            "total": 90,
            "estado": 70,
        }
        for column in columns:
            self.invoice_tree.heading(column, text=headings[column])
            self.invoice_tree.column(column, width=widths[column], minwidth=60, stretch=column in {"archivo", "proveedor"})
        self.invoice_tree.grid(row=0, column=0, sticky="nsew")
        self.invoice_tree.bind("<Double-1>", self._mostrar_detalle_factura)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.invoice_tree.yview)
        self.invoice_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _crear_selector_directorio(self, parent, label, variable, command, tutorial_key=None):
        """Widget reutilizable: una etiqueta, un campo de texto (con la
        ruta elegida) y un botón "Examinar" que abre el diálogo del
        sistema. Se usa tanto para el origen como para el destino."""
        ttk.Label(parent, text=label, font=(FONT_UI, _fs(10), "bold")).pack(anchor=tk.W, pady=(0, 6))
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        button = ttk.Button(row, text="Examinar", command=command, style="Secondary.TButton")
        button.pack(side=tk.RIGHT)
        if tutorial_key:
            self._tutorial_targets[tutorial_key] = (row, "left")

    def iniciar_tutorial(self):
        """Abre una guía visual paso a paso sin modificar ninguna configuración."""
        try:
            if self._tutorial_overlay is not None:
                self._cerrar_tutorial()
            self._tutorial_step_index = 0
            self._mostrar_paso_tutorial()
        except tk.TclError as exc:
            self._cerrar_tutorial()
            messagebox.showerror(
                "No se pudo abrir el tutorial",
                f"Ocurrió un problema al mostrar la guía: {exc}",
                parent=self,
            )

    def _pasos_tutorial(self):
        """Contenido del tutorial: una lista de pasos, cada uno con la
        clave del control al que apunta (debe existir en
        `self._tutorial_targets`, registrado al construir los widgets),
        un título y el texto explicativo. Para agregar o editar un paso
        del tutorial, se edita esta lista."""
        return [
            (
                "ayuda",
                "Guía de uso",
                "Este recorrido explica el flujo completo. Puedes abrirlo cuando quieras desde «Cómo usar» o con F1. "
                "No cambiará tus archivos ni tu configuración.",
            ),
            (
                "origen",
                "1. Elige la carpeta de origen",
                "Pulsa «Examinar» y selecciona la carpeta que contiene los archivos por ordenar. "
                "La aplicación revisa también sus subcarpetas.",
            ),
            (
                "destino",
                "2. Indica el destino",
                "Puedes seleccionar la misma carpeta de origen para organizar sus archivos en subcarpetas, "
                "o una carpeta independiente para conservarlos separados. Las carpetas no pueden contenerse entre sí.",
            ),
            (
                "opciones",
                "3. Ajusta las reglas",
                "Marca las reglas que necesites: categorías, carpetas por año y mes, detección de recibos, "
                "duplicados o mover en lugar de copiar. Si no estás seguro, deja los valores iniciales.",
            ),
            (
                "filtro",
                "4. Filtra por tipo si hace falta",
                "Por defecto se organizan todos los tipos. Desmarca esa opción para elegir una única extensión, "
                "por ejemplo .pdf o .jpg.",
            ),
            (
                "analizar",
                "5. Analiza antes de ejecutar",
                "«Analizar directorio» cuenta y revisa los archivos sin organizarlos. Úsalo para confirmar que "
                "elegiste la carpeta correcta y revisar el registro.",
            ),
            (
                "iniciar",
                "6. Inicia la organización",
                "Cuando estés conforme, pulsa «Iniciar organización». Sigue el progreso y usa «Cancelar proceso» "
                "si necesitas detener la tarea. La opción de deshacer permite revertir la última organización "
                "cuando está disponible.",
            ),
            (
                "registro",
                "7. Revisa el resultado",
                "El registro muestra cada acción, advertencia o error. Al terminar, verifica aquí el resumen "
                "antes de cerrar la aplicación.",
            ),
        ]

    def _revelar_objetivo_tutorial(self, widget, panel):
        """Si el control que este paso del tutorial señala está en un
        panel con scroll (izquierdo o derecho) y no es visible en este
        momento, desplaza ese panel para dejarlo a la vista antes de
        mostrar el globo de ayuda."""
        canvas = None
        if panel == "left":
            canvas = self._left_scroll_canvas
        elif panel == "right":
            canvas = self._right_scroll_canvas
        if canvas is None:
            return
        self.update_idletasks()
        visible_top = canvas.winfo_rooty()
        visible_bottom = visible_top + canvas.winfo_height()
        if widget.winfo_rooty() < visible_top or widget.winfo_rooty() + widget.winfo_height() > visible_bottom:
            region = canvas.bbox("all")
            if region and region[3] > canvas.winfo_height():
                relative_y = widget.winfo_rooty() - visible_top + canvas.canvasy(0)
                fraction = max(0, min(1, (relative_y - canvas.winfo_height() * 0.2) / region[3]))
                canvas.yview_moveto(fraction)
                self.update_idletasks()

    def _mostrar_paso_tutorial(self):
        """Dibuja el paso actual del tutorial: se asegura de que el
        control señalado esté visible, lo resalta con un borde especial,
        y abre (o reemplaza) una ventana flotante con el título, la
        explicación y los botones Anterior/Siguiente, posicionada junto al
        control (a su derecha, o a la izquierda si no cabe a la derecha)."""
        pasos = self._pasos_tutorial()
        key, title, message = pasos[self._tutorial_step_index]
        target, panel = self._tutorial_targets.get(key, (self, None))
        self._revelar_objetivo_tutorial(target, panel)
        self.update_idletasks()

        if self._tutorial_overlay is not None:
            self._tutorial_overlay.destroy()
        self._restaurar_resaltado_tutorial()
        self._resaltar_objetivo_tutorial(target)

        paleta = self._paleta()
        overlay = tk.Toplevel(self)
        self._tutorial_overlay = overlay
        overlay.title("Tutorial de FileSync Pro")
        overlay.transient(self)
        overlay.resizable(False, False)
        overlay.configure(background=paleta["bg_card"])
        overlay.protocol("WM_DELETE_WINDOW", self._cerrar_tutorial)
        overlay.bind("<Escape>", lambda _: self._cerrar_tutorial())

        content = tk.Frame(overlay, background=paleta["bg_card"], padx=18, pady=16)
        content.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            content,
            text="➜  Control señalado",
            background=paleta["bg_card"],
            foreground=paleta["accent"],
            font=(FONT_UI, _fs(9), "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            content,
            text=title,
            background=paleta["bg_card"],
            foreground=paleta["text_primary"],
            font=(FONT_UI, _fs(13), "bold"),
        ).pack(anchor=tk.W, pady=(6, 8))
        tk.Label(
            content,
            text=message,
            background=paleta["bg_card"],
            foreground=paleta["text_muted"],
            font=(FONT_UI, _fs(10)),
            wraplength=390,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, fill=tk.X)

        actions = tk.Frame(content, background=paleta["bg_card"])
        actions.pack(fill=tk.X, pady=(16, 0))
        tk.Label(
            actions,
            text=f"Paso {self._tutorial_step_index + 1} de {len(pasos)}",
            background=paleta["bg_card"],
            foreground=paleta["text_secondary"],
            font=(FONT_UI, _fs(9), "bold"),
        ).pack(side=tk.LEFT)
        boton_opciones = {
            "bg": paleta["btn_bg"],
            "fg": paleta["text_primary"],
            "activebackground": paleta["btn_bg_hover"],
            "activeforeground": paleta["text_primary"],
            "highlightbackground": paleta["border"],
            "relief": "flat",
            "borderwidth": 1,
        }
        def crear_boton(texto, comando):
            if IS_MAC:
                # El `tk.Button` clásico de macOS (Aqua) ignora los colores de
                # fondo y deja el texto ilegible en modo oscuro: se usa el
                # botón ttk (tema clam) que ya respeta la paleta de la app.
                return ttk.Button(actions, text=texto, command=comando, style="Secondary.TButton")
            return tk.Button(actions, text=texto, command=comando, **boton_opciones)

        if self._tutorial_step_index > 0:
            crear_boton("‹ Anterior", lambda: self._cambiar_paso_tutorial(-1)).pack(side=tk.RIGHT)
        next_label = "Finalizar" if self._tutorial_step_index == len(pasos) - 1 else "Siguiente ›"
        crear_boton(next_label, lambda: self._cambiar_paso_tutorial(1)).pack(side=tk.RIGHT, padx=(0, 8))

        overlay.update_idletasks()
        target_x, target_y = target.winfo_rootx(), target.winfo_rooty()
        popup_width, popup_height = overlay.winfo_reqwidth(), overlay.winfo_reqheight()
        screen_width, screen_height = overlay.winfo_screenwidth(), overlay.winfo_screenheight()
        preferred_x = target_x + target.winfo_width() + 16
        if preferred_x + popup_width > screen_width - 16:
            preferred_x = target_x - popup_width - 16
        popup_x = max(16, min(screen_width - popup_width - 16, preferred_x))
        popup_y = max(16, min(screen_height - popup_height - 48, target_y))
        overlay.geometry(f"+{popup_x}+{popup_y}")
        overlay.lift()
        overlay.focus_set()

    def _resaltar_objetivo_tutorial(self, target):
        """Cambia el estilo visual del control señalado para que resalte
        (borde de color), recordando su estilo original para poder
        restaurarlo después con `_restaurar_resaltado_tutorial`."""
        try:
            previous_style = target.cget("style")
            widget_class = target.winfo_class()
            if widget_class == "TButton":
                tutorial_style = "TutorialFocus.TButton"
            elif widget_class == "TLabelframe":
                tutorial_style = "TutorialFocus.TLabelframe"
            else:
                tutorial_style = "TutorialFocus.TFrame"
            target.configure(style=tutorial_style)
            self._tutorial_target_style = (target, previous_style)
        except tk.TclError:
            self._tutorial_target_style = None

    def _restaurar_resaltado_tutorial(self):
        """Quita el resaltado del control anteriormente señalado,
        devolviéndole su estilo original."""
        if self._tutorial_target_style is None:
            return
        target, previous_style = self._tutorial_target_style
        try:
            target.configure(style=previous_style)
        except tk.TclError:
            pass
        self._tutorial_target_style = None

    def _cambiar_paso_tutorial(self, direction):
        """Avanza (+1) o retrocede (-1) un paso del tutorial. Si se avanza
        más allá del último paso, se cierra el tutorial en vez de fallar."""
        next_index = self._tutorial_step_index + direction
        if next_index >= len(self._pasos_tutorial()):
            self._cerrar_tutorial()
            return
        if next_index >= 0:
            self._tutorial_step_index = next_index
            self._mostrar_paso_tutorial()

    def _cerrar_tutorial(self):
        """Cierra la ventana flotante del tutorial (si está abierta) y
        quita cualquier resaltado que hubiera quedado activo."""
        if self._tutorial_overlay is not None:
            self._tutorial_overlay.destroy()
            self._tutorial_overlay = None
        self._restaurar_resaltado_tutorial()

    @staticmethod
    def _crear_stat_card(parent, row, column, label, variable):
        """Tarjeta pequeña con una etiqueta y un valor grande, usada en la
        sección de "Estadísticas" (total, procesados, duplicados, categorías)."""
        card = ttk.Frame(parent, padding=(12, 10), style="StatCard.TFrame")
        card.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
        ttk.Label(card, text=label, style="StatLabel.TLabel").pack(anchor=tk.W)
        ttk.Label(card, textvariable=variable, style="StatValue.TLabel").pack(anchor=tk.W, pady=(6, 0))

    # --- Mecánica de scroll de los paneles izquierdo/derecho ---
    # Cada panel es un Canvas con una sola "ventana" adentro (el contenido
    # real). Estos métodos, repetidos en pareja para cada panel, mantienen
    # el área de scroll actualizada cuando cambia el contenido
    # (_actualizar_scroll_*), hacen que el contenido ocupe todo el ancho
    # disponible (_ajustar_ancho_*), y activan/desactivan el scroll con la
    # rueda del mouse solo cuando el cursor está sobre ese panel en
    # concreto (_activar_/_desactivar_scroll_*), para que mover la rueda
    # sobre el panel izquierdo no desplace por accidente el derecho.
    def _actualizar_scroll_panel_izquierdo(self, _event=None):
        if self._left_scroll_canvas is None:
            return
        self._left_scroll_canvas.configure(scrollregion=self._left_scroll_canvas.bbox("all"))

    def _ajustar_ancho_panel_izquierdo(self, event):
        if self._left_scroll_canvas is None or self._left_scroll_window is None:
            return
        self._left_scroll_canvas.itemconfigure(self._left_scroll_window, width=event.width)

    def _activar_scroll_panel_izquierdo(self, _event=None):
        self._scroll_activo_canvas = self._left_scroll_canvas
        self.bind_all("<MouseWheel>", self._scroll_panel_mousewheel)

    def _desactivar_scroll_panel_izquierdo(self, _event=None):
        self._scroll_activo_canvas = None
        self.unbind_all("<MouseWheel>")

    def _actualizar_scroll_panel_derecho(self, _event=None):
        if self._right_scroll_canvas is None:
            return
        self._right_scroll_canvas.configure(scrollregion=self._right_scroll_canvas.bbox("all"))

    def _ajustar_ancho_panel_derecho(self, event):
        if self._right_scroll_canvas is None or self._right_scroll_window is None:
            return
        self._right_scroll_canvas.itemconfigure(self._right_scroll_window, width=event.width)

    def _activar_scroll_panel_derecho(self, _event=None):
        self._scroll_activo_canvas = self._right_scroll_canvas
        self.bind_all("<MouseWheel>", self._scroll_panel_mousewheel)

    def _desactivar_scroll_panel_derecho(self, _event=None):
        self._scroll_activo_canvas = None
        self.unbind_all("<MouseWheel>")

    @staticmethod
    def _scroll_unit_px():
        """Píxeles que mide una "unidad" de scroll en los dos paneles con
        scroll. En Windows es 0, que en Tk significa "un décimo del alto
        visible" (lo que siempre se usó); en macOS se fija a un valor pequeño
        porque el trackpad genera muchos eventos seguidos (ver
        `_scroll_panel_mousewheel`)."""
        return MAC_SCROLL_UNIT_PX if IS_MAC else 0

    def _scroll_panel_mousewheel(self, event):
        """Desplaza con la rueda del mouse el panel que esté "activo" en
        este momento (sobre el que está el cursor)."""
        canvas = self._scroll_activo_canvas
        if canvas is None:
            return
        if IS_MAC:
            # macOS entrega deltas pequeños y proporcionales a la velocidad del
            # gesto: se usa el delta tal cual (sobre unidades de
            # `_scroll_unit_px` píxeles) en vez de un paso fijo por evento.
            canvas.yview_scroll(-event.delta, "units")
            return
        delta = -1 if event.delta > 0 else 1
        canvas.yview_scroll(delta, "units")

    def actualizar_estado_licencia(self):
        """Refresca el texto de la barra de licencia (en la cabecera) y
        vuelve a aplicar las restricciones del plan actual sobre los
        controles de la interfaz. Se llama cada vez que el estado de la
        licencia pudo haber cambiado (al iniciar, tras activar/verificar,
        tras sincronizar con el servidor...). También corrige de una vez
        el estado si la suscripción ya venció pero seguía marcada como
        activa."""
        state = self.license_manager.state
        if state.get("status") == "active" and self.license_manager.is_subscription_expired():
            self.license_manager.set_validation_status("inactive")
            state = self.license_manager.state
        self.subscription_id.set(state.get("subscription_id", ""))
        self.subscription_email.set(state.get("subscription_email", ""))
        self.subscription_plan_code.set(state.get("subscription_plan_code", ""))
        access_mode = self.license_manager.local_unrestricted_access_mode()
        if access_mode == "owner":
            machine_name = self.license_manager.get_machine_name() or "este equipo"
            text = f"Equipo propietario ({machine_name}) | Funciones desbloqueadas"
        elif access_mode == "developer":
            text = "Modo desarrollador activo | Pruebas locales"
        elif state.get("status") == "active":
            plan_name = self._nombre_plan(self._plan_code_activo())
            days_left = self.license_manager.days_until_block()
            if days_left is not None:
                text = f"{plan_name} activa | Se bloquea en {max(days_left, 0)} día(s)"
            else:
                text = f"{plan_name} activa | Dispositivo validado"
            text += self._monthly_quota_text()
        elif state.get("subscription_email"):
            text = f"Compra en proceso | Plan {self._nombre_plan(self._plan_code_activo())}"
        else:
            text = "Compra una licencia para desbloquear la app"
        self.status_text.set(text)
        self._aplicar_restricciones_plan()

    def _mostrar_avisos_licencia(self):
        """Muestra, como mucho una vez por cada fecha de vencimiento (para
        no repetir el mismo aviso cada vez que se abre la app), una
        ventana de advertencia cuando quedan entre 4-7 días o 0-3 días
        para que la suscripción se bloquee. Se llama poco después de
        arrancar (ver `after(700, ...)` en `__init__`)."""
        if self.license_manager.has_local_unrestricted_access():
            return
        if self.license_manager.state.get("status") != "active":
            return

        expires_at = self.license_manager.state.get("subscription_expires_at", "").strip()
        days_left = self.license_manager.days_until_block()
        if not expires_at or days_left is None:
            return

        plan_name = self._nombre_plan(self.license_manager.state.get("subscription_plan_code", ""))
        email = self.license_manager.state.get("email", "tu cuenta")

        if 4 <= days_left <= 7 and self.license_manager.state.get("subscription_warning_7_for") != expires_at:
            self.license_manager.state["subscription_warning_7_for"] = expires_at
            self.license_manager._save_state()
            messagebox.showwarning(
                "Aviso de licencia",
                f"A la {plan_name} de {email} le quedan {days_left} días.\n\n"
                "Cuando se cumplan los 30 días sin renovar, se bloquearán las funciones de esa licencia.",
            )
            return

        if 0 <= days_left <= 3 and self.license_manager.state.get("subscription_warning_3_for") != expires_at:
            self.license_manager.state["subscription_warning_3_for"] = expires_at
            self.license_manager._save_state()
            messagebox.showwarning(
                "Aviso de licencia",
                f"A la {plan_name} de {email} le quedan {days_left} días.\n\n"
                "Faltan 3 días o menos para que se bloqueen las funciones de esa licencia si no se renueva el pago.",
            )

    def _sincronizar_licencia_silenciosa(self):
        """Revisa el estado de la licencia contra el servidor sin
        interrumpir al usuario con diálogos de error (solo muestra algo si
        hay buenas noticias: una compra pendiente que ya se confirmó).
        Se llama al arrancar y cada vez que la ventana recupera el foco
        (por ejemplo, cuando el usuario vuelve de pagar en el navegador).

        Dos casos posibles:
        1. Hay una compra "pendiente" (el usuario iba a pagar un plan pero
           todavía no se ha confirmado ni activado localmente): se
           consulta si ya se puede resolver, y si sí, se activa la
           licencia y se avisa al usuario con un mensaje de éxito.
        2. Ya hay una licencia activada localmente: simplemente se revalida
           contra el servidor (por si venció, se canceló, etc.) y se
           actualiza el estado en silencio.
        """
        if self.license_manager.has_local_unrestricted_access():
            self.actualizar_estado_licencia()
            return

        state = self.license_manager.state
        pending_email = state.get("subscription_email", "").strip()
        pending_plan_code = state.get("subscription_plan_code", "").strip()

        if pending_email and pending_plan_code and (not state.get("license_key") or not state.get("email")):
            try:
                response = self.api_client.resolve_subscription(pending_email, pending_plan_code)
            except LicensingError:
                return

            status = response.get("status", "")
            license_key = response.get("license_key")
            if license_key and status in {"authorized", "active"}:
                try:
                    activated, _ = self._registrar_licencia_confirmada(
                        response.get("email", pending_email),
                        license_key,
                        subscription_id=response.get("subscription_id", ""),
                        subscription_plan_code=response.get("plan_code", pending_plan_code),
                        subscription_expires_at=response.get("next_payment_date", ""),
                    )
                except LicensingError:
                    return
                if not activated:
                    return
                self.actualizar_estado_licencia()
                self._mostrar_avisos_licencia()
                messagebox.showinfo("Licencia activada", "Tu compra fue confirmada y la app ya quedó desbloqueada.")
            return

        if state.get("license_key") and state.get("email"):
            try:
                response = self.api_client.validate_license(
                    state["email"], state["license_key"], self.license_manager.get_device_id()
                )
            except LicensingError:
                return

            if response.get("status") == "active":
                self.license_manager.activate_local(
                    state["email"],
                    state["license_key"],
                    subscription_id=response.get("subscription_id", state.get("subscription_id", "")),
                    subscription_plan_code=response.get("plan_code", state.get("subscription_plan_code", "")),
                    subscription_expires_at=response.get("next_payment_date", state.get("subscription_expires_at", "")),
                )
            else:
                self.license_manager.set_validation_status("inactive")
            self.actualizar_estado_licencia()

    def _registrar_licencia_confirmada(
        self,
        email,
        license_key,
        subscription_id="",
        subscription_plan_code="",
        subscription_expires_at="",
    ):
        """Activa formalmente una licencia contra el servidor (registra
        este dispositivo) y, si sale bien, guarda el resultado localmente.
        Devuelve `(True, "")` si se activó, o `(False, mensaje_de_error)`
        si no."""
        activation = self.api_client.activate_license(email, license_key, self.license_manager.get_device_id())
        if activation.get("status") != "active":
            return False, activation.get("message", "No se pudo activar la licencia en este equipo.")
        self.license_manager.activate_local(
            email,
            license_key,
            subscription_id=activation.get("subscription_id", subscription_id),
            subscription_plan_code=activation.get("plan_code", subscription_plan_code),
            subscription_expires_at=activation.get("next_payment_date", subscription_expires_at),
        )
        return True, ""

    def actualizar_disponibilidad_tipo(self):
        """Habilita el selector de "solo esta extensión" únicamente
        cuando el plan lo permite y el usuario desmarcó "organizar todos
        los tipos"."""
        features = self._plan_features(self._plan_code_activo())
        if not features["type_filter"]:
            self.combo_tipo.configure(state="disabled")
            return
        self.combo_tipo.configure(state="disabled" if self.organizar_todos.get() else "readonly")

    def _cargar_configuracion_guardada(self, state):
        """Restaura en los controles de la interfaz la configuración
        (origen, destino, opciones) guardada de una corrida anterior que
        se está reanudando."""
        self.directorio_origen.set(str(state.get("origen", "")).strip())
        self.directorio_destino.set(str(state.get("destino", "")).strip())
        self.organizar_por_categoria.set(bool(state.get("organizar_por_categoria", True)))
        self.organizar_por_fecha.set(bool(state.get("organizar_por_fecha", True)))
        self.detectar_recibos.set(bool(state.get("detectar_recibos", True)))
        self.eliminar_duplicados.set(bool(state.get("eliminar_duplicados", True)))
        self.mover_en_vez_de_copiar.set(bool(state.get("mover_en_vez_de_copiar", True)))
        self.eliminar_carpetas_vacias.set(bool(state.get("eliminar_carpetas_vacias", False)))
        self.organizar_todos.set(bool(state.get("organizar_todos", True)))
        self.tipo_archivo_seleccionado.set(str(state.get("tipo_archivo", "")).strip())
        self.actualizar_disponibilidad_tipo()

    def _preparar_estado_visual_ejecucion(self, total_archivos, revisados=0, procesados=0, duplicados=0, categorias=0):
        """Actualiza las tarjetas de estadísticas y la barra de progreso
        con los números dados (se usa tanto al empezar una corrida nueva
        como al reanudar una, con los contadores ya avanzados)."""
        self.estadisticas["total"].set(str(total_archivos))
        self.estadisticas["procesados"].set(str(procesados))
        self.estadisticas["duplicados"].set(str(duplicados))
        self.estadisticas["categorias"].set(str(categorias))
        self.progreso.configure(maximum=max(total_archivos, 1), value=min(revisados, total_archivos))

    def _registrar_evento_recuperacion(self, journal_buffer, index, status, origen, destino="", categoria="", file_hash=""):
        """Agrega una línea al "diario" en memoria (`journal_buffer`) por
        cada archivo procesado, sin escribir a disco todavía (eso lo hace
        `_vaciar_buffer_recuperacion` en lotes, para no golpear el disco
        con cada archivo individual)."""
        payload = {
            "index": index,
            "status": status,
            "origen": str(origen),
            "destino": str(destino),
            "categoria": categoria,
            "hash": file_hash,
            "timestamp": dt.datetime.utcnow().isoformat(),
        }
        journal_buffer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _vaciar_buffer_recuperacion(journal_handle, journal_buffer):
        """Escribe a disco todo lo acumulado en el buffer del diario y lo
        vacía. Escribir en lotes (en vez de línea por línea) es mucho más
        rápido cuando se procesan muchos archivos."""
        if not journal_buffer:
            return
        journal_handle.writelines(journal_buffer)
        journal_handle.flush()
        journal_buffer.clear()

    def _persistir_resumen_parcial(self, revisados):
        """Guarda el progreso general (`process_state.json`) cada cierto
        número de archivos o cada segundo como mucho (lo que ocurra
        primero), para no perder demasiado avance si la app se cierra a
        mitad de camino, sin tampoco escribir a disco constantemente."""
        if revisados <= 0:
            return
        ahora = time.monotonic()
        if revisados % self._recovery_batch_size != 0 and (ahora - self._last_recovery_update) < 1.0:
            return
        stats = self.organizador.estadisticas
        self.process_recovery.update_state(
            next_index=revisados,
            procesados=stats["procesados"],
            duplicados=stats["duplicados"],
            errores=stats["errores"],
            categorias_usadas=sorted(stats["categorias_usadas"]),
        )
        self._last_recovery_update = ahora

    def _ofrecer_reanudar_proceso(self):
        """Se llama poco después de abrir la app: si quedó guardada una
        organización que no terminó (interrumpida o en curso cuando se
        cerró la app), le pregunta al usuario si quiere continuarla,
        mostrando un resumen (origen, destino, cuánto ya se procesó,
        cuánto falta). Si acepta, delega en `_reanudar_organizacion_guardada`."""
        if self._run_in_progress:
            return
        state = self.process_recovery.load_state()
        if not state or state.get("status") not in {"running", "interrupted"}:
            return

        files = self.process_recovery.get_manifest_files(state)
        if not files:
            self.process_recovery.clear()
            return

        snapshot = self.process_recovery.rebuild_runtime_state(state)
        pendientes = max(len(files) - snapshot["next_index"], 0)
        if pendientes <= 0:
            self.process_recovery.clear()
            return

        tipo = "Todos" if state.get("organizar_todos", True) else (state.get("tipo_archivo") or "Filtro")
        mensaje = (
            "Encontramos una organización pendiente de la sesión anterior.\n\n"
            f"Origen: {state.get('origen', '')}\n"
            f"Destino: {state.get('destino', '')}\n"
            f"Tipo: {tipo}\n"
            f"Procesados antes del cierre: {snapshot['procesados']}\n"
            f"Pendientes por revisar: {pendientes}\n\n"
            "¿Deseas continuar desde el último punto guardado?"
        )
        if not messagebox.askyesno("Continuar organización", mensaje):
            self.process_recovery.clear()
            self.log("Se descartó la organización pendiente guardada.", "info")
            return

        self._reanudar_organizacion_guardada(state, files, snapshot)

    def _reanudar_organizacion_guardada(self, state, files=None, snapshot=None):
        """Restaura toda la configuración y el progreso de una corrida
        guardada (opciones elegidas, hashes ya vistos para seguir
        detectando duplicados, contadores) y arranca el hilo de
        organización empezando justo después del último archivo ya
        procesado, en vez de desde cero."""
        if not self._asegurar_acceso():
            return

        files = files or self.process_recovery.get_manifest_files(state)
        if not files:
            self.process_recovery.clear()
            messagebox.showwarning("No se pudo reanudar", "No encontramos la lista de archivos de la ejecución pendiente.")
            return

        snapshot = snapshot or self.process_recovery.rebuild_runtime_state(state)
        next_index = min(snapshot["next_index"], len(files))
        if next_index >= len(files):
            self.process_recovery.clear()
            messagebox.showinfo("Nada pendiente", "La organización guardada ya no tiene archivos pendientes.")
            return

        self._cargar_configuracion_guardada(state)
        self._current_run_limit = state.get("limite_ejecucion")
        self.organizador.reset()
        self.organizador.hashes_archivos = dict(snapshot["hashes"])
        self.organizador.estadisticas["total_archivos"] = len(files)
        self.organizador.estadisticas["procesados"] = snapshot["procesados"]
        self.organizador.estadisticas["duplicados"] = snapshot["duplicados"]
        self.organizador.estadisticas["errores"] = snapshot["errores"]
        self.organizador.estadisticas["categorias_usadas"] = set(snapshot["categorias_usadas"])
        self._preparar_estado_visual_ejecucion(
            len(files),
            revisados=next_index,
            procesados=snapshot["procesados"],
            duplicados=snapshot["duplicados"],
            categorias=len(snapshot["categorias_usadas"]),
        )
        self.log(
            f"Se reanuda una organización pendiente. Revisados: {next_index}/{len(files)} | Procesados: {snapshot['procesados']}.",
            "info",
        )
        self._iniciar_hilo_organizacion(files, state, start_index=next_index)

    def seleccionar_origen(self):
        """Abre el diálogo del sistema para elegir la carpeta de origen."""
        seleccionado = filedialog.askdirectory(title="Seleccionar directorio de origen")
        if seleccionado:
            self.directorio_origen.set(seleccionado)
            self.log("Directorio de origen seleccionado.", "info")

    def seleccionar_destino(self):
        """Abre el diálogo del sistema para elegir la carpeta de destino."""
        seleccionado = filedialog.askdirectory(title="Seleccionar directorio de destino")
        if seleccionado:
            self.directorio_destino.set(seleccionado)
            self.log("Directorio de destino seleccionado.", "info")

    def log(self, mensaje, tipo="normal"):
        """Agrega una línea con hora al panel de "Registro de actividad",
        coloreada según el tipo (éxito/error/advertencia/info), y también
        la manda al archivo de log (`filesync_pro.log`) con el nivel que
        corresponda."""
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        prefijos = {"exito": "[OK] ", "error": "[ERROR] ", "advertencia": "[WARN] ", "info": "[INFO] ", "subtitulo": ""}
        linea = f"[{timestamp}] {prefijos.get(tipo, '')}{mensaje}\n"
        getattr(LOGGER, {"error": "error", "advertencia": "warning"}.get(tipo, "info"))(mensaje)
        self.texto_log.insert(tk.END, linea)
        if tipo != "normal":
            self.texto_log.tag_add(tipo, "end-2l linestart", "end-2l lineend")
        self.texto_log.see(tk.END)

    def limpiar_log(self):
        """Borra todo el contenido del panel de registro."""
        self.texto_log.delete("1.0", tk.END)
        self.log("Registro limpiado.", "info")

    def seleccionar_facturas(self):
        """Deja elegir una o varias facturas sueltas (imagen, PDF o texto)
        y lanza su extracción."""
        if not self._asegurar_acceso():
            return
        paths = filedialog.askopenfilenames(
            title="Seleccionar facturas",
            filetypes=[
                ("Facturas e imagenes", "*.pdf *.png *.jpg *.jpeg *.bmp *.tiff *.webp *.txt *.csv *.xml"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if paths:
            self._iniciar_extraccion_facturas([Path(path) for path in paths])

    def seleccionar_carpeta_facturas(self):
        """Deja elegir una carpeta completa, busca dentro (y en
        subcarpetas) todos los archivos que la app sabe leer como
        factura, y lanza su extracción en conjunto."""
        if not self._asegurar_acceso():
            return
        folder = filedialog.askdirectory(title="Seleccionar carpeta de facturas")
        if not folder:
            return
        files = []
        for root, _dirnames, filenames in walk_files(folder):
            for filename in filenames:
                path = Path(root) / filename
                if self.invoice_extractor.is_supported(path):
                    files.append(path)
        files.sort(key=lambda path: str(path).lower())
        if not files:
            messagebox.showinfo("Sin facturas", "No se encontraron archivos compatibles en esa carpeta.")
            return
        self._iniciar_extraccion_facturas(files)

    def _iniciar_extraccion_facturas(self, paths):
        """Filtra a solo los archivos soportados, comprueba que quede uso
        mensual disponible, limpia resultados previos, y lanza el trabajo
        pesado (OCR/lectura de PDF) en un hilo aparte para no congelar la
        interfaz mientras procesa."""
        supported_paths = [Path(path) for path in paths if self.invoice_extractor.is_supported(path)]
        if not supported_paths:
            messagebox.showinfo("Sin archivos compatibles", "Selecciona facturas en PDF digital, imagen o texto.")
            return
        if not self._asegurar_uso_mensual_disponible():
            return

        self.limpiar_facturas_extraidas(confirm=False)
        self.progreso["maximum"] = len(supported_paths)
        self.progreso["value"] = 0
        self._cambiar_estado_botones_facturas("disabled")
        self.log(f"Extractor de facturas: {len(supported_paths)} archivo(s) en cola.", "info")
        thread = threading.Thread(target=self._ejecutar_extraccion_facturas, args=(supported_paths,), daemon=True)
        thread.start()

    def _ejecutar_extraccion_facturas(self, paths):
        """Corre en un hilo aparte (`threading.Thread`, ver
        `_iniciar_extraccion_facturas`): procesa cada factura una por una.
        Tkinter no es seguro de tocar desde otro hilo, así que cada
        actualización de la interfaz (agregar fila, mover progreso) se
        programa con `self.after(0, ...)`, que la ejecuta en el hilo
        principal en cuanto pueda."""
        for index, path in enumerate(paths, start=1):
            result = self.invoice_extractor.extract(path)
            self.after(0, self._agregar_factura_extraida, result)
            self.after(0, self._actualizar_progreso_facturas, index)
        self.after(0, self._finalizar_extraccion_facturas, len(paths))

    def _agregar_factura_extraida(self, result):
        """Agrega una fila a la tabla de facturas con el resultado de una
        extracción."""
        self.invoice_results.append(result)
        if self.invoice_tree is None:
            return
        iid = str(len(self.invoice_results) - 1)
        self.invoice_tree.insert(
            "",
            tk.END,
            iid=iid,
            values=(
                result.get("archivo", ""),
                result.get("proveedor", ""),
                result.get("numero", ""),
                result.get("fecha", ""),
                result.get("nit", ""),
                result.get("total", ""),
                result.get("estado", ""),
            ),
        )

    def _actualizar_progreso_facturas(self, value):
        self.progreso["value"] = value

    def _finalizar_extraccion_facturas(self, total):
        """Al terminar de procesar todas las facturas de la cola: cuenta
        cuántas quedaron completas/parciales/con error, registra un uso
        mensual (una sola vez por lote, no por factura), reactiva los
        botones y muestra un resumen final."""
        ok_count = sum(1 for result in self.invoice_results if result.get("estado") == "ok")
        partial_count = sum(1 for result in self.invoice_results if result.get("estado") == "parcial")
        error_count = max(total - ok_count - partial_count, 0)
        if total > 0:
            self._registrar_uso_mensual("extractor de facturas")
            self.actualizar_estado_licencia()
        self._cambiar_estado_botones_facturas("normal")
        self.log(
            f"Extractor finalizado: {ok_count} ok, {partial_count} parcial(es), {error_count} sin texto/error.",
            "exito" if ok_count else "advertencia",
        )
        messagebox.showinfo(
            "Extraccion finalizada",
            f"Facturas revisadas: {total}\n"
            f"Completas: {ok_count}\n"
            f"Parciales: {partial_count}\n"
            f"Sin texto o error: {error_count}",
        )

    def exportar_facturas_csv(self):
        """Guarda todos los resultados extraídos hasta ahora en un archivo
        CSV que el usuario elige, con codificación `utf-8-sig` (para que
        Excel en Windows lea bien las tildes y la ñ)."""
        if not self.invoice_results:
            messagebox.showinfo("Sin datos", "Primero extrae informacion de una o varias facturas.")
            return
        output_path = filedialog.asksaveasfilename(
            title="Exportar facturas",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not output_path:
            return
        fields = ("archivo", "ruta", "proveedor", "numero", "fecha", "nit", "email", "subtotal", "iva", "total", "confianza", "estado")
        try:
            with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for result in self.invoice_results:
                    writer.writerow({field: result.get(field, "") for field in fields})
        except OSError as exc:
            messagebox.showerror("No se pudo exportar", str(exc))
            return
        self.log(f"Facturas exportadas a CSV: {output_path}", "exito")
        messagebox.showinfo("CSV exportado", "La informacion extraida fue exportada correctamente.")

    def limpiar_facturas_extraidas(self, confirm=True):
        """Vacía la tabla de facturas y la barra de progreso. Pide
        confirmación salvo que `confirm=False` (se usa así internamente,
        por ejemplo antes de empezar una extracción nueva)."""
        if confirm and self.invoice_results and not messagebox.askyesno("Limpiar facturas", "Deseas limpiar los resultados extraidos?"):
            return
        self.invoice_results = []
        if self.invoice_tree is not None:
            for item in self.invoice_tree.get_children():
                self.invoice_tree.delete(item)
        if hasattr(self, "progreso"):
            self.progreso["value"] = 0

    def _mostrar_detalle_factura(self, _event=None):
        """Al hacer doble clic en una fila de la tabla de facturas, abre
        una ventana con todos los campos extraídos y el texto completo
        reconocido (OCR/PDF), para que el usuario pueda verificar o
        corregir manualmente lo que la app detectó."""
        if self.invoice_tree is None:
            return
        selected = self.invoice_tree.selection()
        if not selected:
            return
        try:
            result = self.invoice_results[int(selected[0])]
        except (ValueError, IndexError):
            return

        paleta = self._paleta()
        dialog = tk.Toplevel(self)
        dialog.title("Detalle de factura")
        dialog.transient(self)
        dialog.configure(bg=paleta["bg_app"])
        dialog.geometry(f"760x520+{self.winfo_rootx() + 90}+{self.winfo_rooty() + 70}")
        wrapper = ttk.Frame(dialog, padding=14)
        wrapper.pack(fill=tk.BOTH, expand=True)

        summary = (
            f"Archivo: {result.get('archivo', '')}\n"
            f"Proveedor: {result.get('proveedor', '')}\n"
            f"Factura: {result.get('numero', '')}\n"
            f"Fecha: {result.get('fecha', '')}\n"
            f"NIT/ID: {result.get('nit', '')}\n"
            f"Subtotal: {result.get('subtotal', '')} | IVA: {result.get('iva', '')} | Total: {result.get('total', '')}\n"
            f"Confianza: {result.get('confianza', '')} | Estado: {result.get('estado', '')}\n"
            f"Ruta: {result.get('ruta', '')}\n"
        )
        ttk.Label(wrapper, text=summary, justify=tk.LEFT, wraplength=720).pack(fill=tk.X, anchor=tk.W, pady=(0, 8))
        text_box = scrolledtext.ScrolledText(
            wrapper,
            height=16,
            font=(FONT_MONO, _fs(9)),
            wrap=tk.WORD,
            bg=paleta["log_bg"],
            fg=paleta["text_primary"],
            insertbackground=paleta["text_primary"],
        )
        text_box.pack(fill=tk.BOTH, expand=True)
        text_box.insert(tk.END, result.get("texto", ""))
        text_box.configure(state="disabled")
        ttk.Button(wrapper, text="Cerrar", command=dialog.destroy, style="Secondary.TButton").pack(anchor=tk.E, pady=(10, 0))

    def _cambiar_estado_botones_facturas(self, estado):
        """Habilita/deshabilita todos los botones de la pestaña de
        facturas a la vez (se deshabilitan mientras hay una extracción en
        curso, para evitar iniciar dos a la vez)."""
        for button in self._invoice_buttons:
            button.state(["disabled"] if estado == "disabled" else ["!disabled"])

    def comprar_licencia(self):
        """Flujo completo de compra: pide y confirma el correo, deja
        elegir el plan, y según el tipo de acceso del equipo:
        - Equipo "owner": avisa que ya tiene todo desbloqueado, no hace
          falta comprar.
        - Modo "developer": simula la compra sin cobrar ni abrir Wompi
          (para poder probar el flujo completo en desarrollo).
        - Caso normal: guarda la intención de compra localmente
          ("pendiente") y abre el link de pago de Wompi en el navegador.
          La confirmación real llega después, cuando Make registra el
          pago y `_sincronizar_licencia_silenciosa` lo detecta.
        """
        plan_links = self._plan_links()
        plans = self._sort_plan_codes(plan_links.keys())
        if not plans:
            messagebox.showerror(
                "Compra no disponible",
                "La compra no está disponible por el momento.\n\nIntenta nuevamente en unos minutos.",
            )
            self.log("La compra no está disponible en este momento.", "error")
            return

        email = self._pedir_correo_confirmado(
            "Comprar licencia",
            "Escribe tu correo dos veces para evitar errores antes de abrir el pago en Wompi.",
        )
        if not email:
            return

        default_plan = self._default_plan_freq_code(plan_links, plans)
        plan_code = self._elegir_plan(plans, default_plan)
        if not plan_code:
            return
        if plan_code not in plan_links:
            messagebox.showerror(
                "Plan no válido",
                "Ese plan no está disponible en este momento.",
            )
            return

        plan_base, plan_freq = self._dividir_plan_frecuencia(plan_code)
        plan_name = self._nombre_plan(plan_base)
        access_mode = self.license_manager.local_unrestricted_access_mode()
        if access_mode == "owner":
            machine_name = self.license_manager.get_machine_name() or "este equipo"
            self.log(f"El equipo propietario {machine_name} ya tiene acceso completo. No hace falta comprar el plan {plan_base}.", "info")
            messagebox.showinfo(
                "Equipo propietario",
                f"Este equipo ({machine_name}) ya tiene todas las funciones desbloqueadas.\n\nNo hace falta comprar ni activar una licencia aquí.",
            )
            return
        if access_mode == "developer":
            self.log(f"Simulación de compra en modo desarrollador para {email}. Plan {plan_base} ({plan_freq}).", "info")
            messagebox.showinfo(
                "Modo desarrollador",
                f"Prueba local completada.\n\nCorreo: {email}\nPlan: {plan_name} ({plan_freq})\n\nNo se abrirá Wompi ni se realizará un cobro real.",
            )
            return

        checkout_url = plan_links[plan_code]
        self.license_manager.remember_pending_subscription(email, plan_base)
        self.actualizar_estado_licencia()
        self.log(f"Link de plan local listo para {email}. Plan {plan_code}.", "info")
        if checkout_url:
            webbrowser.open(checkout_url)
            messagebox.showinfo(
                "Suscripción iniciada",
                "Se abrió Wompi en tu navegador.\n\n"
                "Usa el mismo correo que acabas de escribir para completar tu compra.\n\n"
                "Cuando termines, vuelve a la app y tu licencia se revisará automáticamente con Make.",
            )

    def verificar_licencia(self):
        """Botón "Verificar licencia": revisa el estado actual y decide
        qué hacer según el caso:
        - Equipo owner/developer: informa que ya tiene acceso completo.
        - No hay licencia guardada localmente: si hay una compra
          pendiente recordada en este equipo, intenta resolverla sola
          (`_verificar_suscripcion_pendiente`); si no, pide SOLO el correo
          (nada de plan — el plan real ya está en el Sheet, elegir uno acá
          no cambiaba qué licencia se encontraba, solo agregaba un paso) y
          busca con eso la compra.
        - Ya hay una licencia guardada: la revalida contra el servidor y
          actualiza el estado (activa o inválida) según la respuesta.
        """
        access_mode = self.license_manager.local_unrestricted_access_mode()
        if access_mode == "owner":
            machine_name = self.license_manager.get_machine_name() or "este equipo"
            self.actualizar_estado_licencia()
            self.log(f"Equipo propietario detectado: {machine_name}.", "exito")
            messagebox.showinfo(
                "Equipo propietario",
                f"Este equipo ({machine_name}) es propietario de la app.\n\nLa app funciona aquí sin limitaciones de licencia.",
            )
            return
        if access_mode == "developer":
            self.actualizar_estado_licencia()
            self.log("Acceso de desarrollador habilitado en este equipo.", "exito")
            messagebox.showinfo(
                "Acceso de desarrollador",
                "Este equipo tiene acceso de desarrollador activo.\n\nLa app funciona sin limitaciones de licencia.",
            )
            return

        state = self.license_manager.state
        if not state.get("license_key") or not state.get("email"):
            pending_email = state.get("subscription_email", "").strip()
            pending_plan_code = state.get("subscription_plan_code", "").strip()
            if pending_email and pending_plan_code:
                self._verificar_suscripcion_pendiente(pending_email, pending_plan_code)
            else:
                email = self._pedir_correo_confirmado(
                    "Verificar compra",
                    "Escribe el mismo correo con el que hiciste la compra para buscar tu licencia.",
                )
                if not email:
                    return
                # Sin plan: se busca en el Sheet solo por correo. El plan real
                # ya viene guardado ahí (lo puso Make al confirmar el pago), así
                # que pedírselo al usuario aquí no cambiaba qué se encontraba —
                # solo agregaba un paso. Ver `LegacySheetsLicenseClient._find_row`
                # y `resolve_subscription`: con plan_code vacío, buscan por
                # correo únicamente.
                self._verificar_suscripcion_pendiente(email, "")
            return

        try:
            response = self.api_client.validate_license(
                state["email"], state["license_key"], self.license_manager.get_device_id()
            )
        except LicensingError as exc:
            messagebox.showerror("No fue posible validar", str(exc))
            return

        if response.get("status") == "active":
            self.license_manager.activate_local(
                state["email"],
                state["license_key"],
                subscription_id=response.get("subscription_id", state.get("subscription_id", "")),
                subscription_plan_code=response.get("plan_code", state.get("subscription_plan_code", "")),
                subscription_expires_at=response.get("next_payment_date", state.get("subscription_expires_at", "")),
            )
            self.actualizar_estado_licencia()
            self._mostrar_avisos_licencia()
            self.log("Licencia validada correctamente.", "exito")
            messagebox.showinfo("Licencia válida", "La licencia sigue activa en este equipo.")
        else:
            pending_email = state.get("subscription_email", "").strip()
            pending_plan_code = state.get("subscription_plan_code", "").strip()
            if pending_email and pending_plan_code:
                self._verificar_suscripcion_pendiente(pending_email, pending_plan_code)
                return
            self.license_manager.set_validation_status("inactive")
            self.actualizar_estado_licencia()
            self.log("La licencia no fue validada.", "advertencia")
            messagebox.showwarning("Licencia inválida", response.get("message", "La licencia ya no es válida."))

    def _verificar_suscripcion_pendiente(self, email, plan_code):
        """Intenta activar la licencia automáticamente contra el servicio de
        licencias (`resolve_subscription` — la misma consulta que ya hace
        `_sincronizar_licencia_silenciosa` en segundo plano) y, solo si el
        cliente configurado exige un código de activación explícito
        (`status == "activation_required"`, el caso del backend propio
        `LicenseApiClient`), lo pide con un diálogo y activa con él.

        Antes este método se saltaba `resolve_subscription` por completo y
        siempre pedía el código, sin importar qué cliente estuviera
        configurado. Con `LegacySheetsLicenseClient` (el que usa la app hoy,
        ver `ModernOrganizadorGUI.__init__`) nunca se emite ni se envía un
        código de activación — la licencia se resuelve sola por correo+plan
        contra el Google Sheet que sincroniza Make — así que el diálogo
        dejaba a cualquier comprador real pidiéndole un código que jamás
        iba a recibir. Este es el bug reportado: al comprador se le mostraba
        el diálogo de código en vez de activarse solo, como sí hace la
        sincronización silenciosa al abrir la app."""
        try:
            response = self.api_client.resolve_subscription(email, plan_code)
        except LicensingError as exc:
            messagebox.showerror("No fue posible verificar", str(exc))
            return

        status = response.get("status", "")
        license_key = response.get("license_key")

        if status in {"active", "authorized"} and license_key:
            try:
                activated, message = self._registrar_licencia_confirmada(
                    response.get("email", email),
                    license_key,
                    subscription_id=response.get("subscription_id", ""),
                    subscription_plan_code=response.get("plan_code", plan_code),
                    subscription_expires_at=response.get("next_payment_date", ""),
                )
            except LicensingError as exc:
                messagebox.showerror("No fue posible activar", str(exc))
                return
            if not activated:
                messagebox.showwarning("Licencia no activada", message)
                return
            self.actualizar_estado_licencia()
            self._mostrar_avisos_licencia()
            messagebox.showinfo("Licencia activada", "Tu compra fue confirmada y la app ya quedó desbloqueada.")
            return

        if status != "activation_required":
            messagebox.showinfo(
                "Compra no encontrada todavía",
                "Todavía no encontramos tu compra confirmada.\n\n"
                "Si acabas de pagar, espera unos minutos (la sincronización puede tardar un poco) "
                "e inténtalo de nuevo con \"Verificar licencia\".",
            )
            return

        activation_code = simpledialog.askstring(
            "Activar licencia",
            "Pega el código de activación enviado tras confirmar tu pago.",
            parent=self,
        )
        if not activation_code:
            return
        try:
            activated, message = self._registrar_licencia_confirmada(email, activation_code)
        except LicensingError as exc:
            messagebox.showerror("No fue posible activar", str(exc))
            return
        if not activated:
            messagebox.showwarning("Licencia no activada", message)
            return
        self.actualizar_estado_licencia()
        self._mostrar_avisos_licencia()
        messagebox.showinfo("Licencia activada", "La licencia fue activada correctamente en este equipo.")

    def _asegurar_acceso(self):
        """Comprobación de guardia usada al inicio de las acciones que
        requieren licencia (organizar, extraer facturas...): si no hay
        acceso desbloqueado, avisa y devuelve `False` para que la acción
        se cancele."""
        if self.license_manager.is_feature_unlocked():
            return True
        messagebox.showwarning(
            "Licencia requerida",
            "Esta función requiere una licencia activa.\n\nCompra o activa una licencia para continuar.",
        )
        return False

    def analizar_directorio(self):
        """Botón "Analizar directorio": recorre el origen SIN mover ni
        copiar nada, solo cuenta cuántos archivos hay por categoría, y
        vuelca el resumen en el registro. Sirve para revisar antes de
        organizar de verdad."""
        origen = self.directorio_origen.get()
        if not origen or not os.path.exists(origen):
            messagebox.showwarning("Directorio inválido", "Selecciona un directorio de origen válido.")
            return

        self.log("Analizando directorio...", "info")
        categorias_count = {}
        total_archivos = 0
        archivos_vacios = 0
        for root, _, files in walk_files(origen):
            for nombre_archivo in sorted(files, key=self.organizador.clave_orden_personalizada):
                total_archivos += 1
                try:
                    if os.path.getsize(os.path.join(root, nombre_archivo)) == 0:
                        archivos_vacios += 1
                except OSError:
                    pass
                categoria = self.organizador.obtener_categoria(Path(nombre_archivo).suffix, nombre_archivo)
                categorias_count[categoria] = categorias_count.get(categoria, 0) + 1

        self.estadisticas["total"].set(str(total_archivos))
        self.estadisticas["categorias"].set(str(len(categorias_count)))
        self.log("=" * 50, "subtitulo")
        self.log("Análisis completado", "subtitulo")
        self.log(f"Archivos encontrados: {total_archivos}", "info")
        self.log(f"Categorías detectadas: {len(categorias_count)}", "info")
        if archivos_vacios:
            self.log(
                f"Archivos vacíos (0 bytes, sin contenido): {archivos_vacios}. "
                "No se comparan como duplicados.",
                "advertencia",
            )
        for categoria, cantidad in sorted(categorias_count.items()):
            porcentaje = (cantidad / total_archivos * 100) if total_archivos else 0
            categoria_ui = self.organizador.obtener_nombre_categoria_ui(categoria)
            self.log(f"{categoria_ui}: {cantidad} archivo(s) ({porcentaje:.1f}%)")
        self.log("=" * 50, "subtitulo")

    def iniciar_organizacion(self):
        """Botón "Iniciar organización": valida todo lo necesario (acceso,
        rutas, filtro de tipo, uso mensual disponible), arma la lista
        completa de archivos a procesar, muestra un resumen para que el
        usuario confirme, registra la corrida en `ProcessRecoveryManager`
        (para poder reanudarla si algo falla) y arranca el hilo que
        realmente mueve/copia los archivos.

        Importante: la lista de archivos se calcula UNA VEZ al confirmar
        (`_construir_lista_archivos_elegibles`) y no se vuelve a escanear
        la carpeta después; así, si se crean archivos nuevos en el origen
        mientras se está organizando, no se procesan en esa misma corrida.
        """
        if not self._asegurar_acceso():
            return

        origen = self.directorio_origen.get()
        destino = self.directorio_destino.get()
        extension_filtrada = self.tipo_archivo_seleccionado.get().lower()
        if not origen or not os.path.exists(origen):
            messagebox.showerror("Error", "Selecciona un directorio de origen válido.")
            return
        if not destino:
            messagebox.showerror("Error", "Selecciona un directorio de destino.")
            return
        try:
            origen_path, destino_path = validate_source_destination(origen, destino)
        except PathSafetyError as exc:
            messagebox.showerror("Rutas no seguras", str(exc))
            return
        origen, destino = str(origen_path), str(destino_path)
        if not self.organizar_todos.get() and not self.tipo_archivo_seleccionado.get():
            messagebox.showerror("Error", "Selecciona una extensión para filtrar.")
            return

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        archivos_elegibles = self._contar_archivos_elegibles(origen, extension_filtrada)
        if not self._asegurar_uso_mensual_disponible():
            return
        if archivos_elegibles <= 0:
            messagebox.showinfo("Sin archivos", "No se encontraron archivos compatibles con la seleccion actual.")
            return
        self._current_run_limit = None

        resumen = [
            f"Origen: {origen}",
            f"Destino: {destino}",
            f"Archivos a organizar: {archivos_elegibles}",
            f"Tipo: {'Todos' if self.organizar_todos.get() else self.tipo_archivo_seleccionado.get()}",
            f"Estructura final: Fecha / Tipo de archivo / orden numerico y alfabetico",
            f"Carpetas por fecha: {'Sí' if self.organizar_por_fecha.get() else 'No'}",
            f"Detección de recibos: {'Sí' if self.detectar_recibos.get() else 'No'}",
            f"Eliminar duplicados: {'Sí' if self.eliminar_duplicados.get() else 'No'}",
            f"Mover archivos: {'Sí' if self.mover_en_vez_de_copiar.get() else 'No'}",
            (
                "Eliminar carpetas vacías del origen: "
                + (
                    "Sí"
                    if self.eliminar_carpetas_vacias.get() and self.mover_en_vez_de_copiar.get()
                    else "No"
                )
            ),
        ]
        if usage_info["limit"] is None:
            resumen.append("Usos mensuales: Ilimitados")
        else:
            resumen.append(f"Usos mensuales restantes: {usage_info['remaining']} de {usage_info['limit']}")
            resumen.append("Esta organizacion consumira 1 uso.")
        if not messagebox.askyesno("Confirmar organización", "\n".join(resumen)):
            self._current_run_limit = None
            return

        self.organizador.reset()
        archivos_planificados = self._construir_lista_archivos_elegibles(origen, extension_filtrada)
        self.organizador.estadisticas["total_archivos"] = len(archivos_planificados)
        self._preparar_estado_visual_ejecucion(len(archivos_planificados))
        self.log(
            f"Se prepararon {len(archivos_planificados)} archivo(s) para organizar. La app procesará el listado sin volver a escanear la carpeta.",
            "info",
        )
        run_state = self.process_recovery.create_run(
            {
                "origen": origen,
                "destino": destino,
                "organizar_por_categoria": self.organizar_por_categoria.get(),
                "organizar_por_fecha": self.organizar_por_fecha.get(),
                "detectar_recibos": self.detectar_recibos.get(),
                "eliminar_duplicados": self.eliminar_duplicados.get(),
                "mover_en_vez_de_copiar": self.mover_en_vez_de_copiar.get(),
                "eliminar_carpetas_vacias": self.eliminar_carpetas_vacias.get(),
                "organizar_todos": self.organizar_todos.get(),
                "tipo_archivo": self.tipo_archivo_seleccionado.get(),
                "limite_ejecucion": self._current_run_limit,
            },
            archivos_planificados,
        )
        self._iniciar_hilo_organizacion(archivos_planificados, run_state, start_index=0)

    def _iniciar_hilo_organizacion(self, archivos_planificados, run_state, start_index=0):
        """Prepara el estado de "corrida en curso" (activa el botón
        Cancelar, deshabilita el resto de botones) y lanza
        `_ejecutar_organizacion` en un hilo aparte, para que la interfaz
        siga respondiendo (se pueda cancelar, se vea el progreso) mientras
        se procesan los archivos."""
        self._run_in_progress = True
        self._cancel_requested = False
        self._cancel_button.state(["!disabled"])
        self._last_recovery_update = 0.0
        self.process_recovery.update_state(status="running", next_index=start_index)
        self._cambiar_estado_botones("disabled")
        hilo = threading.Thread(
            target=self._ejecutar_organizacion,
            args=(archivos_planificados, run_state, start_index),
            daemon=True,
        )
        hilo.start()

    def _ejecutar_organizacion(self, archivos_planificados, run_state, start_index=0):
        """El corazón de la app: procesa uno por uno los archivos de
        `archivos_planificados`, empezando en `start_index` (0 si es una
        corrida nueva, o más adelante si se está reanudando una
        interrumpida). Corre en un hilo aparte (ver
        `_iniciar_hilo_organizacion`), así que cualquier actualización de
        la interfaz se hace con `self.after(0, ...)` para que se ejecute
        de forma segura en el hilo principal.

        Por cada archivo, en resumen:
        1. Si se pidió cancelar, se guarda el progreso como "interrumpido"
           y se corta ahí (se puede reanudar después).
        2. Si el archivo ya no existe, se cuenta como error y se salta.
        3. Si está activa la eliminación de duplicados, se calcula su
           hash; si ya se vio ese mismo hash antes, va a cuarentena en vez
           de al destino.
        4. Si no es duplicado, se calcula su categoría/fecha de destino,
           se genera un nombre único si hace falta, se copia o mueve, y se
           anota el resultado en el diario de recuperación.

        Cada evento (copiado, movido, duplicado, error) se anota en el
        diario para que, si el proceso se corta, se sepa exactamente por
        dónde iba y qué hashes ya se habían visto.
        """
        destino_base = Path(run_state["destino"])
        organizar_por_categoria = bool(run_state.get("organizar_por_categoria", True))
        organizar_por_fecha = bool(run_state.get("organizar_por_fecha", True))
        detectar_recibos = bool(run_state.get("detectar_recibos", True))
        eliminar_duplicados = bool(run_state.get("eliminar_duplicados", True))
        mover_archivos = bool(run_state.get("mover_en_vez_de_copiar", True))
        # Solo se borran carpetas vacías si el usuario lo pidió Y se está moviendo
        # (al copiar, el origen no se toca, así que no tiene sentido limpiarlo).
        eliminar_carpetas_vacias = bool(run_state.get("eliminar_carpetas_vacias", False)) and mover_archivos
        total_archivos = len(archivos_planificados)
        directorios_creados = set()

        journal_path = Path(run_state["journal_path"])
        try:
            with journal_path.open("a", encoding="utf-8") as journal_handle:
                journal_buffer = []
                for index in range(start_index, total_archivos):
                    if self._cancel_requested:
                        self._vaciar_buffer_recuperacion(journal_handle, journal_buffer)
                        self.process_recovery.mark_interrupted()
                        self.after(0, self.log, "Proceso cancelado por el usuario. Puedes reanudarlo más tarde.", "advertencia")
                        self.after(0, self._cambiar_estado_botones, "normal")
                        self.after(0, lambda: self._cancel_button.state(["disabled"]))
                        self.after(0, lambda: setattr(self, "_run_in_progress", False))
                        return
                    ruta_completa = Path(archivos_planificados[index])
                    nombre_archivo = ruta_completa.name
                    categoria = ""
                    ruta_final = None
                    hash_archivo = ""
                    estado = "error"

                    if not ruta_completa.exists():
                        self.organizador.estadisticas["errores"] += 1
                        estado = "missing"
                        self._registrar_evento_recuperacion(journal_buffer, index, estado, ruta_completa)
                        self.after(0, self.log, f"El archivo ya no existe y se omitió: {ruta_completa}", "advertencia")
                    else:
                        extension = ruta_completa.suffix.lower()
                        # "ok" = el archivo se puede organizar. (Antes esta variable seguía en
                        # "error" cuando la opción de duplicados estaba apagada, y en ese caso
                        # ningún archivo se organizaba.) Solo pasa a "error" si falla la lectura.
                        estado = "ok"

                        # Un archivo vacío (0 bytes) se detecta SIEMPRE, para avisarle al usuario,
                        # pero NO se compara por hash: todos los archivos vacíos dan el mismo hash
                        # y se marcarían como "duplicados" entre sí, lo cual no es útil. Se organiza
                        # normalmente como cualquier otro archivo.
                        try:
                            es_vacio = ruta_completa.stat().st_size == 0
                        except OSError:
                            es_vacio = False
                        if es_vacio:
                            self.organizador.estadisticas["archivos_vacios"].append(str(ruta_completa))
                            if len(self.organizador.estadisticas["archivos_vacios"]) <= 20:
                                self.after(
                                    0,
                                    self.log,
                                    f"Archivo vacío (0 bytes, no tiene contenido): {ruta_completa}",
                                    "advertencia",
                                )

                        if eliminar_duplicados and not es_vacio:
                            hash_resultado = self.organizador.calcular_hash_archivo(ruta_completa)
                            if hash_resultado is None:
                                self.organizador.estadisticas["errores"] += 1
                                estado = "error"
                                self._registrar_evento_recuperacion(journal_buffer, index, estado, ruta_completa)
                                self.after(0, self.log, f"No se pudo leer {nombre_archivo}.", "error")
                            else:
                                hash_archivo = hash_resultado

                        if estado != "error":
                            if eliminar_duplicados and hash_archivo and hash_archivo in self.organizador.hashes_archivos:
                                # Ya se vio antes un archivo con este mismo contenido (mismo
                                # hash): en vez de organizarlo normalmente, se cuenta como
                                # duplicado. Si se está "moviendo" (no copiando), el duplicado
                                # se manda a una carpeta de cuarentena del día (recuperable),
                                # en vez de borrarlo directamente. Si se está copiando, el
                                # original se deja donde estaba y simplemente no se copia.
                                self.organizador.estadisticas["duplicados"] += 1
                                estado = "duplicate"
                                if mover_archivos:
                                    try:
                                        duplicate_root = get_app_storage_dir() / "duplicate_quarantine" / dt.datetime.now().strftime("%Y-%m-%d")
                                        ruta_final = quarantine_file(ruta_completa, duplicate_root)
                                    except OSError as exc:
                                        self.organizador.estadisticas["errores"] += 1
                                        estado = "error"
                                        self.after(0, self.log, f"No se pudo eliminar duplicado: {exc}", "error")
                                self._registrar_evento_recuperacion(
                                    journal_buffer, index, estado, ruta_completa, destino=ruta_final or "", file_hash=hash_archivo
                                )
                            elif estado != "error":
                                categoria, ruta_destino = self.organizador.construir_ruta_destino(
                                    destino_base,
                                    ruta_completa,
                                    organizar_por_categoria=organizar_por_categoria,
                                    organizar_por_fecha=organizar_por_fecha,
                                    detectar_recibos=detectar_recibos,
                                )
                                destino_cache = str(ruta_destino)
                                if destino_cache not in directorios_creados:
                                    ruta_destino.mkdir(parents=True, exist_ok=True)
                                    directorios_creados.add(destino_cache)
                                ruta_final = self.organizador.generar_nombre_unico(ruta_destino, nombre_archivo)

                                try:
                                    if mover_archivos:
                                        shutil.move(str(ruta_completa), str(ruta_final))
                                        estado = "moved"
                                    else:
                                        shutil.copy2(str(ruta_completa), str(ruta_final))
                                        estado = "copied"
                                    if hash_archivo:
                                        self.organizador.hashes_archivos[hash_archivo] = str(ruta_final)
                                    self.organizador.estadisticas["procesados"] += 1
                                    self.organizador.estadisticas["categorias_usadas"].add(categoria)
                                    self._registrar_evento_recuperacion(
                                        journal_buffer,
                                        index,
                                        estado,
                                        ruta_completa,
                                        destino=ruta_final,
                                        categoria=categoria,
                                        file_hash=hash_archivo,
                                    )
                                except OSError as exc:
                                    self.organizador.estadisticas["errores"] += 1
                                    estado = "error"
                                    self._registrar_evento_recuperacion(journal_buffer, index, estado, ruta_completa)
                                    self.after(0, self.log, f"Error con {nombre_archivo}: {exc}", "error")

                    # Cada archivo actualiza el progreso, pero escribir a disco o tocar la
                    # interfaz por CADA archivo sería lento con miles de ellos. Por eso el
                    # diario, el resumen de recuperación, la barra de progreso y el log se
                    # actualizan solo cada N archivos (_recovery_batch_size,
                    # _progress_batch_size, _log_batch_size) o al llegar al último.
                    revisados = index + 1
                    if revisados % self._recovery_batch_size == 0 or revisados == total_archivos:
                        self._vaciar_buffer_recuperacion(journal_handle, journal_buffer)
                    self._persistir_resumen_parcial(revisados)
                    if revisados % self._progress_batch_size == 0 or revisados == total_archivos:
                        self.after(0, self._actualizar_progreso, revisados, self.organizador.estadisticas["procesados"])
                    if revisados % self._log_batch_size == 0 or revisados == total_archivos:
                        self.after(
                            0,
                            self.log,
                            (
                                f"Progreso: {revisados}/{total_archivos} revisados | "
                                f"{self.organizador.estadisticas['procesados']} organizados | "
                                f"{self.organizador.estadisticas['duplicados']} duplicados | "
                                f"{self.organizador.estadisticas['errores']} errores."
                            ),
                            "info",
                        )
                self._vaciar_buffer_recuperacion(journal_handle, journal_buffer)

            # Solo se llega aquí si la corrida terminó completa (si se cancela, arriba se
            # hace `return` antes), así que nunca se limpian carpetas a medias.
            if eliminar_carpetas_vacias:
                eliminadas = self._eliminar_carpetas_vacias(run_state["origen"])
                self.organizador.estadisticas["carpetas_vacias_eliminadas"] = eliminadas

            self.process_recovery.update_state(
                next_index=total_archivos,
                procesados=self.organizador.estadisticas["procesados"],
                duplicados=self.organizador.estadisticas["duplicados"],
                errores=self.organizador.estadisticas["errores"],
                categorias_usadas=sorted(self.organizador.estadisticas["categorias_usadas"]),
            )
            self.after(0, self._finalizar_proceso)
        except Exception as exc:
            self.process_recovery.mark_interrupted()
            self.after(0, self.log, f"Error crítico en el proceso: {exc}", "error")
            self.after(0, self._cambiar_estado_botones, "normal")
            self.after(0, lambda: setattr(self, "_run_in_progress", False))

    def _eliminar_carpetas_vacias(self, origen):
        """Borra las carpetas que quedaron completamente vacías dentro de
        `origen` (por ejemplo, después de mover todos sus archivos) y
        devuelve cuántas eliminó.

        Reglas de seguridad:
        - Recorre de abajo hacia arriba (`topdown=False`), así una carpeta
          que solo contenía subcarpetas vacías también queda vacía y se borra.
        - Usa `os.rmdir`, que SOLO borra carpetas vacías: si por cualquier
          motivo hay un archivo dentro (incluso oculto, como `desktop.ini`),
          falla y la carpeta se deja intacta. Nunca borra archivos, con una
          única excepción en macOS: si lo ÚNICO que hay dentro es `.DS_Store`
          (metadatos que Finder crea solo en casi cualquier carpeta que se
          abre), se quita ese archivo para que la carpeta cuente como vacía.
        - Jamás borra la carpeta de origen en sí, ni enlaces simbólicos /
          uniones (junctions), ni nada dentro del almacenamiento de la app.
          En macOS tampoco entra a los paquetes (`.app`, `.photoslibrary`...).
        """
        raiz = Path(origen)
        if not raiz.is_dir():
            return 0
        almacenamiento_app = get_app_storage_dir()
        es_junction = getattr(os.path, "isjunction", lambda _ruta: False)
        eliminadas = 0
        for carpeta_actual, _subcarpetas, _archivos in os.walk(raiz, topdown=False):
            ruta = Path(carpeta_actual)
            if ruta == raiz:
                continue
            if os.path.islink(ruta) or es_junction(ruta):
                continue
            if is_within(ruta, almacenamiento_app):
                continue
            if IS_MAC:
                if is_inside_macos_package(ruta, raiz):
                    continue
                if contains_only_ds_store(ruta):
                    try:
                        (ruta / ".DS_Store").unlink()
                    except OSError:
                        continue
            try:
                os.rmdir(ruta)  # Falla (OSError) si la carpeta no está vacía.
                eliminadas += 1
            except OSError:
                continue
        return eliminadas

    # ------------------------------------------------------------------
    # Cuarentena de duplicados: vaciado manual y borrado automático
    # ------------------------------------------------------------------
    @staticmethod
    def _carpeta_cuarentena():
        """Carpeta donde se guardan los duplicados apartados al mover
        archivos (subcarpetas por fecha, ver `_ejecutar_organizacion`)."""
        return get_app_storage_dir() / "duplicate_quarantine"

    @staticmethod
    def _formatear_bytes(cantidad):
        """Convierte bytes a un texto legible (KB, MB, GB)."""
        valor = float(cantidad)
        for unidad in ("B", "KB", "MB", "GB"):
            if valor < 1024 or unidad == "GB":
                return f"{int(valor)} {unidad}" if unidad == "B" else f"{valor:.1f} {unidad}"
            valor /= 1024
        return f"{valor:.1f} GB"

    def _guardar_preferencia_purga_cuarentena(self):
        """Guarda en disco si el usuario quiere el borrado automático."""
        self._preferencias["purga_automatica_cuarentena"] = bool(self.purga_automatica_cuarentena.get())
        guardar_preferencias(self._preferencias)

    def vaciar_cuarentena(self):
        """Botón "Vaciar cuarentena ahora": muestra cuántos archivos y
        cuánto espacio hay, pide confirmación y, si el usuario acepta, los
        borra DEFINITIVAMENTE (no van a la papelera de Windows). El borrado
        corre en un hilo aparte para no congelar la ventana si hay muchos."""
        if self._run_in_progress:
            messagebox.showinfo("Proceso en curso", "Espera a que termine la organización antes de vaciar la cuarentena.")
            return
        raiz = self._carpeta_cuarentena()
        cantidad, tamano = quarantine_summary(raiz)
        if cantidad == 0:
            messagebox.showinfo("Cuarentena vacía", "No hay duplicados en la cuarentena.")
            return
        confirmar = messagebox.askyesno(
            "Vaciar cuarentena",
            f"La cuarentena tiene {cantidad} archivo(s) ({self._formatear_bytes(tamano)}).\n\n"
            "Se van a borrar DEFINITIVAMENTE: no van a la papelera y después no se podrán "
            "restaurar con \"Deshacer última organización\".\n\n"
            "¿Quieres continuar?",
            icon="warning",
        )
        if not confirmar:
            return
        self._cambiar_estado_botones("disabled")
        self.log("Vaciando la cuarentena de duplicados...", "info")
        threading.Thread(target=self._trabajo_vaciar_cuarentena, args=(raiz,), daemon=True).start()

    def _trabajo_vaciar_cuarentena(self, raiz):
        """Hilo del vaciado manual: borra todo y avisa el resultado a la
        interfaz con `after` (los widgets solo se tocan desde el hilo
        principal)."""
        try:
            borrados, liberados = purge_quarantine(raiz)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo se le muestra al usuario
            LOGGER.exception("No se pudo vaciar la cuarentena")
            self.after(0, self._finalizar_vaciado_cuarentena, 0, 0, str(exc))
            return
        self.after(0, self._finalizar_vaciado_cuarentena, borrados, liberados, "")

    def _finalizar_vaciado_cuarentena(self, borrados, liberados, error):
        """Vuelve a habilitar los botones y muestra el resultado del vaciado."""
        self._cambiar_estado_botones("normal")
        self.actualizar_estado_licencia()
        if error:
            self.log(f"No se pudo vaciar la cuarentena: {error}", "error")
            messagebox.showerror("Vaciar cuarentena", f"No se pudo vaciar la cuarentena:\n{error}")
            return
        self.log(
            f"Cuarentena vaciada: {borrados} archivo(s) borrados, {self._formatear_bytes(liberados)} liberados.",
            "exito",
        )
        messagebox.showinfo(
            "Cuarentena vaciada",
            f"Se borraron {borrados} archivo(s) y se liberaron {self._formatear_bytes(liberados)}.",
        )

    def _purgar_cuarentena_automatica(self):
        """Al abrir la app, si la casilla está activa, borra (en segundo
        plano) los duplicados de la cuarentena que llevan más de
        `CUARENTENA_DIAS_MAX` días. Si no hay nada que borrar, no muestra
        nada."""
        if not self.purga_automatica_cuarentena.get():
            return
        threading.Thread(target=self._trabajo_purga_automatica, daemon=True).start()

    def _trabajo_purga_automatica(self):
        """Hilo del borrado automático (ver `_purgar_cuarentena_automatica`).
        Un fallo aquí nunca debe molestar al usuario: solo queda en el log."""
        try:
            borrados, liberados = purge_quarantine(self._carpeta_cuarentena(), older_than_days=CUARENTENA_DIAS_MAX)
        except Exception:  # noqa: BLE001
            LOGGER.exception("No se pudo limpiar la cuarentena automáticamente")
            return
        if borrados:
            self.after(
                0,
                self.log,
                (
                    f"Cuarentena limpiada automáticamente: {borrados} duplicado(s) con más de "
                    f"{CUARENTENA_DIAS_MAX} días, {self._formatear_bytes(liberados)} liberados."
                ),
                "info",
            )

    def _actualizar_progreso(self, valor, procesados=None):
        """Mueve la barra de progreso y actualiza el contador visible de
        "procesados" (llamado desde el hilo de organización vía `after`)."""
        self.progreso["value"] = valor
        self.estadisticas["procesados"].set(str(valor if procesados is None else procesados))

    def _finalizar_proceso(self):
        """Se ejecuta cuando `_ejecutar_organizacion` termina con éxito
        (llamado vía `after` desde el hilo en segundo plano): actualiza
        las estadísticas finales en pantalla, registra el uso mensual, y
        escribe el resumen completo en el registro de actividad."""
        stats = self.organizador.estadisticas
        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        if stats["total_archivos"] > 0:
            self._registrar_uso_mensual("organizacion de archivos")
        self.estadisticas["procesados"].set(str(stats["procesados"]))
        self.estadisticas["duplicados"].set(str(stats["duplicados"]))
        self.estadisticas["categorias"].set(str(len(stats["categorias_usadas"])))

        self.log("=" * 50, "subtitulo")
        self.log("Organización completada", "subtitulo")
        self.log(f"Archivos encontrados: {stats['total_archivos']}")
        self.log(f"Archivos procesados: {stats['procesados']}")
        self.log(f"Duplicados detectados: {stats['duplicados']}")
        self.log(f"Errores: {stats['errores']}")
        archivos_vacios = stats.get("archivos_vacios") or []
        if archivos_vacios:
            self.log(
                f"Archivos vacíos (0 bytes, sin contenido): {len(archivos_vacios)}. "
                "No se compararon como duplicados.",
                "advertencia",
            )
        if stats.get("carpetas_vacias_eliminadas"):
            self.log(f"Carpetas vacías eliminadas: {stats['carpetas_vacias_eliminadas']}")
        self.log(f"Categorías usadas: {len(stats['categorias_usadas'])}")
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None:
            self.log("Usos mensuales: ilimitados", "info")
        else:
            self.log(
                f"Usos mensuales restantes del plan {self._nombre_plan(plan_code)}: {usage_info['remaining']} de {usage_info['limit']}.",
                "info",
            )
        self.process_recovery.mark_completed(
            {
                "total_archivos": stats["total_archivos"],
                "procesados": stats["procesados"],
                "duplicados": stats["duplicados"],
                "errores": stats["errores"],
                "categorias_usadas": len(stats["categorias_usadas"]),
            }
        )
        self.progreso["value"] = 0
        self._cambiar_estado_botones("normal")
        self._current_run_limit = None
        self._run_in_progress = False

        # Aviso final (después de dejar la app lista de nuevo): hay archivos sin ningún contenido.
        if archivos_vacios:
            ejemplos = "\n".join(f"- {ruta}" for ruta in archivos_vacios[:5])
            resto = len(archivos_vacios) - 5
            if resto > 0:
                ejemplos += f"\n... y {resto} más (revisa el registro de actividad)."
            messagebox.showwarning(
                "Archivos vacíos encontrados",
                f"Se encontraron {len(archivos_vacios)} archivo(s) vacíos: miden 0 bytes y no tienen "
                "ningún contenido.\n\n"
                "No se compararon como duplicados y se organizaron normalmente. Si no los necesitas, "
                "puedes revisarlos y borrarlos manualmente.\n\n"
                f"Ejemplos:\n{ejemplos}",
            )

    def deshacer_ultima_organizacion(self):
        """Revierte los archivos movidos/copiados en la última organización completada.
        Disponible solo para planes Pro y Premium. No consume un uso mensual.

        Recorre el diario guardado de la última corrida y, según cómo
        quedó cada archivo: los que se MOVIERON se regresan a su carpeta
        de origen; los que se COPIARON simplemente se borran del destino
        (el original nunca se tocó); los que fueron a cuarentena por
        duplicados se regresan a su origen. Antes de tocar cualquier
        archivo se valida que su ruta esté dentro del destino de esa
        corrida (o de la cuarentena), para no borrar/mover nada fuera de
        lo que la propia app organizó.
        """
        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        if not features.get("undo_organization", False):
            messagebox.showinfo(
                "Función no disponible",
                "Deshacer una organización solo está disponible en los planes Pro y Premium.",
            )
            return

        resumen = self.process_recovery.get_last_run_summary()
        if not resumen:
            messagebox.showinfo("Nada que deshacer", "No hay ninguna organización reciente para deshacer.")
            return
        if resumen.get("deshecho"):
            messagebox.showinfo("Ya deshecho", "La última organización ya fue deshecha anteriormente.")
            return

        entries = self.process_recovery.get_last_run_journal_entries()
        if not entries:
            messagebox.showinfo("Nada que deshacer", "No se encontró información de la última organización.")
            return

        confirmar = messagebox.askyesno(
            "Deshacer última organización",
            "Esto va a devolver los archivos movidos a su carpeta de origen, restaurar los "
            "duplicados que están en la cuarentena de la app y eliminar las copias creadas por "
            "la última organización.\n\n"
            "Los duplicados solo se pueden restaurar si siguen en la cuarentena; si ya los "
            "borraste de allí manualmente, no se podrán recuperar.\n\n"
            "Las carpetas vacías que se hayan eliminado no se vuelven a crear (solo se recrean "
            "las necesarias para devolver archivos).\n\n"
            "¿Quieres continuar?",
        )
        if not confirmar:
            return

        restaurados = 0
        eliminados_copia = 0
        omitidos_duplicados = 0
        errores = 0
        destino_raiz_texto = str(resumen.get("destino", "")).strip()
        destino_raiz = Path(destino_raiz_texto) if destino_raiz_texto else None

        for entry in entries:
            status = entry.get("status", "")
            origen = entry.get("origen", "")
            destino = entry.get("destino", "")

            try:
                duplicate_root = get_app_storage_dir() / "duplicate_quarantine"
                allowed_destination = (destino_raiz is not None and is_within(destino, destino_raiz)) or (
                    status == "duplicate" and is_within(destino, duplicate_root)
                )
                if not destino or not allowed_destination:
                    errores += 1
                    self.log(f"Se rechazó una ruta de recuperación no segura: {destino}", "error")
                    continue
                if status == "moved" and destino and origen:
                    destino_path = Path(destino)
                    origen_path = Path(origen)
                    if not destino_path.exists():
                        errores += 1
                        self.log(f"No se pudo deshacer, ya no existe: {destino_path}", "advertencia")
                        continue
                    origen_path.parent.mkdir(parents=True, exist_ok=True)
                    destino_final = self.organizador.generar_nombre_unico(origen_path.parent, origen_path.name)
                    shutil.move(str(destino_path), str(destino_final))
                    restaurados += 1

                elif status == "copied" and destino:
                    destino_path = Path(destino)
                    if destino_path.exists():
                        os.remove(destino_path)
                        eliminados_copia += 1

                elif status == "duplicate":
                    duplicate_path = Path(destino)
                    origen_path = Path(origen)
                    if duplicate_path.exists() and origen:
                        origen_path.parent.mkdir(parents=True, exist_ok=True)
                        destino_final = self.organizador.generar_nombre_unico(origen_path.parent, origen_path.name)
                        shutil.move(str(duplicate_path), str(destino_final))
                        restaurados += 1
                    else:
                        omitidos_duplicados += 1

            except OSError as exc:
                errores += 1
                self.log(f"Error al deshacer {origen or destino}: {exc}", "error")

        self.process_recovery.mark_last_run_undone()

        self.log("=" * 50, "subtitulo")
        self.log("Deshacer última organización completado", "subtitulo")
        self.log(f"Archivos restaurados a su origen: {restaurados}")
        self.log(f"Copias eliminadas: {eliminados_copia}")
        if omitidos_duplicados:
            self.log(f"Duplicados que no se pudieron recuperar: {omitidos_duplicados}", "advertencia")
        if errores:
            self.log(f"Errores durante el proceso: {errores}", "advertencia")

        mensaje = (
            f"Archivos restaurados: {restaurados}\n"
            f"Copias eliminadas: {eliminados_copia}\n"
        )
        if omitidos_duplicados:
            mensaje += f"\nNo se pudieron recuperar {omitidos_duplicados} duplicado(s) porque ya no estaban en la cuarentena."
        if errores:
            mensaje += f"\n{errores} archivo(s) tuvieron un error al deshacer."

        messagebox.showinfo("Deshacer completado", mensaje)
        self.actualizar_estado_licencia()

    def _cambiar_estado_botones(self, estado):
        """Habilita/deshabilita todos los botones principales a la vez
        (mientras hay una organización en curso, por ejemplo)."""
        for boton in self._buttons:
            boton.state(["disabled"] if estado == "disabled" else ["!disabled"])

    def cancelar_organizacion(self):
        """Botón "Cancelar proceso": no detiene el hilo de golpe (eso
        podría dejar un archivo a medio copiar); en cambio, levanta una
        bandera que `_ejecutar_organizacion` revisa entre archivo y
        archivo, así que la cancelación real ocurre "al terminar el
        archivo actual", como dice el mensaje."""
        if not self._run_in_progress:
            return
        self._cancel_requested = True
        self._cancel_button.state(["disabled"])
        self.log("Cancelación solicitada; se detendrá al terminar el archivo actual.", "advertencia")

    def salir(self):
        """Maneja tanto el botón "Salir" como el botón de cerrar la
        ventana (X). Si hay una organización en curso, avisa que se puede
        continuar la próxima vez antes de cerrar (el progreso ya está
        guardado en disco por `ProcessRecoveryManager`)."""
        if self._run_in_progress:
            if messagebox.askyesno(
                "Salir",
                "Hay una organización en curso.\n\nSi cierras ahora, la app guardará el avance y te preguntará al volver a abrir si deseas continuar.\n\n¿Deseas salir?",
            ):
                self.process_recovery.mark_interrupted()
                self.destroy()
                return
            return
        if messagebox.askyesno("Salir", "¿Deseas cerrar la aplicación?"):
            self.destroy()


def main(argv=None):
    """Punto de entrada de la aplicación: crea la ventana principal y
    arranca el bucle de eventos de Tkinter (`mainloop`), que mantiene la
    app corriendo y respondiendo a clics, teclado, etc. hasta que se
    cierra la ventana. Con `--ocr-selftest [archivo]` solo comprueba el
    OCR y termina (ver `ocr_selftest`)."""
    argumentos = sys.argv[1:] if argv is None else list(argv)
    if argumentos and argumentos[0] == "--ocr-selftest":
        sys.exit(ocr_selftest(argumentos[1] if len(argumentos) > 1 else None))
    app = ModernOrganizadorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
