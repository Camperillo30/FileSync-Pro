# FileSync Pro

Aplicacion de escritorio en `tkinter` para organizar archivos y controlar acceso por licencia.

## Que incluye

- Compra de licencia desde la app con links de Mercado Pago.
- Activacion y validacion de licencia por dispositivo.
- Restricciones por plan: `basica`, `pro` y `premium`.
- Ejecutable `.exe` generado con `PyInstaller`.

## Archivos principales

- `Organizador.py`: app de escritorio.
- `backend_app.py`: servicio interno de licencias y suscripciones.
- `desktop_runtime_config.json`: configuracion embebida para compra y activacion.
- `build.ps1`: crea el entorno de build y genera el `.exe`.
- `FileSync Pro.spec`: especificacion activa de `PyInstaller`.

## Python recomendado

- Version objetivo: `Python 3.13.5`
- Referencia local: `.python-version`

## Uso local

### Servicio de licencias

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-backend.txt
Copy-Item .env.example .env
uvicorn backend_app:app --reload
```

### App de escritorio

```powershell
python Organizador.py
```

## Empaquetado

```powershell
.\build.ps1
```

El ejecutable queda en `dist\FileSync Pro.exe`.

El script:

- usa `Python 3.13`
- crea `.\.venv313` si hace falta
- instala dependencias de escritorio
- genera el `.exe` con `FileSync Pro.spec`
- embebe `desktop_runtime_config.json`

## Restricciones por plan

- `basica`: sin filtro por extension, sin mover archivos, sin eliminar duplicados y con cupo de `15` archivos por mes.
- `pro`: habilita filtro por extension y eliminacion de duplicados, con cupo de `100` archivos por mes.
- `premium`: habilita todas las funciones y no tiene limite mensual.
- Si un `plan_code` antiguo no coincide con esos nombres, la app conserva compatibilidad para no bloquear licencias previas por error.

## Modo desarrollador local

- `FILESYNC_PRO_DEV_MODE=1` desbloquea todas las funciones en el equipo actual.
- `FILESYNC_PRO_DEV_DEVICE_IDS` permite habilitar equipos puntuales por `device_id`.
- No distribuyas el `.env` local si tiene modo desarrollador activo.
