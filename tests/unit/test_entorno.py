"""`entorno.resolver`: rutas y Kafka desde un dict, con los defectos y la precedencia de siempre."""
from pathlib import Path

from cauce import entorno
from cauce.bordes import fuente_eventos


def test_defectos_relativos_al_cwd():
    e = entorno.resolver({})
    assert e == entorno.Entorno(
        raiz=Path("."), data=Path("data"), out=Path("out"), log=Path("logs/pipeline.log"),
        clasificacion=Path("data_classification.yaml"), credenciales=Path("credenciales.yaml"), env=Path("estado/.env"),
        kafka_bootstrap="localhost:9092", kafka_topico="mediciones.raw")


def test_cauce_raiz_mueve_todas_las_rutas():
    e = entorno.resolver({"CAUCE_RAIZ": "/r"})
    assert (e.data, e.out, e.log) == (Path("/r/data"), Path("/r/out"), Path("/r/logs/pipeline.log"))
    assert (e.clasificacion, e.credenciales, e.env) == (Path("/r/data_classification.yaml"),
                                                       Path("/r/credenciales.yaml"), Path("/r/estado/.env"))


def test_las_variables_especificas_ganan_a_cauce_raiz():
    e = entorno.resolver({"CAUCE_RAIZ": "/r", "CAUCE_DATA": "/d", "CAUCE_OUT": "/o", "CAUCE_LOG": "/l/t.log",
                          "CAUCE_CLASIFICACION": "/c.yaml", "KAFKA_BOOTSTRAP": "redpanda:29092",
                          "KAFKA_TOPIC": "otro"})
    assert (e.data, e.out, e.log, e.clasificacion) == (Path("/d"), Path("/o"), Path("/l/t.log"), Path("/c.yaml"))
    assert (e.credenciales, e.env) == (Path("/r/credenciales.yaml"), Path("/r/estado/.env"))  # sin override: CAUCE_RAIZ
    assert (e.kafka_bootstrap, e.kafka_topico) == ("redpanda:29092", "otro")


def test_vacio_es_como_ausente():
    e = entorno.resolver({"CAUCE_DATA": "", "CAUCE_OUT": "", "CAUCE_LOG": "", "CAUCE_CLASIFICACION": "",
                          "KAFKA_BOOTSTRAP": "", "KAFKA_TOPIC": ""})
    assert e == entorno.resolver({})


def test_el_topico_por_defecto_es_el_del_adapter():
    assert fuente_eventos.TOPICO == entorno.TOPICO

