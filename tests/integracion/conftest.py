import pytest

from cauce.bordes import almacen


def _escalar(cur):
    """Primera columna de la unica fila de la ultima consulta; falla si no hubo fila."""
    fila = cur.fetchone()
    assert fila is not None, "la consulta no devolvio filas"
    return fila[0]


@pytest.fixture()
def escalar():
    """Helper de cursor compartido por los tests de integracion (no es una operacion del dominio)."""
    return _escalar


@pytest.fixture()
def con(maestros_limpios):
    """Conexion como `ingesta` a cauce_test, ya limpia de archivos, Cuarentena y maestros."""
    c = almacen.conectar(maestros_limpios, maestros_limpios.ingesta, "cauce_test")
    yield c
    c.close()
