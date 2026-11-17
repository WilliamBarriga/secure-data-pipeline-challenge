"""Apoyo compartido de los tests: rutas, encabezados del catalogo, llaves de prueba y helpers de proceso y de base.

No es un modulo del dominio: solo evita que cada archivo de test copie lo mismo. Los fixtures viven en los
`conftest.py`; aca van constantes y funciones simples.
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from cauce.aplicacion import corrida
from cauce.bordes import almacen
from cauce.nucleo import custodia, fuentes

RAIZ = Path(__file__).resolve().parent.parent
GENERADOR = RAIZ / "tools" / "sinteticos.py"
SEMILLA = 2026  # fija: misma semilla, mismos bytes y misma Verdad plantada en cada `make test`
CLASIF = corrida.leer_clasificacion(RAIZ / "data_classification.yaml")
BOGOTA = timezone(timedelta(hours=-5))
NDJSON = Path("eventos") / "mediciones_stream.ndjson"
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")

# encabezados del catalogo cerrado de `fuentes`, con salto de linea
H_FRONTERA = "frontera_id,codigo_sic,cliente_id,mercado,operador_red,nivel_tension,fecha_registro,estado\n"
H_LIMITE = "frontera_id,min_kwh,max_kwh\n"
H_CLIENTE = ("cliente_id,razon_social,nit,email_contacto,celular_contacto,"
             "direccion,ciudad,departamento,representante_legal,observaciones\n")
H_CONSUMO = "frontera_id,timestamp,consumo_kwh,version_lectura,calidad_lectura\n"
H_CONSUMO_MWH = "frontera_id,timestamp,kwh,unidad,version_lectura,calidad_lectura\n"

F_CONSUMO = "CO0000000011"


def fila_consumo(valor, ts="2026-01-01 00:00:00", version=1, f=F_CONSUMO, calidad="OK") -> str:
    """Una fila del formato de consumo base (`H_CONSUMO`)."""
    return f"{f},{ts},{valor},{version},{calidad}\n"


# llaves de prueba (base64 de 32 bytes): sin DEK abierta, solo sirven para fuentes sin PII
KEK_PRUEBA, K_BIDX_PRUEBA = "A" * 43 + "=", "B" * 43 + "="


def custodio_prueba() -> custodia.Custodio:
    return custodia.Custodio(KEK_PRUEBA, K_BIDX_PRUEBA)


# --- base ----------------------------------------------------------------------------------------
def filas(con, sql, *args) -> list[tuple]:
    """Filas de una consulta sobre una conexion abierta; [] si la sentencia no devuelve filas."""
    with con.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def consultar(cfg, sql, *args, rol=None) -> list[tuple]:
    """Filas de una consulta en cauce_test con una conexion propia (superusuario si no se da `rol`)."""
    con = almacen.conectar(cfg, rol or cfg.superusuario, "cauce_test")
    try:
        return filas(con, sql, *args)
    finally:
        con.close()


def cargar_texto(con, fuente: str, nombre: str, texto: str, *, custodio=None, sha: str | None = None,
                 codificacion: str = "utf-8") -> int:
    """leer -> custodiar -> cargar -> commit de un archivo en memoria, sin `ingerir`. Devuelve `archivo_id`."""
    leido = fuentes.leer(fuente, nombre, texto.encode(codificacion))
    custodiado = custodia.custodiar(leido, CLASIF, custodio or custodio_prueba(), fuente=fuente)
    archivo_id = almacen.cargar(con, fuente, nombre, sha or uuid.uuid4().hex * 2, custodiado, uuid.uuid4())
    con.commit()
    return archivo_id


# --- proceso `python -m cauce` ----------------------------------------------------------------------
def correr(data: Path, *args: str, extra: dict[str, str] | None = None, timeout: int = 600):
    """`python -m cauce <args>` (por defecto `run`) contra cauce_test con `CAUCE_DATA=data`."""
    env = {**os.environ, "PYTHONPATH": str(RAIZ / "src"), "CAUCE_RAIZ": str(RAIZ), "PG_DATABASE": "cauce_test",
           "CAUCE_DATA": str(data), **(extra or {})}
    return subprocess.run([sys.executable, "-m", "cauce", *(args or ("run",))], env=env, cwd=RAIZ,
                          capture_output=True, text=True, timeout=timeout)


def salida(nombre: str) -> bytes:
    return (Path(os.environ["CAUCE_OUT"]) / nombre).read_bytes()


def metrics() -> dict:
    return json.loads(salida("metrics.json"))


def log() -> list[dict]:
    return [json.loads(x) for x in Path(os.environ["CAUCE_LOG"]).read_text().splitlines()]


@dataclass(frozen=True)
class Sinteticos:
    """Datos sinteticos de la sesion (`data`) y su Verdad plantada (`verdad`, esquema `cauce.verdad/1`, ver el
    docstring de `tools/sinteticos.py`). El manifiesto vive fuera de `data`."""
    data: Path
    verdad: dict

    def leer(self, ruta: str) -> bytes:
        return (self.data / ruta).read_bytes()

    def archivos(self, *prefijos: str) -> list[str]:
        """Rutas relativas de la Verdad plantada (solo las de `prefijos`, si se dan)."""
        return [r for r in self.verdad["archivos"] if not prefijos or r.startswith(prefijos)]


def generar_sinteticos(base: Path) -> Sinteticos:
    """Corre la CLI del generador con la semilla fija: Datos sinteticos en `base/data`, manifiesto en
    `base/verdad.json` (fuera de `data`)."""
    r = subprocess.run([sys.executable, str(GENERADOR), str(base / "data"), "--semilla", str(SEMILLA),
                        "--verdad", str(base / "verdad.json")], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        pytest.fail(f"el generador de Datos sinteticos fallo: {r.stderr}", pytrace=False)
    verdad = json.loads((base / "verdad.json").read_text(encoding="utf-8"))
    assert verdad["esquema"] == "cauce.verdad/1" and verdad["semilla"] == SEMILLA
    return Sinteticos(base / "data", verdad)


def copiar(origen: Sinteticos, destino: Path, *prefijos: str) -> Path:
    """Copia a `destino` los archivos de los Datos sinteticos (solo los de `prefijos`, si se dan). Falla si no hay
    ninguno: cada test arma su propio `data/` y puede modificarlo sin tocar el de la sesion."""
    rutas = origen.archivos(*prefijos)
    if not rutas:
        pytest.fail(f"los Datos sinteticos no tienen archivos para {prefijos}", pytrace=False)
    for ruta in rutas:
        (destino / ruta).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(origen.data / ruta, destino / ruta)
    return destino


MAESTROS_FRONTERAS = ("maestros/fronteras.csv", "maestros/limites_frontera.csv")
MAESTROS = (*MAESTROS_FRONTERAS, "maestros/clientes.csv")


# --- canario de PII: valores derivados de los Datos sinteticos en tiempo de test (nunca copiados aca) ---------
CAMPOS_PII_CLIENTE = ("nit", "email_contacto", "celular_contacto", "direccion", "representante_legal")
_EN_TEXTO = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+|\d{7,}")


def valores_pii_clientes(datos: Path) -> set[str]:
    """Valores PII de `datos/maestros/clientes.csv` (campos PII, emails y numeros largos de `observaciones`), con
    y sin separadores, de 7+ caracteres: ninguno debe aparecer en tablas, log ni out/."""
    with open(datos / "maestros" / "clientes.csv", encoding="iso-8859-1", newline="") as fh:
        filas = list(csv.DictReader(fh))
    valores: set[str] = set()
    for f in filas:
        for c in CAMPOS_PII_CLIENTE:
            if f[c].strip():
                valores.add(f[c].strip())
        valores.update(_EN_TEXTO.findall(f["observaciones"]))
    sin_separadores = {re.sub(r"[^0-9A-Za-z]", "", v) for v in valores}
    return {v for v in valores | sin_separadores if len(v) >= 7}


def valores_pii_facturacion(datos: Path) -> dict[str, set[str]]:
    """Valores de cliente y de cuenta de los JSON de `datos/facturacion`. `razon_social` va aparte: es Confidencial,
    y en core la razon social del maestro de clientes (en claro por clasificacion) puede coincidir."""
    nombres = {"cliente": set(), "razon_social": set()}
    for archivo in sorted((datos / "facturacion").glob("*.json")):
        for f in json.loads(archivo.read_text())["facturas"]:
            nombres["cliente"] |= {f["cliente"]["nit"], f["cliente"]["email_facturacion"],
                                   f["metodo_pago"]["cuenta_enmascarada"]}
            nombres["razon_social"].add(f["cliente"]["razon_social"])
    return nombres


def volcado_superusuario(cfg, tablas: list[str] | None = None) -> str:
    """Texto de todas las filas (como superusuario) de `tablas` (`esquema.tabla`); sin `tablas`, de todas las
    tablas y vistas de las zonas de `almacen`."""
    con = almacen.conectar(cfg, cfg.superusuario, "cauce_test")
    try:
        with con.cursor() as cur:
            if tablas is None:
                cur.execute("select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                            "where c.relkind in ('r', 'v') and n.nspname = any(%s) order by 1, 2",
                            (list(almacen.ZONAS),))
                tablas = [f'"{esquema}"."{tabla}"' for esquema, tabla in cur.fetchall()]
            partes = []
            for t in tablas:
                cur.execute(f"select t::text from {t} t")  # noqa: S608 (nombres de pg_class o del test)
                partes.append("\n".join(r[0] for r in cur.fetchall()))
        return "\n".join(partes)
    finally:
        con.close()


def cambiar_banda(datos: Path, min_kwh: str, max_kwh: str) -> str:
    """Reescribe la Banda operativa de la primera Frontera de `datos/maestros/limites_frontera.csv` (un maestro
    modificado) y devuelve su `frontera_id`."""
    lim = datos / "maestros" / "limites_frontera.csv"
    encabezado, primera, *resto = lim.read_text().splitlines(keepends=True)
    frontera_id = primera.split(",")[0]
    lim.write_text("".join([encabezado, f"{frontera_id},{min_kwh},{max_kwh}\n", *resto]))
    return frontera_id


def consumo_mensual_esperado(verdad: dict, *meses: str) -> list[list[str]]:
    """Filas de `out/consumo_mensual.csv` (sin encabezado) que planto el generador, solo de `meses` si se dan."""
    return [[f["frontera_id"], f["mes"], f["consumo_kwh"], str(f["horas_con_dato"])]
            for f in verdad["consumo_mensual"] if not meses or f["mes"] in meses]
