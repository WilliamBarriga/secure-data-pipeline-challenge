"""DEK (bootstrap) y carga de Clientes custodiados contra Postgres real (cauce_test)."""
import uuid

import psycopg2
import pytest

from apoyo import H_CLIENTE, cargar_texto, filas
from cauce.bordes import almacen
from cauce.nucleo import custodia, fuentes

# filas CSV partidas en dos literales solo por el ancho de linea
F1 = ("CLI9001,Acme SAS,900123456-8,uno@example.com,3990112233,"
      'Calle 1,Cali,Valle,Ana Inventada,"tel 3990445566 y x@example.com"\n')
F2 = ("CLI9002,Beta SAS,NIT 900.222.333-5,dos@example.com,3990223344,"
      "Calle 2,Bogota,Cundinamarca,Beto Inventado,Sin novedades.\n")
MALA = "CLI9003,,900333444-0,tres@example.com,3990334455,Calle 3,Cali,Valle,Cata Inventada,cel 3990556677\n"


@pytest.fixture()
def custodio(maestros_limpios, con):
    c = custodia.Custodio(maestros_limpios.kek, maestros_limpios.k_bidx)
    almacen.abrir_dek(con, c)
    return c


def _cargar(con, custodio, texto, nombre="clientes.csv"):
    return cargar_texto(con, "cliente", nombre, texto, custodio=custodio, codificacion="iso-8859-1")


def test_bootstrap_deja_una_dek_que_la_kek_abre_y_ingesta_no_puede_escribirla(maestros_limpios, con, custodio):
    assert custodio.key_version == 1
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        filas(con, "INSERT INTO crypto.dek (key_version, dek_envuelta) VALUES (2, 'x')")
    con.rollback()


def test_kek_equivocada_falla_con_instruccion(maestros_limpios, con):
    otra = custodia.Custodio("Y" * 43 + "=", maestros_limpios.k_bidx)
    with pytest.raises(custodia.KekInvalida, match="make clean && make up"):
        almacen.abrir_dek(con, otra)


def test_cargar_cliente_deja_cifrado_upsert_por_cliente_id_y_conteo(con, custodio):
    aid = _cargar(con, custodio, H_CLIENTE + F1 + F2 + MALA)
    assert filas(con, "select count(*) from raw.cliente") == [(2,)]
    assert filas(con, "select count(*) from core.dim_cliente") == [(2,)]
    assert filas(con, "select observaciones_enmascarada from core.dim_cliente where cliente_id = 'CLI9001'") == [
        ("tel [CELULAR] y [EMAIL]",)]
    assert filas(con, "select count(*) from raw.linea") == [(0,)]  # el crudo trae PII
    assert filas(con, "select motivo, payload is null, payload_cifrado is not null, key_version from "
                      "quarantine.rechazo") == [("campo_obligatorio_vacio", True, True, 1)]
    assert dict(filas(con, "select contador, valor from raw.archivo_conteo where archivo_id = %s", aid)) == {
        "leidas": 3, "cargadas": 2, "rechazadas": 1, "clientes_con_pii": 2}
    # idempotencia por cliente_id aunque el ciphertext cambie
    antes = filas(con, "select nit_cifrado, nit_bidx from core.dim_cliente where cliente_id = 'CLI9002'")
    _cargar(con, custodio, H_CLIENTE + F1 + F2.replace("Beta SAS", "Beta 2 SAS"), nombre="clientes.csv")
    despues = filas(con, "select nit_cifrado, nit_bidx, razon_social from core.dim_cliente "
                         "where cliente_id = 'CLI9002'")
    assert filas(con, "select count(*) from core.dim_cliente") == [(2,)]
    assert despues[0][2] == "Beta 2 SAS" and bytes(despues[0][0]) != bytes(antes[0][0])
    assert bytes(despues[0][1]) == bytes(antes[0][1])  # el bidx es estable


def test_nit_en_claro_no_esta_en_ninguna_tabla(maestros_limpios, con, custodio):
    _cargar(con, custodio, H_CLIENTE + F1 + F2 + MALA)
    su = almacen.conectar(maestros_limpios, maestros_limpios.superusuario, "cauce_test")
    try:
        with su.cursor() as cur:
            volcado = ""
            for t in ("raw.cliente", "core.dim_cliente", "quarantine.rechazo", "raw.archivo_conteo"):
                cur.execute(f"select t::text from {t} t")  # noqa: S608 (nombres fijos)
                volcado += "\n".join(r[0] for r in cur.fetchall())
    finally:
        su.close()
    for valor in ("900123456", "900222333", "uno@example.com", "3990112233", "3990445566", "Ana Inventada",
                  "Calle 1", "3990556677"):
        assert valor not in volcado


def test_cargar_rechaza_lo_que_no_pasa_por_custodia(con):
    r = fuentes.leer("cliente", "c.csv", (H_CLIENTE + F1).encode())
    with pytest.raises(TypeError):
        almacen.cargar(con, "cliente", "c.csv", "a" * 64, r, uuid.uuid4())  # pyright: ignore[reportArgumentType] (a proposito)
