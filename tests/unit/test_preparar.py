"""Seam: cauce.preparar.preparar(credenciales, env) -> .env 0600."""
import os
import stat

import pytest

from cauce.arranque import preparar, secretos

PW = secretos.generar_password


def _credenciales(tmp_path, **over):
    pw = {"su": PW(), "ing": PW(), "ana": PW(), "aud": PW()}
    pw.update(over)
    ruta = tmp_path / "credenciales.yaml"
    ruta.write_text(f"""
postgres:
  host: localhost
  port: 5432
  database: cauce_datos
  superusuario: {{usuario: "cauce_admin", password: "{pw['su']}"}}
  roles:
    ingesta: {{usuario: "ingesta", password: "{pw['ing']}"}}
    analista: {{usuario: "analista", password: "{pw['ana']}"}}
    auditor: {{usuario: "auditor", password: "{pw['aud']}"}}
""")
    return ruta, pw


def _leer_env(ruta):
    return dict(ln.split("=", 1) for ln in ruta.read_text().splitlines() if ln and not ln.startswith("#"))


def test_renderiza_env_0600_con_kek_y_kbidx(tmp_path):
    ent, pw = _credenciales(tmp_path)
    env = tmp_path / "estado" / ".env"
    preparar.preparar(ent, env)
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    d = _leer_env(env)
    assert d["PG_SUPERUSER"] == "cauce_admin" and d["PG_SUPERPASSWORD"] == pw["su"]
    assert d["INGESTA_PASSWORD"] == pw["ing"]
    assert d["PG_DATABASE"] == "cauce_datos" and d["PG_PORT"] == "5432"
    assert len(d["CAUCE_KEK"]) >= 43 and len(d["CAUCE_K_BIDX"]) >= 43
    assert d["CAUCE_KEK"] != d["CAUCE_K_BIDX"]


def test_segundo_preparar_conserva_kek_y_kbidx(tmp_path):
    ent, _ = _credenciales(tmp_path)
    env = tmp_path / ".env"
    preparar.preparar(ent, env)
    antes = _leer_env(env)
    preparar.preparar(ent, env)
    despues = _leer_env(env)
    assert antes["CAUCE_KEK"] == despues["CAUCE_KEK"]
    assert antes["CAUCE_K_BIDX"] == despues["CAUCE_K_BIDX"]


@pytest.mark.parametrize("valor", ["TODO", "cambiame", "postgres", "minioadmin", "corta"])
def test_falla_nombrando_la_clave_nunca_el_valor(tmp_path, valor):
    ent, _ = _credenciales(tmp_path, ing=valor)
    env = tmp_path / ".env"
    with pytest.raises(secretos.CredencialDebil) as exc:
        preparar.preparar(ent, env)
    assert "ingesta" in str(exc.value)
    assert valor not in str(exc.value).replace("postgres.", "").replace("TODO", "")
    assert not env.exists()


def test_rechaza_passwords_repetidas_entre_roles(tmp_path):
    p = PW()
    ent, _ = _credenciales(tmp_path, ing=p, ana=p)
    with pytest.raises(secretos.CredencialDebil):
        preparar.preparar(ent, tmp_path / ".env")
