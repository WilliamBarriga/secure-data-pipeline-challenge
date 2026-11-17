"""Borde con I/O hacia la Bitacora (`audit.bitacora_corrida`). Solo escribe via `audit.registrar_evento`.

Sin texto libre: `codigo_error` sale de un catalogo cerrado (el mismo CHECK del DDL). Nunca recibe
valores de fila: solo nombres y conteos.
"""
from __future__ import annotations

import getpass
import hashlib
import os
import socket
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

CODIGOS_ERROR = frozenset({
    "clasificacion_incompleta", "archivo_corrupto", "archivo_modificado", "configuracion_invalida",
    "infraestructura_no_disponible", "error_base_datos", "error_interno", "corrida_concurrente",
    "pii_en_salida"})


class Evento(StrEnum):
    """Catalogo cerrado de eventos de la Bitacora (mismo CHECK que `audit.bitacora_corrida.evento`)."""
    CORRIDA_INICIO = "corrida_inicio"
    ETAPA_FIN = "etapa_fin"
    ARCHIVO_CARGADO = "archivo_cargado"
    ARCHIVO_OMITIDO = "archivo_omitido"
    ARCHIVO_RECHAZADO = "archivo_rechazado"
    CORRIDA_FIN = "corrida_fin"


class Resultado(StrEnum):
    """Resultado de una etapa o de la Corrida (mismo CHECK que `audit.bitacora_corrida.resultado`)."""
    OK = "ok"
    PARCIAL = "parcial"
    FALLO = "fallo"


@dataclass(frozen=True)
class Conteos:
    """Filas de un archivo o de una etapa: leidas, cargadas, a Cuarentena y duplicadas (si aplica)."""
    leidas: int
    cargadas: int
    cuarentena: int
    duplicadas: int | None = None


class Identidad(TypedDict):
    """Quien y que de una Corrida: lo comparten todos sus eventos en la Bitacora."""
    corrida_id: uuid.UUID
    ejecutor: str
    comando: str
    version_codigo: str


def identidad(corrida_id: uuid.UUID, comando: str) -> Identidad:
    return Identidad(corrida_id=corrida_id, ejecutor=ejecutor(), comando=comando, version_codigo=version_codigo())


def _en_catalogo(campo: str, valor: str | None, catalogo) -> None:
    """Falla antes de la base si `valor` no esta en el catalogo; el mensaje nombra el campo, nunca el valor."""
    if valor is not None and valor not in catalogo:
        raise ValueError(f"{campo} fuera del catalogo de la Bitacora")


def registrar_evento(con, ident: Identidad, evento: Evento, *, fuente: str | None = None,
                     archivo_id: int | None = None, conteos: Conteos | None = None,
                     resultado: Resultado | None = None, codigo_error: str | None = None) -> int:
    """Llama a `audit.registrar_evento` en la transaccion de `con` (el commit es del llamador)."""
    _en_catalogo("evento", evento, frozenset(Evento))
    _en_catalogo("resultado", resultado, frozenset(Resultado))
    _en_catalogo("codigo_error", codigo_error, CODIGOS_ERROR)
    filas = (None, None, None, None) if conteos is None else (  # eventos sin conteos: NULL
        conteos.leidas, conteos.cargadas, conteos.cuarentena, conteos.duplicadas)
    with con.cursor() as cur:
        cur.execute("SELECT audit.registrar_evento(%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (str(ident["corrida_id"]), str(evento), ident["ejecutor"], ident["comando"],
                     ident["version_codigo"], fuente, archivo_id,
                     *filas, None if resultado is None else str(resultado), codigo_error))
        return cur.fetchone()[0]


def registrar_etapas_fin(con, ident: Identidad, filas: Iterable[tuple[str, Conteos, Resultado]]) -> None:
    """Un `etapa_fin` por fila (fuente, conteos, resultado), en orden, y commit."""
    for fuente, conteos, resultado in filas:
        registrar_evento(con, ident, Evento.ETAPA_FIN, fuente=fuente, conteos=conteos, resultado=resultado)
    con.commit()


def verificar_cadena(con) -> int | None:
    """evento_id del primer eslabon roto de la cadena de hash, o None si es integra."""
    with con.cursor() as cur:
        cur.execute("SELECT audit.verificar_cadena()")
        return cur.fetchone()[0]


def ejecutor() -> str:
    """usuario OS@host que lanza la Corrida (dato Confidencial: no va a los logs)."""
    try:
        usuario = getpass.getuser()
    except (KeyError, OSError):
        usuario = f"uid{os.getuid()}"
    return f"{usuario}@{socket.gethostname()}"


def version_codigo(raiz: Path | None = None) -> str:
    """CAUCE_VERSION si esta definida; si no, huella corta de los fuentes de `cauce` (no hay git en la imagen)."""
    if os.environ.get("CAUCE_VERSION"):
        return os.environ["CAUCE_VERSION"][:40]
    h = hashlib.sha256()
    raiz = raiz or Path(__file__).resolve().parents[1]  # paquete `cauce` completo, con subcarpetas
    for f in sorted(raiz.rglob("*.py")):
        h.update(f.relative_to(raiz).as_posix().encode() + b"\0" + f.read_bytes())
    return "src-" + h.hexdigest()[:12]
