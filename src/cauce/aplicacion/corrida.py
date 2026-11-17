"""Ciclo de vida comun de una Corrida (batch y stream). `ejecutar` es la unica entrada: `abrir_corrida`
(corrida_inicio, lock, rollback ante error, corrida_fin, logs/pipeline.log y out/metrics.json) -> etapa
`clasificacion` fail-fast -> el cuerpo del runner (`corrida_batch`, `corrida_stream`) -> exit code. Una Corrida
no iniciada (lock tomado) termina en exit 1.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol

import psycopg2

from cauce.bordes import almacen, bitacora, salidas
from cauce.bordes.bitacora import Evento, Resultado
from cauce.bordes.observabilidad import Metricas
from cauce.config import Config
from cauce.nucleo import clasificacion, fuentes
from cauce.nucleo.clasificacion import Causa, Clasificacion, ClasificacionIncompleta


class ObservabilidadCorrida(Protocol):
    """Lo que la Corrida usa de los logs y las metricas. `bordes.observabilidad.Observabilidad` lo cumple;
    los tests le pasan un doble."""

    def clasificar(self, clasificacion: Mapping[str, str]) -> None: ...
    def evento(self, nombre: str, /, **campos: Any) -> None: ...
    def error(self, exc: BaseException, codigo_error: str | None = None) -> None: ...
    def etapa(self, nombre: str) -> AbstractContextManager[None]: ...
    def cerrar(self, resultado: str) -> Metricas: ...


def leer_clasificacion(ruta: Path) -> Clasificacion:
    """Borde de I/O: lee el yaml y se lo pasa al nucleo puro `clasificacion`."""
    try:
        texto = ruta.read_text(encoding="utf-8")
    except OSError:
        raise ClasificacionIncompleta([Causa("esquema_invalido", ("archivo_ilegible",))]) from None
    return Clasificacion.desde_texto(texto)


def verificar_esquema(con, ruta: Path, obs: ObservabilidadCorrida | None) -> Clasificacion:
    """Check fail-fast (C1) dentro de una Corrida (batch y stream): el catalogo de todas las fuentes, incluido el
    esquema declarado de `mediciones.raw`, contra el yaml y las columnas del modelo."""
    clasif = leer_clasificacion(ruta)
    if obs is not None:
        obs.clasificar(clasif.niveles_logs())  # el filtro de logs por clave sale del yaml
    clasif.verificar(fuentes.CATALOGO, almacen.columnas_modelo(con))
    return clasif


def verificar_clasificacion(cfg: Config, ruta: Path = clasificacion.RUTA_POR_DEFECTO) -> Clasificacion:
    """Check fail-fast (C1) suelto (`make test`). Levanta ClasificacionIncompleta."""
    con = almacen.conectar(cfg, cfg.ingesta)
    try:
        return verificar_esquema(con, ruta, None)
    finally:
        con.close()


def codigo_de(exc: BaseException) -> str:
    """Contrato unico de errores: `codigo_error` de clase si lo hay;
    psycopg2 -> error_base_datos; si no, error_interno."""
    codigo = getattr(exc, "codigo_error", None)
    if isinstance(codigo, str):
        return codigo
    return "error_base_datos" if isinstance(exc, psycopg2.Error) else "error_interno"


# fallos esperados: se registran y el exit code es 1; el resto sube a la cli (exit 2).
FALLOS_ESPERADOS = (almacen.CorridaConcurrente, psycopg2.Error)


class CorridaNoIniciada(RuntimeError):
    """Un fallo esperado al tomar el lock, antes del cuerpo: ya quedo en la Bitacora y en metrics.json.

    Un context manager no puede saltarse el cuerpo, asi que `abrir_corrida` lo levanta desde `__enter__` y el
    llamador lo atrapa (exit 1). Envuelve la causa para no confundirla con un psycopg2.Error de
    corrida_inicio/corrida_fin, que sigue subiendo tal cual."""


class EstadoCorrida:
    """Lo que ve el cuerpo del `with`: la conexion, la identidad de la Corrida y el resultado (`ok` | `parcial`)."""

    def __init__(self, ident: bitacora.Identidad):
        self.con = None
        self.ident = ident
        self.resultado: Resultado = Resultado.OK
        self.conteos_base: dict[str, Any] | None = None  # el cuerpo batch los fija junto con las Fotos

    @property
    def exit_code(self) -> int:
        return 0 if self.resultado in (Resultado.OK, Resultado.PARCIAL) else 1


@contextmanager
def abrir_corrida(cfg: Config, obs: ObservabilidadCorrida, corrida_id: uuid.UUID, comando: str = "run", *,
                  conectar=almacen.conectar, lock=almacen.lock_corrida, escribir=salidas.escribir_atomico):
    """Ciclo de vida de una Corrida (batch y stream): conectar, corrida_inicio, lock, rollback uniforme ante
    cualquier error, corrida_fin con `codigo_error`, cerrar la conexion, `obs.cerrar` y metrics.json.

    El cuerpo hace clasificacion, DEK y etapas y puede fijar `c.resultado = Resultado.PARCIAL`. CorridaConcurrente y
    psycopg2.Error dentro del cuerpo se registran sin relanzar (`c.resultado == "fallo"`); si ocurren al tomar el
    lock (antes de entrar al cuerpo) se registran y se levanta `CorridaNoIniciada`, que el llamador atrapa.
    El resto se registra y se relanza siempre.
    """
    ident = bitacora.identidad(corrida_id, comando)
    c = EstadoCorrida(ident)
    fallo: Exception | None = None
    dentro = False
    try:
        con = c.con = conectar(cfg, cfg.ingesta)
        try:
            bitacora.registrar_evento(con, ident, Evento.CORRIDA_INICIO)
            con.commit()
            codigo = None
            try:
                with lock(con):  # una Corrida a la vez; la segunda falla
                    dentro = True
                    yield c
            except Exception as e:
                con.rollback()
                if isinstance(e, ClasificacionIncompleta):
                    for causa in e.causas:  # nombres de columna/header, nunca valores
                        obs.evento("clasificacion_fallo", causa=causa.codigo, nombres=list(causa.nombres))
                codigo = codigo_de(e)
                obs.error(e, codigo)
                c.resultado = Resultado.FALLO
                if not isinstance(e, FALLOS_ESPERADOS):
                    fallo = e
                elif not dentro:
                    fallo = CorridaNoIniciada(type(e).__name__)
                    fallo.__cause__ = e
            bitacora.registrar_evento(con, ident, Evento.CORRIDA_FIN, resultado=c.resultado,
                                      codigo_error=codigo)
            con.commit()
        finally:
            con.close()
    except Exception as e:  # sin conexion o sin Bitacora: el cierre y metrics.json se escriben igual; luego se relanza
        obs.error(e)
        fallo = e
    metricas = obs.cerrar("fallo" if fallo or c.resultado == Resultado.FALLO else "exito")
    metricas = metricas.con_conteos_de_base(c.conteos_base)
    try:
        escribir("metrics.json", metricas.a_json())
        obs.evento("salida_escrita", nombre="metrics.json")
    except salidas.PiiEnSalida as e:
        obs.error(e)
        raise
    if fallo:
        raise fallo


def ejecutar(cfg: Config, obs: ObservabilidadCorrida, corrida_id: uuid.UUID, comando: str,
             cuerpo: Callable[[EstadoCorrida, Clasificacion], None], *,
             ruta_clasificacion: Path = clasificacion.RUTA_POR_DEFECTO, escribir=salidas.escribir_atomico) -> int:
    """Esqueleto de toda Corrida: `abrir_corrida` -> etapa `clasificacion` (fail-fast, antes de leer datos) ->
    `cuerpo(c, clasif)`, que puede fijar `c.resultado`. Devuelve el exit code; la Corrida no iniciada es 1.
    `escribir` es como se escribe metrics.json (el stream reemplaza solo su subclave)."""
    try:
        with abrir_corrida(cfg, obs, corrida_id, comando, escribir=escribir) as c:
            with obs.etapa("clasificacion"):  # antes de leer datos
                clasif = verificar_esquema(c.con, ruta_clasificacion, obs)
            cuerpo(c, clasif)
    except CorridaNoIniciada:  # ya registrada en la Bitacora y en metrics.json
        return 1
    return c.exit_code
