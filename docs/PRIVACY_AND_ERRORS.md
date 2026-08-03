# Privacidad, traces y manejo de errores

## Datos procesados

Una revisión envía a OpenAI el texto acotado del reporte/hilo y las imágenes seleccionadas. Los datos
se usan únicamente para producir el veredicto solicitado mediante mención explícita.

Los mensajes sin mención no generan llamadas, almacenamiento ni logs de contenido.

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

Cada evento añade campos `clave=valor`. Se registran IDs técnicos, entorno, modelo, intento, latencia,
tokens, costo aproximado, veredicto o clase de error. Los errores incluyen tipo, mensaje técnico,
estado/código del proveedor cuando existe y stacktrace completo.

Los eventos del runner repiten `guild_id`, `parent_channel_id`, `channel_id`, `thread_id`, `user_id`,
`message_id`, `root_message_id`, `trigger_message_id` e `attempt` para poder filtrarlos sin depender
de otra línea del log.

No se registran:

- nombres o narrativa del reporte;
- texto extraído de imágenes;
- URLs de Discord CDN;
- payloads de herramientas;
- prompts o respuestas completas;
- secretos.

## Metadata de traces

El workflow se llama `arrest_report_review` y agrupa intentos mediante
`discord-report:{guild_id}:{root_message_id}`. Cada run incluye:

- environment, app_version, prompt_version y modelo;
- guild_id, channel_id, parent_channel_id y thread_id;
- message_id, root_message_id, trigger_message_id y user_id;
- review_attempt e interaction_mode;
- guide_version y hash SHA-256 del catálogo;
- estado del contexto.

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
datos del proveedor quedan en los logs. Si existe `MANUAL_REVIEW_ROLE_ID`, menciona el rol una sola
vez por ejecución.
