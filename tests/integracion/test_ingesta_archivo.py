"""`ingerir` contra Postgres real (cauce_test): un archivo, un Desenlace, su evento en la Bitacora."""
import uuid

import pytest

from apoyo import CLASIF, H_CONSUMO, custodio_prueba, filas
from cauce.aplicacion.ingesta_archivo import ContextoCarga, Desenlace, ingerir
from cauce.bordes import almacen, bitacora

F1 = "CO0000000011"
T = "consumos_2026-01.csv"


@pytest.fixture()
def ctx(maestros_limpios):
    con = almacen.conectar(maestros_limpios, maestros_limpios.ingesta, "cauce_test")
    ident = bitacora.identidad(uuid.uuid4(), "run")
    yield ContextoCarga(con, ident, CLASIF, custodio_prueba())
    con.close()


def fila(valor, hora=0):
    return f"{F1},2026-01-01 {hora:02d}:00:00,{valor},1,OK\n"


def _ingerir(ctx, tmp_path, texto, nombre=T, fuente="consumo"):
    ruta = tmp_path / nombre
    ruta.write_bytes(texto.encode() if isinstance(texto, str) else texto)
    return ingerir(ctx, fuente, nombre, ruta)


def _eventos(ctx, cfg):
    """Eventos de archivo de la Corrida, leidos como `auditor` (ingesta no puede leer la Bitacora)."""
    con = almacen.conectar(cfg, cfg.auditor, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("select evento, fuente, archivo_id, filas_leidas, filas_cargadas, filas_cuarentena, "
                        "codigo_error from audit.bitacora_corrida where corrida_id = %s and evento like 'archivo_%%' "
                        "order by evento_id", (str(ctx.ident["corrida_id"]),))
            return cur.fetchall()
    finally:
        con.close()


def test_archivo_nuevo_es_cargado_con_conteos_y_evento(ctx, tmp_path, maestros_limpios):
    d = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10") + fila("-1", 1) + fila("20", 2))
    assert isinstance(d, Desenlace) and d.tipo == "cargado" and d.motivo is None
    assert d.conteos["leidas"] == 3 and d.conteos["cargadas"] == 2 and d.conteos["rechazadas"] == 1
    assert d.conteos["rechazadas_por_motivo"] == {"consumo_negativo": 1}
    assert filas(ctx.con, "select estado from raw.archivo where archivo_id = %s", d.archivo_id) == [("cargado",)]
    assert _eventos(ctx, maestros_limpios) == [("archivo_cargado", "consumo", d.archivo_id, 3, 2, 1, None)]


def test_mismo_contenido_y_nombre_es_omitido(ctx, tmp_path, maestros_limpios):
    primero = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10"))
    d = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10"))
    assert d.tipo == "omitido" and d.archivo_id == primero.archivo_id and d.motivo is None
    assert [e[0] for e in _eventos(ctx, maestros_limpios)] == ["archivo_cargado", "archivo_omitido"]
    assert filas(ctx.con, "select count(*) from raw.archivo") == [(1,)]


def test_archivo_corrupto_es_rechazado_con_subcodigo_y_sin_filas(ctx, tmp_path, maestros_limpios):
    d = _ingerir(ctx, tmp_path, (H_CONSUMO + fila("10")).rstrip("\n"))
    assert d.tipo == "rechazado" and d.motivo == "archivo_corrupto:sin_salto_final"
    assert d.conteos["leidas"] == 0 and d.conteos["cargadas"] == 0
    assert filas(ctx.con, "select estado from raw.archivo where archivo_id = %s", d.archivo_id) == [("rechazado",)]
    assert filas(ctx.con, "select count(*) from raw.lectura_consumo") == [(0,)]
    assert filas(ctx.con, "select linea, motivo from quarantine.rechazo") == [(None, "sin_salto_final")]
    assert _eventos(ctx, maestros_limpios) == [
        ("archivo_rechazado", "consumo", d.archivo_id, 0, 0, 0, "archivo_corrupto")]


def test_ingerir_hace_commit_del_archivo(ctx, tmp_path, maestros_limpios):
    d = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10"))
    otra = almacen.conectar(maestros_limpios, maestros_limpios.ingesta, "cauce_test")
    try:
        with otra.cursor() as cur:
            cur.execute("select count(*) from raw.archivo where archivo_id = %s", (d.archivo_id,))
            assert cur.fetchone() == (1,)
    finally:
        otra.close()


def test_consumo_modificado_se_rechaza_entero_y_lo_cargado_no_cambia(ctx, tmp_path, maestros_limpios):
    primero = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10") + fila("20", 1))
    antes = filas(ctx.con, "select frontera_id, ts_hora, consumo_kwh, archivo_id "
                           "from core.fact_consumo_horario order by 2")
    d = _ingerir(ctx, tmp_path, H_CONSUMO + fila("999") + fila("20", 1) + fila("30", 2))
    assert d.tipo == "rechazado" and d.motivo == "archivo_modificado" and d.archivo_id != primero.archivo_id
    assert filas(ctx.con, "select estado from raw.archivo where archivo_id = %s", d.archivo_id) == [("rechazado",)]
    assert filas(ctx.con, "select frontera_id, ts_hora, consumo_kwh, archivo_id from core.fact_consumo_horario "
                          "order by 2") == antes
    assert filas(ctx.con, "select count(*) from raw.lectura_consumo") == [(2,)]
    assert filas(ctx.con, "select linea, motivo from quarantine.rechazo") == [(None, "archivo_modificado")]
    assert _eventos(ctx, maestros_limpios)[-1] == (
        "archivo_rechazado", "consumo", d.archivo_id, 0, 0, 0, "archivo_modificado")
    # siguiente corrida: el mismo contenido modificado ya esta registrado, es Archivo omitido
    assert _ingerir(ctx, tmp_path, H_CONSUMO + fila("999") + fila("20", 1) + fila("30", 2)).tipo == "omitido"


def test_mismo_contenido_con_otro_nombre_se_procesa_como_nuevo(ctx, tmp_path):
    a = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10"), nombre="consumos_2026-01.csv")
    b = _ingerir(ctx, tmp_path, H_CONSUMO + fila("10"), nombre="consumos_2026-01_copia.csv")
    assert b.tipo == "cargado" and b.archivo_id != a.archivo_id
    assert filas(ctx.con, "select count(*) from raw.archivo") == [(2,)]


def test_maestro_modificado_se_reaplica_y_es_cargado(ctx, tmp_path):
    hf = "frontera_id,codigo_sic,cliente_id,mercado,operador_red,nivel_tension,fecha_registro,estado\n"
    f = f"{F1},frt00001,C1,M,O,1,2024-01-01,A\n"
    _ingerir(ctx, tmp_path, hf + f, nombre="fronteras.csv", fuente="frontera")
    d = _ingerir(ctx, tmp_path, hf + f.replace(",A\n", ",B\n"), nombre="fronteras.csv", fuente="frontera")
    assert d.tipo == "cargado"
    assert filas(ctx.con, "select estado from core.dim_frontera") == [("B",)]
