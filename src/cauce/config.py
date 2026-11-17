"""Config del proceso: estado/.env + variables de entorno (el entorno gana). Sin defaults de secretos."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_MODELO = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)


class ErrorConfig(RuntimeError):
    """Falta o es invalida una clave de configuracion. Nombra la clave, nunca el valor."""
    codigo_error = "configuracion_invalida"


class Usuario(BaseModel):
    model_config = _MODELO
    usuario: str
    password: str = Field(repr=False)


class Config(BaseModel):
    model_config = _MODELO
    host: str
    port: int
    database: str
    superusuario: Usuario
    ingesta: Usuario
    analista: Usuario
    auditor: Usuario
    kek: str = Field(repr=False)
    k_bidx: str = Field(repr=False)


def _req(env: Mapping[str, str], clave: str) -> str:
    valor = env.get(clave)
    if not valor:
        raise ErrorConfig(f"falta {clave}: corre `make up` (genera estado/.env)")
    return valor


def leer_env(ruta: Path) -> dict[str, str]:
    """`estado/.env` como dict; vacio si no existe. Lo escribe `arranque.preparar`, lo lee todo el proceso."""
    if not ruta.is_file():
        return {}
    pares = (ln.split("=", 1) for ln in ruta.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#") and "=" in ln)
    return {k: v for k, v in pares}


def cargar(ruta_env: Path, entorno: Mapping[str, str] | None = None) -> Config:
    env = {**leer_env(ruta_env), **(os.environ if entorno is None else entorno)}

    def usuario(prefijo: str) -> Usuario:
        return Usuario(usuario=_req(env, f"{prefijo}_USER"), password=_req(env, f"{prefijo}_PASSWORD"))

    try:
        return Config(
            host=env.get("PG_HOST", "localhost"),
            port=int(_req(env, "PG_PORT")),
            database=env.get("PG_DATABASE", "cauce_datos"),
            superusuario=Usuario(usuario=_req(env, "PG_SUPERUSER"), password=_req(env, "PG_SUPERPASSWORD")),
            ingesta=usuario("INGESTA"), analista=usuario("ANALISTA"), auditor=usuario("AUDITOR"),
            kek=_req(env, "CAUCE_KEK"), k_bidx=_req(env, "CAUCE_K_BIDX"),
        )
    except ValueError:
        raise ErrorConfig("configuracion invalida: revisa estado/.env") from None
