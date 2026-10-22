# Variables de entorno del bot

Usa [default.env](../default.env) como plantilla y sigue [Instalación](SETUP.md) para el orden de
arranque. Los nombres y validaciones se definen en [AppSettings](../src/arrest_report_agent/config/settings.py).
Esta referencia distingue secretos del proceso, datos de importación y configuración persistida.

## Dónde se guardan y cuándo se aplican

- **Local:** `.env` en la raíz. Las variables ya exportadas por la terminal tienen prioridad;
  evita mezclar sin revisar entornos de pruebas y producción.
- **Docker:** Compose inyecta `.env` en el contenedor; `BOT_ENV_FILE` permite seleccionar otro archivo.
- **Railway:** variables y referencias a servicios en el entorno del servicio del bot.
- **PostgreSQL:** roles, canales, persona, modelo y opciones de módulos ya publicados por guild.

Cambiar el entorno de un proceso en marcha no recarga sus secretos. En Compose, `docker compose
restart` tampoco incorpora cambios del archivo de variables: recrea el servicio con `docker compose
up -d --force-recreate bot`. En Railway aplica el cambio con un nuevo despliegue.
Para configuración funcional usa `/config`, no una reimportación del entorno.

## Credenciales y administración

| Variable | Valor inicial | Uso y requisito |
|---|---|---|
| `DISCORD_TOKEN` | Vacío en plantilla | Obligatoria, incluso para los comandos `db`; token del bot de Discord. |
| `OPENAI_API_KEY` | Vacío en plantilla | Obligatoria, incluso para los comandos `db`; clave para las llamadas del agente. |
| `DATABASE_URL` | Vacío | Obligatoria para migraciones, verificación y runtime; conexión a la BD dedicada. |
| `PERSONA_ASSET_BUCKET` | Vacío | Obligatoria para importación aplicada, verificación y runtime; nombre del Bucket S3. |
| `PERSONA_ASSET_ENDPOINT` | Vacío | Endpoint S3 completo del Bucket, accesible desde el proceso. |
| `PERSONA_ASSET_REGION` | `auto` | Región usada para firmar; usa la que indique tu proveedor. |
| `PERSONA_ASSET_ACCESS_KEY_ID` | Vacío | Credencial S3 del bot. |
| `PERSONA_ASSET_SECRET_ACCESS_KEY` | Vacío | Secreto de esa credencial S3. |
| `CONFIG_OWNER_USER_IDS` | Vacío | CSV de usuarios owner. Configúralo antes del primer arranque para poder abrir `/config`; sin `*`. |
| `REPORT_OWNER_QUOTA_ADMIN_USER_IDS` | Vacío | Whitelist opcional de usuarios para ampliar cupos. Se combina con los roles gestores de la configuración del módulo; sin `*`. |

No copies secretos en ejemplos ni commits. OpenAI recomienda cargarlos en el servidor desde variables
o un gestor de secretos, no exponerlos en código cliente: [Autenticación](https://developers.openai.com/api/reference/overview#authentication).
El token de SARP y las claves S3 tampoco se almacenan como opciones de `/config`.

## Conexión a PostgreSQL

| Variable | Predeterminado | Uso |
|---|---|---|
| `DATABASE_POOL_SIZE` | `3` | Conexiones base del pool, entre 1 y 20. |
| `DATABASE_MAX_OVERFLOW` | `2` | Conexiones extra, entre 0 y 20. |
| `DATABASE_POOL_TIMEOUT_SECONDS` | `10` | Espera por conexión, mayor que 0 y hasta 120 s. |
| `DATABASE_POOL_RECYCLE_SECONDS` | `900` | Reciclado de conexiones, entre 60 y 86400 s. |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `15000` | Límite de una consulta, entre 1000 y 300000 ms. |
| `CONFIG_REFRESH_SECONDS` | `60` | Reconciliación de configuración, entre 5 y 3600 s; complementa `LISTEN/NOTIFY`. |

Usa el endpoint apropiado para cada red. Los clientes del bot normalizan la URL PostgreSQL para
SQLAlchemy y asyncpg; las herramientas nativas `psql` y `pg_dump` no aceptan prefijos de driver como
`postgresql+asyncpg://`. Conserva los requisitos TLS del proveedor al cambiar de conexión.

## Datos usados solo para la importación inicial

Estos valores inicializan PostgreSQL mediante `db import-env`. Después usa `/config` para cambiar
el acceso, persona, modelo y módulos de las guilds importadas; editar el antiguo valor no sustituye la revisión
publicada. La plantilla trae IDs ficticios, distintos de los defaults abiertos del parser.

| Variable | Default del parser | Uso al importar |
|---|---|---|
| `DISCORD_GUILD_IDS` | `*` | Servidores a importar; usa IDs explícitos para no importar todos los visibles. |
| `REPORT_CHANNEL_IDS` | `*` | Archivo de reportes. Usa canales explícitos para el acceso del propietario. |
| `AUTHORIZED_USER_IDS` | `*` | Usuarios iniciales con acceso completo. Empieza con tu propio ID. |
| `SUPERVISOR_ROLE_IDS` | `*` | Roles iniciales de supervisión; también se usan al construir otras políticas iniciales. |
| `REVIEW_ESCALATION_ROLE_IDS` | Vacío | Roles explícitos para avisos de derivación; vacío no menciona a nadie. |
| `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` | `*` | Canales de entrada para solicitudes de roles. |
| `ROLE_REQUEST_ALLOWED_ROLE_IDS` | `*` | Catálogo inicial de roles seguros solicitables. |
| `ROLE_REQUEST_REVIEWER_ROLE_IDS` | Vacío | Revisores explícitos; sin `*`. En una guild nueva sin defaults propios, vacío deja el módulo deshabilitado. |
| `BOT_PERSONA` | Sin valor | Obligatoria para importar; `walter_vigil` o `roy_velazco` incluidos. |
| `OPENAI_MODEL` | `gpt-6-luna` | Modelo inicial, admitido por el catálogo empaquetado y tu cuenta. |
| `OPENAI_REASONING_EFFORT` | `medium` | Esfuerzo inicial compatible con el modelo elegido. |

En las allowlists abiertas, ausente, vacío y `*` significan permitir a todos. No se pueden mezclar
`*` e IDs. Las listas explícitas de owners, revisores y escalamiento no admiten comodín.
Los defaults específicos de LSSD pueden inicializar módulos y políticas adicionales; revisa el
`--dry-run`. El importador no es una sincronización periódica ni un editor general para añadir
servidores a una instalación en funcionamiento. Para ampliar una instalación multi-guild, prepara
y revisa un plan de importación que conserve las configuraciones existentes.

## Límites iniciales y contexto

Salvo `MAX_CONCURRENT_REVIEWS`, estos valores alimentan la configuración inicial de cada guild.
Los límites de cantidad y tamaño de adjuntos son también techos del proceso: el valor efectivo
es el menor entre el de PostgreSQL y el del entorno. Cambiar solo el entorno no eleva una
configuración de guild más restrictiva. Los demás valores se administran desde `/config` tras importar.

| Variable | Predeterminado | Uso |
|---|---|---|
| `REVIEW_TIMEOUT_SECONDS` | `180` | Tiempo total de revisión, mayor que cero. |
| `MAX_AGENT_TURNS` | `12` | Límite de turnos del agente, entre 2 y 50. |
| `MAX_CONCURRENT_REVIEWS` | `2` | Ejecuciones simultáneas por proceso, entre 1 y 20; no es un límite distribuido. |
| `MAX_IMAGES_PER_REVIEW` | `10` | Cantidad máxima de imágenes, entre 1 y 30. |
| `MAX_IMAGE_BYTES` | `10485760` | Tamaño máximo por imagen. |
| `MAX_FILES_PER_REVIEW` | `5` | Cantidad de documentos, entre 1 y 10. |
| `MAX_FILE_BYTES` | `10485760` | Tamaño máximo por documento. |
| `MAX_TOTAL_FILE_BYTES` | `52428800` | Total máximo de archivos, como máximo 50 MiB. |
| `CHANNEL_CONTEXT_MAX_MESSAGES` | `5` | Contexto del canal padre, entre 0 y 25; cero lo desactiva. |
| `THREAD_CONTEXT_MAX_MESSAGES` | `20` | Historial del hilo, entre 1 y 100 mensajes. |
| `THREAD_CONTEXT_MAX_CHARS` | `8000` | Presupuesto de texto del hilo, mínimo 500 caracteres. |
| `THREAD_CONTEXT_MAX_MESSAGE_CHARS` | `1500` | Máximo por mensaje, mínimo 100 caracteres. |
| `THREAD_CONTEXT_MAX_HISTORICAL_IMAGES` | `2` | Recuperaciones históricas bajo demanda, entre 0 y 10. |
| `THREAD_CONTEXT_MAX_HISTORICAL_FILES` | `2` | Documentos históricos bajo demanda, entre 0 y 5. |
| `THREAD_AUTO_ARCHIVE_MINUTES` | `1440` | Archivo de hilos: 60, 1440, 4320 o 10080 minutos. |

## Proceso y observabilidad

| Variable | Predeterminado | Uso |
|---|---|---|
| `APP_ENV` | `local` | Etiqueta de entorno en logs y traces; usa una distinta para pruebas y producción. |
| `LOG_LEVEL` | `INFO` | Nivel del logging. |
| `LOG_JSON_INDENTED` | `true` | `false` produce un objeto JSON por línea. |
| `HEALTH_HOST` | `0.0.0.0` | Interfaz del servidor de salud y webhooks. |
| `HEALTH_PORT` | `8080` | Puerto local si no se define `PORT`. |
| `PORT` | Lo inyecta Railway | Tiene prioridad sobre `HEALTH_PORT`; no lo fijes manualmente en Railway. |
| `OPENAI_MAX_RETRIES` | `2` | Reintentos del cliente, entre 0 y 10. |
| `OPENAI_TRACING_ENABLED` | `true` | Envío de traces del SDK. |
| `OPENAI_TRACE_INCLUDE_SENSITIVE_DATA` | `false` | Mantén `false` salvo una investigación expresamente autorizada. |
| `TRACE_WORKFLOW_NAME` | `arrest_report_review` | Nombre del workflow de tracing. |

El healthcheck no prueba todas las APIs externas ni todos los adjuntos. Consulta
[Operación](OPERATIONS.md) para interpretar sus campos y hacer una comprobación funcional.

## Integración SARP

| Variable | Predeterminado | Uso |
|---|---|---|
| `SARP_API_ENABLED` | `false` | Switch global; requiere reiniciar al cambiarlo. |
| `SARP_API_TOKEN` | Vacío | Obligatorio cuando SARP está activo; token Bearer emitido para esa API. |
| `SARP_API_BASE_URL` | `https://api.gta-rol.com/api` | Base sin query, fragmento ni credenciales. HTTPS salvo tests en localhost. |
| `SARP_API_TIMEOUT_SECONDS` | `10` | Timeout por intento, mayor que 0 y hasta 120 s. |
| `SARP_API_MAX_RETRIES` | `2` | Reintentos de lecturas seguras, entre 0 y 10. |
| `SARP_EMERGENCY_CALL_POLL_SECONDS` | `30` | Feed de llamadas, entre 10 y 3600 s; requiere módulo y destinos activos. |
| `SARP_STOLEN_VEHICLE_POLL_SECONDS` | `60` | Feed de robados, entre 10 y 3600 s; requiere módulo y destinos activos. |
| `SARP_ONLINE_PLAYERS_CACHE_SECONDS` | `30` | Caché de conectados públicos, entre 0 y 300 s. |
| `ONLINE_INMATES_PAGE_SIZE` | `10` | Nombres por página en `/internos`, entre 1 y 25. |

Activar el switch no configura todas las funciones: cada guild necesita habilitar los módulos,
su facción y sus roles/destinos. Apagarlo no convierte en aprobadas las comprobaciones pendientes
de órdenes de arresto. El modelo no decide el endpoint ni recibe este token.

## Webhooks y monitor de consumo opcionales

Todos pueden quedar sin configurar para un arranque básico. Consulta [Operación](OPERATIONS.md)
para configurar los receptores y sus destinos en Discord.

| Variable | Predeterminado | Uso y dependencia |
|---|---|---|
| `RAILWAY_WEBHOOK_SECRET` | Vacío | Activa el receptor Railway; 43–128 caracteres URL-safe. Se envía en `Authorization: Bearer <secreto>`, nunca en la URL. |
| `RAILWAY_PROJECT_ID` | Sin valor | Proyecto aceptado por ese receptor y scope del monitor de recursos. |
| `RAILWAY_ENVIRONMENT_ID` | Sin valor | Entorno aceptado; necesario junto con proyecto y servicio al activar el receptor. |
| `RAILWAY_SERVICE_ID` | Sin valor | Servicio aceptado por el receptor. |
| `RAILWAY_GIT_COMMIT_SHA` | Lo inyecta Railway | Hace coincidir la instancia con el evento; no inventes este SHA manualmente. |
| `GITHUB_WEBHOOK_SECRET` | Vacío | Receptor de `workflow_run`; 32–256 caracteres, idéntico al configurado en GitHub. |
| `BUY_ME_A_COFFEE_WEBHOOK_SECRET` | Vacío | Receptor de aportes; secreto de firma del proveedor, entre 16 y 512 caracteres. |
| `OPENAI_ADMIN_KEY` | Vacío | Clave administrativa independiente, solo para el monitor de costos. No sustituye `OPENAI_API_KEY`. |
| `OPENAI_PROJECT_ID` | Sin valor | Scope del monitor de costos; exige `OPENAI_ADMIN_KEY`. No selecciona el modelo ni cambia la clave del agente. |
| `RAILWAY_API_TOKEN` | Vacío | Credencial del monitor Railway; requiere `RAILWAY_WORKSPACE_ID`. |
| `RAILWAY_WORKSPACE_ID` | Sin valor | Workspace de facturación; requiere `RAILWAY_API_TOKEN`. |
| `COST_MONITOR_TIMEOUT_SECONDS` | `10` | Timeout por consulta del monitor, mayor que 0 y hasta 60 s. |
| `COST_MONITOR_CACHE_SECONDS` | `300` | Caché del monitor, entre 0 y 3600 s. |

Genera secretos propios para los webhooks de Railway y GitHub:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Introduce el resultado mediante el gestor de secretos, no en capturas ni historial compartido.
Para Buy Me a Coffee usa el secreto de firma del proveedor.

## Aliases compatibles y variables de herramientas

Los nombres canónicos anteriores evitan ambigüedades. El código admite estos aliases; si defines
ambos nombres, la primera opción canónica tiene prioridad:

| Canónico | Aliases |
|---|---|
| `PERSONA_ASSET_BUCKET` | `BUCKET` |
| `PERSONA_ASSET_ENDPOINT` | `ENDPOINT` |
| `PERSONA_ASSET_REGION` | `REGION` |
| `PERSONA_ASSET_ACCESS_KEY_ID` | `ACCESS_KEY_ID` |
| `PERSONA_ASSET_SECRET_ACCESS_KEY` | `SECRET_ACCESS_KEY` |
| `DISCORD_GUILD_IDS` | `DISCORD_GUILD_ID` |
| `REPORT_CHANNEL_IDS` | `ALLOWED_CHANNEL_IDS`, `REVIEW_CHANNEL_IDS` |
| `REVIEW_ESCALATION_ROLE_IDS` | `MANUAL_REVIEW_ROLE_ID` |
| `BUY_ME_A_COFFEE_WEBHOOK_SECRET` | `BMC_WEBHOOK_SECRET` |

`PORT` tiene prioridad sobre `HEALTH_PORT`, a diferencia del orden de los otros aliases.
`TEST_DATABASE_URL` es exclusivo de tests de integración: nunca debe señalar producción.
`BOT_ENV_FILE` lo interpreta Compose, no `AppSettings`; no cambia por sí mismo el `.env` leído
por una ejecución Python fuera de Docker.
