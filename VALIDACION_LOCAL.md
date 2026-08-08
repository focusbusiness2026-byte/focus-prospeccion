# Evidencia de validacion local

Fecha: 8 de agosto de 2026

## Alcance comprobado

- Aplicacion FastAPI importada y compilada sin errores.
- Login local, cookie de sesion firmada y proteccion CSRF.
- Dashboard visual cargado en navegador de escritorio.
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
16 passed, 1 warning in 5.26s
```

La advertencia proviene de una anotacion interna del SDK `google-genai` y no de la aplicacion.

## Prueba visual

Se inicio el servidor en `127.0.0.1:8766`, se accedio mediante el boton de demostracion y se confirmo en el navegador:

- 10 ejecuciones disponibles en la cuenta demo.
- bolsa global y limite interno Gemini visibles;
- formulario de empresa y web corporativa;
- historial de resultados;
- metodologia de fuentes publicas.

## Pendiente de integracion externa

- Configurar las credenciales privadas de Google y Gemini en Render.
- Desplegar despues de restaurar la cuenta de Render, actualmente suspendida por facturacion.
- Ejecutar una investigacion real y verificar su escritura de extremo a extremo en `Prospeccion` y `Ejecuciones`.
