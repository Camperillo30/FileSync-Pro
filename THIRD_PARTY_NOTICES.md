# Avisos de software de terceros

El instalador/ejecutable de FileSync Pro incluye los siguientes componentes de terceros. Cada uno se distribuye bajo su propia licencia; conserva este archivo junto con la aplicación cuando la distribuyas.

## Tesseract OCR

- Uso: lectura de texto (OCR) de imágenes de facturas, con los idiomas español (`spa`) e inglés (`eng`).
- Proyecto: <https://github.com/tesseract-ocr/tesseract> — licencia Apache 2.0 (`LICENSE-tesseract.txt`, que `build.ps1` descarga y deja dentro de `vendor\tesseract`).
- Datos de idioma: <https://github.com/tesseract-ocr/tessdata_fast> — licencia Apache 2.0.
- Compilación para macOS: la de Homebrew (<https://formulae.brew.sh/formula/tesseract>), con las bibliotecas de las que depende, copiadas dentro de la app (`Contents/Frameworks/tesseract/lib`): Leptonica (BSD 2-Clause), libarchive (BSD), libpng, libtiff, libjpeg-turbo, libwebp, OpenJPEG, giflib, zlib, zstd, lz4, xz, bzip2, OpenSSL y otras, cada una bajo su propia licencia. El detalle de cada una está en `/opt/homebrew/opt/<paquete>/` (o `brew info <paquete>`) en el Mac que compila.
- Compilación para Windows: instalador de UB Mannheim (<https://github.com/UB-Mannheim/tesseract/wiki>). Trae, junto a `tesseract.exe`, bibliotecas propias como Leptonica, libarchive, libcurl, zlib, libpng y otras, cada una bajo su licencia (BSD, MIT, zlib, curl, entre otras). Revisa la carpeta `doc` de la instalación de Tesseract si necesitas el detalle completo. Al empaquetar, `tools/slim_tesseract_win.py` no incluye las DLL que `tesseract.exe` no necesita y quita la información de depuración de las que sí (el código y los datos no se modifican; la firma digital de esos archivos se elimina).

## Bibliotecas de Python

Pillow (licencia HPND), pytesseract (Apache 2.0), pypdf (BSD-3-Clause) y PyInstaller (GPL-2.0 con excepción que permite distribuir los programas que empaqueta).
