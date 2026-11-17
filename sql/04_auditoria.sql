-- Bitacora append-only (C5) y funciones de audit. Se aplica como cauce_owner.
-- Debe ir antes de crear funciones: ninguna queda ejecutable por PUBLIC.
ALTER DEFAULT PRIVILEGES FOR ROLE cauce_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Catalogo cerrado de codigos de error: nunca texto libre (el traceback va solo al log JSON).
ALTER TABLE audit.bitacora_corrida
    ADD CONSTRAINT bitacora_codigo_error_catalogo CHECK (codigo_error IS NULL OR codigo_error IN (
        'clasificacion_incompleta', 'archivo_corrupto', 'archivo_modificado', 'configuracion_invalida',
        'infraestructura_no_disponible', 'error_base_datos', 'error_interno', 'corrida_concurrente',
        'pii_en_salida')),
    ALTER COLUMN hash SET NOT NULL;

-- Hash del evento = sha256(hash_previo || contenido). El contenido es canonico: ts en UTC con
-- microsegundos (no depende del TimeZone de la sesion) y NULL como cadena vacia.
CREATE FUNCTION audit.hash_evento(r audit.bitacora_corrida) RETURNS bytea
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog
AS $$
    SELECT sha256(coalesce(r.hash_previo, ''::bytea) || convert_to(
        r.evento_id::text || '|' || r.corrida_id::text || '|' || r.evento || '|'
        || to_char(r.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|'
        || r.usuario_db || '|' || r.ejecutor || '|' || r.comando || '|' || r.version_codigo || '|'
        || coalesce(r.fuente, '') || '|' || coalesce(r.archivo_id::text, '') || '|'
        || coalesce(r.filas_leidas::text, '') || '|' || coalesce(r.filas_cargadas::text, '') || '|'
        || coalesce(r.filas_cuarentena::text, '') || '|' || coalesce(r.filas_duplicadas::text, '') || '|'
        || coalesce(r.resultado, '') || '|' || coalesce(r.codigo_error, ''), 'UTF8'))
$$;

-- Capa 3 (cadena): BEFORE INSERT calcula hash_previo/hash en el orden de evento_id.
-- Tambien fija ts y usuario_db: no se pueden falsificar ni con un INSERT directo.
CREATE FUNCTION audit.encadenar() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(7260101);
    NEW.ts := clock_timestamp();
    NEW.usuario_db := session_user;
    SELECT b.hash INTO NEW.hash_previo FROM audit.bitacora_corrida b ORDER BY b.evento_id DESC LIMIT 1;
    NEW.hash := audit.hash_evento(NEW);
    RETURN NEW;
END
$$;

CREATE TRIGGER bitacora_encadenar BEFORE INSERT ON audit.bitacora_corrida
    FOR EACH ROW EXECUTE FUNCTION audit.encadenar();

-- Capa 2 (triggers): UPDATE/DELETE por fila y TRUNCATE por sentencia hacen RAISE.
CREATE FUNCTION audit.rechazar_modificacion() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'audit.bitacora_corrida es append-only (% rechazado)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER bitacora_sin_update_delete BEFORE UPDATE OR DELETE ON audit.bitacora_corrida
    FOR EACH ROW EXECUTE FUNCTION audit.rechazar_modificacion();
CREATE TRIGGER bitacora_sin_truncate BEFORE TRUNCATE ON audit.bitacora_corrida
    FOR EACH STATEMENT EXECUTE FUNCTION audit.rechazar_modificacion();

-- Capa 1 (grants): ni siquiera el owner puede modificar lo escrito.
REVOKE UPDATE, DELETE, TRUNCATE ON audit.bitacora_corrida FROM PUBLIC, cauce_owner;

-- Unica puerta de escritura. DEFINER del owner con search_path fijo; el lock serializa
-- corridas concurrentes (una que pierde el advisory lock y registra `fallo` no bifurca la cadena).
CREATE FUNCTION audit.registrar_evento(
    p_corrida_id uuid, p_evento text, p_ejecutor text, p_comando text, p_version_codigo text,
    p_fuente text DEFAULT NULL, p_archivo_id bigint DEFAULT NULL,
    p_filas_leidas integer DEFAULT NULL, p_filas_cargadas integer DEFAULT NULL,
    p_filas_cuarentena integer DEFAULT NULL, p_filas_duplicadas integer DEFAULT NULL,
    p_resultado text DEFAULT NULL, p_codigo_error text DEFAULT NULL) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
DECLARE
    v_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(7260101);
    INSERT INTO audit.bitacora_corrida (
        corrida_id, evento, ts, usuario_db, ejecutor, comando, version_codigo, fuente, archivo_id,
        filas_leidas, filas_cargadas, filas_cuarentena, filas_duplicadas, resultado, codigo_error, hash)
    VALUES (
        p_corrida_id, p_evento, clock_timestamp(), session_user, p_ejecutor, p_comando, p_version_codigo,
        p_fuente, p_archivo_id, p_filas_leidas, p_filas_cargadas, p_filas_cuarentena, p_filas_duplicadas,
        p_resultado, p_codigo_error, ''::bytea)
    RETURNING evento_id INTO v_id;
    RETURN v_id;
END
$$;

-- Verificacion: evento_id del primer eslabon roto o NULL si la cadena es integra.
-- Detecta ediciones de un superusuario (que puede saltar grants y deshabilitar triggers).
CREATE FUNCTION audit.verificar_cadena() RETURNS bigint
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, audit
AS $$
DECLARE
    r audit.bitacora_corrida;
    previo bytea := NULL;
BEGIN
    FOR r IN SELECT * FROM audit.bitacora_corrida ORDER BY evento_id LOOP
        IF r.hash_previo IS DISTINCT FROM previo OR r.hash IS DISTINCT FROM audit.hash_evento(r) THEN
            RETURN r.evento_id;
        END IF;
        previo := r.hash;
    END LOOP;
    RETURN NULL;
END
$$;
