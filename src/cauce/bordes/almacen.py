"""Borde con I/O hacia Postgres (rol ingesta): conexion, lock de Corrida, registro y carga de archivos. Las Fotos
de `out/` son de `fotos`, que lee la base y comparte el orden B3 (`ORDEN_B3`).

`cargar` solo acepta `custodia.ResultadoCustodiado` (custodia es la unica puerta). Escribe el linaje comun
(raw.archivo, raw.linea, Cuarentena, raw.archivo_conteo) y delega lo propio de cada fuente en el cargador de su
tipo de carga (`fuentes.ficha_por_lotes(fuente).carga`): maestros como upsert SCD1, consumos como candidatos + merge B3,
facturas con huerfanas y duplicadas resueltas en la base. Las columnas salen siempre de la fila custodiada; solo
lo que pone la base es explicito (`corrida_id`, el `cliente_id` de factura). Orden del modulo: conexion y lock ->
registro -> SQL dinamico -> SQL fijo -> una seccion por fuente -> `cargar` -> conteos del archivo.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

from cauce.config import Config, Usuario
from cauce.nucleo import custodia, fuentes
from cauce.nucleo.clasificacion import ZONAS
from cauce.nucleo.custodia import ResultadoCustodiado

# Las 6 zonas (`ZONAS`) se declaran una vez en `clasificacion`; `almacen.ZONAS` es la misma tupla.
# pg_catalog y no information_schema: este ultimo oculta las columnas sin privilegio para el rol conectado.
_COLUMNAS = """
SELECT n.nspname || '.' || c.relname || '.' || a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0 AND NOT a.attisdropped
"""


def columnas_modelo(con) -> dict[str, str]:
    """esquema.tabla.columna -> tipo, de las 6 zonas (tablas y vistas)."""
    with con.cursor() as cur:
        cur.execute(_COLUMNAS, (list(ZONAS),))
        return dict(cur.fetchall())


class ErrorConexion(RuntimeError):
    """No se pudo conectar. Nunca incluye el mensaje del driver (puede traer valores)."""
    codigo_error = "infraestructura_no_disponible"


def conectar(cfg: Config, usuario: Usuario, database: str | None = None):
    try:
        return psycopg2.connect(host=cfg.host, port=cfg.port, dbname=database or cfg.database,
                                user=usuario.usuario, password=usuario.password, connect_timeout=10)
    except psycopg2.OperationalError as e:
        texto = str(e)
        if "password authentication failed" in texto:
            raise ErrorConexion(
                f"autenticacion fallida como {usuario.usuario}: las credenciales de credenciales.yaml no "
                "coinciden con el volumen existente; corre `make clean && make up`") from None
        raise ErrorConexion(
            f"no se pudo conectar a Postgres en {cfg.host}:{cfg.port}: corre `make up`") from None


# --- Corrida concurrente -----------------------------------------------------------------------
LOCK_CORRIDA = 7260102  # distinto del lock de la cadena de hash de la Bitacora (7260101)


class CorridaConcurrente(RuntimeError):
    """Otra Corrida tiene el lock: esta falla de inmediato, no espera."""
    codigo_error = "corrida_concurrente"


@contextmanager
def lock_corrida(con):
    """`pg_try_advisory_lock` de sesion, un solo intento: si otra Corrida lo tiene, falla rapido.

    Las Corridas nunca se pisan. Los commits no lo sueltan; se libera al salir (o al cerrar la conexion).
    """
    with con.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_CORRIDA,))
        tomado = cur.fetchone()[0]
    con.commit()
    if not tomado:
        raise CorridaConcurrente("otra Corrida sigue en curso")
    try:
        yield
    finally:
        try:
            con.rollback()
            with con.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_CORRIDA,))
            con.commit()
        except psycopg2.Error:
            pass  # conexion rota: el servidor libera el lock al cerrarla


# --- Registro y carga de archivos ----------------------------------------------------------------
class Estado(StrEnum):
    NUEVO = "Nuevo"
    OMITIDO = "Omitido"
    MODIFICADO = "Modificado"


def registrar(con, fuente: str, nombre: str, sha256: str) -> Estado:
    """Clasifica un archivo descubierto contra `raw.archivo`; solo lee.

    Omitido: ese nombre ya se registro con ese contenido (sha256). Modificado: el nombre ya se registro con
    otro contenido (el maestro se reaplica como upsert SCD1; consumos/facturacion lo rechazan en `ingerir`).
    Nuevo: ni uno ni otro; el mismo contenido bajo otro nombre tambien es Nuevo. `raw.archivo` lo inserta
    `cargar` junto con el estado final.
    """
    with con.cursor() as cur:
        cur.execute("SELECT 1 FROM raw.archivo WHERE fuente = %s AND nombre = %s AND sha256 = %s",
                    (fuente, nombre, sha256))
        if cur.fetchone():
            return Estado.OMITIDO
        cur.execute("SELECT 1 FROM raw.archivo WHERE fuente = %s AND nombre = %s LIMIT 1", (fuente, nombre))
        return Estado.MODIFICADO if cur.fetchone() else Estado.NUEVO


def archivo_id_por_sha(con, fuente: str, nombre: str, sha256: str) -> int | None:
    """archivo_id de un archivo ya registrado (lo que une a la Bitacora con nombre y sha256)."""
    with con.cursor() as cur:
        cur.execute("SELECT archivo_id FROM raw.archivo WHERE fuente = %s AND nombre = %s AND sha256 = %s",
                    (fuente, nombre, sha256))
        fila = cur.fetchone()
        return fila[0] if fila else None


def abrir_dek(con, custodio: custodia.Custodio) -> None:
    """Abre con la KEK la DEK vigente de `crypto.dek` (ingesta solo la lee; la crea `bootstrap`).

    Levanta `KekInvalida` ("make clean && make up") si no hay DEK o la KEK no la desenvuelve.
    """
    with con.cursor() as cur:
        cur.execute("SELECT key_version, dek_envuelta FROM crypto.dek ORDER BY key_version DESC LIMIT 1")
        fila = cur.fetchone()
    con.commit()
    if fila is None:
        raise custodia.KekInvalida("no hay DEK en crypto.dek")
    custodio.abrir(fila[0], bytes(fila[1]))


# --- SQL dinamico (identificadores validados) ----------------------------------------------------
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(nombre: str) -> sql.Identifier:
    """Identificador validado. Sale del codigo (`fuentes.FICHAS`) o de las claves que arma custodia, nunca del
    archivo de entrada; el regex es defensa adicional. El mensaje no incluye el valor."""
    if not _IDENT.fullmatch(nombre):
        raise ValueError("identificador SQL invalido")
    return sql.Identifier(nombre)


def _tabla(nombre: str) -> sql.Composed:
    esquema, _, tabla = nombre.partition(".")
    return sql.SQL(".").join([_ident(esquema), _ident(tabla)])


def _raw_de(r: custodia.RegistroCustodiado) -> Mapping[str, Any]:
    if r.raw is None:  # custodia lo garantiza para fuentes con tabla raw
        raise ValueError("registro sin columnas raw para una fuente con tabla raw")
    return r.raw


def _columnas_lote(mapas: list[Mapping[str, Any]]) -> tuple[str, ...]:
    """Las claves del lote; todas las filas deben tener las mismas (error sin valores)."""
    columnas = tuple(mapas[0])
    if any(tuple(m) != columnas for m in mapas[1:]):
        raise ValueError("las filas del lote no tienen las mismas columnas")
    return columnas


def _sql_insert_raw(tabla: str, columnas: Sequence[str], linea: str = "numero_linea") -> sql.Composed:
    """INSERT a la tabla raw: (archivo_id, <linea>, *columnas) VALUES %s. `linea`: la columna de linaje de la
    linea (`raw.lectura_consumo` la llama `linea`)."""
    cols = sql.SQL(", ").join(_ident(c) for c in ("archivo_id", linea, *columnas))
    return sql.SQL("INSERT INTO {} ({}) VALUES %s").format(_tabla(tabla), cols)


def _sql_upsert_core(tabla: str, clave: str, columnas: Sequence[str]) -> sql.Composed:
    """INSERT ... ON CONFLICT (clave) DO UPDATE SET col = EXCLUDED.col para todo menos la llave (SCD1 por
    llave natural, no por ciphertext, que cambia en cada corrida). La Banda operativa (`limite`) se pliega
    sobre la Frontera: sus columnas son solo min/max, asi que solo esas se actualizan."""
    cols = sql.SQL(", ").join(_ident(c) for c in columnas)
    resto = [c for c in columnas if c != clave]
    if not resto:  # solo la llave: no hay nada que actualizar
        return sql.SQL("INSERT INTO {} ({}) VALUES %s ON CONFLICT ({}) DO NOTHING").format(
            _tabla(tabla), cols, _ident(clave))
    sets = sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(_ident(c)) for c in resto)
    return sql.SQL("INSERT INTO {} ({}) VALUES %s ON CONFLICT ({}) DO UPDATE SET {}").format(
        _tabla(tabla), cols, _ident(clave), sets)


def _insertar_raw(cur, tabla: str, registros: Sequence[custodia.RegistroCustodiado], archivo_id: int,
                  previas: Sequence[str] = (), linea: str = "numero_linea", page_size: int = 100) -> None:
    """Columnas raw de cada registro, con linaje (archivo_id, linea). `previas`: columnas del core que tambien van
    a raw, antes de las de raw (la llave de factura)."""
    raws = [_raw_de(r) for r in registros]
    cols = _columnas_lote(raws)
    execute_values(cur, _sql_insert_raw(tabla, (*previas, *cols), linea).as_string(cur),
                   [(archivo_id, r.linea, *(r.core[c] for c in previas), *(raw[c] for c in cols))
                    for r, raw in zip(registros, raws, strict=True)], page_size=page_size)


# --- SQL fijo ------------------------------------------------------------------------------------
# Desempate de la ultima posicion: nombre de archivo en collation C y luego linea. Lo usan B3 (consumos) y las
# Facturas duplicadas; cada uno antepone su propio criterio (version_lectura | generado_en).
_DESEMPATE = 'a.nombre COLLATE "C" DESC, {linea} DESC'
# B3: gana la mayor version_lectura y, en empate, la ultima posicion. Publico: los cubos de `fotos` lo comparten.
ORDEN_B3 = "l.version_lectura DESC, " + _DESEMPATE.format(linea="l.linea")
# Facturas: gana la mayor generado_en y, en empate, la ultima posicion
_ORDEN_FACTURA = "f.generado_en DESC, " + _DESEMPATE.format(linea="f.numero_linea")

_SQL_CUARENTENA = ("INSERT INTO quarantine.rechazo (corrida_id, fuente, archivo_id, linea, motivo, payload, "
                   "payload_cifrado, key_version) VALUES %s")
# Cuarentena de una Factura ya guardada en raw.factura: su cuerpo cifrado ya esta ahi (`almacen` no ve la llave)
_SQL_CUARENTENA_FACTURA = """
INSERT INTO quarantine.rechazo (corrida_id, fuente, archivo_id, linea, motivo, payload_cifrado, key_version)
SELECT %(corrida)s, 'factura', f.archivo_id, f.numero_linea, {motivo}, f.payload_cifrado, f.key_version
"""


# --- consumo ---------------------------------------------------------------------------------------
# B3 sobre las llaves (frontera, hora) que toca el archivo. Solo actualiza si cambio el ganador.
_SQL_MERGE_B3 = f"""
INSERT INTO core.fact_consumo_horario (frontera_id, ts_hora, consumo_kwh, version_lectura, archivo_id, corrida_id)
SELECT g.frontera_id, g.ts_hora, g.consumo_kwh, g.version_lectura, g.archivo_id, %(corrida)s
FROM (
    SELECT DISTINCT ON (l.frontera_id, l.ts_hora) l.frontera_id, l.ts_hora, l.consumo_kwh, l.version_lectura,
           l.archivo_id
    FROM raw.lectura_consumo l
    JOIN raw.archivo a USING (archivo_id)
    WHERE (l.frontera_id, l.ts_hora) IN (
        SELECT frontera_id, ts_hora FROM raw.lectura_consumo WHERE archivo_id = %(archivo)s)
    ORDER BY l.frontera_id, l.ts_hora, {ORDEN_B3}
) g
ON CONFLICT (frontera_id, ts_hora) DO UPDATE
SET consumo_kwh = EXCLUDED.consumo_kwh, version_lectura = EXCLUDED.version_lectura,
    archivo_id = EXCLUDED.archivo_id, corrida_id = EXCLUDED.corrida_id
WHERE core.fact_consumo_horario.archivo_id IS DISTINCT FROM EXCLUDED.archivo_id
"""


# Fronteras distintas del archivo ausentes de `core.dim_frontera`: sin FK, se cargan igual y quedan contadas.
CONTADOR_FUERA_MAESTRO = "fronteras_fuera_maestro"
_SQL_FUERA_MAESTRO = """
SELECT count(DISTINCT l.frontera_id) FROM raw.lectura_consumo l
WHERE l.archivo_id = %s AND NOT EXISTS (SELECT 1 FROM core.dim_frontera f WHERE f.frontera_id = l.frontera_id)
"""


def _cargar_consumo(cur, resultado: ResultadoCustodiado, archivo_id: int, corrida_id: uuid.UUID) -> Mapping[str, int]:
    """Candidatos (toda Lectura valida, perdedoras incluidas) a raw.lectura_consumo y merge B3 a core. Suma
    `CONTADOR_FUERA_MAESTRO` a los conteos solo si es distinto de cero (como los contadores B)."""
    if not resultado.registros:
        return resultado.conteos
    destino = fuentes.ficha_por_lotes(resultado.fuente).destino
    _insertar_raw(cur, destino.raw_requerida, resultado.registros, archivo_id, linea="linea", page_size=5000)
    cur.execute(_SQL_MERGE_B3, {"corrida": str(corrida_id), "archivo": archivo_id})
    cur.execute(_SQL_FUERA_MAESTRO, (archivo_id,))
    fuera = cur.fetchone()[0]
    return {**resultado.conteos, CONTADOR_FUERA_MAESTRO: fuera} if fuera else resultado.conteos


# --- factura ---------------------------------------------------------------------------------------
# Toda factura valida del archivo esta en raw.factura. Huerfana = Frontera ausente de dim_frontera (va a Cuarentena y
# no compite). Entre las demas gana por factura_id `_ORDEN_FACTURA`. Toda factura que pierde cae en Cuarentena una
# sola vez, tambien la fila vieja desplazada por un archivo posterior. Los INSERT ... SELECT a Cuarentena ordenan por
# (archivo, linea): el `rechazo_id` no depende del plan de Postgres.
_SQL_FACTURA_HUERFANAS = _SQL_CUARENTENA_FACTURA.format(motivo="'frontera_huerfana'") + """
FROM raw.factura f
WHERE f.archivo_id = %(archivo)s
  AND NOT EXISTS (SELECT 1 FROM core.dim_frontera d WHERE d.frontera_id = f.frontera_id)
ORDER BY f.archivo_id, f.numero_linea
"""
_RANGO_FACTURA = f"""
WITH r AS (
SELECT f.archivo_id, f.factura_id, f.numero_linea,
       row_number() OVER (PARTITION BY f.factura_id ORDER BY {_ORDEN_FACTURA}) AS rn
FROM raw.factura f JOIN raw.archivo a USING (archivo_id)
WHERE EXISTS (SELECT 1 FROM core.dim_frontera d WHERE d.frontera_id = f.frontera_id)
  AND f.factura_id IN (SELECT factura_id FROM raw.factura WHERE archivo_id = %(archivo)s)
)
"""
_SQL_FACTURA_PERDEDORAS = _RANGO_FACTURA + _SQL_CUARENTENA_FACTURA.format(motivo="'factura_duplicada'") + """
FROM r JOIN raw.factura f USING (archivo_id, factura_id)
WHERE r.rn > 1 AND NOT EXISTS (
    SELECT 1 FROM quarantine.rechazo q WHERE q.fuente = 'factura' AND q.archivo_id = r.archivo_id
      AND q.linea = r.numero_linea AND q.motivo = 'factura_duplicada')
ORDER BY r.archivo_id, r.numero_linea
"""
_SQL_FACTURA_RANGOS = _RANGO_FACTURA + "SELECT factura_id, rn FROM r WHERE archivo_id = %(archivo)s"

# Lo unico de core.fact_factura que pone la base: cliente_id se resuelve por frontera_id -> dim_frontera; NULL si la
# Frontera no trae Cliente o no esta en el maestro. El resto de columnas sale de la fila custodiada.
_CLIENTE_DE_FRONTERA = ("(SELECT c.cliente_id FROM core.dim_frontera f JOIN core.dim_cliente c USING (cliente_id) "
                        "WHERE f.frontera_id = %(frontera_id)s)")
_SQL_FACTURA_COMPONENTES = """
INSERT INTO core.fact_factura_componente (factura_id, concepto, valor_cop_kwh) VALUES %s
ON CONFLICT (factura_id, concepto) DO UPDATE SET valor_cop_kwh = EXCLUDED.valor_cop_kwh
"""


def _cargar_factura(cur, resultado: ResultadoCustodiado, archivo_id: int, corrida_id: uuid.UUID) -> Mapping[str, int]:
    """raw.factura, Cuarentena de huerfanas y perdedoras y core de las ganadoras. Devuelve los conteos finales
    (leidas, cargadas, rechazadas): una huerfana o perdedora del archivo cuenta como rechazada."""
    if not resultado.registros:
        return resultado.conteos
    destino = fuentes.ficha_por_lotes(resultado.fuente).destino
    _insertar_raw(cur, destino.raw_requerida, resultado.registros, archivo_id, previas=(destino.clave,))
    params = {"corrida": str(corrida_id), "archivo": archivo_id}
    cur.execute(_SQL_FACTURA_HUERFANAS, params)
    cur.execute(_SQL_FACTURA_PERDEDORAS, params)
    cur.execute(_SQL_FACTURA_RANGOS, params)
    rangos = dict(cur.fetchall())
    ganadoras = [r.core for r in resultado.registros if rangos.get(r.core["factura_id"]) == 1]
    if ganadoras:
        # `componentes` no es columna: va a core.fact_factura_componente
        cols = tuple(c for c in _columnas_lote(ganadoras) if c != "componentes")
        plantilla = "(" + ", ".join([*(f"%({c})s" for c in cols), _CLIENTE_DE_FRONTERA]) + ")"
        execute_values(cur, _sql_upsert_core(destino.core, destino.clave, (*cols, "cliente_id")).as_string(cur),
                       ganadoras, template=plantilla)
        componentes = [(c["factura_id"], concepto, valor) for c in ganadoras for concepto, valor in c["componentes"]]
        if componentes:
            execute_values(cur, _SQL_FACTURA_COMPONENTES, componentes)
    rechazadas = len(resultado.registros) - len(ganadoras) + len(resultado.rechazos)
    return {**resultado.conteos, "cargadas": len(ganadoras), "rechazadas": rechazadas}


# --- maestros (frontera, limite, cliente) ------------------------------------------------------------
def _cargar_maestro(cur, resultado: ResultadoCustodiado, archivo_id: int, corrida_id: uuid.UUID) -> Mapping[str, int]:
    """Una fila raw por registro (si la fuente tiene tabla raw) y upsert SCD1 al core por llave natural."""
    if not resultado.registros:
        return resultado.conteos
    destino = fuentes.ficha_por_lotes(resultado.fuente).destino
    if destino.raw is not None:
        _insertar_raw(cur, destino.raw, resultado.registros, archivo_id)
    cols_core = _columnas_lote([r.core for r in resultado.registros])
    extra: tuple[str, ...] = ()
    if destino.con_corrida:
        if "corrida_id" in cols_core:  # la pone almacen, no custodia
            raise ValueError("corrida_id no debe venir del registro custodiado")
        extra = (str(corrida_id),)
    columnas = (*cols_core, "corrida_id") if extra else cols_core
    execute_values(cur, _sql_upsert_core(destino.core, destino.clave, columnas).as_string(cur),
                   [(*(r.core[c] for c in cols_core), *extra) for r in resultado.registros])
    return resultado.conteos


# --- carga de un archivo -----------------------------------------------------------------------------
_CARGADORES = {"maestro": _cargar_maestro, "consumo": _cargar_consumo, "factura": _cargar_factura}


def cargador(carga: str):
    """El cargador del tipo de carga de una Ficha de Fuente. Sin defecto silencioso: un tipo sin cargador falla."""
    if carga not in _CARGADORES:
        raise ValueError(f"carga desconocida: {carga}")
    return _CARGADORES[carga]


def cargar(con, fuente: str, nombre: str, sha256: str, resultado: ResultadoCustodiado,
           corrida_id: uuid.UUID) -> int:
    """Linaje (raw.archivo, raw.archivo_conteo, raw.linea), Cuarentena, raw y core, en la transaccion de
    `con` (el commit es del llamador). Devuelve `archivo_id`. Solo acepta un `ResultadoCustodiado`."""
    if not isinstance(resultado, ResultadoCustodiado) or resultado.fuente != fuente:
        raise TypeError("almacen.cargar solo acepta el ResultadoCustodiado de custodia.custodiar")
    cargar_fuente = cargador(fuentes.ficha_por_lotes(fuente).carga)
    estado = "rechazado" if resultado.archivo_rechazado else "cargado"
    with con.cursor() as cur:
        cur.execute("INSERT INTO raw.archivo (nombre, sha256, fuente, estado, corrida_id) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING archivo_id",
                    (nombre, sha256, fuente, estado, str(corrida_id)))
        archivo_id = cur.fetchone()[0]
        if resultado.lineas:
            execute_values(cur, "INSERT INTO raw.linea (archivo_id, numero_linea, linea) VALUES %s",
                           [(archivo_id, n, t) for n, t in resultado.lineas])
        if resultado.rechazos:
            execute_values(cur, _SQL_CUARENTENA,
                           [(str(corrida_id), fuente, archivo_id, r.linea, r.motivo, r.payload,
                             r.payload_cifrado, r.key_version) for r in resultado.rechazos])
        conteos = cargar_fuente(cur, resultado, archivo_id, corrida_id)
        execute_values(cur, "INSERT INTO raw.archivo_conteo (archivo_id, contador, valor) VALUES %s",
                       [(archivo_id, k, v) for k, v in conteos.items()])
    return archivo_id


def conteos_de_archivo(con, archivo_id: int) -> dict[str, Any]:
    """Contadores finales de un archivo (`raw.archivo_conteo`, lo que `cargar` dejo) y su Cuarentena por motivo
    (solo filas, no el archivo entero). Es la unica fuente de los conteos del Desenlace: facturacion resuelve
    huerfanas y perdedoras en la base, asi que el Resultado en memoria no basta."""
    with con.cursor() as cur:
        cur.execute("SELECT contador, valor FROM raw.archivo_conteo WHERE archivo_id = %s", (archivo_id,))
        conteos = dict(cur.fetchall())
        cur.execute("SELECT motivo, count(*) FROM quarantine.rechazo WHERE archivo_id = %s AND linea IS NOT NULL "
                    "GROUP BY motivo", (archivo_id,))
        return {**conteos, "rechazadas_por_motivo": dict(cur.fetchall())}
