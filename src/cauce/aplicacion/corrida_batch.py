"""Cuerpo de `make run` (Corrida batch): por cada etapa de `ETAPAS`, descubrir e `ingerir` cada archivo ->
Fotos de out/. El ciclo de vida (lock, clasificacion fail-fast, Bitacora, metrics.json) es `corrida.ejecutar`.

Que se carga y en que orden es una tabla (`ETAPAS`, por nombre de fuente; donde estan sus archivos lo dice su
Ficha de Fuente); lo que le pasa a cada archivo lo decide `ingesta_archivo.ingerir`. La Bitacora lleva un
etapa_fin por fuente y un evento por archivo.
"""
from __future__ import annotations

import uuid
from itertools import groupby
from pathlib import Path

from cauce.aplicacion import corrida
from cauce.aplicacion.corrida import EstadoCorrida, ObservabilidadCorrida
from cauce.aplicacion.ingesta_archivo import ContextoCarga, Desenlace, conteos_bitacora, evento_log, ingerir
from cauce.bordes import almacen, bitacora, fotos, salidas
from cauce.bordes.bitacora import Conteos, Resultado
from cauce.config import Config
from cauce.nucleo import clasificacion, custodia, fuentes
from cauce.nucleo.clasificacion import Clasificacion

# Etapas de carga en orden, con sus fuentes en orden: facturacion va despues de los maestros (la Frontera huerfana se
# resuelve contra `core.dim_frontera`). Los archivos de una fuente quedan contiguos dentro de su etapa.
ETAPAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fronteras", ("frontera", "limite")),
    ("clientes", ("cliente",)),
    ("consumos", ("consumo",)),
    ("facturacion", ("factura",)),
)


def _motivo_ignorado(entrada: Path, extension: str) -> str | None:
    if entrada.name.startswith("."):
        return "oculto"
    if entrada.is_dir():
        return "subdirectorio"
    if not entrada.is_file():
        return "no_regular"
    if entrada.suffix != extension:
        return "extension"
    return None


def descubrir(dir_datos: Path, fuente: str, obs: ObservabilidadCorrida) -> list[tuple[str, str, Path]]:
    """(fuente, nombre, ruta) de los archivos de la fuente que existen en su origen (Ficha de Fuente), en orden de
    carga. Al descubrir por extension, lo que no aplica (oculto, subdirectorio, no regular, otra extension) se
    ignora y se registra en el log, solo por nombre."""
    origen = fuentes.ficha_por_lotes(fuente).origen
    directorio = origen.directorio(dir_datos)
    if origen.extension is None:
        return [(fuente, r.name, r) for r in origen.rutas(dir_datos) if r.is_file()]
    if not directorio.is_dir():
        return []
    archivos = []
    for entrada in sorted(directorio.iterdir(), key=lambda e: e.name):
        motivo = _motivo_ignorado(entrada, origen.extension)
        if motivo is None:
            archivos.append((fuente, entrada.name, entrada))
        else:
            obs.evento("archivo_ignorado", fuente=fuente, archivo=entrada.name, motivo=motivo)
    return archivos


def _cargar_archivo(ctx: ContextoCarga, obs: ObservabilidadCorrida, fuente: str, nombre: str,
                    ruta: Path) -> Desenlace:
    """Ingiere un archivo y loguea su Desenlace."""
    d = ingerir(ctx, fuente, nombre, ruta)
    evento, campos = evento_log(d, fuente, nombre)
    obs.evento(evento, **campos)
    if fuera := d.conteos.get(almacen.CONTADOR_FUERA_MAESTRO):  # sin FK: la Frontera ausente se acepta y se avisa
        obs.evento("frontera_fuera_maestro", fuente=fuente, cantidad=fuera)
    return d


def _cargar_etapa(ctx: ContextoCarga, obs: ObservabilidadCorrida, etapa: str,
                  archivos: list[tuple[str, str, Path]]) -> Resultado:
    """Una etapa de carga: ingiere cada archivo y escribe un `etapa_fin` por fuente con los conteos sumados de
    todos sus archivos (`parcial` si alguno se rechazo). Devuelve el resultado de la etapa (ok|parcial). Los
    maestros son una fuente por archivo, asi que ahi sale uno por archivo."""
    resultado = Resultado.OK
    with obs.etapa(etapa):
        for fuente, de_la_fuente in groupby(archivos, key=lambda a: a[0]):  # los de una fuente son contiguos
            desenlaces = [_cargar_archivo(ctx, obs, *a) for a in de_la_fuente]
            conteos = [conteos_bitacora(d) for d in desenlaces]
            totales = Conteos(sum(c.leidas for c in conteos), sum(c.cargadas for c in conteos),
                              sum(c.cuarentena for c in conteos))
            parcial = any(d.tipo == "rechazado" for d in desenlaces)
            bitacora.registrar_etapas_fin(ctx.con, ctx.ident,
                                          [(fuente, totales, Resultado.PARCIAL if parcial else Resultado.OK)])
            if parcial:
                resultado = Resultado.PARCIAL
    return resultado


def correr_batch(cfg: Config, obs: ObservabilidadCorrida, corrida_id: uuid.UUID,
                 ruta_clasificacion: Path = clasificacion.RUTA_POR_DEFECTO, *, dir_datos: Path) -> int:
    def cuerpo(c: EstadoCorrida, clasif: Clasificacion) -> None:
        custodio = custodia.Custodio(cfg.kek, cfg.k_bidx)
        almacen.abrir_dek(c.con, custodio)  # KekInvalida: la KEK no abre la DEK del volumen
        ctx = ContextoCarga(c.con, c.ident, clasif, custodio)
        for etapa, de_la_etapa in ETAPAS:
            archivos = [a for f in de_la_etapa for a in descubrir(dir_datos, f, obs)]
            if _cargar_etapa(ctx, obs, etapa, archivos) == Resultado.PARCIAL:
                c.resultado = Resultado.PARCIAL
        with obs.etapa("salidas"):
            for nombre, contenido in fotos.materializar_salidas(c.con).items():
                salidas.escribir_atomico(nombre, contenido)
                obs.evento("salida_escrita", nombre=nombre)
            c.conteos_base = fotos.metricas_acumuladas(c.con)

    return corrida.ejecutar(cfg, obs, corrida_id, "run", cuerpo, ruta_clasificacion=ruta_clasificacion)
