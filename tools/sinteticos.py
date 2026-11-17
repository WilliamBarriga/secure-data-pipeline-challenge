#!/usr/bin/env python3
"""Generador de Datos sinteticos sucios para `data/` (solo stdlib, fuera del paquete `cauce`).

Reproduce la forma de la entrada del brief (§2): el layout `data/{maestros,consumos,facturacion,eventos}`, los
mismos nombres de archivo, las mismas fechas (consumo 2026-01..2026-04 + correccion de abril, facturacion de esos
periodos, stream sobre la Rejilla del 2026-06-01 -05:00) y un defecto por archivo. Todos los valores salen de
catalogos inventados de este archivo y de `random.Random` con semilla fija: misma semilla y mismos parametros dan
los mismos bytes. La generacion nunca lee archivos; solo `--corromper` lee el archivo que el operador nombra para
dejar una copia danada. El pipeline nunca importa este modulo.

Uso:
    python3 tools/sinteticos.py [DESTINO=data] [--forzar] [--semilla N] [--fronteras N] [--verdad RUTA]
    python3 tools/sinteticos.py [DESTINO] --mes AAAA-MM [--forzar] [--verdad RUTA]
    python3 tools/sinteticos.py --corromper RUTA [--forzar]

Defecto plantado por archivo:
    maestros/clientes.csv              ISO-8859-1, 4 formatos de NIT (+1 DV invalido), PII en `observaciones`,
                                       4 campos hostiles (B7), razon social y correo repetidos
    maestros/fronteras.csv, limites    limpios (referencia de huerfanas y Banda operativa)
    consumos/consumos_2026-01.csv      base + Duplicados exactos/conflictivos, nulos, negativos, outliers, Huecos
    consumos/consumos_2026-02.csv      `;` + coma decimal + instantes UTC con `Z` (columna `timestamp_utc`)
    consumos/consumos_2026-03.csv      columna `kwh` + columna `unidad` con filas en MWh
    consumos/consumos_2026-04.csv      formato base, mismo mix que enero
    consumos/consumos_2026-04_correccion.csv  Lecturas v2 `CORREGIDA` sobre horas validas de abril
    facturacion/facturacion_*.json     Facturas huerfanas al final (Frontera inexistente, `componentes` vacio)
    eventos/mediciones_stream.ndjson   Caidas, fuera de banda, Mediciones retroactivas, reenvios y malformados

Verdad plantada (`--verdad RUTA`, nunca dentro de DESTINO): un JSON con lo que la entrada sembro a proposito,
contado por construccion (no reaplica reglas B/C). Esquema `cauce.verdad/1`:
    esquema, semilla, fronteras, modo ("completo" | "mes")
    archivos            ruta relativa -> {bytes, sha256}
    por_archivo         ruta relativa de consumo -> {filas, nulos, negativos, outlier, mwh_convertidas,
                        duplicados_exactos, duplicados_conflictivos, huecos_sin_fila, correcciones}
    dq_report           las 12 llaves de `out/dq_report.json` (brief §9.3) con su valor esperado
    consumo_mensual     filas esperadas de `out/consumo_mensual.csv`: [{frontera_id, mes, consumo_kwh, horas_con_dato}]
                        (consumo_kwh como texto con 3 decimales, orden frontera_id y mes)
    cuarentena          fuente -> motivo -> filas esperadas en Cuarentena
    maestros            {clientes, fronteras, limites, clientes_hostiles}
    facturacion         {facturas, huerfanas, fronteras_huerfanas}
    alertas             lo esperado en `out/alertas.json`: [{tipo, frontera_id, ts}] ordenado (tipo, frontera_id, ts)
    stream              {eventos, duplicados, invalidos_por_motivo, retroactivas, caidas, fuera_de_banda,
                        bajo_banda, igual_al_maximo}
En modo `mes` solo vienen las llaves del consumo de ese mes (archivos, por_archivo, dq_report parcial de consumo,
consumo_mensual, cuarentena.consumo).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import sys
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SEMILLA = 2026
FRONTERAS = 40
MINIMO_FRONTERAS = 10  # por debajo no caben todos los defectos plantados en Fronteras distintas
MESES = ("2026-01", "2026-02", "2026-03", "2026-04")
FORMATO_DEL_MES = {"2026-01": "base", "2026-02": "utc", "2026-03": "unidad", "2026-04": "base"}
MES_CORREGIDO = "2026-04"
HORA_LOCAL = timezone(timedelta(hours=-5))  # America/Bogota, sin horario de verano
INICIO_REJILLA = datetime(2026, 6, 1, tzinfo=HORA_LOCAL)  # = cauce.nucleo.alertas.INICIO_REJILLA
INTERVALO = timedelta(minutes=15)
INTERVALOS = 96
RACHA_CAIDA = 3
ESQUEMA_VERDAD = "cauce.verdad/1"

# Dominios reservados por RFC 2606 (example.com/.org/.net y los TLD .test, .example, .invalid): ningun correo
# llega a nadie real. https://www.rfc-editor.org/rfc/rfc2606
DOMINIOS = ("example.com", "example.org", "example.net", "empresa.test", "correo.test", "facturas.invalid")

# Celulares: 10 digitos con indicativo movil 399 y numero de abonado que empieza por 0. Ningun operador tiene el
# indicativo 399 (los moviles asignados son 300-305, 310-324, 333 y 350-351:
# https://en.wikipedia.org/wiki/Telephone_numbers_in_Colombia, verificable en el SIGRI de la CRC,
# https://www.crcom.gov.co), y las series de abonado que la CRC comunica a la UIT arrancan en 2000000
# (https://www.itu.int/dms_pub/itu-t/oth/02/02/T020200002C0002PDFE.pdf). Siguen pareciendo celulares a los
# detectores de PII (`3` + 9 digitos).
INDICATIVO_CELULAR = "399"

# --- catalogos inventados ----------------------------------------------------------------------------------
_NOMBRES = ("Álvaro", "Ángela", "Begoña", "Benjamín", "Celmira", "Damián", "Efraín", "Elíades", "Fabián", "Gonzalo",
            "Hernán", "Inés", "Joaquín", "Leonor", "Matías", "Nubia", "Octavio", "Patricio", "Remedios", "Rubén",
            "Sebastián", "Tránsito", "Ulises", "Valentín", "Ximena", "Yesenia", "Zoraida", "Aníbal", "Belén",
            "Críspulo")
_APELLIDOS = ("Albarracín", "Bermúdez", "Cifuentes", "Dávila", "Echeverri", "Fajardo", "Galindo", "Hinestroza",
              "Ibáñez", "Jaramillo", "Lozada", "Montaño", "Núñez", "Ocampo", "Peñaranda", "Quiñones", "Restrepo",
              "Salcedo", "Tobón", "Urrutia", "Valderrama", "Yepes", "Zuluaga", "Arbeláez", "Benítez", "Cañizares",
              "Duarte", "Espíndola", "Gaitán", "Henao", "Llinás", "Muñetón", "Oviedo", "Pizarro", "Rengifo",
              "Sarmiento")
_RUBROS = ("Textiles", "Cerámicas", "Panificadora", "Ingeniería", "Logística", "Fundición", "Lácteos", "Plásticos",
           "Química", "Metalmecánica", "Hilandería", "Curtiembres", "Molinería", "Tostadora", "Litografía", "Avícola",
           "Frigorífico", "Papelería", "Vidriería", "Destilería")
_NOMBRES_FANTASIA = ("Albarrán", "Cumbre Añil", "Río Tenue", "Mirabel", "Ñandutí", "Bruma Alta", "Sotavento",
                     "Peñón Gris", "Quebrada Lúa", "Vereda Ámbar", "Colina Ocre", "Lucerna", "Páramo Azul", "Trébol",
                     "Garza Mora", "Cañaveral Sur", "Monteluz", "Ceiba Roja", "Marañón", "Altamira Baja")
_SUFIJOS = ("S.A.S.", "S.A.", "Ltda.")
_CIUDADES = (("Medellín", "Antioquia"), ("Bogotá", "Cundinamarca"), ("Cali", "Valle del Cauca"),
             ("Barranquilla", "Atlántico"), ("Bucaramanga", "Santander"), ("Pereira", "Risaralda"),
             ("Cartagena", "Bolívar"), ("Manizales", "Caldas"))
_OPERADORES = ("Red Andina", "Red Caribe", "Red Pacifico", "Red Llanos", "Red Cafetera", "Red Santanderes",
               "Red Altiplano", "Red Sabana")
_USUARIOS = ("contacto", "gerencia", "compras", "energia", "tesoreria", "operaciones", "facturacion", "planta")
_OBSERVACIONES_LIMPIAS = ("Cliente con contrato vigente", "Sin novedades en la ultima visita",
                          "Prefiere contacto en horario de oficina", "Solicita factura electronica",
                          "Planta con turno nocturno", "Revisar acometida en la proxima visita",
                          "Pago puntual, sin reclamos abiertos", "Contrato renovado sin cambios")
_TIPOS_PAGO = ("pse", "transferencia", "debito_automatico")
_PATRONES_SERIAL = ("LLDDDDDD", "LLLLDDDD", "DDLLDDLL")
_LETRAS = "ABCDEFGHJKLMNPRSTUVWXYZ"
_MINUSCULAS = "abcdefghjkmnpqrstuvwxyz"
_PESOS_DIAN = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)


def _rng(semilla: int, *etiqueta: str) -> random.Random:
    """Un generador por archivo: misma semilla y etiqueta, misma secuencia (las semillas str son estables)."""
    return random.Random(":".join((str(semilla), *etiqueta)))


def _escala(n: int, conteo: int, minimo: int = 1) -> int:
    """Conteo de un defecto plantado para 40 Fronteras, llevado a `n` Fronteras."""
    return max(minimo, round(conteo * n / FRONTERAS))


def _ascii(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")


def digito_verificacion(base: str) -> int:
    """DV de un NIT (modulo 11 de la DIAN, pesos de derecha a izquierda)."""
    resto = sum(int(d) * p for d, p in zip(reversed(base), _PESOS_DIAN, strict=False)) % 11
    return resto if resto < 2 else 11 - resto


def _nit_texto(base: str, dv: int, formato: int) -> str:
    """Las cuatro formas de NIT de la entrada: guion, puntos, prefijo `NIT ` y sin separador."""
    puntos = f"{base[:3]}.{base[3:6]}.{base[6:]}"
    return (f"{base}-{dv}", f"{puntos}-{dv}", f"NIT {base}-{dv}", f"{base}{dv}")[formato]


def _celular(rng: random.Random) -> str:
    return f"{INDICATIVO_CELULAR}0{rng.randrange(10**6):06d}"


def _texto_kwh(valor: Decimal, decimales: int) -> str:
    return f"{valor:.{decimales}f}"


def _decimal(rng: random.Random, x: float) -> Decimal:
    """Valor con 3 decimales (~90%), 2 (~9%) o 1 (~1%), como texto exacto."""
    decimales = rng.choices((3, 2, 1), (90, 9, 1))[0]
    return Decimal(_texto_kwh(Decimal(repr(x)), decimales))


def _csv(filas: list[list[str]], separador: str = ",") -> str:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=separador, lineterminator="\r\n").writerows(filas)
    return buffer.getvalue()


# --- maestros ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Frontera:
    frontera_id: str
    codigo_sic: str
    cliente_id: str
    nivel_kwh: float  # consumo horario medio de la Frontera
    min_kwh: Decimal  # Banda operativa por intervalo de 15 minutos
    max_kwh: Decimal
    serial: str
    firmware: str


@dataclass(frozen=True)
class Cliente:
    cliente_id: str
    razon_social: str
    nit_base: str
    nit_dv: int
    email: str
    celular: str


@dataclass(frozen=True)
class Maestros:
    fronteras: tuple[Frontera, ...]
    clientes: dict[str, Cliente]
    archivos: dict[str, bytes]
    clientes_con_pii: int
    hostiles: int


def _serial(rng: random.Random) -> str:
    patron = rng.choice(_PATRONES_SERIAL)
    return "".join(rng.choice(_LETRAS) if c == "L" else str(rng.randrange(10)) for c in patron)


def _razones(rng: random.Random, n: int) -> list[str]:
    combos = [(r, f, s) for r in _RUBROS for f in _NOMBRES_FANTASIA for s in _SUFIJOS]
    return [f"{r} {f} {s}" for r, f, s in rng.sample(combos, n)]


def _nombre(rng: random.Random) -> str:
    return f"{rng.choice(_NOMBRES)} {rng.choice(_APELLIDOS)} {rng.choice(_APELLIDOS)}"


def _observaciones(rng: random.Random, n: int, clientes: list[Cliente]) -> tuple[list[str], int]:
    """PII en texto libre en ~25 de cada 40: correo, celular (a veces el de contacto), ambos o cedula."""
    con_pii = _escala(n, 25)
    mezcla = ["correo"] * 8 + ["correo_celular"] * 6 + ["celular"] * 5 + ["cedula"] * 6
    tipos = [mezcla[round(i * len(mezcla) / con_pii)] for i in range(con_pii)] + ["limpia"] * (n - con_pii)
    rng.shuffle(tipos)
    salida = []
    for tipo, cliente in zip(tipos, clientes, strict=True):
        nombre = _ascii(rng.choice(_NOMBRES))
        correo = f"{nombre.lower()}{rng.randrange(10, 100)}@{rng.choice(DOMINIOS)}"
        celular = cliente.celular if rng.random() < 0.5 else _celular(rng)
        cedula = f"{rng.randrange(10**7, 10**8)}"
        salida.append({
            "limpia": rng.choice(_OBSERVACIONES_LIMPIAS),
            "correo": f"Escribir a {correo} para cortes programados",
            "correo_celular": f"Enviar copia a {correo} o llamar al {celular}",
            "celular": f"Contactar a {nombre} al {celular}",
            "cedula": f"Representante identificado con CC{rng.choice((' ', ': '))}{cedula}",
        }[tipo])
    return salida, con_pii


def maestros(semilla: int, n: int) -> Maestros:
    rng = _rng(semilla, "maestros")
    ids = [f"CO7{x:09d}" for x in rng.sample(range(10**9), n)]
    sics = [f"frt{x:05d}" for x in rng.sample(range(10**5), n)]
    clientes_ids = [f"CLI{x:04d}" for x in sorted(rng.sample(range(1, 10**4), n))]
    razones = _razones(rng, n)
    for i in range(min(3, n // 10)):  # razones sociales repetidas con NIT distinto
        razones[n - 1 - i] = razones[i]
    clientes = []
    for i, cid in enumerate(clientes_ids):
        base = f"95{rng.randrange(10**7):07d}"
        usuario = f"{rng.choice(_USUARIOS)}{rng.randrange(1, 100)}"
        clientes.append(Cliente(cid, razones[i], base, digito_verificacion(base),
                                f"{usuario}@{rng.choice(DOMINIOS)}", _celular(rng)))
    malo = clientes[n // 2]  # un NIT con DV invalido
    clientes[n // 2] = Cliente(malo.cliente_id, malo.razon_social, malo.nit_base, (malo.nit_dv + 1) % 10, malo.email,
                               malo.celular)
    repetido = clientes[1]  # un correo repetido en dos Clientes
    clientes[n - 2] = Cliente(**{**clientes[n - 2].__dict__, "email": repetido.email})
    observaciones, con_pii = _observaciones(rng, n, clientes)

    filas_cliente = [["cliente_id", "razon_social", "nit", "email_contacto", "celular_contacto", "direccion",
                      "ciudad", "departamento", "representante_legal", "observaciones"]]
    hostiles = rng.sample(range(n), 4)
    for i, c in enumerate(clientes):
        ciudad, departamento = rng.choice(_CIUDADES)
        direccion = f"Cra {rng.randrange(1, 120)} # {rng.randrange(1, 150)}-{rng.randrange(1, 99)}"
        if i == n - 3:
            direccion = f"Km {rng.randrange(2, 40)} vía {rng.choice(_CIUDADES)[0]}"
        razon = c.razon_social
        if i == hostiles[0]:  # comillas anidadas y salto de linea dentro del campo citado
            razon = f'Inversiones "La {rng.choice(_NOMBRES_FANTASIA)}"\nSucursal "Norte" S.A.S.'
        elif i == hostiles[1]:  # direccion con comillas anidadas
            direccion = f'Cra {rng.randrange(1, 120)} # {rng.randrange(1, 150)}-{rng.randrange(1, 99)} "Bodega 3"'
        elif i == hostiles[2]:  # cadena larguisima
            razon = ("Comercializadora Ábaco y Cía " * 58).strip()
        elif i == hostiles[3]:  # aspecto de inyeccion SQL
            razon = f"{rng.choice(_RUBROS)} El Tornillo'); DROP TABLE core.dim_cliente; --"
        nit = _nit_texto(c.nit_base, c.nit_dv, rng.choices(range(4), (12, 10, 9, 9))[0])
        filas_cliente.append([c.cliente_id, razon, nit, c.email, c.celular, direccion, ciudad, departamento,
                              _nombre(rng), observaciones[i]])

    fronteras = []
    asignacion = list(clientes_ids)
    rng.shuffle(asignacion)
    for fid, sic, cid in zip(ids, sics, asignacion, strict=True):
        nivel = rng.uniform(60, 700)
        cuarto = nivel / 4
        fronteras.append(Frontera(fid, sic, cid, nivel, Decimal(f"{cuarto * rng.uniform(0.25, 0.5):.1f}"),
                                  Decimal(f"{cuarto * rng.uniform(4, 12):.1f}"), _serial(rng),
                                  f"{rng.randrange(1, 4)}.{rng.randrange(10)}.{rng.randrange(10)}"))
    filas_frontera = [["frontera_id", "codigo_sic", "cliente_id", "mercado", "operador_red", "nivel_tension",
                       "fecha_registro", "estado"]]
    filas_limite = [["frontera_id", "min_kwh", "max_kwh"]]
    for f in fronteras:
        registro = date(2024, 1, 1) + timedelta(days=rng.randrange(700))
        filas_frontera.append([f.frontera_id, f.codigo_sic, f.cliente_id,
                               rng.choices(("no_regulado", "regulado"), (78, 22))[0], rng.choice(_OPERADORES),
                               str(rng.randint(1, 4)), registro.isoformat(), "ACTIVA"])
        filas_limite.append([f.frontera_id, str(f.min_kwh), str(f.max_kwh)])
    archivos = {
        "maestros/clientes.csv": _csv(filas_cliente).encode("iso-8859-1"),
        "maestros/fronteras.csv": _csv(filas_frontera).encode("ascii"),
        "maestros/limites_frontera.csv": _csv(filas_limite).encode("ascii"),
    }
    return Maestros(tuple(fronteras), {c.cliente_id: c for c in clientes}, archivos, con_pii, 4)


# --- consumos ------------------------------------------------------------------------------------------------
@dataclass
class Mes:
    """Un archivo de consumo y lo plantado en el: Lectura ganadora por (Frontera, hora) y conteos."""
    mes: str
    horas: int
    ganadoras: dict[tuple[str, datetime], Decimal]
    conteos: dict[str, int]
    filas: list[tuple[str, datetime, str, str, int, str]]  # frontera, hora, valor texto, unidad, version, calidad


def _horas(mes: str) -> list[datetime]:
    anio, m = (int(x) for x in mes.split("-"))
    inicio = datetime(anio, m, 1, tzinfo=HORA_LOCAL)
    return [inicio + timedelta(hours=h) for h in range(monthrange(anio, m)[1] * 24)]


def _curva(f: Frontera, hora: datetime, rng: random.Random) -> float:
    """Curva diaria simple: minimo a medianoche, maximo al mediodia (~2.2x), fin de semana mas bajo, ruido."""
    dia = 1 - 0.37 * math.cos(2 * math.pi * hora.hour / 24)
    semana = 0.85 if hora.weekday() >= 5 else 1.0
    return f.nivel_kwh * dia * semana * rng.uniform(0.9, 1.1)


def consumo_mes(semilla: int, m: Maestros, mes: str, etiqueta: str, unidad: bool) -> Mes:
    rng = _rng(semilla, etiqueta, mes)
    n = len(m.fronteras)
    horas = _horas(mes)
    plan = {"hueco": _escala(n, rng.randint(85, 155)), "nulo": _escala(n, rng.randint(10, 20)),
            "negativo": _escala(n, rng.randint(18, 26)), "outlier": _escala(n, rng.randint(8, 17)),
            "exacto": _escala(n, rng.randint(40, 51)), "conflictivo": _escala(n, rng.randint(23, 34)),
            "mwh": _escala(n, 400) if unidad else 0}
    ids = sorted(f.frontera_id for f in m.fronteras)
    por_id = {f.frontera_id: f for f in m.fronteras}
    celdas = [(fid, i) for fid in ids for i in range(len(horas))]
    elegidas = rng.sample(range(len(celdas)), sum(plan.values()))
    tipo: dict[int, str] = {}
    cursor = 0
    for nombre, cantidad in plan.items():
        for c in elegidas[cursor:cursor + cantidad]:
            tipo[c] = nombre
        cursor += cantidad
    ganadoras: dict[tuple[str, datetime], Decimal] = {}
    filas = []
    for k, (fid, i) in enumerate(celdas):
        hora = horas[i]
        valor = _decimal(rng, _curva(por_id[fid], hora, rng))
        texto = str(valor)
        t = tipo.get(k, "normal")
        if t == "hueco":
            continue
        if t in ("nulo", "negativo", "outlier"):
            texto = {"nulo": "", "negativo": f"-{texto}", "outlier": str(rng.randrange(10**8, 10**12))}[t]
            filas.append((fid, hora, texto, "kWh", 1, "OK"))
            continue
        ganadoras[(fid, hora)] = valor
        if t == "mwh":
            filas.append((fid, hora, f"{Decimal(_texto_kwh(valor, 3)) / 1000:.6f}", "MWh", 1, "OK"))
            continue
        if t == "conflictivo":  # misma hora y version, otro valor antes: gana la ultima del archivo
            otro = _decimal(rng, float(valor) * rng.choice((rng.uniform(0.5, 0.9), rng.uniform(1.1, 1.5))))
            filas.append((fid, hora, str(otro), "kWh", 1, "OK"))
        elif t == "exacto":
            filas.append((fid, hora, texto, "kWh", 1, "OK"))
        filas.append((fid, hora, texto, "kWh", 1, "OK"))
    conteos = {"filas": len(filas), "nulos": plan["nulo"], "negativos": plan["negativo"], "outlier": plan["outlier"],
               "mwh_convertidas": plan["mwh"], "duplicados_exactos": plan["exacto"],
               "duplicados_conflictivos": plan["conflictivo"], "huecos_sin_fila": plan["hueco"], "correcciones": 0}
    return Mes(mes, len(horas), ganadoras, conteos, filas)


def _correccion(semilla: int, base: Mes, n: int) -> Mes:
    """Lecturas v2 `CORREGIDA` sobre horas con una sola Lectura valida v1 del mes (Correccion tardia)."""
    rng = _rng(semilla, "correccion", base.mes)
    unicas = {(f, h) for f, h, *_ in base.filas}
    repetidas = _repetidas(base.filas)
    candidatas = sorted(k for k in base.ganadoras if k in unicas and k not in repetidas)
    elegidas = sorted(rng.sample(candidatas, _escala(n, 300)))
    ganadoras = {}
    filas = []
    for fid, hora in elegidas:
        nuevo = Decimal(_texto_kwh(base.ganadoras[(fid, hora)] * Decimal(repr(rng.uniform(0.6, 1.8))), 3))
        ganadoras[(fid, hora)] = nuevo
        filas.append((fid, hora, str(nuevo), "kWh", 2, "CORREGIDA"))
    conteos = {"filas": len(filas), "nulos": 0, "negativos": 0, "outlier": 0, "mwh_convertidas": 0,
               "duplicados_exactos": 0, "duplicados_conflictivos": 0, "huecos_sin_fila": 0,
               "correcciones": len(filas)}
    return Mes(base.mes, base.horas, ganadoras, conteos, filas)


def _repetidas(filas: list[tuple]) -> set[tuple[str, datetime]]:
    vistas: set[tuple[str, datetime]] = set()
    repetidas = set()
    for f, h, *_ in filas:
        (repetidas if (f, h) in vistas else vistas).add((f, h))
    return repetidas


def consumo_csv(mes: Mes, formato: str) -> bytes:
    """Bytes del archivo en uno de los tres formatos del catalogo: base, utc (`;`, coma decimal, `Z`) o unidad."""
    if formato == "utc":
        filas = [["frontera_id", "timestamp_utc", "consumo_kwh", "version_lectura", "calidad_lectura"]]
        filas += [[f, h.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), v.replace(".", ","), str(ver), c]
                  for f, h, v, _, ver, c in mes.filas]
        return _csv(filas, ";").encode("ascii")
    if formato == "unidad":
        filas = [["frontera_id", "timestamp", "kwh", "unidad", "version_lectura", "calidad_lectura"]]
        filas += [[f, h.strftime("%Y-%m-%d %H:%M:%S"), v, u, str(ver), c] for f, h, v, u, ver, c in mes.filas]
        return _csv(filas).encode("ascii")
    filas = [["frontera_id", "timestamp", "consumo_kwh", "version_lectura", "calidad_lectura"]]
    filas += [[f, h.strftime("%Y-%m-%d %H:%M:%S"), v, str(ver), c] for f, h, v, _, ver, c in mes.filas]
    return _csv(filas).encode("ascii")


# --- facturacion ---------------------------------------------------------------------------------------------
def factura_json(semilla: int, m: Maestros, mes: Mes) -> tuple[bytes, list[str]]:
    """Una Factura por Frontera y Facturas huerfanas al final; devuelve los bytes y las Fronteras huerfanas."""
    rng = _rng(semilla, "facturacion", mes.mes)
    n = len(m.fronteras)
    huerfanas = [f"CO8{x:09d}" for x in rng.sample(range(10**9), _escala(n, 3))]
    numeros = sorted(rng.sample(range(1, 10**4), n + len(huerfanas)))
    aaaamm = mes.mes.replace("-", "")
    por_frontera: dict[str, Decimal] = {}
    for (fid, _), v in mes.ganadoras.items():
        por_frontera[fid] = por_frontera.get(fid, Decimal(0)) + v
    facturas = []
    for numero, f in zip(numeros, m.fronteras, strict=False):
        c = m.clientes[f.cliente_id]
        componentes = [{"concepto": k, "valor_cop_kwh": round(rng.uniform(a, b), 2)}
                       for k, a, b in (("G", 300, 380), ("T", 40, 60), ("D", 150, 250), ("C", 50, 90),
                                       ("P", 20, 40), ("R", 30, 80))]
        tarifa = round(sum(x["valor_cop_kwh"] for x in componentes), 2)
        consumo = round(float(por_frontera.get(f.frontera_id, 0)) * rng.uniform(0.97, 1.03), rng.choice((1, 2)))
        facturas.append(_factura(rng, f"F-{aaaamm}-{numero:04d}", f.frontera_id,
                                 _nit_texto(c.nit_base, c.nit_dv, rng.randrange(4)), c.razon_social, c.email,
                                 consumo, tarifa, round(consumo * tarifa, 2), componentes))
    for numero, fid in zip(numeros[n:], huerfanas, strict=True):
        base = f"96{rng.randrange(10**7):07d}"
        consumo = round(rng.uniform(5_000, 300_000), 2)
        tarifa = round(rng.uniform(600, 900), 2)
        facturas.append(_factura(rng, f"F-{aaaamm}-{numero:04d}", fid,
                                 _nit_texto(base, digito_verificacion(base), 1),
                                 f"{rng.choice(_RUBROS)} {rng.choice(_NOMBRES_FANTASIA)} S.A.S.",
                                 f"{rng.choice(_USUARIOS)}@{rng.choice(DOMINIOS)}", consumo, tarifa,
                                 round(consumo * tarifa), []))
    emisor_base = "950000001"
    doc = {"periodo": mes.mes,
           "emisor": {"nombre": "Comercializadora Cauce S.A.S. E.S.P.",
                      "nit": _nit_texto(emisor_base, digito_verificacion(emisor_base), 1)},
           "generado_en": f"{mes.mes}-{rng.randint(25, 28):02d}T{rng.randint(5, 9):02d}:30:00-05:00",
           "facturas": facturas}
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"), huerfanas


def _factura(rng: random.Random, factura_id: str, frontera_id: str, nit: str, razon: str, email: str,
             consumo: float, tarifa: float, total: float | int, componentes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"factura_id": factura_id, "frontera_id": frontera_id,
            "cliente": {"nit": nit, "razon_social": razon, "email_facturacion": email},
            "consumo_facturado_kwh": consumo, "tarifa_cop_kwh": tarifa, "valor_total_cop": total,
            "componentes": componentes,
            "metodo_pago": {"tipo": rng.choice(_TIPOS_PAGO), "cuenta_enmascarada": f"****{rng.randrange(10**4):04d}"}}


# --- stream --------------------------------------------------------------------------------------------------
def _instante(i: int) -> datetime:
    return INICIO_REJILLA + i * INTERVALO


def _evento_id(rng: random.Random, usados: set[str]) -> str:
    while True:
        eid = f"{rng.choice(_MINUSCULAS)}{rng.choice(_MINUSCULAS)}{rng.randrange(10**7):07d}"
        if eid not in usados:
            usados.add(eid)
            return eid


def stream(semilla: int, m: Maestros) -> tuple[bytes, list[dict[str, str]], dict[str, Any]]:
    """Mediciones de la Rejilla con Caidas, fuera de banda, retroactivas, reenvios y malformadas."""
    rng = _rng(semilla, "stream")
    n = len(m.fronteras)
    fronteras = list(m.fronteras)
    orden = list(range(n))
    rng.shuffle(orden)
    n_caidas, n_sueltos, n_retro = _escala(n, 6), _escala(n, 5, 0), _escala(n, 5)
    caidas_f = [fronteras[i] for i in orden[:n_caidas]]
    sueltos_f = [fronteras[i] for i in orden[n_caidas:n_caidas + n_sueltos]]
    retro_f = [fronteras[i] for i in orden[n_caidas + n_sueltos:n_caidas + n_sueltos + n_retro]]
    ausentes: dict[str, set[int]] = {f.frontera_id: set() for f in fronteras}
    alertas = []
    for f in caidas_f:
        largo = rng.randint(RACHA_CAIDA, 9)
        inicio = rng.randint(2, INTERVALOS - largo - 2)
        ausentes[f.frontera_id] = set(range(inicio, inicio + largo))
        alertas.append(("FRONTERA_SIN_REPORTE", f.frontera_id, _instante(inicio + RACHA_CAIDA - 1)))
    for f in sueltos_f:  # un intervalo suelto: no alcanza la racha
        ausentes[f.frontera_id] = {rng.randint(1, INTERVALOS - 2)}

    # valores especiales sobre (Frontera, intervalo) presentes y distintos
    presentes = [(f, i) for f in fronteras for i in range(INTERVALOS) if i not in ausentes[f.frontera_id]]
    n_banda = _escala(n, 22)
    con_banda = rng.sample(fronteras, min(n, max(_escala(n, 10), -(-n_banda // 3))))
    slots_banda: list[tuple[Frontera, int]] = []
    for k in range(n_banda):
        f = con_banda[k % len(con_banda)]
        libres = [i for i in range(INTERVALOS) if i not in ausentes[f.frontera_id]
                  and (f, i) not in slots_banda]
        slots_banda.append((f, rng.choice(libres)))
    resto = [p for p in presentes if p not in slots_banda]
    bajo = rng.sample(resto, _escala(n, 3))
    igual = rng.sample([p for p in resto if p not in bajo], 1)
    especial: dict[tuple[str, int], Decimal | float] = {}
    for f, i in slots_banda:
        especial[(f.frontera_id, i)] = round(float(f.max_kwh) * rng.uniform(1.4, 3.0), 3)
        alertas.append(("CONSUMO_FUERA_DE_BANDA", f.frontera_id, _instante(i)))
    for f, i in bajo:
        especial[(f.frontera_id, i)] = round(float(f.min_kwh) * rng.uniform(0.3, 0.8), 3)
    for f, i in igual:
        especial[(f.frontera_id, i)] = float(f.max_kwh)

    usados: set[str] = set()

    def evento(f: Frontera, ts: str, kwh: Any, firmware: str | None = None, fid: str | None = None) -> str:
        return json.dumps({"evento_id": _evento_id(rng, usados), "frontera_id": fid or f.frontera_id,
                           "ts_medicion": ts, "kwh": kwh,
                           "medidor": {"serial": f.serial, "firmware": firmware or f.firmware}})

    def normal(f: Frontera, i: int) -> float:
        return round(_curva(f, _instante(i), rng) / 4, rng.choices((3, 2, 1), (90, 9, 1))[0])

    grupos: list[list[str]] = []
    for i in range(INTERVALOS):
        grupo = []
        for f in fronteras:
            if i in ausentes[f.frontera_id]:
                continue
            kwh = especial.get((f.frontera_id, i))
            grupo.append(evento(f, _instante(i).isoformat(), normal(f, i) if kwh is None else kwh))
        rng.shuffle(grupo)
        grupos.append(grupo)

    for f in retro_f:  # repite un instante ya emitido de su Frontera, 4 a 10 intervalos despues
        llegada = rng.randint(12, INTERVALOS - 1)
        i = llegada - rng.randint(4, 10)
        firmware = f.firmware if rng.random() < 0.5 else f"{f.firmware[:-1]}{(int(f.firmware[-1]) + 1) % 10}"
        grupos[llegada].insert(rng.randrange(len(grupos[llegada]) + 1),
                               evento(f, _instante(i).isoformat(), normal(f, i), firmware))
        alertas.append(("LECTURA_RETROACTIVA", f.frontera_id, _instante(i)))

    n_reenvios = _escala(n, 2)
    for _ in range(n_reenvios):  # el mismo mensaje dos veces (mismo evento_id)
        origen = rng.randrange(INTERVALOS - 1)
        linea = rng.choice(grupos[origen])
        destino = rng.randint(origen + 1, INTERVALOS - 1)
        grupos[destino].insert(rng.randrange(len(grupos[destino]) + 1), linea)

    f = rng.choice(fronteras)
    malformados = {
        "kwh_nulo": evento(f, _instante(rng.randrange(INTERVALOS)).isoformat(), None),
        "kwh_negativo": evento(f, _instante(rng.randrange(INTERVALOS)).isoformat(), -round(rng.uniform(1, 50), 3)),
        "frontera_desconocida": evento(f, _instante(rng.randrange(INTERVALOS)).isoformat(), 10.5,
                                       fid=f"CO8{rng.randrange(10**9):09d}"),
        "ts_no_alineado": evento(f, (_instante(rng.randrange(INTERVALOS)) + timedelta(minutes=7)).isoformat(), 12.25),
    }
    for linea in malformados.values():
        g = rng.randrange(INTERVALOS)
        grupos[g].insert(rng.randrange(len(grupos[g]) + 1), linea)

    lineas = [x for g in grupos for x in g]
    esperadas = [{"tipo": t, "frontera_id": fid, "ts": ts.isoformat()} for t, fid, ts in sorted(set(alertas))]
    resumen = {"eventos": len(lineas), "duplicados": n_reenvios,
               "invalidos_por_motivo": dict(sorted((k, 1) for k in malformados)),
               "retroactivas": len(retro_f), "caidas": len(caidas_f), "fuera_de_banda": n_banda,
               "bajo_banda": len(bajo), "igual_al_maximo": len(igual)}
    return ("\n".join(lineas) + "\n").encode("ascii"), esperadas, resumen


# --- Verdad plantada -----------------------------------------------------------------------------------------
def _verdad_consumo(meses: list[tuple[str, Mes]], m: Maestros) -> dict[str, Any]:
    """Conteos de consumo de los archivos generados (en orden de nombre) y Lecturas ganadoras por hora."""
    ganadoras: dict[tuple[str, datetime], Decimal] = {}
    for _, mes in meses:
        ganadoras.update(mes.ganadoras)  # la correccion (v2) se arma despues y pisa a su mes
    horas_mes = {mes.mes: mes.horas for _, mes in meses}
    por_fm: dict[tuple[str, str], list[Decimal]] = {}
    for (fid, hora), v in ganadoras.items():
        por_fm.setdefault((fid, hora.strftime("%Y-%m")), []).append(v)
    mensual = [{"frontera_id": fid, "mes": mes, "consumo_kwh": f"{sum(vs, Decimal(0)):.3f}",
                "horas_con_dato": len(vs)} for (fid, mes), vs in sorted(por_fm.items())]
    huecos = {f.frontera_id: sum(h - len(por_fm.get((f.frontera_id, mes), ())) for mes, h in horas_mes.items())
              for f in m.fronteras}
    suma = {k: sum(mes.conteos[k] for _, mes in meses) for k in meses[0][1].conteos}
    dq = {"filas_leidas": suma["filas"], "duplicados_exactos": suma["duplicados_exactos"],
          "duplicados_conflictivos": suma["duplicados_conflictivos"], "registros_negativos": suma["negativos"],
          "registros_nulos": suma["nulos"], "registros_outlier": suma["outlier"],
          "registros_mwh_convertidos": suma["mwh_convertidas"],
          "correcciones_tardias_aplicadas": suma["correcciones"],
          "horas_sin_lectura_valida_total": sum(huecos.values()),
          "horas_sin_lectura_valida_por_frontera": dict(sorted(huecos.items()))}
    return {"por_archivo": {ruta: mes.conteos for ruta, mes in meses}, "dq_report": dq, "consumo_mensual": mensual,
            "cuarentena": {"consumo": {"consumo_nulo": suma["nulos"], "consumo_negativo": suma["negativos"],
                                       "consumo_outlier": suma["outlier"]}}}


def _archivos(archivos: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {r: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for r, b in sorted(archivos.items())}


def generar(semilla: int = SEMILLA, n: int = FRONTERAS) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Todos los archivos de `data/` (ruta relativa -> bytes) y su Verdad plantada."""
    m = maestros(semilla, n)
    archivos = dict(m.archivos)
    meses: list[tuple[str, Mes]] = []
    huerfanas: list[str] = []
    n_facturas = 0
    for mes in MESES:
        formato = FORMATO_DEL_MES[mes]
        datos = consumo_mes(semilla, m, mes, "consumo", formato == "unidad")
        ruta = f"consumos/consumos_{mes}.csv"
        archivos[ruta] = consumo_csv(datos, formato)
        meses.append((ruta, datos))
        if mes == MES_CORREGIDO:
            correccion = _correccion(semilla, datos, n)
            ruta = f"consumos/consumos_{mes}_correccion.csv"
            archivos[ruta] = consumo_csv(correccion, "base")
            meses.append((ruta, correccion))
    for ruta_mes, datos in meses:
        if ruta_mes.endswith("_correccion.csv"):
            continue
        ganadoras = dict(datos.ganadoras)
        for r, d in meses:
            if r.endswith("_correccion.csv") and d.mes == datos.mes:
                ganadoras.update(d.ganadoras)
        facturable = Mes(datos.mes, datos.horas, ganadoras, datos.conteos, [])
        archivos[f"facturacion/facturacion_{datos.mes}.json"], h = factura_json(semilla, m, facturable)
        huerfanas += h
        n_facturas += n + len(h)
    archivos["eventos/mediciones_stream.ndjson"], alertas, resumen = stream(semilla, m)

    consumo = _verdad_consumo(meses, m)
    consumo["dq_report"].update({"fronteras_huerfanas_facturacion": len(set(huerfanas)),
                                 "clientes_con_pii_en_texto_libre": m.clientes_con_pii})
    consumo["dq_report"] = dict(sorted(consumo["dq_report"].items()))
    consumo["cuarentena"]["factura"] = {"frontera_huerfana": len(huerfanas)}
    verdad = {"esquema": ESQUEMA_VERDAD, "semilla": semilla, "fronteras": n, "modo": "completo",
              "archivos": _archivos(archivos), **consumo,
              "maestros": {"clientes": n, "fronteras": n, "limites": n, "clientes_hostiles": m.hostiles},
              "facturacion": {"facturas": n_facturas, "huerfanas": len(huerfanas),
                              "fronteras_huerfanas": len(set(huerfanas))},
              "alertas": alertas, "stream": resumen}
    return archivos, verdad


def generar_mes(semilla: int, n: int, mes: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Un mes nuevo de consumo en formato base, sobre las Fronteras de los maestros de la misma semilla."""
    m = maestros(semilla, n)
    datos = consumo_mes(semilla, m, mes, "mes", False)
    ruta = f"consumos/consumos_{mes}.csv"
    archivos = {ruta: consumo_csv(datos, "base")}
    consumo = _verdad_consumo([(ruta, datos)], m)
    return archivos, {"esquema": ESQUEMA_VERDAD, "semilla": semilla, "fronteras": n, "modo": "mes",
                      "archivos": _archivos(archivos), **consumo}


def corromper(ruta: Path, forzar: bool) -> Path:
    """Copia truncada a media linea de `ruta` (CSV o JSON), junto a ella con sufijo `_corrupto`. El original no se
    toca. La copia termina sin salto final, asi que el pipeline la rechaza entera."""
    if ruta.suffix not in (".csv", ".json"):
        raise SystemExit(f"--corromper: solo .csv o .json ({ruta.name})")
    datos = ruta.read_bytes()
    destino = ruta.with_name(f"{ruta.stem}_corrupto{ruta.suffix}")
    if destino.exists() and not forzar:
        raise SystemExit(f"{destino} ya existe (usar --forzar)")
    lineas = datos.splitlines(keepends=True)
    if len(lineas) < 2:
        raise SystemExit(f"--corromper: {ruta.name} tiene menos de 2 lineas")
    medio = len(lineas) // 2
    corte = sum(len(x) for x in lineas[:medio]) + max(1, len(lineas[medio].rstrip(b"\r\n")) // 2)
    destino.write_bytes(datos[:corte])
    return destino


# --- CLI -----------------------------------------------------------------------------------------------------
def _mes(texto: str) -> str:
    try:
        datetime.strptime(texto, "%Y-%m")
    except ValueError:
        raise argparse.ArgumentTypeError("se espera AAAA-MM") from None
    return texto


def _fronteras(texto: str) -> int:
    n = int(texto)
    if n < MINIMO_FRONTERAS:
        raise argparse.ArgumentTypeError(f"minimo {MINIMO_FRONTERAS}")
    return n


def _escribir(destino: Path, archivos: dict[str, bytes]) -> None:
    for ruta, datos in archivos.items():
        (destino / ruta).parent.mkdir(parents=True, exist_ok=True)
        (destino / ruta).write_bytes(datos)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Datos sinteticos sucios para data/ (ver docstring del modulo).")
    ap.add_argument("destino", nargs="?", default="data", type=Path)
    ap.add_argument("--forzar", action="store_true", help="escribir aunque el destino ya tenga archivos")
    ap.add_argument("--semilla", type=int, default=SEMILLA)
    ap.add_argument("--fronteras", type=_fronteras, default=FRONTERAS)
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--mes", type=_mes, help="solo consumos/consumos_AAAA-MM.csv, coherente con los maestros")
    modo.add_argument("--corromper", type=Path, metavar="RUTA", help="copia truncada de RUTA (_corrupto)")
    ap.add_argument("--verdad", type=Path, metavar="RUTA", help="manifiesto de Verdad plantada (fuera del destino)")
    a = ap.parse_args(argv)

    if a.corromper:
        print(f"copia danada: {corromper(a.corromper, a.forzar)}")
        return 0
    destino = a.destino.resolve()
    if a.verdad and a.verdad.resolve().is_relative_to(destino):
        print("--verdad no puede quedar dentro del destino", file=sys.stderr)
        return 2
    if a.mes:
        archivos, verdad = generar_mes(a.semilla, a.fronteras, a.mes)
        existentes = [r for r in archivos if (destino / r).exists()]
    else:
        archivos, verdad = generar(a.semilla, a.fronteras)
        existentes = [str(p) for p in destino.rglob("*") if p.is_file()] if destino.is_dir() else []
    if existentes and not a.forzar:
        print(f"{a.destino} ya tiene archivos ({len(existentes)}); usar --forzar para escribir igual",
              file=sys.stderr)
        return 1
    _escribir(destino, archivos)
    if a.verdad:
        a.verdad.parent.mkdir(parents=True, exist_ok=True)
        a.verdad.write_text(json.dumps(verdad, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(archivos)} archivos en {a.destino} (semilla {a.semilla}, {a.fronteras} Fronteras)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
