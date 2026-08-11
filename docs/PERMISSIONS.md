# Permisos de Discord

## Intents

En Discord Developer Portal, habilita **Message Content Intent**. El bot solicita únicamente:

- Guilds
- Guild Messages
- Message Content

No requiere Server Members Intent ni Presence Intent.

## Scopes de instalación

- `bot`
- `applications.commands`

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
incluidos en `ROLE_REQUEST_ALLOWED_ROLE_IDS`; Discord no permite administrar roles iguales o
superiores. Los selectores solo muestran roles configurados que el bot puede administrar. En cada canal
admitido por `ROLE_REQUEST_ALLOWED_CHANNEL_IDS` también necesita `View Channel`, `Read Message
History`, `Send Messages` y `Embed Links`.

`Change Nickname` es opcional. La persona `walter_vigil` configura `nickname: null`, por lo que su
identidad global se sincroniza sin ese permiso. Solo una persona futura con nickname gestionado lo
requeriría.

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
