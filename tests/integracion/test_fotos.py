"""Fotos contra Postgres real (cauce_test): `dq_report.json` como foto, cubos de sobrantes y `core.v_hueco`."""
import json
import uuid
from datetime import datetime, timedelta

from apoyo import BOGOTA, H_CONSUMO, H_CONSUMO_MWH, H_FRONTERA, cargar_texto, filas
from apoyo import fila_consumo as fila
from cauce.bordes import fotos

F1, F2 = "CO0000000011", "CO0000000014"
T0 = datetime(2026, 1, 1, 0, tzinfo=BOGOTA)


def hora(n):
    return f"2026-01-01 {n:02d}:00:00"


def _cargar(con, nombre, *filas):
    return cargar_texto(con, "consumo", nombre, H_CONSUMO + "".join(filas))


def _dq(con) -> dict:
    return json.loads(fotos.materializar_salidas(con)["dq_report.json"])


def _maestro(con, *fronteras):
    with con.cursor() as cur:
        for f in fronteras:
            cur.execute("INSERT INTO core.dim_frontera (frontera_id) VALUES (%s)", (f,))
    con.commit()


def test_dq_report_tiene_todas_las_llaves_ordenadas_y_en_cero_sin_datos(con):
    texto = fotos.materializar_salidas(con)["dq_report.json"]
    assert json.loads(texto) == {
        "clientes_con_pii_en_texto_libre": 0, "correcciones_tardias_aplicadas": 0, "duplicados_conflictivos": 0,
        "duplicados_exactos": 0, "filas_leidas": 0, "fronteras_huerfanas_facturacion": 0,
        "horas_sin_lectura_valida_por_frontera": {}, "horas_sin_lectura_valida_total": 0,
        "registros_mwh_convertidos": 0, "registros_negativos": 0, "registros_nulos": 0, "registros_outlier": 0}
    assert texto == json.dumps(json.loads(texto), indent=2, sort_keys=True) + "\n"


def test_cada_lectura_sobrante_cae_en_un_solo_cubo(con):
    _cargar(con, "a.csv", fila("10", hora(0)), fila("20", hora(1)), fila("5", hora(2)), fila("5", hora(3)),
            fila("7", hora(4)))
    _cargar(con, "b.csv",
            fila("10.000", hora(0)),                    # exacto de a.csv (contenido normalizado)
            fila("21", hora(1)),                        # conflictivo: misma version, otro valor
            fila("6", hora(2), version=2),              # Correccion tardia: v2 sobre v1
            fila("5", hora(3)), fila("5", hora(3)))     # dos exactos mas sobre la hora 3 (a.csv + 2 = 2 sobrantes)
    dq = _dq(con)
    assert (dq["duplicados_exactos"], dq["duplicados_conflictivos"], dq["correcciones_tardias_aplicadas"]) == (
        3, 1, 1)
    sobrantes = filas(con, "select (select count(*) from raw.lectura_consumo) - "
                           "(select count(*) from core.fact_consumo_horario)")[0][0]
    assert sobrantes == 5 == 3 + 1 + 1  # disjuntos: suman los sobrantes


def test_lecturas_que_difieren_en_el_cuarto_decimal_son_conflictivas_no_exactas(con):
    # consumo_kwh es numeric libre: el Duplicado exacto se compara sin redondear (se redondea solo al escribir)
    _cargar(con, "a.csv", fila("10.0001", hora(0)))
    _cargar(con, "b.csv", fila("10.0002", hora(0)))
    dq = _dq(con)
    assert (dq["duplicados_exactos"], dq["duplicados_conflictivos"]) == (0, 1)


def test_correccion_con_v2_invalida_no_cuenta(con):
    _cargar(con, "a.csv", fila("10"))
    _cargar(con, "b.csv", fila("-5", version=2))
    dq = _dq(con)
    assert dq["correcciones_tardias_aplicadas"] == 0 and dq["registros_negativos"] == 1
    assert (dq["duplicados_exactos"], dq["duplicados_conflictivos"]) == (0, 0)


def test_correccion_con_v1_invalida_no_cuenta(con):
    _cargar(con, "a.csv", fila("-1"))
    _cargar(con, "b.csv", fila("10", version=2))
    assert _dq(con)["correcciones_tardias_aplicadas"] == 0


def test_contadores_b_y_filas_leidas_solo_de_archivos_aceptados(con):
    _cargar(con, "a.csv", fila(""), fila("-1", hora(1)), fila("2000000", hora(2)), fila("3", hora(3)))
    cargar_texto(con, "consumo", "x.csv", H_CONSUMO + F1 + ",2026-01-01 00:00:00,1\n")  # Archivo corrupto: aporta 0
    dq = _dq(con)
    assert (dq["filas_leidas"], dq["registros_nulos"], dq["registros_negativos"], dq["registros_outlier"]) == (
        4, 1, 1, 1)


def test_mwh_convertidos_cuenta_tambien_la_fila_rechazada(con):
    cargar_texto(con, "consumo", "m.csv", H_CONSUMO_MWH + f"{F1},2026-03-01 00:00:00,2,MWh,1,OK\n"
                                                         f"{F1},2026-03-01 01:00:00,1001,MWh,1,OK\n")
    dq = _dq(con)
    assert dq["registros_mwh_convertidos"] == 2 and dq["registros_outlier"] == 1


def test_hueco_por_fila_ausente_y_por_fila_rechazada_y_nada_se_imputa(con):
    _maestro(con, F1)
    _cargar(con, "a.csv", fila("1"), fila("-1", hora(1)))  # la hora 01 se rechazo; las demas no existen
    dq = _dq(con)
    assert dq["horas_sin_lectura_valida_por_frontera"] == {F1: 743}
    assert dq["horas_sin_lectura_valida_total"] == 743
    assert filas(con, "select count(*) from core.v_hueco where ts_hora = %s", T0 + timedelta(hours=1)) == [(1,)]
    assert filas(con, "select count(*) from core.fact_consumo_horario") == [(1,)]


def test_mes_ausente_es_mes_completo_y_el_universo_une_maestro_y_consumos(con):
    _maestro(con, F1)  # F2 no esta en el maestro pero aparece en consumos; F1 solo reporta enero
    _cargar(con, "a.csv", fila("1"), fila("1", "2026-02-01 00:00:00", f=F2))
    dq = _dq(con)  # enero 744 h, febrero 672 h: cada Frontera tiene 1 hora con dato
    assert dq["horas_sin_lectura_valida_por_frontera"] == {F1: 743 + 672, F2: 744 + 671}
    assert list(dq["horas_sin_lectura_valida_por_frontera"]) == sorted([F1, F2])


def test_frontera_sin_huecos_aparece_con_cero_y_el_total_es_la_suma(con):
    _maestro(con, F1, F2)
    _cargar(con, "a.csv", *(fila("1", f"2026-01-{d:02d} {h:02d}:00:00") for d in range(1, 32) for h in range(24)))
    dq = _dq(con)
    assert dq["horas_sin_lectura_valida_por_frontera"] == {F1: 0, F2: 744}
    assert dq["horas_sin_lectura_valida_total"] == 744


def test_invariante_horas_con_dato_mas_huecos_es_las_horas_del_mes(con):
    _maestro(con, F1, F2)
    _cargar(con, "a.csv", fila("1"), fila("2", "2026-01-15 05:00:00"), fila("3", "2026-02-10 05:00:00"),
            fila("1", f=F2))
    horas = filas(con, "select f.frontera_id, f.mes_local::text, f.n + coalesce(h.n, 0) from "
                       "(select frontera_id, mes_local, count(*) n from core.fact_consumo_horario group by 1, 2) f "
                       "left join (select frontera_id, "
                       "(date_trunc('month', ts_hora at time zone 'America/Bogota'))::date mes_local, count(*) n "
                       "from core.v_hueco group by 1, 2) h using (frontera_id, mes_local)")
    assert {(x[0], x[1]): x[2] for x in horas} == {
        (F1, "2026-01-01"): 744, (F1, "2026-02-01"): 672, (F2, "2026-01-01"): 744}


def test_fronteras_huerfanas_de_facturacion_cuentan_fronteras_distintas_no_filas(con):
    """Cuarentena no tiene frontera_id: el recuento sale de raw.factura (sin PII) por (archivo, linea)."""
    aid = _cargar(con, "a.csv", fila("1"))
    corrida = str(uuid.uuid4())
    with con.cursor() as cur:
        cur.execute("INSERT INTO raw.factura (archivo_id, factura_id, numero_linea, frontera_id, generado_en, "
                    "payload, payload_cifrado, key_version) VALUES "
                    "(%s, 'F-1', 1, 'CO9990000001', now(), '{}', '\\x00', 1), "
                    "(%s, 'F-2', 2, 'CO9990000001', now(), '{}', '\\x00', 1), "
                    "(%s, 'F-3', 3, 'CO9990000002', now(), '{}', '\\x00', 1), "
                    "(%s, 'F-4', 4, 'CO9990000003', now(), '{}', '\\x00', 1)", (aid,) * 4)
        cur.execute("INSERT INTO quarantine.rechazo (corrida_id, fuente, archivo_id, linea, motivo) VALUES "
                    "(%s, 'factura', %s, 1, 'frontera_huerfana'), (%s, 'factura', %s, 2, 'frontera_huerfana'), "
                    "(%s, 'factura', %s, 3, 'frontera_huerfana'), (%s, 'factura', %s, 4, 'factura_duplicada')",
                    (corrida, aid) * 4)
    con.commit()
    assert _dq(con)["fronteras_huerfanas_facturacion"] == 2  # 3 filas, 2 Fronteras; la duplicada no cuenta


def test_clientes_con_pii_en_texto_libre_suma_los_conteos_de_archivos_de_clientes(con):
    with con.cursor() as cur:
        cur.execute("INSERT INTO raw.archivo (nombre, sha256, fuente, estado, corrida_id) VALUES "
                    "('clientes.csv', 'c', 'cliente', 'cargado', %s) RETURNING archivo_id", (str(uuid.uuid4()),))
        aid = cur.fetchone()[0]
        cur.execute("INSERT INTO raw.archivo_conteo VALUES (%s, 'clientes_con_pii', 25)", (aid,))
    con.commit()
    assert _dq(con)["clientes_con_pii_en_texto_libre"] == 25


def test_las_salidas_son_foto_dos_materializaciones_dan_los_mismos_bytes(con):
    _cargar(con, "a.csv", fila("1"), fila("1"), fila("2", hora(1)))
    assert fotos.materializar_salidas(con) == fotos.materializar_salidas(con)


# --- metrics.json: conteos acumulados de la base (DECISIONS.md) ---------------------------------------
_F_A = "CO0000000014,frt00012,CLI9001,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n"
_F_B = "CO0000000012,frt00011,CLI9002,No Regulado,Red Caribe,1,2025-02-12,ACTIVA\n"


def test_metricas_acumuladas_sin_datos_en_cero(con):
    assert fotos.metricas_acumuladas(con) == {
        "filas_leidas": 0, "filas_cargadas": 0, "filas_rechazadas_por_motivo": {}, "por_fuente": {},
        "archivos": {"cargados": 0, "rechazados": 0}}


def test_metricas_acumuladas_suman_los_archivos_vigentes_y_su_cuarentena(con):
    _cargar(con, "a.csv", fila("10", hora(0)), fila("-1", hora(1)), fila("", hora(2)))
    _cargar(con, "b.csv", fila("4", hora(3)), fila("5", hora(4)))
    cargar_texto(con, "frontera", "fronteras.csv", H_FRONTERA + _F_A)
    # maestro reaplicado con el mismo nombre: cuenta solo la ultima version, no las dos
    cargar_texto(con, "frontera", "fronteras.csv", H_FRONTERA + _F_A + _F_B + "COXX,frt1,C,M,O,1,2024-01-01,A\n")
    cargar_texto(con, "consumo", "roto.csv", H_CONSUMO + F1 + ",2026-01-01 00:00:00,1\n")  # rechazado entero
    assert fotos.metricas_acumuladas(con) == {
        "filas_leidas": 8, "filas_cargadas": 5,
        "filas_rechazadas_por_motivo": {"consumo_negativo": 1, "consumo_nulo": 1, "frontera_id_invalido": 1},
        "por_fuente": {"consumo": {"leidas": 5, "cargadas": 3, "rechazadas": 2},
                       "frontera": {"leidas": 3, "cargadas": 2, "rechazadas": 1}},
        "archivos": {"cargados": 3, "rechazados": 1}}
