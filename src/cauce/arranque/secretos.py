"""Validacion y generacion de credenciales. Los mensajes nombran la clave, nunca el valor."""
from __future__ import annotations

import secrets

LONGITUD_MINIMA = 24
PROHIBIDAS = frozenset({"todo", "cambiame", "postgres", "minioadmin"})


class CredencialDebil(ValueError):
    """Credencial rechazada. El mensaje lleva la clave y el motivo, nunca el valor."""
    codigo_error = "configuracion_invalida"


def generar_password() -> str:
    return secrets.token_urlsafe(24)  # 32 caracteres


def validar_password(clave: str, valor: str) -> None:
    if not isinstance(valor, str) or valor.strip().lower() in PROHIBIDAS:
        raise CredencialDebil(f"{clave}: password de placeholder o por defecto (corre `make credenciales`)")
    if len(valor) < LONGITUD_MINIMA:
        raise CredencialDebil(f"{clave}: password de menos de {LONGITUD_MINIMA} caracteres")
