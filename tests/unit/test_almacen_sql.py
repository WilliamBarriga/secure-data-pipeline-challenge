"""Construccion pura del SQL de `almacen` desde el destino declarado (sin base)."""
import pytest
from psycopg2 import sql

from cauce.bordes import almacen
from cauce.nucleo import fuentes


def _aplanar(c):
    """sql.Composed -> texto con identificadores entre comillas dobles (sin conexion)."""
    if isinstance(c, sql.Composed):
        return "".join(_aplanar(x) for x in c.seq)
    if isinstance(c, sql.Identifier):
        return '"' + ".".join(c.strings) + '"'
    return c.string


def test_upsert_core_actualiza_todo_menos_la_llave():
    q = almacen._sql_upsert_core("core.dim_frontera", "frontera_id", ("frontera_id", "estado", "mercado"))
    assert _aplanar(q) == (
        'INSERT INTO "core"."dim_frontera" ("frontera_id", "estado", "mercado") VALUES %s '
        'ON CONFLICT ("frontera_id") DO UPDATE SET "estado" = EXCLUDED."estado", "mercado" = EXCLUDED."mercado"')


def test_limite_solo_actualiza_banda():
    d = fuentes.ficha_por_lotes("limite").destino
    q = almacen._sql_upsert_core(d.core, d.clave, ("frontera_id", "min_kwh", "max_kwh"))
    assert _aplanar(q).endswith('DO UPDATE SET "min_kwh" = EXCLUDED."min_kwh", "max_kwh" = EXCLUDED."max_kwh"')


def test_insert_raw_antepone_linaje():
    q = almacen._sql_insert_raw("raw.cliente", ("cliente_id", "key_version"))
    assert _aplanar(q) == ('INSERT INTO "raw"."cliente" ("archivo_id", "numero_linea", "cliente_id", '
                           '"key_version") VALUES %s')


def test_cliente_declara_corrida_y_los_demas_no():
    assert fuentes.ficha_por_lotes("cliente").destino.con_corrida
    assert not fuentes.ficha_por_lotes("frontera").destino.con_corrida
    assert not fuentes.ficha_por_lotes("limite").destino.con_corrida


@pytest.mark.parametrize("malo", ["Col", "a-b", "a; DROP TABLE x", "", "1a", 'a"b'])
def test_identificador_invalido_se_rechaza_sin_eco(malo):
    with pytest.raises(ValueError) as e:
        almacen._sql_upsert_core("core.dim_frontera", "frontera_id", ("frontera_id", malo))
    assert malo == "" or malo not in str(e.value)


def test_insert_raw_de_consumo_nombra_su_columna_de_linea():
    q = almacen._sql_insert_raw("raw.lectura_consumo", ("frontera_id",), linea="linea")
    assert _aplanar(q) == 'INSERT INTO "raw"."lectura_consumo" ("archivo_id", "linea", "frontera_id") VALUES %s'


def test_tabla_invalida_se_rechaza():
    with pytest.raises(ValueError):
        almacen._sql_insert_raw("raw.cliente; x", ("a",))


def test_lote_con_claves_distintas_falla_sin_valores():
    with pytest.raises(ValueError) as e:
        almacen._columnas_lote([{"a": "secreto1"}, {"b": "secreto2"}])
    assert "secreto" not in str(e.value)


def test_lote_homogeneo_devuelve_columnas():
    assert almacen._columnas_lote([{"a": 1, "b": 2}, {"a": 3, "b": 4}]) == ("a", "b")


def test_upsert_solo_con_llave_no_actualiza_nada():
    q = almacen._sql_upsert_core("core.dim_frontera", "frontera_id", ("frontera_id",))
    assert _aplanar(q) == (
        'INSERT INTO "core"."dim_frontera" ("frontera_id") VALUES %s ON CONFLICT ("frontera_id") DO NOTHING')
