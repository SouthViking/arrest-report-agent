# Comandos de Discord

## Administración dinámica: `$rac`

Es un comando inline de mensaje plano y no aparece en el selector slash. Pueden usarlo los IDs definidos en
`CONFIG_OWNER_USER_IDS` y los miembros con un rol de la política `config_admin_roles`; cambiar el
perfil global exige ser owner. Sus respuestas son embeds públicos. Un intento no autorizado responde
con la personalidad activa, aplica un cooldown por usuario y no alcanza el runner.

- `$rac ver`: revisión activa, borrador, persona, modelo y resumen de políticas.
- `$rac borrador crear|descartar`: inicia o elimina un borrador sin alterar producción.
- `$rac acceso modo|agregar|quitar …`: edita políticas relacionales del borrador.
- `$rac agente clave=valor …`: cambia modelo, reasoning y límites principales.
- `$rac modulo <clave> <on|off> ['{…}']`: habilita módulos y valida sus opciones JSON.
- `$rac persona importar|publicar|asignar|revertir …`: administra revisiones y avatares.
- `$rac publicar`: valida el agregado y lo activa en una transacción.
- `$rac rollback <revisión>`: clona una revisión histórica como una nueva.
- `$rac perfil-global <persona> [revisión]`: programa username, avatar, bio y presencia globales.
- `$rac ayuda`: muestra la sintaxis compacta en Discord.

`$rac` debe ser el primer token del mensaje y se procesa antes de menciones o allowlists del agente.
Cada mutación usa el ID del mensaje como clave idempotente y queda en `config_audit_events`. Las
publicaciones emiten una notificación de PostgreSQL para refrescar el runtime sin redeploy.

Los comandos se registran directamente en la raíz, sin depender del nombre o personalidad del bot.
Salvo `/ayuda`, `/ts3`, `/info-roles`, los resultados de `/verguenza` y `/honor`, y el expediente publicado
por `/solicitar-roles`, sus respuestas son efímeras: solo las ve quien ejecutó el comando. Los selectores
de consultas quedan ligados a ese usuario, expiran a los 180 segundos y editan el mensaje existente
en vez de publicar respuestas adicionales. Los tres botones de una solicitud de roles son la
excepción: son persistentes, sobreviven reinicios y autorizan al supervisor en cada clic.

Los comandos deterministas no ejecutan el runner, no crean traces de OpenAI y no consumen tokens.

## Disponibilidad

| Comando | Acceso | Visibilidad | Fuente |
|---|---|---|---|
| `/ayuda` | Todos | Pública | Persona seleccionada y configuración del bot |
| `/guia` | Todos | Efímera | `report_guide.json` |
| `/cargos` | Todos | Efímera | `delitos.json` |
| `/faq` | Todos | Efímera | Preguntas curadas exclusivamente desde `report_guide.json` |
| `/verguenza` | Todos | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/honor` | Todos | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/ts3` | Todos | Pública | Dirección fija y texto de la persona activa |
| `/pendientes` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/ranking` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/mis-reportes` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/mi-resumen` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/solicitar-roles` | Todos en guild y canal habilitados | Modal privado; solicitud pública en el canal | Mensaje versionado de Discord |
| `/info-roles` | Todos en una guild habilitada | Pública | Persona y configuración actuales |

Los comandos generales no aplican `AUTHORIZED_USER_IDS`; están destinados a todos los miembros que
puedan ver e invocar al bot. Los comandos personales sí aplican las allowlists centrales antes de
leer el historial de Discord.

Los comandos de supervisión comparten `SUPERVISOR_ROLE_IDS`. Ausente, vacío o `*` permite a
todos; una lista CSV exige que el miembro posea al menos uno de esos roles. La comprobación está
centralizada y no depende de `AUTHORIZED_USER_IDS`.

Los comandos de roles se registran únicamente cuando `ROLE_REQUEST_REVIEWER_ROLE_IDS` contiene al
menos un ID explícito. `ROLE_REQUEST_ALLOWED_ROLE_IDS` ausente, vacío o con `*` permite todos los
roles seguros de la guild; un CSV define un catálogo cerrado. No aplican `AUTHORIZED_USER_IDS`.
Los botones de una solicitud son visibles,
pero sus callbacks verifican siempre que quien decide posea uno de esos roles. Tanto los IDs de
revisores como los de canales pueden mezclar distintas guilds; en cada interacción solo se resuelven
los objetos pertenecientes a la guild actual.

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

## `/ts3`

Publica un embed breve y visible para todo el canal con la dirección `51.81.49.72:11189` del
servidor TeamSpeak 3. La dirección es determinista y el texto introductorio proviene de la persona
activa.

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

## `/honor`

Abre un modal con los mismos periodos de `/verguenza` y publica una sección por cada canal de
reportes configurado. Solo aparecen miembros con al menos un expediente cuyo estado actual sea ✅.
Cada estación presenta hasta diez miembros, ordenados primero por cantidad de aprobaciones, después
por porcentaje de aprobación y finalmente por volumen total de reportes.

Cada fila muestra aprobaciones, reportes presentados y porcentaje. El total conserva los reportes
que todavía estén pendientes o tengan otro resultado, de modo que el porcentaje represente el
snapshot completo del periodo. Es público, no notifica las menciones mostradas, no aplica
`AUTHORIZED_USER_IDS`, no ejecuta IA y no mantiene historial propio.

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
porque PostgreSQL conserva configuración, no el contenido operativo de Discord.

## `/solicitar-roles`

Solo puede ejecutarse en un canal de texto normal de una guild habilitada. Los hilos se rechazan
aunque su canal padre figure en `ROLE_REQUEST_ALLOWED_CHANNEL_IDS`. El modal muestra únicamente los
roles admitidos por `ROLE_REQUEST_ALLOWED_ROLE_IDS` que superan las validaciones de seguridad: para
agregar omite los ya poseídos y para retirar muestra solo los actuales. Si el catálogo supera las
25 opciones de un selector, se divide en dos o más grupos; se admiten hasta 100 roles solicitables
por guild, siempre que quepan en cuatro selectores, y un máximo de diez altas y diez bajas por
solicitud. También permite un motivo opcional. Antes de publicar, valida el
estado actual del miembro, la jerarquía del bot y que no se incluyan `@everyone`, roles gestionados,
roles revisores, roles administrativos o roles iguales o superiores al bot.
Sus títulos, etiquetas y ayudas son neutrales y comunes a todas las personalidades.
Las validaciones esperables —como solicitar un rol ya asignado o retirar uno que no se posee— se
presentan como alertas rojas y efímeras, con textos adaptados a la personalidad activa.

La solicitud se publica en el mismo canal con botones persistentes. El mensaje es la única fuente de
verdad: su ID identifica el expediente y un codec versionado reconstruye solicitante, roles y estado.
Solo se admite una solicitud pendiente por miembro entre todos los canales autorizados y visibles de
esa guild.
El embed omite altas, bajas, motivo, comentarios y grupos de cambios cuando no contienen información;
nunca utiliza una raya como valor de relleno. El codec sigue aceptando solicitudes antiguas que sí la
incluyan.

Un supervisor puede aprobar todo, abrir un segundo modal para seleccionar un subconjunto exacto o
rechazar con motivo. La resolución edita el mismo embed, registra supervisor y fecha, deshabilita los
tres botones y modifica los roles en una sola operación cuando corresponde. Los clics repetidos,
mensajes alterados o solicitudes ya resueltas no vuelven a ejecutar cambios.
Las confirmaciones privadas de creación y resolución también se presentan como embeds con colores
coherentes con su estado; el subsistema no utiliza respuestas planas.

## `/info-roles`

Explica públicamente y de forma breve cómo presentar una solicitud, dónde utilizar el comando y sus
posibles resultados. Puede ejecutarse en cualquier canal o hilo de una guild habilitada.

## Errores y observabilidad

Los errores de consultas deterministas se registran con identificadores de guild, interacción y
usuario cuando están disponibles. Discord recibe un mensaje contextual sin stacktrace ni detalles
técnicos. La búsqueda de cargos registra la categoría, longitud de la consulta y cantidad de
resultados, pero no el texto introducido por el usuario.
