-- Zonas de la plataforma (un esquema por zona). Se aplica dentro de una sola
-- transaccion con SET ROLE cauce_owner: todos los objetos son de cauce_owner
-- (NOLOGIN), asi ningun login puede ALTER/DROP/DISABLE TRIGGER.
-- Modelo: docs/arquitectura/modelo-datos.dg. Los GRANT finos llegan en la fase de auditoria
-- (archivo sql/9x_*.sql nuevo: la huella del DDL cambia con cada archivo).

CREATE SCHEMA raw;
CREATE SCHEMA quarantine;
CREATE SCHEMA crypto;
CREATE SCHEMA core;
CREATE SCHEMA analytics;
CREATE SCHEMA audit;

COMMENT ON SCHEMA raw IS 'Filas fuente con linaje; PII ya cifrada; append-only';
COMMENT ON SCHEMA quarantine IS 'Todo lo rechazado, con motivo; nada se borra';
COMMENT ON SCHEMA crypto IS 'DEK envueltas; la KEK vive fuera de Postgres';
COMMENT ON SCHEMA core IS 'Modelo estrella; llaves naturales; dims SCD1';
COMMENT ON SCHEMA analytics IS 'Unico punto de acceso del analista; sin PII';
COMMENT ON SCHEMA audit IS 'Bitacora append-only';
