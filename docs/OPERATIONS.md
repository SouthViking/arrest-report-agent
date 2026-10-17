# Operación y traspaso del bot

Esta guía cubre una instalación ya inicializada. Para crear una nueva sigue [Instalación](SETUP.md);
para roles, canales y módulos usa [Configuración](CONFIGURATION.md). Trabaja desde la raíz del
repositorio y comprueba siempre el entorno y los recursos seleccionados antes de una operación.

## Comprobación rutinaria

1. Comprueba que hay una sola instancia conectada con el token del bot.
2. Consulta `/health`. HTTP 200 y `ok: true` indican Discord, PostgreSQL y conocimiento local listos;
   HTTP 503 indica que al menos uno no está listo.
3. Lee los campos `discord`, `database` y `knowledge` para acotar el fallo. `sarp_api` indica que
   existe el proveedor, no que cada endpoint SARP esté respondiendo.
4. Ejecuta `python -m arrest_report_agent.main db verify` con el entorno del servicio. Comprueba
   `ok: true` y `missing_assets: []` en el JSON; la verificación no es una auditoría de todos los adjuntos.
5. Prueba un comando determinista habilitado y, si corresponde, una consulta acotada al agente.
   Las llamadas reales al agente consumen créditos; una prueba de escritura deja registros.

En Docker puedes consultar el endpoint con `curl http://localhost:8080/health` en Linux/macOS,
o `Invoke-RestMethod http://localhost:8080/health` en PowerShell. En Railway usa el dominio HTTPS
del servicio seguido de `/health`. El healthcheck no necesita autenticación; no expone los secretos.

Para ver logs de Compose: `docker compose logs --tail=100 bot`. En Railway selecciona el servicio,
entorno y despliegue correctos antes de leer sus logs. Correlaciona el ID de hilo o mensaje, versión,
intento y evento. No pegues variables, URLs de webhooks con secreto ni payloads en tickets.
La taxonomía y los campos están en [Privacidad y errores](PRIVACY_AND_ERRORS.md).

## Actualizar el código

1. Anota el commit y la versión activa, revisa la PR y confirma que sus checks están en verde.
2. Respalda la BD y el Bucket antes de aplicar migraciones. Conserva el commit del respaldo.
3. No uses `db import-env --replace` para actualizar. La configuración publicada debe conservarse.
4. Actualiza y aplica migraciones según el entorno.
5. Comprueba salud, `db verify`, configuración activa y un flujo funcional después del arranque.

En Python, detén primero el proceso mediante su gestor y ejecuta:

```bash
git status --short
git switch main
git pull --ff-only
python -m pip install -e '.[dev]'
python -m arrest_report_agent.main db migrate
python -m arrest_report_agent.main db verify
python -m arrest_report_agent.main
```

Si hay cambios locales, resuélvelos antes de actualizar; no uses un reset destructivo. Usa el Python
de tu entorno virtual y vuelve a iniciar el proceso con su gestor habitual en producción.

En Compose, detén el bot para evitar que el proceso antiguo escriba mientras se migra:

```bash
docker compose stop bot
git pull --ff-only
docker compose build
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db migrate
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db verify
docker compose up -d --force-recreate bot
```

Continúa solo si el paso anterior fue correcto. En Railway el merge a `main` dispara el despliegue;
mantén **Wait for CI** y el pre-deploy de `railway.json`. No ejecutes simultáneamente el gateway
local con los secretos de esa instancia.

## Cambiar configuración o secretos

Los cambios de guild se publican mediante `/config`; su **Rollback** recupera una configuración
histórica, pero no revierte código, datos operacionales, migraciones ni mensajes ya enviados.

Para rotar una credencial, crea la nueva con los permisos necesarios, actualiza el gestor de secretos,
reinicia o recrea el proceso y verifica el flujo afectado antes de retirar la anterior.
Si sospechas una filtración, revoca la credencial comprometida de inmediato aunque implique una
interrupción. Nunca sustituyas un token por otro pegándolo en un commit.

En una rotación de webhook, actualiza tanto el receptor como el proveedor. El secreto Railway
forma parte de la ruta: configura la nueva URL completa en el proveedor sin compartirla.
Cambiar `CONFIG_OWNER_USER_IDS` también exige reiniciar el proceso.

## Respaldar los datos

Conserva juntos la BD, los objetos S3, el commit de código, la revisión Alembic y un inventario
de variables sin sus valores secretos. Guarda las credenciales aparte, en un gestor con acceso
delegado. Un backup del código no sustituye los datos ni las credenciales.

La BD contiene configuración, auditoría, referencias, cuotas, entregas y registros de módulos.
El Bucket conserva avatares y adjuntos; cambiar sus claves de objeto rompe las referencias de BD.
Discord conserva mensajes e hilos: estos respaldos no permiten recuperar publicaciones eliminadas.

Define frecuencia, retención, responsables y una prueba periódica de restauración. Para una copia
coordinada, detén las escrituras del bot durante la captura de BD y Bucket y evita otros escritores.
No crees backups dentro del repositorio ni del contexto de build de Docker.

### Copia de PostgreSQL

Usa `pg_dump` de la misma versión mayor que el servidor o una versión compatible más reciente.
Prepara un directorio privado fuera del repositorio, sustituye los placeholders y conserva el requisito
TLS del proveedor. Este ejemplo pide la contraseña sin incluirla en el comando:

```bash
pg_dump --format=custom --file=../arrest-report-backups/bot.dump --host=DB_HOST --port=5432 --username=BOT_DB_USER --dbname=BOT_DB_NAME --password
pg_restore --list ../arrest-report-backups/bot.dump
```

Revisa los códigos de salida y errores. Usa un archivo o directorio nuevo por captura, cifra los
respaldos y restringe su acceso. El listado confirma que el archivo es legible, no que ya puedas
restaurar el servicio. El dump de una BD no incluye todos los roles globales del clúster.

Referencia: [pg_dump de PostgreSQL](https://www.postgresql.org/docs/16/app-pgdump.html).

### Copia del Bucket

Usa el mecanismo de respaldo del proveedor o una copia S3 que conserve rutas, bytes y metadata.
Una copia a otro Bucket accesible por el mismo endpoint puede prepararse con AWS CLI v2:

```bash
aws s3 sync s3://BUCKET_ORIGEN s3://BUCKET_RESPALDO/FECHA --endpoint-url https://S3_ENDPOINT --region REGION --copy-props default --dryrun
```

Sustituye los nombres y configura antes un perfil de AWS CLI con las credenciales y permisos de
ambos Buckets, sin escribirlos en los comandos. Revisa el plan y repite sin `--dryrun` para copiar.
No uses `--delete`. Usa un prefijo nuevo por fecha: sincronizar siempre sobre el mismo destino
no equivale a conservar versiones.

Este ejemplo requiere que tu proveedor admita la copia entre Buckets y las operaciones de metadata
usadas por `--copy-props`. Si no las admite o usa endpoints distintos, emplea un procedimiento
de exportación/importación que preserve metadata y verifica su resultado. Una descarga de archivos
por sí sola no conserva necesariamente todos los metadatos S3. Registra método, claves y cantidades.

Referencia: [sync de AWS CLI](https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html).

## Restaurar en recursos nuevos

No pruebes restauraciones sobre producción. Usa una BD vacía y un Bucket nuevos, sin conectar
el gateway con el token del bot activo. Solo restaura respaldos de confianza.

1. Detén la instancia afectada si vas a recuperar producción y conserva una copia de su estado actual.
2. Prepara los recursos nuevos. Confirma por nombre, host y entorno que no son los originales.
3. Restaura la BD vacía con un usuario propietario de sus objetos:

```bash
pg_restore --exit-on-error --no-owner --no-acl --host=NUEVO_DB_HOST --port=5432 --username=NUEVO_DB_USER --dbname=NUEVA_DB_VACIA --password ../arrest-report-backups/bot.dump
```

4. Copia los objetos al nuevo Bucket sin renombrar las claves. Para el mismo endpoint y capacidades
   del ejemplo anterior, revisa primero este plan y ejecútalo sin `--dryrun` solo en el destino nuevo:

```bash
aws s3 sync s3://BUCKET_RESPALDO/FECHA s3://NUEVO_BUCKET --endpoint-url https://S3_ENDPOINT --region REGION --copy-props default --dryrun
```

5. Selecciona primero el commit compatible con el respaldo y configura la conexión y credenciales
   de los nuevos recursos. No ejecutes `db import-env` sobre la restauración.
6. Ejecuta `db verify` sin arrancar el gateway. Comprueba además cantidades, tamaños y hashes de
   los adjuntos operacionales: la verificación de personas no cubre todos esos objetos.
7. Cuando el estado recuperado esté validado, aplica las migraciones necesarias para una versión
   más reciente si esa es la versión de destino. Vuelve a verificar.
8. Solo después del corte, arranca una instancia, comprueba `/health` y el flujo funcional.

`--no-owner --no-acl` evita reproducir propietarios y grants del hosting anterior; revisa los
permisos del usuario del bot en el destino. No uses `--clean` ni `--create` sobre una base compartida.
La verificación de restauración puede hacerse sin gateway; para una prueba funcional paralela usa
otro bot, otra guild y configuración aislada, sin workers apuntando a producción.

Referencia: [pg_restore de PostgreSQL](https://www.postgresql.org/docs/16/app-pgrestore.html).

## Volver a una versión anterior del código

Anota qué migraciones se aplicaron y si la versión anterior puede leer el esquema actual. Revertir
solo el contenedor no revierte Alembic. Si existe compatibilidad, despliega el commit anterior con
una sola instancia y verifica; si no, restaura el conjunto código/BD/Bucket en recursos nuevos y
haz un corte controlado. Restaurar una captura anterior implica perder los cambios posteriores.

No ejecutes `alembic downgrade base` ni un downgrade genérico para solucionar un incidente.
La reversibilidad debe revisarse migración por migración; algunas conservan datos o preferencias
aunque se revierta el código.

## Diagnosticar fallos frecuentes

| Síntoma | Qué revisar |
|---|---|
| Error de validación al iniciar | Secretos vacíos, variables dependientes y IDs ficticios sin reemplazar; consulta [Variables](ENVIRONMENT.md). |
| No hay configuración de guild habilitada | Se aplicaron migraciones pero faltan importación y personas. Para instalación nueva sigue [Instalación](SETUP.md). |
| Discord cierra con 4014 | Ambos intents privilegiados habilitados/aprobados en Developer Portal. |
| No aparece un slash command | Instalación con `applications.commands`, guild correcta, módulo y dependencias activos, permisos del miembro y arranque completado. |
| No puedo abrir `/config` | Tu ID en `CONFIG_OWNER_USER_IDS`, proceso reiniciado o rol admitido por `config_admin_roles`. |
| Interacción fallida | Hora e ID del caso, evento y excepción en logs; no presupongas falta de permisos si un reintento funciona. |
| Puede escribir, pero falla con imágenes o roles | `Attach Files`, permisos efectivos del canal/hilo, límites de archivos y jerarquía de roles. |
| `/health` indica `database: false` | Endpoint accesible desde ese entorno, credenciales, TLS, servicio de BD y conexiones disponibles. |
| Falta un avatar o adjunto | Bucket, claves de objetos, credenciales y permisos; no basta con restaurar solo PostgreSQL. |
| PCU/SARP no responde | Switch y token del proceso, endpoint, permisos de la API y módulo/facción de la guild. |
| La revisión falla por cuota o modelo | Credencial del proyecto, acceso al modelo, catálogo, presupuesto y clasificación en logs. |
| No llega un changelog | Webhook, commit activo, módulo, políticas de destinos y `changelog_targets`; no publiques manualmente sin revisar entregas para evitar duplicados. |

## Pruebas y actualizaciones de la documentación

Con dependencias de desarrollo instaladas y el Python del entorno virtual:

```bash
python -m ruff check .
python -m mypy src
python -m pytest -q
docker build --tag arrest-report-agent:check .
```

Sin `TEST_DATABASE_URL` los tests de PostgreSQL se omiten. Para reproducir el CI, provisiona una BD
de pruebas dedicada y exporta su URL **solo** en la terminal de tests; algunos tests destruyen su esquema.
No copies el valor de `DATABASE_URL` de producción a esa variable. La suite predeterminada no
llama a Discord ni a OpenAI; los tests de documentación comprueban enlaces y cobertura de variables.

El CI prepara PostgreSQL con [ci_containers.py](../scripts/ci_containers.py): intenta las imágenes
oficiales desde ECR y, si la descarga falla o expira, desde Docker Hub. Comparte esa selección con
la imagen base de Python del build. Espera a que PostgreSQL esté listo y falla si no consigue
prepararlo; no omite las pruebas por un error de infraestructura. El fallback no oculta errores
reales de compilación o tests. El Dockerfile de producción conserva su origen predeterminado.

Las evaluaciones reales están marcadas `ai_eval` y requieren una credencial válida. Usa un caso
acotado y asume consumo de créditos:

```bash
python -m pytest tests/ai_eval -k spelling --run-ai-evals -q
```

Cuando añadas o cambies una variable, actualiza [la referencia](ENVIRONMENT.md), [la plantilla](../default.env)
y sus tests. Cuando cambies un módulo, revisa [Configuración](CONFIGURATION.md) y [Comandos](COMMANDS.md).

## Configurar notificaciones de despliegue

Los receptores y sus credenciales son opcionales. Requieren un dominio HTTPS accesible desde el
proveedor; no conceden acceso administrativo a Discord. Usa los mismos secretos en ambos extremos.

### Changelog y alertas de despliegue

Cada versión se define una sola vez en `arrest_report_agent.__version__` y debe coincidir con el
manifest estricto [`release.json`](../src/arrest_report_agent/release.json). El manifest contiene el
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

## Transferir la administración

Antes de retirar el acceso del administrador anterior:

- [ ] El sucesor tiene acceso propio al repositorio, hosting, facturación, PostgreSQL, Bucket y
  aplicación de Discord. El acceso al servidor de Discord no concede control de la aplicación del bot.
- [ ] Tiene acceso al proyecto/proveedor de OpenAI y a la integración SARP necesaria, sin depender
  de una cuenta personal del administrador anterior.
- [ ] Sus IDs están en `CONFIG_OWNER_USER_IDS`, el proceso fue reiniciado y pudo abrir `/config`.
- [ ] Se revisaron `config_admin_roles`, whitelists de cupos y destinatarios de avisos para no
  conservar accesos personales innecesarios.
- [ ] Hay una copia reciente de BD y Bucket y una restauración comprobada.
- [ ] Conoce el commit activo, la versión, destinos de changelog/incidentes y procedimiento de actualización.
- [ ] Las credenciales están en un gestor compartido con permisos mínimos, no en este documento.
- [ ] Puede desplegar, leer logs, cambiar una configuración y recuperar el acceso sin ayuda del anterior.
- [ ] Tras verificarlo, se rotaron los secretos necesarios y se retiraron los accesos anteriores.

Para conservar el control de los mensajes históricos, mantén la misma aplicación de Discord al
trasladar el hosting. Un bot nuevo no puede editar publicaciones creadas por la cuenta del anterior.
No borres la BD ni el Bucket originales hasta completar el corte y cumplir la retención acordada.
