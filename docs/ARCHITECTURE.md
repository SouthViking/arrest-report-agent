# Arquitectura del MVP

## Flujo

```mermaid
flowchart LR
    M["Mensaje con mención"] --> T["Política de trigger"]
    T -->|"usuario y canal habilitados"| C["Contexto acotado de Discord"]
    T -->|"usuario no habilitado"| U["Respuesta local de acceso"]
    T -->|"canal no habilitado"| W["Respuesta local con canales"]
    C --> R["Runner de OpenAI"]
    J["Catálogo JSON de modelos"] --> R
    R --> G["Guía local"]
    R --> D["Índice de delitos"]
    R --> P["Tools de publicación"]
    P --> X["Reacción, hilo y embed"]
```

La política de trigger se ejecuta antes de descargar contexto o llamar a OpenAI. Tras validar
servidor, autor humano y mención explícita, comprueba primero `AUTHORIZED_USER_IDS` y luego el canal.
Ambos rechazos usan respuestas locales con cooldown y nunca alcanzan el Runner. El mensaje raíz es
la unidad de exclusión mutua: dos menciones simultáneas para el mismo reporte no generan dos runs.

## Capas

- `domain`: modelos Pydantic y enums que definen el contrato de revisión.
- `config`: entorno y catálogo JSON validado de capacidades, defaults y precios por modelo.
- `knowledge`: guía versionada e índice del JSON del PCU, cargados y validados al iniciar.
- `application`: prompt, tools, contexto del runner, errores, tracing y orquestación.
- `infrastructure/discord`: trigger, reconstrucción de hilos, embeds, reacciones y comandos.
- `bootstrap`: composición de dependencias y healthcheck.

## Herramientas del agente

- `begin_review(case_subject)`: añade `⏳`; no abre hilo.
- `get_report_guide(sections)`: devuelve solo las secciones solicitadas.
- `list_charge_index(category_ids, query, limit)`: sin filtros lista categorías; con filtros retorna
  IDs, códigos y títulos compactos.
- `get_charge_details(charge_ids)`: devuelve hasta diez delitos por ID único.
- `publish_review(publication)`: tool terminal que ejecuta inmediatamente el veredicto en Discord.

`parallel_tool_calls` permanece deshabilitado para mantener un orden determinista en efectos
externos. La publicación terminal impide que el runner continúe generando después de actuar.

## Resolución del modelo y costos

El entorno selecciona únicamente `OPENAI_MODEL` y `OPENAI_REASONING_EFFORT`. Al arrancar, el runtime
carga `config/data/models.json`, lo valida con Pydantic y rechaza modelos o efforts no soportados. El
catálogo aporta detalle de imagen, verbosity, límite de salida, service tier y tarifas.

El costo se calcula desde `Usage` del Agents SDK. Se conservan los contadores crudos y cada llamada
se tarifa de forma independiente para seleccionar correctamente el tramo de contexto. Las escrituras
de caché tienen su propia tarifa; `reasoning_tokens` es un subconjunto de `output_tokens` y no se suma
por segunda vez.

## Contexto de hilo

No se usan sesiones ni persistencia del SDK. Cada mención reconstruye un input autocontenido con:

- reporte raíz;
- mensajes recientes del hilo en orden cronológico;
- referencias de respuesta y extractos acotados;
- indicador de mensaje editado;
- solicitud actual;
- imágenes actuales, imágenes del reporte raíz y hasta dos imágenes históricas recientes.

Los límites de mensajes, caracteres e imágenes se aplican antes del runner. El input indica si el
contexto quedó `complete`, `truncated` o `unavailable` para evitar conclusiones excesivas.

## Extensión futura

`ReviewPublisher` aísla el runner de Discord y las fuentes locales no dependen del bot. Una futura
persistencia puede almacenar configuración, intentos, veredictos y threads sin cambiar el contrato
de `ReviewPublication`. Una API de PCU/OpenMP puede reemplazar `ChargeCatalog` mediante el mismo
interfaz de consulta; una tabla por guild/facción puede sustituir las variables de entorno.
