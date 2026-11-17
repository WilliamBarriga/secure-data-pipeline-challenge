# Brief: plataforma de datos de consumo con PII custodiada

Este documento especifica el problema que resuelve `cauce`. Es la fuente de verdad de mayor precedencia del repositorio: ante una contradicción con un ADR, el glosario o el código, manda lo escrito acá. Las referencias del tipo "brief §N" apuntan a sus secciones numeradas.

Los términos con mayúscula inicial (Frontera, Lectura, Medición, Corrida, Cuarentena, Bitácora, Hueco, Enmascarado…) están definidos en [`GLOSSARY.md`](../GLOSSARY.md).

---

## 1. Contexto

Una comercializadora vende energía eléctrica a Clientes no regulados en Colombia. La unidad de medida comercial es la **Frontera**: un punto con identificador `frontera_id` de la forma `CO` seguido de diez dígitos, y con un código SIC de la forma `frt` seguido de cinco dígitos. Cada Frontera produce una Lectura de consumo en kWh por hora.

La facturación se calcula a partir de esas Lecturas, así que un error de consumo termina como un cobro equivocado a un Cliente. En el mismo recorrido circulan datos de contacto e identificación de empresas: NIT, correos, celulares, direcciones y nombres de representantes. Ese tratamiento cae bajo la Ley 1581 de 2012 (protección de datos personales) y la operación se alinea con ISO/IEC 27001:2022.

El sistema recibe archivos crudos con defectos de origen y debe producir dos cosas a la vez: cifras confiables y datos personales custodiados. Ambas son requisito; ninguna compensa la falta de la otra.

**Stack fijado por el problema.** Python para el pipeline y SQL sobre Postgres para el modelo. Un broker compatible con Kafka (Redpanda) para el stream. Terraform para describir el despliegue en AWS, sin desplegarlo. Todo se ejecuta en una sola máquina, sin cuenta de nube.

**Invariantes que ninguna otra regla relaja.**

1. Ningún valor personal aparece legible en `logs/`, en `out/` ni en lo que puede consultar el rol `analista`.
2. La credencial y la llave que trae el scaffolding heredado (brief §2.5) quedan sin uso: el sistema no las lee ni las reutiliza.
3. Procesar el maestro de Clientes nunca altera ni vacía otras tablas: su contenido hostil se almacena como texto y jamás se interpreta como instrucción.

---

## 2. Entradas

### 2.1 Disposición

Toda la entrada vive bajo `data/`, en cuatro subdirectorios:

| Subdirectorio | Contenido | Formato |
|---|---|---|
| `maestros/` | `clientes.csv`, `fronteras.csv`, `limites_frontera.csv` | CSV con encabezado |
| `consumos/` | un archivo por mes, `consumos_AAAA-MM.csv`, más archivos de corrección `consumos_AAAA-MM_correccion.csv` | CSV con encabezado |
| `facturacion/` | un archivo por periodo, `facturacion_AAAA-MM.json` | JSON con Facturas anidadas |
| `eventos/` | `mediciones_stream.ndjson` | un evento JSON por línea |

`data/` es de solo lectura para el sistema: ningún proceso la modifica, renombra ni limpia. En este repositorio la entrada son **Datos sintéticos** que produce `make datos` (brief §9); reproducen la forma y los defectos de una entrada real sin ninguno de sus valores.

### 2.2 Maestros

- **Clientes** (`clientes.csv`): identificador, razón social, NIT, correo y celular de contacto, dirección, ciudad, departamento, representante legal y un campo de texto libre `observaciones`. Es el archivo con más PII.
- **Fronteras** (`fronteras.csv`): `frontera_id`, código SIC, Cliente titular, mercado, operador de red, nivel de tensión, fecha de registro y estado.
- **Límites** (`limites_frontera.csv`): para cada Frontera, `min_kwh` y `max_kwh`, que forman su Banda operativa.

### 2.3 Consumos, facturación y eventos

- **Consumos**: una fila por Frontera y hora, con el valor, la versión de la Lectura (`version_lectura`) y un indicador de calidad. Los archivos de un mes no comparten necesariamente el formato de los de otro mes.
- **Corrección**: un archivo `_correccion` trae Lecturas nuevas, con versión mayor, para horas que ya llegaron en el archivo mensual.
- **Facturación**: por periodo, un emisor y una lista de Facturas; cada Factura nombra su Frontera, su Cliente (con NIT, razón social y correo de facturación), el consumo facturado, la tarifa, el valor total y el desglose en Componentes de tarifa.
- **Eventos**: cada línea es una Medición de 15 minutos con `evento_id`, `frontera_id`, `ts_medicion`, `kwh` y datos del medidor.

### 2.4 Categorías de defecto presentes

La entrada no está limpia, y eso es parte del problema. Ningún formato se asume: se detecta leyendo los archivos. Las categorías que aparecen son:

- **Codificación**: algún archivo no llega en UTF-8.
- **Separador y decimal**: algún mes usa `;` como separador y coma decimal.
- **Nombre de columna**: una misma magnitud aparece con encabezados distintos según el mes.
- **Unidad**: algún mes declara la unidad por fila y trae valores en MWh.
- **Zona horaria**: algún mes trae sus instantes en UTC en lugar de Hora local.
- **Texto hostil** en el maestro de Clientes: comillas dentro de comillas, un campo entre comillas con un salto de línea interno, cadenas extremadamente largas y fragmentos con aspecto de SQL.
- **PII en texto libre**: el campo `observaciones` contiene correos, celulares y cédulas escritos donde no correspondía.
- **Facturas huérfanas**: Facturas que nombran una Frontera ausente del maestro.
- **Valores inválidos y repetidos** en consumos: vacíos, negativos, fuera de escala, y la misma hora repetida con igual o distinta versión.
- **Desorden en el stream**: eventos duplicados, Mediciones retroactivas, eventos malformados y Fronteras que dejan de reportar.

### 2.5 Scaffolding heredado

El punto de partida incluye scaffolding heredado de una prueba de concepto que nunca pasó revisión de seguridad: un `docker-compose.yml`, un `requirements.txt` con dependencias que arrastran vulnerabilidades conocidas (CVE) y un archivo de configuración en `config/pipeline.yaml` que guarda una credencial de base de datos y una llave de cifrado en texto plano. Esa credencial y esa llave se consideran **comprometidas**. El archivo no se publica en este repositorio; su ruta se conserva porque los controles C6 y C7 (brief §5) existen por él.

---

## 3. Ingesta batch

Un pipeline lee `data/` y carga Postgres en un modelo analítico cuyo diseño (dimensional u otro) queda justificado en `DECISIONS.md` o en un ADR.

Requisitos:

1. **Descubrimiento.** Cada Corrida procesa los archivos que encuentra en `data/consumos/` y `data/facturacion/`, en orden lexicográfico de nombre. No hay lista de meses fija en el código. Un mes que no existía antes, en cualquiera de los formatos ya conocidos, se carga sin cambiar código.
2. **Idempotencia.** Dos ejecuciones seguidas de `make run` sobre la misma entrada producen el mismo estado en la base y los mismos bytes en `out/consumo_mensual.csv` y `out/dq_report.json`.
3. **Correcciones tardías.** El archivo de corrección se absorbe de forma incremental: reemplaza las Lecturas afectadas según B3 sin reconstruir el resto del modelo.
4. **Cuarentena.** Nada rechazado se borra. Cada fila o archivo rechazado queda en una Cuarentena consultable junto con el motivo del rechazo.
5. **Robustez ante archivos dañados.** Un Archivo corrupto puede rechazarse entero o dejar sus filas malas en Cuarentena, pero en ningún caso cambia lo que ya estaba cargado de meses anteriores. Lo mismo vale para un Archivo modificado.

---

## 4. Calidad de datos (B1-B7)

Las reglas B son exactas: de ellas dependen las cifras de `out/` y no admiten lecturas alternativas.

| Id | Nombre | Regla |
|---|---|---|
| **B1** | Hora local | Todo instante se expresa en `America/Bogota`, que es UTC−5 todo el año porque Colombia no aplica horario de verano. La hora y el mes de una Lectura se toman en esa zona; un archivo con instantes en UTC se convierte antes de asignar hora y mes. |
| **B2** | kWh | Toda cifra de consumo que sale del sistema está en kWh. Una fila que declara MWh se multiplica por 1000 antes de cualquier otra regla. |
| **B3** | Ganadora por hora | Entre varias Lecturas de la misma `(frontera_id, timestamp)` queda la de `version_lectura` más alta. Ante igual versión queda la que aparece más tarde, recorriendo los archivos por su nombre en orden lexicográfico y, dentro de cada archivo, de arriba hacia abajo. |
| **B4** | Inválidos | Se descarta, sin imputar ningún reemplazo, la Lectura cuyo valor está vacío o es nulo, la que es menor que cero y la que supera 1.000.000 kWh (un millón, valor imposible para una Frontera de este tipo). Cada descarte se cuenta por su causa. |
| **B5** | Huecos | Un Hueco es una hora local de una Frontera que termina sin ninguna Lectura válida, tanto si la fila nunca llegó como si llegó y B4 la descartó. Los Huecos se cuentan; nunca se rellenan. |
| **B6** | Facturas huérfanas | Una Factura cuya `frontera_id` falta en el maestro de Fronteras no entra al modelo: queda en Cuarentena y se cuenta. |
| **B7** | Entrada hostil | El contenido del maestro de Clientes es dato y nunca código: se parsea con un lector CSV estricto y se persiste solo con SQL parametrizado. Ninguna fila se pierde sin dejar rastro: cada una termina cargada o en Cuarentena. |

---

## 5. Seguridad y gobernanza (C1-C8)

| Id | Nombre | Control |
|---|---|---|
| **C1** | Clasificación | `data_classification.yaml` asigna a cada columna del modelo uno de cuatro Niveles de clasificación: `Público`, `Interno`, `Confidencial` o `Restringido-PII`. El pipeline lo lee en cada Corrida y actúa según él; una columna sin nivel es un error, no un descuido tolerado. |
| **C2** | PII en texto libre | En `observaciones` se detectan correos, celulares y cédulas, y cada hallazgo se Enmascara antes de persistir. Se informa la cantidad de Clientes con al menos un hallazgo. |
| **C3** | Cifrado en reposo | Ninguna columna `Restringido-PII` existe en claro en Postgres. El mecanismo (cifrado en la aplicación, envelope, extensión de la base…) se elige y se justifica en un ADR. Si hace falta cruzar o buscar por un campo sensible, se resuelve sin descifrarlo. La comprobación es leer las tablas crudas como superusuario, que es lo que ve quien obtiene un volcado o acceso de DBA. |
| **C4** | Menor privilegio | La base define tres roles, cada uno con el mínimo permiso efectivo: **`ingesta`** escribe en el modelo y no lee PII en claro; **`analista`** solo consulta la vista analítica, no accede a tablas base, no ve PII en claro y no escribe; **`auditor`** solo lee la Bitácora y no ve PII en claro. Cualquier permiso por encima de esa descripción es una falla del control. |
| **C5** | Trazabilidad | Una Bitácora de solo inserción registra cada Corrida: ejecutor, operación, momento, cantidad de filas y resultado. `UPDATE` y `DELETE` sobre ella están revocados en la base, no prohibidos por convención. |
| **C6** | Secretos | El repositorio no contiene credenciales. El secreto del scaffolding heredado (brief §2.5) se da por filtrado: el código no lo lee y las credenciales vigentes se generan fuera de git. |
| **C7** | Dependencias | Las dependencias heredadas se auditan contra vulnerabilidades conocidas y las afectadas se actualizan o se reemplazan. |
| **C8** | Modelo de amenazas | `THREAT_MODEL.md` aplica STRIDE a la arquitectura construida: al menos 8 amenazas específicas de este pipeline, cada una con su control y su referencia a un control del Anexo A de ISO/IEC 27001:2022. No cuentan las amenazas de catálogo sin anclaje en el sistema. |

---

## 6. Streaming

Un consumidor lee el tópico `mediciones.raw`, alimentado por el productor de `tools/producer.py`, y emite Alertas. `make stream` lee hasta el final de lo publicado y termina por su cuenta; quedarse esperando mensajes indefinidamente es una falla.

La **Rejilla** es la jornada local que empieza a la medianoche del 1 de junio de 2026 en `America/Bogota` (offset `-05:00`), dividida en 96 intervalos de 15 minutos. Las tres reglas son exactas:

| Tipo | Se emite cuando | Instante (`ts`) |
|---|---|---|
| `FRONTERA_SIN_REPORTE` | una Frontera completa 3 intervalos seguidos de la Rejilla, es decir 45 minutos, sin ninguna Medición. Cada Caída produce una única Alerta, por larga que sea. | el instante del tercer intervalo vacío de la racha |
| `CONSUMO_FUERA_DE_BANDA` | el `kwh` de una Medición supera, con desigualdad estricta, el `max_kwh` de su Frontera en `limites_frontera.csv` | el instante de esa Medición |
| `LECTURA_RETROACTIVA` | llega una Medición cuyo `ts_medicion` es anterior al de la Medición más reciente ya procesada de su Frontera | el instante de la Medición atrasada |

---

## 7. Observabilidad

- **Logs**: JSON estructurado, un evento por línea, en `logs/pipeline.log`. Llevan nombres y conteos, nunca valores de fila. Ningún correo, celular ni cédula de la entrada puede aparecer en ellos.
- **Métricas**: `out/metrics.json` describe la última Corrida; su contenido mínimo está en brief §9.
- **Operación**: `RUNBOOK.md` breve con los pasos para diagnosticar y recuperar el sistema cuando una Corrida falla fuera de horario.

---

## 8. Infraestructura como código

`infra/` contiene Terraform que describe cómo correría el sistema en AWS, sin desplegarse nunca: un bucket S3 cifrado y con versionado, una llave KMS con política explícita, roles IAM de menor privilegio equivalentes a los de C4, y los recursos adicionales que la arquitectura justifique. No requiere cuenta ni credenciales de AWS. La condición de aceptación es que `terraform validate` termine sin errores.

---

## 9. Contrato de ejecución

### 9.1 Targets

Los nombres de estos targets de `make` son fijos; su implementación es libre. El **operador** los corre en este orden:

```bash
make datos      # Datos sintéticos en data/ (aborta si data/ ya tiene archivos)
make setup      # dependencias e imagen
make up         # Postgres y Redpanda listos; genera credenciales.yaml si falta
make run        # Corrida batch completa, límite 900 s
make run        # segunda Corrida: comprueba idempotencia
make stream     # consumidor del stream, límite 300 s, termina solo
make test       # suite de pruebas
make down       # baja el stack
```

Antes de la primera Corrida el operador vacía `out/` y `logs/`: todo lo que se examina ahí lo escribe el sistema en esa ejecución. Un target que falta o termina con código distinto de 0 se considera incumplido. La única dependencia del host es Docker con Compose v2 y `make`.

### 9.2 `out/consumo_mensual.csv`

CSV en UTF-8, separador coma, fin de línea `\n`, con encabezado. Columnas, en este orden:

| Posición | Columna | Contenido |
|---|---|---|
| 1 | `frontera_id` | identificador de la Frontera |
| 2 | `mes` | `YYYY-MM`, en Hora local (B1) |
| 3 | `consumo_kwh` | suma de las Lecturas válidas ganadoras del mes, en kWh, con exactamente 3 decimales |
| 4 | `horas_con_dato` | cantidad de horas del mes con Lectura válida |

Hay una fila por cada par Frontera-mes que tenga al menos una Lectura válida, y ninguna más. Ejemplo de fila: `CO0000000001,2026-02,98765.432,672`.

### 9.3 `out/dq_report.json`

Un objeto JSON con estas llaves, todas presentes siempre, aunque valgan 0:

| Llave | Tipo | Significado |
|---|---|---|
| `clientes_con_pii_en_texto_libre` | entero | Clientes con al menos un hallazgo de C2 |
| `correcciones_tardias_aplicadas` | entero | Correcciones tardías que desplazaron una Lectura válida |
| `duplicados_conflictivos` | entero | filas sobrantes descartadas por B3 con igual versión y contenido distinto |
| `duplicados_exactos` | entero | filas sobrantes descartadas por B3 con igual versión y contenido idéntico |
| `filas_leidas` | entero | filas de consumo leídas |
| `fronteras_huerfanas_facturacion` | entero | Fronteras distintas que nombran las Facturas huérfanas (B6) |
| `horas_sin_lectura_valida_por_frontera` | objeto | `frontera_id` → cantidad de Huecos (B5) |
| `horas_sin_lectura_valida_total` | entero | suma de los Huecos de todas las Fronteras |
| `registros_mwh_convertidos` | entero | filas convertidas de MWh a kWh (B2) |
| `registros_negativos` | entero | descartes por valor menor que cero (B4) |
| `registros_nulos` | entero | descartes por valor vacío o nulo (B4) |
| `registros_outlier` | entero | descartes por superar 1.000.000 kWh (B4) |

Los dos contadores de duplicados cuentan filas descartadas, no grupos de filas repetidas.

### 9.4 `out/alertas.json`

Un arreglo JSON de objetos con tres llaves: `tipo` (uno de los tres de brief §6), `frontera_id` y `ts` en ISO 8601 con offset explícito. El orden es libre y no hay dos objetos iguales. Ejemplo:

```json
[
  {"tipo": "CONSUMO_FUERA_DE_BANDA", "frontera_id": "CO0000000001", "ts": "2026-06-01T13:15:00-05:00"}
]
```

### 9.5 `out/metrics.json`

Un objeto JSON que incluye, como mínimo, estas llaves:

- `ts_inicio` y `ts_fin`: instantes de apertura y cierre de la Corrida.
- `filas_leidas`: filas leídas de la entrada.
- `filas_cargadas`: filas que llegaron al modelo.
- `filas_rechazadas_por_motivo`: objeto motivo → conteo.
- `duracion_s_por_etapa`: objeto etapa → segundos.

---

## 10. Escenarios de aceptación

El operador comprueba el sistema con estos seis escenarios, además del contrato de brief §9:

1. **Mes nuevo.** Agrega a `data/consumos/` un mes que no estaba (`make datos` lo produce con `--mes AAAA-MM`), en uno de los formatos conocidos, y corre `make run`. El mes aparece en `out/consumo_mensual.csv` sin tocar código.
2. **Archivo dañado.** Agrega una copia truncada o alterada de un archivo de consumos (`--corromper RUTA`) y corre `make run`. El archivo se rechaza o sus filas malas van a Cuarentena; las cifras de los meses ya cargados no cambian.
3. **Idempotencia.** Corre `make run` dos veces y compara: el estado de la base y las Fotos de `out/` son idénticos.
4. **Volcado.** Se conecta a Postgres como superusuario y lee las tablas crudas: los campos `Restringido-PII` solo aparecen como ciphertext, blind index o texto Enmascarado.
5. **Roles.** Se conecta como `ingesta`, `analista` y `auditor` e intenta, con cada uno, lo que C4 le prohíbe. Todo intento falla.
6. **Logs.** Pasa expresiones regulares de correo, celular y cédula sobre `logs/`. No hay coincidencias con valores de la entrada.
