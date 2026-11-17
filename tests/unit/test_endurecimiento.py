"""Controles de la infraestructura versionada (compose, pg_hba, requirements, secretos)."""
import re
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((RAIZ / "docker-compose.yml").read_text())


def test_compose_sin_minio_e_imagenes_por_version_y_digest():
    assert "minio" not in COMPOSE["services"]
    assert "minio" not in (RAIZ / "docker-compose.yml").read_text().lower().replace("sin minio", "")
    for nombre in ("postgres", "redpanda"):
        imagen = COMPOSE["services"][nombre]["image"]
        assert ":latest" not in imagen
        assert re.search(r":[\w.\-]+@sha256:[0-9a-f]{64}$", imagen), imagen
    assert COMPOSE["services"]["postgres"]["image"].startswith("postgres:16")


def test_puertos_solo_en_loopback_y_healthchecks():
    for nombre in ("postgres", "redpanda"):
        svc = COMPOSE["services"][nombre]
        assert "healthcheck" in svc
        assert svc["ports"] and all(p.startswith("127.0.0.1:") for p in svc["ports"])


def test_redpanda_sin_autocreacion_de_topicos():
    cmd = " ".join(COMPOSE["services"]["redpanda"]["command"])
    assert "auto_create_topics_enabled=false" in cmd


def test_app_en_profile_con_data_y_credenciales_solo_lectura():
    app = COMPOSE["services"]["app"]
    assert app["profiles"] == ["app"]
    assert "${CAUCE_DATA:-./data}:/app/data:ro" in app["volumes"]  # data/ o el temporal de CAUCE_DATA
    assert "./credenciales.yaml:/app/credenciales.yaml:ro" in app["volumes"]
    assert app["image"] != "cauce-app:latest"


def test_compose_sin_credenciales_literales():
    env = COMPOSE["services"]["postgres"]["environment"]
    for clave in ("POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert env[clave].startswith("${"), clave


def test_pg_hba_scram_sin_trust_ni_replication():
    reglas = [ln.split() for ln in (RAIZ / "postgres/pg_hba.conf").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
    assert reglas
    assert all(r[-1] == "scram-sha-256" for r in reglas)
    assert not any("replication" in r for r in reglas)
    assert "trust" not in " ".join(sum(reglas, []))


def _pines_con_hash(archivo: str) -> dict[str, str]:
    """Los pines `nombre==version` del archivo; falla si alguno no trae hash."""
    texto = (RAIZ / archivo).read_text()
    for b in re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", texto):
        if re.match(r"[A-Za-z0-9_.\-]+==", b):
            assert "--hash=sha256:" in b, b.splitlines()[0]
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==(\S+)", texto, re.M)}


def test_requirements_solo_lo_permitido_con_hashes():
    pines = _pines_con_hash("requirements.txt")
    assert {"psycopg2-binary", "kafka-python", "cryptography", "pyyaml", "pydantic"} <= set(pines)
    assert pines["pyyaml"] == "6.0.3"
    for prohibido in ("requests", "urllib3", "jinja2", "pandas", "pytest", "ruff"):
        assert prohibido not in pines


def test_requirements_dev_con_hashes_y_sin_repetir_runtime():
    dev = _pines_con_hash("requirements-dev.txt")
    assert {"pytest", "ruff"} <= set(dev)
    assert not set(dev) & set(_pines_con_hash("requirements.txt"))


def test_dockerfile_instala_con_require_hashes():
    df = (RAIZ / "Dockerfile").read_text()
    assert "--require-hashes -r requirements.txt -r requirements-dev.txt" in df
    assert re.search(r"^FROM python:3\.13-slim@sha256:[0-9a-f]{64}", df, re.M)


def test_makefile_instala_con_require_hashes():
    assert "--require-hashes -r requirements.txt -r requirements-dev.txt" in (RAIZ / "Makefile").read_text()


def test_el_codigo_no_trae_credenciales_literales():
    patron = re.compile(r"""(password|passwd|secret|llave_maestra)\s*[:=]\s*["'][^"'$\s{][^"']{5,}["']""", re.I)
    for f in list((RAIZ / "src").rglob("*.py")) + [RAIZ / "docker-compose.yml", RAIZ / "Makefile"]:
        assert not patron.search(f.read_text()), f


def test_solo_credenciales_monta_credenciales_en_escritura():
    for nombre, svc in COMPOSE["services"].items():
        rw = [v for v in svc.get("volumes", []) if "credenciales.yaml" in v and v.endswith(":rw")]
        assert bool(rw) == (nombre == "credenciales"), nombre
