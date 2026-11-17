"""Catalogo cerrado de formatos de las 6 fuentes y su lectura: la unica verdad de headers.

Cada fuente tiene una Ficha de Fuente (`FICHAS`): sus campos, como se lee, su destino en la base, su tipo de
carga, su origen en data/ y si rechaza el Archivo modificado. Los demas modulos consultan la ficha y no ramifican
por nombre de fuente; una fuente nueva es una entrada nueva en `FICHAS`.

Un header fuera de este catalogo es `formato_desconocido` (rechazo entero); la clasificacion
(`clasificacion.verificar`) valida `CATALOGO` (derivado de las fichas), nunca lee headers de data/. Claves de
factura/stream: rutas del JSON (`cliente.nit`).

`leer` es puro: bytes -> Resultado, con `csv` y `json` de la stdlib. Un archivo ilegible (Archivo corrupto) se
rechaza entero (`Resultado.archivo_rechazado`); una fila invalida va sola a Cuarentena con un motivo del
catalogo. Nunca lanza por contenido: la entrada hostil es dato. `leer` no sabe que es PII (eso lo decide
`custodia` desde la Clasificacion): devuelve el crudo de cada fila rechazada (`Rechazo.payload`), sus campos
parseados (`Rechazo.campos`) y `lineas`. `custodia.custodiar` cifra o descarta lo que corresponda; el Resultado
de `leer` nunca debe persistirse ni loguearse directo.

Orden del modulo: catalogo y tipos de la ficha -> tipos del Resultado -> validaciones comunes -> filas CSV por
fuente -> lectura CSV -> lectura JSON (factura) -> `FICHAS` -> `leer`.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

# --- catalogo cerrado y tipos de la ficha --------------------------------------------------------------
# Columnas de los maestros, en el orden del archivo (y de los campos que ve `custodia`).
_COLUMNAS_FRONTERA = ("frontera_id", "codigo_sic", "cliente_id", "mercado", "operador_red", "nivel_tension",
                      "fecha_registro", "estado")
_COLUMNAS_LIMITE = ("frontera_id", "min_kwh", "max_kwh")
_COLUMNAS_CLIENTE = ("cliente_id", "razon_social", "nit", "email_contacto", "celular_contacto", "direccion",
                     "ciudad", "departamento", "representante_legal", "observaciones")


@dataclass(frozen=True)
class FormatoConsumo:
    """Una entrada del catalogo cerrado de formatos de consumo: el encabezado exacto y como leerlo. El separador
    sale del encabezado (cada entrada tiene el suyo); un formato nuevo es una entrada nueva aca, nunca
    inferencia."""
    encabezado: tuple[str, ...]
    separador: str = ","
    decimal: str = "."
    utc: bool = False  # timestamp ISO en UTC con `Z`; si no, hora local naive (America/Bogota)
    columna_ts: str = "timestamp"
    columna_valor: str = "consumo_kwh"
    columna_unidad: str | None = None  # si existe, la unidad del valor (kWh | MWh)


FORMATOS_CONSUMO: tuple[FormatoConsumo, ...] = (
    # 2026-01, 2026-04 y la correccion de abril
    FormatoConsumo(("frontera_id", "timestamp", "consumo_kwh", "version_lectura", "calidad_lectura")),
    FormatoConsumo(("frontera_id", "timestamp_utc", "consumo_kwh", "version_lectura", "calidad_lectura"),
                   separador=";", decimal=",", utc=True, columna_ts="timestamp_utc"),  # 2026-02
    FormatoConsumo(("frontera_id", "timestamp", "kwh", "unidad", "version_lectura", "calidad_lectura"),
                   columna_valor="kwh", columna_unidad="unidad"),  # 2026-03
)

_CAMPOS_FACTURA = ("periodo", "emisor.nombre", "emisor.nit", "generado_en", "factura_id", "frontera_id",
                   "cliente.nit", "cliente.razon_social", "cliente.email_facturacion", "consumo_facturado_kwh",
                   "tarifa_cop_kwh", "valor_total_cop", "componentes[].concepto", "componentes[].valor_cop_kwh",
                   "metodo_pago.tipo", "metodo_pago.cuenta_enmascarada")
_CAMPOS_STREAM = ("evento_id", "frontera_id", "ts_medicion", "kwh", "medidor.serial", "medidor.firmware")


@dataclass(frozen=True)
class Destino:
    """Donde aterriza una fuente en la base: tabla raw (None si no tiene), tabla core, columna de su llave natural
    y si la fila core lleva `corrida_id` (lo agrega `almacen.cargar`, no custodia)."""
    raw: str | None
    core: str
    clave: str
    con_corrida: bool = False

    @property
    def raw_requerida(self) -> str:
        """La tabla raw de un destino que la tiene (consumo, factura, cliente). Falla fuerte si no tiene."""
        if self.raw is None:
            raise ValueError(f"{self.core}: el destino no tiene tabla raw")
        return self.raw


@dataclass(frozen=True)
class Origen:
    """Donde estan en data/ los archivos de una fuente: nombres fijos (maestros) o todo archivo regular
    `*<extension>` del subdirectorio, en orden lexicografico (consumos y facturacion: sin lista de meses)."""
    subdir: str
    nombres: tuple[str, ...] = ()
    extension: str | None = None

    def directorio(self, dir_datos: Path) -> Path:
        return dir_datos / self.subdir

    def rutas(self, dir_datos: Path) -> tuple[Path, ...]:
        """Las rutas de los nombres fijos bajo `dir_datos`, sin mirar si existen (eso es del borde). Un origen
        por extension no tiene nombres fijos: lo lista el borde."""
        return tuple(self.directorio(dir_datos) / n for n in self.nombres)


# Como se carga una fuente (`almacen` tiene un cargador por tipo): maestro = upsert SCD1 por llave natural;
# consumo = candidatos a raw + merge B3; factura = raw + huerfanas y duplicadas resueltas en la base.
Carga = Literal["maestro", "consumo", "factura"]


@dataclass(frozen=True)
class Ficha:
    """Ficha de Fuente: todo lo que define una fuente, como datos. Una `Ficha` sola declara solo su catalogo
    (`campos`, rutas del JSON): la fuente no tiene lectura por lotes (`leer` lanza NotImplementedError) ni
    destino, carga u origen (stream). Las que se cargan por lotes son `FichaPorLotes`."""
    campos: tuple[str, ...] = ()

    @property
    def catalogo(self) -> frozenset[str]:
        return frozenset(self.campos)


@dataclass(frozen=True, kw_only=True)
class FichaPorLotes(Ficha):
    """Ficha de una fuente que se lee y carga por lotes desde data/: todo obligatorio salvo lo que tiene defecto.
    Las CSV toman su catalogo de los encabezados de sus `perfiles` (candidatos, en orden)."""
    formato: Literal["csv", "json"]
    destino: Destino
    carga: Carga
    origen: Origen
    codificaciones: tuple[str, ...] = ("utf-8-sig", "cp1252")  # en orden; la primera que decodifica gana
    perfiles: tuple[_PerfilCsv, ...] = ()
    rechaza_modificado: bool = False  # Archivo modificado: solo consumos y facturacion; el maestro se reaplica

    @property
    def catalogo(self) -> frozenset[str]:
        return frozenset(self.campos).union(*(p.encabezado for p in self.perfiles))

    @property
    def columnas(self) -> tuple[str, ...]:
        """Encabezado canonico de una fuente CSV (el de su primer perfil): los campos que ve `custodia`."""
        return self.perfiles[0].encabezado

UNIDADES_KWH = {"kWh": Decimal(1), "MWh": Decimal(1000)}
HORA_LOCAL = timezone(timedelta(hours=-5))  # America/Bogota: UTC-5 fijo, sin horario de verano
CONSUMO_MAXIMO_KWH = Decimal(1_000_000)
CONCEPTOS_TARIFA = frozenset("GTDCPR")

_FRONTERA_ID = re.compile(r"CO\d{10}")
_CODIGO_SIC = re.compile(r"frt\d{5}")
_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIODO = re.compile(r"\d{4}-\d{2}")
_TS_LOCAL = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_TS_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_DECIMAL = {".": re.compile(r"\d+(?:\.\d+)?"), ",": re.compile(r"\d+(?:,\d+)?")}
_LINEAS = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
_NULOS = frozenset({"", "null", "nan"})


# --- Resultado -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Registro:
    linea: int  # linea fisica donde empieza el registro (1 = encabezado)
    campos: Mapping[str, Any] = field(repr=False)  # puede traer PII: fuera de repr/tracebacks


@dataclass(frozen=True)
class Rechazo:
    linea: int | None  # None: rechazo del archivo entero
    motivo: str
    payload: str = field(repr=False)  # texto crudo de la fila; vacio si se rechaza el archivo
    # campos parseados de la fila; None si se rechaza el archivo
    campos: Mapping[str, Any] | None = field(default=None, repr=False)


class MotivoRechazoArchivo(StrEnum):
    """Por que se rechaza un archivo entero; cada motivo nace con su categoria (GLOSSARY "Desenlace"): Archivo
    corrupto (estructura ilegible), formato desconocido o Archivo modificado."""
    NO_DECODIFICA = "no_decodifica"
    CARACTER_NULO = "caracter_nulo"
    FORMATO_DESCONOCIDO = "formato_desconocido"
    COMILLA_SIN_CERRAR = "comilla_sin_cerrar"
    NUMERO_CAMPOS = "numero_campos"
    SIN_SALTO_FINAL = "sin_salto_final"
    ARCHIVO_MODIFICADO = "archivo_modificado"

    @property
    def codigo_error(self) -> str:
        """El `codigo_error` de la Bitacora (`bitacora.CODIGOS_ERROR`)."""
        return "archivo_modificado" if self is MotivoRechazoArchivo.ARCHIVO_MODIFICADO else "archivo_corrupto"

    @property
    def motivo_desenlace(self) -> str:
        """El motivo del Desenlace rechazado y de su log: `archivo_corrupto:<motivo>` si es Archivo corrupto."""
        if self in (MotivoRechazoArchivo.FORMATO_DESCONOCIDO, MotivoRechazoArchivo.ARCHIVO_MODIFICADO):
            return self.value
        return f"archivo_corrupto:{self.value}"


@dataclass(frozen=True)
class Resultado:
    registros: tuple[Registro, ...]
    rechazos: tuple[Rechazo, ...]
    formato: str  # "csv" | "json"; "desconocido" si el archivo se rechazo antes de identificarlo
    conteos: Mapping[str, int]  # leidas = cargadas + rechazadas (0 si se rechazo el archivo)
    # (numero_linea, texto crudo) de cada registro, para raw.linea (custodia lo vacia si hay PII)
    lineas: tuple[tuple[int, str], ...] = field(repr=False)
    archivo_rechazado: MotivoRechazoArchivo | None = None  # si el archivo se rechaza entero


# Contadores B de un archivo de consumo (claves de `Resultado.conteos` y de `raw.archivo_conteo`): `fotos` los suma
# por estos nombres para dq_report.json. Una fila MWh cuenta en `mwh_convertidas` aunque luego se rechace.
CONTADOR_NULOS, CONTADOR_NEGATIVOS, CONTADOR_OUTLIER = "nulos", "negativos", "outlier"
CONTADOR_NO_NUMERICO, CONTADOR_MWH_CONVERTIDAS = "no_numerico", "mwh_convertidas"
CONTADORES_B = (CONTADOR_NULOS, CONTADOR_NEGATIVOS, CONTADOR_OUTLIER, CONTADOR_NO_NUMERICO, CONTADOR_MWH_CONVERTIDAS)


def rechazar_archivo(motivo: MotivoRechazoArchivo, formato: str = "desconocido") -> Resultado:
    """Resultado de un archivo rechazado entero (Archivo corrupto, formato desconocido o Archivo modificado)."""
    return Resultado((), (Rechazo(None, motivo.value, ""),), formato, {"leidas": 0, "cargadas": 0, "rechazadas": 0},
                     (), motivo)


def _resultado(formato: str, registros: list[Registro], rechazos: list[Rechazo],
               lineas: tuple[tuple[int, str], ...] = (), **contadores: int) -> Resultado:
    n = len(registros) + len(rechazos)
    return Resultado(tuple(registros), tuple(rechazos), formato,
                     {"leidas": n, "cargadas": len(registros), "rechazadas": len(rechazos), **contadores}, lineas)


# --- validaciones comunes --------------------------------------------------------------------------------
class _Invalida(Exception):
    def __init__(self, motivo: str, convertida: bool = False):
        self.motivo = motivo
        self.convertida = convertida  # la unidad ya se llevo a kWh (MWh) antes de rechazar la fila


def _no_vacio(v: str) -> str:
    if not v.strip():
        raise _Invalida("campo_obligatorio_vacio")
    return v


def _frontera_id(v: str) -> str:
    if not _FRONTERA_ID.fullmatch(v):
        raise _Invalida("frontera_id_invalido")
    return v


def _decimal(texto: str, separador: str = ".") -> Decimal:
    """Decimal con signo opcional y el separador decimal del formato; sin exponentes, espacios ni `nan`."""
    sin_signo = texto[1:] if texto.startswith("-") else texto
    if not _DECIMAL[separador].fullmatch(sin_signo):
        raise _Invalida("valor_no_numerico")
    return Decimal(texto.replace(separador, "."))


# --- filas CSV por fuente: dict de texto -> campos tipados, o _Invalida con el motivo ----------------------
def _frontera(c: dict[str, str]) -> dict[str, Any]:
    _frontera_id(c["frontera_id"])
    if not _CODIGO_SIC.fullmatch(c["codigo_sic"]):
        raise _Invalida("codigo_sic_invalido")
    for k in ("cliente_id", "mercado", "operador_red", "estado"):
        _no_vacio(c[k])
    if not re.fullmatch(r"\d{1,2}", c["nivel_tension"]):
        raise _Invalida("nivel_tension_invalido")
    if not _FECHA.fullmatch(c["fecha_registro"]):
        raise _Invalida("fecha_invalida")
    try:
        fecha = date.fromisoformat(c["fecha_registro"])
    except ValueError:
        raise _Invalida("fecha_invalida") from None
    return {**c, "nivel_tension": int(c["nivel_tension"]), "fecha_registro": fecha}


def _limite(c: dict[str, str]) -> dict[str, Any]:
    _frontera_id(c["frontera_id"])
    valores = {k: _decimal(c[k].strip()) for k in ("min_kwh", "max_kwh")}
    if valores["min_kwh"] < 0 or valores["min_kwh"] > valores["max_kwh"]:
        raise _Invalida("banda_invalida")
    return {"frontera_id": c["frontera_id"], **valores}


def _cliente(c: dict[str, str]) -> dict[str, Any]:
    for k in ("cliente_id", "razon_social", "nit"):
        _no_vacio(c[k])
    if not 9 <= len(re.sub(r"\D", "", c["nit"])) <= 10:
        raise _Invalida("nit_invalido")
    return dict(c)


def _hora(texto: str, f: FormatoConsumo) -> datetime:
    """Hora de la Lectura en hora local de Bogota, en punto."""
    patron, plantilla = (_TS_UTC, "%Y-%m-%dT%H:%M:%SZ") if f.utc else (_TS_LOCAL, "%Y-%m-%d %H:%M:%S")
    if not patron.fullmatch(texto):
        raise _Invalida("timestamp_invalido")
    try:
        ts = datetime.strptime(texto, plantilla)
    except ValueError:
        raise _Invalida("timestamp_invalido") from None
    ts = ts.replace(tzinfo=UTC).astimezone(HORA_LOCAL) if f.utc else ts.replace(tzinfo=HORA_LOCAL)  # naive = Bogota
    if ts.minute or ts.second:
        raise _Invalida("timestamp_fuera_de_hora")
    return ts


def _consumo(c: dict[str, str], f: FormatoConsumo = FORMATOS_CONSUMO[0]) -> dict[str, Any]:
    """Una Lectura: parseo -> unidad a kWh -> B4 (nulo, negativo, outlier en kWh). B3 (duplicados) no es de esta
    funcion."""
    _frontera_id(c["frontera_id"])
    ts = _hora(c[f.columna_ts].strip(), f)
    if not re.fullmatch(r"\d{1,9}", c["version_lectura"].strip()):
        raise _Invalida("version_invalida")
    texto = c[f.columna_valor].strip()
    if texto.lower() in _NULOS:
        raise _Invalida("consumo_nulo")
    valor = _decimal(texto, f.decimal)
    convertida = False
    if f.columna_unidad:
        factor = UNIDADES_KWH.get(c[f.columna_unidad].strip())
        if factor is None:
            raise _Invalida("unidad_desconocida")
        convertida = factor != 1
        valor *= factor  # antes de B4: el outlier se mide en kWh
    if valor < 0:
        raise _Invalida("consumo_negativo", convertida)
    if valor > CONSUMO_MAXIMO_KWH:
        raise _Invalida("consumo_outlier", convertida)
    return {"frontera_id": c["frontera_id"], "ts_hora": ts, "consumo_kwh": valor,
            "version_lectura": int(c["version_lectura"]), "calidad": c["calidad_lectura"],
            "mwh_convertida": convertida}


# --- lectura CSV -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _PerfilCsv:
    """Un formato CSV del catalogo y como se lee: encabezado, separador, validacion de fila, llaves unicas dentro
    del archivo (llave, motivo de rechazo), que campos ven el registro y el rechazo, y si cuenta los contadores
    B. Se identifica por la primera linea exacta; con `orden_libre` (maestros) el encabezado puede venir en
    cualquier orden, sin repetidos."""
    encabezado: tuple[str, ...]
    separador: str
    validar: Callable[[dict[str, str]], dict[str, Any]]
    unicos: tuple[tuple[str, str], ...]
    proyectar: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    contadores_b: bool = False
    orden_libre: bool = False


def _solo(columnas: tuple[str, ...]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    return lambda campos: {k: campos[k] for k in columnas}


def _sin_cambios(campos: Mapping[str, Any]) -> Mapping[str, Any]:
    return campos


def _perfil_maestro(columnas: tuple[str, ...], validar: Callable[[dict[str, str]], dict[str, Any]],
                    *unicos: tuple[str, str]) -> _PerfilCsv:
    return _PerfilCsv(columnas, ",", validar, unicos, _solo(columnas), orden_libre=True)

# motivo de Cuarentena -> contador B del archivo (cubos disjuntos; el resto de motivos no suma a B)
_CONTADOR_B = {"consumo_nulo": CONTADOR_NULOS, "consumo_negativo": CONTADOR_NEGATIVOS,
               "consumo_outlier": CONTADOR_OUTLIER, "valor_no_numerico": CONTADOR_NO_NUMERICO}


def _perfil_consumo(f: FormatoConsumo) -> _PerfilCsv:
    # los duplicados de consumo son candidatos de B3, no rechazos
    return _PerfilCsv(f.encabezado, f.separador, lambda c: _consumo(c, f), (), _sin_cambios, contadores_b=True)


def _decodificar(datos: bytes, codificaciones: tuple[str, ...]) -> str | None:
    for codificacion in codificaciones:
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return None


def _primera_linea_es(primera: str, perfil: _PerfilCsv) -> bool:
    try:
        return next(csv.reader([primera], delimiter=perfil.separador, strict=True)) == list(perfil.encabezado)
    except (csv.Error, StopIteration):
        return False


def _perfil(perfiles: tuple[_PerfilCsv, ...], texto: str) -> _PerfilCsv | None:
    """El primer perfil candidato que coincide con la primera linea; uno de `orden_libre` se toma y su encabezado
    se verifica al leerlo. None si ninguna entrada del catalogo coincide."""
    primera = texto.partition("\n")[0].rstrip("\r")
    return next((p for p in perfiles if p.orden_libre or _primera_linea_es(primera, p)), None)


def _leer_csv(ficha: FichaPorLotes, texto: str) -> Resultado:
    if "\x00" in texto:
        return rechazar_archivo(MotivoRechazoArchivo.CARACTER_NULO)
    if not texto:
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    csv.field_size_limit(16 * 1024 * 1024)  # cadenas larguisimas son datos (B7); el defecto es 128 KiB
    perfil = _perfil(ficha.perfiles, texto)
    if perfil is None:
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    reader = csv.reader(io.StringIO(texto, newline=""), delimiter=perfil.separador, strict=True)
    try:
        encabezado = next(reader)
    except csv.Error:
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    if perfil.orden_libre and (frozenset(encabezado) != frozenset(perfil.encabezado)
                               or len(encabezado) != len(set(encabezado))):
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    fisicas = _LINEAS.findall(texto)  # mismo corte que csv.line_num (no splitlines: parte en \x0b, ...)
    registros: list[Registro] = []
    rechazos: list[Rechazo] = []
    lineas: list[tuple[int, str]] = []
    vistos: dict[str, set[str]] = {k: set() for k, _ in perfil.unicos}
    contadores_b: Counter[str] = Counter()
    previa = reader.line_num
    while True:
        try:
            fila = next(reader)
        except StopIteration:
            break
        except csv.Error:
            return rechazar_archivo(MotivoRechazoArchivo.COMILLA_SIN_CERRAR, "csv")
        inicio, previa = previa + 1, reader.line_num
        if len(fila) != len(encabezado):
            return rechazar_archivo(MotivoRechazoArchivo.NUMERO_CAMPOS, "csv")
        crudo = "".join(fisicas[inicio - 1:previa]).rstrip("\r\n")
        lineas.append((inicio, crudo))
        campos = dict(zip(encabezado, fila, strict=True))
        try:
            valido = perfil.validar(campos)
            for clave, motivo in perfil.unicos:
                if valido[clave] in vistos[clave]:
                    raise _Invalida(motivo)
        except _Invalida as e:
            rechazos.append(Rechazo(inicio, e.motivo, crudo, perfil.proyectar(campos)))
            if perfil.contadores_b:
                convertida = e.convertida and CONTADOR_MWH_CONVERTIDAS
                contadores_b.update(n for n in (_CONTADOR_B.get(e.motivo), convertida) if n)
            continue
        for clave in vistos:
            vistos[clave].add(valido[clave])
        if perfil.contadores_b and valido["mwh_convertida"]:
            contadores_b[CONTADOR_MWH_CONVERTIDAS] += 1
        registros.append(Registro(inicio, perfil.proyectar(valido)))
    if not texto.endswith(("\n", "\r")):
        return rechazar_archivo(MotivoRechazoArchivo.SIN_SALTO_FINAL, "csv")
    return _resultado("csv", registros, rechazos, tuple(lineas), **contadores_b)


# --- lectura JSON (factura) ------------------------------------------------------------------------------
_RAIZ_FACTURA = frozenset({"periodo", "emisor", "generado_en", "facturas"})
_CLAVES_FACTURA = frozenset({"factura_id", "frontera_id", "cliente", "consumo_facturado_kwh", "tarifa_cop_kwh",
                             "valor_total_cop", "componentes", "metodo_pago"})


def _hijos(fuente_catalogo: str, prefijo: str) -> frozenset[str]:
    """Claves permitidas bajo `prefijo` (`cliente`) segun el catalogo cerrado."""
    return frozenset(c[len(prefijo) + 1:] for c in CATALOGO[fuente_catalogo] if c.startswith(prefijo + "."))


def _numero(v: Any) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise _Invalida("valor_no_numerico")
    return Decimal(v)


def _objeto(v: Any, prefijo: str) -> dict[str, str]:
    """Subobjeto del catalogo (`cliente`, `metodo_pago`): claves del catalogo y valores texto."""
    if not isinstance(v, dict) or not set(v) <= _hijos("factura", prefijo) or not all(
            isinstance(x, str) for x in v.values()):
        raise _Invalida("estructura_invalida")
    return dict(v)


def _componentes(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        raise _Invalida("estructura_invalida")
    componentes = []
    for c in v:
        if not isinstance(c, dict) or set(c) != {"concepto", "valor_cop_kwh"} or not isinstance(c["concepto"], str):
            raise _Invalida("estructura_invalida")
        if c["concepto"] not in CONCEPTOS_TARIFA:
            raise _Invalida("concepto_desconocido")
        componentes.append({"concepto": c["concepto"], "valor_cop_kwh": _numero(c["valor_cop_kwh"])})
    if len({c["concepto"] for c in componentes}) != len(componentes):
        raise _Invalida("estructura_invalida")
    return componentes


def _factura(x: Any, periodo: date, generado_en: datetime, emisor: dict[str, str]) -> dict[str, Any]:
    if not isinstance(x, dict) or set(x) != _CLAVES_FACTURA:
        raise _Invalida("estructura_invalida")
    if not isinstance(x["factura_id"], str) or not isinstance(x["frontera_id"], str):
        raise _Invalida("estructura_invalida")
    _no_vacio(x["factura_id"])
    _frontera_id(x["frontera_id"])
    cliente, pago = _objeto(x["cliente"], "cliente"), _objeto(x["metodo_pago"], "metodo_pago")
    cifras = {k: _numero(x[k]) for k in ("consumo_facturado_kwh", "tarifa_cop_kwh", "valor_total_cop")}
    return {"factura_id": x["factura_id"], "frontera_id": x["frontera_id"], "periodo": periodo,
            "generado_en": generado_en, "emisor": emisor, "cliente": cliente, **cifras,
            "componentes": _componentes(x["componentes"]), "metodo_pago": pago}


def _rechazar_constante(_: str) -> Any:
    raise ValueError("constante no JSON")


def _cabecera_factura(doc: Any) -> tuple[date, datetime, dict[str, str]] | None:
    """(periodo, generado_en, emisor) del archivo; None si la raiz no es la del catalogo."""
    if not isinstance(doc, dict) or set(doc) != _RAIZ_FACTURA or not isinstance(doc["facturas"], list):
        return None
    if not isinstance(doc["periodo"], str) or not _PERIODO.fullmatch(doc["periodo"]):
        return None
    periodo = date.fromisoformat(doc["periodo"] + "-01")
    generado_en = datetime.fromisoformat(doc["generado_en"])
    emisor = doc["emisor"]
    if generado_en.tzinfo is None or not isinstance(emisor, dict) or not all(
            isinstance(v, str) for v in emisor.values()):
        return None
    return periodo, generado_en, emisor


def _leer_factura(_: FichaPorLotes, texto: str) -> Resultado:
    """`facturacion_*.json`: un objeto con `periodo` y `generado_en` a nivel de archivo y `facturas`. Una
    factura invalida va sola a Cuarentena; si la raiz no es la del catalogo, el archivo se rechaza entero.
    `factura_id` repetido en el archivo: gana la ultima posicion (todas comparten `generado_en`). Hoy es la unica
    fuente JSON por lotes."""
    try:
        doc = json.loads(texto, parse_float=Decimal, parse_constant=_rechazar_constante)
        cabecera = _cabecera_factura(doc)
    except (ValueError, TypeError, RecursionError):
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    if cabecera is None:
        return rechazar_archivo(MotivoRechazoArchivo.FORMATO_DESCONOCIDO)
    periodo, generado_en, emisor = cabecera
    validas: list[tuple[int, dict[str, Any]]] = []
    rechazos: list[Rechazo] = []
    for pos, x in enumerate(doc["facturas"], start=1):
        try:
            validas.append((pos, _factura(x, periodo, generado_en, dict(emisor))))
        except _Invalida as e:
            rechazos.append(Rechazo(pos, e.motivo, "", None))  # fila malformada: nunca se interpreta como campos
    ultima = {c["factura_id"]: pos for pos, c in validas}
    registros = []
    for pos, c in validas:
        if ultima[c["factura_id"]] == pos:
            registros.append(Registro(pos, c))
        else:
            rechazos.append(Rechazo(pos, "factura_duplicada", "", c))
    rechazos.sort(key=lambda r: r.linea or 0)
    return _resultado("json", registros, rechazos)


# --- Fichas de Fuente ------------------------------------------------------------------------------------
FICHAS: Mapping[str, Ficha] = {
    "frontera": FichaPorLotes(
        formato="csv",
        perfiles=(_perfil_maestro(_COLUMNAS_FRONTERA, _frontera, ("frontera_id", "duplicado_en_archivo"),
                                  ("codigo_sic", "codigo_sic_duplicado")),),
        destino=Destino(None, "core.dim_frontera", "frontera_id"), carga="maestro",
        origen=Origen("maestros", ("fronteras.csv",))),
    "limite": FichaPorLotes(
        formato="csv",
        perfiles=(_perfil_maestro(_COLUMNAS_LIMITE, _limite, ("frontera_id", "duplicado_en_archivo")),),
        destino=Destino(None, "core.dim_frontera", "frontera_id"), carga="maestro",
        origen=Origen("maestros", ("limites_frontera.csv",))),
    "cliente": FichaPorLotes(
        # clientes.csv es ISO-8859-1: nunca falla al decodificar (si fuera UTF-8 valido, se respeta)
        formato="csv", codificaciones=("utf-8-sig", "iso-8859-1"),
        perfiles=(_perfil_maestro(_COLUMNAS_CLIENTE, _cliente, ("cliente_id", "duplicado_en_archivo")),),
        destino=Destino("raw.cliente", "core.dim_cliente", "cliente_id", con_corrida=True), carga="maestro",
        origen=Origen("maestros", ("clientes.csv",))),
    "consumo": FichaPorLotes(
        # sin PII: custodiar solo la pasa; almacen guarda cada candidata en raw y resuelve B3 hacia core
        formato="csv", perfiles=tuple(_perfil_consumo(f) for f in FORMATOS_CONSUMO),
        destino=Destino("raw.lectura_consumo", "core.fact_consumo_horario", "frontera_id", con_corrida=True),
        carga="consumo", origen=Origen("consumos", extension=".csv"), rechaza_modificado=True),
    "factura": FichaPorLotes(
        # JSON anidado con PII: `almacen` resuelve Frontera huerfana, duplicadas y cliente_id (custodia no ve la base)
        campos=_CAMPOS_FACTURA, formato="json", codificaciones=("utf-8-sig",),
        destino=Destino("raw.factura", "core.fact_factura", "factura_id"), carga="factura",
        origen=Origen("facturacion", extension=".json"), rechaza_modificado=True),
    "stream": Ficha(campos=_CAMPOS_STREAM),  # sin lectura por lotes: solo declara su catalogo
}

# El ndjson que publica `make producir` (tools/producer.py). No va en la Ficha `stream`: sin formato no tiene origen.
ORIGEN_STREAM = Origen("eventos", ("mediciones_stream.ndjson",))

CATALOGO: Mapping[str, frozenset[str]] = {fuente: ficha.catalogo for fuente, ficha in FICHAS.items()}

_LECTORES: Mapping[str, Callable[[FichaPorLotes, str], Resultado]] = {"csv": _leer_csv, "json": _leer_factura}


def ficha_por_lotes(fuente: str) -> FichaPorLotes:
    """La Ficha de una fuente que se carga por lotes. ValueError si la fuente no existe; NotImplementedError si
    solo declara catalogo (aun sin lectura por lotes)."""
    ficha = FICHAS.get(fuente)
    if ficha is None:
        raise ValueError(f"fuente desconocida: {fuente}")
    if not isinstance(ficha, FichaPorLotes):
        raise NotImplementedError(f"{fuente}: sin lectura por lotes aun")
    return ficha


# --- entrada -----------------------------------------------------------------------------------------------
def leer(fuente: str, nombre: str, datos: bytes) -> Resultado:
    """Parsea `datos` (el archivo `nombre` de `fuente`) segun su Ficha. Puro: sin I/O y sin lanzar por
    contenido."""
    ficha = ficha_por_lotes(fuente)
    texto = _decodificar(datos, ficha.codificaciones)
    if texto is None:
        return rechazar_archivo(MotivoRechazoArchivo.NO_DECODIFICA)
    return _LECTORES[ficha.formato](ficha, texto)
