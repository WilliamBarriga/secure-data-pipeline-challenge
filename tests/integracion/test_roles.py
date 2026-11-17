"""Menor privilegio real (C4): cada prohibicion se intenta con el rol y debe fallar."""
import psycopg2
import pytest
from psycopg2 import errors

from cauce.bordes import almacen

FIRMA_REGISTRAR = ("audit.registrar_evento(uuid,text,text,text,text,text,bigint,"
                   "integer,integer,integer,integer,text,text)")


def _intentar(cfg, usuario, sentencia, parametros=None):
    """Ejecuta como `usuario` en cauce_test.

    Devuelve filas ([] si la sentencia no las tiene) o lanza el error de Postgres.
    """
    con = almacen.conectar(cfg, usuario, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute(sentencia, parametros)
            return cur.fetchall() if cur.description else []
    finally:
        con.rollback()
        con.close()


def _su(cfg, sentencia):
    return _intentar(cfg, cfg.superusuario, sentencia)


def _prohibido(cfg, usuario, sentencia):
    with pytest.raises(psycopg2.Error) as e:
        _intentar(cfg, usuario, sentencia)
    assert isinstance(e.value, (errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)), \
        f"{sentencia!r} fallo por otra razon (sqlstate {e.value.pgcode})"


def _sin_public(acl: str | None) -> bool:
    """True si el aclitem text (`{a=r/o,=U/o}`) no otorga nada a PUBLIC (grantee vacio)."""
    if not acl:
        return True
    return not any(p.startswith("=") for p in acl.strip("{}").split(","))


# --- ingesta ---------------------------------------------------------------
def test_ingesta_tiene_usage_en_sus_esquemas_y_ninguno_en_analytics(cauce_test):
    cfg = cauce_test
    for z in ("raw", "quarantine", "crypto", "core", "audit"):
        assert _intentar(cfg, cfg.ingesta, f"select has_schema_privilege('{z}', 'USAGE')") == [(True,)]
    assert _intentar(cfg, cfg.ingesta, "select has_schema_privilege('analytics', 'USAGE')") == [(False,)]
    _prohibido(cfg, cfg.ingesta, "select * from analytics.v_consumo_mensual")


def test_ingesta_inserta_en_raw_quarantine_y_core(cauce_test, escalar):
    cfg = cauce_test
    con = almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("insert into raw.archivo (nombre, sha256, fuente, estado, corrida_id) "
                        "values ('t', 'h-roles', 'cliente', 'cargado', gen_random_uuid()) returning archivo_id")
            aid = escalar(cur)
            cur.execute("insert into quarantine.rechazo (corrida_id, fuente, archivo_id, motivo) "
                        "values (gen_random_uuid(), 'cliente', %s, 'm')", (aid,))
            cur.execute("insert into core.dim_frontera (frontera_id) values ('CO0000000009')")
            cur.execute("update core.dim_frontera set mercado = 'x' where frontera_id = 'CO0000000009'")
            cur.execute("select count(*) from crypto.dek")
    finally:
        con.rollback()
        con.close()


@pytest.mark.parametrize("sentencia", [
    "update raw.archivo set estado = 'rechazado'",
    "update raw.cliente set ciudad = 'x'",
    "update quarantine.rechazo set motivo = 'x'",
    "delete from raw.archivo",
    "delete from quarantine.rechazo",
    "delete from core.dim_cliente",
    "truncate raw.archivo cascade",
    "truncate core.dim_frontera cascade",
    "insert into crypto.dek (key_version, dek_envuelta) values (1, '\\x00')",
    "update crypto.dek set dek_envuelta = '\\x00'",
])
def test_ingesta_sin_update_en_raw_quarantine_ni_delete_truncate(cauce_test, sentencia):
    _prohibido(cauce_test, cauce_test.ingesta, sentencia)


def test_ingesta_sobre_la_bitacora_solo_ejecuta_registrar_evento(cauce_test):
    cfg = cauce_test
    for s in ("select * from audit.bitacora_corrida",
              "insert into audit.bitacora_corrida (corrida_id, evento, ejecutor, comando, version_codigo) "
              "values (gen_random_uuid(), 'corrida_inicio', 'x', 'run', 'v')",
              "update audit.bitacora_corrida set ejecutor = 'x'",
              "delete from audit.bitacora_corrida",
              "truncate audit.bitacora_corrida"):
        _prohibido(cfg, cfg.ingesta, s)
    assert _intentar(cfg, cfg.ingesta,
                     f"select has_function_privilege('{FIRMA_REGISTRAR}', 'EXECUTE')") == [(True,)]


def test_ingesta_usa_secuencias_de_raw(cauce_test):
    cfg = cauce_test
    assert _intentar(cfg, cfg.ingesta,
                     "select has_sequence_privilege('raw.archivo_archivo_id_seq', 'USAGE')") == [(True,)]


# --- analista --------------------------------------------------------------
def test_analista_solo_ve_la_vista_analitica(cauce_test):
    cfg = cauce_test
    assert _intentar(cfg, cfg.analista, "select count(*) from analytics.v_consumo_mensual") == [(0,)]
    for s in ("select * from core.dim_cliente", "select * from core.dim_frontera",
              "select * from raw.cliente", "select * from quarantine.rechazo",
              "select * from audit.bitacora_corrida", "select * from crypto.dek",
              "select * from core.v_hueco"):
        _prohibido(cfg, cfg.analista, s)


@pytest.mark.parametrize("sentencia", [
    "insert into core.dim_frontera (frontera_id) values ('X')",
    "create table public.t (x int)",
    "create table analytics.t (x int)",
    "create temp table t (x int)",
    "select lo_create(0)",
    "select lo_from_bytea(0, 'x')",
    "select lo_creat(-1)",
    "create schema s",
])
def test_analista_no_escribe_ni_crea_nada(cauce_test, sentencia):
    _prohibido(cauce_test, cauce_test.analista, sentencia)


def test_analista_y_auditor_son_read_only_con_timeout_a_nivel_de_rol(cauce_test):
    cfg = cauce_test
    for u in (cfg.analista, cfg.auditor):
        assert _intentar(cfg, u, "show default_transaction_read_only") == [("on",)]
        assert _intentar(cfg, u, "show statement_timeout") != [("0",)]
        _prohibido(cfg, u, "create table public.t (x int)")


def test_la_vista_analitica_es_definer_con_security_barrier(cauce_test):
    r = _su(cauce_test, "select reloptions from pg_class where oid = 'analytics.v_consumo_mensual'::regclass")
    opciones = r[0][0] or []
    assert "security_barrier=true" in opciones
    assert "security_invoker=true" not in opciones


# --- auditor ---------------------------------------------------------------
def test_auditor_lee_solo_la_bitacora(cauce_test):
    cfg = cauce_test
    _intentar(cfg, cfg.auditor, "select count(*) from audit.bitacora_corrida")
    for s in ("select * from audit.ddl_huella", "select * from core.dim_cliente",
              "select * from analytics.v_consumo_mensual", "select * from quarantine.rechazo",
              "update audit.bitacora_corrida set ejecutor = 'x'",
              "delete from audit.bitacora_corrida", "truncate audit.bitacora_corrida"):
        _prohibido(cfg, cfg.auditor, s)


# --- PUBLIC y endurecimiento global ----------------------------------------
def test_public_sin_connect_temp_ni_schema_public(cauce_test):
    cfg = cauce_test
    assert _sin_public(_su(cfg, "select datacl::text from pg_database where datname = 'cauce_test'")[0][0])
    assert _sin_public(_su(cfg, "select nspacl::text from pg_namespace where nspname = 'public'")[0][0])


def test_roles_tienen_connect_y_limite_de_conexiones(cauce_test):
    cfg = cauce_test
    for u in (cfg.ingesta, cfg.analista, cfg.auditor):
        assert _su(cfg, f"select has_database_privilege('{u.usuario}', 'cauce_test', 'CONNECT')") == [(True,)]
        assert _su(cfg, f"select rolconnlimit > 0 from pg_roles where rolname = '{u.usuario}'") == [(True,)]


def test_funciones_lo_sin_execute_para_public(cauce_test):
    expuestas = _su(cauce_test,
                    "select p.oid::regprocedure::text from pg_proc p "
                    "where p.pronamespace = 'pg_catalog'::regnamespace "
                    "and (p.proname like 'lo\\_%' or p.proname in ('loread', 'lowrite')) "
                    "and (p.proacl is null or exists (select 1 from aclexplode(p.proacl) a "
                    "where a.grantee = 0 and a.privilege_type = 'EXECUTE'))")
    assert expuestas == []


def test_funciones_de_audit_sin_execute_para_public_por_default_privileges(cauce_test):
    r = _su(cauce_test,
            "select p.oid::regprocedure::text from pg_proc p where p.pronamespace = 'audit'::regnamespace "
            "and (p.proacl is null or exists (select 1 from aclexplode(p.proacl) a where a.grantee = 0))")
    assert r == []
