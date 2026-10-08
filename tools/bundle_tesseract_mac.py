#!/usr/bin/env python3
"""Mete Tesseract OCR dentro de "FileSync Pro.app" (macOS) para que la lectura de
facturas funcione en un Mac que no lo tenga instalado.

Homebrew instala Tesseract como un ejecutable que depende de decenas de bibliotecas
(`.dylib`) con rutas absolutas (`/opt/homebrew/opt/leptonica/lib/...`). Copiar solo el
ejecutable no sirve: en otro Mac esas rutas no existen. Este script:

1. Encuentra Tesseract (Homebrew/MacPorts) y calcula TODAS las bibliotecas que necesita
   (recursivamente, ignorando las del sistema: `/usr/lib` y `/System`).
2. Las copia a `Contents/Frameworks/tesseract/` (ejecutable) y `.../lib/` (bibliotecas) y
   reescribe sus rutas internas para que apunten a esa carpeta (`@loader_path`).
3. Deja los idiomas `spa` y `eng` en `Contents/Resources/tesseract/tessdata` (los descarga de
   `tessdata_fast` si faltan) y los enlaza desde Frameworks, igual que hace PyInstaller
   con los datos: en macOS el código va en Frameworks y los datos en Resources.
4. Firma cada archivo (tras `install_name_tool` la firma queda invalidada y en Apple Silicon
   un binario sin firma no arranca) y vuelve a firmar el .app completo.
5. Comprueba que no quedó ninguna dependencia fuera del paquete y que el Tesseract
   empaquetado arranca solo (sin Homebrew ni PATH) y lee un texto de prueba.

La app lo encuentra en `Organizador.configure_ocr()` (`sys._MEIPASS/tesseract/tesseract`).

Uso (solo en un Mac; lo llama `build_mac.sh`):
    python tools/bundle_tesseract_mac.py --check --expect-arch arm64
    python tools/bundle_tesseract_mac.py --app "dist/FileSync Pro.app"
    python tools/bundle_tesseract_mac.py --verify "dist/FileSync Pro.app"

Solo usa la biblioteca estándar (la prueba de lectura usa Pillow si está instalado).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_LANGS = ("spa", "eng")
# Un .traineddata válido pesa varios MB; algo menor es una descarga cortada o una página de error.
MIN_TRAINEDDATA_BYTES = 500_000
TESSDATA_FAST_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
TESSERACT_LICENSE_URL = "https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE"

TESSERACT_CANDIDATES = (
    "/opt/homebrew/bin/tesseract",  # Homebrew en Apple Silicon
    "/usr/local/bin/tesseract",  # Homebrew en Mac Intel
    "/opt/local/bin/tesseract",  # MacPorts
)
TESSDATA_CANDIDATES = (
    "/opt/homebrew/share/tessdata",
    "/usr/local/share/tessdata",
    "/opt/local/share/tessdata",
)
LIB_SEARCH_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")

EXE_KEY = "<tesseract>"  # clave del ejecutable en el plan (las bibliotecas se identifican por su nombre)
APP_TESSERACT_DIR = "tesseract"

Runner = Callable[..., str]
Fetcher = Callable[[str, Path], None]


class BundleError(Exception):
    """Fallo esperado (falta algo, una dependencia no se encuentra...) con un mensaje para el usuario."""


# --- Ejecución de comandos ---------------------------------------------------------------
def run_command(args: Sequence[str], env: Mapping[str, str] | None = None) -> str:
    """Ejecuta un comando y devuelve su salida (stdout + stderr). Falla si el código de salida no es 0."""
    try:
        result = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            check=False,
            env=None if env is None else dict(env),
        )
    except OSError as exc:
        raise BundleError(f"No se pudo ejecutar {args[0]}: {exc}") from exc
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise BundleError(f"Falló el comando: {' '.join(str(a) for a in args)}\n{output.strip()}")
    return output


def fetch_with_curl(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_command(["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20", "-o", str(dest), url])


# --- Lectura de la salida de otool -------------------------------------------------------
_DEP_LINE = re.compile(r"^\s+(\S.*?) \(compatibility version")
_OFFSET_SUFFIX = re.compile(r"\s*\(offset \d+\)\s*$")


def parse_otool_deps(output: str) -> list[str]:
    """Rutas de las bibliotecas que lista `otool -L` (en un .dylib la primera es su propio nombre)."""
    deps = []
    for line in output.splitlines():
        match = _DEP_LINE.match(line)
        if match:
            deps.append(match.group(1))
    return deps


def parse_otool_id(output: str) -> str | None:
    """Nombre de instalación (`LC_ID_DYLIB`) que imprime `otool -D`; None si no tiene."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[1] if len(lines) > 1 else None


def parse_otool_rpaths(output: str) -> list[str]:
    """Rutas de búsqueda (`LC_RPATH`) que imprime `otool -l`."""
    rpaths = []
    expecting_path = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "cmd LC_RPATH":
            expecting_path = True
        elif expecting_path and stripped.startswith("path "):
            rpaths.append(_OFFSET_SUFFIX.sub("", stripped[len("path ") :]))
            expecting_path = False
    return rpaths


def is_system_dep(path: str) -> bool:
    """Las bibliotecas de macOS están en todos los Macs: no se copian."""
    return path.startswith(("/usr/lib/", "/System/Library/"))


# --- Cálculo de dependencias ---------------------------------------------------------------
@dataclass
class DependencyPlan:
    # nombre con el que se guarda en lib/  ->  archivo real de origen
    libs: dict[str, Path] = field(default_factory=dict)
    # archivo (EXE_KEY o nombre en lib/)  ->  {ruta original de la dependencia: nombre en lib/}
    references: dict[str, dict[str, str]] = field(default_factory=dict)
    # archivo  ->  sus LC_RPATH originales (se eliminan al copiar)
    rpaths: dict[str, list[str]] = field(default_factory=dict)


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _expand_placeholders(text: str, referrer: Path, main_exe: Path) -> Path:
    if text.startswith("@loader_path"):
        text = str(referrer.parent) + text[len("@loader_path") :]
    elif text.startswith("@executable_path"):
        text = str(main_exe.parent) + text[len("@executable_path") :]
    return Path(os.path.normpath(text))


def resolve_dependency(
    dep: str, referrer: Path, main_exe: Path, rpaths: Sequence[str], search_dirs: Sequence[Path] = ()
) -> Path | None:
    """Ubica en disco la biblioteca `dep` pedida por `referrer` (sigue las reglas de dyld:
    `@rpath`, `@loader_path`, `@executable_path` o ruta absoluta)."""
    leaf = dep.rsplit("/", 1)[-1]
    candidates: list[Path] = []
    if dep.startswith("@rpath/"):
        rest = dep[len("@rpath/") :]
        candidates.extend(_expand_placeholders(rpath, referrer, main_exe) / rest for rpath in rpaths)
    elif dep.startswith(("@loader_path/", "@executable_path/")):
        candidates.append(_expand_placeholders(dep, referrer, main_exe))
    else:
        candidates.append(Path(dep))
    candidates.extend(directory / leaf for directory in search_dirs)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def collect_dependencies(exe: Path, runner: Runner, search_dirs: Sequence[Path] = ()) -> DependencyPlan:
    """Recorre las dependencias de `exe` (y las de sus dependencias...) que no son del sistema."""
    plan = DependencyPlan()
    exe = exe.resolve()
    queue: list[tuple[str, Path]] = [(EXE_KEY, exe)]
    while queue:
        key, source = queue.pop(0)
        rpaths = _unique(parse_otool_rpaths(runner(["otool", "-l", str(source)])))
        own_id = parse_otool_id(runner(["otool", "-D", str(source)])) if key != EXE_KEY else None
        mapping: dict[str, str] = {}
        for dep in parse_otool_deps(runner(["otool", "-L", str(source)])):
            if dep == own_id or is_system_dep(dep):
                continue
            found = resolve_dependency(dep, source, exe, rpaths, search_dirs)
            if found is None:
                raise BundleError(
                    f"No se encontró la biblioteca {dep}, que necesita {source.name}. "
                    "Reinstala Tesseract (brew reinstall tesseract) y vuelve a intentar."
                )
            leaf = dep.rsplit("/", 1)[-1]
            if plan.libs.get(leaf, found) != found:
                raise BundleError(f"Dos bibliotecas distintas se llaman {leaf}: {plan.libs[leaf]} y {found}.")
            mapping[dep] = leaf
            if leaf not in plan.libs:
                plan.libs[leaf] = found
                queue.append((leaf, found))
        plan.references[key] = mapping
        plan.rpaths[key] = rpaths
    return plan


# --- Reescritura de rutas y firma -----------------------------------------------------------
def build_rewrite_args(
    target: Path, *, new_id: str | None, changes: Mapping[str, str], delete_rpaths: Sequence[str]
) -> list[str]:
    """Comando `install_name_tool` que deja `target` apuntando a las copias (lista vacía si no hay nada que cambiar)."""
    args = ["install_name_tool"]
    if new_id:
        args += ["-id", new_id]
    for old, new in changes.items():
        args += ["-change", old, new]
    for rpath in delete_rpaths:
        args += ["-delete_rpath", rpath]
    return [*args, str(target)] if len(args) > 1 else []


def codesign_args(target: Path, identity: str | None, entitlements: str | None = None, deep: bool = False) -> list[str]:
    """Mismos parámetros que usa PyInstaller para firmar (ad hoc si no hay identidad)."""
    args = ["/usr/bin/codesign", "-s", identity or "-", "--force", "--all-architectures", "--timestamp"]
    if identity:
        args.append("--options=runtime")  # runtime reforzado: lo exige la notarización
    if entitlements:
        args += ["--entitlements", entitlements]
    if deep:
        args.append("--deep")
    return [*args, str(target)]


# --- Búsqueda de Tesseract y de los idiomas -------------------------------------------------
def find_tesseract(explicit: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    wanted = [explicit, env.get("TESSERACT_BIN"), shutil.which("tesseract"), *TESSERACT_CANDIDATES]
    for candidate in wanted:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise BundleError(
        "No se encontró Tesseract en este Mac. Instálalo con:  brew install tesseract\n"
        "(o indica su ruta con --tesseract / la variable TESSERACT_BIN, o usa FILESYNC_SKIP_OCR=1 "
        "para compilar la app sin OCR)."
    )


def find_tessdata_dirs(exe: Path, env: Mapping[str, str] | None = None) -> list[Path]:
    """Carpetas `tessdata` candidatas, la más probable primero (Homebrew deja los idiomas de
    `tesseract-lang` en `<prefijo>/share/tessdata`, no dentro de la carpeta de la versión)."""
    env = os.environ if env is None else env
    candidates: list[Path] = []
    prefix = env.get("TESSDATA_PREFIX")
    if prefix:
        candidates += [Path(prefix), Path(prefix) / "tessdata"]
    candidates += [*(Path(p) for p in TESSDATA_CANDIDATES), exe.parent.parent / "share" / "tessdata"]
    return [c for c in dict.fromkeys(candidates) if c.is_dir() and any(c.glob("*.traineddata"))]


def assemble_tessdata(dest: Path, sources: Sequence[Path], fetch: Fetcher, log: Callable[[str], None] = print) -> None:
    """Deja en `dest` los idiomas requeridos y las carpetas `configs`/`tessconfigs`."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("configs", "tessconfigs"):
        for source in sources:
            if (source / name).is_dir():
                shutil.copytree(source / name, dest / name, dirs_exist_ok=True)
                break
    for lang in REQUIRED_LANGS:
        target = dest / f"{lang}.traineddata"
        existing = next(
            (
                s / target.name
                for s in sources
                if (s / target.name).is_file() and (s / target.name).stat().st_size >= MIN_TRAINEDDATA_BYTES
            ),
            None,
        )
        if existing is not None:
            shutil.copyfile(existing, target)
        else:
            log(f"  Descargando el idioma '{lang}' (tessdata_fast)...")
            fetch(TESSDATA_FAST_URL.format(lang=lang), target)
        if not target.is_file() or target.stat().st_size < MIN_TRAINEDDATA_BYTES:
            raise BundleError(f"El idioma '{lang}' no se pudo obtener o quedó incompleto ({target}).")


# --- Aplicación (.app) ------------------------------------------------------------------------
def app_main_executable(app: Path) -> Path:
    name = "FileSync Pro"
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            name = plistlib.load(handle).get("CFBundleExecutable", name)
    except (OSError, plistlib.InvalidFileException):
        pass
    return app / "Contents" / "MacOS" / name


def binary_archs(path: Path, runner: Runner) -> list[str]:
    return runner(["lipo", "-archs", str(path)]).split()


def check_architecture(tesseract_archs: Sequence[str], app_archs: Sequence[str]) -> str:
    """Devuelve la arquitectura de destino; falla si Tesseract no sirve para la app."""
    if len(app_archs) != 1:
        raise BundleError(
            f"La app es {'/'.join(app_archs)} (universal2) y Tesseract existe para una sola arquitectura por Mac. "
            "Compila arm64 y x86_64 por separado (GitHub Actions lo hace) o usa FILESYNC_SKIP_OCR=1."
        )
    arch = app_archs[0]
    if arch not in tesseract_archs:
        raise BundleError(
            f"La app es {arch} pero este Tesseract es {'/'.join(tesseract_archs)}. En un Mac Apple Silicon usa el "
            "Homebrew de /opt/homebrew; para una app Intel compila en un Mac Intel (o con Rosetta y el Homebrew "
            "de /usr/local)."
        )
    return arch


def _copy_binary(source: Path, dest: Path, arch: str, runner: Runner) -> None:
    archs = binary_archs(source, runner)
    if arch not in archs:
        raise BundleError(f"{source.name} es {'/'.join(archs)} y la app es {arch}.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(archs) > 1:
        runner(["lipo", str(source), "-thin", arch, "-output", str(dest)])
    else:
        shutil.copyfile(source, dest)
    os.chmod(dest, 0o755)  # Homebrew deja las bibliotecas de solo lectura; hay que poder reescribirlas


def bundle_tesseract(
    app: Path,
    exe: Path,
    *,
    identity: str | None = None,
    entitlements: str | None = None,
    notices: Path | None = None,
    runner: Runner = run_command,
    fetch: Fetcher = fetch_with_curl,
    tessdata_dirs: Sequence[Path] | None = None,
    log: Callable[[str], None] = print,
) -> None:
    """Instala Tesseract dentro del .app ya generado por PyInstaller y lo firma."""
    if not app.is_dir():
        raise BundleError(f"No existe {app}. Ejecuta primero PyInstaller.")
    exe = exe.resolve()
    arch = check_architecture(binary_archs(exe, runner), binary_archs(app_main_executable(app), runner))

    log(f"Tesseract: {exe} ({arch})")
    search_dirs = [exe.parent.parent / "lib", *(Path(p) for p in LIB_SEARCH_DIRS)]
    plan = collect_dependencies(exe, runner, search_dirs)
    log(f"  {len(plan.libs)} bibliotecas de terceros para incluir.")

    code_dir = app / "Contents" / "Frameworks" / APP_TESSERACT_DIR
    data_dir = app / "Contents" / "Resources" / APP_TESSERACT_DIR
    for old in (code_dir, data_dir):
        shutil.rmtree(old, ignore_errors=True)
    lib_dir = code_dir / "lib"
    _copy_binary(exe, code_dir / "tesseract", arch, runner)
    for leaf, source in plan.libs.items():
        _copy_binary(source, lib_dir / leaf, arch, runner)

    # Rutas internas: el ejecutable busca en ./lib y cada biblioteca, junto a sí misma.
    installed = [(EXE_KEY, code_dir / "tesseract", None, "@loader_path/lib/")]
    installed += [(leaf, lib_dir / leaf, f"@loader_path/{leaf}", "@loader_path/") for leaf in plan.libs]
    for key, path, new_id, prefix in installed:
        changes = {old: prefix + leaf for old, leaf in plan.references[key].items()}
        args = build_rewrite_args(path, new_id=new_id, changes=changes, delete_rpaths=plan.rpaths[key])
        if args:
            runner(args)

    sources = list(tessdata_dirs) if tessdata_dirs is not None else find_tessdata_dirs(exe)
    assemble_tessdata(data_dir / "tessdata", sources, fetch, log)
    # Mismo esquema que PyInstaller: los datos viven en Resources y se enlazan desde Frameworks.
    (code_dir / "tessdata").symlink_to(Path("..") / ".." / "Resources" / APP_TESSERACT_DIR / "tessdata")
    try:
        fetch(TESSERACT_LICENSE_URL, data_dir / "LICENSE-tesseract.txt")
    except BundleError:
        log("  AVISO: no se pudo descargar la licencia de Tesseract; agrégala a mano si distribuyes la app.")
    if notices is not None and notices.is_file():
        shutil.copyfile(notices, app / "Contents" / "Resources" / notices.name)

    log("Firmando...")
    for _key, path, _new_id, _prefix in reversed(installed):  # primero las bibliotecas, al final el ejecutable
        runner(codesign_args(path, identity, entitlements))
    runner(codesign_args(app, identity, entitlements, deep=True))
    try:
        runner(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    except BundleError as exc:
        log(f"  AVISO: la firma del paquete no se pudo verificar:\n{exc}")


# --- Verificación --------------------------------------------------------------------------------
def _ocr_smoke_test(exe: Path, tessdata: Path, runner: Runner, env: Mapping[str, str]) -> str | None:
    """Lee una imagen con dígitos usando el Tesseract empaquetado. None = no se pudo preparar la prueba
    (sin Pillow); en otro caso devuelve el texto leído."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.load_default(size=56)
    except (ImportError, TypeError):
        return None
    image = Image.new("RGB", (900, 140), "white")
    ImageDraw.Draw(image).text((30, 30), "FACTURA 12345", fill="black", font=font)
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "prueba.png"
        image.save(png)
        return runner([str(exe), str(png), "stdout", "-l", "spa", "--tessdata-dir", str(tessdata)], env=env)


def verify_bundle(app: Path, runner: Runner = run_command, *, ocr_test: bool = True) -> list[str]:
    """Lista de problemas del Tesseract empaquetado (vacía = todo bien)."""
    code_dir = app / "Contents" / "Frameworks" / APP_TESSERACT_DIR
    exe = code_dir / "tesseract"
    if not exe.is_file():
        return [f"Falta {exe}"]
    problems: list[str] = []
    for path in [exe, *sorted((code_dir / "lib").glob("*.dylib"))]:
        own_id = parse_otool_id(runner(["otool", "-D", str(path)])) if path != exe else None
        for dep in parse_otool_deps(runner(["otool", "-L", str(path)])):
            if dep == own_id or is_system_dep(dep):
                continue
            if dep.startswith("@loader_path/"):
                if not Path(os.path.normpath(path.parent / dep[len("@loader_path/") :])).is_file():
                    problems.append(f"{path.name} pide {dep}, que no está en el paquete.")
            else:
                problems.append(f"{path.name} depende de {dep}, fuera del paquete (no existirá en otro Mac).")
    tessdata = code_dir / "tessdata"
    problems += [
        f"Falta el idioma {lang} en {tessdata}"
        for lang in REQUIRED_LANGS
        if not (tessdata / f"{lang}.traineddata").is_file()
    ]
    if problems:
        return problems

    # Entorno mínimo: sin Homebrew en el PATH ni variables de dyld, como en un Mac sin Tesseract.
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "TESSDATA_PREFIX": str(tessdata)}
    try:
        langs = runner([str(exe), "--list-langs"], env=env)
    except BundleError as exc:
        return [f"El Tesseract empaquetado no arranca:\n{exc}"]
    problems += [f"El Tesseract empaquetado no lista el idioma {lang}." for lang in REQUIRED_LANGS if lang not in langs]
    if problems or not ocr_test:
        return problems
    try:
        text = _ocr_smoke_test(exe, tessdata, runner, env)
    except BundleError as exc:
        return [f"El Tesseract empaquetado falló al leer una imagen:\n{exc}"]
    if text is not None and "12345" not in "".join(ch for ch in text if ch.isdigit()):
        problems.append(f"La prueba de lectura no reconoció '12345' (leyó: {text.strip()!r}).")
    return problems


# --- Línea de comandos ------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Empaqueta Tesseract OCR dentro de FileSync Pro.app (macOS).")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="solo comprobar que Tesseract se puede empaquetar")
    mode.add_argument("--app", type=Path, help="ruta del .app al que se añade Tesseract")
    mode.add_argument("--verify", type=Path, help="verificar un .app que ya lleva Tesseract")
    parser.add_argument("--tesseract", help="ruta de tesseract (por defecto, PATH o Homebrew)")
    parser.add_argument("--expect-arch", help="con --check: arquitectura de la app (arm64, x86_64 o universal2)")
    parser.add_argument("--identity", default=os.environ.get("FILESYNC_CODESIGN_IDENTITY", ""))
    parser.add_argument("--entitlements", default=os.environ.get("FILESYNC_ENTITLEMENTS", ""))
    parser.add_argument("--notices", type=Path, default=Path("THIRD_PARTY_NOTICES.md"))
    parser.add_argument("--no-ocr-test", action="store_true", help="no leer la imagen de prueba al verificar")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.platform != "darwin":
        print("ERROR: este script solo funciona en macOS.", file=sys.stderr)
        return 1
    try:
        if args.verify:
            problems = verify_bundle(args.verify, ocr_test=not args.no_ocr_test)
        else:
            exe = find_tesseract(args.tesseract)
            if args.check:
                archs = binary_archs(exe, run_command)
                if args.expect_arch:
                    app_archs = ["arm64", "x86_64"] if args.expect_arch == "universal2" else [args.expect_arch]
                    check_architecture(archs, app_archs)
                plan = collect_dependencies(exe, run_command, [exe.parent.parent / "lib", *map(Path, LIB_SEARCH_DIRS)])
                print(f"Tesseract {exe} ({'/'.join(archs)}): {len(plan.libs)} bibliotecas de terceros. OK")
                return 0
            bundle_tesseract(
                args.app,
                exe,
                identity=args.identity or None,
                entitlements=args.entitlements or None,
                notices=args.notices,
            )
            problems = verify_bundle(args.app, ocr_test=not args.no_ocr_test)
    except BundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("ERROR: el Tesseract empaquetado tiene problemas:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Tesseract empaquetado y verificado: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
