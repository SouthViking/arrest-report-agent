# Configuración inicial del servidor

Después de [instalar e importar](SETUP.md), configura la guild mediante `/config`. Los cambios
funcionales se guardan en PostgreSQL; modificar su antigua variable de importación no cambia la
revisión activa. Las credenciales y switches globales permanecen en el entorno del proceso.

## 1 Obtener acceso administrativo

Pon tu ID de usuario en `CONFIG_OWNER_USER_IDS` y reinicia el proceso si acabas de cambiarlo.
Acepta varios IDs separados por comas, sin `*`. Estos owners pueden recuperar la administración
aunque la política de roles de configuración haya quedado mal configurada.

Para delegar, configura `config_admin_roles`, mostrado como **Administradores de configuración**.
Ese rol no sustituye al owner para acciones exclusivas, como cambiar el perfil global. Ser
administrador del servidor de Discord no sustituye por sí solo estas listas.

`CONFIG_OWNER_USER_IDS=111111111111111111,222222222222222222` concede acceso a ambos usuarios;
no contiene ninguna cuenta de creador implícita. Para administrar exclusivamente mediante esa
whitelist, deja `config_admin_roles` deshabilitada. Durante un traspaso añade primero al sucesor,
reinicia y comprueba su acceso; después retira al administrador anterior y vuelve a desplegar.

## 2 Crear un borrador

1. Ejecuta `/config` en el servidor que quieres administrar.
2. Abre **Borrador** y elige crear uno. El panel es privado y queda ligado a tu usuario.
3. Ajusta **Accesos**, **Agente**, **Módulos** y **Personas** según los pasos siguientes.
4. Comprueba los valores y pulsa **Publicar** cuando el conjunto esté listo.

Un borrador no cambia el comportamiento del bot. La publicación valida el conjunto y actualiza
la configuración sin desplegar código; las interacciones en curso conservan su snapshot.
Si el panel expira, vuelve a abrirlo; la configuración está en PostgreSQL, no en ese mensaje.

## 3 Configurar el acceso del agente

Define usuarios o roles permitidos antes de abrirlo a la facción:

| Política | Uso |
|---|---|
| `agent_users` | Usuarios con acceso completo al agente. |
| `agent_roles` | Roles con acceso completo al agente. |
| `agent_blocked_users` | Usuarios bloqueados; prevalece sobre cualquier permiso positivo. |
| `operational_staff_roles` | Roles de miembros operativos para los comandos que los requieren. |
| `supervisor_roles` | Acceso a los comandos de supervisión que usan esta política. |
| `report_analytics_roles` | Acceso independiente a las analíticas globales de reportes. |
| `config_admin_roles` | Roles que pueden administrar la configuración de esta guild. |

En `/config`, **Restringir** permite solo los IDs seleccionados, **Deshabilitar** no concede acceso
y **Permitir todos** abre el ámbito cuando la política lo admite. No todas las políticas permiten
estar abiertas; los módulos que necesitan destinos o roles concretos exigen listas explícitas.

`agent_users` y `agent_roles` se combinan como OR: dejar una abierta permite activar al agente
aunque la otra esté restringida. Para acceso solo por roles, deshabilita `agent_users` y restringe
`agent_roles`. No deduzcas los permisos de un comando por su nombre: consulta [Comandos](COMMANDS.md).

## 4 Configurar canales y permisos de Discord

`report_channels` define el archivo de reportes y dónde puede ejecutarse `/revisar-arresto`.
En el panel aparece como **Canales del agente**, pero **no** limita las conversaciones generales
por mención a esos canales. Configura IDs explícitos si vas a habilitar el acceso del propietario.

Configura por separado los destinos de bitácora (`logbook_channels`), revisión privada de roles,
pagers y notificaciones. No reutilices un canal público como destino de incidentes privados.
La visibilidad y los permisos efectivos de Discord se verifican además de las políticas del bot.

Revisa [Permisos](PERMISSIONS.md): enviar mensajes en el canal padre no garantiza poder enviar
adjuntos, crear hilos o escribir dentro de ellos. Para gestionar roles, el rol del bot debe estar
por encima de cada rol que deba asignar o retirar.

## 5 Elegir persona y modelo

Selecciona una revisión de persona válida y el modelo con su esfuerzo desde **Agente** y **Personas**.
Las combinaciones admitidas se resuelven desde [models.json](../src/arrest_report_agent/config/data/models.json).
No basta con escribir un nombre de modelo nuevo en el entorno si no figura en ese catálogo.

Cada guild puede tener persona propia, pero la cuenta de Discord comparte identidad global;
cambiarla afecta a la misma aplicación en sus servidores. La elección inicial de `BOT_PERSONA`
y `OPENAI_MODEL` solo se usa durante la importación. No la repitas para editar una instalación activa.

## 6 Habilitar solo los módulos que necesitas

Empieza con lo mínimo, configura sus dependencias y publica. Las opciones de módulos usan un número
de esquema y JSON validados; consulta [Comandos](COMMANDS.md) para los campos completos.
Los ejemplos siguientes usan IDs ficticios y `faction_id: 3` solo como ejemplo, no como valor universal.

### Aportes voluntarios, dedeo y enlaces de conocimiento

Estos módulos usan esquema `1`. Se editan en un borrador y requieren **Publicar**:

| Clave | Opciones JSON de ejemplo | Comportamiento al deshabilitar |
|---|---|---|
| `support` | `{"url":"https://buymeacoffee.com/tu-usuario"}` | `/support` y `/agradecimientos` responden con un aviso privado; ayuda y recursos no añaden enlaces ni botones de aportes. Si no hay ningún servidor con este módulo activo, no se registran nuevos aportes ni se normalizan los pendientes. |
| `dedeo` | `{"protected_user_ids":[111111111111111111]}` | `/dedear` queda inactivo. Una lista `[]` permite seleccionar a cualquier usuario elegible; no hay un creador protegido implícito ni acceso administrativo asociado. |
| `knowledge_links` | `{"procedures_manual_url":"https://example.org/manual","penal_code_url":"https://example.org/codigo-penal"}` | Guía, FAQ y búsqueda de cargos mantienen el contenido local sin enlaces externos. Cada URL también admite `null` para omitirla. |
| `teamspeak` | `{"address":"voice.example.org:9987"}` | `/ts3` informa que no está configurado; recursos no publica una dirección predeterminada de otra instalación. |
| `release_routing` | `{"internal_channel_ids":[333333333333333333]}` | Los releases con audiencia `internal` no tienen destinos. No altera las alertas de incidentes. |

Las URLs de aportes y conocimiento deben ser HTTPS y no contener usuario ni contraseña.
`protected_user_ids` e `internal_channel_ids` aceptan como máximo 25 IDs positivos distintos.
Los canales internos son una clasificación explícita del operador: siempre se intersectan con
la política de canales de despliegues; esa lista no concede acceso a canales por sí sola.

El módulo `support` no sustituye el secreto del webhook. Para recibir eventos de una cuenta propia
configura también `BUY_ME_A_COFFEE_WEBHOOK_SECRET` en el proceso. Quitar ese secreto y redesplegar
elimina el receptor HTTP; los enlaces visibles dependen del módulo, no de la existencia del secreto.
Cambiar la URL no traslada dinero ni reinicia el historial de aportes. Los mensajes ya publicados
conservan sus enlaces; decide por separado cómo tratar los registros históricos al cambiar de cuenta.

Cambiar `knowledge_links` no sustituye el contenido del manual indexado: para cambiarlo usa
`MANUAL_CATALOG_PATH` y conserva la procedencia correcta en ese catálogo. La identidad y voz de Roy
o Walter se editan en **Personas**; el banner global se configura con `PROFILE_BANNER_PATH`.

### Bitácora

Restringe `logbook_channels` a los canales elegidos y habilita `logbook`, esquema `1`, opciones `{}`.
Comprueba `/guia-bitacora` y publica una entrada de prueba autorizada con `/bitacora`.

### Revisión de arrestos desde PCU

1. Configura `SARP_API_ENABLED=true` y `SARP_API_TOKEN` en el proceso y reinícialo.
2. Restringe `report_channels` y `operational_staff_roles` a tus canales y roles reales.
3. Habilita `report_owner_access`, esquema `1`, por ejemplo `{"max_rounds":5}`.
4. Habilita `arrest_report_lookup`, esquema `1`, con `{"faction_id":3}`.
5. Publica y comprueba `/info-reportes` y `/revisar-arresto` con un ID existente.

El modo de imagen es `generated` por defecto: el reporte se genera desde PCU. Las fotos del
sospechoso siguen siendo obligatorias. Si necesitas adjuntar también la imagen del reporte manualmente,
usa `"report_attachment_mode":"manual"` dentro del mismo módulo.

El módulo `stolen_weapons` habilita `/registrar-robo-arma`, `/cerrar-reporte-robo` y la consulta de
seriales del agente. Configura
`{"source_channel_ids":[1421923642983317545],"destination_channel_id":1565184787591860345}`
desde **Configurar módulo → Registro de armas robadas**. Requiere la política
`operational_staff_roles` restringida y el bucket ya usado por los adjuntos; no añade variables de
entorno. El bot necesita ver el destino, enviar mensajes, insertar enlaces y adjuntar archivos.
Los IDs anteriores se cargan inicialmente para LSSD, preservando configuraciones existentes y
habilitando el módulo solo si ya hay una política restringida de roles operativos. No se habilita
en otros servidores.
`notifications_enabled` controla las alertas por coincidencias abiertas durante una revisión y
vale `true` si se omite. Las alertas se publican en `destination_channel_id`; el backend fija su
plantilla y evita duplicados por registro y revisión. Desactivar las alertas no desactiva la
consulta ni convierte sus resultados informativos en requisitos para aprobar.

Las órdenes abiertas del sospechoso bloquean la aprobación completa hasta verificar su cierre.
La excepción de Command/Executive Staff requiere una instrucción explícita actual y un ID de rol
incluido en `approval_override_role_ids` dentro de `arrest_report_lookup`. Esa misma lista habilita
`/aprobar-arresto`, que abre un formulario de motivo para aprobar manualmente dentro del hilo. La migración
configura los dos roles actuales de LSSD y conserva una lista explícita, incluso si está vacía.
Las aprobaciones manuales registran quién autorizó y por qué; no cierran órdenes en SARP.
Aunque desactives sus avisos, se mantiene esa comprobación. Los avisos a supervisión son opcionales:

```json
{
  "faction_id": 3,
  "warrant_alerts_enabled": true,
  "warrant_alert_channel_ids": [555555555555555555],
  "warrant_alert_role_id": 666666666666666666
}
```

Sustituye los IDs y verifica que el bot pueda ver y publicar en el destino y mencionar ese rol.
`/health` no comprueba que el token SARP tenga permiso para todas las consultas necesarias.

### Solicitudes de roles

Configura roles revisores explícitos en `role_request_reviewer_roles`, destinos privados en
`role_request_review_channels`, el catálogo `role_request_allowed_roles` y los canales de entrada
`role_request_channels`. Habilita `role_requests`, esquema `1`, con las opciones necesarias para
tu flujo. No incluyas roles revisores en el catálogo solicitables ni concedas roles de administración
por conveniencia. El flujo de ingreso FTT, estaciones y turnos se configura por guild.

En `role_requests.ftt_entry.welcome`, `channel_id` es el destino de la bienvenida, `rules_url` el
post del FTP y `reference_channel_ids` los canales de lectura recomendada. Los campos opcionales
`information_channel_id` y `questions_channel_id` añaden el canal de novedades del FTP y el de
consultas. Si se omiten, no se imprimen menciones vacías. El instructor no es una opción fija de
configuración: se elige, opcionalmente, al aprobar cada ingreso FTT. No modifica sus roles.
La migración de LSSD completa los nuevos canales en borradores y revisiones publicadas, conserva
los valores personalizados y no modifica el historial archivado. No hay variables de entorno nuevas.

### Avisos de fallos de revisión

Para notificar cuando se agotan las correcciones automáticas de argumentos, habilita
`review_failure_notifications`, esquema `1`, con `{"role_ids":[],"user_ids":[]}` y completa solo
los destinatarios deseados. Una lista vacía no menciona a nadie. Este aviso es independiente de
`review_escalation_roles`, usado para derivaciones inconclusas.

### Otros módulos

Pagers, blacklists, FTP, pendientes y feeds tienen políticas y dependencias propias. Configura sus
roles y destinos antes de habilitarlos. No copies opciones de LSSD suponiendo que los mismos IDs
existen en otra guild. Los detalles están en [Comandos](COMMANDS.md) y [Referencia funcional](FEATURES.md).

## 7 Publicar y comprobar

Publica el borrador y confirma la revisión activa con **Actualizar**. Prueba con un usuario permitido
y uno sin acceso, además de comprobar la visibilidad pública o privada del resultado. Un rol
mencionado para avisos no recibe por ello permisos adicionales para decidir solicitudes.

Si la publicación rechaza una combinación, corrige el campo o la dependencia indicada. No edites
filas directamente para saltarte la validación. Para volver a una configuración válida, usa
**Rollback**; para recuperar el acceso del administrador, cambia `CONFIG_OWNER_USER_IDS` y reinicia.
El rollback de configuración no revierte código, migraciones ni mensajes ya enviados.
