"""e2e: `python -m cauce run` como proceso con los maestros de Fronteras de los Datos sinteticos en cauce_test."""
import os
import shutil
from pathlib import Path

import pytest

from apoyo import MAESTROS_FRONTERAS, cambiar_banda, consultar, copiar, correr, metrics
from cauce.bordes import almacen, bitacora


def _estado(cfg):
    su = cfg.superusuario
    return {
        "dim": consultar(cfg, "select count(*), count(min_kwh), count(codigo_sic) from core.dim_frontera", rol=su),
        "raw": consultar(cfg, "select fuente, estado, count(*) from raw.archivo group by 1, 2 order by 1, 2", rol=su),
        "lineas": consultar(cfg, "select count(*) from raw.linea", rol=su),
        "cuarentena": consultar(cfg, "select count(*) from quarantine.rechazo", rol=su),
    }


def _eventos_ultima_corrida(cfg):
    return consultar(cfg, "select evento, fuente, resultado, filas_leidas, filas_cargadas, filas_cuarentena, "
                          "archivo_id is not null, codigo_error from audit.bitacora_corrida where corrida_id = "
                          "(select corrida_id from audit.bitacora_corrida order by evento_id desc limit 1) "
                          "order by evento_id", rol=cfg.auditor)


@pytest.fixture()
def datos_fronteras(tmp_path_factory, sinteticos):
    """Solo los maestros de Fronteras (los de Clientes se prueban en test_clientes.py)."""
    return copiar(sinteticos, tmp_path_factory.mktemp("fronteras") / "data", *MAESTROS_FRONTERAS)


@pytest.fixture()
def n(sinteticos) -> int:
    """Fronteras (y limites) que planto el generador."""
    plantado = sinteticos.verdad["maestros"]
    assert plantado["fronteras"] == plantado["limites"]
    return plantado["fronteras"]


def test_dos_corridas_mismos_conteos_y_bitacora_solo_crece(maestros_limpios, datos_fronteras, n):
    cfg = maestros_limpios
    r1 = correr(datos_fronteras)
    assert r1.returncode == 0, r1.stderr
    m1 = metrics()
    assert m1["por_fuente"] == {"frontera": {"leidas": n, "cargadas": n, "rechazadas": 0},
                                "limite": {"leidas": n, "cargadas": n, "rechazadas": 0}}
    assert m1["filas_leidas"] == 2 * n and m1["filas_cargadas"] == 2 * n and m1["archivos"]["cargados"] == 2
    assert _eventos_ultima_corrida(cfg) == [
        ("corrida_inicio", None, None, None, None, None, False, None),
        ("archivo_cargado", "frontera", None, n, n, 0, True, None),
        ("etapa_fin", "frontera", "ok", n, n, 0, False, None),
        ("archivo_cargado", "limite", None, n, n, 0, True, None),
        ("etapa_fin", "limite", "ok", n, n, 0, False, None),
        ("corrida_fin", None, "ok", None, None, None, False, None)]
    antes = _estado(cfg)
    assert antes["dim"] == [(n, n, n)] and antes["cuarentena"] == [(0,)]
    assert antes["raw"] == [("frontera", "cargado", 1), ("limite", "cargado", 1)]
    total = consultar(cfg, "select count(*) from audit.bitacora_corrida", rol=cfg.auditor)[0][0]

    r2 = correr(datos_fronteras)
    assert r2.returncode == 0, r2.stderr
    m2 = metrics()
    assert m2["corrida"]["filas_leidas"] == 0 and m2["corrida"]["filas_cargadas"] == 0
    assert m2["corrida"]["archivos"] == {"cargados": 0, "omitidos": 2, "rechazados": 0}
    assert (m2["filas_leidas"], m2["por_fuente"], m2["archivos"]) == (2 * n, m1["por_fuente"],
                                                                      {"cargados": 2, "rechazados": 0})
    assert _estado(cfg) == antes
    assert [e[0] for e in _eventos_ultima_corrida(cfg)] == [
        "corrida_inicio", "archivo_omitido", "etapa_fin", "archivo_omitido", "etapa_fin", "corrida_fin"]
    assert consultar(cfg, "select count(*) from audit.bitacora_corrida", rol=cfg.auditor)[0][0] > total
    con = almacen.conectar(cfg, cfg.auditor, "cauce_test")
    try:
        assert bitacora.verificar_cadena(con) is None
    finally:
        con.close()


def test_logs_sin_filas_de_los_maestros(maestros_limpios, datos_fronteras):
    assert correr(datos_fronteras).returncode == 0
    log = Path(os.environ["CAUCE_LOG"]).read_text()
    primera = (datos_fronteras / "maestros" / "fronteras.csv").read_text().splitlines()[1].split(",")
    frontera_id, codigo_sic = primera[0], primera[1]
    assert "fronteras.csv" in log and frontera_id not in log and codigo_sic not in log


def test_corrida_con_el_lock_tomado_falla_de_inmediato_sin_cargar(maestros_limpios, datos_fronteras, n):
    cfg = maestros_limpios
    otra = almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    try:
        with almacen.lock_corrida(otra):  # otra Corrida en curso
            r = correr(datos_fronteras)
    finally:
        otra.close()
    assert r.returncode != 0
    assert _estado(cfg) == {"dim": [(0, 0, 0)], "raw": [], "lineas": [(0,)], "cuarentena": [(0,)]}
    assert _eventos_ultima_corrida(cfg)[-1][:3] == ("corrida_fin", None, "fallo")
    assert consultar(cfg, "select codigo_error from audit.bitacora_corrida "
                          "where evento = 'corrida_fin' order by evento_id desc limit 1", rol=cfg.auditor) == [
        ("corrida_concurrente",)]
    assert correr(datos_fronteras).returncode == 0  # liberado el lock, la siguiente Corrida carga
    assert _estado(cfg)["dim"] == [(n, n, n)]


def test_maestro_modificado_se_reaplica_y_archivo_corrupto_se_rechaza(maestros_limpios, datos_fronteras, tmp_path,
                                                                       n):
    cfg = maestros_limpios
    datos = tmp_path / "data"
    shutil.copytree(datos_fronteras / "maestros", datos / "maestros")
    assert correr(datos).returncode == 0
    frontera_id = cambiar_banda(datos, "9.0", "300.0")
    assert correr(datos).returncode == 0
    assert consultar(cfg, "select min_kwh, max_kwh from core.dim_frontera "
                          "where frontera_id = %s", frontera_id, rol=cfg.superusuario) == [(9, 300)]
    assert _estado(cfg)["dim"] == [(n, n, n)]
    assert _estado(cfg)["raw"] == [("frontera", "cargado", 1), ("limite", "cargado", 2)]
    # truncado: sin salto final -> Archivo corrupto, se rechaza entero, la Corrida sigue en exit 0
    fr = datos / "maestros" / "fronteras.csv"
    fr.write_text(fr.read_text().rstrip("\n"))
    r = correr(datos)
    assert r.returncode == 0, r.stderr
    assert metrics()["corrida"]["archivos"] == {"cargados": 0, "omitidos": 1, "rechazados": 1}
    assert _eventos_ultima_corrida(cfg)[1:] == [
        ("archivo_rechazado", "frontera", None, 0, 0, 0, True, "archivo_corrupto"),
        ("etapa_fin", "frontera", "parcial", 0, 0, 0, False, None),
        ("archivo_omitido", "limite", None, None, None, None, True, None),
        ("etapa_fin", "limite", "ok", 0, 0, 0, False, None),
        ("corrida_fin", None, "parcial", None, None, None, False, None)]
    assert _estado(cfg)["dim"] == [(n, n, n)]  # lo ya cargado no cambia


def test_filas_invalidas_van_a_cuarentena_y_el_resto_se_carga(maestros_limpios, datos_fronteras, tmp_path, n):
    cfg = maestros_limpios
    datos = tmp_path / "data"
    shutil.copytree(datos_fronteras / "maestros", datos / "maestros")
    lim = datos / "maestros" / "limites_frontera.csv"
    lim.write_text(lim.read_text() + "CO0000000001,500,100\nCO0000000002,x,100\n")
    assert correr(datos).returncode == 0
    m = metrics()
    assert m["por_fuente"]["limite"] == {"leidas": n + 2, "cargadas": n, "rechazadas": 2}
    assert m["filas_rechazadas_por_motivo"] == {"banda_invalida": 1, "valor_no_numerico": 1}
    assert consultar(cfg, "select motivo, payload from quarantine.rechazo order by linea", rol=cfg.superusuario) == [
        ("banda_invalida", "CO0000000001,500,100"), ("valor_no_numerico", "CO0000000002,x,100")]
    assert _estado(cfg)["dim"] == [(n, n, n)]
