# Decisiones

Dos capas. Las decisiones de diseño se cerraron antes de construir: están en `docs/adr/` y en la tabla de abajo, una fila por decisión con lo que se resignó. Las de construcción son lo que se cerró al construir y que el diseño no dice: van después, una entrada por decisión y en orden de construcción.

## Decisiones de diseño (antes de construir)

| Decisión | Qué se resignó | Fuente |
|---|---|---|
| PII cifrada en la app (AES-256-GCM, envelope local) desde `raw`, con blind index HMAC para joins; original de `observaciones` nunca persiste | Depurar `raw` en claro; re-enmascarar sin volver al archivo fuente; pgcrypto (la llave viajaría a la base) | [ADR 0001](docs/adr/0001-pii-cifrada-desde-raw-y-minimizada.md) |
| Stack: Python `csv` stdlib + SQL en Postgres, sin MinIO ni pandas; app en Docker por defecto o `.venv` (`RUNTIME=local`); 5 deps runtime con hashes, sin uv | Comodidad de pandas/DuckDB (inferencia de tipos sobre entrada hostil); MinIO como landing (imagen ya no descargable) | [ADR 0002](docs/adr/0002-stack-sin-minio-ni-pandas-con-runtime-docker.md) |
| Esquema por zona (`raw`/`quarantine`/`crypto`/`core`/`analytics`/`audit`), estrella en `core` con llaves naturales y SCD1 | Data Vault o historia SCD2 (no lo pide el brief) | [ADR 0001](docs/adr/0001-pii-cifrada-desde-raw-y-minimizada.md) |
| Ingesta incremental por (fuente, nombre)+sha256; corrupción estructural = rechazo entero con exit 0; sin DELETE para `ingesta` | Reprocesar todo en cada Corrida; aceptar filas sueltas de un archivo roto | [Archivo = (fuente, nombre, sha256)](#archivo--fuente-nombre-sha256-un-rechazado-queda-omitido) |
| Roles con grants mínimos, owner NOLOGIN, vista definer + `security_barrier`; Bitácora por eventos vía SECURITY DEFINER, append-only + cadena de hash | Bitácora con texto libre; prevenir al superusuario (imposible en Postgres: solo se detecta) | [`SECURITY.md` §3 y §6](SECURITY.md) |
| Clasificación de columnas como artefacto vivo con check fail-fast en `run`/`stream`/`test` | Una lista fija en el código que se desactualiza en silencio | [`SECURITY.md` §1](SECURITY.md) |
| Logs JSON sin texto libre en 3 capas; logs enmascaran y siguen, `out/` falla cerrado | Logs legibles con mensajes de excepción | [Logs enmascaran, `out/` falla](#logs-enmascaran-out-falla) |
| Semántica del stream: Rejilla 0..95 × maestro, Caída por conjunto al cierre, retro estricta, dedupe por `evento_id`; `assign` hasta el high watermark | Consumer group con commits (re-ejecutar no daría lo mismo) | [`make stream`](#make-stream) |
| Terraform como raíz plana en `infra/` sin backend ni módulos hijos | Módulos reutilizables (más difícil de leer de un tirón) | [Terraform sin Object Lock ni CloudTrail](#terraform-brief-8-sin-object-lock-ni-cloudtrail) |
| Threat model con 5 Límites de confianza y "Sí" solo con test citado | Un threat model genérico que promete de más | [`THREAT_MODEL.md`](THREAT_MODEL.md) |
| Recorte de alcance: se mantienen la cadena de hash y `RUNTIME=local`; Object Lock/CloudTrail van a `no_implementado` | El WORM en la nube (control propuesto en `infra/README.md`) | [`infra/README.md`](infra/README.md) |

## Decisiones de construcción

### Logs enmascaran, `out/` falla

Un log es observabilidad: si el detector estricto (`pii.detectar_estricto`) encuentra PII en una línea, se enmascara, se emite `pii_filtrada` y se cuenta; la Corrida sigue. Una salida de `out/` es un producto: si el detector encuentra algo, no se escribe y la Corrida falla con `pii_en_salida`. Trade-off: un log perdido es recuperable, un archivo de `out/` publicado con PII no.

### Falso positivo de `frontera_id` en el detector estricto

`CO3000000001` contiene 10 dígitos que empiezan en 3 y parecen un celular. El detector estricto exige `+57` o un separador, nunca pega un celular a una letra y no tiene el fallback de 7 dígitos, así que no marca ninguna `frontera_id`. Resignamos detectar celulares pegados y sin separador en logs/`out/`; esos solo los atrapa `enmascarar` en `observaciones`, que sí usa el fallback.

### La Bitácora crece por diseño en la prueba de idempotencia

La Bitácora es append-only: dos corridas iguales dejan los mismos conteos en `raw`, `quarantine` y `core`, pero la Bitácora siempre suma eventos. La idempotencia se prueba sobre las tablas de datos y sobre "la Bitácora solo crece", nunca sobre su cantidad.

### DEK generada por `bootstrap`, no por `run`

`ingesta` solo tiene SELECT sobre `crypto.dek`. Crear la DEK exige escribir ahí, así que lo hace `bootstrap` (superusuario, dentro de `make up`) si la tabla está vacía; `preparar` genera KEK y `K_bidx` en `estado/.env` solo si faltan. Descartado: darle INSERT a `ingesta` (el rol que procesa datos hostiles no debe poder reemplazar la DEK). Consecuencia: borrar `estado/.env` con el volumen vivo pierde las llaves; `run` lo detecta (la KEK nueva no abre la DEK) y pide `make clean && make up`. Rotar la DEK queda fuera de alcance (`key_version` ya está en el modelo).

### El original de `observaciones` no se guarda, tampoco en Cuarentena

Una fila de cliente rechazada va a `quarantine.rechazo.payload_cifrado` como JSON cifrado de sus campos con `observaciones` ya Enmascarado. `fuentes.leer` no expone el texto crudo de las fuentes con PII (`lineas` vacío: `raw.linea` no se usa para clientes) y `custodia` es quien enmascara y cifra. Depurar un rechazo exige la KEK. Ver [ADR 0001](docs/adr/0001-pii-cifrada-desde-raw-y-minimizada.md).

### `enmascarar` enmascara de más

Cualquier racha de 7 o más dígitos (con puntos, guiones o espacios) pasa a `[CEDULA]`, incluidas fechas como `2024-08-03`. Es deliberado (ADR 0001: la PII residual queda en claro si el detector falla), a costa de perder algún número legítimo en el texto libre.

### ISO-8859-1 sin rechazo por decodificación

`clientes.csv` se decodifica como UTF-8 si es válido y, si no, como ISO-8859-1, que decodifica cualquier byte. Resignamos el rechazo `no_decodifica` para clientes: un archivo en otra codificación se carga con caracteres cambiados en vez de rechazarse.

### `credenciales.yaml` se lee con `extra="ignore"` (revisión de arquitectura antes de construir)

La regla general es `extra="forbid"`, pero `credenciales.yaml` es un archivo del operador que puede traer claves que el código no consume (comentarios convertidos en metadatos, claves de otra herramienta, ...). Con `forbid`, `make up` fallaría sobre un archivo válido. Se ignoran solo en el lector de `credenciales.yaml` (`arranque`); las claves que sí leemos (Postgres, roles, `objetos`) siguen tipadas y estrictas.

### El código no lee `config/pipeline.yaml` (decide el autor)

Prevalece el guardrail de `AGENTS.md` sobre la idea inicial de comparar las credenciales nuevas contra el secreto heredado. Se eliminó `secretos.valores_heredados` y el parámetro `heredado` de `preparar`. Se mantiene el check de password débil (`TODO`, `cambiame`, `postgres`, `minioadmin`, menos de 24 caracteres) y el de repetidos entre roles, nombrando la clave y nunca el valor. Las credenciales de `make credenciales` son aleatorias, por lo que no pueden coincidir con el secreto heredado. Un test (`test_el_codigo_no_referencia_el_secreto_heredado`) falla si `src/cauce/` nombra `pipeline.yaml`.

### El lock de Corrida falla rápido (decide el autor)

`lock_corrida` toma `pg_try_advisory_lock` una sola vez; si está tomado, la Corrida termina con `corrida_fin=fallo`, `codigo_error=corrida_concurrente` y exit != 0, sin cargar nada. Reemplaza el sondeo de hasta 600 s ("la segunda se serializa"). Las Corridas igual nunca se pisan; el operador reintenta.

### `fact_consumo_horario` sin FK a `dim_frontera`

Una Frontera vista solo en consumos entra igual (y al universo de Huecos) y se marca en el log (`frontera_fuera_maestro`, con la cantidad de Fronteras distintas). La cantidad es un contador del archivo (`fronteras_fuera_maestro` en `raw.archivo_conteo`, solo si no es cero) que `almacen` calcula en la misma transacción de la carga. Cambia el DDL: un volumen existente necesita `make clean && make up` (la huella lo exige).

### Catálogo cerrado de formatos de consumo, solo enero por ahora (superado, ver abajo)

`fuentes.FORMATOS_CONSUMO` trae unicamente el encabezado de enero. Febrero, marzo y abril/correccion se rechazan como `formato_desconocido` hasta el [catálogo completo](#catálogo-de-formatos-de-consumo-completo), que agrega sus entradas. Ojo: un archivo rechazado queda en `raw.archivo` con su sha256, y `registrar` lo trata como Archivo omitido; al agregar formatos hay que decidir como se reprocesan (no correr `make run` real entre los dos pasos sobre un volumen que se vaya a conservar).

### Archivo = (fuente, nombre, sha256); un rechazado queda omitido

`raw.archivo` pasa de `sha256 UNIQUE` a `UNIQUE (fuente, nombre, sha256)`: el mismo contenido con otro nombre es un archivo Nuevo y un Archivo modificado (mismo nombre, otro sha256) de consumos o facturación se rechaza entero con `archivo_modificado`, queda registrado como `rechazado` con su sha256 y una fila de Cuarentena con `linea` nula. Un archivo rechazado (corrupto, formato desconocido o modificado) sigue siendo Archivo omitido en la siguiente Corrida: reprocesarlo haría crecer la Cuarentena en cada Corrida y rompería la idempotencia. Para recuperarlo hay que corregir el archivo (otro sha256). Cambia el DDL: un volumen existente necesita `make clean && make up`.

`Bitacora.codigo_error` es un catálogo cerrado (el CHECK del DDL): `formato_desconocido` y los subcódigos de corrupto se registran como `archivo_corrupto`; el detalle va en el log (`motivo`, p. ej. `archivo_corrupto:sin_salto_final`) y en `quarantine.rechazo.motivo`.

### Catálogo de formatos de consumo completo

`fuentes.FORMATOS_CONSUMO` tiene tres entradas: enero/abril/corrección (`,`, `.`, hora local), febrero (`;`, coma decimal, `timestamp_utc` ISO con `Z`, convertido a hora local) y marzo (`kwh` + `unidad` kWh|MWh; MWh se multiplica por 1000 antes de B4 y marca `mwh_convertida`). El encabezado exacto elige la entrada (así se "sniffea" el separador); la unidad se compara exacta, y cualquier otra va a Cuarentena como `unidad_desconocida`.

### Cubos de sobrantes disjuntos y la alternativa de 420

Cada Lectura sobrante (candidata válida que no ganó B3) cae en un solo cubo respecto de la ganadora de su `(frontera, hora)`: versión menor que la ganadora es Corrección tardía (cuenta una hora, no una fila); misma versión y contenido normalizado idéntico (`numeric` y calidad iguales; `10.000` = `10`) es Duplicado exacto; misma versión y otro contenido es Duplicado conflictivo. Sobre la entrada de referencia da 180 / 120 / 300, 600 sobrantes en total. Alternativa descartada: contar como conflictivo todo sobrante de contenido distinto sin mirar la versión (120 + 300 = 420) y dejar la Corrección tardía como una vista aparte; las Correcciones tardías quedarían doblemente contadas y los cubos dejarían de sumar los sobrantes. Una v1 o v2 inválida nunca llega a `raw.lectura_consumo`, así que no puede desplazar ni ser desplazada. "Tardía" se decide por versión, no por orden de carga: así el cubo sale igual cargando los archivos en cualquier orden.

### Universo de Huecos

`core.v_hueco` = (`core.dim_frontera` ∪ Fronteras con filas en `core.fact_consumo_horario`) × (meses locales con al menos una fila cargada, de cualquier Frontera) × horas calendario del mes en `America/Bogota`. Una Frontera fuera del maestro entra solo si tiene alguna Lectura válida (una Frontera que solo tuvo filas rechazadas no es identificable: Cuarentena no guarda su `frontera_id` aparte). Un mes ausente entero para una Frontera es un mes completo de Huecos; un mes sin filas de nadie no existe en el universo. Nada se imputa. Cambia el DDL (la vista): un volumen existente necesita `make clean && make up`.

### `dq_report.json` es una Foto, no un delta

`fotos.materializar_salidas` recalcula todo desde la base: contadores por archivo de `raw.archivo_conteo` (solo archivos aceptados; un rechazado aporta 0), cubos B3 sobre `raw.lectura_consumo` y Huecos desde `core.v_hueco`. Un delta (sumar lo que cada Corrida cargó) se rompe con la 2.ª Corrida (todo Omitido, aportaría 0) y con archivos que llegan fuera de orden. `registros_mwh_convertidos` cuenta también las filas MWh rechazadas (outlier tras la conversión), por eso `fuentes.leer` lo cuenta por archivo y no se recalcula desde las Lecturas cargadas. `clientes_con_pii_en_texto_libre` suma solo la última versión de cada archivo de clientes (el maestro se reaplica como upsert). `fronteras_huerfanas_facturacion` cuenta Fronteras distintas entre las Facturas huérfanas (ver [la decisión de abajo](#fronteras-distintas-no-facturas-en-fronteras_huerfanas_facturacion)). Los contadores B de `Resultado.conteos` son dispersos (solo los distintos de cero) y las claves salen ordenadas.

### Fronteras distintas, no Facturas, en `fronteras_huerfanas_facturacion`

El indicador cuenta Fronteras distintas entre las Facturas huérfanas (en la entrada de referencia, 12, que coincide con las 12 Facturas porque cada huérfana trae una Frontera propia). Alternativa descartada: contar Facturas (o filas de Cuarentena `frontera_huerfana`), que infla el número cuando una Frontera ausente factura varios meses y mide el volumen de Cuarentena, no el hueco del maestro. Cuarentena no tiene `frontera_id` (el cuerpo va cifrado), así que el recuento une Cuarentena con `raw.factura` por (archivo, linea), tabla sin PII.

### `raw.factura` guarda todas las Facturas válidas del archivo

Como las candidatas de consumo, `raw.factura` guarda toda Factura válida (huérfanas y perdedoras incluidas), con `generado_en`, posición, `frontera_id` y el cuerpo cifrado (`payload_cifrado`). `almacen` resuelve desde ahí: huérfana (Frontera ausente de `core.dim_frontera`) y perdedora van a Cuarentena copiando ese ciphertext (`almacen` no tiene llave); entre las demás gana por `factura_id` la mayor `generado_en`, empate por último (nombre en collation C, posición). Una fila vieja desplazada por un archivo posterior también cae en Cuarentena `factura_duplicada` (una sola vez), con su archivo y línea. Un `factura_id` repetido dentro del archivo se resuelve en `fuentes.leer` (gana la última posición; la PK de `raw.factura` no admite dos). Consecuencia: `cargadas` de un archivo de facturación cuenta solo las que llegan a `core`; los conteos finales salen de la base (`almacen.conteos_de_archivo`). Cambia el DDL: un volumen existente necesita `make clean && make up`.

### Subobjeto cifrado en `raw.factura.payload`

El subobjeto de primer nivel que contiene algún campo cifrable según el yaml (`cliente`: NIT, email, razón social) viaja entero cifrado en base64 dentro del payload; el resto queda en claro (tarifa, componentes, `metodo_pago.tipo`). `metodo_pago.cuenta_enmascarada` se poda de `raw`, `core` y del cuerpo de Cuarentena. Core no lleva datos de cliente: `cliente_id` se resuelve vía `frontera_id -> dim_frontera -> dim_cliente` y queda NULL si falta el Cliente en el maestro (no hay FK en `dim_frontera.cliente_id`). Sin DELETE para `ingesta`: si una Factura nueva trae menos componentes que la anterior, los sobrantes quedan (los datos siempre traen G, T, D, C, P, R).

### `v_consumo_mensual` cruza por `periodo = mes_local`, ambas `date`

La vista une medido y facturado por `frontera_id` + `periodo = mes_local` (las dos `date`, mes en hora local de Bogotá); `mes` es solo presentación (`to_char(mes_local, 'YYYY-MM')`). El FULL OUTER JOIN se expresa como unión de llaves (`base`) con LEFT JOIN a medido, hueco y facturado: es equivalente, y cada lado ausente queda NULL (`consumo_kwh`, `consumo_facturado_kwh`, `diferencia_*`), nunca 0; solo `horas_con_dato` y `horas_hueco` son conteos y valen 0. `diferencia_pct` es NULL si falta un lado o el medido es 0. Las Facturas duplicadas de un mismo Frontera-mes se suman. La vista une con `core.dim_frontera` por LEFT JOIN (corregido el 2026-11-11) para `cliente_id`, `operador_red` y `nivel_tension`: una Frontera con consumo pero fuera del maestro aparece con esas columnas en NULL (las Facturas huérfanas ya van a Cuarentena, así que solo puede venir del consumo). Con la entrada de referencia siguen siendo 160 filas. Sigue definer + `security_barrier`; no sale a `out/`.

### Ronda de fixes del code review a mitad de construcción

- `etapa_fin`: uno por fuente, con los conteos sumados de todos sus archivos y `parcial` si alguno se rechazó (antes, uno por archivo). Los maestros son una fuente por archivo (`frontera`, `limite`, `cliente`), así que ahí no cambia.
- `consumo_kwh` ya era `numeric` libre en `raw.lectura_consumo` y `core.fact_consumo_horario`: el Duplicado exacto se compara sin redondear (cubierto por un test: difieren en el cuarto decimal, son conflictivas). Se redondea solo al escribir `consumo_mensual.csv`.
- Duración medida del batch real (maestros, 5 consumos, 4 facturas, contra `cauce_test`, `RUNTIME=local`): 14,1 s. El e2e `test_corrida_completa_real_cabe_en_900_s_y_metrics_trae_el_desglose` acota a < 900 s (brief §9).
- Bitácora `duplicadas` queda NULL (no 0): solo cae natural para facturación (`factura_duplicada` en `rechazadas_por_motivo`), no para consumos, donde el cubo exacto/conflictivo/tardío se calcula sobre toda la base (`_SQL_CUBOS`) y no por archivo. Poblarlo solo en una fuente daría un conteo con significado distinto según la fuente; el desglose sigue en `dq_report.json`.

### Ficha de Fuente y forma única de la fila core (revisión de arquitectura a mitad de construcción)

- Todo lo que define una fuente (campos, codificaciones, perfiles CSV candidatos, destino raw/core, tipo de carga, origen en `data/` y si rechaza el Archivo modificado) vive en `fuentes.FICHAS`. Va en `fuentes` porque ya era la única verdad del catálogo, es núcleo puro y tanto `custodia` como `almacen` y `corrida` lo importan sin ciclo. `CATALOGO` se deriva de las fichas. Una fuente nueva es una entrada nueva; `stream` tiene ficha solo con campos.
- `almacen` elige el cargador por el tipo de carga de la ficha (`maestro`, `consumo`, `factura`), sin defecto: un tipo sin cargador falla con `ValueError` antes de escribir.
- El destino de consumo es honesto: cada Lectura candidata aterriza en `raw.lectura_consumo` y el merge B3 la lleva a `core.fact_consumo_horario`; `custodia` devuelve la fila en `raw` (y en `core`, como toda fuente sin PII con tabla raw).
- Las columnas de la fila se toman siempre de la fila custodiada; lo único explícito en `almacen` es lo que pone la base (`corrida_id` y el `cliente_id` de factura). Una ruta de factura ausente o descartada llega como `None` (NULL), no como columna faltante.
- Sin cambio de comportamiento: `out/`, la base (sin ciphertext ni UUID) y el log de una Corrida real sobre `data/` son idénticos antes y después.

### Oráculo de 33 Alertas y Caídas juzgadas al cierre

La entrada de referencia trae 22 eventos con `kwh > max_kwh`, no los 25 que decía el diseño: por la regla exacta del brief §6 el oráculo es 6 + 22 + 5 = 33. Manda el dato sobre el diseño. La confirmación de Caídas por marca de agua existe, pero va apagada por defecto (`margen=None`): con particiones de Kafka intercaladas, juzgar antes del cierre hacía que el resultado dependiera del orden de llegada. Se resigna emitir la Caída antes de terminar el stream. El diseño admite las dos opciones, y `make stream` termina igual en segundos.

### `make stream`

- `make stream` = `$(CAUCE) producir` (asegura `mediciones.raw` con 3 particiones si falta y corre `tools/producer.py --rate 0`) + `$(CAUCE) stream`. Tópico y broker salen de `KAFKA_TOPIC` y `KAFKA_BOOTSTRAP`; los tests usan un tópico efímero `cauce-test-<uuid>`.
- El universo de Fronteras y la Banda operativa salen de `data/maestros/` vía `fuentes.leer` (brief §6 nombra `limites_frontera.csv`), no de `core.dim_frontera`: el stream no depende del estado del batch. Si falta un maestro, `MaestroNoDisponible` (`configuracion_invalida`).
- La Bitácora no tiene columnas para motivos ni tipos de Alerta y el diseño del stream no admite cambio de DDL. El cierre del stream son `etapa_fin` con `fuente` de catálogo cerrado: `stream` (leídos, válidos, inválidos, duplicados), `stream.descartado.<motivo>` y `stream.alerta.<TIPO>`. Solo nombres de enum y conteos. Una Alerta no es una Medición válida ni se carga: `stream.alerta.<TIPO>` lleva la cantidad emitida en `filas_leidas` y 0 en `filas_cargadas`.
- `metrics.json`: conteos de la base arriba, los de la Corrida en `corrida` (cambio del 2026-11-12, ver [la entrada de abajo](#metricsjson-conteos-de-la-base-la-corrida-en-una-subclave-cambio-del-2026-11-12)).
- `metrics.json`: el stream solo reemplaza la subclave `stream` (lectura + escritura atómica), que además lleva `corrida_id`, `ts_fin`, `resultado` e `invalidos_por_motivo`. Sin `metrics.json` previo, escribe solo `{"stream": ...}`.
- Si la red de seguridad corta la lectura (inactividad 10 s o tope 210 s, conexión incluida) antes del high watermark de Kafka, la Corrida queda `parcial` y `alertas.json` se escribe con lo leído. El motivo (`fuente_eventos.Corte`) es parte del puerto `FuenteEventos`.
- Cota total de `make stream` < 300 s (brief §9): `asegurar_topico` ≤ 20 s + producer cortado a 30 s (exit 1) + tope del stream 210 s + arranque y escrituras. El tope bajó de los ~240 s del diseño para que la suma cierre.

### Dedupe del stream frente a datos hostiles (revisión de arquitectura final)

- Solo un evento **válido** reserva su `evento_id`. Antes se marcaba visto antes de validar, y un inválido hostil con el id de un evento real hacía que el real se descartara como duplicado: suprimía su Alerta y fabricaba una Caída. Los inválidos con `evento_id` se recuerdan aparte, solo para que la alimentación doble no infle los descartes; ese registro nunca bloquea a un válido.
- Instante igual al máximo de su Frontera = duplicado: se cuenta en `duplicados` y no pasa por las reglas. Se descartó tratarlo como Medición normal evaluada contra la Banda operativa: el brief §6 define Fuera de banda sobre "la medición", y una segunda Medición del mismo instante ya procesado no es una Medición nueva sino un reenvío. El oráculo no cambia (33): la entrada de referencia no trae instantes repetidos.

### Lo que la revisión de arquitectura a mitad de construcción descartó a propósito

Se registra para que una próxima review no lo vuelva a proponer:

- **Sumar `Conteos` automáticamente** en `corrida._cargar_etapa`: son 3 líneas explícitas, y `docs/anexos/ai-usage-registro.md` cita esa suma como la corrección de un error real de la IA (`zip` sobre un dataclass).
- **Pasar `conteos_bitacora` y `evento_log` a métodos del Desenlace**: cambio cosmético. Hoy son un solo traductor libre que `docs/anexos/ai-usage-registro.md` cita.
- **Mover `leer_clasificacion` fuera de `corrida`**: el núcleo no puede hacer I/O de disco (`test_capas`), `bordes` no tiene un módulo de clasificación y crear uno para 7 líneas rompe unos 10 imports de tests.
- **Unificar UTC−5** (`fuentes.HORA_LOCAL` y `observabilidad.TZ`): resuelven dos cosas distintas, el parseo de la entrada y la hora del log. Bogotá no tiene horario de verano, y acoplar `observabilidad` a `fuentes` por una constante costaría más que el duplicado.
- **Un solo `ConfigDict` pydantic**: no hay un lugar común que pase `test_capas`, porque `config` no importa núcleo y el núcleo solo importa núcleo. Además, el lector de `credenciales.yaml` usa `extra="ignore"`, así que no es la misma constante.
- **Derivar `observabilidad.ETAPAS` de `corrida`**: `ETAPAS` es el contrato de `duracion_s_por_etapa` en `metrics.json`, `bordes` no puede importar `aplicacion` y `Observabilidad.etapa` ya falla con una etapa desconocida.

### Terraform (brief §8) sin Object Lock ni CloudTrail

`infra/` es una raíz plana (S3 `datos` + `auditoria`, tres llaves KMS, roles IAM `ingesta`/`analista`/`auditor`) que solo se valida (`make infra-validar`, imagen de Terraform por digest, lock en `readonly`). No se despliega. Por el recorte de alcance (tabla de arriba), Object Lock COMPLIANCE y CloudTrail quedan como control propuesto en `infra/README.md`. Para compensar en parte, `auditoria` niega `DeleteObject` y `DeleteObjectVersion` a todos, aunque eso no es WORM: un administrador puede cambiar la política. `ingesta` lee también `salidas/*`, porque `make stream` relee `metrics.json` para reemplazar solo `stream`. `retencion_auditoria_dias` no se declaró: sin Object Lock sería una variable que nadie usa.

### Cierre: `make auditar`, `make auditar-salidas` y el threat model

- `make auditar` corre `pip-audit --disable-pip --require-hashes` sobre `requirements.txt` + `requirements-dev.txt`: audita los pins declarados sin instalar nada ni crear un venv temporal. pip-audit entra por `requirements-dev.in` con hashes (ADR 0002), y con él 23 transitivas (requests, rich, cyclonedx...) que van a la imagen porque `make test`/`make lint` corren ahí. Alternativa descartada: un venv aparte solo para auditar. Sería menos superficie en la imagen, pero un tercer archivo de requirements y un camino de instalación que nadie más usa. Necesita red, así que queda fuera de `make test`.
- `make auditar-salidas` (`python -m cauce auditar-salidas out logs`) pasa `pii.detectar_estricto` por cada archivo ya escrito, incluidos `logs/test.log` y las salidas viejas. Sale con 1 nombrando el archivo y los tipos, nunca el valor, y con 2 si falta un directorio: no se certifica lo que no existe. Vive en `salidas` (borde) porque es la misma regla de `escribir_atomico`, aplicada después de escribir. No necesita base ni `estado/.env`.
- El canario de PII de `run` + `stream` juntos extiende el e2e de `make stream` en lugar de duplicarlo: suma el volcado de las 6 zonas como superusuario y `salidas.auditar` sobre ese `out/` y `logs/`.
- `THREAT_MODEL.md` marca "Sí" solo con un test o target citado que exista (las 49 citas se verificaron contra `tests/` y el `Makefile`). T1, T4, T9, T10 y T12 quedan "Parcial", con el porqué en la fila.

### `metrics.json`: conteos de la base, la Corrida en una subclave (cambio del 2026-11-12)

El diseño había fijado `metrics.json` como foto de la Corrida: la segunda `make run` daba `filas_leidas: 0`, `filas_rechazadas_por_motivo: {}` y 12 archivos omitidos. Se cambia porque el operador corre `make run` dos veces y compara (brief §10). Ids, timestamps y duraciones cambian siempre, así que lo comparable son los conteos, y con la foto de la Corrida pasaban de 115.340 a 0: parecía que el pipeline no era idempotente. Además, `dq_report.filas_leidas` (estado de la base) y `metrics.filas_leidas` (Corrida) no coincidían.

Ahora `filas_leidas`, `filas_cargadas`, `filas_rechazadas_por_motivo`, `por_fuente` y `archivos` salen de `fotos.metricas_acumuladas`: `raw.archivo_conteo` y la Cuarentena por motivo de los archivos vigentes, es decir el último cargado de cada `(fuente, nombre)`, para que un maestro reaplicado no cuente dos veces. `archivos.rechazados` cuenta todos los registros rechazados de `raw.archivo`. Lo de la Corrida se conserva entero en `corrida`; `ts_inicio`, `ts_fin` y `duracion_s_por_etapa` siguen siendo de la Corrida, como pide el brief §9.5. Si la Corrida falla antes de las Fotos, no hay lectura de la base y `conteos_de` vale `"corrida"` en vez de `"base"`.

Alternativa descartada: dejar la foto de la Corrida y explicarlo en el README. Es la lectura más literal y la más útil para operar, pero si el operador compara conteos entre las dos corridas o contra `dq_report`, el pipeline parece no idempotente. Con el cambio no se pierde información: el delta sigue en `corrida`.


### `CAUCE_DATA` y `--semilla` del generador

`CAUCE_DATA=ruta` apunta `make datos`, `make run` y `make stream` a otro directorio de datos (en docker se monta en `/app/data`); lo usan los tests y el e2e para trabajar sobre Datos sintéticos en un temporal sin tocar `data/`. Sin la variable, el default de `make run` sigue siendo `data/`. El generador (`tools/sinteticos.py`) acepta `--semilla N`; el default es fijo (`SEMILLA`), así que sin flags la misma versión del generador produce siempre los mismos archivos.
