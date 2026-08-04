# Arrest Report Agent

Bot agente de Discord para revisar reportes de arresto de roleplay en español. La identidad visible
por defecto es **Sgt. Vigil**, pero el repositorio, el paquete y la lógica mantienen un nombre neutral:
la identidad se inyecta desde `BOT_DISPLAY_NAME` y `BOT_ROLE_TITLE`.

El MVP está preparado para los canales privados `gnt-arrestos` y `dlm-arrestos` del servidor LSSD.
Solo actúa cuando un usuario habilitado menciona explícitamente al bot, en cualquier parte del
mensaje, o responde directamente a uno de sus mensajes. Las publicaciones que no se dirigen al bot,
los bots, los webhooks y otros servidores se ignoran sin llamar a OpenAI. Las invocaciones fuera de
los canales configurados o realizadas por usuarios no habilitados reciben una orientación
determinista, también sin ejecutar el agente.

## Qué hace

- Responde con naturalidad preguntas relacionadas con reportes, cargos, evidencia, procedimientos y
  correcciones previas, sin convertir cada mención en una revisión.
- Aplica primero un guardrail textual de alcance e intención; las consultas claramente ajenas
  reciben una orientación breve sin enviar imágenes al agente principal.
- Revisa la narrativa, evidencia visible y cargos de un reporte multimodal.
- Envía todas las imágenes seleccionadas, incluidas las fotografías frontal y lateral.
- Contrasta descripciones expresas de ropa y accesorios con flexibilidad por iluminación, ángulo,
  compresión y limitaciones gráficas; no infiere atributos sensibles.
- Consulta una guía local por secciones y un índice validado de 164 delitos descargados del PCU; la
  búsqueda de cargos permite seleccionar campos, combinar términos y tolerar typos cuando proceda.
- Mantiene el indicador nativo de escritura mientras procesa una solicitud.
- Muestra una presencia global priorizada: `⭐ De guardia`, `📻 Atendiendo consultas`,
  `🔎 Consultando el código` o `📂 Revisando expedientes`. Las ejecuciones paralelas no se pisan.
- Añade `⏳` desde un hook determinista cuando el preflight detecta una revisión con material; no
  expone esa operación como tool ni consume una ronda adicional del modelo.
- Puede enviar varios mensajes intermedios enriquecidos con Markdown de Discord mientras continúa
  trabajando y cerrar la conversación mediante una tool terminal explícita.
- Una mención nueva en un canal padre crea o reutiliza un hilo; una respuesta directa al bot en el
  canal permanece como reply y toda interacción iniciada dentro de un hilo se mantiene allí.
- Aprueba con `✅`, las correcciones usan `❌` y los casos inconclusos o técnicos usan `⚠️` con
  derivación manual opcional; el contenido se publica siempre en el destino ya autorizado.
- Reconstruye hasta 20 mensajes de un hilo, incluyendo respuestas, ediciones y correcciones recientes.
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
REVIEW_CHANNEL_IDS=1462992794569281617,1462992859761217745
AUTHORIZED_USER_IDS=*
BOT_IDLE_ACTIVITY=⭐ De guardia
BOT_CONVERSATION_ACTIVITY=📻 Atendiendo consultas
BOT_KNOWLEDGE_ACTIVITY=🔎 Consultando el código
BOT_REVIEW_ACTIVITY=📂 Revisando expedientes
OPENAI_MODEL=gpt-5.6-terra
OPENAI_REASONING_EFFORT=medium
```

Las tres allowlists comparten una sola regla: variable ausente, vacía o con `*` permite cualquier ID
del scope; una lista CSV como `123,456` restringe el acceso a esos IDs. Esto aplica a servidores
(`DISCORD_GUILD_IDS`), canales (`REVIEW_CHANNEL_IDS`) y usuarios (`AUTHORIZED_USER_IDS`). El nombre
anterior `DISCORD_GUILD_ID` sigue siendo compatible para no romper entornos existentes.

Una política previa única evalúa las tres antes de leer contexto o iniciar el Runner. Los servidores
no habilitados se ignoran; un canal o usuario no habilitado recibe una respuesta local breve, sin
consumo de OpenAI. Las tools posteriores no reciben IDs de destino libres: operan exclusivamente
sobre el mensaje y el hilo ya autorizados.

Antes de recomendar los canales permitidos, el bot los resuelve con Discord y comprueba que tanto él
como el solicitante puedan verlos. Los IDs eliminados, inaccesibles o de tipos no soportados se
omiten; si no queda un destino visible, entrega una orientación administrativa sin mostrar
referencias `#desconocido`. Los canales confirmados como eliminados se recuerdan hasta reiniciar el
proceso para evitar consultas REST repetidas.

El modelo y el nivel de razonamiento son la única selección de modelo requerida en el entorno. El
detalle de imagen, verbosity, límite de salida, service tier, capacidades admitidas y precios se
resuelven al iniciar desde el catálogo JSON interno. Una combinación desconocida detiene el arranque
con un error explícito en vez de aplicar valores silenciosos.

La identidad puede cambiarse sin tocar código ni prompts:

```dotenv
BOT_DISPLAY_NAME=Sgt. Vigil
BOT_ROLE_TITLE=Watch Sergeant
```

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

## Uso en Discord

1. Para una revisión, publica el nombre o identificador del sospechoso junto con el material habitual
   y solicita la revisión; también puedes hacer preguntas pertinentes sin adjuntar un expediente.
2. Menciona al bot en cualquier parte del mensaje.
3. El bot abrirá un hilo para esa solicitud. Dentro de él, vuelve a mencionarlo o responde uno de
   sus mensajes para continuar con el historial reciente.
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

Comandos disponibles:

- `/wv help`: presentación, instrucciones y enlaces oficiales visibles para todo el canal.

## Configuración relevante

| Variable | Predeterminado | Función |
|---|---:|---|
| `DISCORD_GUILD_IDS` | `*` | Servidores permitidos; acepta CSV. `DISCORD_GUILD_ID` queda como alias compatible. |
| `REVIEW_CHANNEL_IDS` | `*` | Canales padre permitidos; acepta CSV y cubre sus hilos. |
| `AUTHORIZED_USER_IDS` | `*` | `*`/vacío permite a todos; una lista CSV limita la ejecución a esos usuarios. |
| `REVIEW_ESCALATION_ROLE_IDS` | vacío | Roles existentes que reciben una mención ante resultados inconclusos o fallas; acepta CSV. |
| `BOT_IDLE_ACTIVITY` | `⭐ De guardia` | Presencia global sin solicitudes activas. |
| `BOT_CONVERSATION_ACTIVITY` | `📻 Atendiendo consultas` | Presencia inicial de una consulta habilitada. |
| `BOT_KNOWLEDGE_ACTIVITY` | `🔎 Consultando el código` | Presencia al consultar la guía o el catálogo. |
| `BOT_REVIEW_ACTIVITY` | `📂 Revisando expedientes` | Presencia de una revisión con material. `BOT_BUSY_ACTIVITY` queda como alias compatible. |
| `LOG_JSON_INDENTED` | `true` | Usa JSON indentado; `false` genera un objeto compacto por línea. |
| `OPENAI_MODEL` | `gpt-5.6-terra` | Modelo del runner. |
| `OPENAI_REASONING_EFFORT` | `medium` | Esfuerzo validado contra los valores admitidos por el modelo. |
| `OPENAI_MAX_RETRIES` | `2` | Reintentos del proveedor. |
| `REVIEW_TIMEOUT_SECONDS` | `180` | Tiempo total máximo de una revisión. |
| `MAX_CONCURRENT_REVIEWS` | `2` | Límite global en memoria. |
| `MAX_IMAGES_PER_REVIEW` | `10` | Máximo multimodal por ejecución. |
| `MAX_FILES_PER_REVIEW` | `5` | Máximo de documentos compatibles por ejecución. |
| `MAX_FILE_BYTES` | `10485760` | Tamaño máximo de cada archivo enviado al runner. |
| `MAX_TOTAL_FILE_BYTES` | `52428800` | Máximo combinado para archivos en una ejecución. |
| `THREAD_CONTEXT_MAX_MESSAGES` | `20` | Historial reciente reconstruido. |
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

Las pruebas no llaman a Discord ni a OpenAI. Cubren catálogo, configuración, trigger, contexto
multimodal y documental, efectos inmediatos de las tools, embeds, clasificación de errores, metadata
y costos.

## Límites del MVP

- No hay base de datos: intentos, locks y estado operativo se pierden al reiniciar.
- Los canales, usuarios habilitados, servidor y roles de escalamiento se configuran por entorno, no mediante
  comandos persistentes.
- El catálogo y la guía son archivos versionados; todavía no existe sincronización con OpenMP/PCU.
- No existe configuración por facción o tenant.
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
