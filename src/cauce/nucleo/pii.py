"""Deteccion de PII en texto. Solo el detector estricto (red de seguridad de logs y out/).

`detectar_estricto` no marca `frontera_id` (CO##########) ni usa el fallback de 7 digitos; por eso es
seguro como ultimo filtro. `enmascarar` (PII en texto libre de observaciones, mas agresivo y con
fallback de >= 7 digitos) se agrega aparte sin tocar el detector estricto.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TipoPII(StrEnum):
    CORREO = "correo"
    CELULAR = "celular"
    CEDULA = "cedula"


_TOKEN = {TipoPII.CORREO: "[EMAIL]", TipoPII.CELULAR: "[CELULAR]", TipoPII.CEDULA: "[CEDULA]"}

_PATRONES: tuple[tuple[TipoPII, re.Pattern[str]], ...] = (
    (TipoPII.CORREO, re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")),
    (TipoPII.CEDULA, re.compile(
        r"(?i)(?<![\w])(?:C\.?\s?C\.?|c[eé]dula)[ \t:]*(?:No\.?|#)?[ \t:]*(?<!\d)\d(?:\.?\d){5,9}(?!\d)")),
    # +57 (separador opcional) o 10 digitos con al menos un separador; nunca pegado a una letra (CO3000000001)
    (TipoPII.CELULAR, re.compile(
        r"\+57[ \t\-]?3\d{2}[ \t.\-]?\d{3}[ \t.\-]?\d{4}(?!\d)"
        r"|(?<![\w+])3\d{2}[ \t\-]\d{3}[ \t\-]?\d{4}(?!\d)"
        r"|(?<![\w+])3\d{2}[ \t\-]?\d{3}[ \t\-]\d{4}(?!\d)")),
)


# Un uuid (corrida_id) no es PII, pero "cc345678-..." se leia como "CC 345678". El estricto lo salta entero:
# `_UUID.split` alterna tramos escaneables (indices pares) y uuids intactos (impares).
_UUID = re.compile(r"((?<![0-9a-z])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-z]))",
                   re.IGNORECASE)


def detectar_estricto(texto: str) -> tuple[TipoPII, ...]:
    """Tipos de PII presentes, sin repetir, en orden estable. Solo detecta."""
    escaneable = "\n".join(_UUID.split(texto)[::2])
    return tuple(t for t, p in _PATRONES if p.search(escaneable))


def enmascarar_estricto(texto: str) -> tuple[str, tuple[TipoPII, ...]]:
    """Reemplaza cada hallazgo del detector estricto por su token tipado; los uuids quedan intactos."""
    tramos = _UUID.split(texto)
    hallados: list[TipoPII] = []
    for tipo, patron in _PATRONES:
        n_total = 0
        for i in range(0, len(tramos), 2):
            tramos[i], n = patron.subn(_TOKEN[tipo], tramos[i])
            n_total += n
        if n_total:
            hallados.append(tipo)
    return "".join(tramos), tuple(hallados)


@dataclass(frozen=True)
class Enmascarado:
    texto: str                       # sin ningun fragmento del original
    hallazgos: tuple[TipoPII, ...]   # uno por ocurrencia, en orden de aparicion


# Prioridad: el primero que reclama un tramo gana. Mas agresivo que el estricto (ADR 0001: enmascarar de
# mas antes que de menos): celular sin separadores y cualquier racha de >= 7 digitos (cedula) en el fallback.
_PATRONES_TEXTO_LIBRE: tuple[tuple[TipoPII, re.Pattern[str]], ...] = (
    (TipoPII.CORREO, _PATRONES[0][1]),
    (TipoPII.CEDULA, _PATRONES[1][1]),
    (TipoPII.CELULAR, re.compile(r"(?<![A-Za-z0-9+])(?:\+?57[ \-]?)?3\d{2}[ .\-]?\d{3}[ .\-]?\d{4}(?!\d)")),
    (TipoPII.CEDULA, re.compile(r"(?<![A-Za-z0-9])\d(?:[ .\-]?\d){6,}(?![A-Za-z0-9])")),
)


def enmascarar(texto: str) -> Enmascarado:
    """Reemplaza correos (tambien de rol), celulares colombianos y cedulas por [EMAIL]/[CELULAR]/[CEDULA]."""
    tramos: list[tuple[int, int, TipoPII]] = []
    for tipo, patron in _PATRONES_TEXTO_LIBRE:
        for m in patron.finditer(texto):
            if all(m.end() <= a or m.start() >= b for a, b, _ in tramos):
                tramos.append((m.start(), m.end(), tipo))
    tramos.sort()
    salida, cursor = [], 0
    for a, b, tipo in tramos:
        salida.append(texto[cursor:a])
        salida.append(_TOKEN[tipo])
        cursor = b
    salida.append(texto[cursor:])
    return Enmascarado("".join(salida), tuple(t for _, _, t in tramos))
