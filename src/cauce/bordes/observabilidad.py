"""Logs JSON sin PII + metricas de la Corrida, acumuladas desde los mismos eventos.

Tres capas: (1) el logger nunca recibe valores de fila; (2) filtro por clave (catalogo cerrado +
clasificacion); (3) ultimo recurso `pii.detectar_estricto` sobre la linea serializada.
Escribir `out/metrics.json` es de `salidas`: `cerrar` no escribe.
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from cauce.nucleo import pii

TZ = timezone(timedelta(hours=-5))
NIVELES = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
MODOS = ("run", "stream", "test", "bootstrap")
ETAPAS = ("clasificacion", "fronteras", "clientes", "consumos", "facturacion", "salidas")
REDACTADO = "[REDACTADO]"
_BASE = {"ts", "nivel", "evento", "corrida_id", "modo"}
_NIVELES_VISIBLES = {"Publico", "Público", "Interno"}
# subclave `stream` de metrics sin stream (Corrida batch): sus claves son las unicas permitidas en `stream_fin`
STREAM_VACIO: dict[str, Any] = {"eventos_leidos": 0, "invalidos": 0, "invalidos_por_motivo": {}, "duplicados": 0,
                                "alertas_por_tipo": {}, "duracion_s": 0.0}

# evento -> (nivel, claves permitidas). Catalogo cerrado; las claves son del nivel Interno (logs.*).
CATALOGO: dict[str, tuple[str, frozenset[str]]] = {
    "corrida_inicio": ("INFO", frozenset()),
    "corrida_fin": ("INFO", frozenset({"resultado", "duracion_s_total"})),
    "etapa_inicio": ("INFO", frozenset({"etapa"})),
    "etapa_fin": ("INFO", frozenset({"etapa", "duracion_s", "resultado"})),
    "archivo_cargado": ("INFO", frozenset({"fuente", "archivo", "leidas", "cargadas", "rechazadas_por_motivo"})),
    "archivo_omitido": ("INFO", frozenset({"fuente", "archivo"})),
    "archivo_ignorado": ("INFO", frozenset({"fuente", "archivo", "motivo"})),
    "archivo_rechazado": ("WARNING", frozenset({"fuente", "archivo", "motivo"})),
    "frontera_fuera_maestro": ("WARNING", frozenset({"fuente", "cantidad"})),
    "stream_fin": ("INFO", frozenset(STREAM_VACIO)),
    "salida_escrita": ("INFO", frozenset({"nombre"})),
    "clasificacion_fallo": ("ERROR", frozenset({"causa", "nombres"})),  # nombres de columna/header, nunca valores
    "error": ("ERROR", frozenset({"error_tipo", "sqlstate", "pila", "codigo_error"})),
    "pii_filtrada": ("WARNING", frozenset({"evento_origen", "tipo"})),
    "libreria_externa": ("WARNING", frozenset({"logger", "clase"})),
}


class ClaveNoDeclarada(RuntimeError):
    """Clave fuera del catalogo del evento (solo en modo test; en runtime se redacta)."""


class ErrorInvariante(RuntimeError):
    """filas_leidas != filas_cargadas + suma de rechazadas."""


class Reloj(Protocol):
    def ahora(self) -> datetime: ...
    def monotonico(self) -> float: ...


class RelojSistema:
    def ahora(self) -> datetime:
        return datetime.now(TZ)

    def monotonico(self) -> float:
        return time.monotonic()


# Conteos de metrics.json que tienen version de la base y version de la Corrida (ver DECISIONS.md).
CONTEOS = ("filas_leidas", "filas_cargadas", "filas_rechazadas_por_motivo", "por_fuente", "archivos")


@dataclass(frozen=True)
class Metricas:
    datos: dict[str, Any]

    def a_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.datos))

    def a_json(self) -> str:
        return json.dumps(self.datos, indent=2, ensure_ascii=True) + "\n"

    def con_conteos_de_base(self, base: Mapping[str, Any] | None) -> Metricas:
        """Correccion: los conteos de primer nivel pasan a ser los de la base (`fotos.metricas_acumuladas`)
        y los de esta Corrida quedan en `corrida`. Sin `base` (la Corrida fallo antes de las Fotos) el primer nivel
        repite los de la Corrida y `conteos_de` lo dice."""
        corrida = {k: self.datos[k] for k in CONTEOS if k in self.datos}
        datos: dict[str, Any] = {}
        for k, v in self.datos.items():
            if k == "filas_leidas":
                datos["conteos_de"] = "corrida" if base is None else "base"
            if k == "stream":
                datos["corrida"] = corrida
            datos[k] = base[k] if base is not None and k in CONTEOS else v
        return Metricas(datos)


def _iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="milliseconds")


def _json_seguro(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Mapping):
        return {str(k): _json_seguro(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_seguro(x) for x in v]
    return REDACTADO  # nunca str(obj): puede traer valores de fila


class _ManejadorTerceros(logging.Handler):
    """Root WARNING y kafka ERROR -> evento libreria_externa; nunca el mensaje formateado."""

    def __init__(self, obs: Observabilidad):
        super().__init__(level=logging.WARNING)
        self._obs = obs

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] == "cauce" or self._obs._cerrada:
            return
        if record.name.split(".")[0] == "kafka" and record.levelno < logging.ERROR:
            return
        clase = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "-"
        self._obs.evento("libreria_externa", logger=record.name, clase=clase)


class _Stderr(logging.StreamHandler):
    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setLevel(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr  # tolera redirecciones (capsys, contenedores)
        super().emit(record)


class Observabilidad:
    def __init__(self, modo: str, corrida_id: str, destino: Path, clasificacion: Mapping[str, str],
                 reloj: Reloj):
        if modo not in MODOS:
            raise ValueError(f"modo invalido: {modo}")
        self._modo, self._corrida_id, self._reloj = modo, corrida_id, reloj
        self._clasif = dict(clasificacion)
        self._cerrada = False
        self._hook_previo: Any = None
        self._ts_inicio = reloj.ahora()
        self._t0 = reloj.monotonico()
        self._pii = 0
        self._leidas = self._cargadas = 0
        self._motivos: dict[str, int] = {}
        self._por_fuente: dict[str, dict[str, int]] = {}
        self._archivos = {"cargados": 0, "omitidos": 0, "rechazados": 0}
        self._etapas = {e: 0.0 for e in ETAPAS}
        self._stream = copy.deepcopy(STREAM_VACIO)
        destino.parent.mkdir(parents=True, exist_ok=True)
        self._log = logging.Logger(f"cauce.observabilidad.{id(self)}")  # fuera del arbol global
        self._log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(message)s")
        self._manejadores: list[logging.Handler] = [logging.FileHandler(destino, encoding="utf-8"), _Stderr()]
        for h in self._manejadores:
            h.setFormatter(fmt)
            self._log.addHandler(h)
        self._terceros = _ManejadorTerceros(self)
        self._raiz = logging.getLogger()
        self._kafka = logging.getLogger("kafka")
        self._nivel_raiz, self._nivel_kafka = self._raiz.level, self._kafka.level
        self._raiz.setLevel(logging.WARNING)
        self._kafka.setLevel(logging.ERROR)
        self._raiz.addHandler(self._terceros)
        self.evento("corrida_inicio")

    def clasificar(self, clasificacion: Mapping[str, str]) -> None:
        """Fija los niveles `logs.*` una vez cargado el yaml (la clasificacion se carga despues de abrir)."""
        self._clasif = dict(clasificacion)

    @classmethod
    def abrir(cls, modo: str, corrida_id: str, destino: Path, clasificacion: Mapping[str, str],
              reloj: Reloj) -> Observabilidad:
        """`clasificacion`: clave de log -> nivel (de data_classification.yaml, logs.*); ausente = Interno."""
        return cls(modo, corrida_id, destino, clasificacion, reloj)

    # --- eventos ---------------------------------------------------------------------------
    def evento(self, nombre: str, /, **campos: Any) -> None:
        nivel, permitidas = CATALOGO[nombre]
        datos: dict[str, Any] = {}
        for k, v in campos.items():
            if k in _BASE or k not in permitidas:
                if self._modo == "test":
                    raise ClaveNoDeclarada(f"{nombre}: clave no declarada en el catalogo: {k}")
                datos[k] = REDACTADO
            elif self._clasif.get(k, "Interno") not in _NIVELES_VISIBLES:
                datos[k] = REDACTADO
            else:
                datos[k] = _json_seguro(v)
        self._acumular(nombre, campos)
        self._emitir(nombre, nivel, datos)

    def _emitir(self, nombre: str, nivel: str, datos: dict[str, Any]) -> None:
        base = {"ts": _iso(self._reloj.ahora()), "nivel": nivel, "evento": nombre,
                "corrida_id": self._corrida_id, "modo": self._modo}
        linea = json.dumps({**base, **datos}, ensure_ascii=True)
        linea, tipos = pii.enmascarar_estricto(linea)
        self._log.log(NIVELES[nivel], linea)
        if tipos and nombre != "pii_filtrada":
            for t in tipos:
                self._pii += 1
                self._emitir("pii_filtrada", "WARNING", {"evento_origen": nombre, "tipo": t.value})

    def error(self, exc: BaseException, codigo_error: str | None = None) -> None:
        """Evento de error sin str(e): tipo, sqlstate y pila (archivo, linea, funcion)."""
        pila = [[Path(f.filename).name, f.lineno, f.name] for f in traceback.extract_tb(exc.__traceback__)]
        campos: dict[str, Any] = {"error_tipo": type(exc).__name__, "pila": pila}
        sqlstate = getattr(exc, "pgcode", None)
        if isinstance(sqlstate, str):
            campos["sqlstate"] = sqlstate
        codigo = codigo_error or getattr(exc, "codigo_error", None)
        if isinstance(codigo, str):
            campos["codigo_error"] = codigo
        self.evento("error", **campos)

    def instalar_excepthook(self) -> None:
        self._hook_previo = sys.excepthook

        def hook(tipo: type[BaseException], valor: BaseException, tb: Any) -> None:
            if valor.__traceback__ is None:
                valor = valor.with_traceback(tb)
            if not self._cerrada:
                self.error(valor)
            print(f"cauce: error no manejado: {tipo.__name__}", file=sys.stderr)

        sys.excepthook = hook

    # --- etapas y metricas -----------------------------------------------------------------
    @contextmanager
    def etapa(self, nombre: str) -> Iterator[None]:
        if nombre not in ETAPAS:
            raise ValueError(f"etapa desconocida: {nombre}")
        self.evento("etapa_inicio", etapa=nombre)
        t0 = self._reloj.monotonico()
        resultado = "exito"
        try:
            yield
        except BaseException:
            resultado = "fallo"
            raise
        finally:
            dur = round(self._reloj.monotonico() - t0, 3)
            self.evento("etapa_fin", etapa=nombre, duracion_s=dur, resultado=resultado)

    def _acumular(self, nombre: str, c: Mapping[str, Any]) -> None:
        if nombre == "etapa_fin" and c.get("etapa") in self._etapas:
            self._etapas[c["etapa"]] = round(self._etapas[c["etapa"]] + float(c["duracion_s"]), 3)
        elif nombre == "archivo_cargado":
            motivos = {str(k): int(v) for k, v in dict(c.get("rechazadas_por_motivo") or {}).items()}
            rech = sum(motivos.values())
            if c["leidas"] != c["cargadas"] + rech:
                raise ErrorInvariante("archivo_cargado: leidas != cargadas + rechazadas")
            self._leidas += c["leidas"]
            self._cargadas += c["cargadas"]
            for m, n in motivos.items():
                self._motivos[m] = self._motivos.get(m, 0) + n
            f = self._por_fuente.setdefault(str(c["fuente"]), {"leidas": 0, "cargadas": 0, "rechazadas": 0})
            f["leidas"] += c["leidas"]
            f["cargadas"] += c["cargadas"]
            f["rechazadas"] += rech
            self._archivos["cargados"] += 1
        elif nombre == "archivo_omitido":
            self._archivos["omitidos"] += 1
        elif nombre == "archivo_rechazado":
            self._archivos["rechazados"] += 1
        elif nombre == "stream_fin":
            self._stream = copy.deepcopy({**STREAM_VACIO, **{k: v for k, v in c.items() if k in STREAM_VACIO}})

    def cerrar(self, resultado: str) -> Metricas:
        """Emite corrida_fin y devuelve las metricas de la Corrida. No escribe."""
        if resultado not in ("exito", "fallo"):
            raise ValueError("resultado debe ser exito|fallo")
        if self._leidas != self._cargadas + sum(self._motivos.values()):
            raise ErrorInvariante("filas_leidas != filas_cargadas + rechazadas")
        total = round(self._reloj.monotonico() - self._t0, 3)
        fin = self._reloj.ahora()
        self.evento("corrida_fin", resultado=resultado, duracion_s_total=total)
        self._raiz.removeHandler(self._terceros)
        return Metricas({
            "corrida_id": self._corrida_id, "ts_inicio": _iso(self._ts_inicio), "ts_fin": _iso(fin),
            "resultado": resultado, "filas_leidas": self._leidas, "filas_cargadas": self._cargadas,
            "filas_rechazadas_por_motivo": dict(self._motivos),
            "por_fuente": {k: dict(v) for k, v in self._por_fuente.items()},
            "archivos": dict(self._archivos), "duracion_s_por_etapa": dict(self._etapas),
            "duracion_s_total": total, "pii_filtrada_en_logs": self._pii,
            "stream": copy.deepcopy(self._stream),
        })

    def liberar(self) -> None:
        """Cierra archivos y restaura logging/excepthook. Idempotente."""
        if self._cerrada:
            return
        self._cerrada = True
        self._raiz.removeHandler(self._terceros)
        self._raiz.setLevel(self._nivel_raiz)
        self._kafka.setLevel(self._nivel_kafka)
        if self._hook_previo is not None:
            sys.excepthook = self._hook_previo
        for h in self._manejadores:
            self._log.removeHandler(h)
            h.close()
