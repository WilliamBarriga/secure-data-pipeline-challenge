"""Ficha de Fuente: una entrada de `fuentes.FICHAS` por fuente, y los modulos la consultan (sin `if fuente == ...`)."""
from pathlib import Path

import pytest

from cauce.aplicacion import corrida_batch
from cauce.bordes import almacen
from cauce.nucleo import fuentes

CON_LECTURA = [f for f, ficha in fuentes.FICHAS.items() if isinstance(ficha, fuentes.FichaPorLotes)]


def test_el_catalogo_sale_de_las_fichas():
    assert set(fuentes.CATALOGO) == set(fuentes.FICHAS) == {"cliente", "frontera", "limite", "consumo", "factura",
                                                            "stream"}
    assert fuentes.CATALOGO["limite"] == {"frontera_id", "min_kwh", "max_kwh"}
    # consumo: la union de los encabezados de sus 3 formatos
    assert {"timestamp", "timestamp_utc", "kwh", "unidad"} <= fuentes.CATALOGO["consumo"]


@pytest.mark.parametrize("fuente", CON_LECTURA)
def test_toda_fuente_con_lectura_declara_destino_carga_y_origen(fuente):
    ficha = fuentes.ficha_por_lotes(fuente)
    assert ficha.destino is not None and ficha.carga is not None and ficha.origen is not None
    assert ficha.codificaciones


@pytest.mark.parametrize("fuente", CON_LECTURA)
def test_toda_carga_declarada_tiene_cargador(fuente):
    assert callable(almacen.cargador(fuentes.ficha_por_lotes(fuente).carga))


def test_una_carga_desconocida_falla_fuerte():
    with pytest.raises(ValueError, match="carga desconocida: otra"):
        almacen.cargador("otra")


def test_stream_solo_declara_campos_y_su_lectura_sigue_pendiente():
    ficha = fuentes.FICHAS["stream"]
    assert not isinstance(ficha, fuentes.FichaPorLotes) and ficha.catalogo
    with pytest.raises(NotImplementedError):
        fuentes.ficha_por_lotes("stream")
    with pytest.raises(NotImplementedError):
        fuentes.leer("stream", "s.jsonl", b"{}")


def test_una_fuente_desconocida_falla_fuerte():
    with pytest.raises(ValueError, match="fuente desconocida: otra"):
        fuentes.ficha_por_lotes("otra")


def test_solo_consumos_y_facturacion_rechazan_el_archivo_modificado():
    # GLOSSARY: los maestros no tienen Archivo modificado
    assert {f for f in CON_LECTURA if fuentes.ficha_por_lotes(f).rechaza_modificado} == {"consumo", "factura"}


def test_consumo_aterriza_en_raw_y_se_funde_a_core():
    destino = fuentes.ficha_por_lotes("consumo").destino
    assert (destino.raw, destino.core) == ("raw.lectura_consumo", "core.fact_consumo_horario")


def test_raw_requerida_falla_fuerte_si_el_destino_no_tiene_raw():
    assert fuentes.ficha_por_lotes("factura").destino.raw_requerida == "raw.factura"
    with pytest.raises(ValueError, match="no tiene tabla raw"):
        _ = fuentes.ficha_por_lotes("frontera").destino.raw_requerida


def test_las_etapas_cubren_cada_fuente_con_lectura_una_vez():
    en_etapas = [f for _, de_la_etapa in corrida_batch.ETAPAS for f in de_la_etapa]
    assert sorted(en_etapas) == sorted(CON_LECTURA)


def test_el_origen_arma_sus_rutas_bajo_data_sin_mirar_el_disco():
    origen = fuentes.ficha_por_lotes("frontera").origen
    assert origen.directorio(Path("/d")) == Path("/d/maestros")
    assert origen.rutas(Path("/d")) == (Path("/d/maestros/fronteras.csv"),)
    assert fuentes.ficha_por_lotes("consumo").origen.rutas(Path("/d")) == ()  # por extension: lo lista el borde


def test_el_ndjson_del_stream_es_un_origen_fuera_de_la_ficha():
    # la Ficha `stream` sigue sin origen (no es FichaPorLotes); el ndjson que publica `make producir` es una constante
    assert not hasattr(fuentes.FICHAS["stream"], "origen")
    assert fuentes.ORIGEN_STREAM.rutas(Path("/d")) == (Path("/d/eventos/mediciones_stream.ndjson"),)
