# Comparación local: GPT investigador y Prospección

Fecha: 28 de octubre de 2026. Estado de esta revisión: cambios validados localmente; publicación y prueba real pendientes de comprobación.

## Prueba observable

Se abrió en Edge, con la sesión del usuario, el GPT «Investigador Empresas Focus Business» y se consultaron `https://nutraessential.com`, `https://farmalider.com/` y `https://www.cinfa.com/`. Las respuestas observadas separan identificación, actividad, tamaño, finanzas, señales recientes, decisores y veredicto. El GPT distinguió la sociedad Nutra Essential OTC extinguida de Farmalíder, su absorbente. El [BORME oficial](https://www.boe.es/diario_borme/txt.php?id=BORME-A-2025-16-28) respalda la extinción; las demás afirmaciones del GPT requieren contrastarse con sus fuentes antes de usarlas como hechos.

Las instrucciones privadas y los archivos de conocimiento del GPT no son visibles desde una conversación. Esta prueba permite comparar su **salida**, no copiar ni certificar su implementación interna.

## Diferencia técnica comprobada

Prospección usaba una sola respuesta con hasta cinco búsquedas web para encontrar hasta cinco empresas. La ficha del GPT dedica la investigación a una empresa. El esquema de Prospección ya tenía contactos, señales, decisores y fuentes, pero esa amplitud de búsqueda favorecía webs generales y podía aceptar cargos históricos o señales no citadas.

## Cambio local

1. La fase de descubrimiento pide situación legal, cita de esa situación y vigencia del cargo. Excluye sociedades inactivas, absorbidas o extinguidas y elimina decisores históricos/no verificados. Un contacto, señal o cargo sin URL presente en las fuentes de la respuesta no se incorpora. Se toleran diferencias inocuas de URL, incluidos parámetros de seguimiento.
2. La segunda fase investiga por separado cada candidata nueva, con hasta tres búsquedas adicionales por empresa y hasta cinco empresas. Excluye primero los duplicados existentes para no gastar llamadas en ellos. Contrasta la web pública de forma acotada, pide fuentes primarias y descarta candidatas no prospectables. Agrega fuentes y uso a la trazabilidad de la ejecución.
3. Cada lead nuevo conserva un dossier de siete apartados con hechos, fecha, fuente y grado de certeza. El portal lo muestra con formato editorial; los leads existentes permanecen sin modificaciones. La hoja añade únicamente una columna al final para el JSON del dossier. Se limita el tamaño de cada apartado para ajustarlo a una celda de Google Sheets.
4. `OPENAI_DEEP_RESEARCH_ENABLED=true` por defecto. Cada ejecución puede añadir hasta cinco solicitudes y quince búsquedas web de OpenAI además del descubrimiento. Esto puede elevar coste y latencia. Render ya tiene la clave configurada, pero esta revisión local no ejecutó el proveedor real.

## Límites de validación

La suite local pasó con 181 pruebas, compilación, comprobación de sintaxis JavaScript y `git diff --check`. El filtro comprueba que la URL figure entre las fuentes; no certifica por sí solo que la página sustente cada afirmación. Las pruebas simulan las respuestas del proveedor; **no prueban** que una búsqueda real produzca decisores, señales o finanzas comparables al GPT ni que cada dato citado sea correcto. No se ejecutó una búsqueda real de producción ni se consumió cuota del cliente durante esta validación.
