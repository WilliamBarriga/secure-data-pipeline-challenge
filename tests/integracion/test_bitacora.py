"""Bitacora append-only en 3 capas + `cauce.bitacora` (seam: registrar_evento / verificar_cadena)."""
import uuid
from typing import Any

import psycopg2
import pytest
from psycopg2 import errors

from cauce.bordes import almacen, bitacora

COLUMNAS_TEXTO_LIBRE_PERMITIDAS = {"evento", "usuario_db", "ejecutor", "comando", "version_codigo",
                                   "fuente", "resultado", "codigo_error"}


def _registrar(cfg, **kw: Any):
    con = almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    try:
        ident = bitacora.Identidad(corrida_id=kw.pop("corrida_id", uuid.uuid4()), ejecutor="u@h",
                                   comando=kw.pop("comando", "run"), version_codigo="v1")
        eid = bitacora.registrar_evento(con, ident, kw.pop("evento", "corrida_inicio"), **kw)
        con.commit()
        return eid, ident["corrida_id"]
    finally:
        con.close()


def _su(cfg):
    con = almacen.conectar(cfg, cfg.superusuario, "cauce_test")
    return con


def test_registrar_evento_pone_ts_y_usuario_db_del_servidor(cauce_test):
    eid, cid = _registrar(cauce_test)
    con = almacen.conectar(cauce_test, cauce_test.auditor, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("select usuario_db, ts <= clock_timestamp(), ts > now() - interval '1 minute', "
                        "ejecutor, comando, version_codigo, length(hash) from audit.bitacora_corrida "
                        "where evento_id = %s", (eid,))
            assert cur.fetchone() == (cauce_test.ingesta.usuario, True, True, "u@h", "run", "v1", 32)
    finally:
        con.close()


def test_la_cadena_enlaza_hash_previo_y_verifica_limpia(cauce_test):
    _registrar(cauce_test)
    _registrar(cauce_test, evento="corrida_fin", resultado="ok")
    con = almacen.conectar(cauce_test, cauce_test.auditor, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("select count(*) from (select hash_previo, lag(hash) over (order by evento_id) h "
                        "from audit.bitacora_corrida) t where hash_previo is distinct from h")
            assert cur.fetchone() == (0,)
        assert bitacora.verificar_cadena(con) is None
    finally:
        con.close()


@pytest.mark.parametrize("sentencia", [
    "update audit.bitacora_corrida set ejecutor = 'x'",
    "delete from audit.bitacora_corrida",
    "truncate audit.bitacora_corrida",
])
def test_ni_el_owner_ni_el_superusuario_modifican_la_bitacora(cauce_test, sentencia):
    _registrar(cauce_test)
    con = _su(cauce_test)
    try:
        with con.cursor() as cur:
            # capa 2: el superusuario salta los GRANT pero el trigger hace RAISE
            with pytest.raises(psycopg2.Error) as e:
                cur.execute(sentencia)
            assert "append-only" in str(e.value)
        con.rollback()
        with con.cursor() as cur:
            # capa 1: cauce_owner dueno de la tabla sin privilegios de modificacion
            cur.execute("set local role cauce_owner")
            with pytest.raises(errors.InsufficientPrivilege):
                cur.execute(sentencia)
    finally:
        con.rollback()
        con.close()


def test_edicion_como_superusuario_con_trigger_deshabilitado_se_detecta(cauce_test):
    eid, _ = _registrar(cauce_test)
    _registrar(cauce_test, evento="corrida_fin", resultado="ok")
    con = _su(cauce_test)
    try:
        with con.cursor() as cur:
            cur.execute("alter table audit.bitacora_corrida disable trigger user")
            cur.execute("update audit.bitacora_corrida set ejecutor = 'falsificado' where evento_id = %s", (eid,))
        assert bitacora.verificar_cadena(con) == eid
    finally:
        con.rollback()  # el DDL es transaccional: la tabla queda intacta
        con.close()


def test_borrar_una_fila_del_medio_se_detecta(cauce_test):
    eid, _ = _registrar(cauce_test)
    _registrar(cauce_test)
    con = _su(cauce_test)
    try:
        with con.cursor() as cur:
            cur.execute("alter table audit.bitacora_corrida disable trigger user")
            cur.execute("delete from audit.bitacora_corrida where evento_id = %s", (eid,))
        assert bitacora.verificar_cadena(con) is not None
    finally:
        con.rollback()
        con.close()


def test_catalogo_cerrado_rechaza_valores_fuera_de_catalogo(cauce_test):
    # directo a `audit.registrar_evento`: `bitacora.registrar_evento` ya los frena en Python, y el CHECK es
    # la capa que queda si alguien llama a la funcion sin pasar por el modulo
    for evento, resultado, codigo_error, comando in (
            ("otra_cosa", None, None, "run"), ("corrida_fin", "quiza", None, "run"),
            ("corrida_fin", "fallo", "valor con Juan Perez", "run"), ("corrida_inicio", None, None, "borrar")):
        con = almacen.conectar(cauce_test, cauce_test.ingesta, "cauce_test")
        try:
            with con.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute("SELECT audit.registrar_evento(%s::uuid, %s, 'u@h', %s, 'v1', NULL, NULL, NULL, NULL, "
                            "NULL, NULL, %s, %s)", (str(uuid.uuid4()), evento, comando, resultado, codigo_error))
        finally:
            con.close()


def test_codigo_error_del_catalogo_se_acepta(cauce_test):
    _registrar(cauce_test, evento="corrida_fin", resultado="fallo", codigo_error="clasificacion_incompleta")
    _registrar(cauce_test, evento="corrida_fin", resultado="fallo", codigo_error="pii_en_salida")


def test_la_bitacora_no_tiene_columnas_de_texto_libre(cauce_test):
    con = _su(cauce_test)
    try:
        with con.cursor() as cur:
            cur.execute("select column_name from information_schema.columns where table_schema = 'audit' "
                        "and table_name = 'bitacora_corrida' and data_type = 'text'")
            texto = {r[0] for r in cur.fetchall()}
            assert texto <= COLUMNAS_TEXTO_LIBRE_PERMITIDAS
            cur.execute("select conrelid::regclass::text, pg_get_constraintdef(oid) from pg_constraint "
                        "where conrelid = 'audit.bitacora_corrida'::regclass and contype = 'c'")
            defs = " ".join(r[1] for r in cur.fetchall())
            for col in ("evento", "resultado", "codigo_error", "comando"):
                assert col in defs, f"{col} sin catalogo cerrado"
    finally:
        con.close()


def test_registrar_evento_toma_el_advisory_lock_y_tiene_search_path_fijo(cauce_test):
    con = _su(cauce_test)
    try:
        with con.cursor() as cur:
            cur.execute("select prosecdef, proconfig, pg_get_functiondef(oid) from pg_proc "
                        "where oid = 'audit.registrar_evento(uuid,text,text,text,text,text,bigint,"
                        "integer,integer,integer,integer,text,text)'::regprocedure")
            fila = cur.fetchone()
            assert fila is not None
            secdef, config, ddl = fila
            assert config is not None
            assert secdef is True
            assert any(c.startswith("search_path=") for c in config)
            assert "pg_advisory_xact_lock" in ddl
    finally:
        con.close()
