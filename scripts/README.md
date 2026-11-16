# Scripts de operación y desarrollo

Estas herramientas se ejecutan manualmente desde la raíz del repositorio. No se importan ni
ejecutan desde el runtime del bot. La lógica específica de Railway está en `scripts/railway/`.

## Catálogo

| Script | Función | Dependencias y efectos |
|---|---|---|
| [railway/copy_railway_bucket.py](railway/copy_railway_bucket.py) | Vista previa, copia entre buckets y verificación. | Python 3.12–3.14, boto3 y Railway CLI autenticado. Solo `copy` escribe en el bucket de destino. |
| [railway/download_bucket.py](railway/download_bucket.py) | Respaldo local con metadatos y comprobación SHA-256. | Python 3.12–3.14 y boto3. Railway CLI solo para consultar/descargar; `verify` funciona sin conexión. |
| [probe_sarp_api.py](probe_sarp_api.py) | Comprueba el token con una lectura de multas en la API SARP. | `requests` y `SARP_API_TOKEN` en el entorno. No carga `.env` por sí solo. |
| [ci_containers.py](ci_containers.py) | Prepara PostgreSQL aislado o construye la imagen de CI con fallback de registros. | Python y Docker en marcha. Crea contenedores o imágenes locales. |
| [seed_manual_quiz_questions.sql](seed_manual_quiz_questions.sql) | Carga o actualiza las preguntas iniciales del manual. | `psql`, BD migrada y `guild_id`. Escribe en la BD; consulta el alcance más abajo. |

`railway/bucket_storage.py` es una utilidad interna compartida por los dos scripts S3, no un comando.
El copiador y el descargador reciben los recursos por argumentos: no contienen cuentas, proyectos
ni buckets de una instalación concreta y no necesitan importar el paquete del bot.

## Preparar los scripts Railway

1. Usa un entorno Python 3.12–3.14. Si ya instalaste el repositorio, boto3 está incluido.
   Para ejecutar únicamente estas herramientas basta con:

   ```bash
   python -m pip install "boto3>=1.36,<2"
   ```

2. Instala [Railway CLI](https://docs.railway.com/cli) con el comando `api` disponible
   (validado con 5.54.0) y ejecuta `railway login`.
3. Tu cuenta necesita acceso a los proyectos, entornos y buckets seleccionados. Usa los UUID de
   Railway, no el nombre visible ni el nombre S3. La herramienta obtiene este último del proveedor.

Las credenciales se consultan por stdin a Railway CLI y se conservan solo en memoria. No se
introducen como argumentos, no se escriben en los manifiestos y no se imprimen errores crudos
del SDK. Los scripts no cambian el proyecto enlazado al CLI, variables del bot ni despliegues.

En los ejemplos, reemplaza los valores `UUID_...` por UUID reales. `python` debe ser el del entorno
preparado; en PowerShell puedes usar `.\.venv\Scripts\python.exe`.

## Copiar entre buckets

```powershell
$copyArguments = @(
    '--source-project', 'UUID_PROYECTO_ORIGEN',
    '--source-environment', 'UUID_ENTORNO_ORIGEN',
    '--source-bucket', 'UUID_BUCKET_ORIGEN',
    '--destination-project', 'UUID_PROYECTO_DESTINO',
    '--destination-environment', 'UUID_ENTORNO_DESTINO',
    '--destination-bucket', 'UUID_BUCKET_DESTINO'
)
python scripts/railway/copy_railway_bucket.py @copyArguments
python scripts/railway/copy_railway_bucket.py @copyArguments --mode copy --workers 8
python scripts/railway/copy_railway_bucket.py @copyArguments --mode verify
```

| Argumento | Requisito / predeterminado |
|---|---|
| `--source-project`, `--source-environment`, `--source-bucket` | UUID obligatorios del origen. |
| `--destination-project`, `--destination-environment`, `--destination-bucket` | UUID obligatorios del destino. |
| `--mode` | `plan` por defecto; también `copy` o `verify`. |
| `--workers` | Entre 1 y 16; predeterminado 8. |

`plan` solo lista objetos y cantidades; no descarga contenido ni escribe en buckets. `copy`
descarga a temporales y carga los objetos faltantes, conservando las claves, bytes, metadatos y
etiquetas. Vuelve a leer cada copia para comprobar su SHA-256. Los temporales se eliminan: esta
operación **no crea un respaldo local permanente**.

Los objetos ya presentes se verifican y omiten. Una diferencia o una clave ajena al origen
detiene la operación. No se borra ni sobrescribe nada; las cargas usan `If-None-Match: *`.
Se puede repetir una copia interrumpida para verificar lo existente y continuar con lo faltante.
La carga simple admite objetos de hasta 5 GiB. `verify` compara contenido y metadatos de ambos
buckets sin escribir. Una copia no conserva los ETag o `LastModified` originales en el destino.

Para realizar el corte del bot después de copiar, sigue la
[guía de migración](../docs/BUCKET_MIGRATION.md).

## Descargar un respaldo local

```powershell
$downloadArguments = @(
    '--project', 'UUID_PROYECTO',
    '--environment', 'UUID_ENTORNO',
    '--bucket', 'UUID_BUCKET',
    '--destination', '../arrest-report-backups/bucket-AAAA-MM-DD'
)
python scripts/railway/download_bucket.py @downloadArguments --mode plan
python scripts/railway/download_bucket.py @downloadArguments --workers 8
python scripts/railway/download_bucket.py @downloadArguments --prefix 'suspects/'
```

La última línea es un **ejemplo alternativo** de respaldo parcial: usa otra carpeta nueva para
esa ejecución. `--prefix` se envía al listado S3 y conserva las claves originales completas.

| Argumento | Requisito / predeterminado |
|---|---|
| `--project`, `--environment`, `--bucket` | UUID obligatorios para `plan` y `download`. |
| `--destination` | Carpeta local obligatoria, fuera del repositorio; nueva o vacía para descargar. |
| `--mode` | `download` por defecto; también `plan` o `verify`. |
| `--prefix` | Prefijo S3 literal opcional; vacío incluye todo el bucket. No es una expresión regular. |
| `--workers` | Entre 1 y 16 descargas simultáneas; predeterminado 8. |

`plan` muestra cantidades y bytes sin descargar contenido ni crear la carpeta. `download` solo
lee del bucket y escribe una copia local permanente. Comprueba el tamaño y SHA-256 del flujo,
valida el hash declarado por el objeto cuando existe y vuelve a leer el archivo desde disco.

El resultado contiene:

```text
bucket-AAAA-MM-DD/
  manifest.json
  objects/
    <primeros dos caracteres del hash>/
      <hash de la clave S3>.png
```

Los nombres locales usan SHA-256 de la **clave S3** y una extensión segura. Esto evita conflictos
de nombres, rutas fuera del respaldo, caracteres inválidos y nombres reservados de Windows.
El hash de contenido se guarda por separado. El manifiesto conserva cada clave S3 original,
ruta local, tamaño, SHA-256 de contenido, ETag y fecha originales, propiedades S3 y etiquetas.
Las fechas se serializan como ISO 8601. Estos datos permiten reconstruir las claves originales
en una restauración; copiar los nombres locales al bucket sin consultar el manifiesto las cambiaría.
La herramienta no incluye un comando de restauración.

Cada ejecución requiere un destino nuevo o vacío. No sobrescribe respaldos anteriores.
Si falla o se interrumpe, conserva una copia parcial marcada `failed` o `in_progress`; no se
considera un respaldo válido. Para repetir la descarga, elige otra carpeta nueva.

### Verificar sin conexión

```powershell
python scripts/railway/download_bucket.py --mode verify --destination '../arrest-report-backups/bucket-AAAA-MM-DD'
```

Esta modalidad no recibe proyecto, entorno, bucket ni prefijo y no consulta Railway. Exige un
manifiesto completo y compatible, valida las rutas y comprueba cada archivo, tamaño, SHA-256 y
totales. Detecta datos faltantes o alterados respecto al manifiesto; no es una firma criptográfica
del respaldo ni puede detectar una modificación coordinada de archivos y manifiesto.

### Coherencia y resultado

Los dos scripts Railway comparan inventarios inicial y final. Si detectan cambios durante la
operación, terminan con error. Para una captura coherente, detén las escrituras mientras se hace
el respaldo final. No equivale a una instantánea transaccional con PostgreSQL.

El respaldo contiene objetos y sus metadatos, no credenciales, configuración del bucket, permisos
ni la BD. Respalda PostgreSQL por separado y conserva los respaldos fuera del repo y del contexto
de build. Los archivos y metadatos del respaldo pueden contener datos privados del servidor.

Ambos comandos devuelven `0` al completar la operación, `1` ante un fallo y `2` ante argumentos
inválidos. No cambian la conexión del bot, eliminan objetos S3 ni notifican a Discord.

## Otras herramientas

### Acceso a SARP

Configura `SARP_API_TOKEN` mediante el entorno y, opcionalmente, `SARP_API_BASE_URL`. Ejecuta:

```bash
python scripts/probe_sarp_api.py
```

Realiza una lectura de multas con límite de 25, informa el estado HTTP y distingue un challenge
de Cloudflare de una respuesta JSON. No requiere levantar el bot.

### Contenedores de CI

```bash
python scripts/ci_containers.py postgres
python scripts/ci_containers.py build --tag arrest-report-agent:local
```

`postgres` crea `arrest-report-agent-ci-postgres`, con BD `arrest_report_agent_test`, puerto
`127.0.0.1:5432` y autenticación `trust`. Se usa únicamente para pruebas aisladas; no sustituye
la BD de producción. La limpieza la realiza el workflow de CI. `build` construye el Dockerfile
del repo. Ambos intentan registros alternativos para descargar imágenes oficiales.

### Preguntas iniciales del manual

```bash
psql --host=DB_HOST --username=DB_USER --dbname=DB_NAME --password --set=guild_id=GUILD_ID --file=scripts/seed_manual_quiz_questions.sql
```

Usa un `guild_id` ya existente y revisa el archivo antes de aplicarlo. Es idempotente por
`(guild_id, slug)`: crea preguntas ausentes, actualiza las declaradas cuando cambian y reemplaza
sus alternativas. No modifica otras guilds ni slugs ajenos al archivo. Esto **sí escribe en la BD**.

## Comprobaciones

```bash
python -m pytest tests/test_bucket_copy.py tests/test_bucket_download.py -q
python -m ruff check scripts/railway tests/test_bucket_copy.py tests/test_bucket_download.py
python scripts/railway/copy_railway_bucket.py --help
python scripts/railway/download_bucket.py --help
```

Las pruebas usan almacenamiento simulado y carpetas temporales, sin credenciales reales ni red.
