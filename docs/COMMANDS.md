# Comandos de Discord

Los comandos se registran directamente en la raíz, sin depender del nombre o personalidad del bot.
Salvo `/ayuda` y el resultado de `/verguenza`, sus respuestas son efímeras: solo las ve quien ejecutó el comando. Los selectores y
botones quedan ligados a ese usuario, expiran a los 180 segundos y editan el mensaje existente en
vez de publicar respuestas adicionales.

Los comandos deterministas no ejecutan el runner, no crean traces de OpenAI y no consumen tokens.

## Disponibilidad

| Comando | Acceso | Visibilidad | Fuente |
|---|---|---|---|
| `/ayuda` | Todos | Pública | Persona seleccionada y configuración del bot |
| `/guia` | Todos | Efímera | `report_guide.json` |
| `/cargos` | Todos | Efímera | `delitos.json` |
| `/faq` | Todos | Efímera | Preguntas curadas exclusivamente desde `report_guide.json` |
| `/verguenza` | Todos | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/pendientes` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/ranking` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/mis-reportes` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/mi-resumen` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |

Los comandos generales no aplican `AUTHORIZED_USER_IDS`; están destinados a todos los miembros que
puedan ver e invocar al bot. Los comandos personales sí aplican las allowlists centrales antes de
leer el historial de Discord.

Los comandos de supervisión comparten `SUPERVISOR_ROLE_IDS`. Ausente, vacío o `*` permite a
todos; una lista CSV exige que el miembro posea al menos uno de esos roles. La comprobación está
centralizada y no depende de `AUTHORIZED_USER_IDS`.

Las consultas deterministas también reconocen `⏳`, `✅`, `❌` y `⚠️` cuando la reacción pertenece
al bot o a un miembro autorizado por esta misma política. Las reacciones de otros miembros no
alteran el estado del expediente.

## `/ayuda`

Publica la presentación de la identidad configurada, capacidades de revisión, canales de guardia,
forma de invocación, comandos disponibles y enlaces oficiales. Es el único comando deliberadamente
visible para todo el canal.

## `/guia`

Muestra una referencia compacta del manual mediante un selector. Cada selección edita el mismo embed
y conserva un enlace al documento oficial.

Secciones disponibles:

- Visión general.
- Redacción.
- Estructura narrativa.
- Contenido esencial.
- Evidencia y anexos.
- Descripción física.
- Revisión de cargos.

La interfaz no mantiene una copia independiente de las reglas: renderiza las secciones versionadas
de `report_guide.json` cargadas por el runtime.

## `/cargos`

Abre un modal con dos campos:

- Búsqueda obligatoria por código, título o términos relacionados.
- Categoría opcional, limitada a las categorías que contienen cargos individuales.

La búsqueda normaliza mayúsculas y tildes, inspecciona código, título, categoría y descripción, y
tolera errores tipográficos de forma conservadora. Primero exige que coincidan todos los términos;
si no obtiene resultados con una consulta compuesta, repite permitiendo coincidencia con cualquiera
de ellos.

Cada resultado muestra:

- Código y título.
- Categoría y tipo.
- Condena, libertad condicional, multa y puntos cuando existen en el catálogo.
- Descripción oficial disponible en el JSON local.
- Enlace al PCU oficial.

La consulta devuelve como máximo 50 coincidencias y presenta cinco por página. Si hay más de una
página aparecen los botones `Anterior` y `Siguiente`; ambos editan el mismo mensaje efímero.

## `/faq`

Presenta un selector de preguntas frecuentes. Su contenido se limita estrictamente a reglas que
existen en `report_guide.json`; no incorpora criterios surgidos de conversaciones, inferencias del
agente ni conocimiento adicional.

Preguntas disponibles:

- Persona y tiempo verbal de la redacción.
- Flexibilidad de la estructura narrativa.
- Contenido esencial del reporte.
- Obligatoriedad del número de llamada 911.
- Tratamiento de grabaciones BWC, DICV o Axon como entorno narrativo.
- Referencia de bolsas de evidencia.
- Corrección de contradicciones en la descripción física.
- Validación de cargos contra los hechos y el catálogo.

Cada respuesta indica la sección de la guía que la respalda y enlaza el manual oficial. Al modificar
la guía debe revisarse también este listado para evitar que una respuesta quede desactualizada.

## `/pendientes`

Abre un modal para filtrar por periodo y estación. Devuelve publicaciones sin estado terminal e
incluye tanto expedientes todavía sin reacción como aquellos marcados `⏳` durante una revisión
activa. Cada resultado muestra autor, estación, fecha y enlaces directos a la publicación y al hilo.
Presenta cinco expedientes por página.

## `/verguenza`

Abre un modal para seleccionar mes actual o los últimos 7, 30 o 90 días. Al enviarlo, publica una
sección por cada canal configurado con su propio top 10 de miembros que todavía no han alcanzado una
reacción ✅ dentro del periodo. Cuenta como abiertos los estados sin reacción, `⏳`, `❌` y `⚠️`;
los reportes aprobados quedan fuera antes de agrupar. Cada sección ordena por cantidad descendente y
muestra el desglose con una leyenda común: `📭` sin revisión, `⏳` en revisión, `❌` con
correcciones y `⚠️` inconcluso.

El resultado es visible para todo el canal, pero usa menciones sin notificación. Cualquier miembro
de una guild habilitada puede ejecutarlo y el snapshot solo incluye canales configurados que ese
miembro y el bot puedan leer. No aplica `AUTHORIZED_USER_IDS`, no ejecuta IA y no conserva historial.

## `/ranking`

Abre un modal con periodo, estación y criterio de orden. Puede ordenar por:

- Reportes presentados.
- Reportes aprobados.
- Reportes con correcciones.
- Porcentaje de aprobación.

Cada fila muestra los conteos actuales de aprobados, correcciones, expedientes sin revisión,
revisiones activas, inconclusos y tasa de aprobación. Presenta diez miembros por página. Es un snapshot de las reacciones actuales; no
representa cuántas veces cambió un resultado ni conserva publicaciones eliminadas.

## `/mis-reportes`

Abre un modal con filtros de periodo, estado y estación. Reconstruye un snapshot acotado desde los
canales configurados y devuelve enlaces directos a las publicaciones y sus hilos. Solo considera
publicaciones raíz del usuario con adjuntos o con una reacción de estado creada por esta cuenta del
bot. Presenta cinco resultados por página.

## `/mi-resumen`

Calcula el resumen privado del mes actual a partir del mismo snapshot de Discord: presentados,
aprobados, con correcciones, pendientes, casos que requieren supervisión y porcentaje de aprobación.
No representa un historial; publicaciones eliminadas o reacciones reemplazadas no pueden recuperarse
sin una base de datos.

## Errores y observabilidad

Los errores de consultas deterministas se registran con identificadores de guild, interacción y
usuario cuando están disponibles. Discord recibe un mensaje contextual sin stacktrace ni detalles
técnicos. La búsqueda de cargos registra la categoría, longitud de la consulta y cantidad de
resultados, pero no el texto introducido por el usuario.
