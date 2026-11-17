"""Seam: la clasificacion real contra el esquema real de cauce_test (6 zonas)."""
import pytest

from cauce.aplicacion import corrida
from cauce.bordes import almacen
from cauce.nucleo import clasificacion, fuentes
from cauce.nucleo.clasificacion import ClasificacionIncompleta


@pytest.fixture
def columnas(cauce_test):
    con = almacen.conectar(cauce_test, cauce_test.ingesta)
    try:
        return almacen.columnas_modelo(con)
    finally:
        con.close()


def test_el_esquema_real_esta_totalmente_mapeado(columnas):
    assert "audit.bitacora_corrida.ejecutor" in columnas and "analytics.v_consumo_mensual.mes" in columnas
    corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO).verificar(fuentes.CATALOGO, columnas)


def test_columna_nueva_en_la_base_hace_fallar(columnas):
    cls = corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO)
    with pytest.raises(ClasificacionIncompleta) as e:
        cls.verificar(fuentes.CATALOGO, {**columnas, "raw.cliente.telefono_nuevo": "bytea"})
    assert [(c.codigo, c.nombres) for c in e.value.causas] == [("columna_sin_mapear", ("raw.cliente.telefono_nuevo",))]


def test_yaml_que_menciona_columna_inexistente_falla(columnas):
    quitada = {k: v for k, v in columnas.items() if k != "core.dim_cliente.nit_bidx"}
    with pytest.raises(ClasificacionIncompleta) as e:
        corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO).verificar(fuentes.CATALOGO, quitada)
    assert {c.codigo for c in e.value.causas} == {"fisica_inexistente"}
