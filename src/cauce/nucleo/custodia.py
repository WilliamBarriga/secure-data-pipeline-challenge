"""Custodia (C2, C3): la unica puerta a `almacen`. Nucleo puro: sin I/O, llaves inyectadas.

`custodiar(resultado, clasificacion, custodio, fuente=...)` aplica la politica del yaml a cada campo
(`clasificacion.politica`): enmascara (via `pii`), cifra AES-256-GCM, calcula el blind index o descarta; lo
que sale es un `ResultadoCustodiado`, el unico tipo que `almacen.cargar` acepta y que no se construye
fuera de este modulo. Ningun campo Restringido-PII sale en claro; el original de `observaciones` no se
guarda en ninguna zona, ni cifrado (ADR 0001).

Envelope: la KEK (de `estado/.env`) envuelve la DEK (`crypto.dek`); la DEK cifra los campos. Cada valor
va como nonce(12) | ciphertext | tag con AAD `tabla|col|llave_natural`: un ciphertext copiado a otra
fila o columna no abre. `nit_bidx = HMAC-SHA256(K_bidx, nit_normalizado)`, K_bidx distinta de la KEK.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cauce.nucleo import pii
from cauce.nucleo.clasificacion import Clasificacion
from cauce.nucleo.fuentes import MotivoRechazoArchivo, Resultado, ficha_por_lotes

CLIENTES_CON_PII = "clientes_con_pii"
_NONCE = 12
_SELLO = object()


Zona = Literal["raw", "core"]  # los nombres que usa `descartar` en el yaml


@dataclass
class _ColumnasZona:
    """Una zona (`raw` o `core`, los nombres que usa `descartar` en el yaml) y las columnas que se le arman."""
    zona: Zona
    tabla: str
    columnas: dict[str, Any] = field(default_factory=dict)


class KekInvalida(RuntimeError):
    """La KEK no desenvuelve la DEK (o no hay DEK). Sin valores."""
    codigo_error = "configuracion_invalida"

    def __init__(self, motivo: str = "la KEK no desenvuelve la DEK"):
        super().__init__(f"{motivo}: corre `make clean && make up`")


class ErrorDescifrado(RuntimeError):
    """Ciphertext alterado, AAD distinta o llave equivocada."""


def _llave(texto: str, nombre: str) -> bytes:
    try:
        b = base64.b64decode(texto, validate=True)
    except (binascii.Error, ValueError):
        raise KekInvalida(f"{nombre} no es base64") from None
    if len(b) != 32:
        raise KekInvalida(f"{nombre} no mide 32 bytes")
    return b


def _sellar(llave: bytes, aad: bytes, plano: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return nonce + AESGCM(llave).encrypt(nonce, plano, aad)


def _abrir(llave: bytes, aad: bytes, blob: bytes) -> bytes:
    try:
        return AESGCM(llave).decrypt(bytes(blob[:_NONCE]), bytes(blob[_NONCE:]), aad)
    except InvalidTag:
        raise ErrorDescifrado("no se pudo descifrar") from None


def _aad_dek(version: int) -> bytes:
    return f"crypto.dek|dek_envuelta|{version}".encode()


def nueva_dek(kek: str, key_version: int) -> bytes:
    """DEK aleatoria envuelta con la KEK (lo que `bootstrap` guarda en `crypto.dek`). No usa K_bidx."""
    return _sellar(_llave(kek, "CAUCE_KEK"), _aad_dek(key_version), AESGCM.generate_key(bit_length=256))


class Custodio:
    def __init__(self, kek: str, k_bidx: str):
        self._kek = _llave(kek, "CAUCE_KEK")
        self._k_bidx = _llave(k_bidx, "CAUCE_K_BIDX")
        self._dek: bytes | None = None
        self.key_version: int | None = None

    # --- DEK -------------------------------------------------------------------------------
    def abrir(self, key_version: int, dek_envuelta: bytes) -> None:
        try:
            self._dek = _abrir(self._kek, _aad_dek(key_version), dek_envuelta)
        except ErrorDescifrado:
            raise KekInvalida() from None
        self.key_version = key_version

    # --- campos ----------------------------------------------------------------------------
    def _requiere_dek(self) -> bytes:
        if self._dek is None:
            raise KekInvalida("no hay DEK abierta")
        return self._dek

    @staticmethod
    def _aad(tabla: str, col: str, llave: str) -> bytes:
        return f"{tabla}|{col}|{llave}".encode()

    def cifrar(self, tabla: str, col: str, llave: str, plano: str) -> bytes:
        return _sellar(self._requiere_dek(), self._aad(tabla, col, llave), plano.encode())

    def descifrar(self, tabla: str, col: str, llave: str, blob: bytes) -> str:
        return _abrir(self._requiere_dek(), self._aad(tabla, col, llave), blob).decode()

    def indice(self, nit: str) -> bytes:
        """Blind index del NIT normalizado (solo digitos: cubre 'NIT ', puntos, guion y sin guion)."""
        normalizado = re.sub(r"\D", "", nit)
        return hmac.new(self._k_bidx, normalizado.encode(), hashlib.sha256).digest()


# --- resultado custodiado ----------------------------------------------------------------------
@dataclass(frozen=True)
class RegistroCustodiado:
    linea: int
    raw: Mapping[str, Any] | None   # columnas de la tabla raw (None si la fuente no tiene)
    core: Mapping[str, Any]         # columnas de la tabla core (factura: mas `componentes`, que no es columna)


@dataclass(frozen=True)
class RechazoCustodiado:
    linea: int | None
    motivo: str
    payload: str | None             # solo fuentes sin PII (custodia decide desde la Clasificacion)
    payload_cifrado: bytes | None   # fuentes con PII: fila con observaciones ya enmascarada
    key_version: int | None


@dataclass(frozen=True)
class ResultadoCustodiado:
    """Lo unico que `almacen.cargar` acepta. Solo `custodiar` lo construye (sello privado)."""
    fuente: str
    registros: tuple[RegistroCustodiado, ...]
    rechazos: tuple[RechazoCustodiado, ...]
    conteos: Mapping[str, int]
    lineas: tuple[tuple[int, str], ...]
    archivo_rechazado: MotivoRechazoArchivo | None
    key_version: int | None
    _sello: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._sello is not _SELLO:
            raise TypeError("ResultadoCustodiado solo lo construye custodia.custodiar")


def _texto_json(valor: Any) -> str:
    # Decimal y fechas como texto: sin perdida de precision en el payload
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, default=str)


def _cuerpo_cifrado(custodio: Custodio, linea: int | None, cuerpo: Any) -> bytes:
    """Cuerpo de una fila (ya podado o Enmascarado) cifrado para `quarantine.rechazo`, atado a su linea. Facturas
    lo guardan tambien en raw: si la Factura pierde despues, `almacen` lo copia a Cuarentena sin ver la llave."""
    return custodio.cifrar("quarantine.rechazo", "payload_cifrado", str(linea), _texto_json(cuerpo))


def _rechazo_cifrado(custodio: Custodio, linea: int | None, motivo: str, cuerpo: Any) -> RechazoCustodiado:
    """Rechazo de una fuente con PII: el crudo no sale, solo el cuerpo cifrado."""
    return RechazoCustodiado(linea, motivo, None, _cuerpo_cifrado(custodio, linea, cuerpo), custodio.key_version)


def _sin_cuerpo(linea: int | None, motivo: str) -> RechazoCustodiado:
    """Rechazo sin campos que custodiar (fila malformada o archivo entero): el crudo nunca sale."""
    return RechazoCustodiado(linea, motivo, None, None, None)


def _sellado(resultado: Resultado, fuente: str, registros: list[RegistroCustodiado],
             rechazos: list[RechazoCustodiado], conteos: Mapping[str, int], lineas: tuple[tuple[int, str], ...],
             custodio: Custodio) -> ResultadoCustodiado:
    return ResultadoCustodiado(
        fuente=fuente, registros=tuple(registros), rechazos=tuple(rechazos), conteos=dict(conteos), lineas=lineas,
        archivo_rechazado=resultado.archivo_rechazado, key_version=custodio.key_version, _sello=_SELLO)


# --- factura (JSON anidado) --------------------------------------------------------------------------
# La forma de la fila core de factura, declarada una sola vez: columna de core.fact_factura -> ruta del campo en el
# JSON (la politica de cada ruta sale del yaml). `almacen` toma las columnas de la fila; una ruta ausente o
# descartada va como None (NULL), asi todas las filas tienen las mismas columnas.
_CORE_FACTURA = {"factura_id": "factura_id", "frontera_id": "frontera_id", "periodo": "periodo",
                 "generado_en": "generado_en", "consumo_facturado_kwh": "consumo_facturado_kwh",
                 "tarifa_cop_kwh": "tarifa_cop_kwh", "valor_total_cop": "valor_total_cop",
                 "metodo_pago_tipo": "metodo_pago.tipo"}


_DESCARTADO = object()  # hoja que el yaml descarta en la zona, o ruta ausente


def _en_ruta(valor: Mapping[str, Any], ruta: str) -> Any:
    for k in ruta.split("."):
        if not isinstance(valor, Mapping) or k not in valor:
            return _DESCARTADO
        valor = valor[k]
    return valor


def _hijos(valor: Any, ruta: str) -> list[tuple[Any, str]] | None:
    """Recorrido compartido de un JSON anidado: (subvalor, ruta del yaml) de cada hijo de un dict o una lista
    (las listas usan `ruta[]`); None si `valor` es una hoja."""
    if isinstance(valor, dict):
        return [(v, f"{ruta}.{k}" if ruta else k) for k, v in valor.items()]
    if isinstance(valor, list):
        return [(v, f"{ruta}[]") for v in valor]
    return None


def _podar(valor: Any, ruta: str, zona: Zona, clasificacion: Clasificacion) -> Any:
    """Copia de un JSON anidado sin las hojas que el yaml descarta en `zona`."""
    hijos = _hijos(valor, ruta)
    if hijos is None:
        pol = clasificacion.politica(clasificacion.campo_de("factura", ruta))
        return _DESCARTADO if zona in pol.descartar else valor
    podados = [_podar(v, r, zona, clasificacion) for v, r in hijos]
    if isinstance(valor, dict):
        return {k: p for k, p in zip(valor, podados, strict=True) if p is not _DESCARTADO}
    return [p for p in podados if p is not _DESCARTADO]


def _contiene_cifrable(valor: Any, ruta: str, clasificacion: Clasificacion) -> bool:
    hijos = _hijos(valor, ruta)
    if hijos is None:
        return clasificacion.politica(clasificacion.campo_de("factura", ruta)).cifrar
    return any(_contiene_cifrable(v, r, clasificacion) for v, r in hijos)


def _custodiar_factura(resultado: Resultado, clasificacion: Clasificacion, custodio: Custodio, *,
                       fuente: str) -> ResultadoCustodiado:
    """Facturas: el subobjeto de primer nivel que contiene un campo cifrable (`cliente`) viaja entero cifrado en
    el payload de raw; lo que el yaml descarta no se guarda en ninguna zona que lo descarte."""
    tabla_raw = ficha_por_lotes(fuente).destino.raw_requerida

    def raw_podado(campos: Mapping[str, Any]) -> dict[str, Any]:
        return _podar(dict(campos), "", "raw", clasificacion)

    registros = []
    for reg in resultado.registros:
        c = reg.campos
        fid = str(c["factura_id"])
        payload: dict[str, Any] = {}
        for k, v in raw_podado(c).items():
            if _contiene_cifrable(v, k, clasificacion):
                sello = custodio.cifrar(tabla_raw, f"payload.{k}", fid, _texto_json(v))
                payload[k] = base64.b64encode(sello).decode()
            else:
                payload[k] = v
        raw = {"frontera_id": c["frontera_id"], "generado_en": c["generado_en"],
               "payload": _texto_json(payload),
               "payload_cifrado": _cuerpo_cifrado(custodio, reg.linea, raw_podado(c)),
               "key_version": custodio.key_version}
        core_c = _podar(dict(c), "", "core", clasificacion)
        en_core = {col: _en_ruta(core_c, ruta) for col, ruta in _CORE_FACTURA.items()}
        core: dict[str, Any] = {col: None if v is _DESCARTADO else v for col, v in en_core.items()}
        core["componentes"] = tuple((x["concepto"], x["valor_cop_kwh"]) for x in core_c.get("componentes", []))
        registros.append(RegistroCustodiado(reg.linea, raw, core))
    rechazos = [_sin_cuerpo(r.linea, r.motivo) if r.campos is None
                else _rechazo_cifrado(custodio, r.linea, r.motivo, raw_podado(r.campos))
                for r in resultado.rechazos]
    return _sellado(resultado, fuente, registros, rechazos, resultado.conteos, (), custodio)


def _custodiar_tabular(resultado: Resultado, clasificacion: Clasificacion, custodio: Custodio, *,
                       fuente: str) -> ResultadoCustodiado:
    """Fuentes CSV (maestros y consumos): la politica del yaml por columna, hacia raw y core. Una fuente sin
    campos de PII pasa tal cual (a raw, si su destino tiene, y a core), con su crudo; una con PII enmascara, cifra
    o descarta y nunca guarda el crudo."""
    ficha = ficha_por_lotes(fuente)
    tablas = ficha.destino
    politicas = {h: clasificacion.politica(clasificacion.campo_de(fuente, h)) for h in ficha.columnas}
    if not any(p.cifrar or p.enmascarar for p in politicas.values()):
        registros = [RegistroCustodiado(reg.linea, None if tablas.raw is None else dict(reg.campos), dict(reg.campos))
                     for reg in resultado.registros]
        rechazos = [RechazoCustodiado(r.linea, r.motivo, r.payload or None, None, None) for r in resultado.rechazos]
        return _sellado(resultado, fuente, registros, rechazos, resultado.conteos, resultado.lineas, custodio)

    ids_con_pii: set[str] = set()

    def enmascarar(campos: Mapping[str, str], linea: int | None) -> dict[str, str]:
        """Copia con los campos `enmascarar` del yaml ya Enmascarados; anota la fila si tenia PII."""
        salida, hallazgos = dict(campos), 0
        for h, p in politicas.items():
            if p.enmascarar:
                m = pii.enmascarar(campos[h])
                salida[h] = m.texto
                hallazgos += len(m.hallazgos)
        if hallazgos:
            ids_con_pii.add(campos.get(tablas.clave) or f"linea:{linea}")
        return salida

    def columnas(destino: _ColumnasZona, valores: Mapping[str, str], llave: str) -> dict[str, Any]:
        for h, p in politicas.items():
            if destino.zona in p.descartar:
                continue
            v = valores[h]
            if p.enmascarar:
                destino.columnas[f"{h}_enmascarada"] = v
            elif p.cifrar:
                col = f"{h}_cifrado"
                destino.columnas[col] = custodio.cifrar(destino.tabla, col, llave, v) if v.strip() else None
                if p.bidx and destino.zona == "core":
                    destino.columnas[f"{h}_bidx"] = custodio.indice(v) if v.strip() else None
            else:
                destino.columnas[h] = v
        destino.columnas["key_version"] = custodio.key_version
        return destino.columnas

    registros = []
    for reg in resultado.registros:
        llave = str(reg.campos[tablas.clave])
        valores = enmascarar(reg.campos, reg.linea)
        raw = None if tablas.raw is None else columnas(_ColumnasZona("raw", tablas.raw), valores, llave)
        registros.append(RegistroCustodiado(reg.linea, raw, columnas(_ColumnasZona("core", tablas.core), valores,
                                                                     llave)))
    rechazos = [_sin_cuerpo(r.linea, r.motivo) if r.campos is None
                else _rechazo_cifrado(custodio, r.linea, r.motivo, enmascarar(r.campos, r.linea))
                for r in resultado.rechazos]
    conteos = dict(resultado.conteos)
    if any(p.enmascarar for p in politicas.values()):
        conteos[CLIENTES_CON_PII] = len(ids_con_pii)
    return _sellado(resultado, fuente, registros, rechazos, conteos, (), custodio)


# formato de la Ficha de Fuente -> como se custodia (por columna o por ruta del JSON anidado)
_CUSTODIAS = {"csv": _custodiar_tabular, "json": _custodiar_factura}


def custodiar(resultado: Resultado, clasificacion: Clasificacion, custodio: Custodio, *,
              fuente: str) -> ResultadoCustodiado:
    """Aplica la politica del yaml al Resultado de `fuentes.leer`. Ver el docstring del modulo."""
    return _CUSTODIAS[ficha_por_lotes(fuente).formato](resultado, clasificacion, custodio, fuente=fuente)
