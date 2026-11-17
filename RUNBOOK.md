# Runbook operativo

Para quien lo abra a las 2 a.m. Términos en `GLOSSARY.md`; controles en `SECURITY.md`.

Acceso rápido a la base (credenciales en `credenciales.yaml` o `estado/.env`):

```bash
docker compose --env-file estado/.env exec postgres psql -U cauce_admin -d cauce_datos   # superusuario: solo diagnóstico
```

## Cómo corre normalmente

| Comando | Qué hace | Duración | Qué deja |
|---|---|---|---|
| `make up` | Genera `credenciales.yaml` si falta, renderiza `estado/.env` desde ese archivo, levanta Postgres y Redpanda, aplica roles y DDL (idempotente por huella) | < 1 min | Infra arriba y sana |
| `make run` | Corrida batch: maestros → consumos → facturación → salidas | ~15 s con la entrada de referencia (tope del contrato: 900 s) | `out/consumo_mensual.csv`, `out/dq_report.json`, `out/metrics.json`, líneas en `logs/pipeline.log` y eventos en `audit.bitacora_corrida` |
| `make stream` | Crea el tópico si falta, produce el ndjson y consume hasta el high watermark; termina solo | segundos (tope: 300 s) | `out/alertas.json`, subclave `stream` de `metrics.json` |

Propiedades que importan para operar:

- **Un archivo, una transacción.** Cada archivo se registra en `raw.archivo` con su sha256. En la siguiente Corrida, el mismo nombre con el mismo sha256 se omite: `make run` es idempotente y no recarga nada desde cero.
- **Una Corrida a la vez.** Un advisory lock lo garantiza; una segunda Corrida concurrente falla con `corrida_concurrente` y no toca nada.
- **Nada se borra.** `ingesta` no tiene `DELETE` ni `TRUNCATE`. Lo rechazado queda en `quarantine.rechazo` con su motivo.
- **Salida de `make run`:** `0` si terminó, aunque haya rechazado archivos o filas; `2` si falló, con `codigo_error` en stderr, en el log y en la Bitácora.

## Señales de que algo anda mal

| Síntoma | Dónde se ve | Causa probable |
|---|---|---|
| `make run` sale con 2 y `infraestructura_no_disponible` | stderr, `logs/pipeline.log` | Postgres o Redpanda caídos, o el puerto ocupado por otro proceso |
| `configuracion_invalida` | stderr | `estado/.env` ausente o con una KEK que no abre la DEK, password débil o repetido en `credenciales.yaml`, o volumen viejo con otra huella de DDL |
| `clasificacion_incompleta` | stderr y Bitácora | Llegó un archivo con una columna nueva, o cambió el DDL sin actualizar `data_classification.yaml` |
| `pii_en_salida` | stderr y Bitácora | El detector estricto encontró PII en un archivo de `out/`, que no se escribió. **Tratar como incidente** (ver abajo) |
| `corrida_concurrente` | stderr | Ya hay otra Corrida corriendo (o colgada con la conexión abierta) |
| Archivo con `estado = 'rechazado'` en `raw.archivo` | Bitácora (`codigo_error` `archivo_corrupto` o `archivo_modificado`), log `archivo_rechazado` | Archivo truncado o con bytes inválidos, formato desconocido, o un archivo ya cargado que cambió de contenido |
| `filas_rechazadas_por_motivo` crece de golpe | `out/metrics.json`, `quarantine.rechazo` | Cambio de formato en origen, o un lote con unidad o zona horaria inesperada |
| `horas_sin_lectura_valida_total` sube | `out/dq_report.json` | Una Frontera dejó de reportar o se rechazaron sus lecturas (B4) |
| WARNING `pii_filtrada` en el log | `logs/pipeline.log`, `metrics.json → pii_filtrada_en_logs` | La red de seguridad de logs enmascaró algo: hay un evento que está logueando un valor que no debería |
| `audit.verificar_cadena()` devuelve un id | consulta como `auditor` | Alguien editó o borró la Bitácora saltándose grants y triggers. **Incidente** |

Consultas útiles:

```sql
-- últimas Corridas y cómo terminaron (como auditor)
SELECT corrida_id, comando, evento, ts, resultado, codigo_error, fuente, filas_leidas, filas_cuarentena
FROM audit.bitacora_corrida ORDER BY evento_id DESC LIMIT 30;

-- archivos rechazados
SELECT archivo_id, fuente, nombre, estado, corrida_id FROM raw.archivo WHERE estado = 'rechazado';

-- Cuarentena por archivo y motivo
SELECT a.nombre, r.motivo, count(*) FROM quarantine.rechazo r JOIN raw.archivo a USING (archivo_id)
GROUP BY 1, 2 ORDER BY 3 DESC;

-- integridad de la Bitácora (NULL = íntegra)
SELECT audit.verificar_cadena();
```

## Procedimientos

### El pipeline falló a mitad de camino

**Es seguro reintentar `make run`.** Cada archivo se carga en su propia transacción: los ya confirmados quedan, el que estaba en curso se revierte entero y la siguiente Corrida lo procesa como Nuevo. Lo ya cargado se omite por sha256, y `out/` se recalcula desde la base, no sumando deltas.

1. Leer el `codigo_error` (stderr, o el último `corrida_fin` en el log o en la Bitácora).
2. Si es `infraestructura_no_disponible`: `docker compose --env-file estado/.env ps`, después `make up` (idempotente).
3. Si es `corrida_concurrente`: confirmar que no hay otra Corrida viva (`SELECT pid, usename, state, query_start FROM pg_stat_activity WHERE usename = 'ingesta';`). Si quedó una colgada, `SELECT pg_terminate_backend(<pid>);`: su transacción se revierte sola.
4. Si es `configuracion_invalida` con "la KEK no desenvuelve la DEK": **no** regenerar `estado/.env`. La KEK es la única llave que abre lo cifrado; si se perdió, la PII cifrada se perdió con ella. Recuperar `estado/.env` del respaldo.
5. Reintentar `make run` y confirmar `resultado = exito` en la Bitácora.

### Llegaron datos corregidos de un mes ya cerrado

- La corrección entra como **archivo nuevo** en `data/consumos/` (por ejemplo `consumos_2026-04_correccion2.csv`), con `version_lectura` mayor que la de la lectura que corrige. `make run` la absorbe sin recargar el mes: gana la versión mayor y, si empatan, el archivo posterior en orden lexicográfico (B3). Queda contada en `correcciones_tardias_aplicadas`.
- **No editar un archivo ya cargado.** El mismo nombre con otro sha256 se rechaza entero (`archivo_modificado`) y los datos cargados no cambian. Es a propósito: sin esto, cualquiera que pueda escribir en `data/` reescribiría la historia en silencio.
- Verificar: el mes corregido cambia en `out/consumo_mensual.csv` y los demás meses no.

### La tasa de rechazo se disparó

1. `out/metrics.json → filas_rechazadas_por_motivo` muestra el total de la base; `metrics.json → corrida.filas_rechazadas_por_motivo`, solo lo que trajo la última Corrida. Si `conteos_de` dice `corrida`, la Corrida falló antes de leer la base y los dos niveles muestran lo mismo.
2. Ubicar el archivo y el motivo con la consulta de Cuarentena de arriba.
3. Según el motivo:
   - `archivo_corrupto:*` o `formato_desconocido`: el archivo entero se rechazó y los meses ya cargados no cambiaron. Pedir el reenvío al origen. Un formato nuevo legítimo se agrega como perfil en `fuentes.FICHAS` (`src/cauce/nucleo/fuentes.py`), test-first.
   - Nulos, negativos u outliers (B4): es dato malo en origen. Se reporta y no se imputa.
   - `frontera_huerfana` (facturación): falta la Frontera en `maestros/fronteras.csv`. Corregir el maestro y reprocesar.
4. En las filas de clientes y facturas, el cuerpo de la Cuarentena va cifrado (`payload_cifrado`). Para depurarlo hay que descifrar con la KEK, en un entorno controlado; **nunca pegar el contenido en tickets ni en chat**.

### Sospecha de acceso indebido a PII

Primeros 15 minutos:

1. **Contener sin destruir evidencia.** No reiniciar Postgres, no borrar `logs/`, no hacer `make clean` ni `docker compose down -v`.
2. **Mirar:**
   - `SELECT usename, client_addr, backend_start, state, query FROM pg_stat_activity;` para ver quién está conectado ahora.
   - `SELECT audit.verificar_cadena();` y las últimas filas de la Bitácora: `usuario_db` y `ts` los fija el servidor, no se pueden falsificar desde la aplicación.
   - `make auditar-salidas` para confirmar si hay PII en `out/` o `logs/`.
   - Quién pudo leer `estado/.env` (la KEK). Sin la KEK, un dump solo trae ciphertext; con la KEK, todo lo cifrado está comprometido.
3. **Cortar el acceso:** `make credenciales ROTAR=1 && make up` rota los passwords de los cuatro logins y los reaplica. Si se filtró la KEK, escalar: hay que re-cifrar con una DEK nueva (el esquema soporta `key_version`, pero el procedimiento de rotación de llaves **no está automatizado**).
4. **Avisar** al responsable de seguridad y al oficial de protección de datos personales: la Ley 1581 obliga a reportar incidentes ante la SIC.
5. **No hacer:** no copiar valores de PII a tickets, chat ni correo para "mostrar" el hallazgo (citar tabla, columna y conteo); no descifrar datos para investigar sin autorización; no "arreglar" la Bitácora.

## Contactos y escalamiento

| Nivel | Quién | Cuándo |
|---|---|---|
| 1 | On-call del equipo de Data | Cualquier fallo de `make run` o `make stream` |
| 2 | Líder del equipo de Data | Datos de facturación afectados, o un fallo que no se resuelve en 1 h |
| Seguridad | Responsable de seguridad de la información y oficial de protección de datos personales | `pii_en_salida`, cadena de Bitácora rota, sospecha de acceso indebido o pérdida de la KEK: **siempre, de inmediato** |

## Rotar credenciales

`credenciales.yaml` no se versiona: `make up` lo genera con passwords aleatorios si falta y no lo toca si existe.

```bash
make credenciales ROTAR=1   # reescribe todos los passwords de credenciales.yaml
make clean && make up       # recrea el volumen con los nuevos y regenera estado/.env
```

Los roles ya creados en un volumen existente conservan el password anterior: tras rotar hay que recrear el volumen (`make clean` borra los datos) o cambiarlos con `ALTER ROLE` como superusuario.
