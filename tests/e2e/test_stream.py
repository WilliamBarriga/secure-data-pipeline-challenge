"""e2e: `python -m cauce run` + `producir` + `stream` como procesos contra cauce_test y Redpanda real, con un topico
efimero `cauce-test-<uuid>` (KAFKA_TOPIC) creado por `producir` y borrado por el test. Oraculo: las Alertas de la
Verdad plantada, metrics.json conserva las claves batch, la Corrida del stream queda en la Bitacora y el canario de PII
de clientes.csv no aparece en logs, out/, stdout ni en ninguna tabla tras `run` + `stream` juntos, y
`salidas.auditar` (lo que corre `make auditar-salidas`) no encuentra nada en ese out/ y logs/."""
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from kafka import KafkaAdminClient

from apoyo import KAFKA_BOOTSTRAP as BOOTSTRAP
from apoyo import MAESTROS, consultar, copiar, correr, valores_pii_clientes, volcado_superusuario
from cauce.bordes import salidas


@pytest.fixture()
def topico():
    nombre = f"cauce-test-{uuid.uuid4().hex}"
    yield nombre
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        if nombre in admin.list_topics():
            admin.delete_topics([nombre])
    finally:
        admin.close()


@pytest.fixture()
def datos(tmp_path_factory, sinteticos):
    return copiar(sinteticos, tmp_path_factory.mktemp("e2e_stream") / "data", *MAESTROS, "eventos/")


def test_run_mas_stream_alertas_plantadas_metrics_bitacora_y_canario(maestros_limpios, datos, topico, sinteticos):
    cfg, out = maestros_limpios, Path(os.environ["CAUCE_OUT"])
    kafka = {"KAFKA_TOPIC": topico, "KAFKA_BOOTSTRAP": BOOTSTRAP}
    r_run = correr(datos)
    assert r_run.returncode == 0, r_run.stderr
    batch = json.loads((out / "metrics.json").read_text())

    t0 = time.monotonic()
    pasos = [correr(datos, "producir", extra=kafka), correr(datos, "stream", extra=kafka)]
    assert time.monotonic() - t0 < 300  # tope del contrato (brief §9)
    for r in pasos:
        assert r.returncode == 0, r.stderr
    primero = (out / "alertas.json").read_bytes()
    assert json.loads(primero) == sinteticos.verdad["alertas"]
    plantado = sinteticos.verdad["stream"]

    m = json.loads((out / "metrics.json").read_text())
    assert {k: v for k, v in m.items() if k != "stream"} == {k: v for k, v in batch.items() if k != "stream"}
    assert m["stream"]["eventos_leidos"] == plantado["eventos"] and m["stream"]["resultado"] == "exito"
    corrida_stream = m["stream"]["corrida_id"]
    assert consultar(cfg, "select evento, resultado from audit.bitacora_corrida where corrida_id = %s "
                          "and evento like 'corrida_%%' order by evento_id", corrida_stream) == [
        ("corrida_inicio", None), ("corrida_fin", "ok")]

    # el topico crecio (producer otra vez): mismo alertas.json; el stream relee el topico desde earliest y en la
    # segunda copia cada linea repite un evento_id ya visto, ademas de los reenvios plantados en la primera
    for paso in ("producir", "stream"):
        r = correr(datos, paso, extra=kafka)
        assert r.returncode == 0, r.stderr
    assert (out / "alertas.json").read_bytes() == primero
    assert json.loads((out / "metrics.json").read_text())["stream"]["duplicados"] == (
        plantado["duplicados"] + plantado["eventos"])

    # canario de PII sobre run + stream juntos: valores conocidos de clientes.csv y el detector estricto
    valores = valores_pii_clientes(datos)
    assert len(valores) > 100  # el oraculo realmente tiene con que comparar
    log = Path(os.environ["CAUCE_LOG"])
    superficies = {"tablas": volcado_superusuario(cfg), "log": log.read_text(encoding="utf-8"),
                   "out": "\n".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file()),
                   "stdout": "".join(r.stdout + r.stderr for r in (r_run, *pasos))}
    assert {n: sum(1 for v in valores if v in t) for n, t in superficies.items()} == {
        "tablas": 0, "log": 0, "out": 0, "stdout": 0}
    assert {p.name for p in out.iterdir()} >= {"alertas.json", "metrics.json", "consumo_mensual.csv", "dq_report.json"}
    assert salidas.auditar([out, log.parent]) == {}


def test_stream_sin_topico_falla_con_mensaje_y_bitacora(cauce_test, datos, topico):
    r = correr(datos, "stream", extra={"KAFKA_TOPIC": topico, "KAFKA_BOOTSTRAP": BOOTSTRAP})
    assert r.returncode != 0
    assert "make up" in r.stderr
    assert consultar(cauce_test, "select comando, resultado, codigo_error from audit.bitacora_corrida "
                               "order by evento_id desc limit 1") == [
        ("stream", "fallo", "infraestructura_no_disponible")]
