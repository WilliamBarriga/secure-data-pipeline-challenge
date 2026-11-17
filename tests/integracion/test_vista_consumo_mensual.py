"""`analytics.v_consumo_mensual` contra Postgres real (cauce_test): facturado vs medido por Frontera-mes."""
import uuid
from decimal import Decimal

import pytest

from cauce.bordes import almacen

F1, F2, F3 = "CO0000000001", "CO0000000002", "CO0000000003"
COLUMNAS = ["frontera_id", "cliente_id", "mes", "consumo_kwh", "horas_con_dato", "horas_hueco",
            "consumo_facturado_kwh", "diferencia_kwh", "diferencia_pct", "operador_red", "nivel_tension"]


@pytest.fixture()
def su(maestros_limpios):
    con = almacen.conectar(maestros_limpios, maestros_limpios.superusuario, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("TRUNCATE core.fact_factura, core.fact_consumo_horario, core.dim_cliente CASCADE")
            for f in (F1, F2, F3):
                cur.execute("INSERT INTO core.dim_frontera (frontera_id, cliente_id, operador_red, nivel_tension) "
                            "VALUES (%s, 'C1', 'OR', 2)", (f,))
            cur.execute("INSERT INTO raw.archivo (nombre, sha256, fuente, estado, corrida_id) "
                        "VALUES ('t', 'h-vista', 'consumo', 'cargado', gen_random_uuid())")
        con.commit()
    except BaseException:
        con.close()
        raise
    yield con
    con.close()


def _consumo(con, f, ts, kwh):
    with con.cursor() as cur:
        cur.execute("INSERT INTO core.fact_consumo_horario (frontera_id, ts_hora, consumo_kwh, version_lectura, "
                    "archivo_id, corrida_id) VALUES (%s, %s, %s, 1, "
                    "(SELECT archivo_id FROM raw.archivo WHERE sha256 = 'h-vista'), %s)",
                    (f, ts, kwh, str(uuid.uuid4())))


def _factura(con, fid, f, periodo, kwh):
    with con.cursor() as cur:
        cur.execute("INSERT INTO core.fact_factura (factura_id, frontera_id, cliente_id, periodo, "
                    "consumo_facturado_kwh) VALUES (%s, %s, NULL, %s, %s)", (fid, f, periodo, kwh))


def _vista(con):
    with con.cursor() as cur:
        cur.execute("SELECT * FROM analytics.v_consumo_mensual ORDER BY frontera_id, mes")
        cols = [d[0] for d in cur.description]
        return cols, [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_full_outer_con_lado_faltante_null_nunca_cero(su):
    _consumo(su, F1, "2026-01-01 00:00-05", 100)          # solo medido
    _factura(su, "FA", F2, "2026-01-01", 50)              # solo facturado
    _consumo(su, F3, "2026-01-01 00:00-05", 100)          # ambos
    _factura(su, "FB", F3, "2026-01-01", 110)
    su.commit()
    cols, filas = _vista(su)
    assert cols == COLUMNAS
    por = {f["frontera_id"]: f for f in filas}
    assert len(filas) == 3
    solo_medido, solo_fact, ambos = por[F1], por[F2], por[F3]
    assert solo_medido["consumo_facturado_kwh"] is None
    assert (solo_medido["diferencia_kwh"], solo_medido["diferencia_pct"]) == (None, None)
    assert solo_fact["consumo_kwh"] is None and solo_fact["horas_con_dato"] == 0
    assert (solo_fact["diferencia_kwh"], solo_fact["diferencia_pct"]) == (None, None)
    assert ambos["mes"] == "2026-01" and ambos["horas_con_dato"] == 1 and ambos["horas_hueco"] == 743
    assert ambos["diferencia_kwh"] == Decimal(10) and ambos["diferencia_pct"] == Decimal("10.00")
    assert (ambos["cliente_id"], ambos["operador_red"], ambos["nivel_tension"]) == ("C1", "OR", 2)


def test_diferencia_pct_con_dos_decimales_y_null_si_medido_es_cero(su):
    _consumo(su, F1, "2026-01-01 00:00-05", 3)
    _factura(su, "FA", F1, "2026-01-01", 4)               # (4-3)/3 = 33.33...
    _consumo(su, F2, "2026-01-01 00:00-05", 0)
    _factura(su, "FB", F2, "2026-01-01", 5)
    su.commit()
    _, filas = _vista(su)
    por = {f["frontera_id"]: f for f in filas}
    assert por[F1]["diferencia_pct"] == Decimal("33.33")
    assert por[F2]["diferencia_kwh"] == Decimal(5) and por[F2]["diferencia_pct"] is None


def test_el_mes_cruza_en_hora_local_de_bogota(su):
    _consumo(su, F1, "2026-02-01 03:00+00", 7)            # 2026-01-31 22:00 en Bogota: pertenece a enero
    _factura(su, "FA", F1, "2026-01-01", 7)
    su.commit()
    _, filas = _vista(su)
    assert [(f["mes"], f["diferencia_kwh"]) for f in filas] == [("2026-01", Decimal(0))]


def test_analista_lee_la_vista_y_no_core(su, maestros_limpios):
    _consumo(su, F1, "2026-01-01 00:00-05", 1)
    su.commit()
    cfg = maestros_limpios
    con = almacen.conectar(cfg, cfg.analista, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("SELECT count(*) FROM analytics.v_consumo_mensual")
            assert cur.fetchone() == (1,)
        for tabla in ("core.fact_factura", "core.fact_consumo_horario"):
            with pytest.raises(Exception, match="permission denied"):
                with con.cursor() as cur:
                    cur.execute(f"SELECT * FROM {tabla}")
            con.rollback()
    finally:
        con.close()


def test_frontera_fuera_del_maestro_aparece_con_atributos_null(su):
    fuera = "CO9999999999"                                # consumo sin fila en dim_frontera (fact_factura tiene FK)
    _consumo(su, fuera, "2026-01-01 00:00-05", 10)
    su.commit()
    _, filas = _vista(su)
    fila = next(f for f in filas if f["frontera_id"] == fuera)
    assert (fila["cliente_id"], fila["operador_red"], fila["nivel_tension"]) == (None, None, None)
    assert fila["consumo_kwh"] == Decimal(10) and fila["consumo_facturado_kwh"] is None
