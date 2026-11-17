"""Smoke e2e: `python -m cauce run` como proceso, contra la base de pruebas, termina en 0 (Corrida vacia)."""
import os
from pathlib import Path

from apoyo import RAIZ, correr, log, metrics

# sin data/: Corrida vacia (la carga real de los maestros se prueba en test_maestros.py)
SIN_DATOS = RAIZ / "tests" / "no_existe"


def test_run_vacio_termina_en_cero_dos_veces(cauce_test):
    for _ in range(2):
        r = correr(SIN_DATOS)
        assert r.returncode == 0, r.stderr


def test_run_deja_metrics_y_log_sin_pii(cauce_test):
    r = correr(SIN_DATOS)
    assert r.returncode == 0, r.stderr
    m = metrics()
    assert m["resultado"] == "exito" and m["corrida"]["filas_leidas"] == 0 and m["ts_inicio"].endswith("-05:00")
    lineas = log()
    assert {"corrida_inicio", "corrida_fin"} <= {x["evento"] for x in lineas}
    assert all("mensaje" not in x for x in lineas)


def test_run_fallido_deja_metrics_fallo_y_log_error(cauce_test):
    r = correr(SIN_DATOS, extra={"PG_HOST": "127.0.0.1", "PG_PORT": "1"})
    assert r.returncode != 0
    m = metrics()
    assert m["resultado"] == "fallo"
    assert cauce_test.ingesta.password not in Path(os.environ["CAUCE_LOG"]).read_text()


def test_run_aborta_con_clasificacion_incompleta_sin_valores(cauce_test, tmp_path):
    malo = tmp_path / "data_classification.yaml"
    malo.write_text((RAIZ / "data_classification.yaml").read_text(encoding="utf-8")
                    .replace("cliente.nit:\n    nivel: Restringido-PII", "cliente.nit:\n    nivel: SECRETO-PRUEBA", 1),
                    encoding="utf-8")
    r = correr(SIN_DATOS, extra={"CAUCE_CLASIFICACION": str(malo)})
    assert r.returncode != 0
    assert "clasificacion_incompleta" in r.stderr
    log = Path(os.environ["CAUCE_LOG"]).read_text()
    assert "SECRETO-PRUEBA" not in r.stderr + r.stdout + log
    assert "clasificacion_incompleta" in log
    assert metrics()["resultado"] == "fallo"


def test_run_con_columna_sin_clasificar_nombra_la_columna(cauce_test, tmp_path):
    malo = tmp_path / "data_classification.yaml"
    malo.write_text((RAIZ / "data_classification.yaml").read_text(encoding="utf-8")
                    .replace("    - raw.archivo.sha256\n", "", 1), encoding="utf-8")
    r = correr(SIN_DATOS, extra={"CAUCE_CLASIFICACION": str(malo)})
    assert r.returncode != 0
    assert "raw.archivo.sha256" in r.stderr
    assert "raw.archivo.sha256" in Path(os.environ["CAUCE_LOG"]).read_text()


def test_clasificacion_incompleta_queda_en_la_bitacora_como_fallo(cauce_test, tmp_path):
    from cauce.bordes import almacen
    malo = tmp_path / "data_classification.yaml"
    malo.write_text((RAIZ / "data_classification.yaml").read_text(encoding="utf-8")
                    .replace("    - raw.archivo.sha256\n", "", 1), encoding="utf-8")
    r = correr(SIN_DATOS, extra={"CAUCE_CLASIFICACION": str(malo)})
    assert r.returncode != 0
    con = almacen.conectar(cauce_test, cauce_test.auditor, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("select resultado, codigo_error from audit.bitacora_corrida order by evento_id desc limit 1")
            assert cur.fetchone() == ("fallo", "clasificacion_incompleta")
    finally:
        con.close()


def test_run_con_infra_inalcanzable_falla_con_mensaje_claro(cauce_test):
    r = correr(SIN_DATOS, extra={"PG_HOST": "127.0.0.1", "PG_PORT": "1"})
    assert r.returncode != 0
    assert "make up" in r.stderr
    assert cauce_test.ingesta.password not in r.stderr + r.stdout


def test_cada_run_queda_en_la_bitacora_con_inicio_y_fin(cauce_test):
    from cauce.bordes import almacen, bitacora
    r = correr(SIN_DATOS)
    assert r.returncode == 0, r.stderr
    con = almacen.conectar(cauce_test, cauce_test.auditor, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("select evento, resultado, comando, usuario_db, ejecutor <> '', version_codigo <> '' "
                        "from audit.bitacora_corrida where corrida_id = (select corrida_id from "
                        "audit.bitacora_corrida where evento = 'corrida_fin' order by evento_id desc limit 1) "
                        "order by evento_id")
            assert cur.fetchall() == [("corrida_inicio", None, "run", "ingesta", True, True),
                                      ("corrida_fin", "ok", "run", "ingesta", True, True)]
        assert bitacora.verificar_cadena(con) is None
    finally:
        con.close()
