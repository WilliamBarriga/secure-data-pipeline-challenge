# cauce: plataforma de datos de consumo

Toma archivos crudos de consumo, maestros y facturación de clientes no regulados en Colombia y los convierte en información confiable y bien custodiada.

## Proyecto

**Brief**:
Especificación del problema en `docs/brief.md`: entradas, reglas B1-B7, controles C1-C8, contrato de ejecución y escenarios de aceptación. Es la fuente de verdad de mayor precedencia; "brief §N" apunta a su sección N.
_Avoid_: consigna, requerimientos

**Operador**:
Persona que corre el sistema con el contrato `make` (brief §9) y verifica sus resultados: lee la base como superusuario, prueba cada rol contra lo que tiene prohibido y revisa `out/` y `logs/`. No confundir con el operador de red, que es un atributo de la Frontera.
_Avoid_: usuario, cliente (del sistema)

**Datos sintéticos**:
Entrada inventada que genera `make datos` (`tools/sinteticos.py`) en `data/`: misma forma y mismas categorías de defecto que la entrada real (brief §2.4), sin ninguno de sus valores. Misma semilla, mismos bytes. Los correos usan dominios reservados y los celulares un rango no asignado.
_Avoid_: dataset, mock, datos de prueba, datos falsos

**Verdad plantada**:
Manifiesto que escribe el generador de Datos sintéticos con los defectos que sembró (Duplicados, Huecos, Facturas huérfanas, Alertas esperadas…) y los conteos que el pipeline debe reportar. Los e2e contrastan contra él en lugar de contra cifras fijas. Nunca se escribe dentro de `data/`.
_Avoid_: oráculo fijo, expected, golden

## Reglas de calidad (brief §4)

**B1, Hora local**: todo instante se expresa en Hora local; un archivo en UTC se convierte antes de asignar hora y mes.

**B2, kWh**: toda cifra de consumo sale en kWh; una fila en MWh se multiplica por 1000 antes de cualquier otra regla.

**B3, Ganadora por hora**: entre Lecturas de la misma Frontera y hora gana la de `version_lectura` mayor; ante igual versión, la última en orden de archivo. Las sobrantes son Duplicado exacto, Duplicado conflictivo o Corrección tardía.

**B4, Inválidos**: se descarta sin imputar la Lectura vacía o nula, la negativa y la que supera 1.000.000 kWh, contando cada causa.

**B5, Huecos**: cada Hora local de una Frontera sin Lectura válida es un Hueco; se cuenta y nunca se rellena.

**B6, Facturas huérfanas**: una Factura huérfana no entra al modelo; queda en Cuarentena y se cuenta.

**B7, Entrada hostil**: el maestro de Clientes es dato, nunca código; cada fila termina cargada o en Cuarentena.

## Controles (brief §5)

**C1, Clasificación**: cada columna del modelo tiene un Nivel de clasificación en `data_classification.yaml`; una columna sin nivel es un error.

**C2, PII en texto libre**: la PII en texto libre de `observaciones` se Enmascara antes de persistir y se cuentan los Clientes afectados.

**C3, Cifrado en reposo**: ninguna columna `Restringido-PII` existe en claro en Postgres, ni siquiera para el superusuario.

**C4, Menor privilegio**: los roles `ingesta`, `analista` y `auditor` tienen solo el permiso efectivo mínimo.

**C5, Trazabilidad**: la Bitácora registra cada Corrida y es de solo inserción por revocación en la base.

**C6, Secretos**: el repositorio no contiene credenciales y el secreto heredado (`config/pipeline.yaml`) no se lee.

**C7, Dependencias**: las dependencias se auditan contra vulnerabilidades conocidas y las afectadas se actualizan o se reemplazan.

**C8, Modelo de amenazas**: `THREAT_MODEL.md` aplica STRIDE al sistema construido, con control y referencia al Anexo A de ISO/IEC 27001:2022 por amenaza.

## Medición

**Frontera**:
Punto de medición comercial de energía, identificado por `frontera_id` (`CO##########`) y por un código SIC (`frtXXXXX`). Pertenece a un Cliente.
_Avoid_: medidor, punto, sitio

**Lectura**:
Valor de consumo en kWh de una Frontera para una hora local, con una versión (`version_lectura`).
_Avoid_: registro, medición (en batch), dato

**Medición**:
Evento del stream con el consumo de una Frontera en un intervalo de 15 minutos.
_Avoid_: lectura (en stream), evento

**Hora local**:
Hora en `America/Bogota` (UTC−5 fijo). Define el mes y la hora de toda Lectura.
_Avoid_: hora, timestamp sin zona

**Corrección tardía**:
Lectura válida con versión mayor que desplaza a otra Lectura válida de la misma Frontera y hora presente en algún archivo aceptado, sin importar cuál llegó primero. Si no había Lectura válida que desplazar, o si la de versión mayor es inválida, no hay Corrección tardía.
_Avoid_: relectura, ajuste, update

**Hueco**:
Hora local de una Frontera sin ninguna Lectura válida, exista o no una fila en el archivo.
_Avoid_: gap, faltante, nulo

**Foto**:
Salida calculada entera desde el estado de la base (todos los archivos presentes), no acumulada corrida a corrida (delta). Dos Corridas sobre la misma base dan los mismos bytes; `dq_report.json` y `consumo_mensual.csv` son Fotos.

**Banda operativa**:
Rango `[min_kwh, max_kwh]` esperado para las Mediciones de una Frontera.
_Avoid_: límite, umbral

**Rejilla**:
Los 96 intervalos de 15 minutos de un día en Hora local sobre los que se evalúa el stream.
_Avoid_: grilla, slots

**Caída**:
Racha máxima de 3 o más intervalos consecutivos de la Rejilla sin Medición válida de una Frontera, juzgada sobre todo lo recibido. Genera una sola Alerta, en el tercer intervalo ausente.
_Avoid_: hueco (en stream), desconexión

**Marca de agua**:
Intervalo más alto de la Rejilla con Medición válida recibida en el stream, sobre todas las Fronteras. Con un margen, permite confirmar una Caída antes del cierre; sin margen, las Caídas se juzgan solo al cierre. No confundir con el high watermark de Kafka (offset final de cada partición al empezar a leer), que marca dónde termina la lectura del stream y siempre se nombra así, completo.
_Avoid_: watermark, reloj

**Medición retroactiva**:
Medición cuyo instante es estrictamente anterior al de la última ya procesada para su Frontera. Un instante igual es un duplicado, no una Medición retroactiva: se cuenta como duplicado y no pasa por las reglas.
_Avoid_: medición tardía, corrección (en stream)

**Alerta**:
Aviso emitido por el stream sobre una Frontera (Caída, consumo fuera de la Banda operativa o Medición retroactiva), identificado por tipo, Frontera e instante. Una vez emitida no se retira.
_Avoid_: evento, notificación

## Calidad

**Cuarentena**:
Zona consultable donde queda todo lo rechazado, con su motivo. Nada rechazado se borra.
_Avoid_: descarte, papelera, errores

**Archivo corrupto**:
Archivo de entrada cuya estructura no se puede leer con confianza (no decodifica, encabezado desconocido, filas con número de campos distinto, comilla sin cerrar, fila cortada o último registro sin salto de línea final). Se rechaza entero y ninguna de sus filas se carga. Distinto de una fila inválida, que va sola a Cuarentena.
_Avoid_: archivo malo, archivo dañado

**Archivo modificado**:
Archivo de consumos o facturación que llega con un nombre ya cargado pero con contenido distinto. Se rechaza entero: lo ya cargado no cambia. Los maestros no tienen Archivo modificado; una versión nueva del maestro lo reemplaza.
_Avoid_: archivo actualizado, nueva versión del archivo

**Duplicado exacto**:
Lectura válida sobrante con la misma Frontera, hora, versión y contenido que la ganadora.
_Avoid_: repetido

**Duplicado conflictivo**:
Lectura válida sobrante con la misma Frontera, hora y versión que la ganadora pero con distinto contenido; gana la última en orden de archivo. Distinta de una Corrección tardía, que difiere en versión.
_Avoid_: conflicto, colisión

**Factura**:
Cobro mensual a un Cliente por el consumo de una Frontera, con su tarifa desglosada en Componentes de tarifa.
_Avoid_: cobro, recibo

**Componente de tarifa**:
Parte de la tarifa por kWh de una Factura (G, T, D, C, P, R); la suma de los componentes es la tarifa.
_Avoid_: concepto, cargo

**Factura huérfana**:
Factura cuya Frontera no existe en el maestro. `fronteras_huerfanas_facturacion` cuenta las Fronteras distintas de esas Facturas, no las Facturas.
_Avoid_: factura inválida

## Partes

**Cliente**:
Empresa no regulada que compra energía a la comercializadora; titular de una o más Fronteras.
_Avoid_: usuario, cuenta

**PII en texto libre**:
Dato de contacto o identificación (correo, celular, cédula) escrito en un campo de texto libre que no lo debería contener. Cuenta cualquier correo, también los de rol (`cartera@empresa`), y cualquier celular, también los de la empresa.
_Avoid_: dato sensible (genérico)

**Enmascarado**:
Reemplazo de cada PII en texto libre por un marcador de su tipo (`[EMAIL]`, `[CELULAR]`, `[CEDULA]`), sin conservar ningún fragmento del original. El original no se guarda.
_Avoid_: ofuscación, anonimización, redacción

## Trazabilidad

**Corrida**:
Una ejecución completa del pipeline (batch o stream), con identidad propia; todo lo que carga o rechaza queda atado a ella. Si no tiene cierre registrado, se considera abortada.
_Avoid_: ejecución, job, run

**Identidad** (de la Corrida):
Quién y qué de una Corrida: `corrida_id`, ejecutor (`usuario@host` del sistema operativo, Confidencial), comando (`run` o `stream`) y versión del código. Se arma una vez al abrir la Corrida (`bitacora.Identidad`) y la comparten todos sus eventos en la Bitácora.
_Avoid_: contexto, metadatos de la corrida

**Corrida no iniciada**:
Corrida que abrió y registró su inicio en la Bitácora pero no llegó a procesar nada porque falló al tomar el lock (otra Corrida en curso o un error de base de datos). Queda cerrada como `fallo` en la Bitácora y en `metrics.json`, y termina con exit 1 sin traza (`CorridaNoIniciada`).
_Avoid_: corrida abortada, corrida rechazada

**Archivo omitido**:
Archivo que una Corrida encuentra pero no procesa porque ya fue cargado con el mismo contenido. Una Corrida repetida sin archivos nuevos solo tiene Archivos omitidos.
_Avoid_: archivo saltado, skip

**Desenlace**:
Lo que le pasa a un archivo en una Corrida: cargado, omitido (Archivo omitido) o rechazado (Archivo corrupto, formato desconocido o Archivo modificado). Cada archivo tiene un solo Desenlace por Corrida, y de él salen su evento en la Bitácora y su línea de log. Un Desenlace rechazado deja la Corrida parcial.
_Avoid_: resultado del archivo, estado del archivo

**Bitácora**:
Registro inalterable de los eventos de cada Corrida: quién, qué, cuándo, cuántas filas y con qué resultado. Nunca contiene PII.
_Avoid_: log, auditoría (como objeto)

**Ficha de Fuente**:
Todo lo que define una fuente, declarado como datos en un solo lugar (`fuentes.FICHAS`): sus campos, cómo se lee (formato, codificaciones y formatos CSV candidatos), su destino en la base, su tipo de carga, dónde están sus archivos en `data/` y si rechaza el Archivo modificado. Los módulos consultan la ficha en vez de preguntar por el nombre de la fuente; una fuente nueva es una ficha nueva. Una fuente que aún no se carga por lotes (`stream`) solo declara su catálogo (`Ficha`); las que se cargan por lotes son `FichaPorLotes` y se piden con `fuentes.ficha_por_lotes(fuente)`, que falla fuerte si la fuente no tiene lectura por lotes.
_Avoid_: configuración de fuente, registro de fuentes, metadata

## Gobernanza

**Nivel de clasificación**:
Etiqueta de cada campo según quién puede verlo: `Público` (publicable fuera de la comercializadora), `Interno` (cualquier rol de la comercializadora, incluido el analista), `Confidencial` (no sale de los roles operativos: ni analista ni logs), `Restringido-PII` (nadie lo ve en claro fuera del proceso que lo cifra). La sensibilidad es del dato, no de la tabla donde vive.
_Avoid_: sensibilidad, categoría, tag

**Minimización**:
Descartar un dato que viene en la fuente pero no tiene uso, en vez de custodiarlo.
_Avoid_: limpieza, recorte

**Límite de confianza**:
Punto donde un dato pasa entre partes con distinto nivel de confianza (por ejemplo, de un archivo recibido al proceso que lo lee). Equivale a la "frontera de confianza" de la plantilla del modelo de amenazas; no tiene relación con una Frontera.
_Avoid_: frontera de confianza, perímetro, zona

**KEK / DEK**:
Esquema envelope de la custodia. La DEK cifra los campos Restringido-PII y vive envuelta en `crypto.dek`; la KEK (en `estado/.env`, nunca en Postgres) envuelve la DEK. Si la KEK no desenvuelve la DEK, la Corrida falla pidiendo `make clean && make up`.
_Avoid_: clave maestra, llave de datos

**Blind index**:
HMAC-SHA256, con una llave distinta de la KEK, del NIT normalizado (solo dígitos). Permite unir y buscar por NIT sin exponerlo; es determinista, a diferencia del ciphertext.
_Avoid_: hash del NIT, índice cifrado
