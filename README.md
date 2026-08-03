# Arrest Report Agent

Bot agente de Discord para revisar reportes de arresto de roleplay en español. La identidad visible
por defecto es **Sgt. Vigil**, pero el repositorio, el paquete y la lógica mantienen un nombre neutral:
la identidad se inyecta desde `BOT_DISPLAY_NAME` y `BOT_ROLE_TITLE`.

El MVP está preparado para los canales privados `gnt-arrestos` y `dlm-arrestos` del servidor LSSD.
Solo actúa cuando un miembro menciona explícitamente al bot, en cualquier parte del mensaje. Las
publicaciones sin mención, los bots, los webhooks, otros servidores y otros canales se ignoran sin
llamar a OpenAI.

## Qué hace

- Revisa la narrativa, evidencia visible y cargos de un reporte multimodal.
- Envía todas las imágenes seleccionadas, incluidas las fotografías frontal y lateral.
- Contrasta descripciones expresas de ropa y accesorios con flexibilidad por iluminación, ángulo,
  compresión y limitaciones gráficas; no infiere atributos sensibles.
- Consulta una guía local por secciones y un índice validado de 164 delitos descargados del PCU.
- Mantiene el indicador nativo de escritura mientras procesa una solicitud.
- Añade `⏳` al comenzar desde una tool del agente y publica el resultado desde una tool terminal.
- Aprueba con `✅` sin crear un hilo nuevo; las correcciones usan `❌` y un hilo estructurado; los
  casos inconclusos o técnicos usan `⚠️` y derivación manual opcional.
- Reconstruye hasta 20 mensajes de un hilo, incluyendo respuestas, ediciones y correcciones recientes.
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
DISCORD_GUILD_ID=1234674521416663070
REVIEW_CHANNEL_IDS=1462992794569281617,1462992859761217745
OPENAI_MODEL=gpt-5.6-terra
OPENAI_REASONING_EFFORT=medium
OPENAI_IMAGE_DETAIL=original
```

La identidad puede cambiarse sin tocar código ni prompts:

```dotenv
BOT_DISPLAY_NAME=Sgt. Vigil
BOT_ROLE_TITLE=Watch Sergeant
```

`MANUAL_REVIEW_ROLE_ID` es opcional. Si se configura, el rol recibe una sola mención en una
derivación inconclusa o técnica.

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

1. Publica el nombre o identificador del sospechoso junto con las capturas habituales.
2. Menciona al bot en cualquier parte del mensaje.
3. Para una corrección, responde dentro del hilo, adjunta el material actualizado y vuelve a
   mencionar al bot.

El agente no exige cuatro imágenes ni un orden fijo. Puede reconocer fotografías de personaje,
capturas de cargos, narrativa en el juego, un formulario o texto escrito directamente en Discord.

Comandos disponibles:

- `/wv help`: presentación, instrucciones y enlaces oficiales visibles para todo el canal.

## Configuración relevante

| Variable | Predeterminado | Función |
|---|---:|---|
| `LOG_JSON_INDENTED` | `true` | Usa JSON indentado; `false` genera un objeto compacto por línea. |
| `OPENAI_MODEL` | `gpt-5.6-terra` | Modelo del runner. |
| `OPENAI_REASONING_EFFORT` | `medium` | Esfuerzo de razonamiento; puede optimizarse después. |
| `OPENAI_IMAGE_DETAIL` | `original` | Conserva texto pequeño y detalle visual. |
| `OPENAI_MAX_OUTPUT_TOKENS` | `2500` | Límite por respuesta del modelo. |
| `OPENAI_MAX_RETRIES` | `2` | Reintentos del proveedor. |
| `REVIEW_TIMEOUT_SECONDS` | `180` | Tiempo total máximo de una revisión. |
| `MAX_CONCURRENT_REVIEWS` | `2` | Límite global en memoria. |
| `MAX_IMAGES_PER_REVIEW` | `10` | Máximo multimodal por ejecución. |
| `THREAD_CONTEXT_MAX_MESSAGES` | `20` | Historial reciente reconstruido. |
| `OPENAI_TRACING_ENABLED` | `true` | Habilita traces del Agents SDK. |
| `OPENAI_TRACE_INCLUDE_SENSITIVE_DATA` | `false` | Excluye mensajes, imágenes y tool payloads del trace. |

Las tarifas por millón de tokens usadas únicamente para estimar costo en logs también son variables
de entorno. Deben actualizarse al cambiar de modelo; no afectan la facturación real de OpenAI.

## Calidad y pruebas

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy
```

Las pruebas no llaman a Discord ni a OpenAI. Cubren catálogo, configuración, trigger, contexto
multimodal, efectos inmediatos de las tools, embeds, clasificación de errores, metadata y costos.

## Límites del MVP

- No hay base de datos: intentos, locks y estado operativo se pierden al reiniciar.
- Los canales, servidor y rol manual se configuran por entorno, no mediante comandos persistentes.
- El catálogo y la guía son archivos versionados; todavía no existe sincronización con OpenMP/PCU.
- No existe configuración por facción o tenant.
- No se ejecuta una segunda revisión automática con otro modelo.

La separación entre contexto Discord, dominio, conocimiento y runner permite añadir más adelante una
base de datos, configuración por servidor/facción, catálogo sincronizado mediante API y políticas
distintas para LSSD, LSPD u otras organizaciones. Consulta [Arquitectura](docs/ARCHITECTURE.md).

## Privacidad y observabilidad

Los logs contienen identificadores técnicos, latencia, modelo, uso de tokens, costo aproximado y
veredicto. No registran nombres, narrativa, OCR, URLs de adjuntos ni respuestas completas. Los traces
usan IDs de servidor, canal, hilo, mensajes y usuario para depuración, con payload sensible desactivado.
Consulta [Privacidad y errores](docs/PRIVACY_AND_ERRORS.md).
