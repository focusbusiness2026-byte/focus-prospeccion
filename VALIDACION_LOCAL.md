# Evidencia de validacion local

Fecha: 10 de agosto de 2026

## Alcance comprobado

- Aplicacion FastAPI importada y compilada sin errores.
- Login local, cookie de sesion firmada y proteccion CSRF.
- Dashboard visual cargado en navegador de escritorio y movil.
- Resumen de cuota, semaforo, estados de lead e historial de ejecuciones.
- Ficha completa con evidencia, redes publicas, copia, aprobacion y descarte.
- Filtros y exportacion CSV por el alcance autorizado de la cuenta.
- Cuotas por correo y devolucion ante fallo implementadas sobre Google Sheets.
- Registro de ejecuciones, consumo de tokens y prospectos en la hoja.
- Bloqueo de destinos privados o reservados para evitar SSRF.
- Deteccion de LinkedIn, Instagram y otras redes enlazadas desde la web corporativa sin consultar perfiles privados.
- Respeto de `robots.txt`, limite de cuatro paginas y 2 MB por pagina.
- Gemini configurado para responder JSON y scoring Focus aplicado sobre la evidencia.
- Blueprint de un unico servicio web gratuito de Render, sin base de datos efimera.

## Pruebas automatizadas

```text
.venv\Scripts\python.exe -m compileall -q app
.venv\Scripts\python.exe -m pytest -q
```

Resultado:

```text
21 passed, 1 warning in 2.88s
```

La advertencia proviene de una anotacion interna del SDK `google-genai` y no de la aplicacion.

## Prueba visual

Se inicio el servidor en `127.0.0.1:8765`, se accedio mediante el boton de demostracion y se confirmo en el navegador:

- 8 ejecuciones disponibles de 10 en la cuenta demo.
- bolsa global y limite interno Gemini visibles;
- formulario de empresa y web corporativa;
- tarjetas, filtros, ficha completa e historial de resultados;
- tabla de ejecuciones, exportacion CSV y estados de lead;
- composicion adaptable sin desbordamiento horizontal a 390 x 844 px;
- metodologia de fuentes publicas.

En Google Sheets se verifico:

- `Prospeccion!W:X` con encabezados, formato, filtro y validacion de estado;
- `Dashboard Prospeccion!A14:E18` con formulas efectivas y valores sin errores;
- grafico de consumo reubicado debajo de las metricas para evitar solapamientos.

## Pendiente de integracion externa

- Configurar las credenciales privadas de Google y Gemini en Render.
- Ejecutar una investigacion real y verificar su escritura de extremo a extremo en `Prospeccion` y `Ejecuciones`.

El servicio publico responde correctamente, pero el 10 de agosto de 2026 `/health` informa `sheets_configured=false`, `google_login_configured=false` y `gemini_configured=false`; por eso el raspado real continua bloqueado hasta completar esas variables privadas.
