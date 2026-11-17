"""Seam 2: la CLI del generador de Datos sinteticos (`tools/sinteticos.py`). Sin base: los archivos generados se
leen con el nucleo (`fuentes.leer`, `alertas.Detector`, `pii.enmascarar`) y se contrastan con la Verdad plantada."""
import ast
import csv
import io
import json
import re
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from cauce.nucleo import alertas, fuentes, pii

RAIZ = Path(__file__).resolve().parents[2]
GENERADOR = RAIZ / "tools" / "sinteticos.py"
ARCHIVOS = ("maestros/clientes.csv", "maestros/fronteras.csv", "maestros/limites_frontera.csv",
            "consumos/consumos_2026-01.csv", "consumos/consumos_2026-02.csv", "consumos/consumos_2026-03.csv",
            "consumos/consumos_2026-04.csv", "consumos/consumos_2026-04_correccion.csv",
            "facturacion/facturacion_2026-01.json", "facturacion/facturacion_2026-02.json",
            "facturacion/facturacion_2026-03.json", "facturacion/facturacion_2026-04.json",
            "eventos/mediciones_stream.ndjson")
DOMINIO_RESERVADO = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|[\w-]+\.(?:test|example|invalid))$")
CORREO = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def generar(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GENERADOR), *args], capture_output=True, text=True, cwd=cwd,
                          timeout=120)


def todos(destino: Path) -> dict[str, bytes]:
    return {str(p.relative_to(destino)): p.read_bytes() for p in sorted(destino.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def sintetico(tmp_path_factory) -> tuple[Path, dict]:
    """Datos sinteticos por defecto (semilla y volumen por defecto) y su Verdad plantada."""
    base = tmp_path_factory.mktemp("sintetico")
    r = generar(str(base / "data"), "--verdad", str(base / "verdad.json"))
    assert r.returncode == 0, r.stderr
    return base / "data", json.loads((base / "verdad.json").read_text(encoding="utf-8"))


def leer(data: Path, fuente: str, ruta: str) -> fuentes.Resultado:
    return fuentes.leer(fuente, Path(ruta).name, (data / ruta).read_bytes())


def test_escribe_el_layout_de_la_entrada(sintetico):
    data, _ = sintetico
    assert tuple(sorted(todos(data))) == tuple(sorted(ARCHIVOS))


def test_misma_semilla_da_bytes_identicos(sintetico, tmp_path):
    data, _ = sintetico
    assert generar(str(tmp_path / "otra")).returncode == 0
    assert todos(tmp_path / "otra") == todos(data)


def test_otra_semilla_da_otros_bytes(tmp_path):
    for semilla in ("1", "2"):
        assert generar(str(tmp_path / semilla), "--semilla", semilla, "--fronteras", "10").returncode == 0
    assert todos(tmp_path / "1")["maestros/fronteras.csv"] != todos(tmp_path / "2")["maestros/fronteras.csv"]


def test_aborta_si_el_destino_tiene_archivos_salvo_con_forzar(tmp_path):
    previo = tmp_path / "data" / "consumos" / "ya_estaba.csv"
    previo.parent.mkdir(parents=True)
    previo.write_bytes(b"x")
    r = generar(str(tmp_path / "data"), "--fronteras", "10")
    assert r.returncode != 0 and "--forzar" in r.stderr
    assert todos(tmp_path / "data") == {"consumos/ya_estaba.csv": b"x"}
    assert generar(str(tmp_path / "data"), "--fronteras", "10", "--forzar").returncode == 0
    assert set(ARCHIVOS) <= set(todos(tmp_path / "data"))


def test_la_verdad_nunca_queda_dentro_del_destino(tmp_path):
    r = generar(str(tmp_path / "data"), "--fronteras", "10", "--verdad", str(tmp_path / "data" / "verdad.json"))
    assert r.returncode != 0
    assert not (tmp_path / "data").exists()


def test_fronteras_parametriza_el_volumen(tmp_path):
    assert generar(str(tmp_path / "data"), "--fronteras", "12", "--verdad", str(tmp_path / "v.json")).returncode == 0
    fronteras = leer(tmp_path / "data", "frontera", "maestros/fronteras.csv")
    assert len(fronteras.registros) == 12
    verdad = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    assert verdad["fronteras"] == 12 and len(verdad["dq_report"]["horas_sin_lectura_valida_por_frontera"]) == 12


def test_el_volumen_por_defecto_es_del_orden_del_original(sintetico):
    data, verdad = sintetico
    assert len(leer(data, "frontera", "maestros/fronteras.csv").registros) == 40
    assert 100_000 < verdad["dq_report"]["filas_leidas"] < 130_000  # 4 meses horarios x 40 Fronteras
    assert 3_700 < verdad["stream"]["eventos"] < 3_900


def test_maestros_y_consumos_los_acepta_el_catalogo_con_los_conteos_plantados(sintetico):
    data, verdad = sintetico
    for fuente, ruta in (("cliente", "maestros/clientes.csv"), ("frontera", "maestros/fronteras.csv"),
                         ("limite", "maestros/limites_frontera.csv")):
        r = leer(data, fuente, ruta)
        assert r.archivo_rechazado is None and not r.rechazos and len(r.registros) == 40, ruta
    for ruta, plantado in verdad["por_archivo"].items():
        r = leer(data, "consumo", ruta)
        assert r.archivo_rechazado is None, ruta
        assert (r.conteos["leidas"], r.conteos.get("nulos", 0), r.conteos.get("negativos", 0),
                r.conteos.get("outlier", 0), r.conteos.get("mwh_convertidas", 0)) == (
            plantado["filas"], plantado["nulos"], plantado["negativos"], plantado["outlier"],
            plantado["mwh_convertidas"]), ruta


def test_un_formato_de_consumo_distinto_por_defecto(sintetico):
    data, _ = sintetico
    primeras = {m: (data / f"consumos/consumos_{m}.csv").read_bytes().split(b"\r\n", 1)[0]
                for m in ("2026-01", "2026-02", "2026-03")}
    assert primeras == {"2026-01": b"frontera_id,timestamp,consumo_kwh,version_lectura,calidad_lectura",
                        "2026-02": b"frontera_id;timestamp_utc;consumo_kwh;version_lectura;calidad_lectura",
                        "2026-03": b"frontera_id,timestamp,kwh,unidad,version_lectura,calidad_lectura"}
    with pytest.raises(UnicodeDecodeError):
        (data / "maestros/clientes.csv").read_bytes().decode("utf-8")  # latin-1


def test_consumo_mensual_y_huecos_de_la_verdad_salen_de_las_lecturas(sintetico):
    """B3 independiente sobre lo que lee `fuentes`: mayor version y, a igual version, la ultima en orden de archivo."""
    data, verdad = sintetico
    ganadora: dict[tuple, tuple] = {}
    exactos = conflictivos = 0
    for ruta in sorted(verdad["por_archivo"]):
        for reg in leer(data, "consumo", ruta).registros:
            c = reg.campos
            clave, nueva = (c["frontera_id"], c["ts_hora"]), (c["version_lectura"], c["consumo_kwh"])
            previa = ganadora.get(clave)
            if previa is not None and previa[0] == nueva[0]:
                exactos += previa[1] == nueva[1]
                conflictivos += previa[1] != nueva[1]
            if previa is None or nueva[0] >= previa[0]:
                ganadora[clave] = nueva
    mensual: dict[tuple, list] = {}
    for (f, ts), (_, v) in ganadora.items():
        mensual.setdefault((f, ts.strftime("%Y-%m")), []).append(v)
    esperado = [{"frontera_id": f, "mes": m, "consumo_kwh": f"{sum(vs, Decimal(0)):.3f}", "horas_con_dato": len(vs)}
                for (f, m), vs in sorted(mensual.items())]
    assert verdad["consumo_mensual"] == esperado
    dq = verdad["dq_report"]
    assert (dq["duplicados_exactos"], dq["duplicados_conflictivos"]) == (exactos, conflictivos)
    horas_totales = 40 * (744 + 672 + 744 + 720)
    assert dq["horas_sin_lectura_valida_total"] == horas_totales - len(ganadora)
    assert dq["correcciones_tardias_aplicadas"] == verdad["por_archivo"][
        "consumos/consumos_2026-04_correccion.csv"]["filas"] > 0


def test_facturas_huerfanas_y_pii_en_texto_libre_de_la_verdad(sintetico):
    data, verdad = sintetico
    maestro = {r.campos["frontera_id"] for r in leer(data, "frontera", "maestros/fronteras.csv").registros}
    huerfanas = []
    for m in ("2026-01", "2026-02", "2026-03", "2026-04"):
        r = leer(data, "factura", f"facturacion/facturacion_{m}.json")
        assert r.archivo_rechazado is None and not r.rechazos
        huerfanas += [x.campos["frontera_id"] for x in r.registros if x.campos["frontera_id"] not in maestro]
    assert verdad["dq_report"]["fronteras_huerfanas_facturacion"] == len(set(huerfanas)) > 0
    assert verdad["cuarentena"]["factura"]["frontera_huerfana"] == len(huerfanas)
    clientes = leer(data, "cliente", "maestros/clientes.csv").registros
    con_pii = sum(bool(pii.enmascarar(r.campos["observaciones"]).hallazgos) for r in clientes)
    assert verdad["dq_report"]["clientes_con_pii_en_texto_libre"] == con_pii > 0


def test_texto_hostil_en_clientes(sintetico):
    data, _ = sintetico
    texto = (data / "maestros/clientes.csv").read_bytes().decode("iso-8859-1")
    campos = [c for fila in csv.reader(io.StringIO(texto, newline="")) for c in fila]
    assert any('"' in c and "\n" in c for c in campos)
    assert any(len(c) > 1_000 for c in campos)
    assert any("DROP TABLE" in c for c in campos)


def test_alertas_del_stream_son_las_de_la_verdad(sintetico):
    data, verdad = sintetico
    maestro = [r.campos for r in leer(data, "limite", "maestros/limites_frontera.csv").registros]
    detector = alertas.Detector([x["frontera_id"] for x in maestro], {x["frontera_id"]: x["max_kwh"] for x in maestro})
    lineas = (data / "eventos/mediciones_stream.ndjson").read_bytes().splitlines()
    for linea in lineas:
        detector.procesar(linea)
    cierre = detector.cerrar()
    assert [{"tipo": a.tipo.value, "frontera_id": a.frontera_id, "ts": a.ts.isoformat()}
            for a in cierre.alertas] == verdad["alertas"]
    stream = verdad["stream"]
    assert (cierre.leidos, cierre.duplicados, dict(cierre.descartados)) == (
        len(lineas), stream["duplicados"], stream["invalidos_por_motivo"])
    por_tipo = Counter(a["tipo"] for a in verdad["alertas"])
    assert (por_tipo["FRONTERA_SIN_REPORTE"], por_tipo["CONSUMO_FUERA_DE_BANDA"], por_tipo["LECTURA_RETROACTIVA"]) == (
        stream["caidas"], stream["fuera_de_banda"], stream["retroactivas"])
    assert all(a["ts"].startswith("2026-06-01T") and a["ts"].endswith("-05:00") for a in verdad["alertas"])


def test_correos_en_dominios_reservados_y_celulares_en_rango_no_asignado(sintetico):
    data, _ = sintetico
    texto = "\n".join(b.decode("iso-8859-1" if r.endswith("clientes.csv") else "utf-8")
                      for r, b in todos(data).items())
    correos = CORREO.findall(texto)
    assert correos and all(DOMINIO_RESERVADO.search(c) for c in correos), [c for c in correos
                                                                          if not DOMINIO_RESERVADO.search(c)][:3]
    clientes = leer(data, "cliente", "maestros/clientes.csv").registros
    celulares = [r.campos["celular_contacto"] for r in clientes]
    celulares += [m for r in clientes for m in re.findall(r"(?<!\d)3\d{9}(?!\d)", r.campos["observaciones"])]
    assert celulares and all(re.fullmatch(r"3990\d{6}", c) for c in celulares)


def test_mes_nuevo_coherente_con_los_maestros(tmp_path):
    data = tmp_path / "data"
    assert generar(str(data), "--fronteras", "10").returncode == 0
    r = generar(str(data), "--fronteras", "10", "--mes", "2026-05", "--verdad", str(tmp_path / "v.json"))
    assert r.returncode == 0, r.stderr
    lecturas = leer(data, "consumo", "consumos/consumos_2026-05.csv")
    assert lecturas.archivo_rechazado is None and lecturas.registros
    maestro = {x.campos["frontera_id"] for x in leer(data, "frontera", "maestros/fronteras.csv").registros}
    assert {x.campos["frontera_id"] for x in lecturas.registros} == maestro
    assert {x.campos["ts_hora"].strftime("%Y-%m") for x in lecturas.registros} == {"2026-05"}
    verdad = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    assert {x["mes"] for x in verdad["consumo_mensual"]} == {"2026-05"}
    assert generar(str(data), "--fronteras", "10", "--mes", "2026-05").returncode != 0  # ya existe: sin --forzar


def test_corromper_deja_una_copia_truncada_que_el_catalogo_rechaza(tmp_path):
    data = tmp_path / "data"
    assert generar(str(data), "--fronteras", "10").returncode == 0
    original = data / "consumos" / "consumos_2026-01.csv"
    antes = original.read_bytes()
    assert generar("--corromper", str(original)).returncode == 0
    copia = data / "consumos" / "consumos_2026-01_corrupto.csv"
    assert original.read_bytes() == antes
    danado = copia.read_bytes()
    assert antes.startswith(danado) and len(danado) < len(antes) and not danado.endswith(b"\n")
    assert fuentes.leer("consumo", copia.name, danado).archivo_rechazado is not None


def test_el_generador_solo_lee_al_corromper_y_el_pipeline_no_lo_importa():
    arbol = ast.parse(GENERADOR.read_text(encoding="utf-8"))
    lecturas = {"open", "read_bytes", "read_text"}
    for funcion in (n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)):
        for n in ast.walk(funcion):
            if isinstance(n, ast.Call):
                nombre = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
                assert nombre not in lecturas or funcion.name == "corromper", (funcion.name, nombre)
    externos = {a.name.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    externos |= {n.module.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom) and n.module}
    assert externos <= set(sys.stdlib_module_names) | {"__future__"}, externos
    for ruta in (RAIZ / "src").rglob("*.py"):
        assert "sinteticos" not in ruta.read_text(encoding="utf-8"), ruta
