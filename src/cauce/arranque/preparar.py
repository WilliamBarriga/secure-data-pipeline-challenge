"""`preparar`: credenciales.yaml -> estado/.env (0600). Genera KEK y K_bidx solo si faltan."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cauce.arranque import credenciales as _credenciales
from cauce.arranque import secretos
from cauce.config import leer_env

LLAVES_CRIPTO = ("CAUCE_KEK", "CAUCE_K_BIDX")


def _llave_nueva() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def preparar(ruta_credenciales: Path, ruta_env: Path) -> None:
    ent = _credenciales.cargar(ruta_credenciales)
    pg = ent.postgres
    credenciales = {
        "postgres.superusuario": ("PG_SUPERUSER", "PG_SUPERPASSWORD", pg.superusuario),
        "postgres.roles.ingesta": ("INGESTA_USER", "INGESTA_PASSWORD", pg.roles.ingesta),
        "postgres.roles.analista": ("ANALISTA_USER", "ANALISTA_PASSWORD", pg.roles.analista),
        "postgres.roles.auditor": ("AUDITOR_USER", "AUDITOR_PASSWORD", pg.roles.auditor),
    }
    vistas: set[str] = set()
    for clave, (_, _, cred) in credenciales.items():
        secretos.validar_password(clave, cred.password)
        if cred.password in vistas:
            raise secretos.CredencialDebil(f"{clave}: password repetida entre usuarios")
        vistas.add(cred.password)

    previo = leer_env(ruta_env)
    env: dict[str, str] = {"PG_DATABASE": pg.database, "PG_PORT": str(pg.port)}
    for _, (k_user, k_pass, cred) in credenciales.items():
        env[k_user], env[k_pass] = cred.usuario, cred.password
    for llave in LLAVES_CRIPTO:
        env[llave] = previo.get(llave) or _llave_nueva()

    ruta_env.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta_env.with_name(ruta_env.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Generado por `make up` desde credenciales.yaml. No versionar.\n")
        fh.writelines(f"{k}={v}\n" for k, v in env.items())
    os.chmod(tmp, 0o600)
    os.replace(tmp, ruta_env)
