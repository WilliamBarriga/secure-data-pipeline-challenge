"""almacen contra Postgres real (cauce_test): candidatos de consumo, merge B3 y consumo_mensual."""
from datetime import datetime, timedelta
from decimal import Decimal

from apoyo import BOGOTA, F_CONSUMO, H_CONSUMO, cargar_texto, filas
from apoyo import fila_consumo as fila
from cauce.bordes import almacen, fotos

F1, F2 = F_CONSUMO, "CO0000000014"
T0 = datetime(2026, 1, 1, 0, tzinfo=BOGOTA)


def _cargar(con, nombre, *filas, sha=None):
    return cargar_texto(con, "consumo", nombre, H_CONSUMO + "".join(filas), sha=sha)


def _ganadora(con, f=F1, ts=T0):
    return filas(con, "select consumo_kwh, version_lectura, a.nombre from core.fact_consumo_horario c "
                      "join raw.archivo a using (archivo_id) where frontera_id = %s and ts_hora = %s", f, ts)


def test_cargar_deja_candidatos_lineas_hechos_y_cuarentena(con):
    aid = _cargar(con, "consumos_2026-01.csv", fila("10.5"), fila("-1", "2026-01-01 01:00:00"),
                  fila("20.25", "2026-01-01 02:00:00"))
    assert filas(con, "select estado, fuente from raw.archivo where archivo_id = %s", aid) == [("cargado", "consumo")]
    assert filas(con, "select linea, consumo_kwh, version_lectura, calidad, mwh_convertida from raw.lectura_consumo "
                      "order by linea") == [(2, Decimal("10.5"), 1, "OK", False), (4, Decimal("20.25"), 1, "OK", False)]
    assert [x[0] for x in filas(con, "select numero_linea from raw.linea order by 1")] == [2, 3, 4]
    assert filas(con, "select fuente, linea, motivo from quarantine.rechazo") == [("consumo", 3, "consumo_negativo")]
    assert filas(con, "select ts_hora, consumo_kwh, mes_local::text, version_lectura from core.fact_consumo_horario "
                      "order by ts_hora") == [(T0, Decimal("10.5"), "2026-01-01", 1),
                                              (T0 + timedelta(hours=2), Decimal("20.25"), "2026-01-01", 1)]
    assert dict(filas(con, "select contador, valor from raw.archivo_conteo")) == {
        "leidas": 3, "cargadas": 2, "rechazadas": 1, "negativos": 1, "fronteras_fuera_maestro": 1}


def test_frontera_fuera_del_maestro_se_acepta_y_se_cuenta(con):
    with con.cursor() as cur:
        cur.execute("INSERT INTO core.dim_frontera (frontera_id) VALUES (%s)", (F1,))
    con.commit()
    aid = _cargar(con, "consumos_2026-01.csv", fila("1"), fila("2", f=F2), fila("3", "2026-01-01 01:00:00", f=F2))
    assert filas(con, "select count(*) from core.fact_consumo_horario") == [(3,)]
    assert filas(con, "select valor from raw.archivo_conteo where archivo_id = %s and contador = %s", aid,
                 almacen.CONTADOR_FUERA_MAESTRO) == [(1,)]  # Fronteras distintas, no filas


def test_mayor_version_gana_aunque_llegue_antes(con):
    _cargar(con, "consumos_2026-04_correccion.csv", fila("99", version=2, calidad="CORREGIDA"))
    _cargar(con, "consumos_2026-04.csv", fila("10"))
    assert _ganadora(con) == [(Decimal("99"), 2, "consumos_2026-04_correccion.csv")]
    assert filas(con, "select count(*) from raw.lectura_consumo") == [(2,)]  # la perdedora queda en raw


def test_archivo_que_llega_despues_pero_ordena_antes_no_gana_el_empate(con):
    _cargar(con, "b.csv", fila("20"))
    _cargar(con, "a.csv", fila("10"))
    assert _ganadora(con) == [(Decimal("20"), 1, "b.csv")]
    _cargar(con, "c.csv", fila("30"))  # y uno que ordena despues si desplaza
    assert _ganadora(con) == [(Decimal("30"), 1, "c.csv")]


def test_desempate_por_nombre_usa_collation_c(con):
    _cargar(con, "consumos_2026-04_correccion.csv", fila("20"))
    _cargar(con, "consumos_2026-04.csv", fila("10"))  # en_US ordenaria '04_correccion' antes que '04'
    assert _ganadora(con) == [(Decimal("20"), 1, "consumos_2026-04_correccion.csv")]


def test_empate_dentro_del_archivo_gana_la_ultima_linea(con):
    _cargar(con, "a.csv", fila("1"), fila("2"), fila("3", "2026-01-01 01:00:00"))
    assert _ganadora(con) == [(Decimal("2"), 1, "a.csv")]


def test_merge_solo_toca_las_llaves_del_archivo_nuevo(con):
    _cargar(con, "a.csv", fila("1"), fila("5", "2026-01-01 01:00:00"))
    ts1 = T0 + timedelta(hours=1)
    antes = filas(con, "select archivo_id, corrida_id from core.fact_consumo_horario where ts_hora = %s", ts1)
    _cargar(con, "b.csv", fila("9", version=2))
    assert filas(con, "select archivo_id, corrida_id from core.fact_consumo_horario where ts_hora = %s", ts1) == antes
    assert _ganadora(con) == [(Decimal("9"), 2, "b.csv")]


def test_lectura_invalida_no_desplaza_a_la_valida(con):
    _cargar(con, "a.csv", fila("10"))
    _cargar(con, "b.csv", fila("-5", version=2))  # v2 invalida: B4 antes que B3
    assert _ganadora(con) == [(Decimal("10"), 1, "a.csv")]


def test_archivo_corrupto_no_carga_filas(con):
    aid = cargar_texto(con, "consumo", "x.csv", H_CONSUMO + F1 + ",2026-01-01 00:00:00,1\n", sha="f" * 64)
    assert filas(con, "select estado from raw.archivo where archivo_id = %s", aid) == [("rechazado",)]
    assert filas(con, "select count(*) from raw.lectura_consumo") == [(0,)]
    assert filas(con, "select linea, motivo from quarantine.rechazo") == [(None, "numero_campos")]


def test_consumo_mensual_suma_por_mes_local_con_tres_decimales(con):
    _cargar(con, "a.csv", fila("0.0005"), fila("0.0005", "2026-01-01 01:00:00"),
            fila("2.5", "2026-01-31 23:00:00"), fila("7", "2026-02-01 00:00:00"), fila("4", f=F2))
    assert fotos.materializar_salidas(con)["consumo_mensual.csv"] == (
        "frontera_id,mes,consumo_kwh,horas_con_dato\n"
        "CO0000000011,2026-01,2.501,3\n"
        "CO0000000011,2026-02,7.000,1\n"
        "CO0000000014,2026-01,4.000,1\n")


def test_consumo_mensual_sin_datos_es_solo_encabezado(con):
    assert fotos.materializar_salidas(con)["consumo_mensual.csv"] == "frontera_id,mes,consumo_kwh,horas_con_dato\n"
