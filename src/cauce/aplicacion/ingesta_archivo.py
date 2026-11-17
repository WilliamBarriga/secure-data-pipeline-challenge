"""Un archivo, un Desenlace: `ingerir` decide y persiste lo que le pasa a un archivo en una Corrida.

Dueño de la transaccion del archivo: sha256 -> estado (Nuevo | Omitido | Modificado) -> `fuentes.leer` ->
`custodia.custodiar` (la unica puerta a `almacen`) -> `almacen.cargar` -> conteos de la base -> evento en la
Bitacora -> commit.
Devuelve un `Desenlace` y no loguea: `corrida_batch` lo registra con `obs` y suma para `etapa_fin`. Un solo traductor
(`conteos_bitacora`, `evento_log`) arma, desde el mismo Desenlace, el `Conteos` de la Bitacora y los campos del
log, para que no diverjan.

Archivo modificado (mismo nombre, otro sha256) solo existe para las fuentes cuya Ficha lo rechaza (consumos y
facturacion): se rechaza entero y lo ya cargado no cambia. El maestro se reaplica como upsert (el Desenlace es
cargado).
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cauce.bordes import almacen, bitacora
from cauce.bordes.bitacora import Conteos, Evento
from cauce.nucleo import custodia, fuentes
from cauce.nucleo.clasificacion import Clasificacion

SIN_CONTEOS: Mapping[str, Any] = {"leidas": 0, "cargadas": 0, "rechazadas": 0, "rechazadas_por_motivo": {}}


@dataclass(frozen=True)
class ContextoCarga:
    """Lo que comparten todos los archivos de una Corrida batch: conexion, Identidad, Clasificacion y custodio."""
    con: Any
    ident: bitacora.Identidad
    clasif: Clasificacion
    custodio: custodia.Custodio


@dataclass(frozen=True)
class Desenlace:
    """Lo que le pasa a un archivo en una Corrida. 1:1 con `Evento.ARCHIVO_CARGADO | OMITIDO | RECHAZADO`.

    `motivo` solo si es rechazado: `archivo_corrupto:<subcodigo>` | `formato_desconocido` | `archivo_modificado`.
    `conteos`: leidas, cargadas, rechazadas y `rechazadas_por_motivo` (el mismo dict de `raw.archivo_conteo`).
    """
    tipo: Literal["cargado", "omitido", "rechazado"]
    archivo_id: int | None
    conteos: Mapping[str, Any] = field(default_factory=lambda: dict(SIN_CONTEOS))
    motivo: str | None = None

    @property
    def evento(self) -> Evento:
        """El evento de la Bitacora (y el nombre del evento de log): `archivo_<tipo>`."""
        return Evento(f"archivo_{self.tipo}")


def conteos_bitacora(d: Desenlace) -> Conteos:
    """El agregado que va a la Bitacora (leidas, cargadas, Cuarentena)."""
    c = d.conteos
    return Conteos(c["leidas"], c["cargadas"], c["rechazadas"])


def evento_log(d: Desenlace, fuente: str, nombre: str) -> tuple[str, dict[str, Any]]:
    """(nombre del evento de log, campos): nombres y conteos, nunca valores de fila. El log toma el desglose."""
    campos: dict[str, Any] = {"fuente": fuente, "archivo": nombre}
    if d.tipo == "rechazado":
        campos["motivo"] = d.motivo
    elif d.tipo == "cargado":
        campos.update({k: d.conteos[k] for k in ("leidas", "cargadas", "rechazadas_por_motivo")})
    return d.evento.value, campos


def ingerir(ctx: ContextoCarga, fuente: str, nombre: str, ruta: Path) -> Desenlace:
    """Un archivo, una transaccion. Ver el docstring del modulo."""
    con, ident = ctx.con, ctx.ident
    datos = ruta.read_bytes()
    sha = hashlib.sha256(datos).hexdigest()
    estado = almacen.registrar(con, fuente, nombre, sha)
    if estado is almacen.Estado.OMITIDO:
        desenlace = Desenlace("omitido", almacen.archivo_id_por_sha(con, fuente, nombre, sha))
        bitacora.registrar_evento(con, ident, desenlace.evento, fuente=fuente, archivo_id=desenlace.archivo_id)
        con.commit()
        return desenlace
    if estado is almacen.Estado.MODIFICADO and fuentes.ficha_por_lotes(fuente).rechaza_modificado:
        leido = fuentes.rechazar_archivo(fuentes.MotivoRechazoArchivo.ARCHIVO_MODIFICADO)
    else:  # Nuevo, o maestro Modificado: se reaplica como upsert SCD1
        leido = fuentes.leer(fuente, nombre, datos)
    resultado = custodia.custodiar(leido, ctx.clasif, ctx.custodio, fuente=fuente)
    archivo_id = almacen.cargar(con, fuente, nombre, sha, resultado, ident["corrida_id"])
    rechazo = resultado.archivo_rechazado
    if rechazo:
        desenlace = Desenlace("rechazado", archivo_id, dict(SIN_CONTEOS), rechazo.motivo_desenlace)
    else:  # los conteos finales salen de la base (facturacion resuelve huerfanas y perdedoras alli)
        desenlace = Desenlace("cargado", archivo_id, almacen.conteos_de_archivo(con, archivo_id))
    bitacora.registrar_evento(con, ident, desenlace.evento, fuente=fuente, archivo_id=archivo_id,
                              conteos=conteos_bitacora(desenlace),
                              codigo_error=rechazo.codigo_error if rechazo else None)
    con.commit()
    return desenlace
