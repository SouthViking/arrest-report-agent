# Comandos de Discord

## Administración dinámica: `/config`

No recibe argumentos. Pueden usarlo los IDs definidos en `CONFIG_OWNER_USER_IDS` y los miembros con
un rol de la política `config_admin_roles`; cambiar el perfil global exige ser owner. Un intento no
autorizado recibe una alerta efímera con la personalidad activa y no alcanza el runner.

El comando abre un panel efímero con botones para:

- Consultar la configuración activa y actualizar la vista.
- Crear o descartar el borrador.
- Editar políticas de acceso mediante selectores e IDs ingresados en un formulario.
- Seleccionar modelo, reasoning y límites principales del agente.
- Configurar módulos y sus opciones JSON.
- Importar, publicar, asignar o revertir personas, incluyendo carga de JSON y avatar.
- Publicar el borrador o restaurar una revisión histórica.
- Cambiar el perfil global cuando quien opera es owner.

El panel está ligado al solicitante, expira tras quince minutos y revalida permisos en cada acción.
Cada mutación usa el ID de la interacción como clave idempotente y queda en `config_audit_events`.
Las publicaciones emiten una notificación de PostgreSQL para refrescar el runtime sin redeploy.

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
| `/verguenza` | Roles de analítica de reportes | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/honor` | Roles de analítica de reportes | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/errores-reportes` | Roles de analítica de reportes | Modal privado; resultado público | Revisiones estructuradas en PostgreSQL |
| `/ts3` | Roles de personal operativo | Pública | Dirección fija, política `operational_staff_roles` y persona activa |
| `/pendientes` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/ranking` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/mis-reportes` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/mi-resumen` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/solicitar-roles` | Todos en guild y canal habilitados | Modal privado; solicitud pública en el canal | Mensaje versionado de Discord |
| `/info-roles` | Todos en una guild habilitada | Pública | Persona y configuración actuales |
| `/pager` | Roles emisores, en canales habilitados | Modal privado; pager público | PostgreSQL y Discord |
| `/pager-activos` | Todos en canales de pager | Pública y paginada | PostgreSQL |
| `/mis-pagers` | Todos en canales de pager | Efímera y paginada | PostgreSQL |
| `/info-pager` | Todos en una guild configurada | Pública | Persona y configuración actuales |
| `/callsign` | Roles de personal operativo | Efímera | PostgreSQL |
| `/quitar-callsign` | Roles de personal operativo | Efímera | PostgreSQL |
| `/callsigns` | Roles de personal operativo | Pública y paginada | PostgreSQL y miembros actuales de Discord |
| `/info-callsigns` | Todos en una guild configurada | Pública | Persona y configuración actuales |
| `/sugerencia` | Todos en una guild configurada | Modal y confirmación efímeros | PostgreSQL |
| `/info-sugerencias` | Todos en una guild configurada | Pública | Persona activa |
| `/sugerencias` | `CONFIG_OWNER_USER_IDS` | Efímera y paginada | PostgreSQL |
| `/estado-sugerencia` | `CONFIG_OWNER_USER_IDS` | Modal y confirmación efímeros | PostgreSQL |

Los comandos generales no aplican `AUTHORIZED_USER_IDS`; están destinados a todos los miembros que
puedan ver e invocar al bot. Los comandos personales sí aplican las allowlists centrales antes de
leer el historial de Discord.

Los comandos de supervisión comparten `SUPERVISOR_ROLE_IDS`. Ausente, vacío o `*` permite a
todos; una lista CSV exige que el miembro posea al menos uno de esos roles. La comprobación está
centralizada y no depende de `AUTHORIZED_USER_IDS`.

`/verguenza` y `/honor` usan la política PostgreSQL independiente `report_analytics_roles`. Debe
mantener IDs explícitos de los roles staff autorizados; una política deshabilitada bloquea ambos
comandos. Esta autorización tampoco depende de los usuarios habilitados para el agente.

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

Solo los miembros incluidos en `operational_staff_roles` pueden ejecutarlo. Publica un embed breve y
visible para todo el canal con la dirección `51.81.49.72:11189` del servidor TeamSpeak 3. La dirección
es determinista y el texto introductorio proviene de la persona activa. Los intentos no autorizados
reciben una alerta efímera ajustada a esa personalidad.

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

El resultado es visible para todo el canal, pero usa menciones sin notificación. Solo los miembros
incluidos en la política `report_analytics_roles` pueden ejecutarlo, y el snapshot incluye únicamente
canales configurados que ese miembro y el bot puedan leer. No aplica la política de usuarios del agente,
no ejecuta IA y no conserva historial.

## `/honor`

Abre un modal con los mismos periodos de `/verguenza` y publica una sección por cada canal de
reportes configurado. Solo aparecen miembros con al menos un expediente cuyo estado actual sea ✅.
Cada estación presenta hasta diez miembros, ordenados primero por cantidad de aprobaciones, después
por porcentaje de aprobación y finalmente por volumen total de reportes.

Cada fila muestra aprobaciones, reportes presentados y porcentaje. El total conserva los reportes
que todavía estén pendientes o tengan otro resultado, de modo que el porcentaje represente el
snapshot completo del periodo. El resultado es público, pero solo los roles de
`report_analytics_roles` pueden generarlo. No notifica las menciones mostradas, no aplica la política
de usuarios del agente, no ejecuta IA y no mantiene historial propio.

## `/errores-reportes`

Abre un modal para filtrar por periodo, estación, severidad y cantidad de categorías. Publica un
ranking determinista construido desde los hallazgos estructurados que el agente registra al terminar
cada revisión completa o parcial. Las re-revisiones se conservan como intentos independientes, pero
el conteo principal considera cada mensaje raíz una sola vez por categoría para evitar inflar un
error repetido dentro del mismo expediente.

El comando usa `report_analytics_roles`, su resultado es público y no ejecuta una llamada adicional
a OpenAI. La acumulación comienza con el despliegue de esta versión; no existe backfill de revisiones
anteriores.

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

## Pagers

El módulo se configura sin redeploy desde `/config`: crea un borrador, deja como restringidas las
políticas **Canales de pagers**, **Emisores de pagers**, **Destinatarios de pagers**,
**Responsables de cierre de pagers** y **Personal operativo con callsign**, habilita el módulo
**Pagers** y publica. La opción
`starting_number` fija el primer correlativo cuando la guild todavía no tiene registros; LSSD se
inicializa en `92`.

`/pager` reutiliza automáticamente el callsign registrado y solicita uno o más destinatarios,
ubicación y situación mediante un modal. Si el emisor todavía no registró un callsign, conserva un
campo libre como fallback. Publica
una única notificación para todos los roles seleccionados y un embed que se actualiza con las unidades
respondientes. El emisor no puede registrarse también como respondiente. Sus botones permiten registrar
o actualizar una respuesta, retirarse, editar sus datos, concluir con resultado o cancelar con motivo. Solo el
emisor y los responsables de cierre pueden editar; los cambios de destinatarios no generan otra
notificación. Las operaciones exitosas se reflejan directamente en el mensaje sin confirmaciones
efímeras adicionales.

Al emitir un pager se exigen los roles configurados en `pager_creator_roles`; al usar
**Responder / actualizar**, uno de los roles de `operational_staff_roles`. En ambos flujos, si el
miembro ya registró `/callsign`, ese valor se utiliza directamente y el campo correspondiente no se
muestra. Si todavía no existe, el pager permite indicarlo libremente como fallback. `/callsign`
permite registrarlo o reemplazarlo con un modal; tanto el
resultado como los errores son efímeros y usan la personalidad activa. Solo acepta dos o tres
números seguidos de `L`, `F`, `B` o `S` —por ejemplo `280L`, `60F`, `282B` o `281S`— y normaliza la
letra a mayúscula. Esta restricción solo aplica al comando: el formulario de respuesta del pager
mantiene un valor libre como fallback. `/callsigns` publica para el
personal operativo un directorio paginado, agrupado por callsign y limitado a miembros que todavía
pertenecen a la guild. Las referencias no generan notificaciones. `/info-callsigns` explica ambos
comandos públicamente y de forma breve, incluido cómo el pager reutiliza el callsign al pulsar
`/pager` o **Responder / actualizar**.

`/quitar-callsign` elimina de forma privada el registro reutilizable. No altera pagers históricos;
los siguientes formularios de creación o respuesta vuelven a mostrar el campo libre.

`/pager-activos` publica cinco avisos por página con enlaces directos. `/mis-pagers` presenta de forma
privada los avisos emitidos o respondidos por el usuario. `/info-pager` explica públicamente el flujo y
los canales habilitados sin mostrar IDs ni configuración interna.

## Sugerencias

`/sugerencia` funciona en cualquier canal o hilo de una guild configurada y no aplica
`AUTHORIZED_USER_IDS`. Abre un formulario privado con seis categorías visuales, título y descripción.
El contenido se guarda en PostgreSQL y el canal recibe únicamente la interacción efímera del autor;
no se necesita un canal de destino.

Cada miembro puede mantener hasta tres sugerencias pendientes por guild y debe esperar 60 segundos
entre envíos. La verificación usa un lock transaccional por guild y usuario, por lo que solicitudes
simultáneas tampoco pueden superar la cuota. `/info-sugerencias` explica públicamente el flujo, las
categorías y la regla de uso responsable, sin exponer los comandos administrativos.

`/sugerencias` está reservado a `CONFIG_OWNER_USER_IDS`. Presenta de forma efímera las entradas más
recientes, cinco por página, con selector para abrir su contenido completo. `/estado-sugerencia`
también está reservado a owners y permite cambiar mediante un modal entre `pending` y `archived`.
Archivar libera un cupo del autor; cada alta y transición queda registrada en `suggestion_events`.
