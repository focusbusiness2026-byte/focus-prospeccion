# Campañas de email con marca y LinkedIn asistido

Estado 2026-11-08: implementación local en la rama de sandbox. No desplegada. Ningún proveedor se habilita por crear una campaña.

## Ejecutar la revisión sin proveedores

Usar el entry point `app.sandbox:app`, `FOCUS_SANDBOX=1` y una carpeta nueva y exclusiva mediante `SANDBOX_DATA_DIR`. No usar una base de producción ni importar su `.env`. No activar `SANDBOX_REAL_MODE`, `OUTREACH_AI_ENABLED` ni `OUTREACH_LIVE_EMAIL_ENABLED` para esta revisión. El sandbox rechaza credenciales convencionales de producción.

La revisión actual está en `http://127.0.0.1:8028/campaigns/outreach`. La biblioteca y destinatarios son ejemplos explícitos; las cuentas reales y la copia publicada no se han modificado. La campaña local queda pausada.

## Persistencia y conservación

Las tablas `outreach_*` son aditivas en el almacén durable de campañas. Se reutiliza su transacción y snapshot/CAS existente. No se sustituyen las tablas anteriores, se escribe en Sheets ni se consume cuota de investigación al redactar. Los identificadores y decisiones del tablero no se reescriben.

La nueva API identifica la cuenta por parámetro y comprueba su lista de usuarios autorizados. La copia real continúa limitada a Three Dots; ampliar la carga de cuentas canónicas en producción requiere su propia migración y aceptación. Los dos tenants de las pruebas son ficticios.

## Reutilizar una secuencia de cinco correos

La preparación del borrador y sus vistas previas se ejecuta en Prospección. GoHighLevel solo recibe datos de una campaña aprobada y confirmada cuando los envíos están habilitados. La activación reutiliza un flujo ya preparado, escribe asunto, preencabezado y cuerpo en los campos propios de cada contacto y solicita su incorporación. El registro de operaciones y las reservas existentes impiden repetir una activación incierta sin conciliación.

Para el piloto, una persona administradora puede instalar una sola vez un [Snapshot de HighLevel](https://help.gohighlevel.com/support/solutions/articles/48000982582-load-snapshots-into-existing-account) que incluya el flujo, cinco plantillas y los campos de contacto. El comprobador de solo lectura busca `Focus · Prospección · 5 correos`, las plantillas `Focus · Prospección · Correo 01` a `05`, los campos `contact.fi_email_01_subject`, `contact.fi_email_01_preheader` y `contact.fi_email_01_body` a `05`, y `contact.fi_next_step`. Rechaza la ambigüedad e indica el estado, coincidencia de remitente y campos encontrados. La consulta de definición puede estar restringida aunque el flujo figure en la lista.

La consulta requiere una conexión de la subcuenta y la opción de servidor `GHL_ASSET_CHECK_ENABLED=1`, con los permisos OAuth de lectura de flujos, plantillas y campos. No requiere habilitar envíos. La comprobación no vincula ni publica recursos. La vinculación, la verificación del dominio y de las acciones de cada correo y las reglas de baja siguen siendo pasos administrativos iniciales. La [API de creación de workflows](https://marketplace.gohighlevel.com/docs/ghl/workflows/create-workflow/index.html) está limitada por subcuenta y crea borradores. El portal no crea ni publica un flujo al abrir la cuenta o al preparar, aprobar o iniciar una campaña.

## Preparación administrativa posterior, con autorización específica

1. El titular debe confirmar la rotación/revocación de la clave expuesta en una inspección anterior. No introducir secretos en formularios, Git, Sheets ni informes.
2. Configurar marca, logo HTTPS público, remitente/dominio, firma, agenda de esa empresa y referencia de procedencia/autorización. Un logo SVG local solo sirve para la vista previa; para el correo real usar un formato compatible probado en Gmail/Outlook.
3. Registrar una aplicación GHL y los permisos oficiales de la subcuenta de pruebas. La configuración de servidor usa `GHL_CLIENT_ID`, `GHL_CLIENT_SECRET`, `OUTREACH_ENCRYPTION_KEY` (32 bytes codificados en base64), `GHL_WEBHOOK_PUBLIC_KEY` y `GHL_OUTREACH_FIELD_IDS`. Ninguno contiene valores en este repositorio. Los tokens se cifran con AES-GCM y AAD por empresa. Una renovación incierta queda bloqueada para reconexión administrativa.
4. `GHL_OUTREACH_FIELD_IDS` es un mapa por tenant. Cada `fi_email_01_subject`, `fi_email_01_preheader`, `fi_email_01_body` hasta el paso 15, y `fi_next_step`, apunta a un objeto con `id` y, para las plantillas, `key` de GHL verificados por el administrador. Los merge keys deben pertenecer a `contact.*` y al espacio propio de la integración. No reutilizar campos ajenos.
5. Con autorización de pruebas, habilitar la conexión GHL en servidor. Conectar mediante `/api/intelligence/{tenant}/outreach/ghl/connect`. El callback valida estado de un uso, sesión, caducidad y subcuenta. La provisión administrativa de plantillas usa POST `/api/intelligence/{tenant}/outreach/ghl/templates/{version}`; no se ejecuta en esta revisión. Guarda IDs parciales y reclama la operación antes de salir a red. Un resultado incierto requiere conciliación, nunca repetir por sistema.
6. Preparar un workflow **nuevo y exclusivo** de la integración. El adaptador permite consultar tipos de pasos, validar una definición y crearla donde la API esté habilitada; un 403 exige preparación administrativa. La versión inicial también admite crear el workflow manualmente, sin tocar los existentes. Verificar las 15 plantillas y su asociación correcta a los 45 campos, el remitente, baja y agenda. No publicar un workflow incompleto.
7. El workflow debe programar 10:00 Europe/Madrid, los siete días, avanzar por fechas naturales, detenerse por respuesta/baja/oposición/rebote permanente/reunión y actualizar `fi_next_step` **después de cada envío confirmado**. El punto de entrada debe saltar pasos anteriores según este campo. Al terminar, marcar 16. Pausa/reanudación sin este contrato debe fallar y requerir revisión; no se presupone que eliminar/reincorporar a cualquier workflow conserve su posición.
8. Verificar los eventos reales y su firma Ed25519. Los envíos/entregas solo se atribuyen cuando traen el workflow correcto: un evento sin atribución se ignora, no se cuenta como éxito. Una apertura no cuenta como respuesta. La conciliación de eventos GHL que no incluyan esta atribución sigue pendiente; no se prometen métricas completas sin validar el payload real.
9. Verificar por separado las reglas de baja del proveedor y sus webhooks de rebote permanente. Un fallo transitorio no se clasifica automáticamente como rebote permanente. Las cancelaciones ambiguas quedan `stop_pending` y se revisan, sin reintento automático.
10. La generación real requiere modo real, `OUTREACH_AI_ENABLED=1` y autorización de la credencial: `OUTREACH_KEY_ROTATION_CONFIRMED=1` tras una rotación confirmada, o `OUTREACH_EXISTING_KEY_REUSE_AUTHORIZED=1` si el titular autoriza expresamente reutilizar la credencial privada configurada. Esta segunda opción no confirma ni modifica una rotación. El 10-11-2026 el titular autorizó esta reutilización exclusivamente en la copia privada para revisar los 15 correos de Three Dots. No habilita GHL: mantener `OUTREACH_LIVE_EMAIL_ENABLED=0`. La primera biblioteca y cada adaptación se solicitan a propósito, se versionan y registran uso/modelo/fuentes. No se vuelve a investigar a cada destinatario.

## Operación del cliente

Seleccionar aprobados → elegir destinatario/canal → preparar → revisar los 15 textos por destinatario y las fechas → aprobar lote → confirmar activación. Cambiar marca, textos o calendario invalida la aprobación. El contenido activo se conserva; duplicarlo genera un borrador y no permite dos campañas activas para la misma identidad.

Los emails dependen del reloj de GHL una vez confirmado el enrolamiento, no del navegador ni de la laptop. Esto es el diseño y el contrato del conector; todavía no se ha verificado un envío real con el ordenador apagado. LinkedIn siempre requiere la acción manual: invitación, conexión aceptada, mensaje y seguimiento tres días después si procede.

## Validación

`python -m pytest -q`: 245 correctas, incluidas 38 nuevas. Transportes externos simulados: biblioteca/caché, selección, aprobación, conservación, restauración, aislamiento API/CSRF, DST/calendario, los 15 eventos del proveedor, pausa/reanudación, timeout incierto, cifrado/renovación y firmas. Sintaxis JS, compilación Python y `git diff --check` correctos.

Onboarding: Node 24.19.0, build y lint correctos; 49 pruebas pasan y una se omite. Incluye arranque frío antes de emitir el acceso firmado. No se migró Apps Script ni la hoja porque no hay cambios de encabezados o lógica de alta en este alcance.

Pendientes de aceptación real: calidad de los 15 textos generados con IA, recepción en Gmail/Outlook, entregabilidad y remitente, OAuth/subcuenta de pruebas, workflow publicado, atribución y parada por eventos reales, pausa/reanudación real, dos empresas reales y continuidad sin navegador. Publicación en copia privada y producción requieren autorizaciones separadas.
