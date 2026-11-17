import json

import pytest

from cauce import cli
from cauce.bordes import salidas
from cauce.bordes.salidas import PiiEnSalida, escribir_atomico
from cauce.nucleo import pii


def test_escribe_y_reemplaza_sin_dejar_tmp(tmp_path):
    escribir_atomico("a.json", '{"x": 1}', tmp_path)
    escribir_atomico("a.json", '{"x": 2}', tmp_path)
    assert (tmp_path / "a.json").read_text() == '{"x": 2}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]


@pytest.mark.parametrize("contenido", ["cliente juan@x.test", "tel +57 399 023 4567", "CC 1234567"])
def test_rechaza_contenido_con_pii_sin_escribir(tmp_path, contenido):
    with pytest.raises(PiiEnSalida) as e:
        escribir_atomico("metrics.json", contenido, tmp_path)
    assert e.value.codigo_error == "pii_en_salida"
    assert contenido not in str(e.value)
    assert list(tmp_path.iterdir()) == []


def test_pii_no_pisa_la_salida_anterior(tmp_path):
    escribir_atomico("m.json", "{}", tmp_path)
    with pytest.raises(PiiEnSalida):
        escribir_atomico("m.json", "a@b.test", tmp_path)
    assert (tmp_path / "m.json").read_text() == "{}"


def test_frontera_id_no_es_pii(tmp_path):
    escribir_atomico("x.csv", "CO0000000013,1\n", tmp_path)


def test_rechaza_nombres_con_ruta(tmp_path):
    with pytest.raises(ValueError):
        escribir_atomico("../fuera.json", "{}", tmp_path)


def test_directorio_por_entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUCE_OUT", str(tmp_path / "o"))
    escribir_atomico("a.json", "{}")
    assert (tmp_path / "o" / "a.json").exists()
    assert salidas.directorio_salidas() == tmp_path / "o"


# --- auditar: `make auditar-salidas` ---------------------------------------------------------
def test_auditar_marca_solo_los_archivos_con_pii_y_sus_tipos(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "logs" / "viejo").mkdir(parents=True)
    (tmp_path / "out" / "ok.csv").write_text("CO0000000013,1\n")
    (tmp_path / "out" / "dq.json").write_text('{"x": "juan@x.test"}')
    (tmp_path / "logs" / "viejo" / "p.log").write_text('{"m": "tel +57 399 023 4567"}\n')
    (tmp_path / "logs" / "enmascarado.log").write_text('{"m": "[EMAIL] [CELULAR] [CEDULA]"}\n')
    hallazgos = salidas.auditar([tmp_path / "out", tmp_path / "logs"])
    assert hallazgos == {tmp_path / "out" / "dq.json": (pii.TipoPII.CORREO,),
                         tmp_path / "logs" / "viejo" / "p.log": (pii.TipoPII.CELULAR,)}


def test_auditar_lee_bytes_invalidos_sin_caerse(tmp_path):
    (tmp_path / "x.log").write_bytes(b"\xff\xfe CC 1234567")
    assert salidas.auditar([tmp_path]) == {tmp_path / "x.log": (pii.TipoPII.CEDULA,)}


def test_auditar_un_directorio_faltante_falla(tmp_path):
    with pytest.raises(FileNotFoundError):
        salidas.auditar([tmp_path / "out"])


def test_cli_auditar_salidas_limpio_sale_0(tmp_path, capsys):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.json").write_text("{}")
    assert cli.main(["auditar-salidas", str(tmp_path / "out")]) == 0
    assert "sin PII" in capsys.readouterr().out


def test_cli_auditar_salidas_con_pii_nombra_archivo_y_tipo_nunca_el_valor(tmp_path, capsys):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "p.log").write_text("juan@x.test\n")
    assert cli.main(["auditar-salidas", str(tmp_path / "logs")]) == 1
    texto = "".join(capsys.readouterr())
    assert "p.log" in texto and "correo" in texto and "juan@x.test" not in texto


def test_cli_auditar_salidas_sin_directorio_pide_correr_el_pipeline(tmp_path, capsys):
    assert cli.main(["auditar-salidas", str(tmp_path / "out")]) == 2
    assert "make run" in capsys.readouterr().err


def test_reemplazar_clave_conserva_las_demas_y_tolera_un_archivo_previo_ilegible(tmp_path):
    escribir_atomico("metrics.json", '{"batch": 1, "stream": {"viejo": 1}}', tmp_path)
    salidas.reemplazar_clave("metrics.json", "stream", {"nuevo": 2}, tmp_path)
    assert json.loads((tmp_path / "metrics.json").read_text()) == {"batch": 1, "stream": {"nuevo": 2}}
    escribir_atomico("metrics.json", "{roto", tmp_path)
    salidas.reemplazar_clave("metrics.json", "stream", {"nuevo": 3}, tmp_path)
    assert json.loads((tmp_path / "metrics.json").read_text()) == {"stream": {"nuevo": 3}}
