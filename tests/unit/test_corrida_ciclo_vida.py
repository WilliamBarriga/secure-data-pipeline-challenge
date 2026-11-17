"""Ciclo de vida de la Corrida (`abrir_corrida`) sin Postgres: dobles minimos de conexion y de Observabilidad."""
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from typing import cast

import psycopg2
import pytest

from cauce import config
from cauce.aplicacion import corrida, corrida_batch
from cauce.aplicacion.ingesta_archivo import SIN_CONTEOS, ContextoCarga, Desenlace
from cauce.arranque import bootstrap, secretos
from cauce.bordes import almacen, bitacora, salidas
from cauce.bordes.bitacora import Resultado
from cauce.bordes.observabilidad import Metricas
from cauce.config import Config
from cauce.nucleo import custodia
from cauce.nucleo.clasificacion import Causa, ClasificacionIncompleta


class Cursor:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "registrar_evento" in sql:  # params: (corrida_id, evento, ..., resultado, codigo_error)
            self.con.llamadas.append(("evento", params[1], params[11], params[12]))

    def fetchone(self):
        return (1,)


class Con:
    def __init__(self):
        self.llamadas = []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.llamadas.append(("commit",))

    def rollback(self):
        self.llamadas.append(("rollback",))

    def close(self):
        self.llamadas.append(("close",))


# doble de Config: `abrir_corrida` solo lee `ingesta` (sin Protocol: Config no tiene un segundo adapter)
CFG = cast(Config, SimpleNamespace(ingesta=None))


class Obs:
    def __init__(self):
        self.llamadas = []

    def error(self, exc, codigo_error=None):
        self.llamadas.append(("error", type(exc).__name__, codigo_error))

    def evento(self, nombre, /, **campos):
        self.llamadas.append(("evento", nombre, campos))

    def clasificar(self, clasificacion):
        self.llamadas.append(("clasificar",))

    def etapa(self, nombre):
        return nullcontext()

    def cerrar(self, resultado):
        self.llamadas.append(("cerrar", resultado))
        return Metricas({})


class Armado:
    def __init__(self, lock=None, escribir=None, falla_conexion=None):
        self.con, self.obs, self.escritos = Con(), Obs(), []
        self.lock = lock or (lambda con: nullcontext())
        self.escribir = escribir or (lambda nombre, texto: self.escritos.append(nombre))
        self.falla_conexion = falla_conexion

    def abrir(self):
        def conectar(cfg, usuario):
            if self.falla_conexion:
                raise self.falla_conexion
            return self.con
        return corrida.abrir_corrida(CFG, self.obs, uuid.uuid4(), conectar=conectar,
                                     lock=self.lock, escribir=self.escribir)

    def eventos(self):
        """(evento, resultado, codigo_error) de la Bitacora, en orden."""
        return [c[1:] for c in self.con.llamadas if c[0] == "evento"]

    def secuencia(self):
        return [c[1] if c[0] == "evento" else c[0] for c in self.con.llamadas]


def lock_concurrente(con):
    raise almacen.CorridaConcurrente("otra")


def test_camino_ok():
    a = Armado()
    with a.abrir() as c:
        pass
    assert c.resultado == "ok" and c.exit_code == 0
    assert a.secuencia() == ["corrida_inicio", "commit", "corrida_fin", "commit", "close"]
    assert a.eventos() == [("corrida_inicio", None, None), ("corrida_fin", "ok", None)]
    assert ("cerrar", "exito") in a.obs.llamadas
    assert a.escritos == ["metrics.json"]


def test_parcial_lo_fija_el_cuerpo():
    a = Armado()
    with a.abrir() as c:
        c.resultado = Resultado.PARCIAL
    assert a.eventos()[-1] == ("corrida_fin", "parcial", None)
    assert c.exit_code == 0
    assert ("cerrar", "exito") in a.obs.llamadas


def test_concurrente_se_registra_y_sale_de_enter():
    """El cuerpo no corre; un `with` no puede saltarselo, asi que el llamador atrapa CorridaNoIniciada."""
    a = Armado(lock=lock_concurrente)
    with pytest.raises(corrida.CorridaNoIniciada) as e:
        with a.abrir():
            pytest.fail("el cuerpo no debe correr")
    assert isinstance(e.value.__cause__, almacen.CorridaConcurrente)
    assert a.eventos()[-1] == ("corrida_fin", "fallo", "corrida_concurrente")
    assert "rollback" in a.secuencia()
    assert ("cerrar", "fallo") in a.obs.llamadas
    assert a.escritos == ["metrics.json"]
    assert isinstance(almacen.CorridaConcurrente("x"), corrida.FALLOS_ESPERADOS)


def test_error_base_datos_no_relanza():
    a = Armado()
    with a.abrir() as c:
        raise psycopg2.Error("x")
    assert c.resultado == "fallo" and c.exit_code == 1
    assert a.eventos()[-1] == ("corrida_fin", "fallo", "error_base_datos")
    assert a.secuencia().index("rollback") < a.secuencia().index("corrida_fin")


@pytest.mark.parametrize("exc,codigo", [
    (custodia.KekInvalida(), "configuracion_invalida"),
    (ClasificacionIncompleta([Causa("esquema_invalido", ("x",))]), "clasificacion_incompleta"),
    (RuntimeError("raro"), "error_interno"),
])
def test_relanza_y_registra_fallo(exc, codigo):
    a = Armado()
    with pytest.raises(type(exc)):
        with a.abrir():
            raise exc
    assert a.eventos()[-1] == ("corrida_fin", "fallo", codigo)
    seq = a.secuencia()
    assert seq.index("rollback") < seq.index("corrida_fin")  # rollback uniforme
    assert seq[-1] == "close"
    assert ("cerrar", "fallo") in a.obs.llamadas
    assert a.escritos == ["metrics.json"]


def test_clasificacion_incompleta_emite_un_evento_por_causa():
    exc = ClasificacionIncompleta([Causa("esquema_invalido", ("a",)), Causa("campo_sin_clasificar", ("b", "c"))])
    a = Armado()
    with pytest.raises(ClasificacionIncompleta):
        with a.abrir():
            raise exc
    fallos = [c for c in a.obs.llamadas if c[0] == "evento" and c[1] == "clasificacion_fallo"]
    assert len(fallos) == 2
    assert ("error", "ClasificacionIncompleta", "clasificacion_incompleta") in a.obs.llamadas


def test_sin_conexion_escribe_metrics_y_relanza():
    a = Armado(falla_conexion=almacen.ErrorConexion("no"))
    with pytest.raises(almacen.ErrorConexion):
        with a.abrir():
            pytest.fail("no debe correr")
    assert ("cerrar", "fallo") in a.obs.llamadas
    assert a.escritos == ["metrics.json"]


def test_pii_en_metrics_relanza():
    def escribir(nombre, texto):
        raise salidas.PiiEnSalida("metrics.json")
    a = Armado(escribir=escribir)
    with pytest.raises(salidas.PiiEnSalida):
        with a.abrir():
            pass
    assert ("error", "PiiEnSalida", None) in a.obs.llamadas


CLASES_DOMINIO = (almacen.CorridaConcurrente, custodia.KekInvalida, ClasificacionIncompleta, salidas.PiiEnSalida,
                  secretos.CredencialDebil, config.ErrorConfig, bootstrap.ErrorBootstrap, almacen.ErrorConexion)


@pytest.mark.parametrize("cls", CLASES_DOMINIO)
def test_codigo_error_de_dominio_esta_en_el_catalogo(cls):
    assert cls.codigo_error in bitacora.CODIGOS_ERROR


@pytest.mark.parametrize("exc,codigo", [(psycopg2.Error("x"), "error_base_datos"), (ValueError("x"), "error_interno")])
def test_codigo_de_excepciones_ajenas_esta_en_el_catalogo(exc, codigo):
    assert corrida.codigo_de(exc) == codigo
    assert codigo in bitacora.CODIGOS_ERROR


class ConQueFallaAlRegistrar(Con):
    def __init__(self, evento):
        super().__init__()
        self.evento = evento

    def cursor(self):
        cur, evento = Cursor(self), self.evento

        def execute(sql, params=()):
            if "registrar_evento" in sql and params[1] == evento:
                raise psycopg2.OperationalError("bitacora caida")
            Cursor.execute(cur, sql, params)
        cur.execute = execute
        return cur


@pytest.mark.parametrize("evento", ["corrida_inicio", "corrida_fin"])
def test_error_de_base_fuera_del_cuerpo_sube_tal_cual(evento):
    """Bitacora caida al abrir o cerrar: no es un fallo esperado; sube (traceback), no exit 1 silencioso."""
    a = Armado()
    a.con = ConQueFallaAlRegistrar(evento)
    with pytest.raises(psycopg2.OperationalError):
        with a.abrir():
            pass
    assert ("cerrar", "fallo") in a.obs.llamadas
    assert a.escritos == ["metrics.json"]


def test_ejecutar_solo_silencia_la_corrida_no_iniciada(monkeypatch):
    """Concurrente al tomar el lock -> exit 1 sin correr el cuerpo; Bitacora caida en corrida_inicio -> sube
    (antes tambien). Es el esqueleto comun de `correr_batch` y `correr_stream`."""
    def cuerpo(c, clasif):
        raise AssertionError("el cuerpo no corre si la Corrida no se inicio")

    original = corrida.abrir_corrida
    a = Armado(lock=lock_concurrente)
    monkeypatch.setattr(corrida, "abrir_corrida", lambda *x, **k: original(
        CFG, a.obs, uuid.uuid4(), conectar=lambda c, u: a.con, lock=a.lock,
        escribir=a.escribir))
    assert corrida.ejecutar(CFG, a.obs, uuid.uuid4(), "run", cuerpo) == 1

    b = Armado()
    b.con = ConQueFallaAlRegistrar("corrida_inicio")
    monkeypatch.setattr(corrida, "abrir_corrida", lambda *x, **k: original(
        CFG, b.obs, uuid.uuid4(), conectar=lambda c, u: b.con, lock=b.lock,
        escribir=b.escribir))
    with pytest.raises(psycopg2.OperationalError):
        corrida.ejecutar(CFG, b.obs, uuid.uuid4(), "run", cuerpo)


# --- etapa_fin: uno por fuente, con los conteos de todos sus archivos -------------------------------------------
class ConEtapas(Con):
    def cursor(self):
        con = self

        class C(Cursor):
            def execute(self, sql, params=()):
                if "registrar_evento" in sql:  # idx 5 fuente, 7-9 conteos, 11 resultado
                    con.llamadas.append(("evento", params[1], params[5], params[7:10], params[11]))
        return C(self)


def _etapa(monkeypatch, tmp_path, desenlaces):
    for i in range(len(desenlaces)):
        (tmp_path / f"c{i}.csv").write_text("x\n")
    archivos = [("consumo", f"c{i}.csv", tmp_path / f"c{i}.csv") for i in range(len(desenlaces))]
    pendientes = iter(desenlaces)
    monkeypatch.setattr(corrida_batch, "ingerir", lambda *a: next(pendientes))
    con = ConEtapas()
    ctx = cast(ContextoCarga, SimpleNamespace(con=con, ident=bitacora.identidad(uuid.uuid4(), "run")))
    resultado = corrida_batch._cargar_etapa(ctx, Obs(), "consumos", archivos)
    return resultado, [x for x in con.llamadas if x[0] == "evento"]


def _cargado(leidas, cargadas, rechazadas):
    return Desenlace("cargado", 1, {"leidas": leidas, "cargadas": cargadas, "rechazadas": rechazadas,
                                    "rechazadas_por_motivo": {}})


def test_etapa_fin_es_uno_por_fuente_con_conteos_agregados(monkeypatch, tmp_path):
    resultado, eventos = _etapa(monkeypatch, tmp_path, [_cargado(10, 8, 2), _cargado(5, 5, 0), _cargado(3, 1, 2)])
    assert eventos == [("evento", "etapa_fin", "consumo", (18, 14, 4), "ok")]
    assert resultado == "ok"


def test_etapa_fin_es_parcial_si_algun_archivo_se_rechazo(monkeypatch, tmp_path):
    rechazado = Desenlace("rechazado", 2, dict(SIN_CONTEOS), "formato_desconocido")
    resultado, eventos = _etapa(monkeypatch, tmp_path, [_cargado(10, 10, 0), rechazado, _cargado(4, 4, 0)])
    assert eventos == [("evento", "etapa_fin", "consumo", (14, 14, 0), "parcial")]
    assert resultado == "parcial"


# --- frontera_fuera_maestro: un contador del archivo, sin rama por fuente en corrida_batch ---------------------------
@pytest.mark.parametrize(("extra", "esperado"), [
    ({almacen.CONTADOR_FUERA_MAESTRO: 2}, [("evento", "frontera_fuera_maestro", {"fuente": "consumo", "cantidad": 2})]),
    ({}, []),
])
def test_frontera_fuera_maestro_sale_del_contador_del_desenlace(monkeypatch, tmp_path, extra, esperado):
    d = Desenlace("cargado", 1, {"leidas": 1, "cargadas": 1, "rechazadas": 0, "rechazadas_por_motivo": {}, **extra})
    monkeypatch.setattr(corrida_batch, "ingerir", lambda *a: d)
    obs = Obs()
    corrida_batch._cargar_archivo(cast(corrida_batch.ContextoCarga, None), obs, "consumo", "c.csv", tmp_path / "c.csv")
    assert [x for x in obs.llamadas if x[1] != "archivo_cargado"] == esperado
