"""Unica puerta de escritura a out/: falla cerrado si detecta PII; si no, tmp + fsync + rename. Tambien la
auditoria posterior de out/ y logs/ ya escritos (`make auditar-salidas`)."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cauce import entorno
from cauce.nucleo import pii

CODIGO_ERROR = "pii_en_salida"


class PiiEnSalida(RuntimeError):
    """El contenido de una salida trae PII. Nombra el archivo y los tipos, nunca el valor."""
    codigo_error = CODIGO_ERROR


def directorio_salidas() -> Path:
    """CAUCE_OUT (tests: temporal) o CAUCE_RAIZ/out, resuelto por `entorno`."""
    return entorno.resolver(os.environ).out


def escribir_atomico(nombre: str, contenido: str | bytes, directorio: Path | None = None) -> Path:
    if not nombre or nombre != os.path.basename(nombre) or nombre.startswith("."):
        raise ValueError("nombre de salida invalido")
    datos = contenido.encode("utf-8") if isinstance(contenido, str) else contenido
    tipos = pii.detectar_estricto(datos.decode("utf-8", errors="replace"))
    if tipos:
        raise PiiEnSalida(f"{nombre}: PII detectada ({', '.join(t.value for t in tipos)}); no se escribe")
    destino_dir = directorio or directorio_salidas()
    destino_dir.mkdir(parents=True, exist_ok=True)
    final = destino_dir / nombre
    tmp = destino_dir / f".{nombre}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(destino_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return final


def reemplazar_clave(nombre: str, clave: str, valor: Any, directorio: Path | None = None) -> Path:
    """Reemplaza solo `clave` del objeto JSON `nombre` (las demas quedan intactas): lectura + escritura atomica.
    Un archivo previo faltante, ilegible o que no es un objeto se trata como vacio."""
    try:
        previo = json.loads(((directorio or directorio_salidas()) / nombre).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previo = None
    base = previo if isinstance(previo, dict) else {}
    return escribir_atomico(nombre, json.dumps({**base, clave: valor}, indent=2, ensure_ascii=True) + "\n", directorio)


def auditar(directorios: Iterable[Path]) -> dict[Path, tuple[pii.TipoPII, ...]]:
    """`make auditar-salidas`: pasa `pii.detectar_estricto` por cada archivo (recursivo) de `directorios`, ya
    escritos. Devuelve solo los archivos con hallazgos y sus tipos, nunca el valor. Un directorio faltante es
    FileNotFoundError: no se certifica lo que no existe."""
    hallazgos: dict[Path, tuple[pii.TipoPII, ...]] = {}
    for directorio in directorios:
        if not directorio.is_dir():
            raise FileNotFoundError(str(directorio))
        for ruta in sorted(p for p in directorio.rglob("*") if p.is_file()):
            tipos = pii.detectar_estricto(ruta.read_bytes().decode("utf-8", errors="replace"))
            if tipos:
                hallazgos[ruta] = tipos
    return hallazgos
