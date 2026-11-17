"""e2e: `python -m cauce run` con el clientes.csv de los Datos sinteticos. Conteos contra la Verdad plantada.
Oraculo canario: como superusuario, leyendo todas las tablas, y escaneando log y out/, no aparece ningun valor PII
del archivo. Los valores se derivan del propio clientes.csv en tiempo de test (nunca copiados aca)."""
import json
import os
from pathlib import Path

import pytest

from apoyo import MAESTROS, consultar, copiar, correr, metrics, valores_pii_clientes, volcado_superusuario


@pytest.fixture()
def datos_maestros(tmp_path_factory, sinteticos):
    """Solo los maestros: los consumos se prueban en test_consumos.py."""
    return copiar(sinteticos, tmp_path_factory.mktemp("maestros") / "data", *MAESTROS)


def test_canario_sin_pii_en_tablas_log_ni_out_y_dos_corridas_mismos_conteos(maestros_limpios, datos_maestros,
                                                                           sinteticos):
    cfg, n = maestros_limpios, sinteticos.verdad["maestros"]["clientes"]
    out = Path(os.environ["CAUCE_OUT"])
    r1 = correr(datos_maestros)
    assert r1.returncode == 0, r1.stderr
    m1 = json.loads((out / "metrics.json").read_text())
    assert m1["por_fuente"]["cliente"] == {"leidas": n, "cargadas": n, "rechazadas": 0}
    estado1 = consultar(cfg, "select (select count(*) from raw.cliente), (select count(*) from core.dim_cliente), "
                             "(select count(*) from quarantine.rechazo), (select count(*) from raw.archivo)")
    assert estado1 == [(n, n, 0, len(MAESTROS))]

    r2 = correr(datos_maestros)
    assert r2.returncode == 0, r2.stderr
    m2 = json.loads((out / "metrics.json").read_text())
    assert m2["corrida"]["archivos"] == {"cargados": 0, "omitidos": len(MAESTROS), "rechazados": 0}
    assert m2["por_fuente"] == m1["por_fuente"]  # conteos de la base: iguales en las dos corridas
    assert consultar(cfg, "select (select count(*) from raw.cliente), (select count(*) from core.dim_cliente), "
                          "(select count(*) from quarantine.rechazo), (select count(*) from raw.archivo)") == estado1

    # clientes con al menos un hallazgo en texto libre: los que planto el generador, consultable (dq_report)
    assert consultar(cfg, "select valor from raw.archivo_conteo c join raw.archivo a using (archivo_id) "
                          "where a.fuente = 'cliente' and c.contador = 'clientes_con_pii'") == [
        (sinteticos.verdad["dq_report"]["clientes_con_pii_en_texto_libre"],)]

    valores = valores_pii_clientes(datos_maestros)
    assert len(valores) > 100  # el oraculo realmente tiene con que comparar
    superficies = {
        "tablas": volcado_superusuario(cfg),
        "log": Path(os.environ["CAUCE_LOG"]).read_text(encoding="utf-8"),
        "out": "\n".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file()),
        "stdout": r1.stdout + r1.stderr + r2.stdout + r2.stderr,
    }
    filtrados = {nombre: sum(1 for v in valores if v in texto) for nombre, texto in superficies.items()}
    assert filtrados == {"tablas": 0, "log": 0, "out": 0, "stdout": 0}


def test_kek_equivocada_falla_con_instruccion_y_queda_en_la_bitacora(maestros_limpios, datos_maestros):
    otra = "Z" * 43 + "="
    r = correr(datos_maestros, extra={"CAUCE_KEK": otra})
    assert r.returncode != 0
    assert "make clean && make up" in r.stderr
    assert consultar(maestros_limpios, "select resultado, codigo_error from audit.bitacora_corrida "
                                       "order by evento_id desc limit 1", maestros_limpios.auditor) == [
        ("fallo", "configuracion_invalida")]
    assert consultar(maestros_limpios, "select count(*) from core.dim_cliente") == [(0,)]
    m = metrics()  # fallo antes de las Fotos: sin conteos de la base, el primer nivel repite los de la Corrida
    assert m["resultado"] == "fallo" and m["conteos_de"] == "corrida"
    assert m["corrida"]["filas_leidas"] == m["filas_leidas"] == 0
