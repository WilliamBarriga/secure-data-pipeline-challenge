"""`corrida_stream.correr_stream` contra cauce_test, alimentado por el adapter en memoria de FuenteEventos:
clasificacion fail-fast -> Bitacora -> Detector -> alertas.json -> metrics.json (solo `stream`) -> Bitacora fin."""
import json
import uuid
from pathlib import Path

import pytest

from apoyo import MAESTROS_FRONTERAS, NDJSON, RAIZ, consultar, copiar
from cauce.aplicacion import corrida_stream
from cauce.bordes.fuente_eventos import Corte, FuenteEnMemoria
from cauce.bordes.observabilidad import Observabilidad, RelojSistema
from cauce.nucleo.clasificacion import ClasificacionIncompleta

CLASIFICACION = RAIZ / "data_classification.yaml"
TABLAS_DATOS = ("raw.archivo", "raw.linea", "raw.lectura_consumo", "raw.cliente", "raw.factura",
                "core.dim_frontera", "core.dim_cliente", "core.fact_consumo_horario", "core.fact_factura",
                "quarantine.rechazo")


@pytest.fixture()
def datos(tmp_path_factory, sinteticos):
    return copiar(sinteticos, tmp_path_factory.mktemp("stream") / "data", *MAESTROS_FRONTERAS, "eventos/")


@pytest.fixture()
def ndjson(datos) -> bytes:
    return (datos / NDJSON).read_bytes()


@pytest.fixture()
def flujo(sinteticos) -> dict:
    """Lo que el stream sintetico planto: eventos, duplicados, invalidos y Alertas por tipo."""
    s = sinteticos.verdad["stream"]
    return {**s, "invalidos": sum(s["invalidos_por_motivo"].values()),
            "alertas_por_tipo": {"FRONTERA_SIN_REPORTE": s["caidas"], "CONSUMO_FUERA_DE_BANDA": s["fuera_de_banda"],
                                 "LECTURA_RETROACTIVA": s["retroactivas"]}}


@pytest.fixture()
def out(tmp_path, monkeypatch):
    d = tmp_path / "out"
    monkeypatch.setenv("CAUCE_OUT", str(d))
    return d


class Contada(FuenteEnMemoria):
    """Adapter en memoria que recuerda si alguien empezo a leer."""
    leida = False

    def eventos(self):
        self.leida = True
        return super().eventos()


class Cortada(Contada):
    """Adapter en memoria cuya lectura termina por la red de seguridad (como el tope de FuenteKafka)."""

    def eventos(self):
        self.corte = Corte.TOPE
        return super().eventos()


def correr_stream_memoria(cfg, datos, tmp_path, eventos: bytes | None = None, clasificacion: Path = CLASIFICACION,
           adapter: type[Contada] = Contada):
    corrida_id = uuid.uuid4()
    obs = Observabilidad.abrir("test", str(corrida_id), tmp_path / "pipeline.log", {}, RelojSistema())
    fuente = adapter.desde_ndjson((datos / NDJSON).read_bytes() if eventos is None else eventos)
    try:
        codigo = corrida_stream.correr_stream(cfg, obs, corrida_id, fuente, clasificacion, dir_datos=datos)
    finally:
        obs.liberar()
    return codigo, corrida_id, fuente


def bitacora(cfg, corrida_id) -> list[tuple]:
    return consultar(cfg, "select evento, comando, usuario_db, fuente, filas_leidas, filas_cargadas, "
                          "filas_cuarentena, filas_duplicadas, resultado from audit.bitacora_corrida "
                          "where corrida_id = %s order by evento_id", str(corrida_id))


def test_alertas_json_es_la_verdad_plantada_ordenada_con_offset(cauce_test, datos, out, tmp_path, sinteticos):
    codigo, _, _ = correr_stream_memoria(cauce_test, datos, tmp_path)
    assert codigo == 0
    alertas = json.loads((out / "alertas.json").read_text())
    assert alertas == sinteticos.verdad["alertas"]
    assert {a["tipo"] for a in alertas} == {"FRONTERA_SIN_REPORTE", "CONSUMO_FUERA_DE_BANDA", "LECTURA_RETROACTIVA"}
    assert all(set(a) == {"tipo", "frontera_id", "ts"} and a["ts"].endswith("-05:00") for a in alertas)
    claves = [(a["tipo"], a["frontera_id"], a["ts"]) for a in alertas]
    assert claves == sorted(set(claves))


def test_reejecutar_con_el_topico_crecido_da_el_mismo_archivo(cauce_test, datos, out, tmp_path, ndjson):
    correr_stream_memoria(cauce_test, datos, tmp_path)
    primero = (out / "alertas.json").read_bytes()
    correr_stream_memoria(cauce_test, datos, tmp_path, eventos=ndjson * 2)  # el producer corrio dos veces
    assert (out / "alertas.json").read_bytes() == primero


def test_metrics_solo_reemplaza_stream_y_conserva_las_claves_batch(cauce_test, datos, out, tmp_path, ndjson, flujo):
    out.mkdir()
    batch = {"corrida_id": "batch-1", "filas_leidas": 7, "duracion_s_por_etapa": {"consumos": 1.5},
             "stream": {"eventos_leidos": 0}}
    (out / "metrics.json").write_text(json.dumps(batch))
    correr_stream_memoria(cauce_test, datos, tmp_path, eventos=ndjson + b"{roto\n")
    m = json.loads((out / "metrics.json").read_text())
    assert {k: v for k, v in m.items() if k != "stream"} == {k: v for k, v in batch.items() if k != "stream"}
    s = m["stream"]
    assert (s["eventos_leidos"], s["invalidos"], s["duplicados"]) == (
        flujo["eventos"] + 1, flujo["invalidos"] + 1, flujo["duplicados"])
    assert s["invalidos_por_motivo"] == {**flujo["invalidos_por_motivo"], "json_invalido": 1}
    assert s["alertas_por_tipo"] == flujo["alertas_por_tipo"]
    assert s["resultado"] == "exito" and s["duracion_s"] >= 0


def test_sin_metrics_previo_escribe_solo_la_subclave_stream(cauce_test, datos, out, tmp_path):
    correr_stream_memoria(cauce_test, datos, tmp_path)
    assert set(json.loads((out / "metrics.json").read_text())) == {"stream"}


def test_la_corrida_del_stream_queda_en_la_bitacora_como_ingesta(cauce_test, datos, out, tmp_path, ndjson, flujo):
    eventos = ndjson + b"{roto\n" + ndjson.split(b"\n")[0] + b"\n"
    _, corrida_id, _ = correr_stream_memoria(cauce_test, datos, tmp_path, eventos=eventos)
    filas = bitacora(cauce_test, corrida_id)
    assert {f[1:3] for f in filas} == {("stream", "ingesta")}
    motivos = sorted({**flujo["invalidos_por_motivo"], "json_invalido": 1}.items())
    assert [f[0] for f in filas] == ["corrida_inicio", *["etapa_fin"] * (1 + len(motivos) + 3), "corrida_fin"]
    assert filas[-1][-1] == "ok"
    leidas, invalidas, duplicadas = flujo["eventos"] + 2, flujo["invalidos"] + 1, flujo["duplicados"] + 1
    assert [f[3:8] for f in filas[1:-1]] == [
        ("stream", leidas, leidas - invalidas - duplicadas, invalidas, duplicadas),  # leidas, validas, invalidas, dup.
        *((f"stream.descartado.{m}", n, 0, n, None) for m, n in motivos),
        *((f"stream.alerta.{tipo}", n, 0, 0, None)                    # Alertas emitidas: no son validas
          for tipo, n in sorted(flujo["alertas_por_tipo"].items())),
    ]


def test_lectura_cortada_por_la_red_de_seguridad_deja_la_corrida_parcial(cauce_test, datos, out, tmp_path):
    codigo, corrida_id, _ = correr_stream_memoria(cauce_test, datos, tmp_path, adapter=Cortada)
    filas = bitacora(cauce_test, corrida_id)
    assert codigo == 0 and filas[-1][0] == "corrida_fin" and filas[-1][-1] == "parcial"


def test_no_persiste_mediciones_alertas_ni_medidor(cauce_test, datos, out, tmp_path):
    contar = "select " + ", ".join(f"(select count(*) from {t})" for t in TABLAS_DATOS)
    antes = consultar(cauce_test, contar)
    correr_stream_memoria(cauce_test, datos, tmp_path)
    assert consultar(cauce_test, contar) == antes
    assert consultar(cauce_test, "select count(*) from information_schema.columns where column_name like %s "
                               "or table_name like %s", "%medidor%", "%alerta%") == [(0,)]


def test_clasificacion_incompleta_aborta_antes_de_leer_el_stream(cauce_test, datos, out, tmp_path):
    malo = tmp_path / "data_classification.yaml"
    malo.write_text(CLASIFICACION.read_text(encoding="utf-8").replace(
        "  stream.medidor.firmware:\n    nivel: Interno\n", "", 1), encoding="utf-8")
    with pytest.raises(ClasificacionIncompleta):
        correr_stream_memoria(cauce_test, datos, tmp_path, clasificacion=malo)
    assert not (out / "alertas.json").exists()
    filas = consultar(cauce_test, "select evento, resultado, codigo_error from audit.bitacora_corrida "
                                "order by evento_id desc limit 1")
    assert filas == [("corrida_fin", "fallo", "clasificacion_incompleta")]
    log = (tmp_path / "pipeline.log").read_text()
    assert "stream.medidor.firmware" in log


def test_el_detector_no_lee_si_la_clasificacion_falla(cauce_test, datos, out, tmp_path, monkeypatch):
    leidas = []
    monkeypatch.setattr(Contada, "eventos", lambda self: leidas.append(1) or iter(()))
    malo = tmp_path / "data_classification.yaml"
    malo.write_text(CLASIFICACION.read_text(encoding="utf-8").replace(
        "  stream.kwh:\n    nivel: Interno\n", "", 1), encoding="utf-8")
    with pytest.raises(ClasificacionIncompleta):
        correr_stream_memoria(cauce_test, datos, tmp_path, clasificacion=malo)
    assert leidas == []


def test_maestro_ausente_falla_con_codigo(cauce_test, out, tmp_path):
    with pytest.raises(corrida_stream.MaestroNoDisponible) as e:
        correr_stream_memoria(cauce_test, tmp_path / "vacio", tmp_path, eventos=b"")
    assert e.value.codigo_error == "configuracion_invalida"
