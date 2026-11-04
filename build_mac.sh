#!/usr/bin/env bash
# Empaqueta FileSync Pro como "FileSync Pro.app" (y .zip / .dmg) para macOS.
# Es el equivalente de build.ps1 en Windows. Solo corre en un Mac.
#
# Uso:
#   bash build_mac.sh
#
# Variables opcionales:
#   PYTHON_BIN                   Python a usar (por defecto python3). Recomendado: el de python.org.
#   VENV_PATH                    Entorno virtual (por defecto .venv_mac).
#   FILESYNC_MAC_ARCH            universal2 | arm64 | x86_64 (por defecto, la del Mac que compila).
#   FILESYNC_CODESIGN_IDENTITY   "Developer ID Application: Nombre (TEAMID)" para firmar.
#   FILESYNC_ENTITLEMENTS        .plist de entitlements (por defecto packaging/entitlements.plist al firmar).
#   FILESYNC_BUNDLE_ID           Identificador del paquete (por defecto com.sca.filesyncpro).
#   APPLE_ID, APPLE_TEAM_ID,     Con FILESYNC_CODESIGN_IDENTITY, notariza el .dmg con Apple y le "grapa"
#   APPLE_APP_PASSWORD           el resultado (a la app y al .dmg). APPLE_APP_PASSWORD es una contraseña
#                                de app de https://account.apple.com (no la de tu Apple ID).
#   FILESYNC_SKIP_OCR            Con valor 1, compila la app SIN Tesseract (el OCR dependera de que el Mac lo tenga).
#   TESSERACT_BIN                Ruta de tesseract si no esta en el PATH ni en Homebrew.
#
# OCR: por defecto se incluye Tesseract (con los idiomas spa y eng) dentro de la app, para que leer
# facturas funcione en un Mac sin Homebrew. Requiere tenerlo instalado en el Mac que compila:
#   brew install tesseract
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_PATH="${VENV_PATH:-.venv_mac}"
APP_PATH="dist/FileSync Pro.app"
ZIP_PATH="dist/FileSync-Pro-macOS.zip"
DMG_PATH="dist/FileSync-Pro-macOS.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Este script solo funciona en macOS (PyInstaller no compila para otro sistema)." >&2
    exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "No se encontro $PYTHON_BIN. Instala Python 3.13 desde https://www.python.org/downloads/macos/ y vuelve a ejecutar." >&2
    exit 1
fi

# Tkinter debe ser Tk 8.6 o superior. El Python que trae Apple (Xcode) usa Tk 8.5 y abre la
# ventana en blanco o falla; el de python.org (o Homebrew con python-tk) trae Tk 8.6.
"$PYTHON_BIN" - <<'PY'
import sys

try:
    import tkinter
except ImportError:
    sys.exit("Este Python no tiene tkinter. Usa el instalador de python.org o: brew install python-tk@3.13")
if tkinter.TkVersion < 8.6:
    sys.exit(f"Este Python usa Tk {tkinter.TkVersion} (se necesita 8.6 o superior). Usa el instalador de python.org.")
print(f"Python {sys.version.split()[0]} con Tk {tkinter.TkVersion}: OK")
PY

if [[ ! -f desktop_runtime_config.json ]]; then
    echo "Falta desktop_runtime_config.json (lo genera build.ps1 en Windows). Copialo a esta carpeta y vuelve a ejecutar." >&2
    exit 1
fi
if ! grep -Eq '"FILESYNC_PRO_LICENSE_SIGNING_SECRET": *"[^"]+' desktop_runtime_config.json; then
    echo "AVISO: FILESYNC_PRO_LICENSE_SIGNING_SECRET esta vacio en desktop_runtime_config.json. La app no podra activar codigos de licencia ni la demo." >&2
fi
if [[ -f .env ]] && grep -Eq '^[[:space:]]*FILESYNC_PRO_DEV_MODE[[:space:]]*=[[:space:]]*1[[:space:]]*$' .env; then
    echo "AVISO: FILESYNC_PRO_DEV_MODE=1 esta activo en .env. No distribuyas la app junto con ese archivo si quieres respetar licencias por plan." >&2
fi

# OCR: comprobar antes de la compilacion larga que Tesseract existe, es de la misma arquitectura que la
# app y que se pueden localizar todas sus bibliotecas.
# Firma con Developer ID: runtime reforzado + entitlements (los exige la notarización).
if [[ -n "${FILESYNC_CODESIGN_IDENTITY:-}" ]]; then
    export FILESYNC_ENTITLEMENTS="${FILESYNC_ENTITLEMENTS:-$PWD/packaging/entitlements.plist}"
    if ! security find-identity -v -p codesigning | grep -Fq "$FILESYNC_CODESIGN_IDENTITY"; then
        echo "No se encontro la identidad de firma \"$FILESYNC_CODESIGN_IDENTITY\" en el llavero." >&2
        exit 1
    fi
fi
NOTARIZE=0
if [[ -n "${FILESYNC_CODESIGN_IDENTITY:-}" && -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    NOTARIZE=1
elif [[ -n "${FILESYNC_CODESIGN_IDENTITY:-}" ]]; then
    echo "AVISO: la app se firmara pero NO se notarizara (faltan APPLE_ID, APPLE_TEAM_ID o APPLE_APP_PASSWORD)." >&2
fi

TARGET_ARCH="${FILESYNC_MAC_ARCH:-$(uname -m)}"
if [[ "${FILESYNC_SKIP_OCR:-}" == "1" ]]; then
    echo "AVISO: FILESYNC_SKIP_OCR=1: la app se compilara SIN Tesseract; el OCR exigira instalarlo en cada Mac (brew install tesseract)." >&2
else
    "$PYTHON_BIN" tools/bundle_tesseract_mac.py --check --expect-arch "$TARGET_ARCH"
fi

if [[ ! -x "$VENV_PATH/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_PATH"
fi
"$VENV_PATH/bin/python" -m pip install --upgrade pip
"$VENV_PATH/bin/python" -m pip install -r requirements-desktop.txt

rm -rf "build/FileSync Pro" "$APP_PATH" "$ZIP_PATH" "$DMG_PATH"

"$VENV_PATH/bin/python" -m PyInstaller --clean --noconfirm "FileSync Pro.spec"

if [[ ! -d "$APP_PATH" ]]; then
    echo "No se genero la app esperada en $APP_PATH" >&2
    exit 1
fi

# OCR: copia Tesseract y sus bibliotecas dentro del .app, reescribe sus rutas, firma cada archivo y
# comprueba que el Tesseract empaquetado arranca solo. Debe ir ANTES del zip/dmg (modifica el .app).
if [[ "${FILESYNC_SKIP_OCR:-}" != "1" ]]; then
    "$VENV_PATH/bin/python" tools/bundle_tesseract_mac.py --app "$APP_PATH"
fi

# DMG con acceso directo a Aplicaciones, para arrastrar e instalar.
STAGE_DIR="$(mktemp -d)"
ditto "$APP_PATH" "$STAGE_DIR/FileSync Pro.app"
ln -s /Applications "$STAGE_DIR/Applications"
hdiutil create -volname "FileSync Pro" -srcfolder "$STAGE_DIR" -ov -format UDZO "$DMG_PATH" >/dev/null
rm -rf "$STAGE_DIR"

if [[ -n "${FILESYNC_CODESIGN_IDENTITY:-}" ]]; then
    codesign --force --timestamp -s "$FILESYNC_CODESIGN_IDENTITY" "$DMG_PATH"
    codesign --verify --strict "$DMG_PATH"
fi

if [[ "$NOTARIZE" == "1" ]]; then
    # Apple revisa el .dmg (y la app que lleva dentro). Suele tardar de 1 a 10 minutos.
    echo "Notarizando con Apple..."
    xcrun notarytool submit "$DMG_PATH" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_PASSWORD" --wait --timeout 30m --output-format json > notarization.json || true
    cat notarization.json
    if ! grep -q '"status" *: *"Accepted"' notarization.json; then
        SUBMISSION_ID="$(sed -n 's/.*"id" *: *"\([^"]*\)".*/\1/p' notarization.json | head -1)"
        echo "La notarizacion fallo o Apple rechazo la app." >&2
        if [[ -n "$SUBMISSION_ID" ]]; then
            xcrun notarytool log "$SUBMISSION_ID" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
                --password "$APPLE_APP_PASSWORD" >&2 || true
        fi
        rm -f notarization.json
        exit 1
    fi
    rm -f notarization.json
    # "Grapar" el resultado: el Mac del cliente lo verifica sin conectarse a Apple.
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler staple "$APP_PATH"
    xcrun stapler validate "$DMG_PATH"
    spctl --assess --type execute -vv "$APP_PATH"
fi

# `ditto` conserva los enlaces y atributos del paquete (un zip normal puede dañar el .app).
# Se hace al final para que el zip lleve la app ya notarizada.
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

echo "Build listo:"
echo "  App: $PWD/$APP_PATH"
echo "  Zip: $PWD/$ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"
echo "  Dmg: $PWD/$DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
if [[ "${FILESYNC_SKIP_OCR:-}" == "1" ]]; then
    echo "OCR: NO incluido (FILESYNC_SKIP_OCR=1)."
else
    echo "OCR: Tesseract incluido en la app (spa + eng). Distribuye THIRD_PARTY_NOTICES.md junto con ella."
fi
if [[ -z "${FILESYNC_CODESIGN_IDENTITY:-}" ]]; then
    echo "La app NO esta firmada ni notarizada: en otros Macs, macOS pedira aprobarla manualmente (ver README)."
elif [[ "$NOTARIZE" == "1" ]]; then
    echo "Firmada con: $FILESYNC_CODESIGN_IDENTITY, y notarizada por Apple."
else
    echo "Firmada con: $FILESYNC_CODESIGN_IDENTITY, pero SIN notarizar: macOS la seguira bloqueando en otros Macs."
fi
