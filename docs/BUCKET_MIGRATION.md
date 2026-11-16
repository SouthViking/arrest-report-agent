# Migrar archivos entre proyectos Railway

El bot puede usar un bucket de otro proyecto o workspace antes de transferir el proyecto del bot.
La conexión depende de las credenciales S3, no de quién sea propietario del servicio.

Railway ofrece una API compatible con S3. Se puede usar un cliente como rclone con credenciales
independientes para ambos buckets. Para este repositorio también está disponible
[`scripts/railway/copy_railway_bucket.py`](../scripts/railway/copy_railway_bucket.py),
que usa boto3 y Railway CLI. Los argumentos y otras herramientas de operación se describen en
[Scripts de operación](../scripts/README.md).
No requiere descargar toda la colección ni introducir secretos en los comandos.

## Preparación y vista previa

1. Crea el bucket de destino en el proyecto del nuevo responsable. No conectes todavía el bot.
2. Instala las dependencias del repositorio y [Railway CLI](https://docs.railway.com/cli).
   Ejecuta `railway login` con un usuario que pueda consultar ambos proyectos y sus buckets.
3. Obtén los UUID de proyecto, entorno e instancia de bucket para ambos lados. El nombre visible
   y el nombre S3 son diferentes; la herramienta obtiene este último de Railway.
4. Desde la raíz del repositorio, prepara los argumentos. Ejemplo para PowerShell:

```powershell
$bucketArguments = @(
    '--source-project', 'UUID_PROYECTO_ORIGEN',
    '--source-environment', 'UUID_ENTORNO_ORIGEN',
    '--source-bucket', 'UUID_BUCKET_ORIGEN',
    '--destination-project', 'UUID_PROYECTO_DESTINO',
    '--destination-environment', 'UUID_ENTORNO_DESTINO',
    '--destination-bucket', 'UUID_BUCKET_DESTINO'
)
.\.venv\Scripts\python.exe scripts/railway/copy_railway_bucket.py @bucketArguments
```

El modo predeterminado, `plan`, solo lista objetos y muestra cantidades, bytes y claves faltantes.
No descarga su contenido ni escribe objetos. Las credenciales se consultan mediante una operación
de lectura de Railway CLI, se mantienen en memoria y no se imprimen ni guardan. El script no cambia
el proyecto enlazado al CLI, las variables del bot ni los despliegues.

## Copia y verificación

Cuando corresponda ejecutar la migración:

```powershell
.\.venv\Scripts\python.exe scripts/railway/copy_railway_bucket.py @bucketArguments --mode copy
.\.venv\Scripts\python.exe scripts/railway/copy_railway_bucket.py @bucketArguments --mode verify
```

- `copy` conserva exactamente las claves, contenido, metadata de usuario (incluido `sha256`),
  tipo de contenido, encabezados de presentación y etiquetas. Descarga cada objeto a un archivo
  temporal del sistema, lo carga y vuelve a leer el destino para comparar su SHA-256 y metadatos.
  Ejecuta hasta 8 transferencias en paralelo (ajustable con `--workers 1` a `--workers 16`).
  Solo necesita espacio temporal para los objetos en curso, no para toda la colección.
- Los objetos existentes se verifican antes de omitirlos. Una diferencia detiene la operación;
  no se sobrescriben objetos ni se elimina nada. Las cargas nuevas utilizan `If-None-Match: *`.
  Si el proveedor rechaza esa condición, la copia falla sin recurrir a una sobrescritura.
- Una ejecución interrumpida puede repetirse: los objetos ya copiados se vuelven a verificar.
  `verify` únicamente lee y exige las mismas claves, tamaños, bytes y metadatos en ambos lados.
  Los ETag y `LastModified` del destino pueden ser distintos a los del origen después de cargar.
- La herramienta detecta cambios en los inventarios durante la ejecución. Detén las escrituras
  para la pasada final, repite `copy` y `verify`, y exige salida 0 antes del corte.
  No es una instantánea transaccional ni crea un respaldo local permanente.
- La copia usa una carga simple por objeto y rechaza objetos mayores a 5 GiB. Es suficiente para
  los adjuntos del bot. No copia configuración de bucket, permisos ni versiones de objetos.

No hace falta modificar las referencias de archivos en PostgreSQL si las claves y contenido se
conservan. La BD sigue necesitando su propio respaldo.

## Conectar el bot, después de verificar

Actualiza conjuntamente estas variables del servicio con los valores del bucket de destino:

```text
PERSONA_ASSET_BUCKET
PERSONA_ASSET_ENDPOINT
PERSONA_ASSET_REGION
PERSONA_ASSET_ACCESS_KEY_ID
PERSONA_ASSET_SECRET_ACCESS_KEY
```

Estas variables tienen prioridad sobre los alias genéricos `BUCKET`, `ENDPOINT`, `REGION`,
`ACCESS_KEY_ID` y `SECRET_ACCESS_KEY`. Para buckets en otro proyecto, configura los valores del
destino en el servicio; las referencias de variables entre servicios son propias de cada proyecto.
No incluyas claves en el repositorio ni en el historial de terminal.

Redespliega una sola instancia y comprueba la lectura de avatares y adjuntos anteriores y la carga
de un adjunto nuevo. La herramienta no ejecuta este corte ni elimina el bucket original.

Referencias: [buckets S3 de Railway](https://docs.railway.com/storage-buckets),
[transferencia de proyectos](https://docs.railway.com/projects#transferring-projects).
