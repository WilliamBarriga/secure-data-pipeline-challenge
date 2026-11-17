"""Borde con I/O hacia Postgres: las Fotos de `out/` (`dq_report.json` y `consumo_mensual.csv`).

Una Foto se calcula entera desde el estado de la base (todos los archivos presentes), no acumulada Corrida a
Corrida: dos llamadas a `materializar_salidas` sobre la misma base dan los mismos bytes. Solo lee; la carga es de
`almacen`, que comparte con este modulo el orden B3 (`almacen.ORDEN_B3`). Los contadores B y el de PII se leen
por los nombres que declara el nucleo (`fuentes.CONTADORES_B`, `custodia.CLIENTES_CON_PII`), no por literal.
Orden del modulo: SQL de dq_report -> SQL de consumo_mensual -> una funcion por Foto -> `materializar_salidas`.
"""
from __future__ import annotations

import csv
import io
import json

from cauce.bordes.almacen import ORDEN_B3
from cauce.nucleo import custodia, fuentes

# B3 sobre lo cargado: cada Lectura sobrante (candidata que no gano) cae en un solo cubo respecto de la ganadora.
# Correccion tardia: version menor que la ganadora (cuenta una hora por llave, no por sobrante). Exacto: misma
# version y contenido normalizado identico (numeric compara sin ceros de mas). Conflictivo: misma version, otro
# contenido. Solo hay candidatas validas en raw.lectura_consumo, asi que v1 o v2 invalida nunca cuenta.
_SQL_CUBOS = f"""
WITH g AS (
    SELECT l.frontera_id, l.ts_hora, l.consumo_kwh, l.version_lectura, l.calidad,
           row_number() OVER (PARTITION BY l.frontera_id, l.ts_hora ORDER BY {ORDEN_B3}) AS rn
    FROM raw.lectura_consumo l JOIN raw.archivo a USING (archivo_id)
), p AS (
    SELECT s.*, w.version_lectura AS v_gana, w.consumo_kwh AS k_gana, w.calidad AS c_gana
    FROM g s JOIN g w ON w.frontera_id = s.frontera_id AND w.ts_hora = s.ts_hora AND w.rn = 1
    WHERE s.rn > 1
)
SELECT
    count(*) FILTER (WHERE version_lectura = v_gana AND consumo_kwh = k_gana
                           AND calidad IS NOT DISTINCT FROM c_gana),
    count(*) FILTER (WHERE version_lectura = v_gana
                           AND NOT (consumo_kwh = k_gana AND calidad IS NOT DISTINCT FROM c_gana)),
    count(DISTINCT (frontera_id, ts_hora)) FILTER (WHERE version_lectura < v_gana)
FROM p
"""

# Suma de los contadores por archivo. Consumos: archivos aceptados. Clientes: solo la ultima version de cada
# nombre (el maestro se reaplica como upsert; sumar todas las versiones contaria dos veces).
_SQL_CONTEOS = """
SELECT c.contador, sum(c.valor)::bigint
FROM raw.archivo_conteo c JOIN raw.archivo a USING (archivo_id)
WHERE a.fuente = 'consumo' AND a.estado = 'cargado'
   OR (a.fuente = 'cliente' AND c.contador = %(clientes_con_pii)s AND a.estado = 'cargado'
       AND a.archivo_id = (SELECT max(archivo_id) FROM raw.archivo x
                           WHERE x.fuente = 'cliente' AND x.nombre = a.nombre AND x.estado = 'cargado'))
GROUP BY a.fuente, c.contador
"""

_SQL_HUECOS = """
SELECT f.frontera_id, count(h.ts_hora)
FROM (SELECT frontera_id FROM core.dim_frontera UNION SELECT frontera_id FROM core.fact_consumo_horario) f
LEFT JOIN core.v_hueco h USING (frontera_id)
GROUP BY f.frontera_id
"""

# B6: Fronteras distintas (no Facturas) entre las huerfanas. Cuarentena no tiene frontera_id: sale de raw.factura, que
# no lleva PII, por (archivo, linea).
_SQL_FRONTERAS_HUERFANAS = """
SELECT count(DISTINCT f.frontera_id)
FROM quarantine.rechazo q
JOIN raw.factura f ON f.archivo_id = q.archivo_id AND f.numero_linea = q.linea
WHERE q.fuente = 'factura' AND q.motivo = 'frontera_huerfana'
"""

_SQL_CONSUMO_MENSUAL = """
SELECT frontera_id, to_char(mes_local, 'YYYY-MM'), round(sum(consumo_kwh), 3)::text, count(*)
FROM core.fact_consumo_horario
GROUP BY frontera_id, mes_local
ORDER BY frontera_id COLLATE "C", mes_local
"""


# metrics.json (ver DECISIONS.md): conteos de los archivos vigentes, es decir el ultimo cargado de cada
# (fuente, nombre). Un maestro reaplicado con el mismo nombre cuenta una vez; un consumo modificado se rechaza, asi
# que su version vigente sigue siendo la primera. Mismos contadores que `almacen.conteos_de_archivo`.
_SQL_VIGENTES = """
WITH vigente AS (
    SELECT max(archivo_id) AS archivo_id FROM raw.archivo WHERE estado = 'cargado' GROUP BY fuente, nombre
)
"""

_SQL_ACUMULADO_FUENTES = _SQL_VIGENTES + """
SELECT a.fuente, c.contador, sum(c.valor)::bigint
FROM vigente v JOIN raw.archivo a USING (archivo_id) JOIN raw.archivo_conteo c USING (archivo_id)
WHERE c.contador IN ('leidas', 'cargadas', 'rechazadas')
GROUP BY a.fuente, c.contador
"""

_SQL_ACUMULADO_MOTIVOS = _SQL_VIGENTES + """
SELECT r.motivo, count(*)
FROM vigente v JOIN quarantine.rechazo r USING (archivo_id)
WHERE r.linea IS NOT NULL
GROUP BY r.motivo
"""

_SQL_ACUMULADO_ARCHIVOS = _SQL_VIGENTES + """
SELECT (SELECT count(*) FROM vigente), (SELECT count(*) FROM raw.archivo WHERE estado = 'rechazado')
"""


def metricas_acumuladas(con) -> dict:
    """Conteos de `metrics.json` sobre el estado de la base (no sobre lo que hizo la Corrida): dos `make run`
    seguidos dan los mismos numeros. Solo lee."""
    with con.cursor() as cur:
        cur.execute(_SQL_ACUMULADO_FUENTES)
        por_fuente: dict[str, dict[str, int]] = {}
        for fuente, contador, valor in cur.fetchall():
            por_fuente.setdefault(fuente, {"leidas": 0, "cargadas": 0, "rechazadas": 0})[contador] = valor
        cur.execute(_SQL_ACUMULADO_MOTIVOS)
        motivos = dict(sorted(cur.fetchall()))
        cur.execute(_SQL_ACUMULADO_ARCHIVOS)
        cargados, rechazados = cur.fetchone()
    return {
        "filas_leidas": sum(f["leidas"] for f in por_fuente.values()),
        "filas_cargadas": sum(f["cargadas"] for f in por_fuente.values()),
        "filas_rechazadas_por_motivo": motivos,
        "por_fuente": dict(sorted(por_fuente.items())),
        "archivos": {"cargados": cargados, "rechazados": rechazados},
    }


def _dq_report(cur) -> str:
    cur.execute(_SQL_CONTEOS, {"clientes_con_pii": custodia.CLIENTES_CON_PII})
    por_contador = dict(cur.fetchall())
    cur.execute(_SQL_CUBOS)
    exactos, conflictivos, correcciones = cur.fetchone()
    cur.execute(_SQL_HUECOS)
    huecos = dict(cur.fetchall())
    cur.execute(_SQL_FRONTERAS_HUERFANAS)
    huerfanas = cur.fetchone()[0]
    reporte = {
        "filas_leidas": por_contador.get("leidas", 0),
        "duplicados_exactos": exactos,
        "duplicados_conflictivos": conflictivos,
        "registros_negativos": por_contador.get(fuentes.CONTADOR_NEGATIVOS, 0),
        "registros_nulos": por_contador.get(fuentes.CONTADOR_NULOS, 0),
        "registros_outlier": por_contador.get(fuentes.CONTADOR_OUTLIER, 0),
        "registros_mwh_convertidos": por_contador.get(fuentes.CONTADOR_MWH_CONVERTIDAS, 0),
        "correcciones_tardias_aplicadas": correcciones,
        "horas_sin_lectura_valida_total": sum(huecos.values()),
        "horas_sin_lectura_valida_por_frontera": dict(sorted(huecos.items())),
        "fronteras_huerfanas_facturacion": huerfanas,
        "clientes_con_pii_en_texto_libre": por_contador.get(custodia.CLIENTES_CON_PII, 0),
    }
    return json.dumps(reporte, indent=2, sort_keys=True) + "\n"


def _consumo_mensual_csv(cur) -> str:
    cur.execute(_SQL_CONSUMO_MENSUAL)
    buffer = io.StringIO()
    escritor = csv.writer(buffer, lineterminator="\n")
    escritor.writerow(("frontera_id", "mes", "consumo_kwh", "horas_con_dato"))
    escritor.writerows(cur.fetchall())
    return buffer.getvalue()


def materializar_salidas(con) -> dict[str, str]:
    """Contenido de las Fotos de `out/`: nombre -> texto. Sin estado propio: dos llamadas sobre la misma base dan
    los mismos bytes."""
    with con.cursor() as cur:
        salidas = {"consumo_mensual.csv": _consumo_mensual_csv(cur), "dq_report.json": _dq_report(cur)}
    con.commit()
    return salidas
