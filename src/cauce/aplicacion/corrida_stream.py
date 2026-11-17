"""Orquestador delgado de `make stream`: clasificacion fail-fast (incluye el esquema de `mediciones.raw`) ->
Bitacora inicio (como `ingesta`, via `corrida.ejecutar`) -> `FuenteEventos` -> `alertas.Detector` -> `alertas.json`
atomico -> `metrics.json` (solo la subclave `stream`) -> Bitacora fin.

No persiste Mediciones, Alertas ni `medidor` en Postgres (Minimizacion): la base solo ve la Bitacora. El universo de
Fronteras y la Banda operativa salen de los maestros de data/ (`limites_frontera.csv`, brief §6).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from cauce.aplicacion import corrida
from cauce.aplicacion.corrida import ObservabilidadCorrida
from cauce.bordes import bitacora, salidas
from cauce.bordes.bitacora import Conteos, Resultado
from cauce.bordes.fuente_eventos import FuenteEventos
from cauce.config import Config
from cauce.nucleo import clasificacion, fuentes
from cauce.nucleo.alertas import Alertas, Detector
from cauce.nucleo.clasificacion import Clasificacion

# `fuente` de los etapa_fin del stream: stream | stream.descartado.<motivo> | stream.alerta.<tipo>
PREFIJO_BITACORA = "stream"


class MaestroNoDisponible(RuntimeError):
    """Falta o es ilegible un maestro que define el universo del stream. Nombra el archivo, nunca valores."""
    codigo_error = "configuracion_invalida"


def _maestro(dir_datos: Path, fuente: str) -> fuentes.Resultado:
    origen = fuentes.ficha_por_lotes(fuente).origen
    ruta = origen.rutas(dir_datos)[0]
    try:
        datos = ruta.read_bytes()
    except OSError:
        raise MaestroNoDisponible(f"falta data/{origen.subdir}/{ruta.name}") from None
    leido = fuentes.leer(fuente, ruta.name, datos)
    if leido.archivo_rechazado is not None or not leido.registros:
        raise MaestroNoDisponible(f"data/{origen.subdir}/{ruta.name} ilegible o vacio")
    return leido


def detector_del_maestro(dir_datos: Path) -> Detector:
    """Universo = Fronteras de `fronteras.csv`; Banda operativa = `max_kwh` de `limites_frontera.csv`."""
    maestro, limites = _maestro(dir_datos, "frontera"), _maestro(dir_datos, "limite")
    return Detector(fronteras=(r.campos["frontera_id"] for r in maestro.registros),
                    max_kwh={r.campos["frontera_id"]: r.campos["max_kwh"] for r in limites.registros})


def alertas_json(cierre: Alertas) -> str:
    """`out/alertas.json`: lista ordenada por (tipo, frontera_id, ts), sin duplicados, `ts` ISO 8601 con -05:00."""
    filas = [{"tipo": a.tipo.value, "frontera_id": a.frontera_id, "ts": a.ts.isoformat()} for a in cierre.alertas]
    return json.dumps(filas, indent=2, ensure_ascii=True) + "\n"


def escribir_metrics_stream(nombre: str, contenido: str) -> Path:
    """Reemplaza solo la subclave `stream` de `metrics.json` (las claves del batch quedan intactas, ver
    `salidas.reemplazar_clave`). La subclave lleva ademas la identidad y el resultado de la Corrida del stream."""
    m: dict[str, Any] = json.loads(contenido)
    stream = {**m["stream"], "corrida_id": m["corrida_id"], "ts_fin": m["ts_fin"], "resultado": m["resultado"]}
    return salidas.reemplazar_clave(nombre, "stream", stream)


def _registrar_cierre(con, ident: bitacora.Identidad, cierre: Alertas) -> None:
    """etapa_fin del stream: totales, uno por motivo de descarte y uno por tipo de Alerta (nombres de catalogo
    cerrado en `fuente`; solo conteos). Una Alerta no es una Medicion valida ni se carga en Postgres
    (Minimizacion): `stream.alerta.<TIPO>` lleva la cantidad emitida en `leidas` y 0 cargadas."""
    filas = [(PREFIJO_BITACORA, Conteos(cierre.leidos, cierre.validos, cierre.invalidos, cierre.duplicados))]
    filas += [(f"{PREFIJO_BITACORA}.descartado.{m}", Conteos(n, 0, n)) for m, n in sorted(cierre.descartados.items())]
    filas += [(f"{PREFIJO_BITACORA}.alerta.{t}", Conteos(n, 0, 0)) for t, n in sorted(cierre.por_tipo().items()) if n]
    bitacora.registrar_etapas_fin(con, ident, [(fuente, conteos, Resultado.OK) for fuente, conteos in filas])


def correr_stream(cfg: Config, obs: ObservabilidadCorrida, corrida_id: uuid.UUID, fuente: FuenteEventos,
                  ruta_clasificacion: Path = clasificacion.RUTA_POR_DEFECTO, *, dir_datos: Path) -> int:
    def cuerpo(c: corrida.EstadoCorrida, clasif: Clasificacion) -> None:
        detector = detector_del_maestro(dir_datos)
        t0 = time.monotonic()
        for evento in fuente.eventos():
            detector.procesar(evento)
        cierre = detector.cerrar()
        salidas.escribir_atomico("alertas.json", alertas_json(cierre))
        obs.evento("salida_escrita", nombre="alertas.json")
        obs.evento("stream_fin", **cierre.resumen(), duracion_s=round(time.monotonic() - t0, 3))
        _registrar_cierre(c.con, c.ident, cierre)
        if fuente.corte is not None:  # red de seguridad: no llego al high watermark de Kafka
            c.resultado = Resultado.PARCIAL

    # la clasificacion de `ejecutar` corre antes de leer: el esquema de mediciones.raw tiene que estar clasificado
    return corrida.ejecutar(cfg, obs, corrida_id, "stream", cuerpo, ruta_clasificacion=ruta_clasificacion,
                            escribir=escribir_metrics_stream)
