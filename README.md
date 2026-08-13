# Arrest Report Agent

Bot agente de Discord para revisar reportes de arresto de roleplay en español. El perfil incluido es
**Walter Vigil**, pero el repositorio, el paquete y la lógica mantienen un nombre neutral. PostgreSQL
almacena configuraciones versionadas por guild y personalidades; Railway Bucket conserva sus avatares.
El runtime toma un snapshot inmutable por interacción y recibe publicaciones nuevas mediante
`LISTEN/NOTIFY`, sin reiniciar ni redesplegar el bot.

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
- PostgreSQL 14 o superior y un Bucket compatible con S3.
- Permisos efectivos en cada canal privado; consulta [Permisos](docs/PERMISSIONS.md).

## Configuración local

```bash
cp default.env .env
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Completa los secretos de Discord, OpenAI, PostgreSQL y Bucket en `.env`. Para una base nueva, los
valores legacy siguientes sirven exclusivamente como entrada del importador inicial:

```dotenv
DISCORD_GUILD_IDS=1234674521416663070
REPORT_CHANNEL_IDS=1462992794569281617,1462992859761217745
AUTHORIZED_USER_IDS=*
ROLE_REQUEST_ALLOWED_CHANNEL_IDS=*
ROLE_REQUEST_ALLOWED_ROLE_IDS=*
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
`ROLE_REQUEST_REVIEWER_ROLE_IDS`. `ROLE_REQUEST_ALLOWED_ROLE_IDS` sigue la regla uniforme de las
allowlists: ausente, vacío o `*` permite todos los roles seguros; un CSV define un catálogo cerrado.
`/solicitar-roles` publica la solicitud en el mismo canal de texto
donde se ejecuta; `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` puede abrir todos esos canales con `*` o
restringirlos mediante CSV. No aplica `AUTHORIZED_USER_IDS`, no funciona dentro de hilos y no usa
IA ni persiste la solicitud en PostgreSQL: el mensaje versionado de Discord conserva la solicitud y
sus botones persistentes. PostgreSQL solo aporta su configuración dinámica. `/info-roles` permanece
disponible en cualquier canal o hilo de una guild habilitada.

El módulo determinista de pagers persiste sus avisos, múltiples roles destinatarios y respondientes
en PostgreSQL. `/pager` emite el aviso en el canal autorizado, `/pager-activos` enlaza los avisos
abiertos, `/mis-pagers` entrega el historial privado del usuario y `/info-pager` publica una guía breve.
El emisor no puede registrarse además como respondiente. Los controles persistentes
permiten responder, retirarse, editar, concluir o cancelar incluso después de reiniciar el proceso.

Las analíticas globales `/verguenza` y `/honor` usan una política independiente
`report_analytics_roles`. Solo sus roles staff autorizados pueden generarlas, aunque el resultado
continúa siendo público para el canal. `/pendientes` y `/ranking` mantienen su política separada de
supervisión.

La política previa valida guild, autor y usuario antes de leer contexto o iniciar el Runner. Las
tools de escritura operan exclusivamente dentro de la conversación autorizada. La tool de reacción
acepta un ID presente en ese contexto, pero no una guild o canal libre y no puede salir del mensaje
raíz, canal o hilo activo. Las consultas de mensajes o adjuntos quedan fijadas a la guild y a los permisos
de lectura del solicitante. El agente solo puede escoger `reply` o `thread` para una mención nueva;
una revisión estructurada se publica en hilo y la primera respuesta fija la ruta de toda esa ejecución.

El directorio agéntico de Discord tampoco acepta `guild_id`: puede descubrir roles, buscar miembros
con filtros combinables y consultar audiencia o participantes de un canal, pero siempre dentro de la
guild del mensaje. Los canales elegidos deben pertenecer a ella y ser visibles para el solicitante.
Cada llamada proyecta solo los campos solicitados, devuelve como máximo 25 resultados y pagina con
un cursor opaco para evitar volcar directorios completos al modelo.

El modelo y el nivel de razonamiento son la única selección de modelo requerida en el entorno. El
detalle de imagen, verbosity, límite de salida, service tier, capacidades admitidas y precios se
resuelven al iniciar desde el catálogo JSON interno. Una combinación desconocida detiene el arranque
con un error explícito en vez de aplicar valores silenciosos.

Durante la migración inicial, la identidad se selecciona con:

```dotenv
BOT_PERSONA=walter_vigil
```

Después de importar, `BOT_PERSONA` deja de ser parte del runtime. Los perfiles
[`walter_vigil.json`](src/arrest_report_agent/config/personas/walter_vigil.json) y
[`roy_velazco.json`](src/arrest_report_agent/config/personas/roy_velazco.json) definen nombre,
organización, tratamiento, tono, presencia, biografía, referencia al avatar y un mapa versionado de
mensajes deterministas. Este último personaliza ayuda, restricciones, redirecciones, fallas seguras,
descripciones de comandos, selectores, modales, navegación, resultados y estados vacíos sin invocar
al modelo. Los nombres y descripciones que provienen del código penal o de la guía, junto con los
hallazgos técnicos de una revisión, permanecen neutrales. Todo se
validan al importarlos y cada revisión publicada permanece inmutable. Una revisión ausente,
desconocida o insegura se rechaza antes de publicarse. La asignación por guild y el perfil global se
administran mediante el panel privado `/config`. La sincronización modifica el usuario bot; el nombre e icono de la
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

### Inicialización de PostgreSQL

La primera migración se ejecuta de forma explícita y nunca arranca el gateway de Discord:

```bash
.venv/bin/arrest-report-agent db migrate
.venv/bin/arrest-report-agent db import-env --dry-run
.venv/bin/arrest-report-agent db import-env --apply
.venv/bin/arrest-report-agent db verify
```

El dry run consulta Discord en modo lectura para distribuir canales y roles entre guilds, pero no
modifica PostgreSQL ni el Bucket. `--apply` importa Walter, Roy, sus avatares y una primera revisión
por guild. La operación registra `bootstrap_env_v1` y una segunda ejecución con el mismo checksum es
un no-op; reemplazar una importación diferente exige `--replace`.

Una vez verificado el corte, el runtime ya no consulta las allowlists, modelo ni persona desde env.
Los cambios se realizan desde el panel efímero `/config` sobre un borrador y solo afectan nuevas
interacciones al publicarlo de forma atómica. La última revisión válida permanece en memoria durante una caída
temporal de PostgreSQL; mientras tanto, las mutaciones administrativas quedan bloqueadas.

El proceso expone `GET /health` en el puerto 8080. Devuelve `200` cuando Discord, PostgreSQL y las
fuentes locales de conocimiento están disponibles; durante el arranque devuelve `503`. El mismo
servidor HTTP puede recibir eventos de Railway sin crear otro servicio.

### Railway

El repositorio incluye [`railway.json`](railway.json) para ejecutar una única réplica desde el
`Dockerfile`, aplicar Alembic como pre-deploy, reiniciar automáticamente, apagar de forma gradual y
usar `/health`. Railway inyecta `PORT`; para Docker local se mantiene `HEALTH_PORT=8080`.

Un bot de Discord necesita una conexión WebSocket permanente. En Railway Hobby deshabilita el modo
serverless (**App Sleeping**) desde la configuración del servicio. El plan Free obliga a mantenerlo
activado y puede desconectar al bot cuando la aplicación entra en reposo.

El servicio de producción se vincula al repositorio de GitHub y a la rama `main`, por lo que cada
push inicia automáticamente un nuevo despliegue. Esa rama también ejecuta el workflow de CI en
GitHub Actions; habilita **Wait for CI** en Railway para que un commit con checks fallidos quede
omitido y no reemplace el despliegue activo.

Carga únicamente secretos y opciones de proceso en **Variables**. Vincula `DATABASE_URL` al servicio
PostgreSQL y las credenciales S3 al Bucket mediante referencias de Railway. No subas `.env.production`,
tokens, URL de base ni credenciales del Bucket. Los env legacy pueden retirarse después de ejecutar
`db verify` con éxito.

### Changelog y alertas de despliegue

Cada versión se define una sola vez en `arrest_report_agent.__version__` y debe coincidir con el
manifest estricto [`release.json`](src/arrest_report_agent/release.json). El manifest contiene el
título, resumen y hasta diez cambios tipados; no contiene copy de una persona.

El receptor se habilita únicamente cuando están presentes `RAILWAY_WEBHOOK_SECRET` y los IDs del
proyecto, entorno y servicio. La URL que se configura en Railway es:

```text
https://<dominio-publico>/webhooks/railway/<RAILWAY_WEBHOOK_SECRET>
```

Acepta `Deployment.deployed`, `Deployment.failed`, `Deployment.crashed` y
`Deployment.oom_killed`. Verifica el secreto con comparación constante, limita el cuerpo, valida el
JSON y exige que los tres IDs coincidan y que el entorno no sea efímero. CORS se habilita únicamente
para el botón de prueba del dashboard de Railway; no sustituye al secreto.

El manifest también declara la URL confiable del repositorio. Los embeds convierten el SHA corto en
un enlace al commit únicamente cuando Railway identifica GitHub como origen y el SHA completo tiene
un formato hexadecimal válido; nunca se acepta una URL proporcionada por el webhook.

Los eventos válidos y las entregas quedan en PostgreSQL. En un despliegue exitoso, la nueva instancia
solo anuncia el changelog cuando `RAILWAY_GIT_COMMIT_SHA` coincide con el commit del evento. Cada
canal y versión tiene una clave idempotente, y los errores transitorios de Discord se reintentan con
backoff. Las alertas de fallo no publican secretos, logs ni mensajes de commit.

Para activarlo en una guild, usa `/config`, crea un borrador, configura **Canales de despliegues** en
modo restringido, habilita **Notificaciones de despliegue** con schema `1` y opciones `{}`, y publica
el borrador. Hasta entonces la migración lo deja explícitamente deshabilitado.

### Buzón de sugerencias

`/sugerencia` está disponible en cualquier canal o hilo de una guild configurada. El formulario
solicita una categoría visual, un título breve y una descripción; la confirmación es efímera y el
contenido nunca se publica en el canal. Cada usuario puede mantener hasta tres sugerencias pendientes
por guild y debe esperar 60 segundos entre envíos. La cuota y el cooldown se validan dentro de una
transacción PostgreSQL para impedir que solicitudes simultáneas los evadan.

`/sugerencias` presenta únicamente a los IDs de `CONFIG_OWNER_USER_IDS` un buzón efímero con cinco
entradas por página, selector y detalle completo. `/estado-sugerencia` abre un formulario privado para
marcar una referencia `SUG-00012` como pendiente o archivada; archivar libera un cupo del autor. Cada
alta y transición queda en un historial inmutable. `/info-sugerencias` es público, conciso y no revela
los comandos administrativos.

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
- `/ts3`: publica para todo el canal la dirección `51.81.49.72:11189` del servidor TeamSpeak 3.
- `/pendientes`: consulta de supervisión para expedientes sin estado terminal o en revisión.
- `/ranking`: ranking de supervisión por volumen, aprobaciones, correcciones o tasa de aprobación.
- `/mis-reportes`: abre un modal privado para filtrar por periodo, estado y estación; entrega
  referencias directas a cada publicación y a su hilo cuando existe.
- `/mi-resumen`: muestra de forma privada el estado personal del mes actual.
- `/solicitar-roles`: abre un formulario privado para solicitar hasta diez altas y diez bajas;
  publica un expediente en el canal de origen para aprobación completa, parcial o rechazo.
- `/info-roles`: publica una explicación breve sobre el uso y los resultados de las solicitudes.
- `/pager`: abre un formulario privado y emite un pager en el canal actual; reutiliza el callsign
  registrado sin volver a solicitarlo.
- `/pager-activos`: publica los pagers abiertos con enlaces directos.
- `/mis-pagers`: consulta de forma privada los pagers creados o respondidos.
- `/info-pager`: publica una explicación breve del sistema.
- `/callsign`: registra o reemplaza de forma privada el callsign operativo del miembro; requiere un
  rol configurado como personal operativo y acepta formatos como `280L`, `60F`, `282B` o `281S`.
- `/quitar-callsign`: elimina de forma privada el callsign guardado; los formularios de pager vuelven
  a solicitar un valor libre.
- `/callsigns`: publica para el personal operativo el directorio paginado, agrupado por callsign y
  sin notificar a los miembros referenciados.
- `/info-callsigns`: explica públicamente cómo registrar y consultar callsigns, y cómo se reutilizan
  al emitir o responder un pager.
- `/sugerencia`: abre un formulario privado con categoría, título y descripción; registra la idea en
  PostgreSQL sin publicar su contenido en el canal.
- `/info-sugerencias`: publica una explicación breve de las categorías, el límite y el uso responsable.
- `/sugerencias`: abre el buzón efímero y paginado; está reservado a `CONFIG_OWNER_USER_IDS`.
- `/estado-sugerencia`: cambia de forma efímera una sugerencia entre pendiente y archivada; está
  reservado a `CONFIG_OWNER_USER_IDS`.
- `/config`: abre un panel efímero sin argumentos, reservado a owners o roles `config_admin_roles`;
  permite inspeccionar la revisión activa y administrar borradores, accesos, agente, módulos,
  personas, publicación y rollback mediante botones y formularios. Los intentos no autorizados
  reciben una alerta efímera tomada del pool de la personalidad activa.

Los comandos de guía, cargos, FAQ, vergüenza, honor, roles, sugerencias y consultas personales son
deterministas:
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
| `DATABASE_URL` | obligatorio | PostgreSQL autoritativo para configuraciones y personas. |
| `PERSONA_ASSET_BUCKET` | obligatorio | Nombre del Railway Bucket que conserva avatares inmutables. |
| `PERSONA_ASSET_ENDPOINT` | obligatorio | Endpoint S3 del Bucket. |
| `PERSONA_ASSET_REGION` | `auto` | Región S3 usada para firmar solicitudes. |
| `PERSONA_ASSET_ACCESS_KEY_ID` | obligatorio | Credencial S3; secreto de proceso. |
| `PERSONA_ASSET_SECRET_ACCESS_KEY` | obligatorio | Credencial S3; secreto de proceso. |
| `CONFIG_OWNER_USER_IDS` | vacío | Owners autorizados para `/config`, `/sugerencias` y `/estado-sugerencia`; no se almacena en DB. |
| `RAILWAY_WEBHOOK_SECRET` | vacío | Secreto URL-safe de 43 a 128 caracteres incluido en la ruta privada del webhook. Vacío deshabilita el receptor. |
| `RAILWAY_PROJECT_ID` | vacío | Proyecto Railway autorizado; obligatorio al habilitar el webhook. |
| `RAILWAY_ENVIRONMENT_ID` | vacío | Entorno Railway autorizado; obligatorio al habilitar el webhook. |
| `RAILWAY_SERVICE_ID` | vacío | Servicio Railway autorizado; obligatorio al habilitar el webhook. |
| `RAILWAY_GIT_COMMIT_SHA` | Railway | SHA inyectado por Railway para que solo la nueva revisión anuncie un despliegue exitoso. |
| `DISCORD_GUILD_IDS` | `*` | Servidores permitidos; acepta CSV. `DISCORD_GUILD_ID` queda como alias compatible. |
| `REPORT_CHANNEL_IDS` | `*` | Canales usados por búsquedas, resúmenes, rankings, supervisión y ayuda; no restringe menciones. `ALLOWED_CHANNEL_IDS` y `REVIEW_CHANNEL_IDS` quedan como aliases compatibles. |
| `AUTHORIZED_USER_IDS` | `*` | `*`/vacío permite a todos; una lista CSV limita la ejecución a esos usuarios. |
| `SUPERVISOR_ROLE_IDS` | `*` | Roles autorizados para comandos de supervisión y para asignar estados manuales reconocidos por las consultas; `*`/vacío permite a todos. |
| `REVIEW_ESCALATION_ROLE_IDS` | vacío | Roles existentes que reciben una mención ante resultados inconclusos o fallas; acepta CSV. |
| `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` | `*` | Canales de texto normales donde puede ejecutarse `/solicitar-roles`; `*`/vacío permite todos y un CSV restringe. Los hilos siempre se rechazan. |
| `ROLE_REQUEST_ALLOWED_ROLE_IDS` | `*` | Roles solicitables: `*`/vacío permite todos los roles seguros; un CSV define un catálogo ordenado y cerrado. |
| `ROLE_REQUEST_REVIEWER_ROLE_IDS` | vacío | Roles que pueden resolver solicitudes. Requiere IDs explícitos, no admite `*`; vacío deshabilita ambos comandos de roles. |
| `BOT_PERSONA` | solo importación | Persona inicial para `db import-env`; se elimina después del corte. |
| `LOG_JSON_INDENTED` | `true` | Usa JSON indentado; `false` genera un objeto compacto por línea. |
| `PORT` | Railway | Puerto inyectado por Railway para el healthcheck; tiene prioridad sobre `HEALTH_PORT`. |
| `OPENAI_MODEL` | `gpt-5.6-terra` | Modelo del runner. |
| `OPENAI_REASONING_EFFORT` | `medium` | Esfuerzo validado contra los valores admitidos por el modelo. |
| `OPENAI_MAX_RETRIES` | `2` | Reintentos del proveedor. |
| `REVIEW_TIMEOUT_SECONDS` | `180` | Tiempo total máximo de una revisión. |
| `MAX_CONCURRENT_REVIEWS` | `2` | Límite global en memoria. |
| `MAX_IMAGES_PER_REVIEW` | `10` | Techo global de imágenes por ejecución; la configuración de una guild solo puede reducirlo. |
| `MAX_IMAGE_BYTES` | `10485760` | Techo global para cada imagen enviada al runner. |
| `MAX_FILES_PER_REVIEW` | `5` | Techo global de documentos compatibles por ejecución. |
| `MAX_FILE_BYTES` | `10485760` | Techo global para cada archivo enviado al runner. |
| `MAX_TOTAL_FILE_BYTES` | `52428800` | Techo global combinado de archivos por ejecución. |
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

## Límites actuales

- PostgreSQL persiste configuración, revisiones, personalidades y auditoría; intentos y locks
  operativos siguen siendo efímeros y se pierden al reiniciar.
- Los callsigns se conservan por servidor y miembro. Al responder un pager se reutiliza el valor
  guardado; si todavía no existe, se solicita una vez y queda disponible para respuestas futuras.
- Las consultas personales reconstruyen un snapshot desde mensajes y reacciones conservados en
  Discord; no pueden recuperar publicaciones eliminadas ni recordar correcciones anteriores.
- Los cambios de configuración se administran como snapshots completos por guild; no hay edición
  directa de filas ni hot reload de secretos de proceso.
- El catálogo y la guía son archivos versionados; todavía no existe sincronización con OpenMP/PCU.
- Cada guild puede tener configuración y personalidad propia, pero el username, avatar, bio y
  presencia de la aplicación de Discord siguen siendo globales por limitación de Discord.
- No se ejecuta una segunda revisión automática ni un fallback de veredicto con otro modelo.

La separación entre contexto Discord, dominio, conocimiento y runner permite añadir más adelante un
catálogo sincronizado mediante API y políticas adicionales para LSSD, LSPD u otras organizaciones.
Consulta [Arquitectura](docs/ARCHITECTURE.md).

## Privacidad y observabilidad

Los logs contienen identificadores técnicos, latencia, modelo, usage crudo, desglose de costo
estimado y veredicto. No registran nombres, narrativa, OCR, URLs de adjuntos ni respuestas completas.
Los traces usan IDs de servidor, canal, hilo, mensajes y usuario para depuración, con payload sensible
desactivado.
Consulta [Privacidad y errores](docs/PRIVACY_AND_ERRORS.md).
