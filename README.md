# Focus Prospeccion

Portal privado de Focus Business para investigar empresas a partir de su web corporativa, descubrir los perfiles sociales que la propia empresa enlaza y estructurar la evidencia con Gemini.

## Que incluye

- Inicio de sesion con Google y autorizacion contra la pestana `Accesos` de Google Sheets.
- Una cuota individual de ejecuciones por correo, reservada antes de cada investigacion y devuelta cuando falla tecnicamente.
- Consulta de paginas HTML publicas de la web indicada, con validacion SSRF, limite de tamano y respeto de `robots.txt`.
- Deteccion de enlaces publicos a LinkedIn, Instagram, Facebook, X, YouTube y TikTok desde la web corporativa. No inicia sesion, no evade CAPTCHA y no extrae zonas privadas de esas redes.
- Analisis estructurado, scoring Focus y consumo de tokens con Gemini.
- Historial en las pestanas `Prospeccion` y `Ejecuciones`.
- Dashboard web con cuotas, semaforo, estado de leads, actividad y vista global para administradores.
- Ficha completa de cada lead con evidencia, redes detectadas, copia independiente, aprobacion o descarte.
- Filtros por empresa, clasificacion y estado, mas exportacion CSV de los resultados visibles para la cuenta.
- Dashboard de control en la hoja, con estados verde, amarillo y rojo y conteos de leads y ejecuciones.
- Despliegue en un unico servicio web gratuito de Render mediante `render.yaml`.

## Arquitectura

El servicio no depende de una base de datos local: Google Sheets es el registro persistente de accesos, cuotas, ejecuciones y resultados. Esto evita perder datos cuando un servicio gratuito de Render se reinicia o entra en reposo.

```text
Google Sign-In -> Accesos (Sheets) -> cuota -> web publica -> Gemini
                                      |                       |
                                      +-> Ejecuciones <-------+
                                      +-> Prospeccion -> dashboard
```

La pestana `Prospeccion` usa 24 columnas (`A:X`). `lead_status` admite `Nuevo`, `Aprobado` o `Descartado`; `updated_at` registra el ultimo cambio. Los usuarios cliente solo reciben sus propios resultados. Un rol cuyo nombre contiene `admin` recibe el resumen y los resultados globales.

## Desarrollo local

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

En local, `DEMO_AUTH_BYPASS=true` permite revisar la interfaz sin credenciales. La ejecucion real permanece bloqueada hasta configurar Google Sheets y Gemini.

Pruebas:

```powershell
pytest -q
```

## Variables de Render

| Variable | Funcion |
|---|---|
| `APP_SECRET` | Firma las sesiones; Render la genera automaticamente. |
| `PUBLIC_BASE_URL` | URL publica final, por ejemplo `https://focus-prospeccion.onrender.com`. |
| `GOOGLE_OAUTH_CLIENT_ID` | ID de cliente web de Google Identity Services. |
| `GOOGLE_SHEET_ID` | ID de la hoja Focus Business. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON completo de una cuenta de servicio con permiso de editor sobre la hoja. |
| `GEMINI_API_KEY` | Clave de Gemini API; nunca se guarda en GitHub. |
| `GEMINI_MODEL` | Modelo configurable; valor inicial `gemini-3.5-flash`. |
| `GEMINI_REQUEST_BUDGET` | Tope interno visible en el dashboard; no sustituye los limites reales de Google AI Studio. |
| `WEB_SCRAPER_MAX_PAGES` | Maximo de paginas publicas consultadas por ejecucion. |

`GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_OAUTH_CLIENT_ID`, `GEMINI_API_KEY` y la URL publica se solicitan como valores privados al crear el servicio.

## Preparar Google

1. Activar Google Sheets API en un proyecto de Google Cloud.
2. Crear una cuenta de servicio y compartir la hoja con su `client_email` como editor.
3. Crear un cliente OAuth 2.0 de tipo aplicacion web para Google Sign-In.
4. Añadir la URL de Render en `Origenes de JavaScript autorizados`.
5. Pegar el ID OAuth y el JSON de servicio unicamente en las variables privadas de Render.

## Limites conscientes

- El contador de la aplicacion es un limite interno por solicitudes; los limites gratuitos reales de Gemini dependen del modelo y del proyecto y deben confirmarse en Google AI Studio.
- Las plataformas sociales restringen la extraccion automatizada. El portal registra los perfiles que aparecen enlazados en la web corporativa y analiza solo evidencia publica permitida.
- El plan gratuito de Render puede poner el servicio en reposo tras inactividad; la primera visita posterior puede tardar mas.
