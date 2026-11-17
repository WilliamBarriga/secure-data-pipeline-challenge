"""`python -m cauce producir` por el CLI: el producer tiene cota de tiempo para que `make stream` termine < 300 s."""
import time

from cauce import cli
from cauce.bordes import fuente_eventos


def test_producer_colgado_se_corta_por_timeout_y_falla(tmp_path, monkeypatch, capsys):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "producer.py").write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setenv("CAUCE_RAIZ", str(tmp_path))
    monkeypatch.setattr(fuente_eventos, "asegurar_topico", lambda bootstrap, topico: False)
    monkeypatch.setattr(cli, "TIMEOUT_PRODUCER_S", 0.5)
    t0 = time.monotonic()
    assert cli.main(["producir"]) == 1
    assert time.monotonic() - t0 < 10
    assert "timeout" in capsys.readouterr().err
