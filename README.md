# Arrest Report Agent

Bot agente de Discord para revisar reportes de arresto de roleplay en español. El perfil incluido es
**Walter Vigil**, pero el repositorio, el paquete y la lógica mantienen un nombre neutral: identidad,
personalidad, presencia y perfil de Discord se cargan desde la persona elegida con `BOT_PERSONA`.

El MVP usa `gnt-arrestos` y `dlm-arrestos` como archivo de reportes del servidor LSSD, pero puede
conversar desde cualquier canal de una guild habilitada. Solo actúa cuando un usuario autorizado
menciona explícitamente al bot, en cualquier parte del mensaje, o responde directamente a uno de sus
mensajes. Las publicaciones que no se dirigen al bot, los bots, los webhooks y otros servidores se
ignoran sin llamar a OpenAI. Los usuarios no habilitados reciben una orientación determinista,
también sin ejecutar el agente.

## Qué hace

- Responde con naturalidad preguntas relacionadas con reportes, cargos, evidencia, procedimientos y
  correcciones previas, sin convertir cada mención en una revisión.
- Aplica primero un guardrail textual de alcance e intención; las consultas claramente ajenas
  reciben una orientación breve sin enviar imágenes al agente principal.
- Revisa la narrativa, evidencia visible y cargos de un reporte multimodal.
- Señala cargos no sustentados como advertencias informativas, pero no bloquea la aprobación por un
  cargo ya inmutable; solo las deficiencias corregibles del reporte pueden exigir correcciones.
- Interpreta la búsqueda de arrestos como un historial: valida el nombre del personaje y considera
  solo filas temporalmente asociables al procedimiento, tolerando demoras y cruces de medianoche.
- Respeta revisiones completas, solo de narrativa, solo de cargos o enfocadas en puntos concretos.
  Una aprobación parcial se identifica en el embed y no añade el ✅ terminal del expediente.
- Envía todas las imágenes seleccionadas, incluidas las fotografías frontal y lateral.
- Contrasta descripciones expresas de ropa y accesorios con flexibilidad por iluminación, ángulo,
  compresión y limitaciones gráficas; no infiere atributos sensibles.
- Consulta una guía local por secciones y un índice validado de 164 delitos descargados del PCU; la
  búsqueda de cargos permite seleccionar campos, combinar términos y tolerar typos cuando proceda.
- Mantiene el indicador nativo de escritura mientras procesa una solicitud.
- Muestra una presencia global priorizada: `⭐ De guardia`, `📻 Atendiendo consultas`,
  `🔎 Consultando el código` o `📂 Revisando expedientes`. Las ejecuciones paralelas no se pisan.
- Al iniciar, sincroniza por separado el nombre y avatar de la personalidad, el banner global LSSD
  y la biografía por servidor; una falla cosmética se registra, no bloquea los demás campos y no
  impide que el bot atienda.
- Añade `⏳` desde un hook determinista cuando el preflight detecta una revisión con material; no
  expone esa operación como tool ni consume una ronda adicional del modelo.
- Puede enviar varios mensajes intermedios enriquecidos con Markdown de Discord mientras continúa
  trabajando y cerrar la conversación mediante una tool terminal explícita.
- Ante una mención nueva, una respuesta breve puede quedar como reply y una conversación extensa o
  revisión usa un hilo; un reply al bot permanece como reply y toda interacción iniciada dentro de
  un hilo se mantiene allí.
- Aprueba con `✅`, las correcciones usan `❌` y los casos inconclusos o técnicos usan `⚠️` con
  derivación manual opcional; el contenido se publica siempre en el destino ya autorizado.
- Reconstruye hasta 20 mensajes de un hilo, incluyendo respuestas, ediciones y correcciones recientes.
- Si el límite inicial omite adjuntos históricos, puede recuperar bajo demanda imágenes o documentos
  de un mensaje visible antes de emitir el veredicto, sin aceptar URLs ni servidores arbitrarios.
- Incluye el nombre visible y hasta ocho títulos de rol relevantes del solicitante para adaptar el
  tratamiento; no envía IDs de roles y nunca altera el rigor de la revisión por jerarquía.
- Conserva metadata útil en traces sin incluir contenidos sensibles por defecto.

## Requisitos

- Python 3.12–3.14.
- Una aplicación de Discord con bot y **Message Content Intent** habilitado.
- Una API key de OpenAI con acceso al modelo configurado.
- Permisos efectivos en cada canal privado; consulta [Permisos](docs/PERMISSIONS.md).

## Configuración local

```bash
cp default.env .env
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Completa al menos `DISCORD_TOKEN` y `OPENAI_API_KEY` en `.env`. Los valores por defecto ya incluyen:

```dotenv
DISCORD_GUILD_IDS=1234674521416663070
REPORT_CHANNEL_IDS=1462992794569281617,1462992859761217745
AUTHORIZED_USER_IDS=*
ROLE_REQUEST_ALLOWED_CHANNEL_IDS=*
ROLE_REQUEST_REVIEWER_ROLE_IDS=
BOT_PERSONA=walter_vigil
OPENAI_MODEL=gpt-5.6-terra
OPENAI_REASONING_EFFORT=medium
```

Las allowlists de acceso comparten una sola regla: variable ausente, vacía o con `*` permite
cualquier ID del scope; una lista CSV como `123,456` lo restringe. Esto aplica a servidores
(`DISCORD_GUILD_IDS`) y usuarios (`AUTHORIZED_USER_IDS`). Dentro de una guild autorizada, cualquier
usuario habilitado puede activar al bot mediante una mención o una respuesta directa desde cualquier
canal visible para el bot.

`REPORT_CHANNEL_IDS` no restringe conversaciones. Define los canales que `/mis-reportes`,
`/mi-resumen`, `/pendientes`, `/ranking`, `/verguenza` y `/honor` utilizan como archivo de reportes y los que `/ayuda`
presenta como destinos habituales. `ALLOWED_CHANNEL_IDS` y `REVIEW_CHANNEL_IDS` siguen siendo aliases
de migración. Al mostrarlos, el bot conserva únicamente los que pertenecen a la guild actual y son
visibles para él y para el solicitante, evitando referencias `#desconocido` entre servidores.

El subsistema de solicitudes de roles se habilita al configurar uno o más IDs explícitos en
`ROLE_REQUEST_REVIEWER_ROLE_IDS`. `/solicitar-roles` publica la solicitud en el mismo canal de texto
donde se ejecuta; `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` puede abrir todos esos canales con `*` o
restringirlos mediante CSV. No aplica `AUTHORIZED_USER_IDS`, no funciona dentro de hilos y no usa
base de datos ni IA: el mensaje versionado de Discord conserva la solicitud y sus botones
persistentes. `/info-roles` permanece disponible en cualquier canal o hilo de una guild habilitada.

La política previa valida guild, autor y usuario antes de leer contexto o iniciar el Runner. Las
tools de escritura no reciben IDs de destino libres: operan exclusivamente sobre el mensaje y el
hilo ya autorizados. Las consultas de mensajes o adjuntos quedan fijadas a la guild y a los permisos
de lectura del solicitante. El agente solo puede escoger `reply` o `thread` para una mención nueva;
una revisión estructurada se publica en hilo y la primera respuesta fija la ruta de toda esa ejecución.

El modelo y el nivel de razonamiento son la única selección de modelo requerida en el entorno. El
detalle de imagen, verbosity, límite de salida, service tier, capacidades admitidas y precios se
resuelven al iniciar desde el catálogo JSON interno. Una combinación desconocida detiene el arranque
con un error explícito en vez de aplicar valores silenciosos.

La identidad se selecciona con una sola variable obligatoria:

```dotenv
BOT_PERSONA=walter_vigil
```

Los perfiles
[`walter_vigil.json`](src/arrest_report_agent/config/personas/walter_vigil.json) y
[`roy_velazco.json`](src/arrest_report_agent/config/personas/roy_velazco.json) definen nombre,
organización, tratamiento, tono, presencia, biografía, referencia al avatar y un mapa versionado de
mensajes deterministas. Este último personaliza ayuda, restricciones, redirecciones, fallas seguras,
descripciones de comandos, selectores, modales, navegación, resultados y estados vacíos sin invocar
al modelo. Los nombres y descripciones que provienen del código penal o de la guía, junto con los
hallazgos técnicos de una revisión, permanecen neutrales. Todo se
valida y carga una sola vez antes de conectar con Discord. Un ID ausente, desconocido o inseguro
detiene el arranque en vez de aplicar una identidad parcial. Para usar a Roy se configura
`BOT_PERSONA=roy_velazco`. La sincronización modifica el usuario bot; el nombre e icono de la
aplicación mostrados en Developer Portal siguen siendo configuración de Discord.

Cada personalidad distingue la voz de conversación de la voz breve usada en el resumen superior
de una revisión. Los hallazgos, cargos, correcciones y motivos de derivación se redactan en español
formal y neutral, sin trasladar insultos, bromas o modismos de la personalidad al contenido técnico.
Ambos registros incluyen pocos ejemplos tipados y acotados que se inyectan como referencias de voz,
no como respuestas fijas.

El banner no pertenece a una personalidad: todas comparten el asset global verde musgo
`config/assets/lssd-profile-banner.png`.

`REVIEW_ESCALATION_ROLE_IDS` es una lista CSV opcional de roles que reciben una sola mención ante
una revisión inconclusa o una falla técnica. Solo se mencionan roles que todavía existan en la guild;
los IDs eliminados se omiten sin interrumpir el manejo del error. `MANUAL_REVIEW_ROLE_ID` sigue
siendo compatible como nombre legado para una configuración de un solo rol. Para que Discord envíe
la notificación sin conceder al bot el permiso amplio de mencionar cualquier rol, los roles de
escalamiento deben estar configurados como mencionables.

## Ejecución

```bash
.venv/bin/arrest-report-agent
```

Con Docker:

```bash
docker compose up --build -d
```

El proceso expone `GET /health` en el puerto 8080. Devuelve `200` cuando Discord está conectado y
las fuentes locales de conocimiento están cargadas; durante el arranque devuelve `503`.

### Railway

El repositorio incluye [`railway.json`](railway.json) para ejecutar una única réplica permanente
desde el `Dockerfile`, sin serverless, con reinicio automático, apagado gradual y healthcheck en
`/health`. Railway inyecta `PORT`; para Docker local se mantiene `HEALTH_PORT=8080`.

El servicio de producción debe vincularse al repositorio de GitHub y a la rama `main`. Los pushes a
esa rama ejecutan primero el workflow de CI en GitHub Actions; habilita **Wait for CI** en Railway
para que un commit con checks fallidos quede omitido y no reemplace el despliegue activo.

Carga las variables desde el archivo local ignorado `.env.production` en la sección **Variables**
del servicio. No subas ese archivo, `DISCORD_TOKEN` ni `OPENAI_API_KEY` al repositorio. El bot no
necesita dominio público, volumen ni base de datos para este MVP.

## Uso en Discord

1. Para una revisión, publica el nombre o identificador del sospechoso junto con el material habitual
   y solicita la revisión; también puedes hacer preguntas pertinentes sin adjuntar un expediente.
2. Menciona al bot en cualquier parte del mensaje.
3. Una revisión abrirá un hilo. Una consulta breve puede responderse como reply; si requiere una
   conversación extensa, el bot puede abrir un hilo. Dentro de él, vuelve a mencionarlo o responde
   uno de sus mensajes para continuar con el historial reciente.
4. Si respondes directamente a un mensaje del bot en el canal padre, contestará mediante otro reply
   sin abrir un hilo.

El agente no exige cuatro imágenes ni un orden fijo. Puede reconocer fotografías de personaje,
capturas de cargos, narrativa en el juego, un formulario, texto escrito directamente en Discord o
archivos compatibles con el runner (PDF, documentos, planillas, presentaciones, texto y código).
El historial se reconstruye como turnos nativos: solamente los mensajes de esta instancia del bot
son `assistant`; miembros, webhooks y otros bots permanecen como `user`. Cada adjunto conserva el
turno de Discord donde fue publicado.
Solo activa el estado y el dictamen de revisión cuando la intención y el material disponible lo
justifican; si faltan antecedentes, los solicita mediante una respuesta normal.
También puede consultar de forma explícita metadata pública acotada de miembros visibles en el
servidor actual, sin enumerar la guild ni acceder a otros servidores.
Cuando necesita ampliar contexto, puede leer ventanas acotadas de mensajes en canales visibles para
el solicitante, usando un mensaje ancla, dirección, filtros y proyección seleccionados por el agente.
La lectura devuelve metadata compacta de los adjuntos; cuando necesita ver su contenido, una segunda
tool puede cargar selectivamente los adjuntos del mensaje como inputs nativos de imagen o archivo.

Comandos disponibles:

- `/ayuda`: presentación, instrucciones y enlaces oficiales visibles para todo el canal.
- `/guia`: referencia privada y navegable de las secciones relevantes del manual de reportes.
- `/cargos`: modal privado para buscar por código, título, descripción o categoría en el catálogo
  penal local; muestra detalles y sanciones con paginación cuando corresponde.
- `/faq`: preguntas frecuentes privadas y navegables, curadas a partir de la guía local.
- `/verguenza`: abre un modal para elegir mes actual o los últimos 7, 30 o 90 días, y publica por
  cada canal configurado un top 10 de quienes acumulan más expedientes que todavía no alcanzan el
  ✅; muestra una leyenda y separa los expedientes sin revisión (`📭`) de las revisiones activas
  (`⏳`), correcciones (`❌`) e inconclusos (`⚠️`).
- `/honor`: abre el mismo selector de periodo y publica por estación un top 10 de miembros con al
  menos un reporte aprobado, ordenado por cantidad de ✅, porcentaje de aprobación y volumen.
- `/pendientes`: consulta de supervisión para expedientes sin estado terminal o en revisión.
- `/ranking`: ranking de supervisión por volumen, aprobaciones, correcciones o tasa de aprobación.
- `/mis-reportes`: abre un modal privado para filtrar por periodo, estado y estación; entrega
  referencias directas a cada publicación y a su hilo cuando existe.
- `/mi-resumen`: muestra de forma privada el estado personal del mes actual.
- `/solicitar-roles`: abre un formulario privado para solicitar hasta diez altas y diez bajas;
  publica un expediente en el canal de origen para aprobación completa, parcial o rechazo.
- `/info-roles`: publica una explicación breve sobre el uso y los resultados de las solicitudes.

Los comandos de guía, cargos, FAQ, vergüenza, honor, roles y consultas personales son deterministas:
no ejecutan el runner ni consumen OpenAI. Los comandos personales consultan un máximo acotado del historial de los canales
configurados, cuentan solo publicaciones raíz del usuario con adjuntos o estado asignado y leen las
reacciones (`⏳`, `✅`, `❌`, `⚠️`) puestas por esta instancia o por miembros que cumplan
`SUPERVISOR_ROLE_IDS`. Los resultados representan el estado actual de Discord, no un historial de
cambios.

Aunque su lógica y sus datos son deterministas, todo el texto visual que rodea esas consultas se toma
de `messages.interface` en la persona activa. Esto incluye opciones generales, placeholders, títulos,
botones, enlaces, paginación y resúmenes.

Consulta [Comandos de Discord](docs/COMMANDS.md) para conocer acceso, navegación, fuentes, límites y
el contenido exacto del FAQ.

## Configuración relevante

| Variable | Predeterminado | Función |
|---|---:|---|
| `DISCORD_GUILD_IDS` | `*` | Servidores permitidos; acepta CSV. `DISCORD_GUILD_ID` queda como alias compatible. |
| `REPORT_CHANNEL_IDS` | `*` | Canales usados por búsquedas, resúmenes, rankings, supervisión y ayuda; no restringe menciones. `ALLOWED_CHANNEL_IDS` y `REVIEW_CHANNEL_IDS` quedan como aliases compatibles. |
| `AUTHORIZED_USER_IDS` | `*` | `*`/vacío permite a todos; una lista CSV limita la ejecución a esos usuarios. |
| `SUPERVISOR_ROLE_IDS` | `*` | Roles autorizados para comandos de supervisión y para asignar estados manuales reconocidos por las consultas; `*`/vacío permite a todos. |
| `REVIEW_ESCALATION_ROLE_IDS` | vacío | Roles existentes que reciben una mención ante resultados inconclusos o fallas; acepta CSV. |
| `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` | `*` | Canales de texto normales donde puede ejecutarse `/solicitar-roles`; `*`/vacío permite todos y un CSV restringe. Los hilos siempre se rechazan. |
| `ROLE_REQUEST_REVIEWER_ROLE_IDS` | vacío | Roles que pueden resolver solicitudes. Requiere IDs explícitos, no admite `*`; vacío deshabilita ambos comandos de roles. |
| `BOT_PERSONA` | obligatorio | ID del perfil empaquetado que controla identidad, tono, presencia y perfil de Discord. |
| `LOG_JSON_INDENTED` | `true` | Usa JSON indentado; `false` genera un objeto compacto por línea. |
| `PORT` | Railway | Puerto inyectado por Railway para el healthcheck; tiene prioridad sobre `HEALTH_PORT`. |
| `OPENAI_MODEL` | `gpt-5.6-terra` | Modelo del runner. |
| `OPENAI_REASONING_EFFORT` | `medium` | Esfuerzo validado contra los valores admitidos por el modelo. |
| `OPENAI_MAX_RETRIES` | `2` | Reintentos del proveedor. |
| `REVIEW_TIMEOUT_SECONDS` | `180` | Tiempo total máximo de una revisión. |
| `MAX_CONCURRENT_REVIEWS` | `2` | Límite global en memoria. |
| `MAX_IMAGES_PER_REVIEW` | `10` | Máximo multimodal por ejecución. |
| `MAX_IMAGE_BYTES` | `10485760` | Tamaño máximo de cada imagen enviada al runner. |
| `MAX_FILES_PER_REVIEW` | `5` | Máximo de documentos compatibles por ejecución. |
| `MAX_FILE_BYTES` | `10485760` | Tamaño máximo de cada archivo enviado al runner. |
| `MAX_TOTAL_FILE_BYTES` | `52428800` | Máximo combinado para archivos en una ejecución. |
| `CHANNEL_CONTEXT_MAX_MESSAGES` | `5` | Mensajes de texto anteriores del canal usados como contexto ambiental; `0` lo desactiva. |
| `THREAD_CONTEXT_MAX_MESSAGES` | `20` | Historial reciente reconstruido. |
| `THREAD_CONTEXT_MAX_HISTORICAL_IMAGES` | `2` | Imágenes previas incluidas inicialmente; las omitidas pueden recuperarse bajo demanda. |
| `THREAD_CONTEXT_MAX_HISTORICAL_FILES` | `2` | Archivos previos reutilizados como contexto. |
| `OPENAI_TRACING_ENABLED` | `true` | Habilita traces del Agents SDK. |
| `OPENAI_TRACE_INCLUDE_SENSITIVE_DATA` | `false` | Excluye mensajes, imágenes, archivos y tool payloads del trace. |

El catálogo versionado
[`models.json`](src/arrest_report_agent/config/data/models.json) contiene las capacidades, defaults y
tarifas del modelo soportado. El runtime calcula el costo estimado desde los contadores reales del
Runner, incluyendo entrada normal, entrada cacheada, escrituras de caché y salida. Los tokens de
razonamiento se registran por separado, pero no se cobran dos veces porque ya forman parte de los
tokens de salida. Estas cifras son estimaciones operativas y no reemplazan la facturación de OpenAI.

## Calidad y pruebas

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy
```

La suite predeterminada no llama a Discord ni a OpenAI. Cubre catálogo, configuración, trigger,
contexto multimodal y documental, efectos inmediatos de las tools, embeds, clasificación de errores,
metadata y costos.

También existe una suite de evaluaciones reales del agente, marcada como `ai_eval` y omitida por
defecto para impedir consumo accidental. Usa el prompt, guardrail, Runner, modelo y tools de
producción, pero reemplaza Discord por un publicador en memoria. Requiere una `OPENAI_API_KEY` válida
en el entorno o en `.env` y se habilita de forma explícita:

```bash
# Ejecutar un solo comportamiento para controlar el gasto.
.venv/bin/pytest tests/ai_eval -k spelling --run-ai-evals -q

# Ejecutar todos los casos; cada uno consume créditos.
.venv/bin/pytest tests/ai_eval --run-ai-evals -q
```

## Límites del MVP

- No hay base de datos: intentos, locks y estado operativo se pierden al reiniciar.
- Las consultas personales reconstruyen un snapshot desde mensajes y reacciones conservados en
  Discord; no pueden recuperar publicaciones eliminadas ni recordar correcciones anteriores.
- Los canales, usuarios habilitados, servidor y roles de escalamiento se configuran por entorno, no
  mediante comandos persistentes.
- El catálogo y la guía son archivos versionados; todavía no existe sincronización con OpenMP/PCU.
- No existe configuración por facción o tenant.
- Se incluyen `walter_vigil` y `roy_velazco`; cambiar la persona requiere reiniciar y todavía no
  existe carga desde base de datos ni configuración distinta por servidor.
- No se ejecuta una segunda revisión automática ni un fallback de veredicto con otro modelo.

La separación entre contexto Discord, dominio, conocimiento y runner permite añadir más adelante una
base de datos, configuración por servidor/facción, catálogo sincronizado mediante API y políticas
distintas para LSSD, LSPD u otras organizaciones. Consulta [Arquitectura](docs/ARCHITECTURE.md).

## Privacidad y observabilidad

Los logs contienen identificadores técnicos, latencia, modelo, usage crudo, desglose de costo
estimado y veredicto. No registran nombres, narrativa, OCR, URLs de adjuntos ni respuestas completas.
Los traces usan IDs de servidor, canal, hilo, mensajes y usuario para depuración, con payload sensible
desactivado.
Consulta [Privacidad y errores](docs/PRIVACY_AND_ERRORS.md).
