"""Seam: cauce.bootstrap.bootstrap(cfg, ...) contra Postgres real en cauce_test."""
import pathlib

import pytest

from cauce.arranque import bootstrap
from cauce.bordes import almacen

ZONAS = {"raw", "quarantine", "crypto", "core", "analytics", "audit"}


def _una(cfg, usuario, sql, database="cauce_test"):
    con = almacen.conectar(cfg, usuario, database)
    try:
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        con.close()


def test_crea_las_6_zonas_con_todo_el_ddl(cauce_test):
    cfg = cauce_test
    zonas = {r[0] for r in _una(cfg, cfg.superusuario,
             "select nspname from pg_namespace where nspname in "
             "('raw','quarantine','crypto','core','analytics','audit')")}
    assert zonas == ZONAS
    tablas = {r[0] for r in _una(cfg, cfg.superusuario,
              "select table_schema||'.'||table_name from information_schema.tables "
              "where table_schema in ('raw','quarantine','crypto','core','analytics','audit')")}
    esperadas = {"raw.archivo", "raw.archivo_conteo", "raw.lectura_consumo", "raw.linea", "raw.cliente",
                 "raw.factura", "quarantine.rechazo", "crypto.dek", "core.dim_cliente", "core.dim_frontera",
                 "core.fact_consumo_horario", "core.fact_factura", "core.fact_factura_componente",
                 "core.dim_componente_tarifa", "core.v_hueco", "analytics.v_consumo_mensual",
                 "audit.bitacora_corrida"}
    assert esperadas <= tablas


def test_todos_los_objetos_son_de_cauce_owner_nologin(cauce_test):
    cfg = cauce_test
    ajenos = _una(cfg, cfg.superusuario,
                  "select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                  "join pg_roles r on r.oid=c.relowner where n.nspname in "
                  "('raw','quarantine','crypto','core','analytics','audit') and r.rolname <> 'cauce_owner' "
                  "and c.relkind in ('r','v')")
    assert ajenos == []
    assert _una(cfg, cfg.superusuario, "select rolcanlogin from pg_roles where rolname='cauce_owner'") == [(False,)]


def test_mes_local_generada_usa_hora_bogota(cauce_test, escalar):
    cfg = cauce_test
    con = almacen.conectar(cfg, cfg.superusuario, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("insert into raw.archivo (nombre, sha256, fuente, estado, corrida_id) "
                        "values ('t', 'h-mes', 'consumo', 'cargado', gen_random_uuid()) returning archivo_id")
            aid = escalar(cur)
            cur.execute("insert into core.dim_frontera (frontera_id) values ('CO0000000001')")
            # 2026-02-01 02:00 UTC = 2026-01-31 21:00 Bogota -> mes local enero
            cur.execute("insert into core.fact_consumo_horario (frontera_id, ts_hora, consumo_kwh, version_lectura,"
                        " archivo_id, corrida_id) values ('CO0000000001', '2026-02-01T02:00:00Z', 1, 1, %s,"
                        " gen_random_uuid()) returning mes_local::text", (aid,))
            assert escalar(cur) == "2026-01-01"
    finally:
        con.rollback()
        con.close()


def test_segundo_bootstrap_con_misma_huella_salta_el_ddl(cauce_test):
    resultado = bootstrap.bootstrap(cauce_test, database="cauce_test")
    assert resultado == bootstrap.Resultado.SALTADO


def test_huella_distinta_falla_pidiendo_clean_up(cauce_test, tmp_path):
    ddl = tmp_path / "sql"
    ddl.mkdir()
    for f in sorted(pathlib.Path(bootstrap.DIR_SQL).glob("*.sql")):
        (ddl / f.name).write_text(f.read_text())
    (ddl / "99_extra.sql").write_text("create table core.otra (x int);")
    with pytest.raises(bootstrap.ErrorBootstrap, match="make clean && make up"):
        bootstrap.bootstrap(cauce_test, database="cauce_test", dir_sql=ddl)


def test_reaplica_passwords_de_roles_aunque_salte_el_ddl(cauce_test):
    from cauce.config import Usuario
    cfg = cauce_test
    nuevo = cfg.model_copy(update={"ingesta": Usuario(usuario=cfg.ingesta.usuario, password="otra-" + "p" * 30)})
    bootstrap.bootstrap(nuevo, database="cauce_test")
    almacen.conectar(nuevo, nuevo.ingesta, "cauce_test").close()
    with pytest.raises(almacen.ErrorConexion, match="no coinciden"):
        almacen.conectar(cfg, cfg.ingesta, "cauce_test")
    bootstrap.bootstrap(cfg, database="cauce_test")  # restaura
    almacen.conectar(cfg, cfg.ingesta, "cauce_test").close()


def test_auth_fallida_del_superusuario_da_mensaje_de_credenciales(cauce_test):
    from cauce.config import Usuario
    malo = cauce_test.model_copy(update={"superusuario": Usuario(usuario=cauce_test.superusuario.usuario,
                                                               password="mal-" + "x" * 30)})
    with pytest.raises(bootstrap.ErrorBootstrap, match="no coinciden con el volumen"):
        bootstrap.bootstrap(malo, database="cauce_test")


def test_los_cuatro_logins_conectan(cauce_test):
    cfg = cauce_test
    for u in (cfg.superusuario, cfg.ingesta, cfg.analista, cfg.auditor):
        almacen.conectar(cfg, u, "cauce_test").close()


def test_asegurar_dek_se_serializa_con_el_lock_y_no_crea_dos(cauce_test, escalar):
    """Dos `make up` concurrentes: quien llega con el lock tomado espera y luego ve la DEK ya creada."""
    import threading
    import time

    from psycopg2 import sql as _sql
    cfg, base = cauce_test, "cauce_dek_race"
    bootstrap.bootstrap(cfg, database=base, recrear=True)
    a = almacen.conectar(cfg, cfg.superusuario, base)
    b = almacen.conectar(cfg, cfg.superusuario, base)
    try:
        with a.cursor() as cur:
            cur.execute("DELETE FROM crypto.dek")
        a.commit()
        with a.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (bootstrap.LOCK_DEK,))  # "otro make up" en curso
        hilo = threading.Thread(target=bootstrap._asegurar_dek, args=(b, cfg))
        hilo.start()
        time.sleep(0.5)
        assert hilo.is_alive(), "no esperó el lock de la DEK"
        with a.cursor() as cur:
            cur.execute("INSERT INTO crypto.dek (key_version, dek_envuelta) VALUES (1, %s)", (b"x",))
        a.commit()  # suelta el lock
        hilo.join(10)
        assert not hilo.is_alive()
        with a.cursor() as cur:
            cur.execute("SELECT count(*) FROM crypto.dek")
            assert escalar(cur) == 1
    finally:
        a.close()
        b.close()
        c = almacen.conectar(cfg, cfg.superusuario, "postgres")
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(_sql.Identifier(base)))
        c.close()
