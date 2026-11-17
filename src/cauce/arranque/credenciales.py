"""credenciales.yaml: lectura (safe_load) y relleno de credenciales preservando comentarios."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from cauce.arranque.secretos import generar_password

# ignore (no forbid): credenciales.yaml puede traer claves que no leemos.
_MODELO = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)

# `password: "..."` en cualquier nivel, tambien en flujo `{usuario: .., password: ..}`.
_EN_LINEA = re.compile(r'''(password:\s*)(["']?)([^"'\s,}]*)\2''')


_PLANTILLA = """\
# Credenciales locales de cauce. Las genera `make up` (o `make credenciales`); no se versionan.
# Los passwords se generan al azar (24+ caracteres).
# `make credenciales ROTAR=1` los reescribe todos (ver RUNBOOK.md).
postgres:
  host: localhost
  port: 5432
  database: cauce_datos

  # Superusuario local: solo diagnostico y verificacion del cifrado en reposo.
  superusuario:
    usuario: "cauce_admin"
    password: "TODO"

  roles:
    ingesta:
      usuario: "ingesta"
      password: "TODO"
    analista:
      usuario: "analista"
      password: "TODO"
    auditor:
      usuario: "auditor"
      password: "TODO"

# Nombres (con esquema) de los objetos principales.
objetos:
  tabla_consumos: "core.fact_consumo_horario"
  tabla_clientes: "core.dim_cliente"        # contiene PII (cifrada)
  vista_analytics: "analytics.v_consumo_mensual"
  tabla_auditoria: "audit.bitacora_corrida"
"""


def plantilla() -> str:
    """Contenido inicial de credenciales.yaml: anonimo, con los password en TODO."""
    return _PLANTILLA


def rellenar_credenciales(texto: str, rotar: bool) -> str:
    """Reemplaza `password` == TODO (o todos si `rotar`) por valores aleatorios."""

    def sustituir(m: re.Match) -> str:
        if rotar or m.group(3) == "TODO":
            return f'{m.group(1)}"{generar_password()}"'
        return m.group(0)

    return "\n".join(_EN_LINEA.sub(sustituir, linea) if "password:" in linea
                     and not linea.lstrip().startswith("#") else linea
                     for linea in texto.split("\n"))


class Credencial(BaseModel):
    model_config = _MODELO
    usuario: str
    password: str


class Roles(BaseModel):
    model_config = _MODELO
    ingesta: Credencial
    analista: Credencial
    auditor: Credencial


class Postgres(BaseModel):
    model_config = _MODELO
    host: str = "localhost"
    port: int
    database: str
    superusuario: Credencial
    roles: Roles


class Credenciales(BaseModel):
    model_config = _MODELO
    postgres: Postgres
    objetos: dict[str, str] = {}


def cargar(ruta: Path) -> Credenciales:
    doc = yaml.safe_load(Path(ruta).read_text(encoding="utf-8"))
    return Credenciales.model_validate(doc)
