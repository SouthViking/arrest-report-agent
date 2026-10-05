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
- Consulta en una sola petición la nómina de una facción y permite al personal operativo publicar
  quién está conectado o quién además figura en servicio mediante `/conectados`.
- Cruza ese mismo roster cacheado con los roles de Discord para que `/ftp-conectados` resuma
  Supervisory Staff, MFTO, FTO y FTT que están conectados, sin duplicar usuarios ni adivinar nombres.
- Resuelve bajo demanda códigos de respuesta, códigos 10, códigos de situación y el alfabeto
  radiofónico APCO desde una referencia oficial acotada, sin inyectar el manual completo al prompt.
- Mantiene el indicador nativo de escritura mientras procesa una solicitud.
- Muestra una presencia global por fase: guardia, conversación, consulta de manual, personal,
  registros, evidencias, revisión y preparación de respuesta. Las ejecuciones paralelas no se pisan.
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

## Consultas de flota

- `/info-flota` publica una guía de uso de `/vehiculo` y `/flota`. Solo puede ejecutarlo el
  personal operativo, con el módulo habilitado; no consulta vehículos ni carga la lista de miembros.
- `/vehiculo matricula` publica el estado de un vehículo de la facción: combustible, aceite,
  salud, ALPR, sirena, cierre, incautación y último conductor. Acepta matrícula real o visible.
  Incluye los niveles positivos de tanque, blindaje, GPS, alarma, cerradura y encendido en una
  sección compacta; omite niveles cero o ausentes. No convierte el nivel del tanque a capacidad
  ni supone cuál es el nivel estándar de seguridad. Usa el mismo listado cacheado, sin consultas extra.
- `/flota` abre un panel privado para elegir un modelo (selector paginado), seguido de un
  formulario de vista, filtro ALPR y visibilidad. Incluye resumen y rankings de menor salud,
  combustible o aceite; por defecto el resultado es privado. La paginación no vuelve a consultar SARP.
- Ambos requieren los roles de `operational_staff_roles`. El último conductor se cruza con
  nombres completos, abreviados o múltiples de Discord solo ante una coincidencia inequívoca;
  si no se puede resolver, se muestra el nombre original sin notificar a nadie.
- La opción `fleet` de `/config` admite `{"faction_id":3,"cache_seconds":120}`. El TTL es
  configurable entre 1 y 3600 segundos y se aplica al publicar la configuración, sin reiniciar.
  La migración habilita LSSD; otras guilds deben configurar su facción y permisos.
- Se cachean todas las páginas de `/police/vehicles` por facción, compartidas por ambos comandos.
  Las solicitudes simultáneas se agrupan; no hay sondeo periódico ni variables de entorno nuevas.
  Combustible, aceite y salud conservan las unidades informadas por SARP, sin convertirlas a porcentajes
  ni clasificar un vehículo como seguro/dañado. No se muestra kilometraje ni se infiere disponibilidad.

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
REPORT_CHANNEL_IDS=1462992794569281617,1462992859761217745,1529616211204378737
AUTHORIZED_USER_IDS=*
ROLE_REQUEST_ALLOWED_CHANNEL_IDS=*
ROLE_REQUEST_ALLOWED_ROLE_IDS=*
ROLE_REQUEST_REVIEWER_ROLE_IDS=
BOT_PERSONA=walter_vigil
OPENAI_MODEL=gpt-6-luna
OPENAI_REASONING_EFFORT=medium
```

Las allowlists de acceso comparten una sola regla: variable ausente, vacía o con `*` permite
cualquier ID del scope; una lista CSV como `123,456` lo restringe. Esto aplica a servidores
(`DISCORD_GUILD_IDS`) y usuarios (`AUTHORIZED_USER_IDS`). Dentro de una guild autorizada, cualquier
usuario habilitado puede activar al bot mediante una mención o una respuesta directa desde cualquier
canal visible para el bot. Tras importar a PostgreSQL, el acceso del agente se administra con las
políticas **Usuarios del agente** y **Roles con acceso al agente**. Ambas se evalúan como un OR: una
coincidencia de usuario o de cualquiera de los roles es suficiente; si alguna está abierta, el acceso
queda abierto para la guild. La política **Usuarios bloqueados del agente** admite únicamente IDs
explícitos o estar deshabilitada y tiene precedencia absoluta: una cuenta incluida no puede activar
el agente aunque también figure en una lista permitida o posea un rol autorizado.

El módulo dinámico **Acceso del propietario del reporte** añade una excepción acotada: el autor
humano de una publicación en un canal de reportes explícito puede mencionar al bot en el propio
reporte, responder ese mensaje y mencionarlo más tarde, o continuar dentro del hilo nacido del
expediente. Tiene hasta cinco ejecuciones para ese expediente; luego debe continuar personal con
acceso completo. La blacklist conserva precedencia y el propietario nunca recibe herramientas de
directorio, lectura de otros canales, reacciones manuales ni menciones salientes.

El módulo **Consulta de reportes de arresto** conecta el agente con la facción SARP configurada.
El personal con acceso completo puede buscar por sospechoso, agente y fechas, leer el detalle vigente
y vincularlo al hilo de revisión. El propietario limitado solo puede volver a leer la referencia de
su propio expediente. PostgreSQL conserva el vínculo, no la narrativa ni los cargos; cada re-revisión
consulta nuevamente SARP y continúa admitiendo imágenes y archivos como fuente complementaria.
El mismo módulo habilita una consulta selectiva de hechos legales del sospechoso: el agente puede
comprobar antecedentes graves previos o multas impagas con más de tres días en la fecha del reporte.
La resolución exacta del personaje, la paginación, la cronología y la clasificación se calculan en
código; la herramienta devuelve hechos y nunca decide por sí sola si un cargo corresponde.
En revisiones que incluyan cargos, si el sospechoso tenía un arma de fuego, el agente comprueba
antecedentes graves anteriores incluso cuando no se declaró CP-29800. Si confirma el antecedente
y falta ese cargo, ofrece una observación para futuros reportes: no exige modificar el arresto
actual ni afecta su aprobación. No la emite con antecedentes no confirmados, armas de terceros,
sin vínculo o que no sean de fuego, ni la repite si el cargo o el consejo ya estaban presentes.

El acceso completo puede resolver roles del servidor por nombre y, ante una solicitud explícita,
notificar hasta tres mediante la tool terminal `mention_roles`. La publicación valida que cada rol
pertenezca a la guild actual, bloquea `@everyone`/`@here` y limita `AllowedMentions` a los IDs
resueltos. Esta capacidad no se expone al acceso limitado del propietario.

`REPORT_CHANNEL_IDS` no restringe las conversaciones generales del agente. Define los canales donde
puede utilizarse `/revisar-arresto` y los que `/mis-reportes`, `/mi-resumen`, `/pendientes`,
`/ranking`, `/verguenza`, `/honor` y `/errores-reportes` utilizan como archivo de reportes y los que `/ayuda`
presenta como destinos habituales. `ALLOWED_CHANNEL_IDS` y `REVIEW_CHANNEL_IDS` siguen siendo aliases
de migración. Al mostrarlos, el bot conserva únicamente los que pertenecen a la guild actual y son
visibles para él y para el solicitante, evitando referencias `#desconocido` entre servidores.

El subsistema de solicitudes de roles se habilita al configurar uno o más IDs explícitos en
`ROLE_REQUEST_REVIEWER_ROLE_IDS`. `ROLE_REQUEST_ALLOWED_ROLE_IDS` sigue la regla uniforme de las
allowlists: ausente, vacío o `*` permite todos los roles seguros; un CSV define un catálogo cerrado.
`/solicitar-roles` publica un estado saneado en el mismo canal de texto donde se ejecuta. Cuando la
guild configura un ingreso FTT, ofrece un flujo guiado que solicita la estación y, si hay turnos
configurados, uno de ellos, y prepara el paquete completo de roles iniciales;
`ROLE_REQUEST_ALLOWED_CHANNEL_IDS` puede abrir todos esos canales con `*` o restringirlos mediante
CSV. El motivo y los controles se replican únicamente en los canales privados de la política
`role_request_review_channels`. No aplica `AUTHORIZED_USER_IDS`, no funciona dentro de hilos, no usa
IA y persiste el expediente y sus referencias en PostgreSQL. `/info-roles` permanece disponible en
cualquier canal o hilo de una guild habilitada.

Una nueva solicitud publicada correctamente reemplaza las pendientes del mismo usuario en la guild.
El historial se conserva y sus tarjetas se actualizan como canceladas, incluso si algunas copias fueron
eliminadas; `/info-roles` explica este comportamiento. No se interrumpe una aprobación en curso.

`role_requests.shifts` configura opciones de turno por guild (`key`, `label`, `role_id`, `description`
y `emoji` opcional). El turno es obligatorio en el ingreso y opcional en cambios de roles; omitirlo
conserva el actual. Estos roles no se duplican en el selector general. Un cambio de turno reemplaza
los anteriores y se aprueba como una unidad, también al aprobar parcialmente otros cambios.
Las descripciones de AM, PM y EM indican horarios ARG como referencia interpretativa, no como
restricciones de conexión. La migración configura los tres roles de LSSD; no requiere variables nuevas.

Cuando `role_requests` define `retired_role_id`, `/solicitar-roles` ofrece un flujo separado para
miembros con alguno de los `operational_staff_roles`. Ese formulario no solicita bajas manuales:
al aprobarlo, el bot vuelve a consultar al miembro, retira todos los roles que puede administrar
y asigna el rol de retiro mediante una única edición. Verifica el resultado y avisa al aprobador
si quedaron roles que no puede retirar. El ingreso FTT se muestra solo a personas sin esos roles
operativos ni el rol de retiro; el reingreso desde `Retired` requiere gestión aparte.

El módulo determinista de pagers persiste sus avisos, múltiples roles destinatarios y respondientes
en PostgreSQL. `/pager` emite el aviso en el canal autorizado, `/pager-activos` enlaza los avisos
abiertos, `/mis-pagers` entrega el historial privado del usuario y `/info-pager` publica una guía breve.
El emisor no puede registrarse además como respondiente. Los controles persistentes
permiten responder, retirarse, editar, concluir o cancelar incluso después de reiniciar el proceso.

Las analíticas globales `/verguenza`, `/honor` y `/errores-reportes` usan una política independiente
`report_analytics_roles`. Solo sus roles staff autorizados pueden generarlas, aunque el resultado
continúa siendo público para el canal. El último comando consulta revisiones estructuradas almacenadas
desde esta versión y cuenta cada expediente una sola vez por tipo de error, aunque haya re-revisiones.
`/pendientes` y `/ranking` mantienen su política separada de supervisión.

La política previa valida guild, autor y acceso por usuario o rol antes de leer contexto o iniciar el Runner. Las
tools de escritura operan exclusivamente dentro de la conversación autorizada. La tool de reacción
acepta un ID presente en ese contexto, pero no una guild o canal libre y no puede salir del mensaje
raíz, canal o hilo activo. Las consultas de mensajes o adjuntos quedan fijadas a la guild y a los permisos
de lectura del solicitante. El agente solo puede escoger `reply` o `thread` para una mención nueva;
una revisión estructurada se publica en hilo y la primera respuesta fija la ruta de toda esa ejecución.

Mientras el agente trabaja, hay una sola ejecución activa por `(guild_id, channel_id)`; los hilos
usan su propio ID. Los mensajes humanos nuevos, con o sin mención, se incorporan a esa ejecución
antes de la siguiente llamada al modelo. Incluyen texto, autor, IDs y metadata de adjuntos, no
binarios. Las nuevas menciones no reservan otra ronda ni elevan los permisos del solicitante original.
Los bots, webhooks y usuarios bloqueados no alimentan la cola. Un hilo creado por el agente se
registra como alias de su conversación activa.

`ConversationStore` define las operaciones atómicas de coordinación y `InMemoryConversationStore`
las implementa sin servicios adicionales. La cola admite hasta 100 mensajes nuevos por ejecución;
también se conservan los límites globales de tiempo y llamadas al modelo. Se comprueban pendientes
antes de acciones de publicación y antes de cerrar. Si llegan durante la última respuesta, se
continúa con la misma sesión y cuota; no se revierten acciones que Discord ya recibió. El filtro
`call_model_input_filter` inyecta los mensajes y los guarda explícitamente en la sesión, avanzando
el cursor de origen para no duplicarlos en la próxima interacción. La cola pendiente se pierde al
reiniciar el proceso; una futura implementación Redis debe resolver también recuperación de runs.

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
título, resumen, hasta diez cambios tipados y `changelog_targets`; no contiene copy de una persona.
`changelog_targets` acepta `"all"` para publicar en todos los canales configurados o una lista de
IDs para publicar solo en esa intersección. Nunca permite enviar a un canal que no esté autorizado
por la política **Canales de despliegues** de su guild. Las alertas `failed`, `crashed` y
`oom_killed` utilizan exclusivamente la política independiente **Canales de incidentes de
despliegue** y nunca reutilizan destinos públicos del changelog.

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
modo restringido y, si deseas alertas operacionales, configura **Canales de incidentes de
despliegue** en modo restringido. Luego habilita **Notificaciones de despliegue** con schema `1` y
opciones `{}` y publica el borrador. La migración restringe los incidentes de LSSD únicamente a
`lssd-bot-git` y los deja deshabilitados en las demás guilds.

### Alertas de GitHub Actions

El mismo servidor HTTP recibe el webhook nativo `workflow_run` de GitHub en:

```text
https://<dominio-publico>/webhooks/github/actions
```

Configura `GITHUB_WEBHOOK_SECRET` en Railway y usa exactamente el mismo secreto al crear el webhook
del repositorio. El receptor valida `X-Hub-Signature-256` sobre el cuerpo original, deduplica con la
identidad de la ejecución y solo conserva fallos completados del workflow `CI` de este repositorio.
No filtra por rama ni por origen: puede avisar fallos de pushes, pull requests o ejecuciones manuales.

La política **Canales de errores de CI** es independiente de **Canales de despliegues**. La migración
inicial la restringe únicamente a `lssd-bot-git` en LSSD y la deja deshabilitada en las demás guilds.
Cada alerta enlaza el commit y GitHub Actions, pero no copia logs, stacktraces ni secretos a Discord.
El evento y sus entregas quedan en PostgreSQL para deduplicar redeliveries y reintentar errores
transitorios de Discord.

### Aportes voluntarios

El mismo servidor puede recibir eventos firmados de Buy Me a Coffee en:

```text
https://<dominio-publico>/webhooks/support/buy-me-a-coffee
```

Configura `BUY_ME_A_COFFEE_WEBHOOK_SECRET` en Railway con el secreto de firma entregado por el
proveedor. El receptor valida `X-Signature-Sha256` sobre el cuerpo original, limita el tamaño,
deduplica reintentos y admite aportes y reembolsos. Los eventos de prueba se conservan como
auditoría, pero no alteran los aportes reales. No se guardan correos ni notas privadas.

Cada transacción conserva el importe y moneda originales. Un worker independiente normaliza el
importe a USD usando la tasa histórica de Frankfurter correspondiente a la fecha del aporte. Las
tasas quedan cacheadas permanentemente en PostgreSQL y las fallas transitorias se reintentan con
backoff, sin retrasar la respuesta al webhook. Los importes en USD no realizan solicitudes externas.
También existe una capa explícita para vincular identidades estables del proveedor con usuarios de
Discord; nunca se unen personas automáticamente por nombre.

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

- `/ayuda`: índice público y compacto hacia las guías de reportes, roles, pagers, callsigns y
  sugerencias, junto con accesos rápidos a `/guia`, `/cargos` y `/faq`.
- `/info-reportes`: guía pública y breve para que el autor inicie o continúe la revisión limitada
  de su propio expediente. Explica el formulario estructurado, la revisión inicial, las
  continuaciones disponibles, los canales habilitados y la compatibilidad con el formato anterior.
- `/ampliar-cupo cantidad`: concede de 1 a 10 continuaciones adicionales al autor del reporte
  desde su hilo y publica allí un embed con los turnos disponibles después del ajuste. Solo
  funciona para los roles configurados para conceder cupos o los IDs incluidos en
  `REPORT_OWNER_QUOTA_ADMIN_USER_IDS`; cada concesión queda auditada en PostgreSQL.
  Al agotarse el cupo, avisa una sola vez por límite alcanzado a los roles autorizados en el
  canal de notificaciones configurado, con enlace al hilo. En LSSD el aviso llega a Command Staff.
- `/revisar-arresto`: permite a `operational_staff_roles` publicar un reporte en un canal de
  `report_channels`, indicar su ID SARP, separar fotografías y respaldos, abrir su hilo e iniciar
  una primera revisión sin consumir una de las cinco continuaciones del propietario. La capa interna
  conserva el flujo sin ID para poder habilitarlo posteriormente sin bifurcar la implementación.
  La tarjeta comienza amarilla y solo una revisión completa puede cambiarla a verde, rojo o naranja;
  consultas y revisiones parciales no alteran su estado general.
- `/guia`: referencia privada y navegable de las secciones relevantes del manual de reportes.
- `/cargos`: modal privado para buscar por código, título, descripción o categoría en el catálogo
  penal activo; muestra detalles y sanciones con paginación cuando corresponde.
- `/mdc`: cuando la integración SARP está activa, abre un modal para que el personal de
  `operational_staff_roles` busque el nombre exacto de un personaje y elija si la ficha operativa
  será visible para el canal o solo para quien consultó. No expone IDs internos ni datos OOC, y las
  secciones independientes conservan resultados parciales ante rate limits o fallos de la API.
- `/info-mdc`: publica una guía breve para consultar nombres exactos, elegir la visibilidad y
  entender qué información puede devolver la ficha.
- `/conectados`: publica para `operational_staff_roles` el personal conectado y en servicio.
- `/ftp-conectados`: publica para `operational_staff_roles` únicamente Supervisory Staff, MFTO,
  FTO y FTT conectados, indicando cuáles figuran en servicio.
- `/solicitar-ficha`: mantiene como alternativa el formulario con el que un FTT programa su ficha,
  elige un evaluador general o supervisor y recibe una notificación cuando alguien la toma. El canal
  configurado también contiene un panel fijado que abre el mismo formulario sin permitir mensajes normales.
- `/ofrecer-ficha`: FTO, MFTO, Supervisory, Command y Executive publican bloques de disponibilidad;
  solo los Field Training Trainees pueden reservarlos seleccionando la fase. También se abre desde
  el panel fijado de FTP, sin escribir mensajes en el canal.
- `/ver-evidencia`: permite al personal de `operational_staff_roles` consultar públicamente una
  bolsa por su número y ver el contenido analizado junto con su cadena de custodia. El agente con
  acceso completo reutiliza la misma consulta cuando necesita contrastar una bolsa durante una
  conversación o revisión.
- `/vehiculos-robados`: abre un formulario para que `operational_staff_roles` consulte entre 1 y
  50 denuncias recientes, filtre por activas, resueltas o todas, y elija entre publicarlas en el
  canal o recibirlas de forma efímera. El resultado compacto usa paginación cuando corresponde.
- `/reportes-pendientes`: abre un formulario para que `operational_staff_roles` seleccione un
  período de hasta 90 días y decida si el resultado será público o privado. Revisa hasta 1.000
  arrestos de SARP sin IA y muestra en un solo embed los que parecen incompletos, agrupados por
  agente según su cantidad y ordenados por antigüedad dentro de cada grupo.
- `/informe-faccion`: abre un formulario efímero para que los roles configurados en
  `faction_report_roles` elijan 1, 2, 7, 14, 30 o 90 días, o un rango personalizado. Entrega un
  `.xlsx` con el registro bancario del período y la nómina activa actual. Una casilla permite
  publicarlo en el canal y, si queda desmarcada, el archivo se entrega privadamente. La nómina se
  ordena por rango descendente y nombre. No incluye IDs internos, conexión exacta ni datos OOC.
- `/estadisticas-arrestos`: permite a `faction_report_roles` analizar entre 1 y 30 días de
  reportes. Antes de consultar los detalles muestra la cantidad encontrada, el uso de caché y las
  solicitudes adicionales estimadas. Tras confirmar, publica o entrega privadamente un top 5 o 10
  de cargos, sus combinaciones frecuentes y la clasificación grave/no grave. Limita cada análisis
  a 200 reportes, consulta detalles con concurrencia acotada y conserva cobertura parcial si algún
  reporte no está disponible.
- `/radios-faccion`: permite a `operational_staff_roles` consultar radios por intervalos UTC desde
  una hora hasta 30 días, o por un rango horario personalizado, slot y miembro. Puede publicarlos o
  recibirlos de forma privada y descargar un registro TXT cuando el resultado es extenso.
- `/auditar-radio`: entrega a `radio_audit_roles` una auditoría asistida del slot 1, fundamentada en
  la sección de comunicaciones del manual y en el catálogo de códigos, sin proponer sanciones.
- `/bitacora`: disponible únicamente en los canales configurados en `logbook_channels`. Permite
  seleccionar una categoría y un movimiento de dinero opcional antes de abrir un formulario
  adaptado con subtipo, asunto, descripción y hasta diez adjuntos. Publica una entrada uniforme
  con referencia `BIT`; cuando hay varias imágenes las presenta en una galería nativa. Conserva
  los datos en PostgreSQL y los archivos en el Bucket.
- `/blacklist registrar`: abre un formulario únicamente en un canal asociado a un tipo de
  blacklist y exige pertenecer a `operational_staff_roles`. El canal determina internamente el
  tipo; el usuario registra los perfiles PCU admitidos, la persona o personaje y una razón de 10
  a 1.000 caracteres. `CCW` (`CCW`, canal `1530621801871441991`) acepta un perfil de personaje;
  `Facción` (`FAC`, canal `1383926305136971906`) acepta hasta cinco perfiles de cuenta, uno por
  línea. Publica un embed con una referencia correlativa propia del tipo y conserva el registro en
  PostgreSQL.
- `/blacklist consultar`: funciona desde cualquier canal del servidor y busca registros activos de
  todos los tipos por ID de PCU, URL completa de cuenta o personaje, o nombre. Exige
  `operational_staff_roles`; el formulario permite publicar el resultado para todos —la opción
  predeterminada— o mostrarlo solo a quien consulta.
- `/blacklist editar`: reservado a los roles gestores de Command Staff y Executive Staff. Desde el
  canal asociado al tipo permite indicar una referencia y reemplazar selectivamente los perfiles
  PCU, el nombre o la razón; actualiza PostgreSQL y el embed original.
- `/blacklist eliminar`: aplica el mismo acceso y asociación canal–tipo. Tras una confirmación,
  retira el mensaje de Discord y conserva la entrada revocada con su responsable y fecha para
  auditoría interna.
- `/blacklist info`: guía pública con los comandos, permisos y tipos habilitados. También aparece
  en `/ayuda` cuando el módulo de blacklists está activo.
- `/rumor`: abre un formulario con aviso de uso responsable y publica un embed anónimo en el
  canal configurado, con fecha relativa nativa de Discord. Por defecto no menciona a nadie; el
  autor puede optar por avisar al rol asociado a ese canal. Su identidad no aparece en la
  publicación: queda vinculada a la interacción y al mensaje en PostgreSQL para auditoría interna.
  El comando aparece en `/ayuda` únicamente dentro de los canales habilitados.
- `/info-bitacora`: explica públicamente el flujo en un canal de bitácora habilitado. `/ayuda`
  solo muestra esta guía cuando se ejecuta dentro de uno de esos canales.
- `/guia-bitacora`: muestra una referencia compacta e indentada de todas las categorías y
  subtipos vigentes, con opción de verla privadamente o publicarla en el canal.
- `/resumen-bitacora`: permite a `faction_report_roles` consultar desde cualquier canal los
  registros publicados por período, categoría y subtipo. Resume cantidades, adjuntos y movimientos
  financieros; puede publicarse en el canal o mostrarse de forma privada.
- `/faq`: preguntas frecuentes privadas y navegables, curadas a partir de la guía local.
- `/verguenza`: abre un modal para elegir mes actual o los últimos 7, 30 o 90 días, y publica por
  cada canal configurado un top 10 de quienes acumulan más expedientes que todavía no alcanzan el
  ✅; muestra una leyenda y separa los expedientes sin revisión (`📭`) de las revisiones activas
  (`⏳`), correcciones (`❌`) e inconclusos (`⚠️`).
- `/honor`: abre el mismo selector de periodo y publica por estación un top 10 de miembros con al
  menos un reporte aprobado, ordenado por cantidad de ✅, porcentaje de aprobación y volumen.
- `/errores-reportes`: abre filtros de periodo, estación, severidad y cantidad; publica las categorías
  de hallazgos más frecuentes registradas por el agente. No procesa reportes históricos anteriores
  al despliegue de esta versión.
- `/ts3`: permite al personal de `operational_staff_roles` publicar para todo el canal la dirección
  `51.81.49.72:11189` del servidor TeamSpeak 3; los intentos no autorizados son efímeros.
- `/manual`: permite al personal de `operational_staff_roles` publicar para todo el canal un acceso
  directo al manual oficial configurado para la facción.
- `/pendientes`: consulta de supervisión para expedientes sin estado terminal o en revisión.
- `/ranking`: ranking de supervisión por volumen, aprobaciones, correcciones o tasa de aprobación.
- `/mis-reportes`: abre un modal privado para filtrar por periodo, estado y estación; entrega
  referencias directas a cada publicación y a su hilo cuando existe.
- `/mi-resumen`: muestra de forma privada el estado personal del mes actual.
- `/solicitar-roles`: permite elegir entre un cambio normal de hasta diez altas y diez bajas o,
  cuando está configurado, un retiro que calcula automáticamente los roles gestionados a quitar;
  publica el estado en el canal de origen y deriva la revisión detallada a canales privados.
- `/info-roles`: publica una explicación breve sobre el uso y los resultados de las solicitudes.
- `/pager`: abre un formulario privado y emite un pager en el canal actual; reutiliza el callsign
  registrado sin volver a solicitarlo.
- `/pager-activos`: publica los pagers abiertos con enlaces directos.
- `/mis-pagers`: consulta de forma privada los pagers creados o respondidos.
- `/info-pager`: publica una explicación breve del sistema.
- `/callsign`: registra o reemplaza de forma privada el callsign operativo del miembro; requiere un
  rol configurado como personal operativo y acepta dos o tres números, cualquier letra de `A` a
  `Z` y un número final opcional, como `280L`, `64A` o `60F1`.
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
- `/pregunta-manual`: abre un formulario privado para que el personal de `operational_staff_roles`
  seleccione dificultad, tipo y un plazo de 10 segundos a 5 minutos antes de publicar una pregunta
  del catálogo PostgreSQL. Cualquier miembro puede responder y la primera respuesta cierra el
  ejercicio; si vence el plazo, el mismo mensaje se cierra y muestra la solución. El resultado,
  explicación y referencia quedan visibles para el canal. Este MVP no conserva puntajes ni intentos.
  El seed inicial incorpora 70 preguntas curadas del manual entre selección única y
  verdadero/falso.
- `/config`: abre un panel efímero sin argumentos, reservado a owners o roles `config_admin_roles`;
  permite inspeccionar la revisión activa y administrar borradores, accesos, agente, módulos,
  personas, tipos de blacklist, publicación y rollback mediante botones y formularios. Cada tipo
  de blacklist define una clave, nombre visible, prefijo de referencia, clase de perfil PCU,
  cantidad máxima de perfiles, etiqueta del sujeto y uno o más canales de registro; un canal no
  puede pertenecer a dos tipos. El mismo formulario administra los roles de gestión para Command
  Staff y Executive Staff. Los intentos no autorizados reciben una alerta efímera tomada del pool
  de la personalidad activa.

En `/config` → **Módulos**, `report_owner_access` acepta `{"max_rounds": 5}` con un valor entre 1 y
20. Solo puede habilitarse cuando `report_channels` contiene IDs explícitos.
Los intentos fallidos no consumen rondas; las ejecuciones en curso reservan su cupo hasta terminar.
`arrest_report_lookup` acepta
`{"faction_id": 3, "report_attachment_mode": "generated", "max_rank_names_per_lookup": 20}`
y habilita las tools SARP del agente
para esa guild. `report_attachment_mode` usa `generated` por defecto y puede cambiarse a `manual`;
el modo generado crea una imagen vigente del reporte SARP al publicarlo y la reemplaza después de
cada revisión completa. El modo se resuelve desde PostgreSQL en cada revisión, sin agregar variables
de entorno ni conservar versiones anteriores de la imagen. El interruptor global `SARP_API_ENABLED`
debe estar activo.
`pending_arrest_reports` acepta
`{"faction_id": 3, "max_range_days": 90, "max_records": 1000, "digest_interval_hours": 6,
"digest_lookback_days": 7, "digest_targets": [{"channel_id": 1537203701893169254,
"mention_users": true}]}`. La consulta manual sigue funcionando en cualquier canal para
`operational_staff_roles`. El resumen automático reutiliza el mismo escaneo y formato; admite varios
canales, pero solo uno con menciones para evitar pings duplicados. No publica si no hay pendientes.
La frecuencia y los destinos se pueden cambiar desde `/config` sin reiniciar; un destino vacío
desactiva el envío periódico. No requiere variables de entorno nuevas.

Al iniciar el acceso limitado fuera de un hilo, la publicación raíz debe contener dos componentes
de nombre, una referencia opcional de bolsa de evidencia y al menos cuatro imágenes compatibles. La
publicación directa debe comenzar con `@mención Nombre Apellido` o
`@mención Nombre Apellido - #NÚMERO`; un reply puede apuntar al mismo reporte ya publicado. Un formato o
material incompleto recibe una explicación determinista, sin crear una ejecución de IA ni consumir
una ronda. Si el autor abre manualmente el hilo de su propio reporte, puede mencionarlo allí con texto
libre: el hilo canónico, la autoría y el canal de reportes reemplazan esa validación de formato, y la
primera ejecución que llegue al agente comienza a consumir el límite configurado.

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

### Cargar el banco inicial de preguntas

Después de aplicar las migraciones, el catálogo se carga para una guild concreta con:

```bash
psql "$DATABASE_URL" \
  --set=guild_id=1234674521416663070 \
  --file=scripts/seed_manual_quiz_questions.sql
```

El archivo valida que la guild exista, carga 120 preguntas activas con sus alternativas y puede
ejecutarse nuevamente sin duplicarlas. La clave estable `slug` permite corregir preguntas existentes
e incrementar su revisión solo cuando cambia su contenido. No altera otras guilds ni preguntas
creadas fuera de ese seed. Cincuenta ejercicios avanzados plantean casos que combinan procedimientos,
criterio operativo, comunicaciones, garantías legales y trazabilidad de reportes.

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
| `GITHUB_WEBHOOK_SECRET` | vacío | Secreto compartido de 32 a 256 caracteres para validar con HMAC-SHA256 el webhook nativo de GitHub. Vacío deshabilita el receptor. |
| `BUY_ME_A_COFFEE_WEBHOOK_SECRET` | vacío | Secreto de firma del webhook de Buy Me a Coffee. Vacío deshabilita tanto el receptor como la normalización de aportes. |
| `OPENAI_ADMIN_KEY` | vacío | Admin API key separada y de solo backend para consultar costos y límites de OpenAI desde el panel owner-only. |
| `OPENAI_PROJECT_ID` | vacío | Proyecto de OpenAI que acota el gasto y el límite; sin él se usa el alcance de organización. |
| `RAILWAY_API_TOKEN` | vacío | Token de Railway limitado al workspace consultado por el monitor de consumo. |
| `RAILWAY_WORKSPACE_ID` | vacío | Workspace de Railway cuyo periodo, hard limit y margen se consultan. El uso del proyecto se filtra automáticamente con `RAILWAY_PROJECT_ID`. |
| `COST_MONITOR_TIMEOUT_SECONDS` | `10` | Timeout individual de las consultas de consumo. |
| `COST_MONITOR_CACHE_SECONDS` | `300` | Vigencia en memoria del último resumen válido para evitar consultas repetidas. |
| `SARP_API_ENABLED` | `false` | Kill switch global. Apagado evita crear el cliente o realizar solicitudes; el código penal permanece local. |
| `SARP_API_TOKEN` | vacío | Token personal enviado exclusivamente como `Authorization: Bearer`; obligatorio al activar SARP y nunca registrado. |
| `SARP_API_BASE_URL` | `https://api.gta-rol.com/api` | Base HTTPS de la API comunitaria de SARP. |
| `SARP_API_TIMEOUT_SECONDS` | `10` | Timeout total de cada intento contra SARP. |
| `SARP_API_MAX_RETRIES` | `2` | Reintentos máximos para lecturas seguras ante transporte, rate limit o errores transitorios. |
| `SARP_EMERGENCY_CALL_POLL_SECONDS` | `30` | Intervalo del feed de llamadas de servicio; admite entre 10 y 3600 segundos y no consulta la API sin canales activos. |
| `SARP_STOLEN_VEHICLE_POLL_SECONDS` | `60` | Intervalo del feed de vehículos robados; admite entre 10 y 3600 segundos y no consulta la API sin canales activos. |
| `SARP_ONLINE_PLAYERS_CACHE_SECONDS` | `30` | Vigencia local del listado público de personajes conectados usado por `/internos`; admite entre 0 y 300 segundos. |
| `ONLINE_INMATES_PAGE_SIZE` | `10` | Cantidad de nombres mostrados por página en `/internos`; admite entre 1 y 25. |
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
| `OPENAI_MODEL` | `gpt-6-luna` | Modelo inicial para nuevas configuraciones; las guilds existentes usan el valor guardado en PostgreSQL. |
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
| `THREAD_CONTEXT_MAX_HISTORICAL_IMAGES` | `2` | Máximo de imágenes históricas que el agente puede recuperar bajo demanda por ejecución. |
| `THREAD_CONTEXT_MAX_HISTORICAL_FILES` | `2` | Archivos previos reutilizados como contexto. |
| `OPENAI_TRACING_ENABLED` | `true` | Habilita traces del Agents SDK. |
| `OPENAI_TRACE_INCLUDE_SENSITIVE_DATA` | `false` | Excluye mensajes, imágenes, archivos y tool payloads del trace. |

Con `SARP_API_ENABLED=true`, el runtime obtiene `GET /police/penal-code` al arrancar, lo refresca
en segundo plano y registra `/mdc`, `/conectados`, `/ftp-conectados`, `/recursos`, `/vehiculos-robados` y
`/reportes-pendientes`. `/cargos` y las tools del agente comparten el mismo snapshot: una caída temporal
conserva el último catálogo remoto válido y, si todavía no existe uno, usa `delitos.json`. Cada
revisión fija su snapshot al comenzar, por lo que una actualización no mezcla fuentes a mitad de
una ejecución. `/mdc` consulta en vivo la búsqueda exacta, perfil IC, posesiones y registros
policiales; no persiste sus resultados y solo muestra como no disponible la sección remota que falle.

Cuando `arrest_report_lookup` está habilitado para la guild, el mismo cliente permite al agente
buscar `GET /my-faction/arrests/:factionId` y recuperar el detalle actual mediante
`GET /police/arrests/:id`. No existe caché de contenido para esta ruta. Solo se persiste la referencia
entre el mensaje raíz de Discord y el arresto SARP, de modo que «listo» o «revisa de nuevo» use la
versión vigente sin permitir al propietario explorar expedientes ajenos. Los errores remotos se
proyectan como estados seguros y no impiden continuar con adjuntos suficientes.

`/conectados` usa el módulo dinámico `faction_status`, cuyo payload contiene el `faction_id` propio
de cada guild. Obtiene el roster completo con `GET /my-faction/faction/:factionId` y publica en una
sola vista los totales de conectados, personal en servicio y porcentaje de cobertura, junto con la
lista alfabética de conectados. La API se consulta una sola vez y el resultado se comparte durante
quince segundos para proteger el límite del proveedor.

`/ftp-conectados` usa el módulo dinámico `ftp_status`. Reutiliza el mismo roster y caché de
`/conectados`, y cruza los personajes conectados con los roles configurados de Supervisory Staff,
MFTO, FTO y FTT en Discord. Los nombres toleran guiones bajos, tildes y alias separados por `/`,
incluido el formato `N. Apellido`; las coincidencias ambiguas se omiten en lugar de elegirse al azar.

`/solicitar-ficha` usa el módulo dinámico `ftp_requests` y persiste la fase, tipo de evaluador, horario,
comentarios opcionales, estado y referencias de Discord necesarias. Cada solicitud comienza con un selector
de zona horaria IANA; la fecha y hora locales se convierten a UTC para que Discord las muestre correctamente
a cada persona. El bot publica, actualiza y fija un panel de entrada persistente en el canal configurado; su
botón abre el mismo flujo sin requerir permiso para escribir mensajes. El comando también puede habilitarse
en canales de entrada adicionales, como `#ftp-chat`, sin cambiar el destino de las tarjetas. Pueden solicitar
FTT, Command o Executive y mantener varias solicitudes para horarios distintos; la opción general admite FTO
o rangos superiores y la opción supervisor exige
Supervisory, Command o Executive. Solo el autor, Command Staff o Executive Staff puede cancelar. El canal,
panel y roles se resuelven desde PostgreSQL, sin nuevos environment vars.
Las solicitudes generales mencionan a FTO, MFTO y Supervisory Staff; las de supervisor mencionan a
Supervisory Staff, Command Staff y los roles de `ftp_requests.supervisor_extra_notification_role_ids`.
Esta lista adicional solo controla menciones, no concede permisos. En LSSD se configura inicialmente
FTP Management (`1390426066455105598`); Executive Staff no recibe menciones salvo configuración explícita.

`/ofrecer-ficha` reutiliza esos canales y roles, sin nuevas variables de entorno. El instructor elige
**Disponible ahora** o fecha/hora y zona IANA, duración de 30/45/60/90 minutos, 1–5 bloques contiguos
y comentarios opcionales. Puede publicar varios horarios independientes; se rechazan solapamientos
con sus bloques libres o reservados aún vigentes. La tarjeta menciona al rol FTT una vez, usa autor
nativo y timestamps locales, y mantiene sus controles tras reiniciar. Seleccionar un bloque abre
un modal de fase/comentarios: solo al enviarlo se reserva, revalidando roles y disponibilidad.
La tarjeta muestra el tipo de instructor según sus roles configurados, priorizando Executive,
Command, Supervisory, MFTO y FTO. Al actualizarse vuelve a resolver el tipo; si no puede obtenerlo,
indica «No disponible». Distingue entre todos los bloques reservados, todos vencidos, una mezcla
de reservados/vencidos y una disponibilidad cerrada manualmente, manteniendo las reservas visibles.
La reserva atómica crea una ficha `claimed` (asignada, no completada), actualiza la tarjeta y
responde mencionando al trainee e instructor. Solo el instructor o Command/Executive pueden cerrar
una oferta; las reservas se conservan. Los bloques libres vencen a su hora de término, sin la gracia
de las solicitudes pendientes. El mantenimiento compartido los revisa cada minuto; no llama a IA ni
SARP. Las ediciones/notificaciones fallidas conservan una marca en BD para reintentarse sin deshacer
reservas. Si se elimina una tarjeta, no se recrea y sus bloques libres se cierran. Las notificaciones
se entregan con reintentos y deduplicación reciente por nonce (no garantizan exactamente una entrega
ante fallos prolongados). `/info-ftp` y el panel fijado explican ambos flujos.

Al tomar una solicitud o reservar un bloque, ambos flujos responden a su publicación original
con el mismo embed azul «Ficha coordinada»: fase, horario nativo de Discord, trainee, instructor
y comentarios opcionales. Las reservas incluyen además el bloque y su hora de término. Las menciones
fuera del embed notifican únicamente a ambos participantes, sin avisar a roles ni al resto del canal.

`/fichas-activas` reutiliza esos registros para publicar en cualquier canal un resumen paginado de las
solicitudes pendientes, ordenado por horario y restringido a `operational_staff_roles`.

Las fichas pendientes vencen automáticamente 6 horas después de `scheduled_for` (UTC);
las solicitudes antiguas sin horario usan `created_at`. Cada solicitud se evalúa por separado,
sin impedir solicitudes múltiples ni fechas futuras. `/config` permite modificar en caliente el
payload de `ftp_requests`: `expiry_enabled` (por defecto `true`), `expiry_check_minutes` (10;
entre 1 y 1440) y `expiry_grace_hours` (6; entre 1 y 168). Desactivar el vencimiento no revierte
cancelaciones previas y permite terminar las ediciones pendientes mientras el módulo siga habilitado.
Las tarjetas vencidas se actualizan sin menciones, en gris y con botones deshabilitados; las ediciones
fallidas quedan pendientes en PostgreSQL para reintentar después de reinicios. Una tarjeta eliminada
no se vuelve a crear. `/info-ftp` muestra el plazo configurado y `/fichas-activas` deja de incluirlas.

El ejecutor genérico `MaintenanceScheduler` comprueba tareas cada 60 segundos dentro del proceso
existente. Cada tarea tiene nombre, intervalo y callback propios, sin solapamientos; los errores y
timeouts de una no interrumpen las demás. Al reiniciar se ejecutan al quedar Discord listo. No persiste
el temporizador: cada consumidor debe ser idempotente y guardar su trabajo pendiente. FTP es el primer
consumidor, con lotes máximos de 50 cancelaciones y 50 ediciones por ejecución; no consulta SARP ni IA.
Los demás workers existentes mantienen sus frecuencias y comportamiento.

`/recursos` usa el módulo dinámico `operational_resources` y la misma política
`operational_staff_roles`. Reúne en un panel público los enlaces oficiales, las direcciones de juego
y TeamSpeak, y los contadores de servidor, facción y servicio. Reutiliza la caché de `/conectados`
y consulta `GET /public/server-cache/download` con un caché adicional de cinco minutos para enlazar
la versión vigente del caché oficial sin bloquear el resto del panel si ese dato no está disponible.

`/foro` es un acceso público y visible para todos al catálogo institucional configurado en el módulo dinámico
`forum_shortcuts`. PostgreSQL conserva por guild el nombre, el texto neutral, el GIF y los enlaces
agrupados; el comando no depende de roles, canales, personas, OpenAI ni SARP. Para LSSD se instala el
foro principal y los accesos de empleo, transferencias, reincorporaciones, FTP, participación pública
y licencias CCW/caza.

`/reportes-pendientes` usa el módulo dinámico `pending_arrest_reports`. Descarga de forma paginada
los reportes del período elegido, considera terminadas las narrativas que comienzan con `Yo` y marca
determinísticamente textos vacíos, placeholders, repeticiones o descripciones breves no estándar.
Cruza cada caso con su revisión más reciente y prioriza los resultados mediante un score interno:
PCU sin completar, revisiones ausentes o pendientes y plazos de 24 y 48 horas. Los círculos rojo,
naranjo, amarillo y verde exponen la urgencia sin mostrar el score. No guarda narrativas ni resultados:
PostgreSQL conserva configuración, referencias de revisión y metadatos de programación/avisos. El
formulario permite publicar el embed en el canal o recibirlo de forma efímera. El envío periódico
guarda solo el estado de programación, las entregas y los avisos: en cada ciclo menciona de forma
agrupada a los agentes que sigan pendientes, incluidos los que aún están dentro de plazo, y cruza
alias de múltiples PJ para no repetir una mención por persona en el mismo aviso. Cuando
`supervisor_alerts_enabled` está activo, `supervisor_alert_channel_ids` y
`supervisor_alert_role_id` configuran una alerta adicional solo si hay PCU vencido (24 h) o revisión
sin aprobar fuera de plazo (48 h). Se publica después del resumen general, enlaza directamente a ese
mensaje y menciona únicamente al rol; los agentes figuran en el embed sin recibir otra notificación.
Estos valores se administran desde `/config`. La primera publicación ocurre después del intervalo
configurado, no inmediatamente al arrancar.

El módulo dinámico `service_calls` consulta `GET /police/emergency-calls` únicamente cuando la
política `service_call_channels` contiene al menos un canal explícito. La primera sincronización fija
el llamado más reciente como cursor sin reproducir historial; después publica solo novedades en
orden cronológico. PostgreSQL conserva un único ID por guild y canal, no los nombres, ubicaciones,
teléfonos ni situaciones recibidas desde SARP. El intervalo global se controla con
`SARP_EMERGENCY_CALL_POLL_SECONDS`, mientras que destinos y estado se publican en tiempo real desde
`/config` sin redesplegar.

El módulo dinámico `stolen_vehicles` registra `/vehiculos-robados` para el personal definido en
`operational_staff_roles` y consulta `GET /police/stolen-vehicles` en vivo, sin guardar resultados.
Cuando la política `stolen_vehicle_channels` contiene canales explícitos, el mismo servicio inicia
un feed de denuncias activas. La primera sincronización fija la denuncia más reciente sin reproducir
historial y después publica solo IDs nuevos en orden cronológico. PostgreSQL conserva únicamente un
cursor por guild y canal. El intervalo se controla con `SARP_STOLEN_VEHICLE_POLL_SECONDS`; los
canales y el módulo pueden cambiarse desde `/config` sin redesplegar.

El módulo dinámico `radio_audit` registra `/radios-faccion` y `/auditar-radio` cuando la integración
SARP está activa. El primero usa `operational_staff_roles`; el segundo usa la política separada
`radio_audit_roles`. La consulta compartida pagina `GET /my-faction/logs/radio/:factionId`, normaliza
el prefijo de canal y slot y aplica localmente el intervalo UTC exacto porque SARP filtra por día. La
paginación se detiene al alcanzar registros anteriores al intervalo y las páginas visibles reutilizan
la muestra en memoria. La auditoría recibe una copia fija del slot 1 y solo puede recorrerla en memoria
o consultar los catálogos locales del manual y códigos. Ni los radios, ni el TXT, ni los hallazgos se
persisten.

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

La suite predeterminada no llama a Discord ni a OpenAI. Cubre catálogos, configuración, trigger,
contexto multimodal y documental, efectos inmediatos de las tools, embeds, clasificación de errores,
metadata y costos.

También existe una suite de evaluaciones reales del agente, marcada como `ai_eval` y omitida por
defecto para impedir consumo accidental. Usa el prompt, guardrail, Runner, modelo y tools de
producción, pero reemplaza Discord por un publicador en memoria. Requiere una `OPENAI_API_KEY` válida
en el entorno o en `.env` y se habilita de forma explícita:

```bash
# Ejecutar un solo comportamiento para controlar el gasto.
.venv/bin/pytest tests/ai_eval -k spelling --run-ai-evals -q

# Consejo de CP-29800: agente real, antecedentes sintéticos y sin publicar en Discord.
.venv/bin/pytest tests/ai_eval -k firearm_prior --run-ai-evals -q

# Ejecutar todos los casos; cada uno consume créditos.
.venv/bin/pytest tests/ai_eval --run-ai-evals -q
```

## Límites actuales

- PostgreSQL persiste configuración, personalidades, auditoría y cada intento estructurado de
  revisión. Los locks del proceso siguen siendo efímeros y se reconstruyen al reiniciar.
- PostgreSQL reserva también cada ejecución limitada del propietario por reporte y trigger. El
  contenido continúa viviendo únicamente en Discord.
- Los callsigns se conservan por servidor y miembro. Al responder un pager se reutiliza el valor
  guardado; si todavía no existe, se solicita una vez y queda disponible para respuestas futuras.
- Las consultas personales reconstruyen un snapshot desde mensajes y reacciones conservados en
  Discord; no pueden recuperar publicaciones eliminadas ni recordar correcciones anteriores.
- Los cambios de configuración se administran como snapshots completos por guild; no hay edición
  directa de filas ni hot reload de secretos de proceso.
- Los catálogos y la guía son archivos versionados; todavía no existe sincronización con OpenMP/PCU.
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
Las conversaciones activas registran `conversation_started`, `conversation_messages_incorporated`,
`conversation_output_deferred`, `conversation_continued` y `conversation_closed`, con IDs,
cantidades y un `lease_id` correlacionable con los logs del runner. El detalle por mensaje encolado
es `debug`; saturación o cierre con pendientes generan `warning`. Nunca se registra su texto.
Consulta [Privacidad y errores](docs/PRIVACY_AND_ERRORS.md).
