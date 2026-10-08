"""Compatibilidad multiplataforma (Windows / macOS) para FileSync Pro.

Este módulo agrupa todo lo que cambia según el sistema operativo, para que
`Organizador.py` no se llene de `if sys.platform == ...` y para poder probarlo
sin abrir ninguna ventana (no importa Tkinter):

- Detección de plataforma (`IS_WINDOWS`, `IS_MAC`).
- Tipografías y escala de tamaño (Tk en macOS trabaja a 72 ppp y en Windows a
  96 ppp, así que el mismo "10 puntos" se ve ~25 % más chico en Mac).
- Carpeta de datos privados de la app (`%APPDATA%` en Windows,
  `~/Library/Application Support` en macOS).
- Recorrido de carpetas que, en macOS, no toca los "paquetes" (`.app`,
  `.photoslibrary`...) ni los archivos de metadatos del sistema (`.DS_Store`).
- Identificador estable del equipo en macOS (UUID de hardware).
- Ubicación de Tesseract: el empaquetado dentro de la app (Windows/macOS) o el de Homebrew/MacPorts.

En Windows todas estas funciones se comportan igual que antes de agregar la
compatibilidad con macOS: la lógica nueva solo actúa cuando `mac` es verdadero.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# --- Tipografías -----------------------------------------------------------
# Segoe UI y Consolas solo existen en Windows. En macOS se usan Helvetica Neue
# (siempre instalada) y Menlo. La escala de macOS compensa los 72 ppp de Tk
# (72 ppp * 4/3 = 96 ppp): así los textos miden los mismos píxeles que en
# Windows y los anchos/`wraplength` de la interfaz siguen encajando.
if IS_WINDOWS:
    FONT_UI, FONT_MONO, FONT_SCALE = "Segoe UI", "Consolas", 1.0
elif IS_MAC:
    FONT_UI, FONT_MONO, FONT_SCALE = "Helvetica Neue", "Menlo", 4 / 3
else:
    FONT_UI, FONT_MONO, FONT_SCALE = "DejaVu Sans", "DejaVu Sans Mono", 1.0

# Píxeles que desplaza cada "unidad" de la rueda/trackpad en los paneles con
# scroll de macOS (ver `_scroll_panel_mousewheel` en Organizador.py). Si el
# desplazamiento se siente muy rápido o muy lento en un Mac real, es el único
# número que hay que ajustar.
MAC_SCROLL_UNIT_PX = 12


def scale_font_size(size: int, scale: float | None = None) -> int:
    """Convierte un tamaño de fuente pensado para Windows al equivalente de
    esta plataforma. En Windows (escala 1.0) devuelve el mismo número."""
    factor = FONT_SCALE if scale is None else scale
    if factor == 1.0:
        return size
    return max(1, round(size * factor))


# --- Carpeta de datos de la app --------------------------------------------
def app_data_root(*, mac: bool | None = None) -> Path:
    """Carpeta base donde la app crea su subcarpeta de datos privados.

    macOS: `~/Library/Application Support` (la ubicación estándar del sistema).
    Resto: `%APPDATA%` si existe, o la carpeta personal (comportamiento
    original de la app en Windows).
    """
    if IS_MAC if mac is None else mac:
        return Path.home() / "Library" / "Application Support"
    return Path(os.getenv("APPDATA", Path.home()))


# --- Recorrido de carpetas en macOS -----------------------------------------
# Archivos que macOS y Finder crean solos y que nunca son "del usuario".
MACOS_METADATA_FILES = frozenset(
    {".ds_store", ".localized", ".apdisk", ".volumeicon.icns", ".com.apple.timemachine.donotpresent"}
)
# Carpetas de sistema típicas de discos externos y del usuario.
MACOS_METADATA_DIRS = frozenset(
    {".trashes", ".trash", ".spotlight-v100", ".fseventsd", ".temporaryitems", ".documentrevisions-v100"}
)
# En macOS estas carpetas se comportan como UN solo archivo (Finder no muestra
# su interior). Si la app las recorriera, sacaría sus piezas internas y
# rompería la aplicación/biblioteca.
MACOS_PACKAGE_SUFFIXES = (
    ".app",
    ".framework",
    ".bundle",
    ".plugin",
    ".kext",
    ".appex",
    ".xpc",
    ".prefpane",
    ".photoslibrary",
    ".musiclibrary",
    ".tvlibrary",
    ".imovielibrary",
    ".fcpbundle",
    ".logicx",
    ".band",
    ".xcodeproj",
    ".xcworkspace",
    ".playground",
    ".rtfd",
    ".sparsebundle",
    ".dsym",
    ".pages",
    ".numbers",
    ".key",
    ".pkg",
    ".mpkg",
)


def is_macos_metadata_file(name: str) -> bool:
    """True para `.DS_Store`, `.localized` y los `._archivo` (AppleDouble) que
    macOS crea junto a los archivos en discos que no son APFS (USB exFAT/FAT)."""
    return name.lower() in MACOS_METADATA_FILES or name.startswith("._")


def is_macos_package_dir(name: str) -> bool:
    """True si una CARPETA se llama como un paquete de macOS (`Foo.app`...)."""
    return name.lower().endswith(MACOS_PACKAGE_SUFFIXES)


def is_macos_skipped_dir(name: str) -> bool:
    """True si el recorrido no debe entrar a esta carpeta en macOS."""
    return name.lower() in MACOS_METADATA_DIRS or is_macos_package_dir(name)


def walk_files(
    top: str | Path, topdown: bool = True, *, mac: bool | None = None
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Igual que `os.walk`, pero en macOS omite los paquetes (`.app`, ...), las
    carpetas de sistema y los archivos de metadatos (`.DS_Store`, `._*`).

    Fuera de macOS entrega exactamente lo mismo que `os.walk`. Se entrega la
    MISMA lista `dirnames` que usa `os.walk`, así que quien llama puede seguir
    ordenándola o podándola en el sitio (`topdown=True`). Con `topdown=False`
    no se puede podar carpetas, por eso las rutas dentro de un paquete se deben
    filtrar con `is_inside_macos_package`.
    """
    for root, dirnames, filenames in os.walk(top, topdown=topdown):
        if IS_MAC if mac is None else mac:
            dirnames[:] = [name for name in dirnames if not is_macos_skipped_dir(name)]
            filenames = [name for name in filenames if not is_macos_metadata_file(name)]
        yield root, dirnames, filenames


def is_inside_macos_package(path: str | Path, root: str | Path) -> bool:
    """True si `path` (dentro de `root`) está dentro de un paquete de macOS
    o de una carpeta de sistema. Sirve para los recorridos `topdown=False`."""
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return False
    return any(is_macos_skipped_dir(part) for part in parts)


def contains_only_ds_store(folder: str | Path) -> bool:
    """True si la carpeta no tiene nada más que `.DS_Store`. Finder lo crea en
    casi cualquier carpeta que se abre, así que una carpeta "vacía" en macOS
    casi siempre tiene ese archivo dentro."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return bool(names) and all(name == ".DS_Store" for name in names)


# --- Identificador del equipo en macOS ---------------------------------------
_IOREG_UUID = re.compile(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{8,})"')


def parse_ioreg_uuid(text: str) -> str:
    """Extrae el `IOPlatformUUID` de la salida de `ioreg` ("" si no aparece)."""
    match = _IOREG_UUID.search(text or "")
    return match.group(1).upper() if match else ""


@lru_cache(maxsize=1)
def mac_hardware_uuid() -> str:
    """UUID de hardware del Mac (el mismo que muestra "Acerca de este Mac >
    Informe del sistema"). No cambia con el nombre del equipo, la red ni el
    Wi-Fi, a diferencia de `platform.node()`, que en macOS varía según la red a
    la que se conecte el Mac. Devuelve "" si no se pudo leer."""
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return parse_ioreg_uuid(result.stdout)


# --- Tesseract (OCR) en macOS -------------------------------------------------
MAC_TESSERACT_CANDIDATES = (
    "/opt/homebrew/bin/tesseract",  # Homebrew en Apple Silicon
    "/usr/local/bin/tesseract",  # Homebrew en Mac Intel
    "/opt/local/bin/tesseract",  # MacPorts
)


def find_bundled_tesseract(
    resolve: Callable[..., Path | None], *, windows: bool | None = None
) -> tuple[str, str | None] | None:
    """Busca el Tesseract que se empaqueta dentro de la app.

    `resolve(*partes)` debe devolver la ruta existente de un recurso de la app
    (en `Organizador.py` es `resolve_app_resource`). Se prueba `tesseract/` (así
    queda dentro del .exe/.app) y `vendor/tesseract/` (al correr desde el código,
    donde `build.ps1` lo prepara). Devuelve `(ruta_del_ejecutable, carpeta_tessdata)`
    o `None` si no hay ninguno; `carpeta_tessdata` es `None` si falta esa carpeta.
    """
    exe_name = "tesseract.exe" if (IS_WINDOWS if windows is None else windows) else "tesseract"
    for parts in (("tesseract", exe_name), ("vendor", "tesseract", exe_name)):
        found = resolve(*parts)
        if found:
            tessdata = Path(found).parent / "tessdata"
            return str(found), (str(tessdata) if tessdata.is_dir() else None)
    return None


def find_tesseract_cmd(
    *,
    mac: bool | None = None,
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
) -> str | None:
    """Ruta de Tesseract que hay que fijar en `pytesseract`, o `None` si no hace
    falta (no es macOS, o ya está en el PATH) o no se encontró.

    Una app abierta con doble clic desde Finder no hereda el PATH de la
    terminal, así que no ve el Tesseract instalado con Homebrew aunque exista.
    """
    if not (IS_MAC if mac is None else mac):
        return None
    if which("tesseract"):
        return None
    for candidate in MAC_TESSERACT_CANDIDATES:
        if exists(candidate):
            return candidate
    return None
