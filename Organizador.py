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
import time
import uuid
import webbrowser
import base64
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog
import tkinter as tk
from tkinter import ttk
from urllib import error, request

try:
    from PIL import ExifTags, Image, UnidentifiedImageError
except ImportError:
    ExifTags = None
    Image = None
    UnidentifiedImageError = OSError

try:
    import pytesseract
except ImportError:
    pytesseract = None


DESKTOP_RUNTIME_CONFIG_NAME = "desktop_runtime_config.json"
DESKTOP_RUNTIME_CONFIG_OVERRIDE_NAME = "desktop_runtime_config.local.json"
ICON_ASSET_DIR = "assets"
ICON_PNG_NAME = "icono.png"
ICON_PNG_VARIANTS = ("icono_16.png", "icono_32.png", "icono_48.png", ICON_PNG_NAME)
ICON_ICO_NAME = "icono.ico"


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


def load_runtime_config_file(path: Path, override_existing: bool = False):
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
        elif value is not None:
            serialized = str(value).strip()
            if override_existing:
                os.environ[normalized_key] = serialized
            else:
                os.environ.setdefault(normalized_key, serialized)


def load_runtime_config():
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


load_runtime_config()


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


def resolve_app_resource(*relative_parts: str):
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


APP_NAME = "FileSync Pro"
APP_VERSION = "3.1.0"
APP_ID = "miempresa.filesyncpro.desktop"
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
DEFAULT_API_BASE_URL = os.getenv("FILESYNC_PRO_API_URL", "").strip()
DEFAULT_PLAN_CODE = "basica"
DEFAULT_SUBSCRIPTION_PLAN_CODE = os.getenv("FILESYNC_PRO_DEFAULT_PLAN_CODE", DEFAULT_PLAN_CODE).strip().lower() or DEFAULT_PLAN_CODE
DEFAULT_SUBSCRIPTION_LINK_MAP = {
    "basica": "https://www.mercadopago.com.co/subscriptions/checkout?preapproval_plan_id=1e1c97ccb30e4f60a44908870323dcef",
    "pro": "https://www.mercadopago.com.co/subscriptions/checkout?preapproval_plan_id=7dba4d5ff8f44a5183af6e122c8eab79",
    "premium": "https://www.mercadopago.com.co/subscriptions/checkout?preapproval_plan_id=1d5fb85741ac4454a871e6f2d5870f4",
}
SUBSCRIPTION_LINK_MAP = {**DEFAULT_SUBSCRIPTION_LINK_MAP, **load_json_object_env("MP_SUBSCRIPTION_LINK_MAP")}
TRIAL_DAYS = 0
EMAIL_REGEX = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
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
DEV_MODE_ENABLED = os.getenv("FILESYNC_PRO_DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEV_DEVICE_IDS = {
    device_id.strip()
    for device_id in os.getenv("FILESYNC_PRO_DEV_DEVICE_IDS", "").split(",")
    if device_id.strip()
}
OWNER_HOSTNAMES = {
    hostname.strip().lower()
    for hostname in os.getenv("FILESYNC_PRO_OWNER_HOSTNAMES", "DESKTOP-335GNPS").split(",")
    if hostname.strip()
}
OWNER_DEVICE_IDS = {
    device_id.strip()
    for device_id in os.getenv("FILESYNC_PRO_OWNER_DEVICE_IDS", "").split(",")
    if device_id.strip()
}
PLAN_FEATURES = {
    "basica": {
        "remove_duplicates": False,
        "move_files": False,
        "type_filter": False,
        "monthly_file_limit": 15,
    },
    "pro": {
        "remove_duplicates": True,
        "move_files": False,
        "type_filter": True,
        "monthly_file_limit": 100,
    },
    "premium": {
        "remove_duplicates": True,
        "move_files": True,
        "type_filter": True,
        "monthly_file_limit": None,
    },
}
FULL_FEATURE_ACCESS = {
    "remove_duplicates": True,
    "move_files": True,
    "type_filter": True,
    "monthly_file_limit": None,
}

PROCESS_STATE_FILE = "process_state.json"
PROCESS_MANIFEST_PREFIX = "process_manifest_"
PROCESS_JOURNAL_PREFIX = "process_journal_"


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
except Exception:
    pass


class LicensingError(Exception):
    pass


def get_app_storage_dir():
    base_dir = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class ModernOrganizadorArchivos:
    """Logica de organizacion de archivos."""

    def __init__(self):
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
        self.receipt_amount_pattern = re.compile(r"(cop|usd|eur|mxn|ars|clp|\$)\s*\d", re.IGNORECASE)
        self.receipt_date_pattern = re.compile(r"\b\d{1,4}[\/\-:]\d{1,2}[\/\-:]\d{1,4}\b")
        self.filename_date_patterns = (
            re.compile(r"(?P<year>19\d{2}|20\d{2})[-_\.]?(?P<month>0[1-9]|1[0-2])[-_\.]?(?P<day>0[1-9]|[12]\d|3[01])"),
            re.compile(r"(?P<day>0[1-9]|[12]\d|3[01])[-_\.](?P<month>0[1-9]|1[0-2])[-_\.](?P<year>19\d{2}|20\d{2})"),
        )
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
        self._receipt_detection_cache = {}
        self._date_reference_cache = {}
        self._ocr_text_cache = {}
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
        self._receipt_detection_cache = {}
        self._date_reference_cache = {}
        self._ocr_text_cache = {}
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

    def es_imagen(self, extension):
        return extension.lower() in self.image_extensions

    def obtener_categoria(self, extension, nombre_archivo="", ruta_archivo=None, detectar_recibos=False):
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
        return self.categorias_ui.get(categoria, categoria)

    def construir_ruta_destino(
        self,
        destino_base,
        ruta_archivo,
        organizar_por_categoria=True,
        organizar_por_fecha=True,
        detectar_recibos=False,
    ):
        categoria = self.obtener_categoria(
            ruta_archivo.suffix,
            ruta_archivo.name,
            ruta_archivo=ruta_archivo,
            detectar_recibos=detectar_recibos,
        )
        ruta_destino = Path(destino_base)
        if organizar_por_categoria:
            ruta_destino = ruta_destino / categoria
        if organizar_por_fecha:
            fecha_referencia = self.obtener_fecha_referencia(ruta_archivo)
            ruta_destino = ruta_destino / str(fecha_referencia.year) / self.formatear_carpeta_mes(fecha_referencia)
        return categoria, ruta_destino

    @staticmethod
    def formatear_carpeta_mes(fecha_referencia):
        return MONTH_FOLDER_NAMES.get(fecha_referencia.month, f"{fecha_referencia.month:02d}")

    def obtener_fecha_referencia(self, ruta_archivo):
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
        return str(valor or "").strip().lower().translate(self._text_translation)

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
        base_dir = get_app_storage_dir()
        self.license_file = base_dir / "license.json"
        self.state = self._load_state()

    @staticmethod
    def _encrypt_local_state(payload: bytes) -> bytes:
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
        }
        self._save_state(state)
        return state

    def _save_state(self, state=None):
        if state is not None:
            self.state = state
        payload = json.dumps(self.state, indent=2).encode("utf-8")
        self.license_file.write_bytes(self._encrypt_local_state(payload))

    @staticmethod
    def get_device_id():
        raw = f"{platform.system()}|{platform.node()}|{uuid.getnode()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def get_machine_name():
        return (os.getenv("COMPUTERNAME") or platform.node() or "").strip()

    def is_owner_device(self):
        machine_name = self.get_machine_name().lower()
        if machine_name and machine_name in OWNER_HOSTNAMES:
            return True
        return self.get_device_id() in OWNER_DEVICE_IDS

    @staticmethod
    def _parse_datetime(value):
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
        if DEV_MODE_ENABLED:
            return True
        return self.get_device_id() in DEV_DEVICE_IDS

    def local_unrestricted_access_mode(self):
        if self.is_owner_device():
            return "owner"
        if self.is_developer_access_enabled():
            return "developer"
        return ""

    def has_local_unrestricted_access(self):
        return bool(self.local_unrestricted_access_mode())

    def days_left_in_trial(self):
        if TRIAL_DAYS <= 0:
            return 0
        installed_at = dt.datetime.fromisoformat(self.state["installed_at"])
        elapsed = (dt.datetime.utcnow() - installed_at).days
        return max(TRIAL_DAYS - elapsed, 0)

    def is_feature_unlocked(self):
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
        self.state["status"] = status
        self.state["last_validation"] = dt.datetime.utcnow().isoformat()
        if self.state.get("subscription_id"):
            self.state["subscription_status"] = status
        self._save_state()

    def update_subscription_expiration(self, subscription_expires_at):
        normalized = (subscription_expires_at or "").strip()
        previous = self.state.get("subscription_expires_at", "")
        self.state["subscription_expires_at"] = normalized
        if normalized and normalized != previous:
            self.state["subscription_warning_7_for"] = ""
            self.state["subscription_warning_3_for"] = ""
        self._save_state()

    def is_subscription_expired(self):
        expires_at = self._parse_datetime(self.state.get("subscription_expires_at", ""))
        if not expires_at:
            return False
        now = dt.datetime.now(expires_at.tzinfo) if expires_at.tzinfo else dt.datetime.utcnow()
        return now >= expires_at

    def days_until_block(self):
        expires_at = self._parse_datetime(self.state.get("subscription_expires_at", ""))
        if not expires_at:
            return None
        now = dt.datetime.now(expires_at.tzinfo) if expires_at.tzinfo else dt.datetime.utcnow()
        return (expires_at.date() - now.date()).days

    @staticmethod
    def _current_usage_period():
        return dt.datetime.utcnow().strftime("%Y-%m")

    def get_monthly_usage_info(self, plan_code, features):
        limit = features.get("monthly_file_limit")
        if limit is None:
            return {
                "period": self._current_usage_period(),
                "used": 0,
                "remaining": None,
                "limit": None,
            }

        usage = self.state.setdefault("monthly_usage", {})
        period = self._current_usage_period()
        used = int(usage.get(period, 0) or 0)
        remaining = max(limit - used, 0)
        return {
            "period": period,
            "used": used,
            "remaining": remaining,
            "limit": limit,
        }

    def register_monthly_processed_files(self, plan_code, features, processed_count):
        if processed_count <= 0:
            return

        limit = features.get("monthly_file_limit")
        if limit is None:
            return

        usage = self.state.setdefault("monthly_usage", {})
        period = self._current_usage_period()
        used = int(usage.get(period, 0) or 0)
        usage[period] = min(used + processed_count, limit)
        self._save_state()

    def remember_pending_subscription(self, email, plan_code, subscription_id=""):
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
    """Persiste el estado de una organizacion para poder retomarla."""

    def __init__(self):
        self.base_dir = get_app_storage_dir()
        self.state_file = self.base_dir / PROCESS_STATE_FILE

    @staticmethod
    def _utc_now():
        return dt.datetime.utcnow().isoformat()

    def _load_json_file(self, path):
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_json_file(self, path, payload):
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_state(self):
        state = self._load_json_file(self.state_file)
        return state if isinstance(state, dict) else {}

    def _manifest_path_for(self, run_id):
        return self.base_dir / f"{PROCESS_MANIFEST_PREFIX}{run_id}.json"

    def _journal_path_for(self, run_id):
        return self.base_dir / f"{PROCESS_JOURNAL_PREFIX}{run_id}.jsonl"

    def clear_run_files(self, state=None):
        current_state = state if isinstance(state, dict) and state else self.load_state()
        for key in ("manifest_path", "journal_path"):
            raw_path = str(current_state.get(key, "")).strip()
            if not raw_path:
                continue
            try:
                Path(raw_path).unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self):
        state = self.load_state()
        self.clear_run_files(state)
        try:
            self.state_file.unlink(missing_ok=True)
        except OSError:
            pass

    def create_run(self, config, files):
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
            "organizar_todos": bool(config["organizar_todos"]),
            "tipo_archivo": config["tipo_archivo"],
            "limite_ejecucion": config["limite_ejecucion"],
            "total_archivos": len(files),
        }
        self._save_json_file(self.state_file, state)
        return state

    def update_state(self, **fields):
        state = self.load_state()
        if not state:
            return {}
        state.update(fields)
        state["updated_at"] = self._utc_now()
        self._save_json_file(self.state_file, state)
        return state

    def mark_interrupted(self):
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
        state = self.load_state()
        if not state:
            return
        if summary:
            state["summary"] = summary
        state["status"] = "completed"
        state["updated_at"] = self._utc_now()
        self._save_json_file(self.state_file, state)
        self.clear()

    def get_manifest_files(self, state):
        manifest_path = Path(str(state.get("manifest_path", "")).strip())
        manifest = self._load_json_file(manifest_path)
        files = manifest.get("files", []) if isinstance(manifest, dict) else []
        return [str(Path(file_path)) for file_path in files if str(file_path).strip()]

    def rebuild_runtime_state(self, state):
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


class PaymentAPIClient:
    """Cliente HTTP minimo para comunicarse con el servicio de licencias."""

    def __init__(self, base_url):
        self.base_url = (base_url or "").rstrip("/")

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
        return (
            "No pudimos conectar con el servicio de licencias en este momento.\n\n"
            "Verifica tu conexion a internet e intenta nuevamente en unos minutos."
        )

    def health_check(self):
        return self._request_json("GET", "/health")

    def _request_json(self, method, path, payload=None):
        if not self.base_url:
            raise LicensingError(self._connection_help_message())
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LicensingError(self._humanize_http_error(exc.code, detail)) from exc
        except error.URLError as exc:
            raise LicensingError(self._connection_help_message()) from exc
        except json.JSONDecodeError as exc:
            raise LicensingError("No fue posible completar la verificacion en este momento. Intenta nuevamente.") from exc

    def create_checkout(self, email, plan_code="pro_lifetime"):
        return self._request_json("POST", "/checkout/create", {"email": email, "plan_code": plan_code})

    def resolve_subscription(self, email, plan_code):
        return self._request_json("POST", "/subscriptions/resolve", {"email": email, "plan_code": plan_code})

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
        self.title(f"{APP_NAME} {APP_VERSION} - Organizador de Archivos")
        self.configure(bg="#f4f6f8")
        self.minsize(1040, 680)
        self._configurar_tamano_inicial()

        self.license_manager = LicenseManager()
        self.process_recovery = ProcessRecoveryManager()
        self.api_client = PaymentAPIClient(DEFAULT_API_BASE_URL)
        self.organizador = ModernOrganizadorArchivos()

        self.directorio_origen = tk.StringVar()
        self.directorio_destino = tk.StringVar()
        self.organizar_por_categoria = tk.BooleanVar(value=True)
        self.organizar_por_fecha = tk.BooleanVar(value=True)
        self.detectar_recibos = tk.BooleanVar(value=True)
        self.eliminar_duplicados = tk.BooleanVar(value=True)
        self.mover_en_vez_de_copiar = tk.BooleanVar(value=True)
        self.organizar_todos = tk.BooleanVar(value=True)
        self.tipo_archivo_seleccionado = tk.StringVar()
        self.status_text = tk.StringVar()
        self.checkout_order_id = tk.StringVar(value="")
        self.checkout_email = tk.StringVar(value="")
        self.subscription_id = tk.StringVar(value=self.license_manager.state.get("subscription_id", ""))
        self.subscription_email = tk.StringVar(value=self.license_manager.state.get("subscription_email", ""))
        self.subscription_plan_code = tk.StringVar(value=self.license_manager.state.get("subscription_plan_code", ""))

        self.estadisticas = {
            "total": tk.StringVar(value="0"),
            "procesados": tk.StringVar(value="0"),
            "duplicados": tk.StringVar(value="0"),
            "categorias": tk.StringVar(value="0"),
        }
        self._buttons = []
        self._feature_controls = {}
        self._current_run_limit = None
        self._run_in_progress = False
        self._last_recovery_update = 0.0
        self._progress_batch_size = 100
        self._log_batch_size = 250
        self._recovery_batch_size = 50
        self._left_scroll_canvas = None
        self._left_scroll_window = None

        self._configurar_icono()
        self.configurar_estilos()
        self.crear_widgets()
        self.update_idletasks()
        self._ajustar_geometria_a_contenido()
        self.actualizar_estado_licencia()
        self._configurar_atajos()
        self.protocol("WM_DELETE_WINDOW", self.salir)
        self.after(700, self._mostrar_avisos_licencia)
        self.after(1200, self._sincronizar_licencia_silenciosa)
        self.after(1400, self._ofrecer_reanudar_proceso)
        self.bind("<FocusIn>", lambda _: self._sincronizar_licencia_silenciosa())

    def _configurar_tamano_inicial(self):
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
        return {key: value for key, value in SUBSCRIPTION_LINK_MAP.items() if value}

    @staticmethod
    def _plan_labels():
        return {
            "basica": "Licencia Básica",
            "pro": "Licencia Pro",
            "premium": "Licencia Premium",
        }

    @staticmethod
    def _plan_descriptions():
        return {
            "basica": "Organización esencial por categorías. No incluye filtro por extensión, mover archivos ni eliminar duplicados.",
            "pro": "Agrega filtro por extensión y eliminación de duplicados. Mantiene copia segura en lugar de mover archivos.",
            "premium": "Desbloquea todas las funciones, incluido mover archivos en lugar de copiarlos.",
        }

    @staticmethod
    def _sort_plan_codes(plan_codes):
        preferred_order = {"basica": 0, "pro": 1, "premium": 2}
        return sorted(plan_codes, key=lambda code: (preferred_order.get(code, 99), code))

    def _nombre_plan(self, plan_code):
        normalized = (plan_code or "").strip().lower()
        return self._plan_labels().get(normalized, normalized.replace("_", " ").title() or "Licencia")

    @staticmethod
    def _normalizar_plan_code(plan_code):
        normalized = (plan_code or "").strip().lower()
        return normalized or DEFAULT_PLAN_CODE

    def _plan_features(self, plan_code):
        normalized = self._normalizar_plan_code(plan_code)
        return PLAN_FEATURES.get(normalized, FULL_FEATURE_ACCESS)

    def _plan_code_activo(self):
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
        widget = self._feature_controls.get(feature_key)
        if widget is None:
            return
        if enabled:
            widget.state(["!disabled"])
        else:
            widget.state(["disabled"])

    def _aplicar_restricciones_plan(self):
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
        self.actualizar_disponibilidad_tipo()

    def _monthly_quota_text(self):
        if self.license_manager.has_local_unrestricted_access():
            return ""

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None:
            return " | Cupo mensual ilimitado"
        return f" | Cupo mensual: {usage_info['remaining']} de {usage_info['limit']} disponibles"

    def _iterar_archivos_elegibles(self, origen, extension_filtrada):
        for root, dirnames, files in os.walk(origen):
            dirnames.sort(key=str.lower)
            for nombre_archivo in sorted(files, key=self.organizador.clave_orden_personalizada):
                extension = Path(nombre_archivo).suffix.lower()
                if not self.organizar_todos.get() and extension != extension_filtrada:
                    continue
                yield str(Path(root) / nombre_archivo)

    def _construir_lista_archivos_elegibles(self, origen, extension_filtrada, limite=None):
        archivos = []
        for ruta_archivo in self._iterar_archivos_elegibles(origen, extension_filtrada):
            archivos.append(ruta_archivo)
            if limite is not None and len(archivos) >= limite:
                break
        return archivos

    def _contar_archivos_elegibles(self, origen, extension_filtrada):
        return sum(1 for _ in self._iterar_archivos_elegibles(origen, extension_filtrada))

    @staticmethod
    def _normalizar_email(email):
        return (email or "").strip().lower()

    def _validar_email(self, email):
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
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        email_var = tk.StringVar()
        confirm_var = tk.StringVar()
        error_var = tk.StringVar()
        result = {"email": ""}

        wrapper = ttk.Frame(dialog, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrapper, text=subtitle, font=("Segoe UI", 10, "bold"), wraplength=380, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(0, 12)
        )

        ttk.Label(wrapper, text="Correo electrónico").pack(anchor=tk.W)
        email_entry = ttk.Entry(wrapper, textvariable=email_var, width=42)
        email_entry.pack(fill=tk.X, pady=(4, 10))

        ttk.Label(wrapper, text="Confirmar correo").pack(anchor=tk.W)
        confirm_entry = ttk.Entry(wrapper, textvariable=confirm_var, width=42)
        confirm_entry.pack(fill=tk.X, pady=(4, 8))

        ttk.Label(wrapper, textvariable=error_var, foreground="#b42318", wraplength=380, justify=tk.LEFT).pack(
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
        dialog = tk.Toplevel(self)
        dialog.title("Elegir plan")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        selected_plan = tk.StringVar(value=default_plan)
        result = {"plan_code": ""}
        labels = self._plan_labels()
        descriptions = self._plan_descriptions()

        wrapper = ttk.Frame(dialog, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            wrapper,
            text="Selecciona el plan que quieres pagar:",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=tk.W, pady=(0, 10))

        for plan_code in plan_codes:
            label = labels.get(plan_code, plan_code.replace("_", " ").title())
            description = descriptions.get(plan_code, "Suscripción mensual con bloqueo automático si no se renueva.")
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
        self._icon_images = []

        for icono_png_name in ICON_PNG_VARIANTS:
            icono_png = resolve_app_resource(ICON_ASSET_DIR, icono_png_name) or resolve_app_resource(icono_png_name)
            if icono_png is None:
                continue
            try:
                self._icon_images.append(tk.PhotoImage(file=str(icono_png)))
            except Exception:
                continue

        if self._icon_images:
            try:
                self.iconphoto(True, *self._icon_images)
            except Exception:
                self._icon_images = []

        icono_ico = resolve_app_resource(ICON_ASSET_DIR, ICON_ICO_NAME) or resolve_app_resource(ICON_ICO_NAME)
        if icono_ico is not None:
            try:
                self.iconbitmap(default=str(icono_ico))
            except Exception:
                pass

    def _configurar_atajos(self):
        self.bind("<Control-o>", lambda _: self.seleccionar_origen())
        self.bind("<Control-d>", lambda _: self.seleccionar_destino())
        self.bind("<F5>", lambda _: self.iniciar_organizacion())
        self.bind("<Control-q>", lambda _: self.salir())

    def configurar_estilos(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Titulo.TLabel", font=("Segoe UI", 26, "bold"), background="#f4f6f8", foreground="#10233b")
        style.configure("Subtitulo.TLabel", font=("Segoe UI", 11), background="#f4f6f8", foreground="#607086")
        style.configure("Card.TLabelframe", background="#ffffff", borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#10233b", font=("Segoe UI", 11, "bold"))
        style.configure(
            "Primary.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=10,
            foreground="#ffffff",
            background="#0f5f8c",
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            foreground=[("disabled", "#eef4f7"), ("pressed", "#ffffff"), ("active", "#ffffff")],
            background=[("disabled", "#8ea8b8"), ("pressed", "#0b4c70"), ("active", "#1b789f")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=8,
            foreground="#10233b",
            background="#ffffff",
            borderwidth=1,
            relief="solid",
            focusthickness=0,
        )
        style.map(
            "Secondary.TButton",
            foreground=[("disabled", "#6f7c8c"), ("pressed", "#10233b"), ("active", "#10233b")],
            background=[("disabled", "#dde4ea"), ("pressed", "#e8eef2"), ("active", "#f7fafc")],
            bordercolor=[("disabled", "#c6d0d8"), ("pressed", "#90a4b2"), ("active", "#a6b8c4")],
        )
        style.configure("TCheckbutton", background="#ffffff", font=("Segoe UI", 10))
        style.configure("StatCard.TFrame", background="#f7fafc", borderwidth=1, relief="solid")
        style.configure("StatLabel.TLabel", background="#f7fafc", foreground="#607086", font=("Segoe UI", 9, "bold"))
        style.configure("StatValue.TLabel", background="#f7fafc", foreground="#10233b", font=("Segoe UI", 18, "bold"))
        style.configure("ScrollHost.TFrame", background="#f4f6f8")

    def crear_widgets(self):
        container = ttk.Frame(self, padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        self._crear_header(container)

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        content.columnconfigure(0, weight=4, minsize=380)
        content.columnconfigure(1, weight=5, minsize=460)
        content.rowconfigure(0, weight=1)
        self._crear_panel_izquierdo(content)
        self._crear_panel_derecho(content)

    def _crear_header(self, parent):
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill=tk.X)

        title_frame = ttk.Frame(wrapper)
        title_frame.pack(fill=tk.X)
        ttk.Label(title_frame, text=f"{APP_NAME} {APP_VERSION}", style="Titulo.TLabel").pack(anchor=tk.W)
        ttk.Label(
            title_frame,
            text="Organiza tus archivos por categorías, fechas y reglas inteligentes.",
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
        ]:
            btn = ttk.Button(license_bar, text=text, command=command, style="Secondary.TButton")
            btn.pack(side=tk.RIGHT, padx=(8, 0))
            self._buttons.append(btn)

    def _crear_panel_izquierdo(self, parent):
        left_host = ttk.Frame(parent, style="ScrollHost.TFrame")
        left_host.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left_host.columnconfigure(0, weight=1)
        left_host.rowconfigure(0, weight=1)

        self._left_scroll_canvas = tk.Canvas(left_host, highlightthickness=0, background="#f4f6f8")
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
        self._crear_selector_directorio(dir_frame, "Directorio de origen", self.directorio_origen, self.seleccionar_origen)
        self._crear_selector_directorio(dir_frame, "Directorio de destino", self.directorio_destino, self.seleccionar_destino)

        opts_frame = ttk.LabelFrame(left_panel, text="Opciones", padding=14, style="Card.TLabelframe")
        opts_frame.pack(fill=tk.X, pady=(0, 18))
        for text, variable in [
            ("Organizar por categorías", self.organizar_por_categoria),
            ("Crear carpetas por a\u00f1o y mes", self.organizar_por_fecha),
            ("Detectar recibos en imágenes", self.detectar_recibos),
            ("Eliminar archivos duplicados", self.eliminar_duplicados),
            ("Mover archivos en lugar de copiar", self.mover_en_vez_de_copiar),
        ]:
            widget = ttk.Checkbutton(opts_frame, text=text, variable=variable)
            widget.pack(anchor=tk.W, pady=3)
            if variable is self.eliminar_duplicados:
                self._feature_controls["remove_duplicates"] = widget
            elif variable is self.mover_en_vez_de_copiar:
                self._feature_controls["move_files"] = widget

        tipo_frame = ttk.LabelFrame(opts_frame, text="Filtro de archivos", padding=10, style="Card.TLabelframe")
        tipo_frame.pack(fill=tk.X, pady=(12, 0))
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
        right_panel = ttk.Frame(parent)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        checkout_frame = ttk.LabelFrame(right_panel, text="Ventas y activación", padding=14, style="Card.TLabelframe")
        checkout_frame.pack(fill=tk.X, pady=(0, 14))
        ttk.Label(
            checkout_frame,
            text="Selecciona el plan que prefieras y completa tu compra para desbloquear la app.",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        log_frame = ttk.LabelFrame(right_panel, text="Registro de actividad", padding=14, style="Card.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.texto_log = scrolledtext.ScrolledText(log_frame, height=14, font=("Consolas", 9), wrap=tk.WORD, bg="#f8fafc")
        self.texto_log.pack(fill=tk.BOTH, expand=True)
        self.texto_log.tag_config("exito", foreground="#117a37")
        self.texto_log.tag_config("error", foreground="#b42318")
        self.texto_log.tag_config("advertencia", foreground="#b54708")
        self.texto_log.tag_config("info", foreground="#175cd3")
        self.texto_log.tag_config("subtitulo", font=("Consolas", 9, "bold"))

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

    def _crear_selector_directorio(self, parent, label, variable, command):
        ttk.Label(parent, text=label, font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, pady=(0, 6))
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row, text="Examinar", command=command, style="Secondary.TButton").pack(side=tk.RIGHT)

    @staticmethod
    def _crear_stat_card(parent, row, column, label, variable):
        card = ttk.Frame(parent, padding=(12, 10), style="StatCard.TFrame")
        card.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
        ttk.Label(card, text=label, style="StatLabel.TLabel").pack(anchor=tk.W)
        ttk.Label(card, textvariable=variable, style="StatValue.TLabel").pack(anchor=tk.W, pady=(6, 0))

    def _actualizar_scroll_panel_izquierdo(self, _event=None):
        if self._left_scroll_canvas is None:
            return
        self._left_scroll_canvas.configure(scrollregion=self._left_scroll_canvas.bbox("all"))

    def _ajustar_ancho_panel_izquierdo(self, event):
        if self._left_scroll_canvas is None or self._left_scroll_window is None:
            return
        self._left_scroll_canvas.itemconfigure(self._left_scroll_window, width=event.width)

    def _activar_scroll_panel_izquierdo(self, _event=None):
        self.bind_all("<MouseWheel>", self._scroll_panel_izquierdo_mousewheel)

    def _desactivar_scroll_panel_izquierdo(self, _event=None):
        self.unbind_all("<MouseWheel>")

    def _scroll_panel_izquierdo_mousewheel(self, event):
        if self._left_scroll_canvas is None:
            return
        delta = -1 if event.delta > 0 else 1
        self._left_scroll_canvas.yview_scroll(delta, "units")

    def actualizar_estado_licencia(self):
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
        features = self._plan_features(self._plan_code_activo())
        if not features["type_filter"]:
            self.combo_tipo.configure(state="disabled")
            return
        self.combo_tipo.configure(state="disabled" if self.organizar_todos.get() else "readonly")

    def _cargar_configuracion_guardada(self, state):
        self.directorio_origen.set(str(state.get("origen", "")).strip())
        self.directorio_destino.set(str(state.get("destino", "")).strip())
        self.organizar_por_categoria.set(bool(state.get("organizar_por_categoria", True)))
        self.organizar_por_fecha.set(bool(state.get("organizar_por_fecha", True)))
        self.detectar_recibos.set(bool(state.get("detectar_recibos", True)))
        self.eliminar_duplicados.set(bool(state.get("eliminar_duplicados", True)))
        self.mover_en_vez_de_copiar.set(bool(state.get("mover_en_vez_de_copiar", True)))
        self.organizar_todos.set(bool(state.get("organizar_todos", True)))
        self.tipo_archivo_seleccionado.set(str(state.get("tipo_archivo", "")).strip())
        self.actualizar_disponibilidad_tipo()

    def _preparar_estado_visual_ejecucion(self, total_archivos, revisados=0, procesados=0, duplicados=0, categorias=0):
        self.estadisticas["total"].set(str(total_archivos))
        self.estadisticas["procesados"].set(str(procesados))
        self.estadisticas["duplicados"].set(str(duplicados))
        self.estadisticas["categorias"].set(str(categorias))
        self.progreso.configure(maximum=max(total_archivos, 1), value=min(revisados, total_archivos))

    def _registrar_evento_recuperacion(self, journal_buffer, index, status, origen, destino="", categoria="", file_hash=""):
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
        if not journal_buffer:
            return
        journal_handle.writelines(journal_buffer)
        journal_handle.flush()
        journal_buffer.clear()

    def _persistir_resumen_parcial(self, revisados):
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

    def limpiar_log(self):
        self.texto_log.delete("1.0", tk.END)
        self.log("Registro limpiado.", "info")

    def comprar_licencia(self):
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
            "Escribe tu correo dos veces para evitar errores antes de abrir el pago en Mercado Pago.",
        )
        if not email:
            return

        default_plan = DEFAULT_SUBSCRIPTION_PLAN_CODE if DEFAULT_SUBSCRIPTION_PLAN_CODE in plan_links else plans[0]
        plan_code = self._elegir_plan(plans, default_plan)
        if not plan_code:
            return
        if plan_code not in plan_links:
            messagebox.showerror(
                "Plan no válido",
                "Ese plan no está disponible en este momento.",
            )
            return

        plan_name = self._nombre_plan(plan_code)
        access_mode = self.license_manager.local_unrestricted_access_mode()
        if access_mode == "owner":
            machine_name = self.license_manager.get_machine_name() or "este equipo"
            self.log(f"El equipo propietario {machine_name} ya tiene acceso completo. No hace falta comprar el plan {plan_code}.", "info")
            messagebox.showinfo(
                "Equipo propietario",
                f"Este equipo ({machine_name}) ya tiene todas las funciones desbloqueadas.\n\nNo hace falta comprar ni activar una licencia aquí.",
            )
            return
        if access_mode == "developer":
            self.log(f"Simulación de compra en modo desarrollador para {email}. Plan {plan_code}.", "info")
            messagebox.showinfo(
                "Modo desarrollador",
                f"Prueba local completada.\n\nCorreo: {email}\nPlan: {plan_name}\n\nNo se abrirá Mercado Pago ni se realizará un cobro real.",
            )
            return

        try:
            self.api_client.health_check()
        except LicensingError as exc:
            self.log("El servicio de licencias no esta disponible; se cancelo la apertura del pago.", "error")
            messagebox.showerror(
                "Compra no disponible",
                f"No se abrira Mercado Pago porque la app no puede conectar con el servicio de licencias.\n\n{exc}",
            )
            return

        checkout_url = plan_links[plan_code]
        self.license_manager.remember_pending_subscription(email, plan_code)
        self.actualizar_estado_licencia()
        self.log(f"Link de plan local listo para {email}. Plan {plan_code}.", "info")
        if checkout_url:
            webbrowser.open(checkout_url)
            messagebox.showinfo(
                "Suscripción iniciada",
                "Se abrió Mercado Pago en tu navegador.\n\n"
                "Usa el mismo correo que acabas de escribir para completar tu compra.\n\n"
                "Cuando termines, vuelve a la app y tu licencia se revisará automáticamente.",
            )

    def activar_licencia(self):
        email = self._pedir_correo_confirmado(
            "Activar licencia",
            "Escribe el mismo correo con el que hiciste la compra para activar esta licencia.",
        )
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
            self.license_manager.activate_local(
                email,
                license_key,
                subscription_id=response.get("subscription_id", ""),
                subscription_plan_code=response.get("plan_code", self.license_manager.state.get("subscription_plan_code", "")),
                subscription_expires_at=response.get("next_payment_date", ""),
            )
            self.actualizar_estado_licencia()
            self._mostrar_avisos_licencia()
            self.log("Licencia activada correctamente.", "exito")
            messagebox.showinfo("Licencia activada", "La app quedó activada en este equipo.")
            return

        messagebox.showwarning("Licencia no válida", response.get("message", "No se pudo activar la licencia."))

    def verificar_licencia(self):
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
                messagebox.showinfo("Sin licencia local", "Todavía no hay una licencia local guardada en esta app.")
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
            try:
                activated, message = self._registrar_licencia_confirmada(email, license_key)
            except LicensingError as exc:
                messagebox.showerror("No fue posible activar", str(exc))
                return
            if not activated:
                messagebox.showwarning("Licencia no activada", message)
                return
            self.actualizar_estado_licencia()
            messagebox.showinfo("Pago aprobado", f"Tu licencia fue emitida y activada.\n\nClave: {license_key}")
        else:
            messagebox.showinfo("Pago en proceso", "Mercado Pago aún no confirma el cobro o la licencia no ha sido emitida.")

    def _verificar_suscripcion_pendiente(self, email, plan_code):
        try:
            response = self.api_client.resolve_subscription(email, plan_code)
        except LicensingError as exc:
            messagebox.showerror("No fue posible consultar la suscripción", str(exc))
            return

        status = response.get("status", "desconocido")
        subscription_id = response.get("subscription_id", "")
        self.license_manager.remember_pending_subscription(email, plan_code, subscription_id=subscription_id)
        self.actualizar_estado_licencia()
        self.log(f"Estado de suscripción para {email}: {status}.", "info")

        license_key = response.get("license_key")
        if license_key and status in {"authorized", "active"}:
            try:
                activated, message = self._registrar_licencia_confirmada(
                    response.get("email", email),
                    license_key,
                    subscription_id=subscription_id,
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
            messagebox.showinfo(
                "Suscripción activa",
                "Tu licencia fue emitida y activada correctamente en este equipo.",
            )
            return

        if status in {"authorized", "active"}:
            messagebox.showinfo(
                "Suscripción detectada",
                "La suscripción ya existe, pero la licencia todavía se está sincronizando. Intenta verificar otra vez en unos segundos.",
            )
            return

        if status == "not_found":
            messagebox.showinfo(
                "Aún no aparece",
                "Todavía no encontramos una suscripción para ese correo y plan. Si acabas de pagar, espera un momento y vuelve a verificar.",
            )
            return

        messagebox.showinfo(
            "Suscripción en proceso",
            "Mercado Pago aún no marca la suscripción como activa. Vuelve a verificar más tarde.",
        )

    def _asegurar_acceso(self):
        if self.license_manager.is_feature_unlocked():
            return True
        messagebox.showwarning(
            "Licencia requerida",
            "Esta función requiere una licencia activa.\n\nCompra o activa una licencia para continuar.",
        )
        return False

    def analizar_directorio(self):
        origen = self.directorio_origen.get()
        if not origen or not os.path.exists(origen):
            messagebox.showwarning("Directorio inválido", "Selecciona un directorio de origen válido.")
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
        self.log("Análisis completado", "subtitulo")
        self.log(f"Archivos encontrados: {total_archivos}", "info")
        self.log(f"Categorías detectadas: {len(categorias_count)}", "info")
        for categoria, cantidad in sorted(categorias_count.items()):
            porcentaje = (cantidad / total_archivos * 100) if total_archivos else 0
            categoria_ui = self.organizador.obtener_nombre_categoria_ui(categoria)
            self.log(f"{categoria_ui}: {cantidad} archivo(s) ({porcentaje:.1f}%)")
        self.log("=" * 50, "subtitulo")

    def iniciar_organizacion(self):
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
        if not self.organizar_todos.get() and not self.tipo_archivo_seleccionado.get():
            messagebox.showerror("Error", "Selecciona una extensión para filtrar.")
            return

        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        archivos_elegibles = self._contar_archivos_elegibles(origen, extension_filtrada)
        if usage_info["limit"] is None:
            self._current_run_limit = None
        else:
            restante = usage_info["remaining"]
            if restante <= 0:
                messagebox.showwarning(
                    "Cupo mensual agotado",
                    f"Tu plan {self._nombre_plan(plan_code)} ya usó los {usage_info['limit']} archivos disponibles de este mes.\n\n"
                    "Espera al siguiente mes o mejora tu licencia para seguir organizando.",
                )
                return
            self._current_run_limit = min(archivos_elegibles, restante)

        resumen = [
            f"Origen: {origen}",
            f"Destino: {destino}",
            f"Tipo: {'Todos' if self.organizar_todos.get() else self.tipo_archivo_seleccionado.get()}",
            f"Carpetas por fecha: {'Sí' if self.organizar_por_fecha.get() else 'No'}",
            f"Detección de recibos: {'Sí' if self.detectar_recibos.get() else 'No'}",
            f"Eliminar duplicados: {'Sí' if self.eliminar_duplicados.get() else 'No'}",
            f"Mover archivos: {'Sí' if self.mover_en_vez_de_copiar.get() else 'No'}",
        ]
        if usage_info["limit"] is None:
            resumen.append("Cupo mensual: Ilimitado")
        else:
            resumen.append(f"Cupo mensual restante: {usage_info['remaining']} de {usage_info['limit']}")
            if archivos_elegibles > usage_info["remaining"]:
                resumen.append(f"Solo se organizarán hasta {usage_info['remaining']} archivo(s) en esta ejecución.")
        if not messagebox.askyesno("Confirmar organización", "\n".join(resumen)):
            self._current_run_limit = None
            return

        self.organizador.reset()
        archivos_planificados = self._construir_lista_archivos_elegibles(origen, extension_filtrada, limite=self._current_run_limit)
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
                "organizar_todos": self.organizar_todos.get(),
                "tipo_archivo": self.tipo_archivo_seleccionado.get(),
                "limite_ejecucion": self._current_run_limit,
            },
            archivos_planificados,
        )
        self._iniciar_hilo_organizacion(archivos_planificados, run_state, start_index=0)

    def _iniciar_hilo_organizacion(self, archivos_planificados, run_state, start_index=0):
        self._run_in_progress = True
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
        destino_base = Path(run_state["destino"])
        organizar_por_categoria = bool(run_state.get("organizar_por_categoria", True))
        organizar_por_fecha = bool(run_state.get("organizar_por_fecha", True))
        detectar_recibos = bool(run_state.get("detectar_recibos", True))
        eliminar_duplicados = bool(run_state.get("eliminar_duplicados", True))
        mover_archivos = bool(run_state.get("mover_en_vez_de_copiar", True))
        total_archivos = len(archivos_planificados)
        directorios_creados = set()

        journal_path = Path(run_state["journal_path"])
        try:
            with journal_path.open("a", encoding="utf-8") as journal_handle:
                journal_buffer = []
                for index in range(start_index, total_archivos):
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
                        if eliminar_duplicados:
                            hash_resultado = self.organizador.calcular_hash_archivo(ruta_completa)
                            if hash_resultado is None:
                                self.organizador.estadisticas["errores"] += 1
                                estado = "error"
                                self._registrar_evento_recuperacion(journal_buffer, index, estado, ruta_completa)
                                self.after(0, self.log, f"No se pudo leer {nombre_archivo}.", "error")
                            else:
                                hash_archivo = hash_resultado
                                estado = "ok"

                        if estado != "error" or not eliminar_duplicados:
                            if eliminar_duplicados and hash_archivo in self.organizador.hashes_archivos:
                                self.organizador.estadisticas["duplicados"] += 1
                                estado = "duplicate"
                                if mover_archivos:
                                    try:
                                        os.remove(ruta_completa)
                                    except OSError as exc:
                                        self.organizador.estadisticas["errores"] += 1
                                        estado = "error"
                                        self.after(0, self.log, f"No se pudo eliminar duplicado: {exc}", "error")
                                self._registrar_evento_recuperacion(
                                    journal_buffer, index, estado, ruta_completa, file_hash=hash_archivo
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

    def _actualizar_progreso(self, valor, procesados=None):
        self.progreso["value"] = valor
        self.estadisticas["procesados"].set(str(valor if procesados is None else procesados))

    def _finalizar_proceso(self):
        stats = self.organizador.estadisticas
        plan_code = self._plan_code_activo()
        features = self._plan_features(plan_code)
        self.license_manager.register_monthly_processed_files(plan_code, features, stats["procesados"])
        self.estadisticas["procesados"].set(str(stats["procesados"]))
        self.estadisticas["duplicados"].set(str(stats["duplicados"]))
        self.estadisticas["categorias"].set(str(len(stats["categorias_usadas"])))

        self.log("=" * 50, "subtitulo")
        self.log("Organización completada", "subtitulo")
        self.log(f"Archivos encontrados: {stats['total_archivos']}")
        self.log(f"Archivos procesados: {stats['procesados']}")
        self.log(f"Duplicados eliminados: {stats['duplicados']}")
        self.log(f"Errores: {stats['errores']}")
        self.log(f"Categorías usadas: {len(stats['categorias_usadas'])}")
        usage_info = self.license_manager.get_monthly_usage_info(plan_code, features)
        if usage_info["limit"] is None:
            self.log("Cupo mensual: ilimitado", "info")
        else:
            self.log(
                f"Cupo mensual restante del plan {self._nombre_plan(plan_code)}: {usage_info['remaining']} de {usage_info['limit']}.",
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
        self.actualizar_estado_licencia()

        messagebox.showinfo(
            "Proceso finalizado",
            "La organización ha terminado.\n\n"
            f"Procesados: {stats['procesados']}\n"
            f"Duplicados: {stats['duplicados']}\n"
            f"Categorías: {len(stats['categorias_usadas'])}",
        )

    def _cambiar_estado_botones(self, estado):
        for boton in self._buttons:
            boton.state(["disabled"] if estado == "disabled" else ["!disabled"])

    def salir(self):
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


def main():
    app = ModernOrganizadorGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
