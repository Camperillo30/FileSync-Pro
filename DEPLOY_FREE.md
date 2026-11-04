# Despliegue Gratis: Render + Neon

Esta ruta sirve para pruebas reales del flujo de compra sin pagar hosting.

## Checklist rapida

1. Crear la base en Neon.
2. Copiar `DATABASE_URL`.
3. Subir este proyecto a GitHub.
4. Crear `Blueprint` en Render con ese repo.
5. Pegar `DATABASE_URL`, `PUBLIC_BASE_URL`, `FILESYNC_PRO_SITE_URL`, `MP_ACCESS_TOKEN` y `MP_WEBHOOK_SECRET`.
6. Abrir `/health` y confirmar que responde.
7. Poner la URL de Render en `FILESYNC_PRO_API_URL` de la app.
8. Probar compra, pago y verificacion de licencia.

## 1. Crear la base de datos en Neon

1. Crea una cuenta en Neon.
2. Crea un proyecto nuevo de Postgres.
3. Copia la cadena de conexion `postgresql://...`.
4. Guarda ese valor como `DATABASE_URL`.

## 2. Subir este proyecto a GitHub

1. Crea un repositorio en GitHub.
2. Sube este proyecto completo.
3. Verifica que `render.yaml` quede en la raiz del repo.

## 3. Crear el servicio en Render

1. Crea una cuenta en Render.
2. Elige `New` -> `Blueprint`.
3. Selecciona tu repositorio de GitHub.
4. Render leerá `render.yaml` y propondrá crear el servicio web.

## 4. Configurar variables en Render

Completa estas variables:

```text
DATABASE_URL=postgresql://...
PUBLIC_BASE_URL=https://tu-servicio.onrender.com
FILESYNC_PRO_SITE_URL=https://tu-servicio.onrender.com
MP_ACCESS_TOKEN=APP_USR_TU_TOKEN_REAL
MP_WEBHOOK_SECRET=TU_SECRETO_WEBHOOK
FILESYNC_PRO_PRICE=49.99
FILESYNC_PRO_CURRENCY=COP
FILESYNC_PRO_MAX_ACTIVATIONS=2
```

Si quieres seguir usando Odoo como sitio visible:

```text
FILESYNC_PRO_SITE_URL=https://fylesinc-pro.odoo.com
```

En ese caso el backend sigue publicado en Render, pero las redirecciones de pago vuelven a Odoo.

## 5. Verificar el backend

Cuando termine el deploy, abre:

```text
https://tu-servicio.onrender.com/health
```

Debe responder con un JSON parecido a:

```json
{"status":"ok"}
```

## 6. Configurar la app de escritorio

En tu `.env` local o en el equipo donde corra el `.exe`:

```text
FILESYNC_PRO_API_URL=https://tu-servicio.onrender.com
PUBLIC_BASE_URL=https://tu-servicio.onrender.com
FILESYNC_PRO_SITE_URL=https://tu-servicio.onrender.com
```

## 7. Probar el flujo

1. Abre la web en Render.
2. Ingresa un correo.
3. Completa el checkout de Mercado Pago.
4. Vuelve a la app.
5. Pulsa `Verificar licencia`.

## Notas importantes

- Este proyecto usa `Postgres` automaticamente cuando existe `DATABASE_URL`.
- Si `DATABASE_URL` no existe, sigue usando SQLite local.
- La web integrada del backend vive en `/`.
- El formulario de compra vive en `/buy`.
- Las paginas de retorno son:
  - `/checkout/return/success`
  - `/checkout/return/pending`
  - `/checkout/return/failure`
