"""`bootstrap`: roles, DDL completo en una tx como cauce_owner, huella sha256.

- Roles (cluster): cauce_owner NOLOGIN + ingesta/analista/auditor LOGIN. Los passwords
  se reaplican siempre (ALTER ROLE), asi un segundo `make up` es seguro.
- DDL: sql/*.sql en orden, una sola tx con SET LOCAL ROLE cauce_owner. La huella
  (sha256 del contenido) se guarda en audit.ddl_huella: misma huella salta el DDL,
  distinta falla pidiendo `make clean && make up` (sin migraciones a medias).
- DEK: `crypto.dek` es SELECT-only para `ingesta`; aca, como superusuario, se genera la DEK y se
  envuelve con la KEK de `estado/.env` (que `preparar` crea solo si falta) si la tabla esta vacia. `run`
  falla con "make clean && make up" si la KEK no la desenvuelve.
- Los GRANT finos son sql/04_auditoria.sql y sql/05_grants.sql; lo que exige superusuario
  (REVOKE a PUBLIC, CONNECT, read-only y limites por rol) va en `_endurecer` y se reaplica siempre.
"""
from __future__ import annotations

import enum
import hashlib
from pathlib import Path

import psycopg2
from psycopg2 import sql

from cauce.bordes import almacen
from cauce.config import Config, Usuario
from cauce.nucleo import custodia

DIR_SQL = Path(__file__).resolve().parents[3] / "sql"
OWNER = "cauce_owner"
LIMITE_CONEXIONES = 10
TIMEOUT_CONSULTA = "30s"
LOCK_DEK = 0x4249415F44454B  # "CAUCE_DEK": serializa la creacion de la primera DEK entre `make up` concurrentes


class ErrorBootstrap(RuntimeError):
    """Falla de bootstrap con mensaje accionable, sin valores de credenciales."""
    codigo_error = "configuracion_invalida"


class Resultado(enum.Enum):
    APLICADO = "aplicado"
    SALTADO = "saltado"


def huella(dir_sql: Path = DIR_SQL) -> str:
    h = hashlib.sha256()
    for f in sorted(Path(dir_sql).glob("*.sql")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _conectar_su(cfg: Config, database: str):
    try:
        con = almacen.conectar(cfg, cfg.superusuario, database)
    except almacen.ErrorConexion as e:
        raise ErrorBootstrap(str(e)) from None
    con.autocommit = True
    return con


def _recrear_base(cfg: Config, database: str) -> None:
    con = _conectar_su(cfg, "postgres")
    try:
        with con.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    finally:
        con.close()


def _asegurar_roles(cur, cfg: Config, database: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (OWNER,))
    if cur.fetchone() is None:
        cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(OWNER)))
    cur.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(OWNER)))
    _rol_login(cur, cfg.ingesta)
    _rol_login(cur, cfg.analista, solo_lectura=True)
    _rol_login(cur, cfg.auditor, solo_lectura=True)
    _endurecer(cur, cfg, database)


def _endurecer(cur, cfg: Config, database: str) -> None:
    """Endurecimiento que exige superusuario (C4); idempotente, corre en cada bootstrap.

    Los GRANT por objeto viven en sql/05_grants.sql (corren como cauce_owner).
    """
    db = sql.Identifier(database)
    cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(db))
    for u in (cfg.ingesta, cfg.analista, cfg.auditor):
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, sql.Identifier(u.usuario)))
    cur.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    # crear large objects es escribir: sin EXECUTE de las funciones lo_* para PUBLIC
    cur.execute("SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'pg_catalog'::regnamespace "
                "AND (p.proname LIKE 'lo\\_%' OR p.proname IN ('loread', 'lowrite'))")
    for (firma,) in cur.fetchall():
        cur.execute(sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(sql.SQL(firma)))


def _rol_login(cur, u: Usuario, solo_lectura: bool = False) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (u.usuario,))
    verbo = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(sql.SQL(verbo + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                        "CONNECTION LIMIT {} PASSWORD {}")
                .format(sql.Identifier(u.usuario), sql.Literal(LIMITE_CONEXIONES), sql.Literal(u.password)))
    if solo_lectura:
        ident = sql.Identifier(u.usuario)
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(ident, sql.Literal(TIMEOUT_CONSULTA)))


def _asegurar_dek(con, cfg: Config) -> None:
    """Primera DEK (version 1) envuelta con la KEK, solo si `crypto.dek` esta vacia."""
    with con.cursor() as cur:
        # se suelta en el commit; exige con.autocommit=False
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_DEK,))
        cur.execute("SELECT 1 FROM crypto.dek LIMIT 1")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO crypto.dek (key_version, dek_envuelta) VALUES (1, %s)",
                        (custodia.nueva_dek(cfg.kek, 1),))
    con.commit()


def bootstrap(cfg: Config, database: str | None = None, recrear: bool = False,
              dir_sql: Path = DIR_SQL) -> Resultado:
    database = database or cfg.database
    if recrear:
        _recrear_base(cfg, database)
    con = _conectar_su(cfg, database)
    try:
        with con.cursor() as cur:
            _asegurar_roles(cur, cfg, database)
            cur.execute("SELECT to_regclass('audit.ddl_huella')")
            fila = cur.fetchone()  # to_regclass devuelve siempre una fila: NULL si la tabla no existe
            existe = fila is not None and fila[0] is not None
            actual = huella(dir_sql)
            if existe:
                cur.execute("SELECT huella FROM audit.ddl_huella")
                guardadas = {r[0] for r in cur.fetchall()}
                if guardadas == {actual}:
                    con.autocommit = False
                    _asegurar_dek(con, cfg)
                    return Resultado.SALTADO
                raise ErrorBootstrap("el DDL cambió respecto al aplicado en el volumen: "
                                     "corre `make clean && make up`")
        con.autocommit = False
        try:
            with con.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER)))
                for f in sorted(Path(dir_sql).glob("*.sql")):
                    cur.execute(f.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO audit.ddl_huella (huella) VALUES (%s)", (actual,))
            con.commit()
            _asegurar_dek(con, cfg)
        except psycopg2.Error as e:
            con.rollback()
            raise ErrorBootstrap(f"falló el DDL (sqlstate {e.pgcode}); no se aplicó nada") from None
        return Resultado.APLICADO
    finally:
        con.close()
