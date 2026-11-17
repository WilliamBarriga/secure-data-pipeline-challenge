# Modelo de amenazas — pipeline cauce (batch + stream)

STRIDE sobre lo construido: batch, stream e infraestructura local (C8, brief §5). Vocabulario de `GLOSSARY.md`: un **Límite de confianza** es donde un dato cambia de dueño o de nivel de confianza; no tiene nada que ver con una Frontera (punto de medición).

## 1. Alcance y diagrama de flujo de datos

Entra: los archivos de `data/` (maestros, consumos, facturación) y los eventos de `mediciones.raw`. Sale: `out/` (`consumo_mensual.csv`, `dq_report.json`, `metrics.json`, `alertas.json`), `logs/pipeline.log`, Postgres (6 zonas) y la Bitácora. Diagrama navegable: Vista `amenazas` en `docs/arquitectura/arquitectura.dg` (un Paso por Límite; las Aristas etiquetadas `T1`..`T12` ubican cada amenaza).

```
                 L1                              L3                       L4
  data/ (hostil) ──┐                                                ┌── analista (solo la vista)
                   ▼                                                │
  ┌───────────── proceso cauce (docker o .venv) ────────────┐       ├── auditor (solo la Bitácora)
  │ fuentes.leer -> custodia (enmascara, cifra) -> almacen ─┼──────►│ Postgres 16 (127.0.0.1:5432)
  │ corrida_stream -> FuenteEventos -> alertas.Detector     │ ingesta└── operador (superusuario)
  └───────┬──────────────────────────▲──────────────────────┘
          │ out/, logs/ (L1)          │ L2: assign hasta el high watermark
          ▼                           │
     disco del host          Redpanda 127.0.0.1:9092 (sin SASL/TLS) ◄── producer.py / cualquier proceso local
                                                                           
  L5: operador del host: estado/.env (KEK, K_bidx, passwords), credenciales.yaml, volúmenes de Docker, imágenes
```

| # | Límite de confianza | Qué cruza | Lado no confiable |
|---|---|---|---|
| L1 | disco del host ↔ app | bytes de `data/` hacia adentro; `out/` y `logs/` hacia afuera | quien deposita archivos en `data/`; quien lee `out/`/`logs/` |
| L2 | localhost/producer → Redpanda → consumidor | eventos JSON de `mediciones.raw` | cualquier proceso en 127.0.0.1 (el broker no autentica) |
| L3 | app → Postgres (rol `ingesta`) | filas ya custodiadas, eventos de Bitácora | el proceso que lee datos hostiles |
| L4 | consumidores de Postgres | `analista`, `auditor`, operador (superusuario, diagnóstico y verificación de C3) | cualquier rol de lectura |
| L5 | operador del host | `estado/.env`, `credenciales.yaml`, KEK, volúmenes, imágenes, dependencias | quien tiene shell en el host o controla la cadena de suministro |

## 2. Activos y clasificación

Niveles de `data_classification.yaml`.

| Activo | Dónde vive | Clasificación | Impacto si se compromete |
|---|---|---|---|
| PII de clientes (NIT, correo, celular, representante, dirección) | `raw`/`core` como ciphertext AES-256-GCM + blind index; nunca en claro | Restringido-PII | Exposición de datos personales (Ley 1581 de 2012) |
| `observaciones` de clientes | solo Enmascarado (`[EMAIL]`, `[CELULAR]`, `[CEDULA]`); el original no se guarda | Restringido-PII | Igual que el anterior |
| Facturas (`cliente` del payload) | `raw.factura` con el subobjeto cifrado; `core` sin datos de cliente | Restringido-PII / Confidencial | PII y dato facturable alterado |
| Consumos y Facturas en `core` | Postgres | Confidencial | Pérdida de integridad del dato facturable |
| KEK y `K_bidx` | `estado/.env` (0600, fuera de git) | Secreto | Descifrado de toda la PII / ataque de diccionario al blind index |
| DEK | `crypto.dek`, envuelta con la KEK | Secreto | Nulo sin la KEK |
| Bitácora | `audit.bitacora_corrida` (append-only, cadena de hash) | Interno | Repudio de una Corrida; trazabilidad perdida |
| Credenciales de roles | `credenciales.yaml` → `estado/.env` | Secreto (efímero) | Acceso a la base con ese rol |
| Alertas | `out/alertas.json` | Interno | Alertas falsas o suprimidas |

## 3. Amenazas (STRIDE)

**Criterio de probabilidad.** A = explotable desde localhost sin credenciales. M = requiere colocar un archivo en `data/` o credenciales de un rol de la base. B = requiere superusuario, shell en el host o comprometer la cadena de suministro.

**Criterio de impacto.** A = PII en claro o pérdida de integridad del dato facturable (consumos, Facturas). M = Alertas falsas o suprimidas, trazabilidad degradada o un comando del contrato que no termina. B = sin efecto sobre los datos ni sobre el contrato.

**Regla de "¿Implementado?".** "Sí" solo si un test de `make test` o un target (`make auditar`, `make auditar-salidas`) lo prueba y está citado en la fila. Si no, "Parcial" o "No", con el porqué. Los tests se citan como `archivo::test`.

**Mapeo ISO/IEC 27001:2022 Anexo A.** Un control primario y, como máximo, uno secundario, con numeración 2022 (ver fuentes al final).

| # | Límite / componente | STRIDE | Amenaza concreta | Prob. | Impacto | Control | ¿Implementado? | Anexo A 2022 |
|---|---|---|---|---|---|---|---|---|
| T1 | L2 · Redpanda `mediciones.raw` | Spoofing | Un proceso local publica eventos falsos (o reenvía `evento_id`) para generar o suprimir Alertas | A | M | Puerto publicado solo en 127.0.0.1; auto-create de tópicos apagado; validación estricta antes de las reglas (inválidas descartadas y contadas por motivo); dedupe por `evento_id` que solo reserva el id de un evento válido (un inválido con el id de uno real no lo suprime); instante igual al máximo = duplicado; lectura acotada al high watermark de Kafka | **Parcial**: el broker no autentica, así que un evento bien formado y con un `evento_id` nuevo se acepta, y uno bien formado que llega antes con el `evento_id` de uno real lo suprime (riesgo aceptado §4). Probado: `tests/unit/test_endurecimiento.py::test_puertos_solo_en_loopback_y_healthchecks`, `::test_redpanda_sin_autocreacion_de_topicos`, `tests/unit/test_alertas.py::test_invalida_se_descarta_antes_de_las_reglas_y_se_cuenta_por_motivo`, `::test_evento_id_repetido_se_deduplica`, `::test_invalido_con_el_evento_id_de_uno_real_no_lo_suprime` | 8.20 Networks security · 8.22 Segregation of networks |
| T2 | L1 · `fuentes.leer` | Tampering | Un archivo hostil (fórmula CSV, SQL, comillas anidadas, encoding raro, YAML con tags) intenta ejecutarse o corromper la carga | M | A | `csv` de la stdlib (nunca se evalúa una celda); catálogo cerrado de formatos por encabezado exacto; corrupción estructural = rechazo entero; `yaml.safe_load`; SQL siempre parametrizado o `psycopg2.sql.Identifier` | **Sí**: `tests/unit/test_fuentes.py::test_entrada_hostil_es_dato_no_instruccion`, `tests/unit/test_fuentes_cliente.py::test_comillas_anidadas_salto_de_linea_largo_y_sql_se_cargan_como_datos`, `tests/unit/test_fuentes_consumo.py::test_encabezado_fuera_del_catalogo_rechaza_el_archivo`, `::test_archivo_corrupto_se_rechaza_entero`, `tests/unit/test_clasificacion.py::test_yaml_no_mapeo_o_roto_es_esquema_invalido`, `tests/unit/test_almacen_sql.py::test_identificador_invalido_se_rechaza_sin_eco` | 8.28 Secure coding · 8.7 Protection against malware |
| T3 | L1 · `ingesta_archivo` / `raw.archivo` | Tampering | Un archivo de consumos o facturación ya cargado se modifica (mismo nombre, otro contenido) y se re-ingiere en silencio | M | A | Registro por (fuente, nombre, sha256): mismo nombre con otro sha256 = Archivo modificado → rechazo entero a Cuarentena y lo cargado no cambia | **Sí**: `tests/integracion/test_ingesta_archivo.py::test_consumo_modificado_se_rechaza_entero_y_lo_cargado_no_cambia`, `tests/integracion/test_almacen_factura.py::test_archivo_de_facturacion_modificado_se_rechaza_entero`, `tests/e2e/test_consumos.py::test_archivo_corrupto_y_modificado_dejan_todo_igual_con_exit_0` | 8.28 Secure coding |
| T4 | L4/L5 · `audit.bitacora_corrida` | Tampering | El superusuario edita o borra eventos de la Bitácora | B | M | Dueño `cauce_owner` NOLOGIN; REVOKE de UPDATE/DELETE/TRUNCATE; triggers append-only; cadena de hash `sha256(hash_previo ‖ evento)` verificable con `audit.verificar_cadena` | **Parcial**: detecta ediciones y borrados hechos fila a fila, pero no los previene. La cadena no lleva llave, así que un superusuario que deshabilita el trigger y recalcula toda la cola de la cadena no deja rastro (riesgo aceptado §4). Probado: `tests/integracion/test_bitacora.py::test_ni_el_owner_ni_el_superusuario_modifican_la_bitacora`, `::test_edicion_como_superusuario_con_trigger_deshabilitado_se_detecta`, `::test_borrar_una_fila_del_medio_se_detecta` | 8.15 Logging |
| T5 | L3 · `bitacora.registrar` | Repudiation | Una Corrida (batch o stream) carga o falla sin dejar rastro, o el operador niega haberla lanzado | M | M | Bitácora por eventos solo a través de `audit.registrar_evento` (SECURITY DEFINER, `search_path` fijo); `ts` y `usuario_db` los pone el servidor; inicio y fin de cada Corrida, también del stream; catálogo cerrado, sin texto libre | **Sí**: `tests/integracion/test_roles.py::test_ingesta_sobre_la_bitacora_solo_ejecuta_registrar_evento`, `tests/integracion/test_bitacora.py::test_registrar_evento_pone_ts_y_usuario_db_del_servidor`, `tests/e2e/test_smoke_run.py::test_cada_run_queda_en_la_bitacora_con_inicio_y_fin`, `tests/integracion/test_corrida_stream.py::test_la_corrida_del_stream_queda_en_la_bitacora_como_ingesta` | 8.15 Logging · 8.16 Monitoring activities |
| T6 | L1 · `observabilidad`, `salidas` | Information disclosure | PII llega a `logs/` o a `out/` por `str(e)`, el eco de pydantic, el log de `kafka` o un valor de fila | M | A | Tres capas: eventos de catálogo sin texto libre ni `str(e)` (`hide_input_in_errors`); filtro por clave según la clasificación; `pii.detectar_estricto` como red (logs enmascaran y siguen, `out/` falla cerrado). Logs de terceros sin mensaje formateado. Canario de PII sobre `run` + `stream` juntos y auditoría posterior de `out/` y `logs/` | **Sí**: `tests/unit/test_observabilidad.py::test_excepcion_con_pii_en_el_mensaje_no_aparece`, `::test_clave_clasificada_confidencial_se_redacta`, `::test_kafka_warning_no_pasa`, `tests/unit/test_salidas.py::test_rechaza_contenido_con_pii_sin_escribir`, `tests/e2e/test_stream.py::test_run_mas_stream_33_alertas_metrics_bitacora_y_canario`, target `make auditar-salidas` | 8.12 Data leakage prevention · 8.11 Data masking |
| T7 | L4 · rol `analista` | Information disclosure | `analista` llega a la PII por tablas base, `TEMP`, funciones `lo_*`, el esquema `public` o una función con fugas | M | A | Grants mínimos por rol; REVOKE a PUBLIC (CONNECT, TEMP, `public`, EXECUTE de `lo_*` y de `audit`); `analista` solo ve `analytics.v_consumo_mensual` (definer + `security_barrier`, sin columnas de cliente) | **Sí**: `tests/integracion/test_roles.py::test_analista_solo_ve_la_vista_analitica`, `::test_public_sin_connect_temp_ni_schema_public`, `::test_funciones_lo_sin_execute_para_public`, `::test_la_vista_analitica_es_definer_con_security_barrier`, `tests/integracion/test_vista_consumo_mensual.py::test_analista_lee_la_vista_y_no_core` | 8.3 Information access restriction · 5.15 Access control |
| T8 | L3/L5 · volumen de Postgres | Information disclosure | Robo del volumen de Docker o de un `pg_dump`: la PII sale de la base | B | A | AES-256-GCM en la app (envelope: DEK en `crypto.dek` envuelta con la KEK); la KEK vive en `estado/.env`, nunca en Postgres; `ingesta` no puede escribir la DEK | **Sí** frente a volumen o dump sin `estado/.env`: `tests/e2e/test_clientes.py::test_canario_sin_pii_en_tablas_log_ni_out_y_dos_corridas_mismos_conteos` (lee las 6 zonas como superusuario), `tests/integracion/test_almacen_clientes.py::test_nit_en_claro_no_esta_en_ninguna_tabla`, `::test_bootstrap_deja_una_dek_que_la_kek_abre_y_ingesta_no_puede_escribirla`, `tests/unit/test_custodia.py::test_kek_distinta_no_desenvuelve_la_dek`. Con shell en el host, la KEK está al lado (riesgo aceptado §4) | 8.24 Use of cryptography · 5.34 Privacy and protection of PII |
| T9 | L5 · blind index | Information disclosure | Si se filtra `K_bidx` junto con un dump, un ataque de diccionario sobre el blind index revela NITs (espacio chico) | B | A | HMAC-SHA256 con `K_bidx`, llave distinta de la KEK y fuera de Postgres; normalización antes del HMAC | **Parcial**: la separación de llaves está probada (`tests/unit/test_custodia.py::test_bidx_determinista_normalizado_y_dependiente_de_la_llave`), pero si `K_bidx` se filtra el diccionario de NITs es factible: el HMAC no agrega costo por intento. Sin KMS/HSM no hay más defensa | 8.24 Use of cryptography |
| T10 | L5 · `config/pipeline.yaml`, `credenciales.yaml` | Spoofing / Elevation of privilege | Alguien reutiliza el secreto heredado (password de Postgres, llave maestra) desde el historial de git o una copia del repositorio | B | A | El código no lee `config/pipeline.yaml` (gitignored, nunca en el historial); credenciales aleatorias de 24+ caracteres con `make credenciales`; check de passwords débiles o repetidos; compose y `src/` sin credenciales literales | **Parcial**: probado por `tests/unit/test_credenciales.py::test_el_codigo_no_referencia_el_secreto_heredado`, `::test_rechaza_passwords_debiles_nombrando_la_clave_no_el_valor`, `tests/unit/test_endurecimiento.py::test_compose_sin_credenciales_literales`, `::test_el_codigo_no_trae_credenciales_literales`; `git log --all -- config/pipeline.yaml` está vacío. Falta el escaneo de secretos (gitleaks) en CI (§5) | 5.17 Authentication information · 8.4 Access to source code |
| T11 | L5 · dependencias e imágenes | Elevation of privilege | Una dependencia vulnerable (el PyYAML 5.3.1 heredado tenía RCE) o una imagen reemplazada en el registro | B | A | `requirements*.txt` con hashes de todos los archivos de PyPI e instalación `--require-hashes`; imágenes por versión + digest (también la de Terraform); `pip-audit` sobre los requirements fijados | **Sí**: `tests/unit/test_endurecimiento.py::test_requirements_solo_lo_permitido_con_hashes`, `::test_requirements_dev_con_hashes_y_sin_repetir_runtime`, `::test_dockerfile_instala_con_require_hashes`, `::test_compose_sin_minio_e_imagenes_por_version_y_digest`, target `make auditar` (sin vulnerabilidades conocidas al 2026-11-11) | 8.8 Management of technical vulnerabilities · 5.21 Managing information security in the ICT supply chain |
| T12 | L1/L2 · `make run`, `make stream` | Denial of service | Un archivo gigante o un tópico inundado cuelga `make run` o deja `make stream` escuchando después de los 300 s | A | M | Stream: `assign` hasta el high watermark de Kafka tomado al empezar, inactividad de 10 s y tope de 210 s con la conexión incluida (corte = Corrida `parcial`); producer cortado a 30 s; suma < 300 s; `statement_timeout` por rol en Postgres; lock de Corrida que falla rápido | **Parcial**: el stream está acotado (`tests/integracion/test_fuente_kafka.py::test_el_tope_corta_la_lectura_y_lo_informa`, `::test_lo_producido_despues_del_watermark_no_se_lee`, `tests/unit/test_producir.py::test_producer_colgado_se_corta_por_timeout_y_falla`, `tests/integracion/test_corrida_stream.py::test_lectura_cortada_por_la_red_de_seguridad_deja_la_corrida_parcial`, `tests/integracion/test_roles.py::test_analista_y_auditor_son_read_only_con_timeout_a_nivel_de_rol`, `tests/e2e/test_maestros.py::test_corrida_con_el_lock_tomado_falla_de_inmediato_sin_cargar`). Los archivos de `data/` se leen enteros a memoria sin tope de tamaño (`csv.field_size_limit` de 16 MiB por campo): un archivo enorme puede agotar memoria | 8.6 Capacity management |

## 4. Riesgos aceptados

| Riesgo | Por qué se acepta | Qué lo acota |
|---|---|---|
| **KEK y `K_bidx` en variable de entorno** (`estado/.env`) | No hay KMS ni Vault en una evaluación local. Hace falta que `make up` funcione en una máquina limpia sin servicios externos | 0600, fuera de git; nunca en Postgres; se generan solo si faltan; `make clean` las borra. Quien tiene shell en el host (L5) tiene la KEK: T8 cubre el robo del volumen o del dump, no el del host |
| **El superusuario puede reescribir la Bitácora** | Postgres no puede negarle nada a un superusuario. La cadena de hash no lleva llave, así que solo detecta ediciones fila a fila (T4) | Ningún login de la app es superusuario; `cauce_owner` es NOLOGIN; triggers y REVOKE frenan a todos los demás. Para que sea WORM de verdad hace falta un almacenamiento externo (ver §5) |
| **Credenciales en claro en `credenciales.yaml` y `estado/.env`** | No hay gestor de secretos en una corrida local, y `make up` las necesita para levantar la infra | Fuera de git; aleatorias, 24+ caracteres y distintas por rol; Postgres solo en loopback; `make credenciales ROTAR=1` las rota (`SECURITY.md` §5) |
| **Redpanda sin SASL/TLS** | El producer (`tools/producer.py`) no autentica y el broker es local; SASL suma llaves y configuración que una corrida local no justifica | Solo loopback, auto-create apagado y validación + dedupe antes de las reglas (T1). El stream no persiste nada en Postgres fuera de la Bitácora |
| **El operador verifica C3 como superusuario** | Leer las tablas crudas como superusuario es la comprobación de C3 (brief §5) | Solo lectura sobre lo que ya existe; el pipeline nunca usa ese login; la credencial es local y aleatoria |
| **Archivos de `data/` sin tope de tamaño** (T12) | Los archivos de entrada son chicos y `data/` lo controla el operador | El rechazo entero por corrupción y el tope por campo de `csv` acotan los casos patológicos de parseo, no el tamaño |
| **Sin escaneo de secretos todavía** (T10) | Va en CI (§5), que este repositorio no tiene | El secreto heredado nunca entró al historial y el código no lo lee |

## 5. Qué haría distinto con más tiempo / en producción real

- **Llaves en KMS o Vault**: la KEK envuelve la DEK con `kms:Encrypt` y nunca sale del HSM; `K_bidx` es una llave HMAC de KMS (`infra/kms.tf` ya declara `kms_pii` y `kms_bidx`). Rotación de la DEK con `key_version`, que ya está en el modelo.
- **SASL/SCRAM + TLS en Redpanda** (o MSK con IAM), con ACLs por tópico: solo el producer escribe en `mediciones.raw` y solo el consumidor lee. Firma de eventos en el origen si las Alertas disparan acciones.
- **Bitácora WORM externa**: exportar cada Corrida a S3 con Object Lock COMPLIANCE y CloudTrail con data events (propuesto en `infra/README.md`, recortado por [alcance](DECISIONS.md#decisiones-de-diseño-antes-de-construir)), y anclar el hash de cabeza de la cadena fuera de Postgres, o pasar a una cadena con HMAC cuya llave el DBA no tenga.
- **pgaudit** para registrar las sesiones del superusuario y de `analista`, con los logs en un destino que el DBA no controle.
- **Escaneo en CI**: `make auditar` (pip-audit), escaneo de imágenes (por ejemplo Trivy), gitleaks y `make auditar-salidas` en cada PR; Dependabot o Renovate para los pins.
- **Rotación automática de credenciales** con un gestor de secretos y credenciales dinámicas de Postgres (Vault database engine), en lugar de `credenciales.yaml`.
- **Tope de tamaño por archivo** y lectura en streaming en `fuentes`, con un rechazo `archivo_demasiado_grande` en el catálogo de errores.
- **Segregación de red**: una red de compose interna para Postgres y Redpanda, sin puertos publicados en el host salvo lo que necesita el operador.

## Fuentes del mapeo ISO/IEC 27001:2022

- Norma: ISO/IEC 27001:2022, Anexo A (93 controles en 4 temas): https://www.iso.org/standard/27001
- Títulos de los controles del Anexo A 2022 (5.15, 5.17, 5.21, 5.34, 8.3, 8.4, 8.6, 8.7, 8.8, 8.11, 8.12, 8.15, 8.16, 8.20, 8.22, 8.24, 8.28): Advisera, "ISO 27001 Annex A controls": https://advisera.com/iso27001/annex-a-controls/
- Controles nuevos de 2022 (8.11 Data masking, 8.12 Data leakage prevention, 8.16 Monitoring activities, 8.28 Secure coding): ISACA, "A Guide to the Updated ISO/IEC 27002:2022 Standard, Part 2": https://www.isaca.org/resources/news-and-trends/newsletters/atisaca/2023/volume-12/a-guide-to-the-updated-iso-iec-27002-2022-standard-part-2
