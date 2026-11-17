"""Las carpetas de `src/cauce/` son seams: el núcleo es puro y las dependencias van hacia adentro."""
import ast
from pathlib import Path

import cauce

PAQUETE = Path(cauce.__file__).parent
# librerías con I/O de red, proceso o base de datos: prohibidas en el núcleo
IO_EXTERNO = {"psycopg2", "kafka", "socket", "subprocess", "shutil", "logging"}
# llamadas que tocan disco
IO_DISCO = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "glob", "rglob", "iterdir"}


def _imports(ruta: Path) -> set[str]:
    mods = set()
    for n in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
    return mods


def _llamadas(ruta: Path) -> set[str]:
    nombres = set()
    for n in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call):
            f = n.func
            nombres.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return nombres


def _modulos(capa: str) -> list[Path]:
    return sorted((PAQUETE / capa).glob("*.py"))


def test_el_nucleo_solo_importa_nucleo_dentro_de_cauce():
    for ruta in _modulos("nucleo"):
        internos = {m for m in _imports(ruta) if m.startswith("cauce")}
        assert all(m == "cauce.nucleo" or m.startswith("cauce.nucleo.") for m in internos), (ruta.name, internos)


def test_el_nucleo_no_hace_io():
    for ruta in _modulos("nucleo"):
        externos = {m.split(".")[0] for m in _imports(ruta)} & IO_EXTERNO
        assert not externos, (ruta.name, externos)
        assert not _llamadas(ruta) & IO_DISCO, (ruta.name, _llamadas(ruta) & IO_DISCO)


def test_los_bordes_no_dependen_de_aplicacion_ni_arranque():
    for ruta in _modulos("bordes"):
        prohibidos = {m for m in _imports(ruta) if m.startswith(("cauce.aplicacion", "cauce.arranque", "cauce.cli"))}
        assert not prohibidos, (ruta.name, prohibidos)


def test_config_no_depende_de_ninguna_capa():
    internos = {m for m in _imports(PAQUETE / "config.py") if m.startswith("cauce")}
    assert not internos, internos


def test_entorno_no_depende_de_ninguna_capa_ni_lee_el_proceso():
    """Puro sobre un mapping: la cli le pasa `os.environ`; nadie mas resuelve rutas ni Kafka."""
    importados = _imports(PAQUETE / "entorno.py")
    assert not {m for m in importados if m.startswith("cauce")} and "os" not in importados, importados


def test_solo_entorno_resuelve_variables_de_ruta_y_kafka():
    variables = {"CAUCE_RAIZ", "CAUCE_DATA", "CAUCE_OUT", "CAUCE_LOG", "CAUCE_CLASIFICACION",
                 "KAFKA_BOOTSTRAP", "KAFKA_TOPIC"}
    for ruta in sorted(PAQUETE.rglob("*.py")):
        if ruta.name == "entorno.py":
            continue
        literales = {n.value for n in ast.walk(ast.parse(ruta.read_text(encoding="utf-8")))
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not literales & variables, (ruta.name, literales & variables)
