# Seguridad de la solución

Resumen de los controles de seguridad y gobernanza (brief §5, C1-C8) tal como quedaron construidos. Las amenazas que cubren y su mapeo al Anexo A de ISO/IEC 27001:2022 están en `THREAT_MODEL.md`; las decisiones y alternativas descartadas, en `DECISIONS.md` y `docs/adr/`.

## 1. Clasificación de datos (C1)

`data_classification.yaml` es un artefacto vivo: la unidad es el campo lógico de la fuente (`cliente.nit`, `factura.cliente.email`…) con su nivel (`Público`, `Interno`, `Confidencial`, `Restringido-PII`) y las columnas físicas donde termina. La política sale del nivel (`src/cauce/nucleo/clasificacion.py`):

| Nivel | Política |
|---|---|
| `Restringido-PII` | Se cifra antes de la primera escritura, en cualquier zona; nunca llega a `analytics`; el valor no va a logs ni a `out/`. |
| `Confidencial` | En claro en Postgres; nunca llega a `analytics`; el valor no va a logs ni a `out/`. |
| `Interno` / `Público` | Sin restricción (`Público` marca lo publicable fuera de la comercializadora). |

El pipeline lo consulta en cada `make run`, `make stream` y `make test`, y **falla cerrado** con `codigo_error = clasificacion_incompleta` (registrado en la Bitácora) si:

- un header que trae un archivo no está clasificado (ni como campo ni como alias);
- una columna física de las seis zonas no aparece exactamente una vez (en `fisicas` o en `tecnicas`), o el yaml nombra una columna que no existe;
- un campo `Restringido-PII` cae en una columna de texto.

Tests: `tests/unit/test_clasificacion.py` y `tests/integracion/test_clasificacion_esquema.py` (esquema real totalmente mapeado; una columna nueva hace fallar).

## 2. Cifrado en reposo (C3)

ADR `docs/adr/0001-pii-cifrada-desde-raw-y-minimizada.md`. Cifrado a nivel de aplicación, con envelope encryption:

- **Algoritmo:** AES-256-GCM (`cryptography`), nonce aleatorio de 12 bytes por valor. La AAD es `tabla|columna|llave natural`, así que un ciphertext copiado a otra fila o columna no descifra (`src/cauce/nucleo/custodia.py:114`).
- **Llaves:** una DEK aleatoria por `key_version`, guardada **envuelta** con la KEK en `crypto.dek`. La KEK (`CAUCE_KEK`) y la llave del blind index (`CAUCE_K_BIDX`) son distintas, viven solo en `estado/.env` (0600, fuera de git) y `make up` las genera una sola vez. Quien tenga un dump de la base tiene ciphertext y una DEK envuelta, nunca una llave usable.
- **Alcance:** desde `raw`, no solo en `core`. Las columnas `Restringido-PII` son `*_cifrado bytea` en `raw.cliente`, `raw.factura` y `core.dim_cliente`, y el cuerpo de las filas de Cuarentena de clientes y facturas va en `quarantine.rechazo.payload_cifrado`. Las filas de consumo van en claro porque no traen PII.
- **Joins sin exponer el valor:** blind index HMAC-SHA256 del NIT normalizado (solo dígitos) en `core.dim_cliente.nit_bidx`. Las facturas embebidas resuelven el cliente por `frontera_id → dim_frontera.cliente_id`, y los datos de cliente que traen (NIT, correo, razón social) no se guardan en `core` (minimización, Ley 1581).
- **Idempotencia:** el ciphertext cambia en cada Corrida, así que los upserts van por llave natural o por blind index, nunca comparando ciphertext.
- **Falla cerrado:** una KEK equivocada no desenvuelve la DEK. La Corrida aborta antes de cargar cualquier fila, con `configuracion_invalida` en la Bitácora y una instrucción en stderr.

Verificación como superusuario: `tests/integracion/test_almacen_clientes.py::test_nit_en_claro_no_esta_en_ninguna_tabla` y el canario de `tests/e2e/test_clientes.py` (un NIT, un correo y un celular conocidos no aparecen en ninguna tabla de ninguna zona, ni en logs ni en `out/`).

Riesgos residuales: T8 (KEK en un archivo, sin KMS ni HSM) y T9 (diccionario sobre el blind index si se filtra `K_bidx`), en `THREAT_MODEL.md`.

## 3. Control de accesos, menor privilegio (C4)

Grants por objeto en `sql/05_grants.sql`; endurecimiento que exige superusuario en `src/cauce/arranque/bootstrap.py` (`_endurecer`, `_rol_login`). Se reaplica en cada `make up`.

| Rol | Puede | No puede |
|---|---|---|
| `cauce_owner` | NOLOGIN. Es dueño de todos los objetos (el DDL corre con `SET LOCAL ROLE cauce_owner`). | Hacer login. Como ningún login es dueño, ninguno puede hacer `ALTER`, `DROP` ni `DISABLE TRIGGER`. |
| `ingesta` | `SELECT, INSERT` en `raw` y `quarantine` (append-only); `SELECT, INSERT, UPDATE` en `core` (upserts SCD1); `SELECT` de la DEK **envuelta**; `EXECUTE audit.registrar_evento`. | `DELETE` o `TRUNCATE` en cualquier zona; `UPDATE` en `raw`, `quarantine` o `crypto`; tocar la tabla de Bitácora directamente; usar `analytics`. Sin la KEK, leer `crypto.dek` no le sirve para descifrar: la KEK solo la tiene el proceso. |
| `analista` | `SELECT` sobre `analytics.v_consumo_mensual`. | Cualquier tabla base (cero grants en `core`), escribir o crear objetos. La sesión es `default_transaction_read_only` y tiene `statement_timeout`. |
| `auditor` | `SELECT` sobre `audit.bitacora_corrida`; ejecutar `audit.verificar_cadena` y `audit.hash_evento`. | El resto de las zonas, y escribir. Es read-only por rol. La Bitácora no tiene columnas de texto libre ni PII. |
| `cauce_admin` (superusuario) | Bootstrap y la verificación de C3 por parte del operador. | No lo usa el pipeline: `make run` y `make stream` corren como `ingesta`. |

Los tres roles de login son `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT` y tienen `CONNECTION LIMIT`. A `PUBLIC` se le revoca `CONNECT`/`TEMP` sobre la base, el esquema `public`, las funciones `lo_*` (crear large objects es escribir) y `EXECUTE` por defecto sobre las funciones del owner.

`analytics.v_consumo_mensual` es una vista *definer* con `security_barrier`: corre con los privilegios del owner, así que el analista no necesita grants en `core`, y de `dim_cliente` solo expone `cliente_id`.

`postgres/pg_hba.conf` propio: solo `scram-sha-256`, sin `trust` ni `replication`. El puerto se publica solo en `127.0.0.1`.

Tests, conectándose con cada rol e intentando lo prohibido: `tests/integracion/test_roles.py` (14 casos).

## 4. Enmascaramiento de PII en texto libre (C2) y en salidas

- **`observaciones` de `clientes.csv`:** `pii.enmascarar` (`src/cauce/nucleo/pii.py`) reemplaza correos, celulares colombianos (con o sin `+57`, con o sin separadores) y cédulas (`CC`, `cédula`, y como fallback cualquier racha de 7 dígitos o más) por `[EMAIL]`, `[CELULAR]` y `[CEDULA]`. Es agresivo a propósito: es preferible enmascarar de más que de menos. Corre en la aplicación **antes** de la primera escritura: el original no existe en ninguna zona, ni siquiera cifrado (ADR 0001). La cantidad de clientes afectados sale en `clientes_con_pii_en_texto_libre` de `out/dq_report.json`.
- **Logs:** llevan nombres de campos y conteos, nunca valores de fila. Como red de seguridad, cada línea pasa por el detector estricto (`pii.detectar_estricto`/`enmascarar_estricto`, que no confunde `frontera_id` ni los uuid de Corrida con PII) antes de escribirse, y lo que enmascara queda contado en `metrics.json → pii_filtrada_en_logs`.
- **`out/`:** antes de escribir, cada archivo pasa por el detector estricto. Si encuentra PII, no lo escribe y la Corrida falla con `pii_en_salida`.
- **Auditoría manual:** `make auditar-salidas` recorre `out/` y `logs/` ya escritos y reporta archivo y tipo, nunca el valor.

Tests: `tests/unit/test_pii_enmascarar.py`, `tests/unit/test_pii_estricto.py`, `tests/unit/test_salidas.py` y el canario e2e de `tests/e2e/test_clientes.py`.

## 5. Gestión de secretos

- **Credenciales locales y efímeras.** Viven en `credenciales.yaml`, que está en `.gitignore`: el repositorio no trae ninguna. `make up` lo genera si falta, con el mismo mecanismo que `make credenciales`: cada password `TODO` pasa a 24+ caracteres aleatorios (`secrets.token_urlsafe`) preservando comentarios; `make credenciales ROTAR=1` reescribe todas. `make clean` no las borra: borra los volúmenes y `estado/.env`, y el siguiente `make up` vuelve a renderizar `estado/.env` desde el mismo `credenciales.yaml`.
- `make up` renderiza `estado/.env` (0600, fuera de git) desde `credenciales.yaml` con `yaml.safe_load` y falla nombrando la **clave**, nunca el valor, si un password es `TODO`, `cambiame`, `postgres`, `minioadmin`, mide menos de 24, o se repite entre usuarios. El código **no lee** `config/pipeline.yaml` ni compara contra el secreto heredado: las credenciales nuevas son aleatorias y por eso no pueden coincidir con él.
- Compose no trae credenciales: `POSTGRES_USER`/`POSTGRES_PASSWORD` salen de `estado/.env`.
- En producción irían a un gestor de secretos con rotación y no a un archivo (`THREAT_MODEL.md` §5).

## 6. Trazabilidad y auditoría (C5)

`audit.bitacora_corrida` registra cada Corrida (batch y stream) con eventos de inicio, por archivo y de cierre: quién (`usuario_db` = `session_user`, `ejecutor`), qué (`comando`, `fuente`, `archivo_id`, `version_codigo`), cuándo (`ts`), cuántas filas (leídas, cargadas, en cuarentena, duplicadas) y con qué resultado (`resultado` y `codigo_error` de un catálogo cerrado, nunca texto libre). Append-only en tres capas (`sql/04_auditoria.sql`):

1. **Grants:** `UPDATE`, `DELETE` y `TRUNCATE` revocados de `PUBLIC` y **del owner**. `ingesta` no tiene ningún privilegio sobre la tabla: solo escribe a través de `audit.registrar_evento` (`SECURITY DEFINER`, `search_path` fijo).
2. **Triggers:** `UPDATE`/`DELETE` por fila y `TRUNCATE` por sentencia hacen `RAISE`, incluso para el superusuario.
3. **Cadena de hash:** un trigger `BEFORE INSERT` fija `ts` y `usuario_db` desde el servidor (no se pueden falsificar) y encadena `hash = sha256(hash_previo || contenido canónico)` bajo un advisory lock. `audit.verificar_cadena()` devuelve el primer eslabón roto: detecta lo que un superusuario puede hacer saltándose las capas 1 y 2 (deshabilitar el trigger y editar, o borrar una fila del medio).

Tests: `tests/integracion/test_bitacora.py`.

## 7. Dependencias
`requirements.txt` se genera desde `requirements.in`: las versiones se resuelven en un venv descartable y los sha256 de todos los archivos publicados de cada versión se toman de la API JSON de PyPI (`pip-compile --generate-hashes` quedó colgado con `cryptography`; ver `AI_USAGE.md` §3). Se instala con `pip install --require-hashes`. Solo: psycopg2-binary, kafka-python, cryptography, PyYAML 6.0.3 y pydantic (más sus transitivas con hash). pytest, ruff y pip-audit van en `requirements-dev.txt`, también con hashes y `--require-hashes`. `make auditar` corre pip-audit sobre ambos archivos sin instalar nada; `make auditar-salidas` busca PII con el detector estricto en el `out/` y `logs/` reales. Amenazas y controles: `THREAT_MODEL.md`. Se eliminaron `requests`, `urllib3`, `Jinja2` y `pandas` (sin uso; ver ADR 0002).

## 8. Hallazgos de la auditoría del scaffolding

Valores literales omitidos a propósito.

| # | Hallazgo | Severidad | Remediación |
|---|---|---|---|
| 1 | `config/pipeline.yaml`: password de Postgres heredado y llave maestra (`algoritmo: none`, el "cifrado" no cifra nada). | Crítica | Tratados como comprometidos: no se usan en ningún lado. El archivo está en `.gitignore`, **nunca estuvo en el historial** (`git log --all -- config/pipeline.yaml` vacío) y no se publica en este repositorio. El código no lee el archivo (un test lo verifica); las credenciales nuevas son aleatorias, así que no pueden coincidir. |
| 2 | `config/pipeline.yaml`: credenciales por defecto de MinIO y un `slack_webhook`. | Alta/Media | MinIO sale del stack; ningún secreto se lee de ese archivo. |
| 3 | `docker-compose.yml`: `POSTGRES_PASSWORD` y `MINIO_ROOT_*` hardcodeados con valores triviales. | Alta | Variables desde `estado/.env`; MinIO eliminado. |
| 4 | `docker-compose.yml`: puertos publicados a `0.0.0.0` (Docker salta el firewall del host). | Alta | `127.0.0.1:<puerto>`. |
| 5 | `docker-compose.yml`: imágenes `:latest` (`minio/minio` ya no se puede bajar: distribución solo como fuente desde 2025-10). | Alta | `postgres:16.15-alpine`, `redpanda:v26.1.20` y `python:3.13-slim` por versión + digest. |
| 6 | Sin healthchecks, sin `no-new-privileges`, Redpanda con auto-creación de tópicos. | Media/Baja | Healthchecks (`pg_isready`, `rpk cluster health`), `no-new-privileges`, `auto_create_topics_enabled=false`, contenedor `app` con rootfs de solo lectura, `cap_drop: ALL` y `--user uid:gid`. |
| 7 | Sin `pg_hba.conf` propio. | Media | `postgres/pg_hba.conf`: solo `scram-sha-256`. |
| 8 | `requirements.txt`: PyYAML 5.3.1 (CVE-2020-14343, RCE crítico); `requests` 2.19.1, `urllib3` 1.24.1 y `Jinja2` 2.11.2 con múltiples CVE y sin uso; falta el cliente Kafka que `tools/producer.py` importa. | Crítica/Alta | PyYAML 6.0.3; dependencias muertas eliminadas; kafka-python 3.0.13 (fuera del rango afectado por CVE-2026-10142 y CVE-2026-10143, DoS del cliente corregidos desde 2.3.2: https://github.com/advisories/GHSA-m3px-q5gj-j9x7, https://github.com/advisories/GHSA-2jcm-hq8r-84wx); todo con hashes. |
| 9 | `.env.example`: passwords `cambiame`, credenciales de MinIO y nombres de rol que no son los del manifiesto. | Baja | Reescrito con las claves vacías que genera `make up`; el pipeline nunca lo lee. `secretos.validar_password` rechaza `cambiame`. |

Fuentes: OSV (https://osv.dev), GitHub Advisory Database, PyPI.

## 9. Cómo verificar todo lo anterior

```bash
make datos && make setup && make up && make run   # up genera credenciales.yaml si falta
make test                       # incluye los controles estáticos de compose, pg_hba y requirements
make e2e                        # contrato completo en Docker desde out/ y logs/ ausentes
make auditar                    # pip-audit sobre requirements*.txt (necesita red)
make auditar-salidas            # sin PII en out/ y logs/ (detector estricto)
docker compose config | grep -c '127.0.0.1'                  # puertos solo en loopback
git log --all --oneline -- config/pipeline.yaml              # vacío: nunca entró al historial
PGPASSWORD=... psql -h localhost -U cauce_admin cauce_datos -c '\dn'   # 6 zonas
```
