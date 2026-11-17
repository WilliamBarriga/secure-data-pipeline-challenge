-- Convencion: <col>_cifrado = AES-256-GCM en la app (nonce|ct|tag),
-- AAD = tabla|col|llave_natural. corrida_id es uuid sin FK en todas las zonas.

-- crypto ---------------------------------------------------------------
CREATE TABLE crypto.dek (
    key_version  integer PRIMARY KEY,
    dek_envuelta bytea NOT NULL,
    creada_en    timestamptz NOT NULL DEFAULT now()
);

-- raw ------------------------------------------------------------------
CREATE TABLE raw.archivo (
    archivo_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre     text NOT NULL,
    sha256     text NOT NULL,
    fuente     text NOT NULL
        CHECK (fuente IN ('consumo', 'cliente', 'frontera', 'limite', 'factura')),
    estado     text NOT NULL CHECK (estado IN ('cargado', 'rechazado')),
    corrida_id uuid NOT NULL,
    UNIQUE (fuente, nombre, sha256)  -- mismo contenido con otro nombre es otro archivo
);
CREATE INDEX archivo_fuente_nombre_idx ON raw.archivo (fuente, nombre);

CREATE TABLE raw.archivo_conteo (
    archivo_id bigint NOT NULL REFERENCES raw.archivo,
    contador   text NOT NULL,
    valor      integer NOT NULL,
    PRIMARY KEY (archivo_id, contador)
);

CREATE TABLE raw.lectura_consumo (
    archivo_id      bigint NOT NULL REFERENCES raw.archivo,
    linea           integer NOT NULL,
    frontera_id     text NOT NULL,
    ts_hora         timestamptz NOT NULL,
    consumo_kwh     numeric NOT NULL,
    version_lectura integer NOT NULL,
    calidad         text,
    mwh_convertida  boolean NOT NULL DEFAULT false,
    PRIMARY KEY (archivo_id, linea)
);

CREATE TABLE raw.linea (
    archivo_id   bigint NOT NULL REFERENCES raw.archivo,
    numero_linea integer NOT NULL,
    linea        text NOT NULL,
    PRIMARY KEY (archivo_id, numero_linea)
);

CREATE TABLE raw.cliente (
    archivo_id                  bigint NOT NULL REFERENCES raw.archivo,
    numero_linea                integer NOT NULL,
    cliente_id                  text,
    razon_social                text,
    ciudad                      text,
    departamento                text,
    nit_cifrado                 bytea,
    email_contacto_cifrado      bytea,
    celular_contacto_cifrado    bytea,
    direccion_cifrado           bytea,
    representante_legal_cifrado bytea,
    observaciones_enmascarada   text,
    key_version                 integer NOT NULL REFERENCES crypto.dek,
    PRIMARY KEY (archivo_id, numero_linea)
);

-- Toda factura valida del archivo (huerfanas y perdedoras incluidas, como las candidatas de consumo): `almacen`
-- resuelve desde aca la Frontera huerfana y la ganadora por factura_id. `payload_cifrado` es el cuerpo
-- completo para Cuarentena (se copia a quarantine.rechazo si la factura pierde o es huerfana).
CREATE TABLE raw.factura (
    archivo_id      bigint NOT NULL REFERENCES raw.archivo,
    factura_id      text NOT NULL,
    numero_linea    integer NOT NULL,
    frontera_id     text NOT NULL,
    generado_en     timestamptz NOT NULL,
    payload         jsonb NOT NULL,
    payload_cifrado bytea NOT NULL,
    key_version     integer NOT NULL REFERENCES crypto.dek,
    PRIMARY KEY (archivo_id, factura_id)
);
CREATE INDEX factura_factura_id_idx ON raw.factura (factura_id);

-- quarantine -----------------------------------------------------------
CREATE TABLE quarantine.rechazo (
    rechazo_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corrida_id      uuid NOT NULL,
    fuente          text NOT NULL,
    archivo_id      bigint NOT NULL REFERENCES raw.archivo,
    linea           integer,
    motivo          text NOT NULL,
    payload         text,
    payload_cifrado bytea,
    key_version     integer REFERENCES crypto.dek,
    CHECK (payload IS NULL OR payload_cifrado IS NULL)
);
CREATE INDEX rechazo_archivo_idx ON quarantine.rechazo (archivo_id);

-- core -----------------------------------------------------------------
CREATE TABLE core.dim_cliente (
    cliente_id                  text PRIMARY KEY,
    razon_social                text,
    ciudad                      text,
    departamento                text,
    nit_cifrado                 bytea,
    nit_bidx                    bytea,
    email_contacto_cifrado      bytea,
    celular_contacto_cifrado    bytea,
    direccion_cifrado           bytea,
    representante_legal_cifrado bytea,
    observaciones_enmascarada   text,
    key_version                 integer NOT NULL REFERENCES crypto.dek,
    corrida_id                  uuid NOT NULL
);
CREATE INDEX dim_cliente_nit_bidx_idx ON core.dim_cliente (nit_bidx);

-- cliente_id sin FK a dim_cliente: los maestros se cargan por archivo y en
-- cualquier orden (maestros antes que clientes); un cliente ausente es dato, no error.
CREATE TABLE core.dim_frontera (
    frontera_id    text PRIMARY KEY,
    codigo_sic     text UNIQUE,
    cliente_id     text,
    mercado        text,
    operador_red   text,
    nivel_tension  integer,
    fecha_registro date,
    estado         text,
    min_kwh        numeric,
    max_kwh        numeric
);

-- frontera_id sin FK a dim_frontera: una Frontera vista solo en consumos entra igual (y al universo de
-- Huecos); se marca en el log, no se pierde la Lectura.
CREATE TABLE core.fact_consumo_horario (
    frontera_id     text NOT NULL,
    ts_hora         timestamptz NOT NULL,
    -- date_trunc sobre timestamp sin zona es IMMUTABLE; to_char no lo es
    mes_local       date GENERATED ALWAYS AS (
        (date_trunc('month', ts_hora AT TIME ZONE 'America/Bogota'))::date
    ) STORED,
    consumo_kwh     numeric NOT NULL,
    version_lectura integer NOT NULL,
    archivo_id      bigint NOT NULL REFERENCES raw.archivo,
    corrida_id      uuid NOT NULL,
    PRIMARY KEY (frontera_id, ts_hora)
);
CREATE INDEX fact_consumo_mes_idx ON core.fact_consumo_horario (frontera_id, mes_local);

CREATE TABLE core.dim_componente_tarifa (
    concepto text PRIMARY KEY,
    nombre   text NOT NULL
);
-- Lectura del estandar CREG, no viene en los datos (ADR 0001).
INSERT INTO core.dim_componente_tarifa (concepto, nombre) VALUES
    ('G', 'Generacion'), ('T', 'Transmision'), ('D', 'Distribucion'),
    ('C', 'Comercializacion'), ('P', 'Perdidas'), ('R', 'Restricciones');

CREATE TABLE core.fact_factura (
    factura_id            text PRIMARY KEY,
    frontera_id           text NOT NULL REFERENCES core.dim_frontera,
    cliente_id            text REFERENCES core.dim_cliente,
    periodo               date NOT NULL,
    generado_en           timestamptz,
    consumo_facturado_kwh numeric,
    tarifa_cop_kwh        numeric,
    valor_total_cop       numeric,
    metodo_pago_tipo      text
);
CREATE INDEX fact_factura_periodo_idx ON core.fact_factura (frontera_id, periodo);

CREATE TABLE core.fact_factura_componente (
    factura_id    text NOT NULL REFERENCES core.fact_factura,
    concepto      text NOT NULL REFERENCES core.dim_componente_tarifa,
    valor_cop_kwh numeric NOT NULL,
    PRIMARY KEY (factura_id, concepto)
);

-- audit ----------------------------------------------------------------
-- Sin columnas de texto libre. registrar_evento, hash y append-only.
CREATE TABLE audit.bitacora_corrida (
    evento_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    corrida_id       uuid NOT NULL,
    evento           text NOT NULL CHECK (evento IN (
        'corrida_inicio', 'etapa_fin', 'archivo_cargado', 'archivo_omitido',
        'archivo_rechazado', 'corrida_fin')),
    ts               timestamptz NOT NULL DEFAULT clock_timestamp(),
    usuario_db       text NOT NULL DEFAULT session_user,
    ejecutor         text NOT NULL,
    comando          text NOT NULL CHECK (comando IN ('run', 'stream')),
    version_codigo   text NOT NULL,
    fuente           text,
    archivo_id       bigint,
    filas_leidas     integer,
    filas_cargadas   integer,
    filas_cuarentena integer,
    filas_duplicadas integer,
    resultado        text CHECK (resultado IN ('ok', 'parcial', 'fallo')),
    codigo_error     text,
    hash_previo      bytea,
    hash             bytea
);
CREATE INDEX bitacora_corrida_idx ON audit.bitacora_corrida (corrida_id);

-- Huella del DDL aplicado por bootstrap: misma huella salta el DDL.
CREATE TABLE audit.ddl_huella (
    huella      text PRIMARY KEY,
    aplicada_en timestamptz NOT NULL DEFAULT now()
);
