# Copia conectada de Prospección

La entrada `app.connected_review:app` usa el portal original. La actualización se carga dentro de Prospección; los leads y su historial se leen con el mismo control de cuenta del CRM.

## Configuración del servicio separado

- Iniciar con `uvicorn app.connected_review_entrypoint:app --host 0.0.0.0 --port $PORT` y comprobar `/health`. Activar con `REVIEW_ACTIVE=1` solo tras configurar las credenciales y el dominio de la revisión.
- Usar `APP_ENV=production`, `CENTRAL_AUTH_ENABLED=true`, el `CENTRAL_AUTH_URL` oficial y un `PUBLIC_BASE_URL` HTTPS propio bajo `focusbusinesslab.es`. La sesión central es compartida por el dominio. El acceso abre el inicio de sesión habitual en otra pestaña, porque el portal central solo admite sus destinos actuales.
- Configurar `GOOGLE_SHEETS_ENABLED=true` y el ID de la copia privada tanto en `GOOGLE_SHEET_ID` como en `REVIEW_GOOGLE_SHEET_ID`. El arranque comprueba ambos y rechaza el ID de producción.
- Dar a una cuenta de servicio acceso de edición únicamente a la copia. Cargar su JSON por el gestor de secretos del servicio como `GOOGLE_SERVICE_ACCOUNT_JSON`.
- Usar `REVIEW_DATA_DIR` para el SQLite local y un `DATABASE_URL` SQLite dedicado. Las respuestas de la guía se restauran de la pestaña oculta `Prospeccion Revision Estado` en la copia. Ejecutar una sola instancia.
- Inicialmente, mantener `REVIEW_ENABLE_SCHEDULER=0`. Las automatizaciones copiadas quedan como referencia. Cuando se autorice activar el motor, solo procesará programaciones creadas en la guía de revisión. Las búsquedas manuales y la IA requieren configurar `OPENAI_API_KEY` en este servicio.

## Verificación antes de entregar el acceso

1. Abrir el dominio sin sesión y comprobar que protege el portal y las APIs. Iniciar sesión con administración y con un cliente autorizado. Comprobar también una cuenta vinculada a la principal y el rechazo de otra empresa.
2. Comparar el panel con el original usando identificadores y recuentos. Confirmar formulario, historial, tablero y descargas por empresa sin mezclar datos.
3. Hacer una edición reversible en la copia y comprobar que cambia la celda correcta. Revisar que la hoja original conserva su valor.
4. Tras autorizar el consumo, validar una ejecución manual y, si procede, un lote finito. Confirmar que leads, registros y descuento solo figuran en la copia.
