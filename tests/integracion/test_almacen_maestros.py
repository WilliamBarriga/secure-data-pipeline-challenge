"""almacen contra Postgres real (cauce_test): registrar, cargar maestros (upsert SCD1), lock de Corrida."""
import time
import uuid
from decimal import Decimal

import pytest

from apoyo import H_FRONTERA, H_LIMITE, cargar_texto, filas
from cauce.bordes import almacen
from cauce.bordes.almacen import Estado

FID = "CO0000000014"
F1 = f"{FID},frt00012,CLI9001,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n"
F2 = "CO0000000012,frt00011,CLI9002,No Regulado,Red Caribe,1,2025-02-12,ACTIVA\n"


def _cargar(con, fuente, nombre, texto, sha=None):
    sha = sha or uuid.uuid4().hex * 2
    return cargar_texto(con, fuente, nombre, texto, sha=sha), sha


def test_registrar_nuevo_omitido_y_modificado(con):
    assert almacen.registrar(con, "frontera", "fronteras.csv", "a" * 64) is Estado.NUEVO
    _cargar(con, "frontera", "fronteras.csv", H_FRONTERA + F1, sha="a" * 64)
    assert almacen.registrar(con, "frontera", "fronteras.csv", "a" * 64) is Estado.OMITIDO
    assert almacen.registrar(con, "frontera", "fronteras.csv", "b" * 64) is Estado.MODIFICADO
    assert almacen.registrar(con, "limite", "fronteras.csv", "b" * 64) is Estado.NUEVO  # otra fuente
    assert almacen.registrar(con, "frontera", "otro.csv", "a" * 64) is Estado.NUEVO  # mismo contenido, otro nombre
    assert almacen.archivo_id_por_sha(con, "frontera", "fronteras.csv", "a" * 64) is not None
    assert almacen.archivo_id_por_sha(con, "frontera", "fronteras.csv", "c" * 64) is None


def test_cargar_frontera_deja_linaje_conteos_y_dim(con):
    mala = "COXX,frt1,C,M,O,1,2024-01-01,A"
    aid, _ = _cargar(con, "frontera", "fronteras.csv", H_FRONTERA + F1 + mala + "\n")
    assert filas(con, "select estado, fuente, nombre from raw.archivo where archivo_id = %s", aid) == [
        ("cargado", "frontera", "fronteras.csv")]
    assert dict(filas(con, "select contador, valor from raw.archivo_conteo where archivo_id = %s", aid)) == {
        "leidas": 2, "cargadas": 1, "rechazadas": 1}
    lineas = filas(con, "select numero_linea from raw.linea where archivo_id = %s order by 1", aid)
    assert [x[0] for x in lineas] == [2, 3]
    assert filas(con, "select fuente, linea, motivo, payload, payload_cifrado from quarantine.rechazo") == [
        ("frontera", 3, "frontera_id_invalido", mala, None)]
    assert filas(con, "select frontera_id, nivel_tension, min_kwh from core.dim_frontera") == [
        (FID, 3, None)]


def test_limite_pliega_la_banda_en_la_frontera_en_cualquier_orden(con):
    _cargar(con, "limite", "limites_frontera.csv", H_LIMITE + f"{FID},8.0,200.0\n")  # antes que el maestro
    _cargar(con, "frontera", "fronteras.csv", H_FRONTERA + F1)
    assert filas(con, "select codigo_sic, min_kwh, max_kwh from core.dim_frontera") == [
        ("frt00012", Decimal("8.0"), Decimal("200.0"))]


def test_maestro_modificado_se_reaplica_como_upsert_scd1(con):
    _cargar(con, "frontera", "fronteras.csv", H_FRONTERA + F1 + F2)
    _cargar(con, "limite", "limites_frontera.csv", H_LIMITE + f"{FID},8.0,200.0\n")
    _cargar(con, "frontera", "fronteras.csv",
            H_FRONTERA + F1.replace("Red Andina", "Red Sabana").replace(",3,", ",4,") + F2)
    assert filas(con, "select operador_red, nivel_tension, min_kwh from core.dim_frontera "
                      "where frontera_id = %s", FID) == [("Red Sabana", 4, Decimal("8.0"))]  # la banda sobrevive
    assert filas(con, "select count(*) from core.dim_frontera") == [(2,)]
    # linaje de ambas versiones
    assert filas(con, "select count(*) from raw.archivo where fuente = 'frontera'") == [(2,)]


def test_archivo_rechazado_queda_registrado_sin_cargar_nada(con):
    aid, _ = _cargar(con, "frontera", "fronteras.csv", H_FRONTERA + F1.rstrip("\n"))
    assert filas(con, "select estado from raw.archivo") == [("rechazado",)]
    assert filas(con, "select linea, motivo from quarantine.rechazo where archivo_id = %s", aid) == [
        (None, "sin_salto_final")]
    assert filas(con, "select count(*) from core.dim_frontera") == [(0,)]
    assert filas(con, "select count(*) from raw.linea") == [(0,)]


def test_lock_corrida_falla_rapido_si_otra_conexion_lo_tiene(maestros_limpios):
    cfg = maestros_limpios
    primera = almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    segunda = almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    try:
        with almacen.lock_corrida(primera):
            t0 = time.monotonic()
            with pytest.raises(almacen.CorridaConcurrente) as e:
                with almacen.lock_corrida(segunda):
                    pytest.fail("la segunda Corrida no debe entrar")
            assert time.monotonic() - t0 < 2  # un solo intento, sin espera
            assert e.value.codigo_error == "corrida_concurrente"
        with almacen.lock_corrida(segunda):  # liberado: se puede volver a tomar
            pass
    finally:
        primera.close()
        segunda.close()
