# Arquitectura del MVP

## Flujo

```mermaid
flowchart LR
    V["BOT_PERSONA"] --> B["PersonaProvider"]
    B --> Q["BotPersona + assets en memoria"]
    Q --> R
    Q --> Y["Perfil y presencia de Discord"]
    M["Mensaje de Discord"] --> T["DiscordAccessPolicy"]
    A["SnowflakeAllowlist x3"] --> T
    T -->|"guild, usuario y canal habilitados"| C["Contexto acotado de Discord"]
    T -->|"guild no habilitada"| I["Ignorar"]
    T -->|"usuario no habilitado"| U["Respuesta local de acceso"]
    T -->|"canal no habilitado"| W["Respuesta local con canales"]
    C --> S["Guardrail textual de alcance e intención"]
    S -->|"claramente ajeno"| O["Respuesta breve sin imágenes"]
    S -->|"permitido o ambiguo"| R["Agente principal multimodal"]
    J["Catálogo JSON de modelos"] --> R
    R --> G["Guía local"]
    R --> D["Índice de delitos"]
    R --> P["Tools de publicación"]
    E["Roles de escalamiento configurados"] --> P
    P --> X["Ruta fijada antes del runner"]
    X --> H["Mención nueva: hilo"]
    X --> N["Reply al bot: reply"]
    X --> F["Invocación en hilo: mismo hilo"]
```

`SnowflakeAllowlist` centraliza el parseo de `DISCORD_GUILD_IDS`, `ALLOWED_CHANNEL_IDS` y
`AUTHORIZED_USER_IDS`: ausente, vacío o `*` abre el scope; cualquier CSV lo restringe.
`REVIEW_CHANNEL_IDS` se conserva únicamente como alias de migración para despliegues existentes.
`DiscordAccessPolicy` es el único preprocesador del listener y se ejecuta antes de descargar contexto
o llamar a OpenAI. Valida guild, autor humano, activación por mención o respuesta directa al bot,
usuario y canal padre, en ese orden. Si Discord no incluyó el mensaje referenciado, resuelve solo esa
referencia antes de decidir; una respuesta a cualquier otra persona se ignora. Los rechazos de
usuario o canal usan respuestas locales con cooldown y nunca alcanzan el Runner.

En una redirección por canal, el adaptador resuelve cada ID configurado primero desde la caché de la
guild y confirma los ausentes mediante REST. Solo muestra canales de texto visibles para el bot y el
solicitante. `NotFound` marca el ID como obsoleto durante la vida del proceso; `Forbidden` y errores
temporales producen una orientación sin referencias y un warning estructurado. Esta comprobación es
local a Discord y no consume una ejecución del agente.

Una vez superada la política local, `request_scope` clasifica alcance e intención usando solo el
texto de la solicitud actual y señales compactas de contexto. Es bloqueante
(`run_in_parallel=False`), usa el modelo soportado con `reasoning=none` y no recibe imágenes. Solo
bloquea solicitudes inequívocamente ajenas; saludos, peticiones ambiguas y conversaciones vinculadas
al expediente continúan hacia el agente principal.

Las tools de publicación reciben un `ReviewTarget` derivado del evento aprobado y no aceptan guilds,
canales o mensajes arbitrarios como argumentos. Por ello sus reacciones, hilos y mensajes quedan
acotados al objetivo que atravesó la política. El mensaje raíz es la unidad de exclusión mutua: dos
invocaciones simultáneas para el mismo reporte no generan dos runs.

Ante una revisión inconclusa o una falla técnica, el adaptador resuelve
`REVIEW_ESCALATION_ROLE_IDS` dentro de la guild y menciona una sola vez únicamente los roles que
todavía existen. La mención y su `AllowedMentions` se construyen de forma determinista; el modelo no
recibe IDs de roles ni decide a quién derivar. El alias legado `MANUAL_REVIEW_ROLE_ID` sigue
admitiendo una configuración de un solo rol.

La política también conserva si la activación fue una mención o una respuesta directa al bot. Una
mención nueva en un canal padre fija la ruta `thread`; un reply al bot en el canal fija
`inline_reply`; dentro de un hilo siempre se usa ese mismo hilo. El modelo no recibe IDs de destino
ni decide esta ruta. El hilo se crea de forma perezosa con la primera publicación, por lo que un
guardrail bloqueado no deja hilos vacíos.

## Capas

- `domain`: modelos Pydantic y enums que definen el contrato de revisión.
- `config`: entorno, allowlists, proveedor de personas y catálogos JSON validados.
- `knowledge`: guía versionada e índice del JSON del PCU, cargados y validados al iniciar.
- `application`: prompt, tools, contexto del runner, errores, tracing y orquestación.
- `infrastructure/discord`: política previa, reconstrucción de hilos, embeds, reacciones y comandos.

Los comandos personales `/mis-reportes` y `/mi-resumen` siguen una ruta determinista separada
del runner. Leen de forma acotada los canales de revisión visibles, atribuyen publicaciones raíz al
usuario que invoca el comando y derivan el estado solo de las reacciones creadas por esta cuenta del
bot. Sus respuestas son efímeras y nunca incorporan información de otros miembros.

Los comandos generales `/guia`, `/cargos` y `/faq` también evitan el runner. Presentan la
guía y el catálogo versionados mediante vistas efímeras ligadas al usuario: los selectores y botones
editan el mismo mensaje, mientras que la búsqueda de cargos resuelve localmente código, título,
categoría y descripción con un límite acotado de resultados.

El contrato funcional completo de los comandos está documentado en [Comandos de Discord](COMMANDS.md).

Los comandos `/pendientes` y `/ranking` reutilizan el mismo finder acotado, pero permiten
consultar autores distintos al solicitante. Una política de roles independiente se construye desde
`SUPERVISOR_ROLE_IDS` y se aplica antes de cualquier lectura; esta frontera permite reemplazar la
configuración por guild desde una base de datos sin modificar las vistas ni las agregaciones.
- `bootstrap`: composición de dependencias y healthcheck.

## Personas

`BOT_PERSONA` es obligatorio y selecciona un perfil empaquetado. `PackagedPersonaProvider` valida el
JSON y carga su avatar antes de construir Discord o el runner. El resultado inmutable se comparte
en memoria; prompt, embeds, mensajes deterministas, presencia, trazas y perfil no vuelven a leer el
entorno ni los archivos.

`PersonaProvider` separa el contrato de su almacenamiento. Una implementación futura puede obtener
el mismo `BotPersona` y assets desde una base de datos sin cambiar los consumidores. En el MVP
existen `walter_vigil` y `roy_velazco`; cambiar la selección requiere reiniciar el proceso.

La personalidad separa la voz de conversación natural de la voz breve del resumen superior de una
revisión. El prompt y el esquema de `publish_review` reservan esos registros para sus contextos; los
hallazgos, cargos, recomendaciones y motivo de derivación permanecen técnicos,
formales y neutrales. Cada registro incluye few-shots tipados, validados y limitados para orientar la
voz sin cargar una conversación completa. El renderer muestra el resumen y los campos técnicos en
secciones distintas.

Cada perfil también declara `messages.access`, `messages.failures`, `messages.help` y
`messages.interface`. Estos registros alimentan el texto local que debe conservar la voz del
personaje: redirecciones, autorizaciones, rechazos de alcance, fallas operativas seguras, ayuda,
descripciones de comandos, selectores, modales, botones, navegación, estados vacíos, embeds y
ownership de interfaces privadas. Los campos con interpolación usan un conjunto exacto de
placeholders validado al arrancar; no se permite acceder a atributos ni aplicar formatos dinámicos.
Los datos y nombres que provienen del catálogo o la guía, además de los hallazgos técnicos de una
revisión, permanecen en la capa funcional para no contaminar información objetiva con la
personalidad.

En `on_ready`, `DiscordProfileSynchronizer` aplica username y avatar desde la persona, el banner
verde musgo global de LSSD y la biografía por guild. Los campos globales se envían por separado para
que el cooldown de uno no bloquee los demás. Un lock evita duplicados durante reconexiones; un error
conserva el bot operativo y permite reintentar solo el campo pendiente en el siguiente `on_ready`.
El nickname es opcional y `null` significa que no se administra por guild.

## Herramientas del agente

- `get_report_guide(sections)`: devuelve solo las secciones solicitadas.
- `list_charge_index(category_ids, query, fields, match_mode, fuzzy, limit)`: sin filtros lista
  categorías; con filtros busca sobre código, título, tipo, categoría o descripción y retorna IDs y
  resultados compactos. Permite exigir todos/cualquiera de los términos y tolerar typos de forma
  explícita; no acepta expresiones regulares.
- `get_charge_details(charge_ids)`: devuelve hasta diez delitos por ID único.
- `send_message(content)`: mensaje intermedio reutilizable con Markdown de Discord, enlaces y emojis
  moderados; ejecuta el envío de inmediato, pero permite que el agente continúe trabajando.
- `finish_interaction(content)`: envía la respuesta conversacional final y termina explícitamente la
  ejecución; también sirve para devolver el turno al usuario mediante una pregunta aclaratoria.
- `publish_review(publication)`: tool terminal que ejecuta inmediatamente el veredicto en Discord.

`parallel_tool_calls` permanece deshabilitado para mantener un orden determinista en efectos
externos. Solo `finish_interaction` y `publish_review` detienen el runner; `send_message` puede
ejecutarse varias veces antes del cierre. El agente conserva `tool_choice=required` durante todas las
rondas: después de consultar conocimiento o enviar un avance debe escoger una tool siguiente y no
puede finalizar con texto suelto o un mensaje vacío. Las tools locales de conocimiento pueden usarse
también para responder dudas.

## Lifecycle del agente

`ReviewLifecycleHooks` recibe `on_agent_start` después del guardrail bloqueante. Si el preflight
clasificó `review` y existe material revisable, añade `⏳` una sola vez; las conversaciones y las
revisiones sin antecedentes no reciben esa marca. `on_agent_end` registra en contexto el cierre
exitoso. Errores y timeouts permanecen bajo el `try/except` del servicio, porque el hook de término no
se ejecuta cuando el agente no produce una salida final. Así se evita exponer `begin_review` y la
ronda adicional de modelo que requería una tool no terminal.

## Presencia global

`ReviewPresence` asigna una sesión a cada interacción y publica la actividad de mayor prioridad:
`review > knowledge > conversation > idle`. Una sesión solo puede promocionarse, por lo que una
búsqueda de conocimiento no degrada una revisión. Al terminar la actividad prioritaria se recupera
la siguiente que siga activa; el último cierre vuelve a reposo. Solo se llama a Discord cuando cambia
el modo global. `on_ready` resincroniza tras una reconexión y cualquier fallo cosmético se registra
sin interrumpir el expediente. Los cuatro textos provienen de la persona cargada.

## Resolución del modelo y costos

El entorno selecciona únicamente `OPENAI_MODEL` y `OPENAI_REASONING_EFFORT`. Al arrancar, el runtime
carga `config/data/models.json`, lo valida con Pydantic y rechaza modelos o efforts no soportados. El
catálogo aporta detalle de imagen, verbosity, límite de salida, service tier y tarifas.

El costo se calcula desde `Usage` del Agents SDK. Se conservan los contadores crudos y cada llamada
se tarifa de forma independiente para seleccionar correctamente el tramo de contexto. Las escrituras
de caché tienen su propia tarifa; `reasoning_tokens` es un subconjunto de `output_tokens` y no se suma
por segunda vez. El uso del guardrail se agrega al wrapper del run principal, por lo que sus tokens y
costo quedan incluidos en el total operativo de la solicitud. Si una validación local falla después
de que el Runner terminó, el log conserva el uso completo del resultado en vez de degradarlo al uso
parcial del guardrail.

## Contexto de hilo

No se usan sesiones ni persistencia del SDK. Cada mención reconstruye un input autocontenido con:

- reporte raíz;
- mensajes recientes del hilo en orden cronológico;
- referencias de respuesta y extractos acotados;
- indicador de mensaje editado;
- solicitud actual;
- nombre visible del solicitante y hasta ocho nombres de roles no administrados por participante,
  ordenados por jerarquía y presentados en líneas compactas, sin IDs de roles;
- imágenes y documentos del mensaje actual;
- adjuntos del reporte raíz y una cantidad acotada de imágenes y archivos históricos recientes.

Cada mensaje se reconstruye como un turno nativo. Solo los mensajes producidos por la cuenta
autenticada del bot usan el rol `assistant`; miembros, webhooks y otros bots permanecen como `user`.
Los adjuntos conservan el turno donde se publicaron y una versión documental incluida en la
solicitud actual prevalece sobre versiones anteriores del mismo material.

Los límites de mensajes, caracteres, imágenes, archivos y tamaño combinado se aplican antes del
runner. El reporte raíz y la solicitud actual se protegen al recortar; el resto conserva los
mensajes más recientes hasta `THREAD_CONTEXT_MAX_MESSAGES`. El input indica si el contexto quedó
`complete`, `truncated` o `unavailable` para evitar conclusiones excesivas.

## Extensión futura

`ReviewPublisher` aísla el runner de Discord y las fuentes locales no dependen del bot. Una futura
persistencia puede almacenar configuración, intentos, veredictos y threads sin cambiar el contrato
de `ReviewPublication`. Una API de PCU/OpenMP puede reemplazar `ChargeCatalog` mediante el mismo
interfaz de consulta; una tabla por guild/facción puede sustituir las variables de entorno.
