# FileSync Pro

Aplicación de escritorio en Tkinter para organizar archivos y extraer datos de facturas.

## Licencias: Wompi, Make y Google Sheets

El flujo de compra se mantiene simple:

1. La app abre el enlace de pago de Wompi del plan elegido.
2. Make registra el pago aprobado en la hoja de Google Sheets.
3. La app consulta el CSV publicado de esa hoja y activa la licencia con el correo de compra.

La hoja debe tener las columnas `email`, `plan`, `estado` y `fecha_pago`. Los valores activos aceptados son `activo`, `active` o `authorized`.

La configuración del ejecutable está en `desktop_runtime_config.json`:

```json
{
  "FILESYNC_PRO_LICENSE_CSV_URL": "https://docs.google.com/.../pub?...&output=csv",
  "WOMPI_SUBSCRIPTION_LINK_MAP": {"basica": "...", "pro": "...", "premium": "..."}
}
```

No compartas archivos `.env` con credenciales de Wompi o Make.

## Uso

```powershell
pip install -r requirements-desktop.txt
python Organizador.py
```

Puedes usar la misma carpeta como origen y destino para organizar los archivos en subcarpetas. Si usas carpetas distintas, ninguna puede estar dentro de la otra. Los duplicados se envían a una cuarentena recuperable y la operación se puede cancelar y reanudar.

## Planes

| Plan | Funciones | Límite mensual |
| --- | --- | --- |
| Básica | Organización por categoría/fecha y movimiento | 3 usos |
| Pro | Añade filtro, duplicados y deshacer | 10 usos |
| Premium | Todas las funciones | Ilimitado |

## Modo desarrollador local

`desktop_runtime_config.local.json` habilita el modo desarrollador solo en el equipo donde se coloque. No se versiona ni se distribuye junto al ejecutable.

## Empaquetado

```powershell
.\build.ps1
```

El ejecutable queda en `dist\FileSync Pro.exe`. Mantén una única especificación activa: `FileSync Pro.spec` (sirve para Windows y para macOS).

### OCR incluido en el .exe

El .exe lleva dentro Tesseract OCR (motor + idiomas `spa` y `eng`), así que leer imágenes de facturas funciona en un PC que no lo tenga instalado. `build.ps1` lo prepara solo en `vendor\tesseract` (no se versiona):

1. Busca una instalación de Tesseract (Archivos de programa, la variable `TESSERACT_DIR`, el PATH o `-TesseractDir`). Si no hay ninguna, intenta instalarla con `winget` (UB-Mannheim.TesseractOCR).
2. Copia `tesseract.exe`, sus DLL y los idiomas; si falta `spa` o `eng`, los descarga de `tessdata_fast`.
3. La adelgaza con `tools\slim_tesseract_win.py` (ver más abajo) y comprueba que sigue leyendo una imagen.
4. Comprueba que la copia arranca sola y ve `spa` y `eng` antes de compilar.

La app usa primero ese Tesseract empaquetado (`configure_ocr` en `Organizador.py`) y, si no está, el del sistema. `.\build.ps1 -SkipOcr` genera el .exe sin OCR. Al distribuir el .exe, incluye `THIRD_PARTY_NOTICES.md` (licencias de Tesseract y otras).

**Tamaño.** Con el Tesseract de UB Mannheim tal cual, el .exe pasó de unos 13 MB a unos 131 MB. Había tres causas, y las tres están corregidas:

- `FileSync Pro.spec` agrega los archivos de Tesseract después del análisis de PyInstaller. Si se pasan en `datas=`, PyInstaller trata cada `.dll` como binario, busca sus dependencias y las vuelve a copiar en la raíz del .exe: el mismo motor dos veces.
- `tools\slim_tesseract_win.py` borra las DLL que `tesseract.exe` no necesita (las de las herramientas de entrenamiento: Pango, Cairo, GLib, HarfBuzz, ICU...; se calcula siguiendo las importaciones de `tesseract.exe` y de cada DLL que sí usa).
- El mismo script quita de las DLL/EXE la información de depuración (DWARF y tabla de símbolos): `libtesseract-5.dll` pasa de ~101 MB a unos pocos MB sin cambiar lo que se ejecuta. Comprueba que las secciones de código y datos quedan idénticas byte a byte, y elimina la firma digital de esos archivos (quitar datos la invalida). Después arranca tesseract y lee una imagen; si algo falla, `build.ps1` restaura la copia completa.

En una prueba con los binarios reales (ejecutados con Wine en Linux) la carpeta de Tesseract bajó de 173,8 MB a 28,1 MB y el texto leído fue idéntico en PNG, JPG, TIFF, WEBP, GIF y BMP. El tamaño del .exe final se estima entre 25 y 45 MB (no está medido: depende de la compresión); compáralo con el que te da `build.ps1` al terminar. `.\build.ps1 -NoSlim` conserva Tesseract completo. Como el .exe es de un solo archivo, se descomprime en una carpeta temporal en cada arranque, así que abrir la app tarda unos segundos más que sin OCR.

Para comprobar en otro PC (sin Tesseract) que el OCR incluido funciona, sin abrir la ventana:

```powershell
Start-Process -Wait -FilePath ".\FileSync Pro.exe" -ArgumentList "--ocr-selftest", "ocr.txt"
Get-Content ocr.txt
```

El informe muestra qué Tesseract usó (debe estar dentro del .exe), sus idiomas y la lectura de una imagen de prueba, y termina en `resultado=OK` si todo va bien.

## macOS

La app corre igual en Mac. Todo lo que cambia según el sistema operativo está en `filesync_core/platform_compat.py`.

**Ejecutar desde el código** (Python 3.13 de [python.org](https://www.python.org/downloads/macos/); el Python de Apple trae Tk 8.5 y no sirve):

```bash
python3 -m pip install -r requirements-desktop.txt
python3 Organizador.py
```

Al ejecutar desde el código, el OCR necesita Tesseract instalado en el Mac: `brew install tesseract tesseract-lang`. La app lo encuentra aunque se abra desde Finder. (En la app empaquetada ya viene incluido; ver más abajo.)

**Empaquetar** (PyInstaller no compila para otro sistema, así que hay que hacerlo en un Mac o con GitHub Actions):

```bash
bash build_mac.sh
```

Genera `dist/FileSync Pro.app`, `dist/FileSync-Pro-macOS.zip` y `dist/FileSync-Pro-macOS.dmg` para la arquitectura del Mac que compila. Sin Mac: en GitHub, *Actions > Build macOS > Run workflow* compila las versiones Apple Silicon e Intel y las deja como artefactos descargables.

**OCR incluido en el .app:** `build_mac.sh` copia dentro de la app el Tesseract de Homebrew, para que leer facturas funcione en un Mac sin Homebrew. Solo hace falta `brew install tesseract` en el Mac que compila (los idiomas `spa` y `eng` se descargan si faltan). El script `tools/bundle_tesseract_mac.py` copia el ejecutable y todas sus bibliotecas a `Contents/Frameworks/tesseract`, reescribe sus rutas para que no apunten a Homebrew, firma cada archivo y comprueba que el Tesseract empaquetado arranca solo y lee una imagen de prueba; si algo falla, el build se detiene. Detalles:

- Cada arquitectura lleva su propio Tesseract: la app de Apple Silicon se compila en un Mac Apple Silicon y la Intel en un Mac Intel (GitHub Actions lo hace). Con `FILESYNC_MAC_ARCH=universal2` no se puede incluir el OCR: usa `FILESYNC_SKIP_OCR=1` o compila cada arquitectura por separado.
- `FILESYNC_SKIP_OCR=1 bash build_mac.sh` compila la app sin OCR incluido. `TESSERACT_BIN` indica otra ruta de tesseract.
- Tamaño: estimado en 30-50 MB más (sin medir; en Windows el aumento sin adelgazar fue mucho mayor de lo previsto, así que compruébalo). En Mac la app es una carpeta, no se descomprime al abrir.
- Para comprobar una app ya compilada: `"dist/FileSync Pro.app/Contents/MacOS/FileSync Pro" --ocr-selftest` (debe terminar en `resultado=OK`).
- Al distribuir la app, incluye `THIRD_PARTY_NOTICES.md` (ya se copia dentro del .app, en `Contents/Resources`).

**Firma:** sin firma ni notarización de Apple, en otro Mac el primer arranque exige aprobar la app a mano (clic derecho > Abrir, o *Ajustes del Sistema > Privacidad y seguridad > Abrir de todos modos*). Para distribuirla sin ese aviso hace falta una cuenta de Apple Developer y las variables `FILESYNC_CODESIGN_IDENTITY` y `FILESYNC_BUNDLE_ID` (ver `build_mac.sh`).

Diferencias frente a Windows:

- Los datos de la app (licencia, preferencias, cuarentena, registro) están en `~/Library/Application Support/FileSync Pro`.
- Al organizar, no se tocan los paquetes de macOS (`.app`, `.photoslibrary`, etc.), `.DS_Store` ni los archivos `._*`. Con "Eliminar carpetas vacías", una carpeta que solo contiene `.DS_Store` cuenta como vacía.
- La licencia se activa con el mismo correo que en Windows. El Mac se identifica por su UUID de hardware, que no cambia al cambiar de Wi-Fi.
- El archivo de licencia no se cifra (macOS no tiene DPAPI); queda con permisos solo para el usuario.
- La primera vez que se elige una carpeta como Descargas o Documentos, macOS pide permiso de acceso.

## Calidad

```powershell
pytest
ruff check .
```
