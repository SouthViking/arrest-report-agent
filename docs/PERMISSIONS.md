# Permisos de Discord

## Intents

En Discord Developer Portal, habilita **Message Content Intent** y **Server Members Intent**. El bot
solicita únicamente:

- Guilds
- Guild Messages
- Message Content
- Guild Members

`Server Members Intent` permite completar el directorio de la guild para las tools de búsqueda de
miembros, roles y audiencia de canales. Sin ese intent, Discord rechazará la conexión con código
`4014`. No requiere Presence Intent.

## Scopes de instalación

- `bot`
- `applications.commands`

El módulo de pagers requiere que el bot pueda ver el canal, enviar mensajes, insertar enlaces,
adjuntar embeds, leer historial y mencionar únicamente los roles destinatarios configurados. Para
mantener permisos mínimos, esos roles deben estar marcados como mencionables; no se necesita
`Mention @everyone, @here, and All Roles`.

## Permisos mínimos por canal

- View Channel
- Read Message History
- Send Messages
- Add Reactions
- Create Public Threads
- Send Messages in Threads
- Manage Threads
- Embed Links

El bitfield base es `326417599552`; con `Manage Roles` es `326686035008`. Se recomienda revisar
permisos por nombre en lugar de conservar un número de forma manual.

`Manage Roles` solo es necesario cuando se habilita el subsistema con
`ROLE_REQUEST_REVIEWER_ROLE_IDS`. El rol más alto del bot debe quedar por encima de todos los roles
admitidos por `ROLE_REQUEST_ALLOWED_ROLE_IDS`; Discord no permite administrar roles iguales o
superiores. Los selectores solo muestran roles admitidos que el bot puede administrar. En cada canal
admitido por `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` también necesita `View Channel`, `Read Message
History`, `Send Messages`, `Embed Links` y `Attach Files`.
Los canales privados definidos en `role_request_review_channels` requieren también `View Channel`,
`Read Message History`, `Send Messages`, `Embed Links` y `Attach Files`; los botones solo se publican allí.

`Change Nickname` es opcional. La persona `walter_vigil` configura `nickname: null`, por lo que su
identidad global se sincroniza sin ese permiso. Solo una persona futura con nickname gestionado lo
requeriría.

Cada canal configurado en `service_call_channels` requiere únicamente `View Channel`, `Send
Messages` y `Embed Links`. El feed desactiva menciones y no necesita leer el historial del canal.
Los canales de `stolen_vehicle_channels` utilizan el mismo conjunto mínimo de permisos y tampoco
requieren leer historial ni mencionar usuarios.

Cada canal asociado a un tipo de blacklist requiere `View Channel`, `Send Messages` y `Embed
Links`. El acceso de los usuarios se controla con los permisos y roles propios del canal; el bot no
permite registrar ni consultar sin pertenecer a `operational_staff_roles`. Los comandos de edición
y eliminación exigen además uno de los roles gestores de Command Staff o Executive Staff definidos
en el módulo. `/blacklist consultar` puede ejecutarse desde cualquier canal visible donde el bot
pueda responder. El bot elimina únicamente sus propios mensajes de blacklist.

La biografía propia del bot se sincroniza mediante el perfil de miembro de cada servidor y no
requiere agregar un permiso de Discord.

Los roles configurados en `REVIEW_ESCALATION_ROLE_IDS` deben permitir menciones para producir una
notificación. Se recomienda habilitar esa propiedad solamente en dichos roles; el bot no necesita el
permiso amplio **Mention @everyone, @here, and All Roles**.

## Canales privados

Los permisos deben ser efectivos en ambos canales y sus overrides:

- `gnt-arrestos`: `1462992794569281617`
- `dlm-arrestos`: `1462992859761217745`

Un rol puede tener permisos globales y perder `Send Messages` por un override específico. Revisa
los permisos efectivos del miembro del bot en ambos canales desde la configuración de Discord.

El bot elimina únicamente sus propias reacciones de estado. La tool agéntica de reacción también
queda limitada al mensaje raíz y al canal o hilo activo; nunca elimina reacciones humanas. Solo
archiva y bloquea automáticamente hilos creados por la propia cuenta del bot.
