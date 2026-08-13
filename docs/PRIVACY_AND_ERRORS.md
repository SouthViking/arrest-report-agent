# Privacidad, traces y manejo de errores

## Datos procesados

Una mención que supera la política local envía primero al guardrail únicamente el texto del mensaje
actual y señales compactas: si está en un hilo, si existe contexto previo, cuántos adjuntos tiene el
mensaje y si el contexto contiene material revisable. No envía imágenes, URLs, historial, nombre ni
roles en esta etapa. El resultado clasifica alcance e intención para decidir si corresponde marcar
determinísticamente una revisión antes de invocar al agente principal.

Si la solicitud está dentro de alcance o es ambigua, el agente principal recibe el texto acotado del
reporte/hilo, las imágenes y documentos seleccionados, los nombres visibles de sus participantes y
hasta ocho nombres de roles relevantes por persona. También recibe un registro acotado con los IDs
de hasta diez participantes visibles para poder consultar su metadata mediante `get_guild_members`.
No envía IDs de roles en el input inicial. Las tools `list_guild_roles`, `find_guild_members` y
`find_channel_members` pueden descubrirlos bajo demanda, pero quedan fijadas a la guild de la
conversación, aplican filtros y proyección antes de responder y retornan un máximo de 25 elementos por
página. Los canales elegidos deben pertenecer a esa guild y ser visibles para el solicitante.
`get_guild_members` amplía hasta diez IDs concretos. Los datos se usan para responder dentro del
ámbito o emitir el veredicto solicitado mediante mención explícita.

`get_messages` puede leer una ventana máxima de veinte mensajes en otro canal de la misma guild solo
si el solicitante también posee `view_channel` y `read_message_history`. El agente puede seleccionar
canal, ancla, dirección, filtros y proyección, pero no la guild. Los adjuntos recuperados exponen
metadata sin URL ni contenido, y los hilos privados ajenos al contexto actual se rechazan.

`get_message_attachments` puede cargar el contenido de hasta diez adjuntos seleccionados desde un
único mensaje ya sujeto a esa misma validación. No recibe URLs ni una guild elegida por el modelo;
resuelve URLs vigentes internamente y las entrega como imágenes o archivos nativos al Runner. Omite
contenido incompatible, repetido o por encima de los límites acumulados de cantidad y tamaño. Estos
outputs pueden aparecer en traces únicamente si se habilita explícitamente la inclusión de datos
sensibles; permanece desactivada por defecto.

Los IDs de `REVIEW_ESCALATION_ROLE_IDS` tampoco se entregan al modelo. Discord los resuelve y limita
de forma determinista únicamente cuando corresponde publicar una derivación.

Los mensajes sin mención no generan llamadas, almacenamiento ni logs de contenido. Guilds y acceso
por usuario o rol pasan por una política local antes de reconstruir el contexto. Una mención que no
coincide con `agent_users` ni con `agent_roles` registra solo IDs técnicos mediante los eventos
`unauthorized_user_redirect_*`.

## Logs

Todos los logs, incluidos los de dependencias, se escriben como JSON. Por defecto son legibles en
varias líneas:

```json
{
  "timestamp": "2026-08-03T00:46:26.518Z",
  "level": "ERROR",
  "logger": "arrest_report_agent.application.service",
  "event": "review_failed"
}
```

`LOG_JSON_INDENTED=false` genera el mismo objeto en una sola línea para facilitar su ingestión en
plataformas de observabilidad.

Cada evento añade campos `clave=valor`. Se registran IDs técnicos, entorno, modelo, effort, intento,
persona seleccionada, latencia, tokens crudos, costo aproximado, versión de precios, veredicto o
clase de error. Los errores incluyen tipo, mensaje técnico, estado/código del proveedor cuando existe
y stacktrace completo.

Una ejecución completada conserva `input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`, `reasoning_tokens` y `total_tokens`. Además registra los
costos separados de entrada normal, entrada cacheada, escritura de caché y salida, junto con moneda,
service tier, tramo de contexto y versión del catálogo usada para el cálculo.

Los eventos del runner repiten `guild_id`, `parent_channel_id`, `channel_id`, `thread_id`, `user_id`,
`message_id`, `root_message_id`, `trigger_message_id` e `attempt` para poder filtrarlos sin depender
de otra línea del log.

No se registran:

- nombres o narrativa del reporte;
- nombres visibles o títulos de roles;
- texto extraído de imágenes;
- URLs de Discord CDN;
- payloads de herramientas;
- prompts o respuestas completas;
- secretos.

## Metadata de traces

El workflow se llama `arrest_report_review` y agrupa intentos mediante
`discord-report:{guild_id}:{root_message_id}`. Cada run incluye:

- environment, app_version, prompt_version y scope_guardrail_version;
- guild_id, parent_channel_id y thread_id;
- root_message_id, trigger_message_id y user_id;
- review_attempt, modelo, reasoning_effort y persona_id.

`OPENAI_TRACE_INCLUDE_SENSITIVE_DATA=false` es el valor predeterminado y recomendado.

## Taxonomía de errores

- `quota_exceeded`: cuota agotada; requiere intervención administrativa.
- `rate_limited`: exceso temporal de solicitudes; puede reintentarse.
- `timeout`: se superó el límite total.
- `provider_internal`: conexión o falla 5xx de OpenAI.
- `auth_configuration`: API key o permisos del proveedor incorrectos.
- `agent_execution`: máximo de turnos, tool inválida o comportamiento inesperado.
- `discord_failure`: Discord rechazó una reacción, hilo, embed o edición.
- `context_unavailable`: no fue posible leer el reporte raíz o el historial.
- `unknown`: fallback seguro.

Ante un error, el bot intenta quitar `⏳`, añade `⚠️`, reutiliza o crea un hilo y publica únicamente
un mensaje contextualizado y el siguiente paso. La clasificación, referencia técnica, excepción y
datos del proveedor quedan en los logs. Si `REVIEW_ESCALATION_ROLE_IDS` contiene roles válidos de la
guild, los menciona una sola vez por ejecución mediante una allowlist exacta de Discord. Los IDs
eliminados se omiten con un warning y `MANUAL_REVIEW_ROLE_ID` permanece como alias legado.

La redacción pública de errores, restricciones y redirecciones se obtiene del mapa validado de la
persona activa. La clasificación y las decisiones de acceso siguen siendo deterministas y no se
delegan al modelo; cambiar la voz no altera permisos, reintentos ni datos expuestos.
