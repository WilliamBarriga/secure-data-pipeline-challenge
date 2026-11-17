"""e2e: `python -m cauce run` con la facturacion de los Datos sinteticos (B6). Oraculos de la Verdad plantada y
canario de PII: los valores se derivan de los propios JSON en tiempo de test (nunca copiados aca)."""
import json
import os
from pathlib import Path

import pytest

from apoyo import consultar, copiar, correr, salida, valores_pii_facturacion, volcado_superusuario


@pytest.fixture()
def datos(tmp_path_factory, sinteticos):
    """Maestros y facturacion (los consumos se prueban aparte)."""
    return copiar(sinteticos, tmp_path_factory.mktemp("facturacion") / "data", "maestros/", "facturacion/")


def test_oraculos_de_facturacion(maestros_limpios, datos, sinteticos):
    r = correr(datos)
    assert r.returncode == 0, r.stderr
    cfg, plantado = maestros_limpios, sinteticos.verdad["facturacion"]
    archivos = sorted((datos / "facturacion").glob("*.json"))
    componentes = sum(len(f["componentes"]) for a in archivos for f in json.loads(a.read_text())["facturas"])
    assert consultar(cfg, "select (select count(*) from raw.factura), (select count(*) from core.fact_factura), "
                          "(select count(*) from core.fact_factura_componente), "
                          "(select count(*) from quarantine.rechazo where fuente = 'factura' "
                          "and motivo = 'frontera_huerfana')") == [
        (plantado["facturas"], plantado["facturas"] - plantado["huerfanas"], componentes, plantado["huerfanas"])]
    # sin duplicadas: la Cuarentena de facturacion son solo las huerfanas
    assert consultar(cfg, "select count(*) from quarantine.rechazo where fuente = 'factura'") == [
        (plantado["huerfanas"],)]
    # el generador planta huerfanas en cada archivo mensual
    assert consultar(cfg, "select count(distinct a.nombre) from quarantine.rechazo q join raw.archivo a "
                          "using (archivo_id) where q.fuente = 'factura'") == [(len(archivos),)]
    dq = json.loads(salida("dq_report.json"))
    assert dq["fronteras_huerfanas_facturacion"] == plantado["fronteras_huerfanas"]  # Fronteras distintas
    # periodo es el primer dia del mes y cruza con mes_local; cliente_id viene de la Frontera
    assert consultar(cfg, "select count(*) from core.fact_factura where extract(day from periodo) <> 1") == [(0,)]
    assert consultar(cfg, "select count(*) from core.fact_factura f join core.dim_frontera d using (frontera_id) "
                          "where f.cliente_id is distinct from d.cliente_id") == [(0,)]
    # los cuatro archivos: facturacion cargada, ninguno rechazado
    assert consultar(cfg, "select count(*) from raw.archivo where fuente = 'factura' and estado = 'cargado'") == [
        (len(archivos),)]


def test_canario_de_pii_de_facturacion_en_tablas_log_y_out_y_segunda_corrida_igual(maestros_limpios, datos):
    cfg = maestros_limpios
    r1 = correr(datos)
    assert r1.returncode == 0, r1.stderr
    estado = consultar(cfg, "select (select count(*) from raw.factura), (select count(*) from core.fact_factura), "
                            "(select count(*) from quarantine.rechazo), (select count(*) from raw.archivo)")
    r2 = correr(datos)
    assert r2.returncode == 0, r2.stderr
    assert consultar(cfg, "select (select count(*) from raw.factura), (select count(*) from core.fact_factura), "
                          "(select count(*) from quarantine.rechazo), (select count(*) from raw.archivo)") == estado
    valores = valores_pii_facturacion(datos)
    assert len(valores["cliente"]) > 100
    out = Path(os.environ["CAUCE_OUT"])
    todas = ["raw.archivo", "raw.factura", "raw.cliente", "quarantine.rechazo", "core.fact_factura",
             "core.fact_factura_componente", "core.dim_frontera", "core.dim_cliente", "audit.bitacora_corrida"]
    superficies = {
        "tablas": volcado_superusuario(cfg, todas),
        "log": Path(os.environ["CAUCE_LOG"]).read_text(encoding="utf-8"),
        "out": "\n".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file()),
        "stdout": r1.stdout + r1.stderr + r2.stdout + r2.stderr,
    }
    assert {n: sum(v in t for v in valores["cliente"]) for n, t in superficies.items()} == {
        "tablas": 0, "log": 0, "out": 0, "stdout": 0}
    # la razon social de facturacion tampoco aparece en raw.factura ni en Cuarentena (ahi solo va cifrada)
    solo_facturas = volcado_superusuario(cfg, ["raw.factura", "quarantine.rechazo", "core.fact_factura"])
    assert sum(v in solo_facturas for v in valores["razon_social"]) == 0
