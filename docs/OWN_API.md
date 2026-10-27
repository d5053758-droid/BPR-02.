# BPR-02: API propia de datos (solo lectura)

## Alcance

La API propia no crea precios ni sustituye a una fuente de cotizaciones. Sirve los datos ya guardados por un adaptador autorizado, en un formato estable que el motor de investigación pueda consumir. No conecta con una cuenta de Pocket Option ni tiene rutas para enviar órdenes.

Por defecto el servidor escucha solo en `127.0.0.1`; no es accesible directamente desde Internet. Si no hay un CSV local, `/v1/candles` responde `503 data_missing` y `/v1/quality` marca `data_available: false`. Nunca genera cotizaciones sintéticas como sustituto de datos ausentes.

## Instalación y ejecución local

Desde la raíz del repositorio:

```bash
python -m pip install -e ".[test]"
bpr02-api
```

El servidor muestra `http://127.0.0.1:8000`. Para detenerlo, presione Ctrl+C en la terminal.

Compruebe que responde:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/quality
```

Cuando exista el archivo `otc_candles_5s.csv`, consulte las últimas 100 velas:

```bash
curl "http://127.0.0.1:8000/v1/candles?symbol=EURUSD_otc&timeframe_seconds=5&limit=100"
```

La API sirve por defecto estos archivos relativos al directorio actual:

- `otc_candles_5s.csv`: velas OHLC validadas al leerlas.
- `otc_candles_5s_report.json`: metadatos de la consulta del proveedor, si existen.
- `otc_ticks_raw.jsonl`: registro de ticks en vivo, si se ha capturado mediante una fuente autorizada.

Se pueden cambiar con `BPR02_API_CANDLES`, `BPR02_API_CANDLES_REPORT` y `BPR02_API_TICKS`. Configure `BPR02_API_SYMBOL` y `BPR02_API_TIMEFRAME_SECONDS` para identificar el conjunto servido. La configuración debe coincidir con los datos reales; el servidor rechaza solicitudes para otro símbolo o intervalo.

## Endpoints

- `GET /`: índice de rutas.
- `GET /health`: estado del servidor (no demuestra que exista una fuente de datos activa).
- `GET /v1/candles?symbol=EURUSD_otc&timeframe_seconds=5&limit=100`: últimas velas del CSV.
- `GET /v1/quality`: conteo, intervalo temporal, huecos aproximados y advertencias.
- `GET /v1/ticks/latest`: último tick del registro JSONL, o error explícito si no existe.

`limit` acepta valores de 1 a 1.500. Todas las respuestas de datos incluyen `orders_submitted: 0). Los errores HTTP se usan de forma intencional: `400` para parámetros incompatibles, `401` para token ausente/incorrecto cuando está configurado, `422` para datos inválidos y `503` cuando falta el archivo o su contenido.

## Seguridad

- No suba claves de proveedores, tokens API ni registros privados a GitHub.
- El servidor se enlaza por defecto solo a loopback (`127.0.0.1`).
- Si intenta enlazar a una dirección no local (por ejemplo `0.0.0.0`), el servicio se niega a arrancar sin `BPR02_API_TOKEN`.
- Configure el token solo en el entorno del servidor y páselo en el encabezado `Authorization: Bearer ...`; el código compara el token de manera segura y no lo escribe en las respuestas.
- Un puerto reenviado debe permanecer privado. No publique esta API ni sus datos en Internet sin autenticarla, revisar el despliegue y habilitar HTTPS.
- Esta API es de datos y análisis únicamente. No incluye autenticación de Pocket Option ni endpoints de operaciones.

## Límites de interpretación

La API valida formato, tiempos y rangos OHLC; eso no demuestra autenticidad ni coincidencia de precios con la terminal de Pocket Option. Los datos de EUR/USD convencional no equivalen a EUR/USD OTC. Las velas de 5 segundos tampoco permiten reconstruir un precio de liquidación exacto a 3 segundos. Esas hipótesis requieren una fuente autorizada, paridad medida y validación aparte.

## Comprobar la clave de OTCharts sin consumir la cuota

Antes de descargar velas, ejecuta:

```bash
bash scripts/check_otcharts_auth.sh
```

La clave se solicita de forma oculta y no se imprime. El script consulta únicamente `GET /v1/usage`, que la documentación del proveedor indica que no consume la cuota que devuelve. Informa el código HTTP, el plan y el saldo de solicitudes sin mostrar la clave. Si devuelve `401`, revisa el correo confirmado y crea una clave nueva desde la cuenta si la anterior se perdió o fue revocada. No pegues la clave en GitHub, en el chat ni en archivos del repositorio.


## Un solo comando para preparar los datos y abrir la API

Desde la raíz de Codespaces, ejecuta:

```bash
git pull && bash scripts/start_bpr02.sh
```

El script instala el paquete, solicita la clave OTCharts de forma oculta solo si aún no existe el CSV, comprueba `GET /v1/usage` (no consume la cuota de datos), pide como máximo una página de 1.500 velas de 5 segundos, genera un informe de calidad y arranca el servidor local. Si ya hay un CSV, no vuelve a descargarlo ni consume otra solicitud. Si la autenticación falla, se detiene antes de pedir velas y muestra el código HTTP sin revelar la clave.

El servidor escucha solamente en `127.0.0.1:8000`. En Codespaces se puede abrir el puerto 8000 desde la pestaña **Ports** y mantenerlo como **Private**. No publiques el puerto y no configures `0.0.0.0` sin token de API. Para detener el servidor, usa Ctrl+C. La API sirve archivos locales; no implica que haya un flujo continuo. El plan gratuito de OTCharts no incluye streaming en vivo.

Si necesitas comprobar solo la clave, sin descargar velas ni abrir el servidor:

```bash
bash scripts/check_otcharts_auth.sh
```
