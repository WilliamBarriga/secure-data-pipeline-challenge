-- Grants minimos por rol (C4). Los roles de login ya existen (bootstrap los crea antes del DDL).
-- CONNECT, REVOKE de PUBLIC sobre la base/schema public/lo_* y los ajustes por rol
-- (read-only, statement_timeout, CONNECTION LIMIT) los aplica bootstrap: requieren superusuario.
-- Ninguno de los tres roles es dueno de nada; sin DELETE ni TRUNCATE en ninguna zona.

-- ingesta ---------------------------------------------------------------
GRANT USAGE ON SCHEMA raw, quarantine, crypto, core, audit TO ingesta;
-- raw y quarantine son append-only: solo SELECT (lo pide ON CONFLICT) + INSERT.
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA raw, quarantine TO ingesta;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO ingesta;
REVOKE INSERT, UPDATE ON core.v_hueco FROM ingesta;
GRANT SELECT ON crypto.dek TO ingesta;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA raw, quarantine, core TO ingesta;
-- Bitacora: solo la funcion; sin acceso directo a la tabla.
GRANT EXECUTE ON FUNCTION audit.registrar_evento TO ingesta;

-- analista --------------------------------------------------------------
-- La vista corre con los privilegios del owner (security_invoker = false): cero grants en core.
GRANT USAGE ON SCHEMA analytics TO analista;
GRANT SELECT ON analytics.v_consumo_mensual TO analista;

-- auditor ---------------------------------------------------------------
GRANT USAGE ON SCHEMA audit TO auditor;
GRANT SELECT ON audit.bitacora_corrida TO auditor;
GRANT EXECUTE ON FUNCTION audit.verificar_cadena, audit.hash_evento TO auditor;
