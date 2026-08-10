# Evidencia de validacion local

Fecha: 11 de agosto de 2026

## Alcance comprobado

- Aplicacion FastAPI importada y compilada sin errores.
- Login local, cookie de sesion firmada y proteccion CSRF.
- Portal visual con navegación superior y módulos independientes cargado en navegador de escritorio y móvil.
- Centro profesional de configuración por pasos con base recomendada desde Onboarding, cantidad objetivo y filtros múltiples.
- Automatización individual por productora, persistida en Sheets, con intervalo validado entre 5 minutos y 3 días.
- Resumen de cuota, semaforo, estados de lead e historial de ejecuciones.
- Ficha completa con evidencia, redes publicas, copia, aprobacion y descarte.
- Filtros y exportacion CSV por el alcance autorizado de la cuenta.
- Cuotas por correo y devolucion ante fallo implementadas sobre Google Sheets.
- Registro de ejecuciones, consumo de tokens y prospectos en la hoja.
- Bloqueo de destinos privados o reservados para evitar SSRF.
- Deteccion de LinkedIn, Instagram y otras redes enlazadas desde la web corporativa sin consultar perfiles privados.
- Respeto de `robots.txt`, limite de cuatro paginas y 2 MB por pagina.
- OpenAI Responses API validada con respuestas simuladas; no se realizó ninguna llamada real.
- Blueprint de un unico servicio web gratuito de Render, sin base de datos efimera.

## Pruebas automatizadas

```text
.venv\Scripts\python.exe -m compileall -q app
.venv\Scripts\python.exe -m pytest -q
```

Resultado:

```text
39 passed
```

También se ejecutó `compileall` y `git diff --check` sin errores.

## Prueba visual

Se inicio el servidor en `127.0.0.1:8767`, se accedio mediante el boton de demostracion y se confirmo en el navegador:

- 8 ejecuciones disponibles de 10 en la cuenta demo.
- bolsa global y límite interno del proveedor visibles;
- productoras y web tomadas exclusivamente desde Onboarding, sin formulario manual de empresa;
- configuración recomendada restaurable, borrador local, cantidad objetivo de 1 a 50 y filtros avanzados de firmografía, madurez, señales, decisores y exclusiones;
- tarjetas, filtros, ficha completa e historial de resultados;
- tabla de ejecuciones, exportacion CSV y estados de lead;
- composicion adaptable sin desbordamiento horizontal a 390 x 844 px;
- metodologia de fuentes publicas.

La hoja externa no se modificó en esta validación. El esquema local preparado es:

- `Prospeccion!A:AS` (45 columnas), incluidos CRM y preparación/aprobación del calentamiento futuro;
- `Ejecuciones!A:X` (24 columnas), incluido proveedor, trazabilidad por consulta y duplicados;
- `Automatizaciones!A:J` (10 columnas), con intervalo, proxima ejecucion, estado y filtros reutilizables;
- `Dashboard Prospeccion` se preserva como panel visual; el portal calcula sus indicadores en vivo sin sobrescribir esa pestaña.

## Pendiente de integracion externa

- Configurar las credenciales privadas de Google y `OPENAI_API_KEY` en el entorno del servidor.
- Ejecutar una investigacion real y verificar su escritura de extremo a extremo en `Prospeccion` y `Ejecuciones`.

La comprobación del servicio público descrita arriba es histórica y no valida estos cambios locales. Esta entrega no se desplegó ni ejecutó búsquedas reales.

## Validación local de la ampliación

- 39 pruebas locales superadas, incluida una prueba integral simulada Onboarding → investigación → deduplicación → persistencia/trazabilidad y pruebas del nuevo programador.
- La cantidad objetivo limita el número de prospectos aceptados por ejecución y se valida entre 1 y 50.
- OpenAI simulado: límite de 5 llamadas, contactos y señales con fuente, sin clave en el payload.
- Persistencia ampliada para configuración, consultas, fuentes, señales y CRM.
- Sin despliegue, escritura real en Sheets ni llamada real a OpenAI.
