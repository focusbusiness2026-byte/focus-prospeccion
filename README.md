# Focus Prospección

Portal privado para convertir cada registro de Onboarding de una productora audiovisual en una configuración de prospección y, cuando el servidor dispone del secreto de OpenAI, investigar prospectos empresariales con evidencia pública.

## Flujo

```text
Formulario Onboarding
  -> Google Sheets / Onboarding
  -> configuración normalizada por productora
  -> ejecución automática (máximo 5 búsquedas web)
  -> Google Sheets / Prospeccion + Ejecuciones
  -> dashboard, filtros, detalle, CRM y CSV
```

- No hay captura manual de correo, productora o web en el dashboard.
- Sin `OPENAI_API_KEY`, el registro queda visible como `Pendiente de configurar OpenAI` y no se realiza ninguna búsqueda.
- Cada productora puede activar una automatización propia entre 5 minutos y 3 días. La programación y sus filtros se guardan en `Automatizaciones`; el motor puede desactivarse globalmente con `AUTO_RESEARCH_ENABLED=false`.
- Una ejecución de OpenAI Responses API usa `web_search` y `max_tool_calls` limitado localmente a 5.
- Las ejecuciones manuales del portal se inician como trabajos en segundo plano. La interfaz consulta su estado, muestra fases reales y actualiza Leads y CRM después de cada prospecto persistido; no inventa avances ni resultados durante la llamada al proveedor.
- Solo se aceptan contactos, redes y señales con URL de evidencia. No se inicia sesión, evade CAPTCHA ni consulta contenido privado.
- Lo que no tiene evidencia se muestra como `No encontrado públicamente`, no como cero.
- La clave de OpenAI vive solo en el entorno del servidor. No existe input de clave ni se guarda en Sheets, UI, logs o Git.

## Portal, dashboard y CRM

El portal usa una navegación superior, sin barra lateral permanente, y presenta cada módulo como una pantalla independiente: Inicio, Prospección, Leads y CRM, Ejecuciones, Metodología y Calentamiento. En móviles el menú se contrae en un botón.

Cada productora recibe automáticamente una configuración recomendada construida con sus respuestas de Onboarding; el botón `Configuración recomendada` restaura esa base sin modificar las respuestas originales. Los filtros avanzados se organizan por pasos para mantener una vista sencilla. La configuración puede guardarse con un nombre, marcarse como favorita y reutilizarse desde Inicio o Automatizaciones. Para una ejecución concreta se puede ajustar:

- una cantidad objetivo de hasta 5 resultados verificables y ajustados a los filtros;
- sectores prioritarios y excluidos, países, ciudad/región, tipo de cliente, tamaño, facturación y modelo empresarial;
- madurez comercial y digital, tecnologías y señales de oportunidad;
- decisores, empresas similares, presupuesto mínimo, preferencias y exclusiones avanzadas.

Los ajustes se guardan como trazabilidad de la ejecución, pero no sobrescriben el Onboarding. La ficha del lead muestra:

- productora e ID de Onboarding;
- consultas y contador de búsquedas;
- resumen, motivo de encaje y scoring;
- web, contactos públicos y redes sociales;
- fuentes y evidencia;
- señales de ingresos, valoración/financiación, publicidad, contratación pública u otras, con fecha, fuente, clase y confianza;
- CRM editable: estado, propietario, notas, próxima acción y fecha de seguimiento.

Los filtros cubren productora, país, sector, tipo de cliente, clasificación y estado. La descarga CSV aplica los filtros y contiene el detalle público y CRM disponible.

## Pestañas y columnas

`Prospeccion` usa `A:AS`:

```text
execution_id, email, created_at, company, website, title, description, sector,
business_model, city, employees, score, classification, summary, entry_angle,
linkedin, instagram, facebook, x, youtube, tiktok, evidence, lead_status, updated_at,
onboarding_id, productora, search_queries, web_search_calls, web_search_call_limit,
public_contacts_json, research_sources_json, no_contacts_reason, prospect_found,
no_prospect_reason, crm_owner, crm_notes, crm_next_action, crm_follow_up_date,
public_signals_json, public_signals_status, country, client_type, decision_makers_json,
warmup_preparation, warmup_approval
```

`Ejecuciones` usa `A:AA`:

```text
execution_id, created_at, email, company, website, status, model, prompt_tokens,
output_tokens, total_tokens, error, onboarding_id, productora, web_search_calls,
web_search_call_limit, search_queries_json, research_sources_json,
no_prospect_reason, research_summary, search_configuration_json, adjustments_json,
research_provider, search_trace_json, duplicates_discarded
actor_email, actor_role, execution_origin
```

`Automatizaciones` usa `A:N`:

```text
onboarding_id, email, enabled, interval_minutes, next_run_at, last_run_at,
last_status, updated_at, last_execution_id, adjustments_json
name, favorite, created_by_email, created_by_role
```

La automatización parte desactivada para cada productora. Al activarla, la primera investigación se agenda al finalizar el intervalo elegido y reutiliza los filtros guardados. El servidor fuerza un mínimo de 5 minutos y un máximo de 4320 minutos (3 días). El nombre es obligatorio y solo puede existir una favorita por cuenta.

La administración puede ejecutar las veces necesarias sin consumir la cuota individual del cliente. Este privilegio no elimina los límites técnicos por ejecución, el presupuesto global ni las salvaguardas del proveedor. El historial registra quién ejecutó y desde qué origen: la administración puede revisar actividad de cliente y administrativa, mientras la vista de cliente excluye las ejecuciones identificadas como administrativas.

`Dashboard Prospeccion` conserva su composición visual existente. El portal calcula el resumen operativo en vivo y no sobrescribe el título, las fórmulas ni los gráficos de esa pestaña.

`ensure_operational_schema()` amplía exclusivamente las columnas operativas de `Prospeccion`, `Ejecuciones` y `Automatizaciones`, añade encabezados finales faltantes y migra el encabezado heredado `gemini_model` a `model`; no reescribe filas de datos. La pestaña visual `Dashboard Prospeccion` queda intacta.

La deduplicación se aplica por dominio (o identidad normalizada disponible) dentro de cada `onboarding_id` antes de guardar en CRM. El número descartado queda en `Ejecuciones.duplicates_discarded`.

El botón de investigación utiliza `POST /api/onboarding-sources/{record_id}/research-jobs` y consulta `GET /api/research-jobs/{job_id}`. Solo la persona que inició el trabajo o una administración autorizada puede ver su estado. El servidor devuelve el trabajo activo si se pulsa de nuevo para la misma cuenta y fuente, evitando consumo duplicado accidental. El endpoint síncrono anterior se conserva para compatibilidad interna.

## Variables

| Variable | Uso |
|---|---|
| `GOOGLE_SHEETS_ENABLED` | Activa Google Sheets. |
| `GOOGLE_SHEET_ID` | ID de la hoja compartida. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Secreto del servidor con permiso de editor. |
| `GOOGLE_OAUTH_CLIENT_ID` | Inicio de sesión Google. |
| `OPENAI_API_KEY` | Secreto del servidor; nunca en cliente o Sheets. |
| `OPENAI_MODEL` | Modelo de Responses API; valor de ejemplo `gpt-5.5`. |
| `OPENAI_REQUEST_BUDGET` | Contador interno visible para administración. |
| `OPENAI_WEB_SEARCH_MAX_CALLS` | Se fuerza al intervalo 1–5. |
| `GOOGLE_AUTOMATION_TAB` | Pestaña de programaciones; valor recomendado `Automatizaciones`. |
| `AUTO_RESEARCH_ENABLED` | Activa el motor de programaciones. Ninguna productora se ejecuta hasta habilitar su programación. |
| `AUTO_RESEARCH_POLL_SECONDS` | Frecuencia interna con la que el servidor revisa programaciones vencidas; mínimo efectivo 60 s. |
| `PROSPECTION_TRIGGER_TOKEN` | Secreto servidor-a-servidor para despertar/procesar un nuevo ID de Onboarding. |

La interfaz muestra únicamente si OpenAI está configurado. La clave nunca se devuelve al navegador, no se guarda en Google Sheets y no debe aparecer en logs o Git.

## Keep-alive de Render preparado

`.github/workflows/keep-render-awake.yml` contiene un workflow programado cada 10 minutos y ejecutable manualmente. Solo realiza un `GET` sin credenciales a `https://focus-prospeccion-fb.onrender.com/health`; no llama al trigger de Onboarding, al scraping, a OpenAI ni a ninguna automatización de negocio.

En producción, la aplicación mantiene además un bucle interno explícito mediante `RENDER_KEEPALIVE_ENABLED=true`. Cada 10 minutos solicita exclusivamente su propio endpoint HTTPS `/health`. El intervalo se limita en código a 5-14 minutos, el destino no admite credenciales, consultas ni rutas distintas de `/health`, y el bucle se cancela de forma limpia al detener el proceso. El workflow de GitHub queda como respaldo independiente ante un reinicio.

La programación de GitHub Actions puede sufrir retrasos y cada ejecución consume recursos de Actions conforme al plan y las políticas vigentes de GitHub. Render puede aplicar límites o cambiar las condiciones de su plan gratuito; estos pings no sustituyen las garantías de un plan de pago.

## Activación pendiente de autorización

1. Verificar que la migración conservadora mantenga `Prospeccion` en 45 columnas, amplíe `Ejecuciones` a 27 y `Automatizaciones` a 14, manteniendo intacto el panel visual `Dashboard Prospeccion`.
2. Cargar `OPENAI_API_KEY` exclusivamente como secreto del servidor y conservar `OPENAI_WEB_SEARCH_MAX_CALLS=5`.
3. Configurar los secretos Google/trigger indicados en `.env.example`.
4. Publicar una nueva versión solo tras aprobación; entonces verificar primero una ejecución controlada.
5. GoHighLevel, mensajes y calentamiento 11-4-7 permanecen desactivados. Las futuras interacciones de LinkedIn/Instagram serán manuales o muy controladas.

## Desarrollo local

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
.\.venv\Scripts\python.exe -m pytest -q
```

El modo demo no usa OpenAI ni realiza investigación real.

## Calentamiento de lead: alcance futuro documentado

La documentación de Focus Business propone seleccionar y limpiar primero los prospectos, luego generar reconocimiento mediante contenido, web, vídeo, publicidad y retargeting, y finalmente hacer contacto personalizado y seguimiento. La referencia 11-4-7 significa impactos de contenido en varios canales antes del contacto; no once mensajes automáticos. LinkedIn e Instagram deben operarse manualmente o con control estricto para evitar automatización agresiva. El planificador actual organiza localmente lead aprobado, base legal, canal, fecha, responsable, nota y estado, pero no ejecuta calentamiento ni mensajes. GoHighLevel continúa sin conexión: una futura integración requerirá OAuth oficial, permisos mínimos, mapeo, prueba controlada y aprobación separada antes de activar Workflows, mensajería o funciones con coste.
