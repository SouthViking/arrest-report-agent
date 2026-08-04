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

El bitfield combinado es `326417599552`, aunque se recomienda revisar permisos por nombre en lugar de
conservar un número de forma manual.

`Change Nickname` es opcional: la identidad del producto no depende de modificar el apodo de Discord.

Los roles configurados en `REVIEW_ESCALATION_ROLE_IDS` deben permitir menciones para producir una
notificación. Se recomienda habilitar esa propiedad solamente en dichos roles; el bot no necesita el
permiso amplio **Mention @everyone, @here, and All Roles**.

## Canales privados

Los permisos deben ser efectivos en ambos canales y sus overrides:

- `gnt-arrestos`: `1462992794569281617`
- `dlm-arrestos`: `1462992859761217745`

Un rol puede tener permisos globales y perder `Send Messages` por un override específico. Revisa
los permisos efectivos del miembro del bot en ambos canales desde la configuración de Discord.

El bot elimina únicamente sus propias reacciones de estado. Nunca elimina reacciones humanas. Solo
archiva y bloquea automáticamente hilos creados por la propia cuenta del bot.
