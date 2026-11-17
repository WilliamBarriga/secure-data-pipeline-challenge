"""e2e: oraculos exactos de B1-B7 (brief §4) y la idempotencia sobre los Datos sinteticos, en cauce_test.

Se copian los Datos sinteticos de la sesion a un temporal antes de correr; el oraculo es su Verdad plantada.
"""
import hashlib
import json
import time

import pytest

from apoyo import consultar, copiar, correr, metrics, salida


@pytest.fixture()
def datos_conocidos(tmp_path_factory, sinteticos):
    return copiar(sinteticos, tmp_path_factory.mktemp("conocidos") / "data")


def _batch(sinteticos) -> list[str]:
    """Archivos que lee `run` (todo menos el stream)."""
    return [r for r in sinteticos.archivos() if not r.startswith("eventos/")]


def test_dq_report_es_la_verdad_plantada(maestros_limpios, datos_conocidos, sinteticos):
    r = correr(datos_conocidos)
    assert r.returncode == 0, r.stderr
    dq = json.loads(salida("dq_report.json"))
    assert dq == sinteticos.verdad["dq_report"]  # las 12 llaves del brief §9.3, valor por valor
    por_frontera = dq["horas_sin_lectura_valida_por_frontera"]
    assert list(por_frontera) == sorted(por_frontera) and len(por_frontera) >= sinteticos.verdad["fronteras"]
    assert dq["horas_sin_lectura_valida_total"] == sum(por_frontera.values())
    assert [(dq["horas_sin_lectura_valida_total"],)] == consultar(maestros_limpios, "select count(*) from core.v_hueco")


def test_invariante_de_horas_y_vista_analitica(maestros_limpios, datos_conocidos, sinteticos):
    assert correr(datos_conocidos).returncode == 0
    filas = consultar(maestros_limpios, "select mes, horas_con_dato + horas_hueco, "
                                        "extract(day from (to_date(mes || '-01', 'YYYY-MM-DD') + interval '1 month' "
                                        "- to_date(mes || '-01', 'YYYY-MM-DD'))) * 24 from analytics.v_consumo_mensual "
                                        "where consumo_kwh is not null")
    assert filas and all(total == horas for _, total, horas in filas)
    assert consultar(maestros_limpios, "select count(*) from analytics.v_consumo_mensual") == [
        (len(sinteticos.verdad["consumo_mensual"]),)]
    # el generador factura cada Frontera-mes medido: todas tienen medido y facturado
    assert consultar(maestros_limpios, "select count(*) from analytics.v_consumo_mensual where diferencia_kwh is null "
                                       "or diferencia_pct is null") == [(0,)]


def _estado(cfg):
    return consultar(cfg, "select (select count(*) from core.dim_frontera), (select count(*) from core.dim_cliente), "
                          "(select count(*) from core.fact_consumo_horario), (select sum(consumo_kwh) from "
                          "core.fact_consumo_horario), (select count(*) from core.v_hueco), "
                          "(select count(*) from analytics.v_consumo_mensual), (select count(*) from "
                          "quarantine.rechazo), (select count(*) from raw.lectura_consumo), "
                          "(select count(*) from raw.archivo), (select count(*) from raw.archivo_conteo), "
                          "(select count(*) from raw.factura), (select count(*) from core.fact_factura), "
                          "(select count(*) from core.fact_factura_componente)")[0]


def test_dos_corridas_dejan_el_mismo_estado_byte_a_byte_y_la_bitacora_solo_crece(maestros_limpios, datos_conocidos):
    assert correr(datos_conocidos).returncode == 0
    estado = _estado(maestros_limpios)
    csv1, dq1 = salida("consumo_mensual.csv"), salida("dq_report.json")
    eventos = consultar(maestros_limpios, "select count(*) from audit.bitacora_corrida")[0][0]
    r = correr(datos_conocidos)
    assert r.returncode == 0, r.stderr
    assert _estado(maestros_limpios) == estado
    assert hashlib.sha256(salida("consumo_mensual.csv")).hexdigest() == hashlib.sha256(csv1).hexdigest()
    assert hashlib.sha256(salida("dq_report.json")).hexdigest() == hashlib.sha256(dq1).hexdigest()
    assert consultar(maestros_limpios, "select count(*) from audit.bitacora_corrida")[0][0] > eventos
    assert metrics()["corrida"]["archivos"]["cargados"] == 0


def test_corrida_completa_cabe_en_900_s_y_metrics_trae_el_desglose(maestros_limpios, datos_conocidos):
    t0 = time.monotonic()
    r = correr(datos_conocidos)
    duracion = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert duracion < 900  # US 40: cota holgada; la medida real esta en DECISIONS.md
    m = metrics()
    assert m["duracion_s_total"] < 900
    assert {"consumos", "facturacion"} <= set(m["duracion_s_por_etapa"])
    assert all(m["duracion_s_por_etapa"][e] >= 0 for e in ("consumos", "facturacion"))
    motivos = m["filas_rechazadas_por_motivo"]  # US 39: motivos de Cuarentena de consumos y facturacion
    assert {"consumo_negativo", "consumo_nulo", "consumo_outlier", "frontera_huerfana"} <= set(motivos)
    assert m["por_fuente"]["consumo"]["rechazadas"] > 0 and m["por_fuente"]["factura"]["rechazadas"] > 0


_CONTEOS = ("filas_leidas", "filas_cargadas", "filas_rechazadas_por_motivo", "por_fuente", "archivos")


def test_metrics_cuenta_el_estado_de_la_base_y_deja_la_corrida_en_su_subclave(maestros_limpios, datos_conocidos,
                                                                              sinteticos):
    # el operador corre `make run` dos veces y compara; los conteos no pueden caer a 0
    assert correr(datos_conocidos).returncode == 0
    m1 = metrics()
    assert m1["conteos_de"] == "base"
    assert m1["por_fuente"]["consumo"]["leidas"] == sinteticos.verdad["dq_report"]["filas_leidas"]
    assert m1["archivos"] == {"cargados": len(_batch(sinteticos)), "rechazados": 0}
    assert {k: m1["corrida"][k] for k in _CONTEOS if k != "archivos"} == {k: m1[k] for k in _CONTEOS
                                                                          if k != "archivos"}  # desde vacio
    r = correr(datos_conocidos)
    assert r.returncode == 0, r.stderr
    m2 = metrics()
    assert {k: m2[k] for k in _CONTEOS} == {k: m1[k] for k in _CONTEOS}
    assert m2["corrida"] == {"filas_leidas": 0, "filas_cargadas": 0, "filas_rechazadas_por_motivo": {},
                             "por_fuente": {}, "archivos": {"cargados": 0, "omitidos": len(_batch(sinteticos)),
                                                            "rechazados": 0}}
    assert {"corrida_id", "ts_inicio", "ts_fin", "duracion_s_por_etapa"} <= set(m2)
