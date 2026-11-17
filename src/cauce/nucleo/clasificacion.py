"""Clasificacion viva (C1): `data_classification.yaml` -> politica por campo + check fail-fast.

Nucleo puro: recibe texto, el catalogo de headers (de `fuentes`, nunca de data/) y las columnas fisicas
(de `almacen`). Los errores y logs llevan nombres, nunca valores.

La unidad es el campo logico de la fuente (`cliente.nit`); sus `fisicas` heredan el nivel. Un header de una
fuente `f` esta clasificado si existe el campo `f.<header>` o si algun campo `f.*` lo declara en `aliases`.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Nivel = Literal["Público", "Interno", "Confidencial", "Restringido-PII"]
ORDEN: dict[str, int] = {"Público": 0, "Interno": 1, "Confidencial": 2, "Restringido-PII": 3}
ZONAS = ("raw", "quarantine", "crypto", "core", "analytics", "audit")
CODIGO_ERROR = "clasificacion_incompleta"
RUTA_POR_DEFECTO = Path(__file__).resolve().parents[3] / "data_classification.yaml"

_MODELO = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
_CAMPO = re.compile(r"^[a-z_]+(\.[a-z_]+(\[\])?)+$")
_FISICA = re.compile(rf"^({'|'.join(ZONAS)})\.[a-z_0-9]+\.[a-z_0-9]+$")  # esquema: una de las ZONAS
_TEXTO = re.compile(r"^(text|character|citext)")


class ErrorClasificacion(RuntimeError):
    pass


class CampoNoClasificado(ErrorClasificacion):
    """Se pidio la politica (o el campo de un header) de algo que el yaml no clasifica."""


@dataclass(frozen=True)
class Causa:
    codigo: str
    nombres: tuple[str, ...]


class ClasificacionIncompleta(ErrorClasificacion):
    """Falla fail-fast. `codigo_error` es el del catalogo cerrado de la Bitacora; solo nombres, nunca valores."""
    codigo_error = CODIGO_ERROR

    def __init__(self, causas: Iterable[Causa]):
        self.causas = tuple(causas)
        detalle = "; ".join(f"{c.codigo}: {', '.join(c.nombres)}" for c in self.causas)
        super().__init__(f"{CODIGO_ERROR}: {detalle}")


class Campo(BaseModel):
    model_config = _MODELO
    nivel: Nivel
    aliases: list[str] = Field(default_factory=list)
    fisicas: list[str] = Field(default_factory=list)
    bidx: bool = False
    tratamiento: Literal["enmascarar_texto_libre"] | None = None
    descartar: list[Literal["raw", "core"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherente(self) -> Campo:
        if self.bidx and self.nivel != "Restringido-PII":
            raise ValueError("bidx exige Restringido-PII")
        if any(not _FISICA.match(f) for f in self.fisicas):
            raise ValueError("fisicas: formato esquema.tabla.columna")
        return self


class Documento(BaseModel):
    model_config = _MODELO
    version: Literal[1]
    campos: dict[str, Campo]
    tecnicas: dict[Nivel, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherente(self) -> Documento:
        if any(not _CAMPO.match(n) for n in self.campos):
            raise ValueError("campos: nombre fuente.campo")
        vistas: set[str] = set()
        todas = [f for c in self.campos.values() for f in c.fisicas] + \
                [f for fs in self.tecnicas.values() for f in fs]
        for f in todas:
            if not _FISICA.match(f) or f in vistas:
                raise ValueError("fisicas: formato invalido o repetida")
            vistas.add(f)
        headers: set[tuple[str, str]] = set()
        for nombre, campo in self.campos.items():
            fuente, _, resto = nombre.partition(".")
            for h in (resto, *campo.aliases):
                if (fuente, h) in headers:
                    raise ValueError("header repetido en la fuente")
                headers.add((fuente, h))
        return self


@dataclass(frozen=True)
class Politica:
    """Que hacer con un campo, derivado del nivel (tabla unica) mas los overrides."""
    nivel: str
    cifrar: bool          # antes de la 1ra escritura; no si se enmascara (el original no persiste)
    bidx: bool
    enmascarar: bool
    descartar: tuple[str, ...]
    en_analytics: bool
    valor_en_logs: bool


def _causa_esquema(e: ValidationError) -> ClasificacionIncompleta:
    nombres = tuple(sorted({".".join(str(p) for p in err["loc"]) or "raiz" for err in e.errors()}))
    return ClasificacionIncompleta([Causa("esquema_invalido", nombres)])


class Clasificacion:
    def __init__(self, doc: Documento):
        self._doc = doc
        self._por_header: dict[tuple[str, str], str] = {}
        for nombre, campo in doc.campos.items():
            fuente, _, resto = nombre.partition(".")
            for h in (resto, *campo.aliases):
                self._por_header[(fuente, h)] = nombre
        self._nivel_fisica: dict[str, str] = {}
        self._campo_fisica: dict[str, str | None] = {}
        for nombre, campo in doc.campos.items():
            for f in campo.fisicas:
                self._nivel_fisica[f], self._campo_fisica[f] = campo.nivel, nombre
        for nivel, fisicas in doc.tecnicas.items():
            for f in fisicas:
                self._nivel_fisica[f], self._campo_fisica[f] = nivel, None

    @classmethod
    def desde_texto(cls, texto: str) -> Clasificacion:
        try:
            crudo = yaml.safe_load(texto)
        except yaml.YAMLError:
            raise ClasificacionIncompleta([Causa("esquema_invalido", ("yaml",))]) from None
        if not isinstance(crudo, dict):
            raise ClasificacionIncompleta([Causa("esquema_invalido", ("raiz",))])
        try:
            return cls(Documento.model_validate(crudo))
        except ValidationError as e:
            raise _causa_esquema(e) from None

    def politica(self, campo: str) -> Politica:
        c = self._doc.campos.get(campo)
        if c is None:
            raise CampoNoClasificado(f"campo sin clasificar: {campo}")
        enmascarar = c.tratamiento == "enmascarar_texto_libre"
        return Politica(
            nivel=c.nivel,
            cifrar=c.nivel == "Restringido-PII" and not enmascarar,
            bidx=c.bidx, enmascarar=enmascarar, descartar=tuple(c.descartar),
            en_analytics=ORDEN[c.nivel] <= ORDEN["Interno"],
            valor_en_logs=ORDEN[c.nivel] <= ORDEN["Interno"],
        )

    def niveles_logs(self) -> dict[str, str]:
        """clave de evento de log -> nivel (campos `logs.*`); alimenta el filtro por clave de observabilidad."""
        return {n.partition(".")[2]: c.nivel for n, c in self._doc.campos.items() if n.startswith("logs.")}

    def campo_de(self, fuente: str, header: str) -> str:
        try:
            return self._por_header[(fuente, header)]
        except KeyError:
            raise CampoNoClasificado(f"header sin clasificar: {fuente}.{header}") from None

    def verificar(self, catalogo: Mapping[str, Iterable[str]], columnas: Mapping[str, str]) -> None:
        """Fail-fast. `catalogo`: fuente -> headers (de `fuentes`); `columnas`: esquema.tabla.columna -> tipo."""
        causas: list[Causa] = []

        sin_clasificar = sorted(f"{fuente}.{h}" for fuente, headers in catalogo.items() for h in headers
                                if (fuente, h) not in self._por_header)
        if sin_clasificar:
            causas.append(Causa("header_sin_clasificar", tuple(sin_clasificar)))

        sin_mapear = sorted(c for c in columnas if c not in self._nivel_fisica)
        if sin_mapear:
            causas.append(Causa("columna_sin_mapear", tuple(sin_mapear)))

        inexistentes = sorted(f for f in self._nivel_fisica if f not in columnas)
        if inexistentes:
            causas.append(Causa("fisica_inexistente", tuple(inexistentes)))

        en_claro = sorted(f for f, nivel in self._nivel_fisica.items()
                          if nivel == "Restringido-PII" and _TEXTO.match(columnas.get(f, "")))
        if en_claro:
            causas.append(Causa("pii_en_claro", tuple(en_claro)))

        excede = sorted(f for f, nivel in self._nivel_fisica.items()
                        if f.startswith("analytics.") and ORDEN[nivel] > ORDEN["Interno"])
        if excede:
            causas.append(Causa("analytics_excede_interno", tuple(excede)))

        if causas:
            raise ClasificacionIncompleta(causas)

