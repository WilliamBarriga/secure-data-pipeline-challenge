"""Smoke del adapter Kafka de FuenteEventos contra Redpanda real, con topico efimero `cauce-test-<uuid>` creado y
borrado por el test: consume desde earliest hasta el high watermark de Kafka y termina solo."""
import time
import uuid

import pytest
from kafka import KafkaAdminClient, KafkaProducer

from apoyo import KAFKA_BOOTSTRAP as BOOTSTRAP
from cauce.bordes.fuente_eventos import Corte, FuenteKafka, KafkaNoDisponible, asegurar_topico


@pytest.fixture()
def topico():
    nombre = f"cauce-test-{uuid.uuid4().hex}"
    try:
        asegurar_topico(BOOTSTRAP, nombre, particiones=3)
    except KafkaNoDisponible as e:
        pytest.fail(f"{e}. Los tests de stream necesitan Redpanda: corre `make up` primero.", pytrace=False)
    yield nombre
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.delete_topics([nombre])
    finally:
        admin.close()


def producir(topico: str, valores: list[bytes]) -> None:
    p = KafkaProducer(bootstrap_servers=BOOTSTRAP)
    try:
        for i, v in enumerate(valores):
            p.send(topico, key=f"F{i % 5}".encode(), value=v)
        p.flush()
    finally:
        p.close()


def test_consume_todo_hasta_el_high_watermark_y_termina(topico):
    valores = [f'{{"n": {i}}}'.encode() for i in range(200)]
    producir(topico, valores)
    fuente = FuenteKafka(BOOTSTRAP, topico)
    t0 = time.monotonic()
    leidos = list(fuente.eventos())
    assert sorted(leidos) == sorted(valores)
    assert fuente.corte is None and time.monotonic() - t0 < 10  # sin esperar la inactividad: para en el high watermark


def test_topico_vacio_termina_sin_eventos(topico):
    fuente = FuenteKafka(BOOTSTRAP, topico)
    assert list(fuente.eventos()) == [] and fuente.corte is None


def test_releer_da_lo_mismo_sin_consumer_group(topico):
    producir(topico, [b"a", b"b", b"c"])
    assert sorted(FuenteKafka(BOOTSTRAP, topico).eventos()) == sorted(FuenteKafka(BOOTSTRAP, topico).eventos())


def test_lo_producido_despues_del_watermark_no_se_lee(topico):
    producir(topico, [b"antes"])
    eventos = FuenteKafka(BOOTSTRAP, topico).eventos()
    primero = next(eventos)  # el high watermark de Kafka se toma al empezar a leer
    producir(topico, [b"despues"])
    assert [primero, *eventos] == [b"antes"]


def test_el_tope_corta_la_lectura_y_lo_informa(topico):
    producir(topico, [b"x"] * 50)
    fuente = FuenteKafka(BOOTSTRAP, topico, tope_s=0)
    assert list(fuente.eventos()) == [] and fuente.corte is Corte.TOPE


def test_asegurar_topico_es_idempotente(topico):
    asegurar_topico(BOOTSTRAP, topico)  # ya existe: no falla


def test_topico_inexistente_falla_con_codigo():
    with pytest.raises(KafkaNoDisponible) as e:
        list(FuenteKafka(BOOTSTRAP, f"cauce-test-{uuid.uuid4().hex}").eventos())
    assert e.value.codigo_error == "infraestructura_no_disponible"


def test_broker_inalcanzable_falla_con_codigo():
    with pytest.raises(KafkaNoDisponible):
        list(FuenteKafka("127.0.0.1:1", "mediciones.raw").eventos())
