# FileSync Pro

Aplicacion de escritorio en `tkinter` para organizar archivos, preparada para venderse como app descargable con activacion por licencia.

## Que incluye

- App de escritorio bloqueada hasta activar una licencia o habilitar acceso de desarrollador.
- Flujo de compra desde la app.
- Activacion y validacion de licencia por dispositivo.
- Backend `FastAPI` para crear cobros en Mercado Pago Checkout Pro.
- Webhook para emitir licencias cuando el pago quede `approved`.
- Script base para empaquetar un `.exe` con `PyInstaller`.

## Arquitectura recomendada

No pongas el `access token` de Mercado Pago dentro de la app de escritorio.

- `Organizador.py`: cliente descargable.
- `backend_app.py`: servidor seguro con credenciales, pagos y licencias.
- `Mercado Pago`: checkout redirigido para cobrar.

El backend ahora tambien puede servir una web simple de ventas:

- `GET /`: landing con formulario de compra.
- `POST /buy`: crea el pedido y redirige al checkout de Mercado Pago.
- `GET /checkout/return/success`: pagina de pago aprobado.
- `GET /checkout/return/pending`: pagina de pago pendiente.
- `GET /checkout/return/failure`: pagina de pago fallido.

## Instalacion local

### 1. Backend

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-backend.txt
Copy-Item .env.example .env
uvicorn backend_app:app --reload
```

Luego define en el entorno las variables de `.env.example`.

Si tu web publica vive en otro dominio distinto al backend, separa ambas URLs:

```powershell
$env:PUBLIC_BASE_URL="https://tu-api.com"
$env:FILESYNC_PRO_SITE_URL="https://tu-sitio.com"
```

- `PUBLIC_BASE_URL`: URL publica del backend que recibe el webhook de Mercado Pago.
- `FILESYNC_PRO_SITE_URL`: URL de tu pagina o landing, usada para las redirecciones `success`, `pending` y `failure`.
- Ejemplo con Odoo: `FILESYNC_PRO_SITE_URL="https://fylesinc-pro.odoo.com"`
- Si quieres usar la web integrada del backend en lugar de Odoo, define `FILESYNC_PRO_SITE_URL` igual a `PUBLIC_BASE_URL`.
- Plantilla local: `.env.local.example`
- Plantilla produccion: `.env.production.example`

## Despliegue gratis recomendado

Para una opcion sin costo, este proyecto queda preparado para:

- `Render`: publica el backend FastAPI usando `render.yaml`.
- `Neon`: provee `Postgres` gratis mediante `DATABASE_URL`.
- Guia paso a paso: `DEPLOY_FREE.md`

Variables minimas en Render:

```text
DATABASE_URL=postgresql://...
PUBLIC_BASE_URL=https://tu-servicio.onrender.com
FILESYNC_PRO_SITE_URL=https://tu-servicio.onrender.com
MP_ACCESS_TOKEN=APP_USR_...
MP_WEBHOOK_SECRET=...
```

Notas:

- Si usas la web integrada del backend, no necesitas depender de Odoo para el flujo de compra.
- Si prefieres seguir con Odoo como web principal, puedes dejar `FILESYNC_PRO_SITE_URL=https://fylesinc-pro.odoo.com`.
- `DATABASE_URL` activa automaticamente Postgres; si no existe, la app sigue usando SQLite local.

### 2. Desktop

```powershell
python Organizador.py
```

Si tu backend no esta en local:

```powershell
$env:FILESYNC_PRO_API_URL="https://tu-api.com"
python Organizador.py
```

## Empaquetado

```powershell
pip install -r requirements-desktop.txt
.\build.ps1
```

El ejecutable quedara en `dist\FileSync Pro.exe`.

## Flujo de venta

1. La app pide correo del comprador.
2. El backend crea una preferencia en Mercado Pago.
3. Mercado Pago redirige al checkout.
4. El webhook recibe la notificacion de pago.
5. El backend consulta el pago, valida que este `approved` y emite una licencia.
6. La app activa la licencia en el equipo.

## Endpoints del backend

- `POST /checkout/create`
- `GET /checkout/status/{order_id}`
- `POST /licenses/activate`
- `POST /licenses/validate`
- `POST /webhooks/mercadopago`
- `GET /health`

## Notas de negocio

- Este ejemplo vende una licencia vitalicia unica.
- Puedes cambiar `FILESYNC_PRO_PRICE`, moneda y limite de activaciones.
- Si quieres mas proteccion, el siguiente paso es firmar la licencia con JWT o clave privada y agregar panel admin.

## Modo desarrollador local

- La app de escritorio ahora acepta `FILESYNC_PRO_DEV_MODE=1` para desbloquear todas las funciones en el equipo donde se ejecute.
- La app busca el `.env` en el directorio actual y tambien junto al script o ejecutable.
- Si prefieres habilitar solo algunos equipos, usa `FILESYNC_PRO_DEV_DEVICE_IDS` con una lista de `device_id` separada por comas.
- En este proyecto, el `.env` local quedó con `FILESYNC_PRO_DEV_MODE=1`, así que este equipo entra con acceso de desarrollador.

## Referencias oficiales usadas

- [Crear y configurar una preferencia de pago](https://www.mercadopago.com.co/developers/es/docs/checkout-pro/create-payment-preference)
- [API reference: crear preferencia](https://www.mercadopago.com.co/developers/en/reference/preferences/_checkout_preferences/post)
- [Notificaciones de Checkout Pro](https://www.mercadopago.com.co/developers/en/docs/checkout-pro/payment-notifications)
- [Referencia: obtener pago](https://www.mercadopago.com.co/developers/pt/reference/payments/_payments_id/get)
