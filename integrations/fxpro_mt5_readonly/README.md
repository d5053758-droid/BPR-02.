# FxPro MT5 — lector de cotizaciones de solo lectura

Este conector es una prueba técnica separada del motor BPR-02. Su único propósito es verificar que una cuenta MT5 demo puede conectarse mediante MetaApi y leer una cotización. **No contiene llamadas para abrir, modificar ni cerrar operaciones.** No demuestra que una estrategia sea rentable.

## Seguridad primero

1. En MetaApi, registra únicamente la cuenta **demo** de FxPro.
2. Cuando agregues las credenciales de MT5, usa la **contraseña de inversor/solo lectura** si FxPro la ofrece. No uses la contraseña maestra de trading para esta prueba.
3. Despliega la cuenta desde el panel de MetaApi y espera a que el estado indique que está conectada.
4. Guarda el token de MetaApi y el ID interno de esa cuenta como secretos del entorno donde ejecutarás el programa. **No los escribas en el código, en este README, en un issue ni en este chat.**
5. No uses una cuenta real. No habilites operaciones automáticas.

El nombre de servidor comunicado para la cuenta demo es `FxPro-MT5 Demo`; cópialo exactamente en el panel al registrar la cuenta. Si MetaApi no reconoce el servidor, no adivines otro nombre: revisa la selección o consulta soporte.

## Costos de MetaApi

La página oficial consultada publica un plan regular de **USD 30/mes**, una prueba de **7 días que se puede activar una sola vez**, una cuenta MT incluida y cargos de acceso a la API que pueden facturarse aparte; impuestos no incluidos. Antes de activar la prueba, revisa las condiciones de facturación y la fecha de conversión. Este repositorio no activa suscripciones ni pagos.

Fuente oficial: https://metaapi.cloud/

## Preparación en GitHub Codespaces

Desde la raíz del repositorio, instala la dependencia:

```bash
python -m pip install -r integrations/fxpro_mt5_readonly/requirements.txt
```

Configura estas variables en el entorno privado de ejecución (por ejemplo, Secrets de Codespaces), no en un archivo público:

- `METAAPI_TOKEN`: token de API obtenido en MetaApi.
- `METAAPI_ACCOUNT_ID`: ID interno de la cuenta que muestra MetaApi, no el número de cuenta MT5.
- `MT5_SYMBOL`: símbolo exacto según el servidor, por defecto `EURUSD`.

Luego ejecuta:

```bash
python integrations/fxpro_mt5_readonly/read_quote.py
```

Si la lectura funciona, imprime una sola cotización y añade un registro a `integrations/fxpro_mt5_readonly/data/quotes.jsonl`. El directorio de datos está ignorado por Git.

## Qué valida y qué no

- Valida autenticación con MetaApi, sincronización del terminal y lectura puntual de una cotización.
- No se conecta directamente al proceso de MT5 Android: la lectura se realiza desde los servidores de MetaApi hacia la cuenta demo.
- No garantiza que se pueda acceder a todos los símbolos ni que los datos coincidan con otro proveedor.
- No deja un proceso de monitoreo permanente.
- No envía órdenes, y no debe ampliarse a ejecución hasta revisar permisos, costos y pruebas por separado.

Documentación del SDK oficial de Python: https://github.com/metaapi/metaapi-python-sdk
