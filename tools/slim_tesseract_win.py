#!/usr/bin/env python3
"""Adelgaza la copia de Tesseract para Windows (`vendor\\tesseract`) antes de empaquetarla en el .exe.

La compilación de Tesseract de UB Mannheim que instala `winget` pesa ~167 MB, casi toda inútil para
leer facturas:

1. `libtesseract-5.dll` trae 97 MB de información de depuración (secciones `.debug_*` de DWARF y la tabla
   de símbolos). El código real ocupa ~4 MB. Se quitan, igual que haría `strip`.
2. Vienen ~25 DLL (pango, cairo, glib, harfbuzz, ICU...) que solo usan las herramientas de
   entrenamiento, no `tesseract.exe`. Se calcula qué DLL necesita de verdad `tesseract.exe` siguiendo sus
   importaciones (y las de sus DLL) y se borran las demás.

Solo usa la biblioteca estándar (la prueba de lectura usa Pillow si está instalado). Se puede correr en
cualquier sistema; en Linux, con `--runner wine64`, la prueba usa Wine.

Uso (lo llama build.ps1):
    python tools/slim_tesseract_win.py vendor/tesseract --selftest
    python tools/slim_tesseract_win.py vendor/tesseract --no-strip      # solo borrar DLL sobrantes

Si cualquier comprobación falla, termina con código 1 y build.ps1 vuelve a copiar Tesseract completo.
"""

from __future__ import annotations

import argparse
import os
import shlex
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_LANGS = ("spa", "eng")
DIR_EXPORT, DIR_IMPORT, DIR_SECURITY, DIR_DEBUG, DIR_DELAY_IMPORT = 0, 1, 4, 6, 13
IMAGE_FILE_LINE_NUMS_STRIPPED, IMAGE_FILE_LOCAL_SYMS_STRIPPED, IMAGE_FILE_DEBUG_STRIPPED = 0x4, 0x8, 0x200
COFF_SYMBOL_SIZE = 18


class SlimError(Exception):
    """Fallo esperado con un mensaje para el usuario."""


# --- Lectura mínima de archivos PE (.exe / .dll) --------------------------------------------------
@dataclass
class Section:
    index: int
    name: str
    virtual_size: int
    virtual_address: int
    raw_size: int
    raw_pointer: int


@dataclass
class PE:
    data: bytes
    coff_offset: int  # inicio de IMAGE_FILE_HEADER (justo después de la firma "PE\0\0")
    optional_offset: int
    is_pe32_plus: bool
    section_table_offset: int
    sections: list[Section] = field(default_factory=list)
    symbol_pointer: int = 0
    symbol_count: int = 0

    @property
    def section_alignment(self) -> int:
        return struct.unpack_from("<I", self.data, self.optional_offset + 32)[0]

    @property
    def file_alignment(self) -> int:
        return struct.unpack_from("<I", self.data, self.optional_offset + 36)[0]

    @property
    def size_of_headers(self) -> int:
        return struct.unpack_from("<I", self.data, self.optional_offset + 60)[0]

    @property
    def data_directory_offset(self) -> int:
        return self.optional_offset + (112 if self.is_pe32_plus else 96)

    def data_directory(self, index: int) -> tuple[int, int]:
        """(RVA, tamaño) de una entrada de la tabla de directorios; (0, 0) si no existe."""
        count_offset = self.optional_offset + (108 if self.is_pe32_plus else 92)
        if index >= struct.unpack_from("<I", self.data, count_offset)[0]:
            return 0, 0
        return struct.unpack_from("<II", self.data, self.data_directory_offset + 8 * index)

    def rva_to_offset(self, rva: int) -> int | None:
        for section in self.sections:
            extent = max(section.virtual_size, section.raw_size)
            if section.virtual_address <= rva < section.virtual_address + extent:
                offset = section.raw_pointer + (rva - section.virtual_address)
                return offset if offset < len(self.data) else None
        return rva if rva < self.size_of_headers else None

    def read_cstring(self, offset: int) -> str:
        end = self.data.index(b"\0", offset)
        return self.data[offset:end].decode("ascii", "replace")


def parse_pe(data: bytes) -> PE:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise SlimError("no es un archivo PE (falta la cabecera MZ)")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise SlimError("no es un archivo PE (falta la firma PE)")
    coff = pe_offset + 4
    _machine, section_count, _stamp, symbol_pointer, symbol_count, optional_size, _chars = struct.unpack_from(
        "<HHIIIHH", data, coff
    )
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic not in (0x10B, 0x20B):
        raise SlimError(f"cabecera opcional desconocida (0x{magic:x})")
    pe = PE(
        data=data,
        coff_offset=coff,
        optional_offset=optional,
        is_pe32_plus=magic == 0x20B,
        section_table_offset=optional + optional_size,
        symbol_pointer=symbol_pointer,
        symbol_count=symbol_count,
    )
    string_table = symbol_pointer + symbol_count * COFF_SYMBOL_SIZE if symbol_pointer else 0
    for index in range(section_count):
        base = pe.section_table_offset + 40 * index
        if base + 40 > len(data):
            raise SlimError("tabla de secciones truncada")
        raw_name = data[base : base + 8].rstrip(b"\0").decode("ascii", "replace")
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
        if raw_name.startswith("/") and raw_name[1:].isdigit() and string_table:
            raw_name = pe.read_cstring(string_table + int(raw_name[1:]))  # nombre largo (.debug_info...)
        pe.sections.append(Section(index, raw_name, virtual_size, virtual_address, raw_size, raw_pointer))
    return pe


def imported_dlls(pe: PE) -> list[str]:
    """Nombres de las DLL que el archivo importa (carga normal y de carga diferida)."""
    names: list[str] = []
    rva, size = pe.data_directory(DIR_IMPORT)
    offset = pe.rva_to_offset(rva) if rva and size else None
    while offset is not None and offset + 20 <= len(pe.data):
        _orig, _stamp, _forward, name_rva, first_thunk = struct.unpack_from("<IIIII", pe.data, offset)
        if not (name_rva or first_thunk):
            break
        name_offset = pe.rva_to_offset(name_rva)
        if name_offset is not None:
            names.append(pe.read_cstring(name_offset))
        offset += 20
    rva, size = pe.data_directory(DIR_DELAY_IMPORT)
    offset = pe.rva_to_offset(rva) if rva and size else None
    while offset is not None and offset + 32 <= len(pe.data):
        attributes, name_rva = struct.unpack_from("<II", pe.data, offset)
        if not name_rva:
            break
        if not attributes & 1:  # dirección virtual en vez de RVA (solo en imágenes antiguas de 32 bits)
            name_rva -= struct.unpack_from("<I", pe.data, pe.optional_offset + 28)[0]
        name_offset = pe.rva_to_offset(name_rva)
        if name_offset is not None:
            names.append(pe.read_cstring(name_offset))
        offset += 32
    return names


# --- Paso 1: borrar las DLL que tesseract.exe no necesita ---------------------------------------------
def find_reachable(folder: Path, entry: str = "tesseract.exe") -> set[str]:
    """Archivos de `folder` (en minúsculas) que `entry` necesita, siguiendo importaciones de DLL. Las DLL
    del sistema de Windows (kernel32...) no están en la carpeta y se ignoran."""
    available = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    if entry.lower() not in available:
        raise SlimError(f"no existe {entry} en {folder}")
    reachable: set[str] = set()
    pending = [entry.lower()]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for dll in imported_dlls(parse_pe(available[name].read_bytes())):
            if dll.lower() in available:
                pending.append(dll.lower())
    return reachable


def prune_unreachable(folder: Path, entry: str = "tesseract.exe") -> list[tuple[Path, int]]:
    """Borra las `.dll` que `entry` no necesita. Devuelve (archivo, tamaño) de cada una."""
    reachable = find_reachable(folder, entry)
    removed = []
    for path in sorted(folder.glob("*")):
        if path.is_file() and path.suffix.lower() == ".dll" and path.name.lower() not in reachable:
            size = path.stat().st_size
            os.chmod(path, 0o666)
            path.unlink()
            removed.append((path, size))
    return removed


# --- Paso 2: quitar la información de depuración -----------------------------------------------------
def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment if alignment else value


def strip_debug(data: bytes) -> bytes | None:
    """Devuelve el archivo PE sin secciones `.debug_*` ni tabla de símbolos, o `None` si no hay nada que
    quitar o no es seguro hacerlo (secciones de depuración no finales, datos extra desconocidos...).
    Las secciones que se conservan quedan byte a byte idénticas."""
    pe = parse_pe(data)
    debug = [s for s in pe.sections if s.name.startswith(".debug")]
    if not debug:
        return None
    kept = [s for s in pe.sections if s not in debug]
    if not kept:
        return None
    # Solo si son las últimas del archivo, de la tabla de secciones y de la memoria: así el resto no se mueve.
    if [s.index for s in debug] != list(range(len(kept), len(pe.sections))):
        return None
    last_kept_raw = max((s.raw_pointer + s.raw_size for s in kept if s.raw_size), default=0)
    last_kept_va = max(s.virtual_address + (s.virtual_size or s.raw_size) for s in kept)
    if any(s.raw_pointer < last_kept_raw or s.virtual_address < last_kept_va for s in debug if s.raw_size):
        return None
    # Un archivo firmado (Authenticode) lleva su certificado al final. Quitar la depuración invalida esa firma;
    # una firma rota es peor que ninguna (algunos antivirus la marcan), así que se elimina junto con los datos.
    cert_offset, cert_size = pe.data_directory(DIR_SECURITY)  # aquí "RVA" es un desplazamiento en el archivo
    tail_end = len(data)
    if cert_offset:
        if cert_offset + cert_size != len(data):
            return None
        tail_end = cert_offset
    truncate_at = last_kept_raw
    if pe.symbol_pointer:
        if pe.symbol_pointer < truncate_at:
            return None
        string_table = pe.symbol_pointer + pe.symbol_count * COFF_SYMBOL_SIZE
        if string_table + 4 > tail_end:
            return None
        string_table_end = string_table + struct.unpack_from("<I", data, string_table)[0]
        padding = 7 if cert_offset else 0  # el certificado empieza en un múltiplo de 8
        if not tail_end - padding <= string_table_end <= tail_end:
            return None  # hay datos después de la tabla de símbolos: no se sabe qué son
    elif tail_end != max(s.raw_pointer + s.raw_size for s in pe.sections if s.raw_size):
        return None  # datos extra al final del archivo

    out = bytearray(data[:truncate_at])
    struct.pack_into("<H", out, pe.coff_offset + 2, len(kept))  # NumberOfSections
    struct.pack_into("<II", out, pe.coff_offset + 8, 0, 0)  # sin tabla de símbolos
    characteristics = struct.unpack_from("<H", out, pe.coff_offset + 18)[0]
    characteristics |= IMAGE_FILE_LINE_NUMS_STRIPPED | IMAGE_FILE_LOCAL_SYMS_STRIPPED | IMAGE_FILE_DEBUG_STRIPPED
    struct.pack_into("<H", out, pe.coff_offset + 18, characteristics)
    for s in debug:  # las entradas de la tabla que sobran quedan en cero
        base = pe.section_table_offset + 40 * s.index
        out[base : base + 40] = bytes(40)
    struct.pack_into("<I", out, pe.optional_offset + 56, _align_up(last_kept_va, pe.section_alignment))  # SizeOfImage
    struct.pack_into("<I", out, pe.optional_offset + 64, 0)  # CheckSum (opcional en DLL/EXE de usuario)
    if cert_offset:
        struct.pack_into("<II", out, pe.data_directory_offset + 8 * DIR_SECURITY, 0, 0)
    debug_rva, _debug_size = pe.data_directory(DIR_DEBUG)
    if debug_rva and any(s.virtual_address <= debug_rva < s.virtual_address + s.virtual_size for s in debug):
        struct.pack_into("<II", out, pe.data_directory_offset + 8 * DIR_DEBUG, 0, 0)
    return bytes(out)


def verify_stripped(before: bytes, after: bytes) -> None:
    """Comprueba que quitar la depuración no alteró nada de lo que se ejecuta."""
    old, new = parse_pe(before), parse_pe(after)
    keep = [s for s in old.sections if not s.name.startswith(".debug")]
    if [(s.name, s.virtual_address, s.virtual_size, s.raw_pointer, s.raw_size) for s in keep] != [
        (s.name, s.virtual_address, s.virtual_size, s.raw_pointer, s.raw_size) for s in new.sections
    ]:
        raise SlimError("las secciones conservadas cambiaron")
    start = old.size_of_headers
    end = max(s.raw_pointer + s.raw_size for s in keep if s.raw_size)
    if before[start:end] != after[start:end]:
        raise SlimError("el contenido de las secciones conservadas cambió")
    for index in (DIR_EXPORT, DIR_IMPORT, DIR_DELAY_IMPORT):
        if old.data_directory(index) != new.data_directory(index):
            raise SlimError(f"cambió el directorio de datos {index}")
    if imported_dlls(old) != imported_dlls(new):
        raise SlimError("cambiaron las DLL importadas")
    if new.symbol_pointer or new.symbol_count:
        raise SlimError("quedó una tabla de símbolos")


def strip_folder(folder: Path) -> list[tuple[Path, int, int]]:
    """Quita la depuración de cada .exe/.dll de `folder`. Devuelve (archivo, tamaño antes, tamaño después)."""
    results = []
    for path in sorted(folder.glob("*")):
        if not path.is_file() or path.suffix.lower() not in (".exe", ".dll"):
            continue
        before = path.read_bytes()
        after = strip_debug(before)
        if after is None:
            continue
        verify_stripped(before, after)
        temporary = path.with_name(path.name + ".slim")
        temporary.write_bytes(after)
        os.chmod(path, 0o666)
        os.replace(temporary, path)
        results.append((path, len(before), len(after)))
    return results


# --- Prueba de funcionamiento -------------------------------------------------------------------------
Runner = Callable[..., str]


def run_process(args: Sequence[str], env: dict[str, str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=180,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SlimError(f"no se pudo ejecutar {args[0]}: {exc}") from exc
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise SlimError(f"{' '.join(str(a) for a in args)} terminó con código {result.returncode}:\n{output.strip()}")
    return output


def selftest(folder: Path, runner_prefix: Sequence[str] = (), run: Runner = run_process) -> list[str]:
    """Arranca el Tesseract de `folder` y le hace leer una imagen. Devuelve las líneas del informe."""
    exe = folder / "tesseract.exe"
    tessdata = folder / "tessdata"
    env = {**os.environ, "TESSDATA_PREFIX": str(tessdata)}
    report = []
    langs = run([*runner_prefix, str(exe), "--list-langs"], env)
    missing = [lang for lang in REQUIRED_LANGS if lang not in langs.split()]
    if missing:
        raise SlimError("Tesseract no ve los idiomas: " + ", ".join(missing))
    report.append("idiomas spa y eng: OK")
    try:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.load_default(size=56)
    except (ImportError, TypeError):
        report.append("lectura de imagen: omitida (falta Pillow)")
        return report
    image = Image.new("RGB", (900, 140), "white")
    ImageDraw.Draw(image).text((30, 30), "FACTURA 12345", fill="black", font=font)
    with tempfile.TemporaryDirectory() as tmp:
        image.save(Path(tmp) / "prueba.png")
        # La imagen va por nombre relativo (cwd = carpeta temporal): funciona igual con rutas de Windows o de Wine.
        command = [*runner_prefix, str(exe), "prueba.png", "stdout", "-l", "spa+eng", "--tessdata-dir", str(tessdata)]
        text = run(command, env, Path(tmp))
    if "12345" not in "".join(ch for ch in text if ch.isdigit()):
        raise SlimError(f"la prueba de lectura no reconoció '12345' (leyó: {text.strip()!r})")
    report.append("lectura de imagen: OK")
    return report


# --- Línea de comandos -----------------------------------------------------------------------------------
def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adelgaza vendor/tesseract (Windows) antes de empaquetarlo.")
    parser.add_argument("folder", type=Path, help="carpeta con tesseract.exe, sus DLL y tessdata")
    parser.add_argument("--no-prune", action="store_true", help="no borrar las DLL que tesseract.exe no necesita")
    parser.add_argument("--no-strip", action="store_true", help="no quitar la información de depuración")
    parser.add_argument("--selftest", action="store_true", help="al terminar, arrancar tesseract y leer una imagen")
    parser.add_argument("--runner", default="", help="comando que antecede a tesseract.exe (p. ej. wine64)")
    args = parser.parse_args(argv)
    folder: Path = args.folder
    try:
        before = folder_size(folder)
        if not args.no_prune:
            removed = prune_unreachable(folder)
            freed = sum(size for _path, size in removed)
            print(f"DLL que tesseract.exe no necesita: {len(removed)} borradas ({freed / 1e6:.1f} MB)")
        if not args.no_strip:
            stripped = strip_folder(folder)
            for path, old, new in stripped:
                print(f"  {path.name}: {old / 1e6:.1f} MB -> {new / 1e6:.1f} MB (sin depuración)")
        after = folder_size(folder)
        print(f"Tamaño de {folder}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
        if args.selftest:
            for line in selftest(folder, shlex.split(args.runner)):
                print(f"  {line}")
    except (SlimError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):  # una consola con otra página de códigos no debe romper el script
        _stream.reconfigure(errors="replace")
    sys.exit(main())
