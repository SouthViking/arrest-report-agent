# Comandos de Discord

Para instalar el bot sigue [Instalación](SETUP.md). Para dar acceso al primer administrador y
publicar roles, canales y módulos sigue [Configuración inicial](CONFIGURATION.md). Las variables
del proceso y la importación inicial están en [Variables de entorno](ENVIRONMENT.md).

## Administración dinámica: `/config`

No recibe argumentos. Pueden usarlo los IDs definidos en `CONFIG_OWNER_USER_IDS` y los miembros con
un rol de la política `config_admin_roles`; cambiar el perfil global exige ser owner. Un intento no
autorizado recibe una alerta efímera con la personalidad activa y no alcanza el runner.

El comando abre un panel efímero con botones para:

- Consultar la configuración activa y actualizar la vista.
- Crear o descartar el borrador.
- Editar políticas de acceso mediante selectores e IDs ingresados en un formulario.
- Bloquear usuarios concretos del agente con precedencia sobre cualquier usuario o rol permitido.
- Seleccionar modelo, reasoning y límites principales del agente.
- Configurar módulos y sus opciones JSON.
- Importar, publicar, asignar o revertir personas, incluyendo carga de JSON y avatar.
- Publicar el borrador o restaurar una revisión histórica.
- Cambiar el perfil global cuando quien opera es owner.
- Publicar en el canal actual un resumen de consumo de OpenAI y Railway; esta acción solo aparece
  para `CONFIG_OWNER_USER_IDS` y no crea un comando adicional.

El panel está ligado al solicitante, expira tras quince minutos y revalida permisos en cada acción.
Cada mutación usa el ID de la interacción como clave idempotente y queda en `config_audit_events`.
Las publicaciones emiten una notificación de PostgreSQL para refrescar el runtime sin redeploy.

El módulo **Llamadas de servicio** admite `{}` y opciones de límites para sus estadísticas. Sus
destinos se administran mediante la política **Canales de llamadas de servicio**, que exige IDs
explícitos. Al retirar el último canal o deshabilitar el módulo, el polling se detiene y deja de
consumir la API SARP. `/estadisticas-llamadas` consulta el historial de esos canales, sin consultar
SARP ni usar IA; sus límites se describen más abajo.

El módulo **Vehículos robados** tampoco recibe opciones JSON. `/vehiculos-robados` reutiliza
`operational_staff_roles` en cualquier canal y permite elegir cantidad, estado y visibilidad. El
feed automático solo consulta SARP cuando la política **Canales de vehículos robados** contiene al
menos un destino explícito; retirar el último canal detiene el polling sin deshabilitar el comando.

El módulo **Estado de facción** recibe `{"faction_id": 3}` y habilita `/conectados` para esa facción.
El ID queda por guild en PostgreSQL y no se fija en el comando ni en el cliente SARP.

El módulo **FTP conectado** configura la facción y los IDs de los roles de Supervisory Staff, MFTO,
FTO y FTT. Habilita `/ftp-conectados` sin fijar esos valores dentro del comando.

El módulo **Solicitudes FTP** recibe `channel_id`, `additional_command_channel_ids`, `command_role_ids`
y `executive_role_ids`. Reutiliza los roles del módulo **FTP conectado**, mantiene un panel fijado en el
canal principal y puede habilitar `/solicitar-ficha` en otros canales de entrada sin cambiar dónde se
publican las tarjetas.

El módulo **Consulta de reportes de arresto** recibe `{"faction_id": 3}`. Admite además
`"max_rank_names_per_lookup": 20` (entre 1 y 50) para limitar los nombres consultados en lote
por la tool de rangos; se puede ajustar en una nueva revisión de configuración. Habilita
`/revisar-arresto`, `/ver-evidencia` y permite al agente completo buscar y vincular arrestos de esa facción, además de
consultar bolsas de evidencias cuando el usuario entrega una referencia explícita. En un expediente ya
vinculado, el propietario con acceso limitado solo puede consultar su referencia vigente al pedir
una nueva revisión. Si SARP está apagado globalmente, las consultas de reportes no se registran;
la aprobación completa sigue pendiente de verificar las órdenes si este módulo está habilitado.

Admite también `warrant_alerts_enabled`, `warrant_alert_channel_ids` y `warrant_alert_role_id`.
Al enviar `/revisar-arresto`, el backend consulta las órdenes abiertas de arresto o ambos del
sospechoso identificado por PCU. Agrupa el aviso en un mensaje por canal, menciona solo el rol
configurado y muestra emisor, firma, fecha, riesgo, cargos y enlace al expediente. PostgreSQL guarda
solo referencias de entregas para no repetir el aviso por arresto, orden y destino, incluso si
se vuelve a publicar el arresto. Las órdenes cerradas y las de solo allanamiento no generan avisos.
La primera revisión recibe el mismo resultado proactivamente. Una orden de arresto abierta,
firmada o pendiente de firma, exige correcciones y bloquea la aprobación completa del reporte.
El agente puede usar `get_suspect_arrest_warrants` para reconsultar las órdenes del expediente actual,
sin aceptar nombres o IDs de otros sospechosos ni repetir el aviso. Si el usuario indica que cerró
la orden, la tool comprueba SARP antes de aprobar; las demás correcciones también deben resolverse.
La publicación de un veredicto completo consulta de nuevo SARP, incluso con el atajo de aprobación
de un supervisor. Si SARP falla o la consulta queda incompleta, la revisión sigue y la aprobación
queda pendiente de verificar SARP, sin inventar una orden ni una deficiencia del reporte. Si solo falta el
detalle, se informa la orden confirmada por el listado sin inventar el emisor ni el estado de firma.
Solo una orden explícita actual de Command/Executive Staff permite al agente aprobar con órdenes
abiertas confirmadas: `approve_arrest_report` recibe `approval_message_id` y el backend verifica
el mensaje y los roles reales de Discord. Una ratificación actual como «ahora sí» o «es una orden»
puede retomar una orden previa del mismo usuario en el mismo hilo. El backend relee el historial
reciente de Discord, limitado al contexto disponible y a 50 mensajes, y enlaza la orden y su
ratificación en la constancia. Una cancelación, una condición o una nueva petición de revisión
posteriores invalidan esa orden previa. Usa siempre el ID del mensaje actual en la herramienta;
un error devuelve ese ID para corregir la llamada y registra el motivo del rechazo en logs.
No sirven citas, nombres de rol ni permisos genéricos
de administrador. La revisión normal y la reacción ✅ siguen sujetas al bloqueo. La excepción
conserva el estado real de las órdenes y deja constancia del autorizante; no permite saltar una
consulta SARP fallida o incompleta.

### `/aprobar-arresto`

Command/Executive Staff puede usarlo **dentro del hilo de un reporte** para una aprobación humana
de emergencia. Abre un modal con un campo de texto multilínea para escribir el motivo, obligatorio
y de 5 a 500 caracteres. No recibe opciones en el comando ni aprueba nada al abrir el formulario.
Al enviarlo se vuelven a comprobar los permisos vigentes y el hilo original. Exige un rol incluido en
`arrest_report_lookup.approval_override_role_ids`. La migración configura los roles actuales de
Command y Executive en LSSD; otros servidores deben indicar sus IDs. Una lista vacía lo deshabilita.
No depende del LLM ni de SARP y permite aprobar aunque haya órdenes abiertas o SARP no responda.
No cierra órdenes: el registro visible aclara que es una autorización humana sin verificar SARP.
Requiere que el bot, Discord y el historial de auditoría estén disponibles.

Utiliza la misma publicación formal de revisiones: historial, reacción ✅, tarjeta y cierre del hilo.
Conserva el responsable, motivo e interacción de Discord, con `model=manual` en el historial.
Si ya hay una revisión en curso, espera a que termine antes de usarlo para evitar resultados
contradictorios. No puede usarse en canales generales, hilos ajenos a reportes o por Supervisory
sin uno de los dos roles autorizados.

Los avisos se pueden deshabilitar o cambiar de destino por `/config` sin redesplegar; el contexto
del agente y el requisito de comprobar el cierre siguen activos aunque los avisos estén deshabilitados.

El módulo **Reportes de arresto pendientes** recibe
`{"faction_id": 3, "max_range_days": 90, "max_records": 1000}`. Habilita la consulta manual
determinista, reutiliza `operational_staff_roles` y no requiere una política de canales.

### `/registrar-robo-arma`

Registra un robo de arma en PostgreSQL y publica su ficha con las evidencias en el canal configurado.
Solo funciona para `operational_staff_roles` desde los canales exactos de
`stolen_weapons.source_channel_ids`; los hilos no heredan esa autorización. Los permisos y el destino
se comprueban nuevamente al adjuntar y confirmar. La migración habilita inicialmente el canal de
origen `1421923642983317545` y el destino `1565184787591860345` en LSSD. Se pueden cambiar o
deshabilitar desde `/config` sin redesplegar.

1. El formulario pide serial (1–100 caracteres), hechos (10–2000), fecha del robo (`DD/MM/AAAA`),
   propietario/víctima y ayudante que reporta (2–100 caracteres cada nombre).
2. Una previsualización privada permite adjuntar o cambiar entre una y cinco imágenes PNG, JPEG,
   WebP o GIF. Los archivos se validan por su contenido y con los límites de tamaño del servidor y
   del proceso. Solo el solicitante puede usar los controles.
3. **Confirmar registro** guarda las evidencias en el bucket privado y el reporte con sus referencias
   en BD antes de publicar. Tras publicarlo se elimina la previsualización privada. Cancelar o dejar
   expirar el formulario sin confirmar no crea un registro.

La fecha del robo es una fecha de calendario, separada del instante UTC de creación. El ayudante
indicado en el formulario y el usuario de Discord que registra quedan identificados por separado.
El serial original se conserva y la búsqueda interna normaliza mayúsculas y espacios, manteniendo
ceros iniciales y separadores. Distintos robos del mismo serial generan registros independientes.
PostgreSQL conserva claves estables del bucket, hashes, tipos, tamaños y orden de las imágenes;
las referencias no dependen de enlaces temporales de Discord. Si falla la publicación después de
guardar, el registro y las evidencias permanecen en BD/bucket. Una entrega incierta no se reintenta
automáticamente para evitar mensajes duplicados. La consulta por serial ya existe a nivel de
repositorio y mediante la herramienta `get_weapon_theft_reports`.

### `/cerrar-reporte-robo`

Usa los mismos canales habilitados y roles operativos que `/registrar-robo-arma`. Abre un formulario
con la referencia `ARM-…` del registro y un **Motivo del cierre** obligatorio de 5–500 caracteres.
Abrir el formulario no cambia nada; al enviarlo se revalidan solicitante, servidor, canal, módulo
y roles actuales. Cierra únicamente ese reporte, conservando archivos e historial, y actualiza
la ficha original a **Reporte de robo cerrado**, con **Cerrado por**, **Fecha de cierre** y el motivo.
Dos cierres concurrentes no sobrescriben la primera autorización. Repetir el comando sobre un
registro cerrado permite reintentar la actualización del mensaje sin cambiar su auditoría.

Si Discord falla después de guardar, el registro permanece cerrado y deja de generar alertas.
No existe borrado automático, reapertura ni cierre por decisión del agente.

### Seriales de armas en las revisiones

Cuando `get_evidence_bag` devuelve un serial explícito dentro de un análisis de arma, el backend
lo contrasta con la BD por servidor y el agente consulta `get_weapon_theft_reports` con todos los
seriales distintos en una lista `serials` (uno a cincuenta por llamada). Ambas rutas comparten
el resultado del mismo run; no repiten la consulta a BD cuando los datos ya están disponibles.
El resultado distingue `reported_stolen`, `closed`, `no_records`, `invalid_serial` y `unavailable`.
Los reportes cerrados se conservan en la respuesta como antecedentes históricos.

En un hilo de revisión, un robo abierto genera una alerta con plantilla fija en
`stolen_weapons.destination_channel_id`, con enlaces a la revisión y al registro del robo.
Se registra una entrega por reporte de robo y raíz de revisión, incluso entre reintentos o reinicios.
Una entrega incierta no se reenvía; un error definitivo admite reintento en otra consulta, sin un
temporizador de reenvíos. `notifications_enabled` permite desactivar las alertas desde `/config`.
Consultar seriales fuera de un reporte o sin una bolsa asociada no genera estas notificaciones.

La revisión muestra por separado una nota básica con serial y bolsa, sin enlaces internos ni datos
del denunciante. Es solo información: no modifica el veredicto, no se registra como deficiencia ni
genera cargos. Un registro cerrado, sin coincidencias o una consulta fallida no bloquea la aprobación.
El propietario del reporte recibe únicamente una proyección de metadatos del historial y puede
consultar seriales verificados en las bolsas de su propio reporte.

### `/justificar-arresto registrar` y `/justificar-arresto quitar`

Ambos reciben `reporte`, el ID SARP del reporte de arresto. Solo permiten gestionar los avisos a
los miembros de `pending_arrest_reports.justification_role_ids`: una lista independiente de roles,
editable desde el módulo **Reportes de arresto pendientes** en `/config`, sin redesplegar. La migración
configura inicialmente Supervisory, Command y Executive Staff en LSSD y preserva listas explícitas,
incluso vacías. Otros servidores deben configurar sus IDs; una lista vacía impide gestionar pausas.
Ser administrador o tener acceso al agente no sustituye estos roles. Usuarios bloqueados y bots
no pueden gestionarlas.

- `/justificar-arresto registrar reporte:1234` abre un modal con motivo obligatorio de 5–500
  caracteres, duración **Con vencimiento** o **Sin vencimiento** y fecha opcional en
  `DD/MM/AAAA HH:MM`, interpretada en la zona elegida mediante el mismo selector con banderas de
  FTP y estadísticas de llamadas. Una pausa temporal exige una fecha futura y una zona explícita;
  una indefinida exige dejar la fecha vacía y no requiere zona horaria. No hay zona preseleccionada.
  Se rechazan horas inexistentes o ambiguas por cambios de horario y el vencimiento se guarda en UTC.
- Al enviar se validan nuevamente el usuario, canal y servidor originales, la configuración y los
  roles actuales de Discord. Se consulta SARP y la revisión para comprobar que ese reporte siga
  pendiente en la facción configurada. Si no se puede comprobar o guardar, no se confirma el cambio.
- Volver a **registrar** el mismo reporte reemplaza el motivo/vencimiento conservando el historial.
- `/justificar-arresto quitar reporte:1234` retira la pausa con roles actuales, sin depender de SARP.
  Las menciones vuelven en el siguiente ciclo si sigue pendiente.

Las respuestas son privadas. Abrir o cerrar el modal no modifica nada. Las pausas afectan solo al
reporte seleccionado, no a otros pendientes del mismo agente ni a reportes futuros. No aprueban,
cierran ni extienden los plazos originales del reporte. PostgreSQL conserva motivo, autorizante,
fechas y las interacciones que registran, reemplazan o retiran cada justificación.

Los resúmenes manuales y automáticos mantienen los totales y la conformidad originales y separan
los casos con justificación en **Con justificación · sin menciones**, indicando su vencimiento.
Las pausas vigentes excluyen esos reportes de los pings al usuario y de las alertas recurrentes a
Supervisory; si todos los vencidos están justificados, no se envía esa alerta al rol. Si el agente
tiene otros reportes sin justificar, estos pueden seguir generando una mención. Una pausa temporal
vence automáticamente. La indefinida termina al retirarla o al comprobar en un escaneo que el
reporte ya está completo. La ausencia del reporte en la ventana consultada no implica que se
completó. Los cambios rigen para los siguientes envíos; los mensajes anteriores no se editan.

El módulo **Consulta y auditoría de radios** recibe
`{"faction_id": 3, "query_max_range_days": 30, "query_max_records": 2000,
"audit_max_range_days": 7, "audit_max_records": 500}`. `/radios-faccion` reutiliza
`operational_staff_roles`; `/auditar-radio` exige la política independiente `radio_audit_roles`.
Ambos permisos se modifican y publican por `/config` sin redesplegar.

El módulo **Blacklists** registra tipos con clave, nombre visible, prefijo, clase y cantidad máxima
de perfiles PCU, etiqueta del sujeto y canales. El canal fija el tipo y el usuario nunca lo
selecciona. `CCW` usa el canal `1530621801871441991`, referencias `CCW` y un perfil de personaje;
`Facción` usa el canal `1383926305136971906`, referencias `FAC` y hasta cinco perfiles de cuenta.
`/blacklist registrar` y `/blacklist consultar` exigen `operational_staff_roles`; la consulta
funciona desde cualquier canal y reconoce IDs o URLs de ambos perfiles. `/blacklist editar` y
`/blacklist eliminar` exigen además uno de los roles gestores de Command Staff o Executive Staff y
solo operan desde un canal del tipo afectado. La eliminación retira el mensaje, pero conserva la
entrada revocada y su auditoría en PostgreSQL. `/blacklist info` es público y se anuncia en
`/ayuda` cuando el módulo está activo.

La acción **Publicar consumo** consulta los proveedores configurados, omite cualquier campo que no
haya sido devuelto y envía un embed público en el canal donde se abrió `/config`. No usa IA. Los
resultados se conservan en memoria por cinco minutos; las credenciales nunca se muestran ni se
registran en logs.

Los comandos se registran directamente en la raíz, sin depender del nombre o personalidad del bot.
Salvo `/ayuda`, `/info-reportes`, `/revisar-arresto`, `/ts3`, `/info-roles`, `/blacklist info`, los
resultados de `/verguenza` y `/honor`, la entrada publicada por `/blacklist registrar` y el expediente
publicado por `/solicitar-roles`, sus respuestas son efímeras: solo las ve quien ejecutó el comando. Los selectores
de consultas quedan ligados a ese usuario, expiran a los 180 segundos y editan el mensaje existente
en vez de publicar respuestas adicionales. Los tres botones de una solicitud de roles son la
excepción: son persistentes, sobreviven reinicios y autorizan al supervisor en cada clic.

Los comandos deterministas no ejecutan el runner, no crean traces de OpenAI y no consumen tokens.

## Estadísticas de llamadas: `/estadisticas-llamadas`

Solo lo pueden usar los roles configurados en `operational_staff_roles` (Field, Supervisory,
Command y Executive en LSSD). No hay excepción por ser administrador. Funciona desde cualquier
canal o hilo de la guild y aparece en `/ayuda` para esos miembros cuando el módulo está activo.
Revalida permisos y configuración al enviar el modal; solo lee canales de `service_call_channels`
cuyo historial puedan ver tanto el solicitante como el bot.

El modal pide **Desde** y **Hasta** en `DD/MM/AAAA HH:MM`, la zona horaria mediante el mismo
catálogo IANA con banderas de `/solicitar-ficha`, y visibilidad (pública por defecto). La zona se
elige en cada consulta, no se guarda como preferencia. Convierte cada extremo a UTC según las
reglas de la fecha elegida; rechaza fechas inválidas, un inicio futuro, rangos invertidos y horas
ambiguas o inexistentes por cambios de horario. Se incluye el minuto final completo. Si **Hasta**
está en el minuto actual o en el futuro, se recorta al momento actual antes de comprobar el límite
de días; el resultado muestra el término efectivo en la zona elegida.

El resultado muestra llamadas únicas, cantidades y porcentajes entre paréntesis de Policía
(LSPD/LSSD), Ambulancia/bomberos (LSFD), y Otros si existen. Incluye zona, rango local seleccionado
y canales consultados. Solo reconoce mensajes del propio bot con el formato de dispatch, omite
webhooks y deduplica por ID incluso entre canales. La fecha válida es la del embed (SARP), no la
fecha de publicación en Discord. No guarda nombres, teléfonos ni contenido de las llamadas.

Las opciones de `service_calls`, esquema 1, se ajustan desde `/config` sin redesplegar:

```json
{
  "statistics_max_range_days": 90,
  "statistics_max_messages_per_channel": 10000,
  "statistics_late_delivery_hours": 48,
  "statistics_timeout_seconds": 60
}
```

La lectura por fecha de publicación se amplía con el margen de retraso configurado para recuperar
llamadas publicadas después del período. Si supera el límite de mensajes, falla algún canal o una
tarjeta carece de fecha original válida, el resultado se marca **Conteo parcial**. Si vence el
timeout se pide acotar el rango. Solo se admite una consulta simultánea por guild.
No garantiza el total de SARP: no recupera publicaciones borradas, llamadas nunca publicadas
ni retrasos superiores al margen indicado en el embed. Funciona sin SARP activo globalmente
porque no llama a esa API; sí requiere `service_calls` habilitado y sus canales configurados.

## Disponibilidad

| Comando | Acceso | Visibilidad | Fuente |
|---|---|---|---|
| `/ayuda` | Todos | Pública | Persona seleccionada y configuración del bot |
| `/info-reportes` | Acceso completo o acceso del propietario habilitado | Pública; rechazo efímero | Persona, canales y configuración de acceso del propietario |
| `/ampliar-cupo cantidad` | Rol configurado en `quota_grant_role_ids` o ID incluido en `REPORT_OWNER_QUOTA_ADMIN_USER_IDS`; acceso del propietario habilitado; dentro del hilo de un reporte | Embed público en el hilo; errores y acuse efímeros | PostgreSQL; añade de 1 a 10 rondas al cupo del autor de ese reporte |
| `/revisar-arresto` | `operational_staff_roles`; módulo `arrest_report_lookup` activo; solo en `report_channels` | Formulario; expediente e hilo públicos; errores efímeros | Reporte vigente de SARP, fotografías en Railway Bucket y tarjeta de Discord; el reporte puede adjuntarse manualmente o generarse según la configuración de la guild |
| `/guia` | Todos | Efímera | `report_guide.json` |
| `/cargos` | Todos | Efímera | Catálogo SARP con fallback `delitos.json` |
| `/mdc` | `operational_staff_roles`; solo si SARP está activo | Modal privado; resultado público o efímero | API SARP en vivo |
| `/info-mdc` | `operational_staff_roles`; solo si SARP está activo | Pública; rechazo efímero | Persona activa |
| `/conectados` | `operational_staff_roles`; módulo `faction_status` activo | Pública; rechazo efímero | Nómina de la facción en SARP |
| `/ftp-conectados` | `operational_staff_roles`; módulo `ftp_status` activo | Pública; rechazo efímero | Nómina cacheada de SARP y roles de Discord |
| `/solicitar-ficha` | FTT, Command o Executive; módulo `ftp_requests` activo y canal de entrada configurado | Modal desde comando o panel fijado; solicitud pública en el canal principal | PostgreSQL y roles de Discord |
| `/fichas-activas` | `operational_staff_roles`; módulo `ftp_requests` activo | Pública en cualquier canal; rechazo efímero | Solicitudes FTP pendientes en PostgreSQL |
| `/ver-evidencia` | `operational_staff_roles`; módulo `arrest_report_lookup` activo | Pública; rechazo efímero | Bolsa de evidencias en SARP |
| `/vehiculos-robados` | `operational_staff_roles`; módulo `stolen_vehicles` activo | Modal privado; resultado público o efímero | Denuncias recientes de vehículos en SARP |
| `/estadisticas-llamadas` | `operational_staff_roles`; módulo `service_calls` activo y canales configurados | Modal; resultado público por defecto o efímero | Historial de Discord, sin consultas a SARP ni IA |
| `/info-flota` | `operational_staff_roles`; módulo `fleet` activo | Pública; rechazo efímero | Guía de `/vehiculo`, `/flota`, filtros y visibilidad |
| `/vehiculo` | `operational_staff_roles`; módulo `fleet` activo | Pública; rechazo de acceso efímero | Estado, equipamiento y niveles disponibles del vehículo por matrícula real o visible |
| `/flota` | `operational_staff_roles`; módulo `fleet` activo | Panel privado; resultado privado por defecto o público | Resumen y rankings de la flota por modelo y ALPR |
| `/recursos` | `operational_staff_roles`; módulo `operational_resources` activo | Pública; rechazo efímero | Configuración de PostgreSQL y API SARP |
| `/foro` | Todos; módulo `forum_shortcuts` activo | Pública | Catálogo institucional configurado en PostgreSQL |
| `/reportes-pendientes` | `operational_staff_roles`; módulo `pending_arrest_reports` activo | Modal privado; resultado público o efímero | Reportes de la facción en SARP |
| `/informe-faccion` | `faction_report_roles`; módulo `faction_reports` activo | Modal privado; archivo público o efímero | Banco, nómina y rangos actuales de SARP |
| `/radios-faccion` | `operational_staff_roles`; módulo `radio_audit` activo | Modal privado; resultado público o efímero | Registros de radio de la facción en SARP |
| `/auditar-radio` | `radio_audit_roles`; módulo `radio_audit` activo | Modal privado; resultado público o efímero | Muestra fija de SARP, sección IV del manual y códigos radiales |
| `/bitacora` | Cualquier miembro en `logbook_channels`; módulo `logbook` activo | Selector privado, modal adaptado y entrada pública | PostgreSQL, Railway Bucket y Discord |
| `/blacklist registrar` | `operational_staff_roles`; módulo activo; canal asociado a un tipo | Modal privado y registro público | PostgreSQL y Discord |
| `/blacklist consultar` | `operational_staff_roles`; módulo activo; cualquier canal | Modal privado; resultado público por defecto o efímero | PostgreSQL |
| `/blacklist editar` | `operational_staff_roles` y rol gestor Command/Executive; canal asociado al tipo | Modal; edita el registro público sin confirmación adicional | PostgreSQL y Discord |
| `/blacklist eliminar` | `operational_staff_roles` y rol gestor Command/Executive; canal asociado al tipo | Confirmación en el modal; retira el registro público sin mensaje adicional | PostgreSQL y Discord |
| `/blacklist info` | Todos; módulo activo | Pública | Configuración de PostgreSQL |
| `/info-bitacora` | Cualquier miembro en `logbook_channels`; módulo `logbook` activo | Pública; rechazo efímero fuera de los canales | Persona activa y configuración de PostgreSQL |
| `/guia-bitacora` | Cualquier miembro en `logbook_channels`; módulo `logbook` activo | Modal privado; resultado público o efímero | Catálogo central de categorías y subtipos |
| `/resumen-bitacora` | `faction_report_roles`; módulo `logbook` activo | Selector y modal privados; resultado público o efímero | Registros estructurados publicados en PostgreSQL |
| `/faq` | Todos | Efímera | Preguntas curadas exclusivamente desde `report_guide.json` |
| `/verguenza` | Roles de analítica de reportes | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/honor` | Roles de analítica de reportes | Modal privado; resultado público | Mensajes y reacciones actuales de Discord |
| `/errores-reportes` | Roles de analítica de reportes | Modal privado; resultado público | Revisiones estructuradas en PostgreSQL |
| `/ts3` | Roles de personal operativo | Pública | Dirección fija, política `operational_staff_roles` y persona activa |
| `/manual` | Roles de personal operativo | Pública | Enlace configurado en el módulo `operational_resources` |
| `/pendientes` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/ranking` | Roles de supervisión | Efímera | Mensajes y reacciones actuales de Discord |
| `/mis-reportes` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/mi-resumen` | Guild y usuario habilitados | Efímera | Mensajes y reacciones actuales de Discord |
| `/solicitar-roles` | Todos en guild y canal habilitados | Estado público y revisión privada | PostgreSQL y Discord |
| `/info-roles` | Todos en una guild habilitada | Pública | Persona y configuración actuales |
| `/pager` | Roles emisores, en canales habilitados | Modal privado; pager público | PostgreSQL y Discord |
| `/pager-activos` | Todos en canales de pager | Pública y paginada | PostgreSQL |
| `/mis-pagers` | Todos en canales de pager | Efímera y paginada | PostgreSQL |
| `/info-pager` | Personal habilitado para crear, responder o cerrar pagers | Pública; rechazo efímero | Persona, roles y configuración actuales |
| `/callsign` | Roles de personal operativo | Efímera | PostgreSQL |
| `/quitar-callsign` | Roles de personal operativo | Efímera | PostgreSQL |
| `/callsigns` | Roles de personal operativo | Pública y paginada | PostgreSQL y miembros actuales de Discord |
| `/info-callsigns` | `operational_staff_roles` | Pública; rechazo efímero | Persona y configuración actuales |
| `/sugerencia` | Todos en una guild configurada | Modal y confirmación efímeros | PostgreSQL |
| `/info-sugerencias` | Todos en una guild configurada | Pública | Persona activa |
| `/sugerencias` | `CONFIG_OWNER_USER_IDS` | Efímera y paginada | PostgreSQL |
| `/estado-sugerencia` | `CONFIG_OWNER_USER_IDS` | Modal y confirmación efímeros | PostgreSQL |
| `/pregunta-manual` | `operational_staff_roles` | Pregunta y resultado públicos | Catálogo PostgreSQL |

Los comandos generales no aplican `AUTHORIZED_USER_IDS`; están destinados a todos los miembros que
puedan ver e invocar al bot. Los comandos personales sí aplican las allowlists centrales antes de
leer el historial de Discord.

`/info-reportes` no ejecuta IA ni describe el catálogo general de revisión. Explica cómo usar
`/revisar-arresto`, indicar el ID de SARP, adjuntar las fotografías y respaldos y continuar después
dentro del hilo. El formulario no pide el nombre del sospechoso: se obtiene desde PCU al validar que
el ID sea numérico y corresponda a un reporte existente. La capa de aplicación conserva la ruta sin ID
para poder habilitarla en el modal sin duplicar el flujo. La revisión inicial creada por el comando
no consume una intervención. El formato anterior con
`@mención Nombre Apellido` o `@mención Nombre Apellido - #NÚMERO` continúa disponible para reportes
legacy y conversaciones ya existentes. Si
`report_owner_access` está deshabilitado, informa que la capacidad no está disponible sin enseñar
pasos que no funcionarían.

`/ampliar-cupo` toma el reporte y su autor del hilo actual, sin permitir elegir otro destinatario.
Las concesiones se suman al límite base, no modifican el historial de rondas consumidas y registran
el ID de quien las otorgó. El embed público muestra únicamente los turnos disponibles en ese momento,
no el total ni los ya consumidos. `REPORT_OWNER_QUOTA_ADMIN_USER_IDS` acepta IDs separados por comas,
sin `*`. Los roles autorizados se configuran por servidor en `report_owner_access.quota_grant_role_ids`;
en LSSD son Command Staff y Executive Staff. Si ambas listas están vacías, nadie puede usarlo.
Al consumirse el último turno, o ante un nuevo intento con el cupo agotado, el bot envía un aviso
al canal `report_owner_access.quota_exhausted_notification_channel_id` mencionando a esos roles y
enlazando el hilo. El aviso se publica una sola vez por cada límite alcanzado; una ampliación permite
un nuevo aviso si también se consumen los turnos adicionales. En LSSD el destino inicial es el canal
privado de Command Staff.

La tarjeta estructurada se publica con franja amarilla. Solo un `review_scope=full` puede actualizar
esa franja: verde para `approved`, rojo para `corrections_required` y naranja para `inconclusive`.
Las conversaciones y revisiones `narrative`, `charges` o `targeted` conservan el último estado general.

Los comandos de supervisión comparten `SUPERVISOR_ROLE_IDS`. Ausente, vacío o `*` permite a
todos; una lista CSV exige que el miembro posea al menos uno de esos roles. La comprobación está
centralizada y no depende de `AUTHORIZED_USER_IDS`.

`/verguenza` y `/honor` usan la política PostgreSQL independiente `report_analytics_roles`. Debe
mantener IDs explícitos de los roles staff autorizados; una política deshabilitada bloquea ambos
comandos. Esta autorización tampoco depende de los usuarios habilitados para el agente.

Los comandos de roles se registran únicamente cuando `ROLE_REQUEST_REVIEWER_ROLE_IDS` contiene al
menos un ID explícito. `ROLE_REQUEST_ALLOWED_ROLE_IDS` ausente, vacío o con `*` permite todos los
roles seguros de la guild; un CSV define un catálogo cerrado. No aplican `AUTHORIZED_USER_IDS`.
Los botones aparecen solo en los canales de `role_request_review_channels` y sus callbacks verifican
siempre que quien decide posea uno de esos roles. Tanto los IDs de
revisores como los de canales pueden mezclar distintas guilds; en cada interacción solo se resuelven
los objetos pertenecientes a la guild actual.
En LSSD, la configuración publicada limita las copias privadas al canal de Command Staff y reserva
las decisiones al rol Command Staff.

Las consultas deterministas también reconocen `⏳`, `✅`, `❌` y `⚠️` cuando la reacción pertenece
al bot o a un miembro autorizado por esta misma política. Las reacciones de otros miembros no
alteran el estado del expediente.

## `/ayuda`

Publica un índice breve y visible para todo el canal. En lugar de repetir el catálogo completo de
comandos, dirige a la guía de cada función:

- `/info-reportes`
- `/info-roles`
- `/info-pager`
- `/info-callsigns`
- `/info-mdc`
- `/info-sugerencias`
- `/info-bitacora`, solo cuando `/ayuda` se abre en un canal de bitácora habilitado
- `/guia-bitacora`, solo cuando `/ayuda` se abre en un canal de bitácora habilitado
- `/resumen-bitacora`, solo para los roles configurados en `faction_report_roles`

Mantiene como referencias rápidas `/guia`, `/cargos`, `/faq` y `/pregunta-manual`. Los flujos operacionales, comandos
privados y herramientas de supervisión se explican en su guía especializada cuando corresponde.
Cuando la consulta SARP está habilitada, `/ayuda` muestra `/ver-evidencia` únicamente al personal
incluido en `operational_staff_roles`.

## `/ver-evidencia`

Recibe directamente un número positivo de bolsa, sin abrir un modal, y publica para todo el canal
el contenido analizado y la cadena de custodia vigente en SARP. Solo puede usarlo el personal de
`operational_staff_roles` cuando el módulo `arrest_report_lookup` y la integración SARP están
activos. No muestra IDs técnicos de análisis, muestras o agentes.

La misma consulta está disponible como tool para el agente, tanto con acceso completo como en una
revisión solicitada por el propietario, cuando las bolsas estén identificadas explícitamente. Puede
consultar hasta diez bolsas en una llamada, ajustando la cadena de custodia y los metadatos de
análisis por bolsa; la forma anterior de consultar una sola bolsa sigue disponible. Se permiten
como máximo diez consultas de bolsa y tres invocaciones de la tool por ejecución. La lógica de
consulta y manejo de fallos es compartida con el comando, y no conserva el contenido en caché.

## `/bitacora`, `/info-bitacora`, `/guia-bitacora` y `/resumen-bitacora`

El módulo `logbook` exige una política restringida `logbook_channels`. No aplica una lista de
usuarios o roles adicional: si una persona puede ver el canal configurado, puede registrar allí.
Fuera de esos canales ambos comandos responden con una alerta efímera y `/ayuda` no anuncia la
función.

`/bitacora` presenta primero un selector privado de categoría y movimiento de dinero. Luego abre un
modal adaptado con los subtipos válidos, asunto, descripción, monto cuando corresponda y hasta diez
adjuntos. La publicación usa una referencia mínima de cinco dígitos (`BIT-00001`) que continúa
creciendo sin un techo de 99.999. PostgreSQL conserva el registro estructurado y la metadata; el
contenido de los adjuntos se almacena en el Railway Bucket con claves basadas en checksum. Una sola
imagen se muestra como portada; entre dos y diez imágenes se presentan en una galería nativa de
Discord y los archivos no visuales permanecen disponibles para descarga.

En **Personal → Autorización de personaje adicional** se registra el miembro involucrado y se
detalla en la descripción qué personaje adicional se autorizó.

`/info-bitacora` publica una explicación breve con el tono de la personalidad activa. Las categorías,
montos, fechas y demás datos operacionales se muestran con redacción neutra.

`/guia-bitacora` abre un selector mínimo que permite mantener la guía privada o publicarla en el
canal. El resultado usa un único embed compacto cuyos campos inline enumeran las categorías y
subtipos del mismo catálogo utilizado por el formulario; Finanzas separa las opciones válidas de
ingreso y retiro. Por ello, la guía no puede quedar desincronizada del modal.

`/resumen-bitacora` puede ejecutarse en cualquier canal, pero exige pertenecer a
`faction_report_roles`. Permite elegir períodos rápidos de 1, 2, 7, 14, 30 o 90 días, o un rango
personalizado de hasta 366 días. Los filtros de categoría y subtipo se derivan del mismo catálogo
central, y el subtipo solo ofrece valores coherentes con la categoría seleccionada. El resultado
considera únicamente entradas publicadas, cuenta sus adjuntos y calcula ingresos, retiros y balance
desde los campos estructurados de PostgreSQL. La publicación es visible por defecto y puede cambiarse
a una respuesta privada.

## `/conectados`

Se registra con el cliente SARP y exige que el módulo `faction_status` contenga un `faction_id`
válido para la guild. Solo puede usarlo el personal de `operational_staff_roles`. Publica una vista
única con los contadores de miembros **Conectados** (`online`), **En servicio** (`online` y
`factionduty`) y el porcentaje en servicio respecto de los conectados. Los nombres se ordenan
alfabéticamente en dos grupos: `🟢` identifica al personal en servicio y `▫️` al conectado fuera de
servicio. Se muestran como máximo diez nombres por grupo y cada sección indica cuántos se omitieron.

La consulta recupera la nómina completa mediante una sola solicitud agrupada. Un caché compartido de
quince segundos evita multiplicar consumo cuando varias personas ejecutan el comando al mismo tiempo.

## `/ftp-conectados`

Publica solamente integrantes conectados relacionados con el Field Training Program. Separa
Supervisory Staff, Master FTO, FTO y FTT en campos no inline, muestra el estado de servicio y limita
cada sección a diez nombres. El cruce normaliza tildes y `_`, admite varios personajes separados por
`/` y el formato abreviado `N. Apellido`. Solo acepta una coincidencia única y nunca infiere una
identidad ambigua. Comparte la consulta y el caché corto de la nómina usado por `/conectados`.

## `/solicitar-ficha`

Abre para los Field Training Trainees, Command Staff y Executive Staff un selector de zona horaria y,
a continuación, un formulario con fase, fecha, hora, tipo de evaluador y comentarios opcionales. Cada
solicitud convierte el horario local elegido a UTC para que Discord lo muestre según la zona horaria de
cada persona. El bot publica y fija un panel en el canal de fichas; su botón abre el mismo flujo aunque los
miembros no puedan escribir mensajes normales. El comando
puede utilizarse además desde los canales de entrada adicionales configurados. Una
solicitud general avisa a FTO, MFTO y Supervisory Staff y puede tomarla cualquier integrante habilitado
de esa cadena o de Command/Executive. Una solicitud de supervisor menciona a Supervisory Staff,
Command Staff y los roles adicionales de notificación configurados (FTP Management en LSSD).
Las menciones adicionales no conceden permisos: una solicitud de supervisor solo admite Supervisory Staff,
Command Staff o Executive Staff. Una persona autorizada puede tomar la solicitud aunque también sea
su autora. El autor, Command Staff y Executive Staff pueden cancelarla. Al tomarla se edita el mismo
mensaje, se notifica al trainee y se desactivan los botones; al cancelarla se actualiza la tarjeta sin
enviar una confirmación adicional. Un trainee puede mantener varias solicitudes para fechas u horarios
distintos.

## `/fichas-activas`

Publica un resumen compacto de las solicitudes que todavía esperan instructor, ordenadas por fecha y
hora. Cada fila muestra trainee, fase, tipo de evaluador y un acceso directo a la solicitud. Presenta
diez resultados por página y puede utilizarse en cualquier canal por `operational_staff_roles`.

## `/radios-faccion` y `/auditar-radio`

`/radios-faccion` funciona en cualquier canal para `operational_staff_roles`. Permite elegir la
última 1, 2, 4 u 8 horas; los últimos 1, 2, 7, 14 o 30 días; o un rango UTC personalizado con
`AAAA-MM-DD HH:MM / AAAA-MM-DD HH:MM`. También admite slot —el 1 por defecto—, miembro opcional y
visibilidad. El servicio solicita a SARP el rango de días documentado, aplica localmente las horas
exactas, interpreta el prefijo `[CH: ..., S: N]` sin depender de mayúsculas o espacios exactos y deja
de paginar al alcanzar registros anteriores al intervalo. Muestra diez líneas compactas por página,
sin IDs técnicos; sobre cien filas adjunta un TXT generado en memoria con un registro por línea. No
guarda radios ni archivos.

`/auditar-radio` está reservado a `radio_audit_roles`, usa siempre el slot 1 de SCC, comparte los
presets de una hora a siete días y admite un rango horario personalizado hasta el máximo configurado.
La muestra se obtiene una sola vez antes de OpenAI y queda inmutable;
el agente no puede volver a consultar SARP. Sus únicas herramientas permiten leer el resto de esa
muestra, buscar códigos radiales, descubrir el índice de comunicación y consultar por conceptos o
claves concretas el catálogo local extraído exclusivamente de la sección IV del manual. El índice
solo se consulta cuando el tema es incierto; una búsqueda conocida puede ir directamente a las
secciones relevantes. El resultado agrupa hallazgos por miembro y clasifica gravedad comunicacional leve,
moderada o grave, sin recomendar sanciones. Los traces usan el workflow `radio_usage_audit` y no
incluyen el contenido de los mensajes.

## `/recursos`

Funciona en cualquier canal y exige pertenecer a `operational_staff_roles`. Publica un único panel
con el estado actual del servidor y la facción, las direcciones de conexión, los enlaces oficiales
configurados y la descarga vigente del caché cuando SARP la informa. Los elementos aparecen
anidados dentro de cada sección y el icono del servidor se usa como miniatura.

El módulo dinámico `operational_resources` almacena por guild el `faction_id`, la dirección del
servidor y los enlaces oficiales. Reutiliza el servicio y las cachés de `/conectados`; la metadata
del caché oficial se conserva durante cinco minutos y, si esa consulta falla, se omite solamente
esa sección. `/conectados` sigue siendo el comando para consultar el detalle nominal del personal.

## `/internos`

Se registra únicamente con `SARP_API_ENABLED=true`, funciona en cualquier canal y exige pertenecer
a `operational_staff_roles`. Publica para todo el canal un listado alfabético de los personajes
conectados cuyo estado actual indica que están en prisión. No muestra nivel, cuenta ni IDs internos.

La consulta usa el endpoint público de personajes conectados mediante el cliente SARP compartido.
La muestra se conserva brevemente en memoria, agrupa solicitudes concurrentes y se reutiliza durante
toda la navegación sin nuevas consultas. `SARP_ONLINE_PLAYERS_CACHE_SECONDS` controla el TTL local y
`ONLINE_INMATES_PAGE_SIZE` la cantidad de nombres por página. Los detalles del caché no se exponen en
Discord.

## `/vehiculos-robados`

Se registra únicamente con `SARP_API_ENABLED=true` y funciona en cualquier canal para miembros de
`operational_staff_roles`. El formulario permite solicitar entre 1 y 50 denuncias recientes,
filtrar por activas, resueltas o todas, y elegir una respuesta pública o efímera. El listado usa una
fila compacta por vehículo, una leyenda de estados y paginación de diez resultados ligada a quien
inició la consulta.

El feed automático comparte el mismo servicio, pero solo consulta denuncias activas cuando existen
canales explícitos en `stolen_vehicle_channels`. Publica cada denuncia nueva con matrícula, modelo,
colores, propietario, descripción y hora relativa. No persiste esos datos: guarda únicamente el
último ID entregado a cada canal.

## `/foro`

Está disponible para todos los miembros, funciona en cualquier canal y publica una respuesta visible
para todos. Presenta un embed institucional y neutral con el GIF, las secciones y
los enlaces oficiales definidos por la guild. No usa IA, la personalidad activa ni la API SARP.

El módulo dinámico `forum_shortcuts` almacena el nombre institucional, la descripción, el banner y
un catálogo tipado de secciones y enlaces. Puede editarse y publicarse mediante `/config`; los
cambios se recargan en tiempo real sin reiniciar el bot.

## `/reportes-pendientes`

Funciona en cualquier canal y solo para miembros incluidos en `operational_staff_roles`. El modal
permite elegir los últimos 7, 14, 30 o 90 días, o un rango personalizado `AAAA-MM-DD`, y seleccionar
si el resultado será visible para todos o únicamente para quien consultó.

La consulta no usa IA ni persiste el contenido. Lee hasta el máximo configurado, considera completa
una narrativa que comienza con `Yo` y detecta como posibles pendientes los textos vacíos, placeholders,
repeticiones y descripciones breves no estándar. El embed resume revisados, pendientes y cumplimiento;
agrupa los resultados por agente y calcula un score interno según la ausencia del PCU, el estado de
la revisión y sus plazos independientes de 24 y 48 horas. Los agentes se ordenan por su caso más
grave, score acumulado y cantidad; dentro de cada grupo aparecen primero los casos más graves y
antiguos. `🔴`, `🟠`, `🟡` y `🟢` indican casos críticos, de alta prioridad, próximos al límite o
todavía dentro de plazo.
Las revisiones existentes enlazan su mensaje; tanto el arresto como la última revisión muestran el
tiempo relativo nativo de Discord. Cada página contiene hasta ocho reportes y repite el agente cuando
su grupo continúa.

## `/mdc`

Se registra únicamente con `SARP_API_ENABLED=true`. El personal incluido en
`operational_staff_roles` abre un formulario privado, indica el nombre exacto del personaje y elige
si recibe una ficha pública en el mismo canal o una respuesta efímera visible solo para sí. La
coincidencia ignora mayúsculas, espacios repetidos y la
diferencia entre espacios y guiones bajos, pero nunca elige mediante búsqueda difusa.

La ficha proyecta solo información utilizable IC: estado de custodia, órdenes activas, multas
impagas, vehículos, propiedades y antecedentes de arresto. No expone el ID interno del personaje,
cuenta, nivel ni otros campos OOC. Después de resolver el personaje, las secciones se consultan en
paralelo; un rate limit o error parcial se identifica en el embed sin descartar las demás secciones.
Si la búsqueda inicial falla, no se publica una ficha. Los errores y rechazos de acceso son efímeros.

`/info-mdc` comparte la misma restricción de acceso y publica una explicación breve del formulario,
la visibilidad seleccionable y las secciones que puede devolver el MDC.

## `/pregunta-manual`

Abre un formulario privado y luego publica una pregunta de práctica en el canal donde se ejecuta.
Solo los miembros incluidos en `operational_staff_roles` pueden solicitarla, pero cualquier miembro
puede responder. El formulario permite elegir dificultad aleatoria, básica, intermedia o avanzada;
tipo aleatorio, selección única o verdadero/falso; y un plazo de 10, 15 o 30 segundos, o 1, 3 o 5
minutos. La primera respuesta válida cierra el ejercicio, deshabilita sus componentes y deja el
resultado visible para todo el canal.

El catálogo vive en PostgreSQL y se carga por guild. El seed contiene 120 preguntas de
selección única y verdadero/falso, distribuidas entre organización, aspectos legales, procedimientos,
escenarios, detección de errores, códigos radiales, alfabeto fonético y reportes. Cincuenta son casos
avanzados de comprensión y relación de conceptos. Las preguntas activas se seleccionan como un mazo
compartido por servidor y combinación de filtros: ninguna se repite dentro de ese conjunto hasta que
se hayan mostrado todas, incluso tras reiniciar el bot. Solo puede mantener una pregunta pendiente y
debe esperar 30 segundos antes de pedir otra. Si nadie responde dentro del plazo seleccionado, el
mismo embed cambia a `Tiempo agotado`, deshabilita las respuestas y muestra la solución, explicación
y referencia del manual. Este MVP no registra respuestas, puntajes, precisión ni rankings, y nunca
llama a OpenAI.

El esquema admite categorías, dificultad, revisión y distintos modos de respuesta. La interfaz
inicial activa únicamente selección única y verdadero/falso; los modos futuros permanecen fuera de
la consulta hasta contar con su interacción y validación correspondientes.

El catálogo inicial se instala de forma idempotente mediante
`scripts/seed_manual_quiz_questions.sql`, pasando el snowflake de la guild como variable `guild_id`.

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
visible para todo el canal con la dirección `172.96.11.180:11189` del servidor TeamSpeak 3. La dirección
es determinista y el texto introductorio proviene de la persona activa. Los intentos no autorizados
reciben una alerta efímera ajustada a esa personalidad.

## `/manual`

Solo los miembros incluidos en `operational_staff_roles` pueden ejecutarlo. Publica un embed breve
y visible para todo el canal con un enlace al manual oficial de la facción. La URL se reutiliza desde
el módulo dinámico `operational_resources`; si no está habilitado, el comando responde de forma
efímera sin exponer una dirección alternativa.

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

Si el módulo configura el ingreso FTT o un rol de retiro, el comando muestra primero opciones privadas
para elegir el flujo. **Ingreso FTT** solicita únicamente la estación de destino y prepara el paquete
de roles iniciales configurado, omitiendo los que el miembro ya posea. La solicitud queda identificada
como ingreso en sus tarjetas pública y privada, y solo admite aprobación o rechazo completos.
El rol CSD TTCF ya no forma parte del paquete de ingreso ni del catálogo manual de LSSD.
La migración retira esa disponibilidad de la configuración publicada y los borradores, sin
modificar los roles que ya poseen los miembros ni las solicitudes anteriores.
**Aprobar ingreso** abre un modal con un selector opcional de un usuario como instructor principal.
Confirmar aprueba el paquete y publica la bienvenida configurada, mencionando al solicitante e
incluyendo los canales y enlaces del FTP. El párrafo del instructor solo aparece si se eligió uno;
cerrar el modal deja la solicitud pendiente. Se vuelven a comprobar los permisos del revisor y que
el instructor siga siendo un miembro del servidor y no sea un bot. Las aprobaciones normales y
de retiro mantienen su flujo sin este selector.
**Solicitar retiro** se muestra solo a miembros con un rol de `operational_staff_roles` y usa un
formulario reducido con motivo opcional y confirmación explícita. La solicitud agrega el rol de
retiro y muestra los roles administrables actuales que se retirarán. Al aprobarla, el bot recalcula
ese conjunto para incluir cambios posteriores, quita todos los roles que puede administrar y
ejecuta una sola edición. Luego comprueba el resultado y advierte si quedan roles que Roy no puede
retirar. El ingreso FTT solo aparece a quienes no tienen un rol operativo ni `Retired`; ambos
criterios se vuelven a comprobar al enviar y aprobar. No existe aprobación parcial para un retiro.

La solicitud deja en el mismo canal un embed público sin motivo, revisor ni comentario. PostgreSQL
conserva el expediente y las referencias a sus copias privadas; cada canal configurado en
`role_request_review_channels` recibe el detalle completo y los botones persistentes. Solo se admite
una solicitud pendiente por miembro en la guild. Enviar otra solicitud válida reemplaza las anteriores
pendientes, aunque sus mensajes hayan sido eliminados. Las tarjetas conservadas se muestran canceladas,
en gris, con sus controles deshabilitados y un enlace a la nueva solicitud. Si falla la publicación de
la nueva, la anterior permanece pendiente. Si una solicitud ya está publicándose o resolviéndose, se
debe esperar a que termine antes de volver a enviar. Las solicitudes antiguas basadas únicamente en el
mensaje público siguen resolviéndose con su codec v1.
El embed omite altas, bajas, motivo, comentarios y grupos de cambios cuando no contienen información;
nunca utiliza una raya como valor de relleno. El codec sigue aceptando solicitudes antiguas que sí la
incluyan.

Un revisor autorizado puede aprobar todo, abrir un segundo modal para seleccionar un subconjunto exacto o
rechazar con motivo. Aprobaciones parciales y rechazos exigen comentario. La resolución edita el
estado público y todas las copias privadas, deshabilita los botones y modifica los roles en una sola
operación cuando corresponde. El solicitante recibe el comentario por MD; si Discord bloquea el MD,
el mensaje público ofrece **Ver resolución**, visible de forma privada solo para ese solicitante o
un revisor autorizado.
Los botones no agregan confirmaciones efímeras de éxito: las ediciones de los mensajes son la
confirmación. Los clics repetidos no vuelven a ejecutar cambios.

## `/info-roles`

Explica públicamente y de forma breve cómo presentar una solicitud, dónde utilizar el comando y sus
posibles resultados, incluido el ingreso guiado FTT cuando está disponible. Puede ejecutarse en
cualquier canal o hilo de una guild habilitada.

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
números, una letra de `A` a `Z` y un número final opcional —por ejemplo `280L`, `64A` o `60F1`— y
normaliza la letra a mayúscula. Esta restricción solo aplica al comando: el formulario de respuesta del pager
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
