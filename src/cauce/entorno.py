"""Rutas y Kafka del proceso, resueltos una sola vez desde un mapping de entorno (puro: no lee `os.environ`, no
toca disco). La cli lo arma con `os.environ` e inyecta a cada runner lo que necesita.

Rutas relativas a CAUCE_RAIZ (defecto: cwd). CAUCE_DATA, CAUCE_OUT, CAUCE_LOG y CAUCE_CLASIFICACION las pisan (tests).
Kafka: KAFKA_BOOTSTRAP (defecto localhost:9092; en el contenedor redpanda:29092) y KAFKA_TOPIC (defecto
`TOPICO`). Sin secretos: esos son de `config`."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# topico del stream (brief §6): `fuente_eventos.TOPICO` lo toma de aca
TOPICO = "mediciones.raw"
BOOTSTRAP = "localhost:9092"


@dataclass(frozen=True)
class Entorno:
    raiz: Path
    data: Path  # solo lectura: data/ es inmutable
    out: Path
    log: Path
    clasificacion: Path
    credenciales: Path
    env: Path  # estado/.env
    kafka_bootstrap: str
    kafka_topico: str


def resolver(environ: Mapping[str, str]) -> Entorno:
    raiz = Path(environ.get("CAUCE_RAIZ", "."))
    return Entorno(
        raiz=raiz,
        data=Path(environ.get("CAUCE_DATA") or raiz / "data"),
        out=Path(environ.get("CAUCE_OUT") or raiz / "out"),
        log=Path(environ.get("CAUCE_LOG") or raiz / "logs" / "pipeline.log"),
        clasificacion=Path(environ.get("CAUCE_CLASIFICACION") or raiz / "data_classification.yaml"),
        credenciales=raiz / "credenciales.yaml",
        env=raiz / "estado" / ".env",
        kafka_bootstrap=environ.get("KAFKA_BOOTSTRAP") or BOOTSTRAP,
        kafka_topico=environ.get("KAFKA_TOPIC") or TOPICO,
    )
