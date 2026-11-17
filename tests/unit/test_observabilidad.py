import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from cauce.bordes import observabilidad
from cauce.bordes.observabilidad import ClaveNoDeclarada, ErrorInvariante, Observabilidad

TZ = timezone(timedelta(hours=-5))


class RelojFalso:
    def __init__(self):
        self.t = 0.0

    def ahora(self):
        return datetime(2026, 6, 1, 8, 0, 0, tzinfo=TZ) + timedelta(seconds=self.t)

    def monotonico(self):
        return self.t

    def avanzar(self, s):
        self.t += s


@pytest.fixture
def reloj():
    return RelojFalso()


@pytest.fixture
def abrir(tmp_path, reloj):
    abiertas = []

    def _abrir(modo="test", clasificacion=None):
        o = Observabilidad.abrir(modo, "corrida-1", tmp_path / "pipeline.log", clasificacion or {}, reloj)
        abiertas.append(o)
        return o

    yield _abrir
    for o in abiertas:
        o.liberar()


def lineas(tmp_path):
    texto = (tmp_path / "pipeline.log").read_text()
    return [json.loads(ln) for ln in texto.splitlines()]


def test_linea_json_con_campos_base_y_sin_mensaje(abrir, tmp_path):
    o = abrir()
    o.evento("archivo_omitido", fuente="consumos", archivo="consumos_2026-06.csv")
    (ln,) = [x for x in lineas(tmp_path) if x["evento"] == "archivo_omitido"]
    assert ln["nivel"] == "INFO" and ln["corrida_id"] == "corrida-1" and ln["modo"] == "test"
    assert re.fullmatch(r"2026-06-01T08:00:00(\.\d+)?-05:00", ln["ts"])
    assert "mensaje" not in ln and "message" not in ln
    assert ln["fuente"] == "consumos"


def test_evento_fuera_de_catalogo_falla(abrir):
    with pytest.raises(KeyError):
        abrir().evento("evento_inventado")


def test_clave_no_declarada_falla_en_test_y_se_redacta_en_runtime(abrir, tmp_path):
    with pytest.raises(ClaveNoDeclarada):
        abrir("test").evento("archivo_omitido", fuente="x", archivo="y", nit="900")
    o = abrir("run")
    o.evento("archivo_omitido", fuente="x", archivo="y", nit="900123456")
    ult = lineas(tmp_path)[-1]
    assert ult["nit"] == "[REDACTADO]"
    assert "900123456" not in (tmp_path / "pipeline.log").read_text()


def test_clave_clasificada_confidencial_se_redacta(abrir, tmp_path):
    o = abrir("run", clasificacion={"archivo": "Confidencial"})
    o.evento("archivo_omitido", fuente="consumos", archivo="secreto.csv")
    ult = lineas(tmp_path)[-1]
    assert ult["archivo"] == "[REDACTADO]" and ult["fuente"] == "consumos"


def test_no_hay_nivel_debug(abrir, tmp_path):
    o = abrir()
    o.evento("etapa_inicio", etapa="consumos")
    assert {ln["nivel"] for ln in lineas(tmp_path)} <= {"INFO", "WARNING", "ERROR"}


def test_excepcion_con_pii_en_el_mensaje_no_aparece(abrir, tmp_path, capsys):
    o = abrir()
    try:
        raise ValueError("fallo con juan.perez@example.org y +57 399 023 4567 y CC 1234567")
    except ValueError as e:
        o.error(e)
    log = (tmp_path / "pipeline.log").read_text()
    assert "juan.perez" not in log and "4567" not in log and "1234567" not in log
    err = capsys.readouterr().err
    assert "juan.perez" not in err
    (ln,) = [x for x in lineas(tmp_path) if x["evento"] == "error"]
    assert ln["nivel"] == "ERROR" and ln["error_tipo"] == "ValueError"
    assert ln["pila"][0][2] == "test_excepcion_con_pii_en_el_mensaje_no_aparece"
    assert isinstance(ln["pila"][0][1], int)


def test_error_incluye_sqlstate(abrir, tmp_path):
    class ErrPG(Exception):
        pgcode = "23505"

    try:
        raise ErrPG("detalle: Key (correo)=(a@b.test) already exists")
    except ErrPG as e:
        abrir().error(e)
    ln = [x for x in lineas(tmp_path) if x["evento"] == "error"][0]
    assert ln["sqlstate"] == "23505" and "a@b.test" not in json.dumps(ln)


def test_excepthook_no_imprime_el_mensaje_crudo(abrir, tmp_path, capsys):
    o = abrir()
    previo = sys.excepthook
    o.instalar_excepthook()
    try:
        assert sys.excepthook is not previo
        try:
            raise RuntimeError("secreto a@b.test")
        except RuntimeError as e:
            sys.excepthook(type(e), e, e.__traceback__)
    finally:
        o.liberar()
    assert sys.excepthook is previo
    assert "a@b.test" not in capsys.readouterr().err
    assert "a@b.test" not in (tmp_path / "pipeline.log").read_text()


def test_terceros_van_como_libreria_externa_sin_mensaje(abrir, tmp_path):
    abrir()
    logging.getLogger("kafka.conn").error("fallo a@b.test 399 023 4567")
    logging.getLogger("otra_lib").warning("aviso juan@x.test")
    logging.getLogger("otra_lib").info("ignorado")
    ls = [x for x in lineas(tmp_path) if x["evento"] == "libreria_externa"]
    assert [ln["logger"] for ln in ls] == ["kafka.conn", "otra_lib"]
    assert all(ln["nivel"] == "WARNING" for ln in ls)
    texto = (tmp_path / "pipeline.log").read_text()
    assert "a@b.test" not in texto and "juan@x.test" not in texto


def test_kafka_warning_no_pasa(abrir, tmp_path):
    abrir()
    logging.getLogger("kafka").warning("ruido")
    assert not [x for x in lineas(tmp_path) if x["evento"] == "libreria_externa"]


def test_hallazgo_en_linea_se_enmascara_avisa_y_cuenta(abrir, tmp_path, reloj):
    o = abrir("run")
    o.evento("archivo_rechazado", fuente="clientes", archivo="juan@x.test.csv", motivo="encabezado")
    texto = (tmp_path / "pipeline.log").read_text()
    assert "juan@x.test" not in texto and "[EMAIL]" in texto
    pf = [x for x in lineas(tmp_path) if x["evento"] == "pii_filtrada"]
    assert len(pf) == 1 and pf[0]["nivel"] == "WARNING"
    assert pf[0]["evento_origen"] == "archivo_rechazado" and pf[0]["tipo"] == "correo"
    assert o.cerrar("exito").a_dict()["pii_filtrada_en_logs"] == 1


def test_etapa_mide_y_emite_inicio_fin(abrir, tmp_path, reloj):
    o = abrir()
    with o.etapa("consumos"):
        reloj.avanzar(1.23456)
    m = o.cerrar("exito").a_dict()
    assert m["duracion_s_por_etapa"]["consumos"] == 1.235
    ev = [(ln["evento"], ln.get("etapa")) for ln in lineas(tmp_path)]
    assert ("etapa_inicio", "consumos") in ev and ("etapa_fin", "consumos") in ev


def test_etapa_desconocida_falla(abrir):
    with pytest.raises(ValueError):
        with abrir().etapa("inventada"):
            pass


def test_metricas_esquema_completo_en_cero(abrir, reloj):
    o = abrir()
    reloj.avanzar(2)
    m = o.cerrar("exito").a_dict()
    assert set(m) == {"corrida_id", "ts_inicio", "ts_fin", "resultado", "filas_leidas", "filas_cargadas",
                      "filas_rechazadas_por_motivo", "por_fuente", "archivos", "duracion_s_por_etapa",
                      "duracion_s_total", "pii_filtrada_en_logs", "stream"}
    assert m["resultado"] == "exito" and m["corrida_id"] == "corrida-1"
    assert m["ts_inicio"].endswith("-05:00") and m["ts_fin"].endswith("-05:00")
    assert m["archivos"] == {"cargados": 0, "omitidos": 0, "rechazados": 0}
    assert set(m["duracion_s_por_etapa"]) == {"clasificacion", "fronteras", "clientes", "consumos",
                                              "facturacion", "salidas"}
    assert m["duracion_s_total"] == 2.0 and m["filas_leidas"] == 0
    assert m["stream"] == {"eventos_leidos": 0, "invalidos": 0, "invalidos_por_motivo": {}, "duplicados": 0,
                           "alertas_por_tipo": {}, "duracion_s": 0.0}
    json.loads(o.cerrar("exito").a_json())


def test_metricas_se_acumulan_desde_los_eventos_e_invariante(abrir):
    o = abrir()
    o.evento("archivo_cargado", fuente="consumos", archivo="c1.csv", leidas=10, cargadas=7,
             rechazadas_por_motivo={"valor_negativo": 2, "fecha_invalida": 1})
    o.evento("archivo_cargado", fuente="consumos", archivo="c2.csv", leidas=5, cargadas=5,
             rechazadas_por_motivo={})
    o.evento("archivo_omitido", fuente="consumos", archivo="c0.csv")
    o.evento("archivo_rechazado", fuente="clientes", archivo="x.csv", motivo="encabezado_desconocido")
    m = o.cerrar("exito").a_dict()
    assert m["filas_leidas"] == 15 and m["filas_cargadas"] == 12
    assert m["filas_rechazadas_por_motivo"] == {"valor_negativo": 2, "fecha_invalida": 1}
    assert m["por_fuente"]["consumos"] == {"leidas": 15, "cargadas": 12, "rechazadas": 3}
    assert m["archivos"] == {"cargados": 2, "omitidos": 1, "rechazados": 1}
    assert m["filas_leidas"] == m["filas_cargadas"] + sum(m["filas_rechazadas_por_motivo"].values())


def test_archivo_cargado_que_rompe_invariante_falla(abrir):
    with pytest.raises(ErrorInvariante):
        abrir().evento("archivo_cargado", fuente="consumos", archivo="c.csv", leidas=10, cargadas=5,
                       rechazadas_por_motivo={"a": 1})


def test_stream_en_metricas(abrir):
    o = abrir()
    o.evento("stream_fin", eventos_leidos=100, invalidos=2, duplicados=3, alertas_por_tipo={"X": 4},
             duracion_s=1.5)
    assert o.cerrar("exito").a_dict()["stream"]["alertas_por_tipo"] == {"X": 4}


def test_catalogo_y_niveles_cerrados():
    assert set(observabilidad.NIVELES) == {"INFO", "WARNING", "ERROR"}
    assert {"pii_filtrada", "libreria_externa", "error", "corrida_inicio", "corrida_fin"} <= set(
        observabilidad.CATALOGO)
