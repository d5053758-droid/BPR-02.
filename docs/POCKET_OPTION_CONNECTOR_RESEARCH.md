# Investigación del conector Pocket Option

**Fecha de revisión:** 2026-10-26  
**Estado:** investigación preliminar; no hay conector implementado.  
**Alcance deseado:** acceso de solo lectura a datos de velas/cotizaciones para generar alertas de investigación en BPR-02. No incluye enviar, modificar ni cerrar operaciones.

## 1. Conclusión provisional

En las páginas públicas oficiales revisadas no encontramos una documentación de desarrolladores con endpoints, SDK, procedimiento de autenticación o permisos de API para BPR-02. Un artículo publicado en el blog oficial de Pocket Option indica que la plataforma no ofrece acceso directo a una API de trading. Esto respalda la conclusión provisional de que **no se ha identificado una API pública documentada**.

Esto no demuestra que no exista una integración privada, comercial o autorizada que el soporte pueda habilitar. La siguiente acción razonable es pedir confirmación por escrito al soporte oficial y conservar su respuesta.

Fuentes oficiales revisadas:

- Oferta pública en español: https://pocketoption.com/es/public-offer
- Artículo del blog oficial que menciona el acceso directo a API: https://pocketoption.com/blog/en/knowledge-base/learning/forex-trading-apis-for-2025-best-broker-trade-options/

## 2. Condiciones y diferencias de cotización

La oferta pública publicada indica que Pocket Option utiliza sus propias fuentes de cotización remuneradas y describe la terminal de trading como una interfaz web conectada a su servidor. También establece que la empresa puede anular o revisar resultados de operaciones realizadas con software bot no autorizado.

Fuentes:

- Sección sobre fuentes de cotización y terminal, oferta pública: https://pocketoption.com/es/public-offer
- Sección sobre revisión de resultados y software bot no autorizado, oferta pública: https://pocketoption.com/es/public-offer

Implicaciones para BPR-02:

1. Una librería que técnicamente consiga conectarse no demuestra que el método esté autorizado.
2. Una serie genérica de EUR/USD de otro proveedor no debe tratarse como equivalente a EUR/USD OTC mostrado por Pocket Option.
3. No se debe automatizar el navegador, imitar tráfico interno ni usar una sesión autenticada mientras no exista autorización y especificación oficial suficiente.
4. En esta etapa no se ejecutarán operaciones ni se solicitarán credenciales, cookies, códigos de acceso o identificadores de sesión.

## 3. Qué encontramos en proyectos comunitarios

Hay repositorios públicos de terceros que se describen como clientes no oficiales de WebSocket para Pocket Option, con funciones que dicen abarcar cotizaciones, velas y datos de cuenta. Sus propias páginas declaran que no son productos oficiales y que el protocolo utilizado no está documentado y puede cambiar.

Ejemplos observados durante la investigación:

- https://github.com/ALSHAWAFI/pocket-option-api
- https://github.com/chema-creator/PocketOptionApi

Estos enlaces son evidencia de que existen implementaciones comunitarias; **no son prueba de autorización, estabilidad, seguridad ni coincidencia de precios**. No se incorporan como dependencia de BPR-02. Uno de los proyectos revisados además documenta el uso de una cadena SSID de sesión y desactiva la verificación TLS en una sesión HTTP auxiliar; ambos son motivos adicionales para no adoptarlo sin una auditoría técnica y autorización explícita.

## 4. Opciones ordenadas por preferencia

### Opción A — API o feed oficial de solo lectura

Preferida. Continuar solo si Pocket Option confirma por escrito un método permitido y proporciona documentación vigente. El acceso debería limitarse a datos de mercado y no disponer de permisos para operar o gestionar la cuenta.

### Opción B — Exportación oficial de datos

Si la plataforma ofrece una función oficial para exportar velas o historial de gráficos, evaluar ese archivo como fuente de investigación. La disponibilidad de una exportación de velas no se ha confirmado todavía. La importación debe validar fechas, zona horaria, instrumento, OTC/no OTC, intervalos, huecos y valores OHLC.

### Opción C — Fuente externa independiente

Puede servir para estudiar la hipótesis general de EUR/USD, pero no para afirmar que el motor replica las cotizaciones de Pocket Option, especialmente en OTC. Debe quedar etiquetada como fuente externa y no mezclarse con datos de la plataforma.

### Opción D — Cliente WebSocket comunitario, scraping o automatización del navegador

No recomendado ni autorizado para implementación en esta etapa. No se usará ingeniería inversa de interfaces privadas ni credenciales/sesiones para intentar sortear la falta de documentación. Reabrir esta opción requeriría permiso expreso y una revisión separada de seguridad y condiciones.

## 5. Preguntas para soporte oficial

Solicitar una respuesta por escrito a estas preguntas:

1. ¿Pocket Option ofrece una API, SDK o feed de datos oficial para desarrolladores o integraciones personales?
2. ¿Existe un mecanismo autorizado de **solo lectura** para recibir ticks o velas OHLC históricas y en tiempo real, sin enviar órdenes?
3. ¿Hay documentación oficial vigente, proceso de aprobación y condiciones de uso para esa integración?
4. ¿La API/feed cubre expresamente el instrumento EUR/USD OTC de la terminal, o solo instrumentos de mercado convencional?
5. ¿Los datos incluyen la zona horaria, el timestamp de cierre real de cada vela, la precisión de precios y los identificadores oficiales del instrumento?
6. ¿Está permitido usar un programa propio externo que consuma esos datos exclusivamente para análisis y alertas, sin automatizar órdenes?
7. Si no hay API, ¿existe una función oficial para exportar o descargar datos históricos de velas?

No enviar al soporte ni a terceros contraseñas, cookies, SSID ni tokens de sesión para pedir orientación.

## 6. Criterios de aceptación antes de escribir un adaptador

No comenzar una conexión específica hasta obtener:

- Confirmación por escrito del método autorizado y sus límites.
- Especificación oficial de acceso, autenticación y formato de datos.
- Identificación verificable del instrumento, incluida la condición OTC.
- Reglas precisas de timestamps, zona horaria, cierre de vela y precisión.
- Permiso para usar el flujo desde una aplicación externa de análisis.
- Pruebas de frescura, desconexión, duplicados, orden temporal, huecos y velas inválidas.
- Comparación de las velas importadas con la terminal para medir diferencias observables.
- Aislamiento estricto de lectura y alertas frente a cualquier capacidad de ejecución.

## 7. Diseño de integración previsto para BPR-02

Si se aprueba la vía A o B, el adaptador se limitará a:

1. Recibir datos por el método documentado o importar el archivo oficial.
2. Normalizar cada vela cerrada al contrato existente de JSON Lines.
3. Añadir metadatos explícitos sobre fuente, instrumento, timeframe y condición OTC confirmada.
4. Detectar datos atrasados, huecos y cambios de identidad del flujo.
5. Pasar las velas validadas al puente de señales existente.
6. Mantener `order_sent: false` y exigir revisión manual.

**Estado de hoy:** investigación en curso; no se ha probado una conexión autenticada ni se ha confirmado un acceso oficial de lectura. No debe describirse BPR-02 como conectado a Pocket Option.
