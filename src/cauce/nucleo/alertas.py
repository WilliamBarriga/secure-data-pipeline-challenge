"""Detector de Alertas del stream (brief §6). Nucleo puro: sin reloj de pared ni I/O.

`procesar(evento)` recibe el evento crudo (bytes del mensaje) y devuelve las Alertas que emite en ese paso;
`cerrar()` juzga la Rejilla completa y devuelve todas las Alertas emitidas con los conteos del stream.

Orden de cada evento: JSON -> `evento_id` (dedupe contra eventos validos) -> validacion (invalidas se descartan
antes de las reglas, se cuentan por motivo y no cuentan como reporte) -> instante igual al maximo de su Frontera
(duplicado) -> reglas. Solo un evento valido reserva su `evento_id`: un invalido con el id de uno real
no lo suprime. El valor de `medidor` no se lee (Minimizacion); sus claves si: una clave fuera del esquema
declarado del evento (`fuentes.CATALOGO["stream"]`) lo invalida.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from cauce.nucleo import fuentes

INICIO_REJILLA = datetime(2026, 6, 1, tzinfo=fuentes.HORA_LOCAL)
INTERVALO = timedelta(minutes=15)
INTERVALOS = 96
RACHA_CAIDA = 3


class TipoAlerta(StrEnum):
    FRONTERA_SIN_REPORTE = "FRONTERA_SIN_REPORTE"
    CONSUMO_FUERA_DE_BANDA = "CONSUMO_FUERA_DE_BANDA"
    LECTURA_RETROACTIVA = "LECTURA_RETROACTIVA"


class MotivoDescarte(StrEnum):
    JSON_INVALIDO = "json_invalido"
    EVENTO_ID_INVALIDO = "evento_id_invalido"
    KWH_NULO = "kwh_nulo"
    KWH_NEGATIVO = "kwh_negativo"
    KWH_NO_NUMERICO = "kwh_no_numerico"
    CLAVE_DESCONOCIDA = "clave_desconocida"
    FRONTERA_DESCONOCIDA = "frontera_desconocida"
    TS_INVALIDO = "ts_invalido"
    TS_FUERA_DE_REJILLA = "ts_fuera_de_rejilla"
    TS_NO_ALINEADO = "ts_no_alineado"


@dataclass(frozen=True, order=True)
class Alerta:
    """Identidad = (tipo, frontera_id, ts); el orden de salida es el mismo. `ts` en Hora local (-05:00)."""
    tipo: TipoAlerta
    frontera_id: str
    ts: datetime


@dataclass(frozen=True)
class Alertas:
    """Cierre del stream: todas las Alertas emitidas (ordenadas, sin repetir) y los conteos para metricas."""
    alertas: tuple[Alerta, ...]
    leidos: int
    duplicados: int
    descartados: Mapping[str, int]

    @property
    def invalidos(self) -> int:
        return sum(self.descartados.values())

    @property
    def validos(self) -> int:
        return self.leidos - self.invalidos - self.duplicados

    def por_tipo(self) -> dict[str, int]:
        return {t.value: sum(a.tipo is t for a in self.alertas) for t in TipoAlerta}

    def resumen(self) -> dict[str, Any]:
        """Forma unica del cierre para el evento `stream_fin` y la subclave `stream` de metrics (solo conteos)."""
        return {"eventos_leidos": self.leidos, "invalidos": self.invalidos,
                "invalidos_por_motivo": dict(self.descartados), "duplicados": self.duplicados,
                "alertas_por_tipo": self.por_tipo()}


def instante(i: int) -> datetime:
    """Instante del intervalo `i` de la Rejilla, en Hora local."""
    return INICIO_REJILLA + i * INTERVALO


def caidas(recibidos: set[int], hasta: int) -> tuple[int, ...]:
    """Intervalo de Alerta (el tercero ausente) de cada Caida en 0..`hasta`, juzgada por conjunto."""
    alertas, racha = [], 0
    for i in range(hasta + 1):
        racha = 0 if i in recibidos else racha + 1
        if racha == RACHA_CAIDA:
            alertas.append(i)
    return tuple(alertas)


_ESQUEMA = fuentes.CATALOGO["stream"]  # esquema declarado de mediciones.raw


class _Descarte(Exception):
    def __init__(self, motivo: MotivoDescarte):
        self.motivo = motivo


@dataclass(frozen=True)
class _Medicion:
    frontera_id: str
    ts: datetime
    intervalo: int
    kwh: Decimal


def _documento(evento: bytes | str) -> dict[str, Any]:
    try:
        doc = json.loads(evento, parse_float=Decimal)
    except (ValueError, RecursionError):  # incluye UnicodeDecodeError
        raise _Descarte(MotivoDescarte.JSON_INVALIDO) from None
    if not isinstance(doc, dict):
        raise _Descarte(MotivoDescarte.JSON_INVALIDO)
    return doc


def _claves(doc: Mapping[str, Any], prefijo: str = "") -> Iterable[str]:
    """Rutas de las hojas del documento (`medidor.serial`), como las claves del catalogo de `fuentes`."""
    for k, v in doc.items():
        ruta = f"{prefijo}{k}"
        if isinstance(v, dict):
            yield from _claves(v, f"{ruta}.")
        else:
            yield ruta


def _kwh(v: Any) -> Decimal:
    if v is None:
        raise _Descarte(MotivoDescarte.KWH_NULO)
    # NaN/Infinity llegan como float (parse_constant por defecto); bool es int en Python
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or not Decimal(v).is_finite():
        raise _Descarte(MotivoDescarte.KWH_NO_NUMERICO)
    if v < 0:
        raise _Descarte(MotivoDescarte.KWH_NEGATIVO)
    return Decimal(v)


def _intervalo(v: Any) -> tuple[datetime, int]:
    try:
        ts = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        raise _Descarte(MotivoDescarte.TS_INVALIDO) from None
    if ts.tzinfo is None:  # sin offset no hay instante
        raise _Descarte(MotivoDescarte.TS_INVALIDO)
    ts = ts.astimezone(fuentes.HORA_LOCAL)
    i, resto = divmod(ts - INICIO_REJILLA, INTERVALO)
    if not 0 <= i < INTERVALOS:
        raise _Descarte(MotivoDescarte.TS_FUERA_DE_REJILLA)
    if resto:
        raise _Descarte(MotivoDescarte.TS_NO_ALINEADO)
    return ts, i


class Detector:
    """Maquina de estados del stream sobre la Rejilla x universo de Fronteras (inyectado, del maestro).

    `max_kwh` es la Banda operativa por Frontera. Sin `margen` (defecto) las Caidas se juzgan solo al cierre, por
    conjunto: el resultado no depende de como se intercalen las particiones del topico. Con `margen` (intervalos),
    una Caida se confirma antes, cuando la marca de agua global (max intervalo valido recibido) supera su tercer
    ausente + `margen`. Una Alerta emitida no se retira, aunque despues llegue el intervalo.
    """

    def __init__(self, fronteras: Iterable[str], max_kwh: Mapping[str, Decimal], margen: int | None = None):
        self._max_kwh = dict(max_kwh)
        self._margen = margen
        self._recibidos: dict[str, set[int]] = {f: set() for f in fronteras}
        self._maximo: dict[str, datetime] = {}  # max ts_medicion procesado por Frontera; la retro no lo mueve
        self._marca_agua = -1
        self._vistos: set[str] = set()  # evento_id de eventos validos (dedupe)
        self._invalidos: set[str] = set()  # evento_id de invalidos ya contados; no bloquean a un valido
        self._emitidas: set[Alerta] = set()
        self._leidos = self._duplicados = 0
        self._descartados: Counter[str] = Counter()

    def procesar(self, evento: bytes | str) -> tuple[Alerta, ...]:
        self._leidos += 1
        evento_id = None
        try:
            doc = _documento(evento)
            evento_id = doc.get("evento_id")
            if not isinstance(evento_id, str) or not evento_id:
                evento_id = None
                raise _Descarte(MotivoDescarte.EVENTO_ID_INVALIDO)
            if evento_id in self._vistos:
                self._duplicados += 1
                return ()
            m = self._medicion(doc)
        except _Descarte as d:
            return self._descartar(d.motivo, evento_id)
        # el id se reserva recien con el evento valido: un invalido hostil no puede reservar el de uno real
        self._vistos.add(evento_id)
        if self._maximo.get(m.frontera_id) == m.ts:  # instante igual al maximo = duplicado
            self._duplicados += 1
            return ()
        return self._emitir(self._reglas(m))

    def _descartar(self, motivo: MotivoDescarte, evento_id: str | None) -> tuple[()]:
        """Un invalido con un `evento_id` ya descartado es duplicado (la alimentacion doble no infla los descartes);
        ese registro no bloquea a un evento valido con el mismo id."""
        if evento_id is not None:
            if evento_id in self._invalidos:
                self._duplicados += 1
                return ()
            self._invalidos.add(evento_id)
        self._descartados[motivo.value] += 1
        return ()

    def cerrar(self) -> Alertas:
        self._emitir(self._caidas(INTERVALOS - 1))
        return Alertas(tuple(sorted(self._emitidas)), self._leidos, self._duplicados, dict(self._descartados))

    def _medicion(self, doc: dict[str, Any]) -> _Medicion:
        if not set(_claves(doc)) <= _ESQUEMA:
            raise _Descarte(MotivoDescarte.CLAVE_DESCONOCIDA)
        f = doc.get("frontera_id")
        if not isinstance(f, str) or f not in self._recibidos:
            raise _Descarte(MotivoDescarte.FRONTERA_DESCONOCIDA)
        ts, i = _intervalo(doc.get("ts_medicion"))
        return _Medicion(f, ts, i, _kwh(doc.get("kwh")))

    def _reglas(self, m: _Medicion) -> list[Alerta]:
        alertas = []
        self._recibidos[m.frontera_id].add(m.intervalo)
        maximo = self._maximo.get(m.frontera_id)
        if maximo is not None and m.ts < maximo:
            alertas.append(Alerta(TipoAlerta.LECTURA_RETROACTIVA, m.frontera_id, m.ts))
        else:
            self._maximo[m.frontera_id] = m.ts
        if m.frontera_id in self._max_kwh and m.kwh > self._max_kwh[m.frontera_id]:
            alertas.append(Alerta(TipoAlerta.CONSUMO_FUERA_DE_BANDA, m.frontera_id, m.ts))
        if self._margen is not None and m.intervalo > self._marca_agua:
            self._marca_agua = m.intervalo
            alertas.extend(self._caidas(self._marca_agua - self._margen - 1))
        return alertas

    def _caidas(self, hasta: int) -> list[Alerta]:
        return [Alerta(TipoAlerta.FRONTERA_SIN_REPORTE, f, instante(i))
                for f, recibidos in self._recibidos.items() for i in caidas(recibidos, hasta)]

    def _emitir(self, candidatas: Iterable[Alerta]) -> tuple[Alerta, ...]:
        nuevas = tuple(sorted(set(candidatas) - self._emitidas))
        self._emitidas.update(nuevas)
        return nuevas
