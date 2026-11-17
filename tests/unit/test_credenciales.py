"""Seam: cauce.credenciales.rellenar_credenciales (puro) y cauce.secretos.validar_password."""
from pathlib import Path

import pytest
import yaml

import cauce
from cauce.arranque import credenciales, secretos

MANIFIESTO = """\
# comentario inicial que debe sobrevivir
postgres:
  host: localhost
  superusuario:
    usuario: "cauce_admin"
    password: "TODO"      # comentario en la misma linea
  roles:
    ingesta:
      usuario: "ingesta"
      password: "TODO"
    analista:
      usuario: "analista"
      password: "ya-definida-por-el-humano-0123456789"
"""


def _passwords(texto):
    d = yaml.safe_load(texto)["postgres"]
    return [d["superusuario"]["password"],
            d["roles"]["ingesta"]["password"],
            d["roles"]["analista"]["password"]]


def test_reemplaza_solo_los_TODO_y_preserva_comentarios():
    nuevo = credenciales.rellenar_credenciales(MANIFIESTO, rotar=False)
    su, ing, ana = _passwords(nuevo)
    assert len(su) >= 24 and len(ing) >= 24
    assert su != ing
    assert ana == "ya-definida-por-el-humano-0123456789"
    assert "# comentario inicial que debe sobrevivir" in nuevo
    assert "# comentario en la misma linea" in nuevo


def test_rotar_reescribe_todas():
    nuevo = credenciales.rellenar_credenciales(MANIFIESTO, rotar=True)
    assert _passwords(nuevo)[2] != "ya-definida-por-el-humano-0123456789"
    assert all(len(p) >= 24 for p in _passwords(nuevo))


def test_idempotente_sin_TODO():
    una = credenciales.rellenar_credenciales(MANIFIESTO, rotar=False)
    dos = credenciales.rellenar_credenciales(una, rotar=False)
    assert una == dos


def test_no_toca_usuarios_ni_otras_claves():
    nuevo = credenciales.rellenar_credenciales(MANIFIESTO, rotar=True)
    d = yaml.safe_load(nuevo)["postgres"]
    assert d["superusuario"]["usuario"] == "cauce_admin"
    assert d["host"] == "localhost"


@pytest.mark.parametrize("debil", ["TODO", "cambiame", "postgres", "minioadmin", "corta"])
def test_rechaza_passwords_debiles_nombrando_la_clave_no_el_valor(debil):
    with pytest.raises(secretos.CredencialDebil) as exc:
        secretos.validar_password("roles.ingesta", debil)
    assert "roles.ingesta" in str(exc.value)
    assert debil not in str(exc.value).replace("TODO", "")


def test_acepta_password_generada():
    secretos.validar_password("k", secretos.generar_password())


def test_el_hint_de_rotar_usa_la_sintaxis_del_makefile():
    with pytest.raises(secretos.CredencialDebil) as e:
        secretos.validar_password("postgres.x", "TODO")
    assert "make credenciales" in str(e.value)


def test_el_codigo_no_referencia_el_secreto_heredado():
    """Guardrail: src/cauce/ nunca lee config/pipeline.yaml (ni lo nombra como ruta)."""
    src = Path(cauce.__file__).parent
    # `logs/pipeline.log` es otra cosa; lo prohibido es el yaml heredado
    ofensores = [f.name for f in src.rglob("*.py") if "pipeline.yaml" in f.read_text(encoding="utf-8")]
    assert ofensores == []


# --- plantilla: `make up` genera credenciales.yaml cuando falta --------------

def test_plantilla_es_anonima_y_carga_tras_rellenar():
    txt = credenciales.plantilla().lower()
    for prohibido in ("candidato", "evaluador", "entrega", "zip", "verificar", "@", "horas"):
        assert prohibido not in txt
    nuevo = credenciales.rellenar_credenciales(credenciales.plantilla(), rotar=False)
    assert "TODO" not in nuevo
    assert len(_passwords(nuevo)[0]) >= 24


def test_cli_crea_si_falta_o_vacio_y_no_pisa_si_existe(tmp_path, monkeypatch, capsys):
    from cauce import cli
    monkeypatch.setenv("CAUCE_RAIZ", str(tmp_path))
    ruta = tmp_path / "credenciales.yaml"
    assert cli.main(["credenciales"]) == 0  # falta
    primero = ruta.read_text()
    assert "TODO" not in primero and len(_passwords(primero)[0]) >= 24
    assert cli.main(["credenciales"]) == 0  # existe: no cambia
    assert ruta.read_text() == primero
    ruta.write_text("")  # archivo vacio (lo deja `make up` antes del bind mount)
    assert cli.main(["credenciales"]) == 0
    assert "TODO" not in ruta.read_text() and ruta.read_text() != primero
    capsys.readouterr()
