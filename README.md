# Arrest Report Agent

Bot de Discord para revisar reportes de arresto de roleplay en español y gestionar tareas de una
facción. Usa PostgreSQL para la configuración y los registros, almacenamiento S3 para avatares y
adjuntos, OpenAI para las revisiones y la API SARP para las consultas habilitadas.

## Por dónde empezar

| Necesitas | Sigue esta guía |
|---|---|
| Instalar una instancia nueva en Railway, Docker o Python | [Instalación](docs/SETUP.md) |
| Saber qué variables configurar y de dónde salen | [Variables de entorno](docs/ENVIRONMENT.md) |
| Darte acceso y configurar roles, canales y módulos | [Configuración inicial](docs/CONFIGURATION.md) |
| Actualizar, respaldar, restaurar o transferir la administración | [Operación y traspaso](docs/OPERATIONS.md) |
| Usar herramientas de migración, respaldo y diagnóstico | [Scripts de operación](scripts/README.md) |
| Usar los comandos y conocer sus permisos | [Comandos de Discord](docs/COMMANDS.md) |
| Entender el comportamiento y las integraciones | [Referencia funcional](docs/FEATURES.md) |

Para una instalación nueva: prepara Discord y los servicios, completa las variables, ejecuta
`db migrate`, revisa `db import-env --dry-run`, aplica `db import-env --apply`, comprueba `db verify`
y arranca **una sola instancia**. Después termina la configuración mediante `/config`.
La [guía de instalación](docs/SETUP.md) desarrolla ese orden con comandos para cada entorno.

## Requisitos

- Python 3.12–3.14, o Docker con Compose.
- Aplicación de Discord instalada con los scopes `bot` y `applications.commands`.
- **Message Content Intent** y **Server Members Intent** habilitados.
- Credencial de OpenAI con acceso al modelo configurado.
- PostgreSQL 14 o superior y un Bucket compatible con S3.
- Token de SARP si se habilitan consultas como `/revisar-arresto` o `/mdc`.

Los permisos por canal y la jerarquía de roles están en [Permisos de Discord](docs/PERMISSIONS.md).
El [Compose incluido](docker-compose.yml) ejecuta solo el bot: PostgreSQL y S3 se preparan aparte.

## Antes de copiar la configuración

[default.env](default.env) es una plantilla, no una configuración lista para producción.
Sus IDs son ficticios y deben reemplazarse. Los secretos quedan vacíos y se cargan en `.env`
o en el gestor de secretos del hosting; no deben subirse al repositorio.

La configuración tiene dos niveles:

- **Proceso:** credenciales, conexiones, logging y switches globales. Cambiarlos requiere reiniciar.
- **Servidor de Discord:** roles, canales, persona, modelo y módulos guardados en PostgreSQL.
  Se editan como borrador en `/config` y solo entran en vigor al publicarlo.

Los valores de importación inicial no sustituyen la configuración ya publicada. No repitas
`db import-env --replace` como parte de una actualización normal.

Los valores institucionales de importación están en un perfil JSON reemplazable mediante
`BOOTSTRAP_PROFILE_PATH`. `/config` permite configurar o deshabilitar aportes, usuarios protegidos
de `/dedear`, enlaces de conocimiento, TeamSpeak y destinos internos del changelog. La whitelist
`CONFIG_OWNER_USER_IDS` define los usuarios administradores sin una cuenta de creador implícita.

Autoría original: **SouthViking**. La cuenta que aloja el servicio y el repositorio operativo pueden
cambiar conservando la atribución y el historial de contribuciones.

## Desarrollo y comprobaciones

Instala las dependencias de desarrollo siguiendo [Instalación](docs/SETUP.md). Después:

```bash
python -m ruff check .
python -m mypy src
python -m pytest -q
```

La suite normal no consume OpenAI. Los tests de integración requieren una base PostgreSQL
**aislada** mediante `TEST_DATABASE_URL`: algunos borran y recrean el esquema.
Las evaluaciones reales de IA son opt-in y sí consumen créditos; consulta
[Pruebas y actualizaciones](docs/OPERATIONS.md).

## Referencias técnicas

- [Arquitectura](docs/ARCHITECTURE.md).
- [Privacidad, logs y errores](docs/PRIVACY_AND_ERRORS.md).
- [Variables validadas por el código](src/arrest_report_agent/config/settings.py).
- [Despliegue en Railway](railway.json) y [Dockerfile](Dockerfile).
- [Versión y changelog](src/arrest_report_agent/release.json).
