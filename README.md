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
- El disparador acepta una notificación servidor-a-servidor con solo el ID de Onboarding y, como respaldo, consulta periódicamente `Onboarding`; el sondeo puede desactivarse con `AUTO_RESEARCH_ENABLED=false`.
- Una ejecución de OpenAI Responses API usa `web_search` y `max_tool_calls` limitado localmente a 5.
- Solo se aceptan contactos, redes y señales con URL de evidencia. No se inicia sesión, evade CAPTCHA ni consulta contenido privado.
- Lo que no tiene evidencia se muestra como `No encontrado públicamente`, no como cero.
- La clave de OpenAI vive solo en el entorno del servidor. No existe input de clave ni se guarda en Sheets, UI, logs o Git.

## Dashboard y CRM

La vista inicial mantiene el resumen sencillo y separa un centro profesional de configuración. Cada productora recibe automáticamente una configuración recomendada construida con sus respuestas de Onboarding; el botón `Configuración recomendada` restaura esa base sin modificar las respuestas originales. Para una ejecución concreta se puede ajustar y guardar localmente un borrador con:

- cantidad objetivo de 1 a 50 leads verificables, sin superar 5 búsquedas web;
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

`Ejecuciones` usa `A:X`:

```text
execution_id, created_at, email, company, website, status, model, prompt_tokens,
output_tokens, total_tokens, error, onboarding_id, productora, web_search_calls,
web_search_call_limit, search_queries_json, research_sources_json,
no_prospect_reason, research_summary, search_configuration_json, adjustments_json,
research_provider, search_trace_json, duplicates_discarded
```

`Dashboard Prospeccion` conserva su composición visual existente. El portal calcula el resumen operativo en vivo y no sobrescribe el título, las fórmulas ni los gráficos de esa pestaña.

`ensure_operational_schema()` amplía exclusivamente las columnas de `Prospeccion` y `Ejecuciones`, añade encabezados finales faltantes y migra el encabezado heredado `gemini_model` a `model`; no reescribe filas de datos. La pestaña visual `Dashboard Prospeccion` queda intacta.

La deduplicación se aplica por dominio (o identidad normalizada disponible) dentro de cada `onboarding_id` antes de guardar en CRM. El número descartado queda en `Ejecuciones.duplicates_discarded`.

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
| `AUTO_RESEARCH_ENABLED` | Activa el disparador automático; por seguridad parte en `false`. |
| `AUTO_RESEARCH_POLL_SECONDS` | Intervalo de lectura de Onboarding, mínimo efectivo 30 s. |
| `PROSPECTION_TRIGGER_TOKEN` | Secreto servidor-a-servidor para despertar/procesar un nuevo ID de Onboarding. |

La interfaz muestra únicamente si OpenAI está configurado. La clave nunca se devuelve al navegador, no se guarda en Google Sheets y no debe aparecer en logs o Git.

## Activación pendiente de autorización

1. Verificar que la migración conservadora haya ampliado `Prospeccion` a 45 columnas y `Ejecuciones` a 24, manteniendo intacto el panel visual `Dashboard Prospeccion`.
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

La documentación de Focus Business propone seleccionar y limpiar primero los prospectos, luego generar reconocimiento mediante contenido, web, vídeo, publicidad y retargeting, y finalmente hacer contacto personalizado y seguimiento. La referencia 11-4-7 significa impactos de contenido en varios canales antes del contacto; no once mensajes automáticos. LinkedIn e Instagram deben operarse manualmente o con control estricto para evitar automatización agresiva. Esta aplicación no ejecuta calentamiento, mensajes ni integración con GoHighLevel.
