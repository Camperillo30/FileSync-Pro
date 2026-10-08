# -*- mode: python ; coding: utf-8 -*-
# Especificación ÚNICA de PyInstaller para FileSync Pro (Windows y macOS).
#   Windows: .\build.ps1     ->  dist\FileSync Pro.exe
#   macOS:   ./build_mac.sh  ->  dist/FileSync Pro.app
# PyInstaller no compila para otro sistema: cada versión se genera en su propio SO.
import glob
import os
import re
import sys

IS_MAC = sys.platform == 'darwin'


def _app_version():
    """Lee APP_VERSION de Organizador.py para que el .app muestre la misma versión."""
    try:
        with open('Organizador.py', encoding='utf-8') as handle:
            match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', handle.read(), re.MULTILINE)
    except OSError:
        return '1.0.0'
    return match.group(1) if match else '1.0.0'


datas = [('desktop_runtime_config.json', '.')]

# OCR: se incluye Tesseract (ejecutable, bibliotecas e idiomas spa+eng) para que la lectura de
# facturas funcione en un equipo que no lo tenga instalado.
#   Windows: `build.ps1` deja la carpeta preparada (y adelgazada) en vendor/tesseract, que no se versiona;
#            la app la busca en `tesseract/` dentro del .exe. Se agrega DESPUÉS de Analysis (ver abajo).
#   macOS:   NO se agrega aquí. Las bibliotecas de Homebrew hay que reubicarlas y firmarlas una por una,
#            así que `build_mac.sh` lo hace después de PyInstaller con tools/bundle_tesseract_mac.py.
_tesseract_root = os.path.join('vendor', 'tesseract')
_tesseract_exe = 'tesseract.exe' if sys.platform.startswith('win') else 'tesseract'
tesseract_files = []
if not IS_MAC:
    if os.path.isfile(os.path.join(_tesseract_root, _tesseract_exe)):
        for _folder, _subfolders, _names in os.walk(_tesseract_root):
            _subfolders.sort()
            for _name in sorted(_names):
                _source = os.path.join(_folder, _name)
                _relative = os.path.relpath(_source, _tesseract_root).replace(os.sep, '/')
                tesseract_files.append(('tesseract/' + _relative, _source, 'DATA'))
    else:
        print('AVISO: no existe vendor/tesseract/' + _tesseract_exe + ': el OCR NO se incluira en este build.')

if IS_MAC:
    # En el .app los recursos gráficos van dentro del paquete (ícono de la ventana y logos
    # del encabezado). No se incluyen las vistas previas (ico_preview*, exe_icon_preview*).
    datas += [(path, 'assets') for path in sorted(glob.glob('assets/icono*.png'))]
    datas += [('assets/logo_marca_28.png', 'assets')]

a = Analysis(
    ['Organizador.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Los archivos de Tesseract se agregan como DATA una vez hecho el análisis. Si se pasaran en `datas=`,
# PyInstaller reclasificaría cada .dll como binario, analizaría sus dependencias y volvería a copiar
# todas las DLL al nivel superior del .exe: el mismo motor dos veces (~130 MB en vez de ~30 MB).
a.datas += tesseract_files
pyz = PYZ(a.pure)

if IS_MAC:
    # macOS: carpeta (no un solo archivo) empaquetada como .app. Un .app de un solo archivo
    # tardaría varios segundos en abrir, porque se descomprime en cada arranque.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='FileSync Pro',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        # Vacío = la arquitectura del Mac que compila (arm64 o x86_64). 'universal2' sirve en
        # ambos, pero exige un Python universal2 (python.org) y dependencias universales.
        target_arch=os.environ.get('FILESYNC_MAC_ARCH') or None,
        # Para firmar: FILESYNC_CODESIGN_IDENTITY="Developer ID Application: Nombre (TEAMID)".
        # Sin firma, PyInstaller aplica una firma ad-hoc (suficiente para correr, no para distribuir).
        codesign_identity=os.environ.get('FILESYNC_CODESIGN_IDENTITY') or None,
        entitlements_file=os.environ.get('FILESYNC_ENTITLEMENTS') or None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='FileSync Pro',
    )
    _version = _app_version()
    _acceso_carpetas = 'FileSync Pro necesita acceder a esta carpeta para organizar tus archivos.'
    app = BUNDLE(
        coll,
        name='FileSync Pro.app',
        icon='assets/icono.icns',
        # Identificador del paquete: cámbialo por uno propio (dominio inverso) antes de firmar
        # y notarizar, o define FILESYNC_BUNDLE_ID.
        bundle_identifier=os.environ.get('FILESYNC_BUNDLE_ID', 'miempresa.filesyncpro.desktop'),
        version=_version,
        info_plist={
            'CFBundleName': 'FileSync Pro',
            'CFBundleDisplayName': 'FileSync Pro',
            'CFBundleShortVersionString': _version,
            'CFBundleVersion': _version,
            'LSApplicationCategoryType': 'public.app-category.productivity',
            'NSHighResolutionCapable': True,
            # Textos del aviso de permisos de macOS al leer estas carpetas por primera vez.
            'NSDesktopFolderUsageDescription': _acceso_carpetas,
            'NSDocumentsFolderUsageDescription': _acceso_carpetas,
            'NSDownloadsFolderUsageDescription': _acceso_carpetas,
            'NSRemovableVolumesUsageDescription': _acceso_carpetas,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='FileSync Pro',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=['assets\\icono.ico'],
    )
