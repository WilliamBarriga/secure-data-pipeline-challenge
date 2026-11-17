"""Puerto `FuenteEventos`: el unico puerto real del sistema. Devuelve los eventos crudos del stream, tal como
llegan (bytes del mensaje), y el Detector los valida. Dos adapters: Kafka y en memoria (tests, este modulo).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from enum import StrEnum
from typing import Protocol

from kafka import KafkaAdminClient, KafkaConsumer
from kafka.admin import NewTopic
from kafka.errors import KafkaError, TopicAlreadyExistsError
from kafka.structs import TopicPartition

from cauce import entorno


class Corte(StrEnum):
    """Por que una lectura termino antes del final del stream (red de seguridad): la Corrida queda parcial."""
    INACTIVIDAD = "inactividad"
    TOPE = "tope"


class FuenteEventos(Protocol):
    corte: Corte | None
    """None si la ultima lectura de `eventos()` llego al final del stream; si no, por que se corto."""

    def eventos(self) -> Iterator[bytes]:
        """Los eventos en orden de llegada; termina sola (el stream tiene fin)."""
        ...


class FuenteEnMemoria:
    """Adapter en memoria: una lista de eventos crudos. `str` se codifica en UTF-8."""

    def __init__(self, eventos: Iterable[bytes | str]):
        self._eventos = tuple(e.encode("utf-8") if isinstance(e, str) else e for e in eventos)
        self.corte: Corte | None = None  # la lista siempre se lee entera

    @classmethod
    def desde_ndjson(cls, contenido: bytes) -> FuenteEnMemoria:
        """Una linea no vacia = un evento (como `tools/producer.py`)."""
        return cls(linea for linea in contenido.split(b"\n") if linea.strip())

    def eventos(self) -> Iterator[bytes]:
        return iter(self._eventos)


TOPICO = entorno.TOPICO
_TIMEOUT_MS = 10_000  # conexion y metadatos: falla rapido si Redpanda no esta


class KafkaNoDisponible(RuntimeError):
    """Redpanda inalcanzable o el topico no existe. Nombra el topico, nunca contenido de mensajes."""
    codigo_error = "infraestructura_no_disponible"


def _no_disponible(que: str) -> KafkaNoDisponible:
    return KafkaNoDisponible(f"Kafka no disponible ({que}): corre `make up`")


def asegurar_topico(bootstrap: str, topico: str = TOPICO, particiones: int = 3) -> bool:
    """Crea `topico` si falta (auto-create esta apagado en Redpanda). Devuelve True si lo creo."""
    try:
        # pyright usa sus stubs de kafka-python 2.3.2; `bootstrap_timeout_ms` es de la 3.x instalada
        admin = KafkaAdminClient(bootstrap_servers=bootstrap,
                                 bootstrap_timeout_ms=_TIMEOUT_MS,  # pyright: ignore[reportCallIssue]
                                 request_timeout_ms=_TIMEOUT_MS)
    except KafkaError:
        raise _no_disponible("broker") from None
    try:
        admin.create_topics([NewTopic(topico, num_partitions=particiones, replication_factor=1)])
        return True
    except TopicAlreadyExistsError:
        return False
    except KafkaError:
        raise _no_disponible("crear topico") from None
    finally:
        admin.close()


class FuenteKafka:
    """Adapter Kafka (kafka-python): `assign` de todas las particiones, sin consumer group ni commits, desde earliest
    hasta el high watermark de Kafka (offset final por particion) tomado al empezar a leer, despues de producir. No
    es la Marca de agua del Detector. Termina sola: al llegar a ese offset en todas las particiones, o por la red de
    seguridad (`inactividad_s` sin mensajes o `tope_s` en total, conexion incluida); en ese caso `corte` dice cual
    (`Corte`) y la Corrida queda parcial."""

    def __init__(self, bootstrap: str, topico: str = TOPICO, *, inactividad_s: float = 10.0, tope_s: float = 210.0,
                 reloj: Callable[[], float] = time.monotonic):
        self._bootstrap, self._topico = bootstrap, topico
        self._inactividad_s, self._tope_s, self._reloj = inactividad_s, tope_s, reloj
        self.corte: Corte | None = None

    def _consumidor(self) -> KafkaConsumer:
        try:
            return KafkaConsumer(bootstrap_servers=self._bootstrap, group_id=None, enable_auto_commit=False,
                                 auto_offset_reset="earliest",
                                 bootstrap_timeout_ms=_TIMEOUT_MS,  # pyright: ignore[reportCallIssue] (stubs 2.3.2)
                                 request_timeout_ms=_TIMEOUT_MS + 1000)
        except KafkaError:
            raise _no_disponible("broker") from None

    def eventos(self) -> Iterator[bytes]:
        self.corte = None
        t0 = self._reloj()  # el tope cuenta desde antes de conectar: es parte de la cota de `make stream` < 300 s
        consumidor = self._consumidor()
        try:
            particiones = consumidor.partitions_for_topic(self._topico)
            if not particiones:
                raise _no_disponible(f"no existe el topico {self._topico}")
            tps = [TopicPartition(self._topico, p) for p in sorted(particiones)]
            consumidor.assign(tps)
            inicio = consumidor.beginning_offsets(tps)
            fin = consumidor.end_offsets(tps)  # high watermark de Kafka: lo producido despues no se lee
            for tp in tps:
                consumidor.seek(tp, inicio[tp])
            pendientes = {tp for tp in tps if fin[tp] > inicio[tp]}
            ultimo = self._reloj()
            while pendientes:
                ahora = self._reloj()
                if ahora - t0 >= self._tope_s:
                    self.corte = Corte.TOPE
                    return
                if ahora - ultimo >= self._inactividad_s:
                    self.corte = Corte.INACTIVIDAD
                    return
                lote = consumidor.poll(timeout_ms=500)
                for tp, mensajes in lote.items():
                    for m in mensajes:
                        if m.offset < fin[tp]:
                            yield m.value
                    if tp in pendientes and mensajes and mensajes[-1].offset + 1 >= fin[tp]:
                        pendientes.discard(tp)
                        consumidor.pause(tp)
                if lote:
                    ultimo = self._reloj()
        except KafkaError:
            raise _no_disponible("lectura") from None
        finally:
            consumidor.close()
