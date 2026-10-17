# Instalación del bot

Sigue estos pasos para crear una instancia nueva. Si vas a mover una instancia existente, conserva
su base de datos, Bucket y aplicación de Discord y sigue [Operación y traspaso](OPERATIONS.md).
No ejecutes una importación nueva sobre la base de producción para cambiar de hosting.

## 1 Preparar Discord y las cuentas

1. Obtén acceso al repositorio y al hosting donde se ejecutará el proceso.
2. Crea una aplicación con bot en Discord Developer Portal y guarda su token en el gestor de
   secretos del hosting o en tu `.env` local.
3. Habilita **Message Content Intent** y **Server Members Intent**. No se requiere Presence Intent.
4. Instala la aplicación en tu servidor con los scopes `bot` y `applications.commands`.
5. Configura los [permisos por canal y la jerarquía de roles](PERMISSIONS.md), incluido **Attach Files**
   para publicar imágenes. `Manage Roles` solo hace falta si habilitas la gestión de roles.
6. Activa el modo desarrollador de Discord para copiar los IDs de servidor, canales, usuarios y roles.
   Anota tu ID para darte acceso inicial a `/config` mediante `CONFIG_OWNER_USER_IDS`.
7. Prepara una credencial de OpenAI con acceso al modelo del catálogo elegido. Guárdala como
   `OPENAI_API_KEY`, no en el código. Consulta la [autenticación oficial](https://developers.openai.com/api/reference/overview#authentication).

No publiques tokens en capturas, comandos compartidos ni tickets. El token de Discord y los IDs
no son lo mismo: los IDs se usan para configurar; el token permite autenticarse como el bot.

## 2 Preparar PostgreSQL y el Bucket

Provisiona PostgreSQL 14 o superior con una base dedicada y un usuario que pueda aplicar migraciones
y operar sus tablas. Provisiona también un Bucket privado compatible con S3, con acceso para leer,
escribir y comprobar los objetos del bot. El Bucket conserva avatares y adjuntos gestionados.

Necesitarás `DATABASE_URL`, el nombre del Bucket, endpoint, región y sus dos credenciales S3.
La lista exacta está en [Variables de entorno](ENVIRONMENT.md).

En Railway puedes crear PostgreSQL y el Bucket en el mismo proyecto y vincular las variables del
servicio mediante referencias. Desde tu equipo local usa una conexión PostgreSQL accesible desde
fuera del hosting; una dirección privada de Railway no es accesible desde tu PC. En un contenedor,
`localhost` apunta al propio contenedor, no a tu equipo ni a otro servicio.

El `docker-compose.yml` incluido solo inicia el bot: no crea PostgreSQL, un Bucket ni sus backups.
Usa credenciales distintas para desarrollo, pruebas y producción.

## 3 Obtener el código y preparar el ejecutor

Ejecuta todos los comandos desde la raíz del repositorio. Usa la rama `main` o un commit publicado
que hayas elegido; no trabajes sobre una rama de cambios sin revisar.

```bash
git clone https://github.com/SouthViking/arrest-report-agent.git
cd arrest-report-agent
```

Elige Python o Docker para ejecutar los comandos iniciales. Aunque el hosting final sea Railway,
puedes inicializar sus servicios desde un equipo con Python o Docker sin abrir el gateway de Discord.

### Python en Linux o macOS

```bash
cp -n default.env .env
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

En los pasos siguientes, `python` significa `.venv/bin/python` de este entorno. Puedes usar ese
ejecutable completo o activar el entorno con `source .venv/bin/activate`.

### Python en Windows PowerShell

```powershell
if (Test-Path -LiteralPath .env) { throw 'Ya existe .env; revisa su configuración antes de continuar.' }
Copy-Item -LiteralPath default.env -Destination .env
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

En los pasos siguientes, `python` significa `.\.venv\Scripts\python.exe`. No es necesario cambiar
la política de ejecución de PowerShell ni activar el entorno para usar la ruta completa.

### Docker

Instala y arranca Docker con Compose. Copia `default.env` a `.env` con el comando correspondiente
a tu sistema. Completa el archivo antes de ejecutar los comandos de inicialización de la sección 5.

No sobrescribas un `.env` existente: conserva sus secretos en un lugar seguro antes de modificarlo.

## 4 Completar las variables iniciales

Edita `.env` localmente, sin subirlo a Git. Completa primero Discord, OpenAI, PostgreSQL y S3.
Luego reemplaza **todos los IDs ficticios** de la plantilla y define tu acceso:

```dotenv
CONFIG_OWNER_USER_IDS=111111111111111111
DISCORD_GUILD_IDS=222222222222222222
REPORT_CHANNEL_IDS=333333333333333333
AUTHORIZED_USER_IDS=111111111111111111
SUPERVISOR_ROLE_IDS=444444444444444444
BOT_PERSONA=walter_vigil
```

Estos números son ejemplos: reemplázalos por tu usuario, servidor, canal de reportes y rol supervisor.
Empieza restringiendo el agente a tu usuario. Una lista ausente, vacía o con `*` permite a todos
en las allowlists legacy; **no** es una forma de desactivar el acceso.

Deja inicialmente `SARP_API_ENABLED=false`, los webhooks sin secreto y los revisores de solicitudes
de roles vacíos. Habilita las integraciones después de comprobar el arranque básico. Para revisar
arrestos desde PCU necesitarás activar SARP y configurar el módulo correspondiente.

Los perfiles incluidos son Walter y Roy. El importador y algunas migraciones contienen valores
iniciales específicos de LSSD; otra guild no hereda una configuración completa de esa facción.
Revisa siempre el plan y los módulos antes de publicar o abrir el acceso a otros usuarios.

## 5 Inicializar una base nueva

Ejecuta cada comando por separado y continúa solo si termina correctamente. Los comandos `db`
no conectan el gateway del bot ni sincronizan su perfil de Discord.

### Con Python

```bash
python -m arrest_report_agent.main db migrate
python -m arrest_report_agent.main db import-env --dry-run
python -m arrest_report_agent.main db import-env --apply
python -m arrest_report_agent.main db verify
```

### Con Docker

```bash
docker compose build
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db migrate
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db import-env --dry-run
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db import-env --apply
docker compose run --rm --no-deps bot python -m arrest_report_agent.main db verify
```

Comprueba los resultados:

| Comando | Resultado que debes revisar |
|---|---|
| `db migrate` | Termina sin error y devuelve `status: migrated`. |
| `db import-env --dry-run` | Devuelve `status: dry_run`; servidores, políticas y módulos coinciden con tu instalación. Consulta Discord en lectura, sin escribir en la BD ni el Bucket. |
| `db import-env --apply` | Devuelve `status: applied`; importa los perfiles, avatares y la configuración inicial. Con el mismo checksum devuelve `noop`. |
| `db verify` | El JSON contiene `ok: true`, al menos una guild y `missing_assets: []`. No basta con mirar el código de salida. |

El importador necesita que el bot ya esté instalado en el servidor y pueda consultar sus canales
y roles. No uses `--replace` para saltarte un error: puede reemplazar la configuración inicial de
una instalación existente. Para cambios posteriores usa `/config`.

`db verify` comprueba los assets de personas registrados; no audita todos los adjuntos operacionales
ni certifica la conexión a SARP u OpenAI. La comprobación final incluye una prueba funcional.

## 6 Arrancar una sola instancia

### Python

```bash
python -m arrest_report_agent.main
```

Mantén el proceso supervisado por tu hosting o gestor de servicios. Cerrar la terminal termina
esta ejecución local. No arranques otra copia con el mismo token para probar en paralelo.

### Docker

```bash
docker compose up --build -d
docker compose logs --tail=100 bot
```

El contenedor ejecuta el bot; las migraciones no se aplican automáticamente con este Compose.
Repite solo `db migrate` al actualizar una instancia ya inicializada, no `db import-env`.

### Railway

1. Completa antes la inicialización contra la BD y el Bucket del proyecto. Puedes usar los comandos
   anteriores con la conexión pública apropiada, sin ejecutar el bot localmente.
2. Crea o conecta el servicio del bot al repositorio y a `main`. Usa el `Dockerfile` y `railway.json`.
3. Carga los secretos y variables de proceso en el servicio, con referencias a PostgreSQL y S3.
   No pegues tu `.env` completo: los valores ya importados de persona, roles y canales viven en BD.
4. Mantén una réplica y ejecución continua; evita la suspensión automática. El bot necesita
   conservar su conexión WebSocket con Discord.
5. Activa **Wait for CI** para que los cambios con checks fallidos no sustituyan el despliegue activo.
6. Despliega y comprueba `/health`. `railway.json` aplica `db migrate` como pre-deploy; no importa
   configuraciones ni avatares. Railway inyecta `PORT`, que tiene prioridad sobre `HEALTH_PORT`.
7. Si quieres recibir webhooks, configura un dominio HTTPS y sigue [Operación](OPERATIONS.md).

No arranques primero sobre una BD vacía esperando que el gateway la rellene: el runtime requiere
al menos una configuración habilitada. No clones los secretos de producción para una prueba con
otro bot o servidor; prepara recursos separados.

## 7 Terminar la configuración del servidor

Abre `/config` con el usuario definido en `CONFIG_OWNER_USER_IDS`. Sigue
[Configuración inicial](CONFIGURATION.md) para revisar administradores, roles, canales, persona,
acceso del agente y módulos. Publica el borrador antes de probarlos con otros miembros.

## 8 Comprobar que está listo

- [ ] Solo hay una instancia usando este token.
- [ ] `db verify` contiene `ok: true`.
- [ ] `/health` responde HTTP 200 y `ok: true` después de conectar Discord.
- [ ] `/ayuda` responde y tu usuario puede abrir `/config`.
- [ ] Otro usuario sin permisos no puede acceder a la administración.
- [ ] Los canales de prueba tienen los permisos efectivos necesarios, también en hilos.
- [ ] Las integraciones habilitadas responden; las deshabilitadas no se anuncian como disponibles.
- [ ] Tras habilitar el agente, una consulta controlada responde. Esta prueba sí puede consumir OpenAI.
- [ ] Si habilitaste `/revisar-arresto`, un reporte de prueba válido obtiene el nombre desde PCU,
  publica sus adjuntos y abre el hilo correspondiente.

Una prueba de escritura deja mensajes y registros: hazla en un entorno de pruebas o en un canal
autorizado para ello. Para diagnosticar fallos y respaldar la instalación sigue
[Operación y traspaso](OPERATIONS.md).
