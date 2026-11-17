"""e2e: `python -m cauce run` con los consumos de los Datos sinteticos en cauce_test, contra la Verdad plantada."""
import csv
import hashlib
import os

import pytest

from apoyo import (
    MAESTROS,
    MAESTROS_FRONTERAS,
    cambiar_banda,
    consultar,
    consumo_mensual_esperado,
    copiar,
    correr,
    log,
    metrics,
    salida,
)

ENERO = "consumos/consumos_2026-01.csv"
CORRECCION = "consumos/consumos_2026-04_correccion.csv"


@pytest.fixture()
def datos_enero(tmp_path_factory, sinteticos):
    destino = copiar(sinteticos, tmp_path_factory.mktemp("consumos") / "data", *MAESTROS_FRONTERAS, ENERO)
    (destino / "consumos" / "subdir").mkdir()
    (destino / "consumos" / ".oculto.csv").write_text("x\n")
    (destino / "consumos" / "notas.txt").write_text("x\n")
    (destino / "consumos" / "subdir" / "consumos_2026-09.csv").write_text("x\n")
    os.mkfifo(destino / "consumos" / "tuberia.csv")  # ni archivo ni directorio: no_regular
    return destino


def test_enero_carga_b4_merge_y_consumo_mensual(maestros_limpios, datos_enero, sinteticos):
    r = correr(datos_enero)
    assert r.returncode == 0, r.stderr
    cfg, plantado = maestros_limpios, sinteticos.verdad["por_archivo"][ENERO]
    esperado = consumo_mensual_esperado(sinteticos.verdad, "2026-01")
    horas = sum(int(f[3]) for f in esperado)
    assert consultar(cfg, "select motivo, count(*) from quarantine.rechazo where fuente = 'consumo' "
                          "group by 1 order by 1") == [
        (m, n) for m, n in (("consumo_negativo", plantado["negativos"]), ("consumo_nulo", plantado["nulos"]),
                            ("consumo_outlier", plantado["outlier"])) if n]
    validas = plantado["filas"] - plantado["negativos"] - plantado["nulos"] - plantado["outlier"]
    assert consultar(cfg, "select count(*) from raw.lectura_consumo") == [(validas,)]  # todas, con duplicados
    assert consultar(cfg, "select count(*) from core.fact_consumo_horario") == [(horas,)]  # una ganadora por hora
    assert consultar(cfg, "select count(*) from raw.linea l join raw.archivo a using (archivo_id) "
                          "where a.fuente = 'consumo'") == [(plantado["filas"],)]
    filas = list(csv.reader(salida("consumo_mensual.csv").decode().splitlines()))
    assert filas[0] == ["frontera_id", "mes", "consumo_kwh", "horas_con_dato"]
    assert filas[1:] == esperado
    assert all(len(f[2].split(".")[1]) == 3 for f in filas[1:])
    eventos = log()
    ignorados = {x["archivo"]: x["motivo"] for x in eventos if x["evento"] == "archivo_ignorado"}
    assert ignorados == {".oculto.csv": "oculto", "notas.txt": "extension", "subdir": "subdirectorio",
                         "tuberia.csv": "no_regular"}
    cargados = [x["fuente"] for x in eventos if x["evento"] == "archivo_cargado"]
    assert cargados.index("consumo") > cargados.index("limite")  # fronteras -> consumos


def test_segunda_corrida_omite_y_deja_la_misma_salida(maestros_limpios, datos_enero):
    assert correr(datos_enero).returncode == 0
    primera = salida("consumo_mensual.csv")
    estado = consultar(maestros_limpios, "select (select count(*) from raw.lectura_consumo), "
                                         "(select count(*) from core.fact_consumo_horario), "
                                         "(select count(*) from quarantine.rechazo)")
    assert correr(datos_enero).returncode == 0
    assert salida("consumo_mensual.csv") == primera
    assert hashlib.sha256(salida("consumo_mensual.csv")).hexdigest() == hashlib.sha256(primera).hexdigest()
    assert consultar(maestros_limpios, "select (select count(*) from raw.lectura_consumo), "
                                       "(select count(*) from core.fact_consumo_horario), "
                                       "(select count(*) from quarantine.rechazo)") == estado
    m = metrics()
    assert m["corrida"]["archivos"]["cargados"] == 0 and m["corrida"]["archivos"]["omitidos"] == 3


# --- los cuatro meses, mes nuevo, Archivo corrupto y Archivo modificado ---------------------------------------
@pytest.fixture()
def datos_todos(tmp_path_factory, sinteticos):
    return copiar(sinteticos, tmp_path_factory.mktemp("todos") / "data", *MAESTROS, "consumos/")


def _por_mes(cfg):
    return consultar(cfg, "select mes_local, count(*), sum(consumo_kwh) from core.fact_consumo_horario "
                          "group by 1 order by 1")


def _todo(cfg):
    return consultar(cfg, "select (select count(*) from raw.lectura_consumo), (select count(*) from raw.archivo), "
                          "(select count(*) from core.fact_consumo_horario), (select sum(consumo_kwh) from "
                          "core.fact_consumo_horario), (select count(*) from quarantine.rechazo)")[0]


def test_los_cuatro_meses_cargan_y_el_mes_nuevo_no_altera_enero(maestros_limpios, datos_enero, datos_todos,
                                                                sinteticos):
    assert correr(datos_enero).returncode == 0
    enero = _por_mes(maestros_limpios)[0]
    r = correr(datos_todos) # enero se omite; febrero, marzo y abril son formatos nuevos
    assert r.returncode == 0, r.stderr
    meses = _por_mes(maestros_limpios)
    assert [str(m[0]) for m in meses] == ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    assert meses[0] == enero  # conteos y sumas de enero iguales
    assert consultar(maestros_limpios, "select count(*) from raw.archivo where estado = 'rechazado'") == [(0,)]
    verdad = sinteticos.verdad
    assert consultar(maestros_limpios, "select count(*) from raw.lectura_consumo where mwh_convertida") == [
        (verdad["dq_report"]["registros_mwh_convertidos"],)]
    assert consultar(maestros_limpios, "select count(*) from core.fact_consumo_horario where "
                                       "archivo_id = (select archivo_id from raw.archivo where "
                                       "nombre = 'consumos_2026-04_correccion.csv')") == [
        (verdad["por_archivo"][CORRECCION]["correcciones"],)]
    # cargados: 4 consumos + clientes
    assert metrics()["corrida"]["archivos"] == {"cargados": 5, "omitidos": 3, "rechazados": 0}


def test_corrida_completa_es_idempotente(maestros_limpios, datos_todos, sinteticos):
    assert correr(datos_todos).returncode == 0
    estado, mensual = _todo(maestros_limpios), salida("consumo_mensual.csv")
    assert list(csv.reader(mensual.decode().splitlines()))[1:] == consumo_mensual_esperado(sinteticos.verdad)
    assert correr(datos_todos).returncode == 0
    assert _todo(maestros_limpios) == estado and salida("consumo_mensual.csv") == mensual


def test_archivo_corrupto_y_modificado_dejan_todo_igual_con_exit_0(maestros_limpios, datos_todos):
    assert correr(datos_todos).returncode == 0
    estado, mensual = _todo(maestros_limpios), salida("consumo_mensual.csv")
    consumos = datos_todos / "consumos"
    (consumos / "consumos_2026-05.csv").write_bytes((consumos / "consumos_2026-04.csv").read_bytes()[:-3])
    febrero = consumos / "consumos_2026-02.csv"
    febrero.write_bytes(febrero.read_bytes() + febrero.read_bytes().splitlines(keepends=True)[-1])  # otro sha256
    r = correr(datos_todos)
    assert r.returncode == 0, r.stderr
    post = _todo(maestros_limpios)
    # solo suman 2 archivos rechazados y 2 filas de Cuarentena con linea nula; lecturas, hechos y suma no cambian
    assert (post[0], post[2], post[3]) == (estado[0], estado[2], estado[3])
    assert post[1] == estado[1] + 2 and post[4] == estado[4] + 2
    assert consultar(maestros_limpios, "select motivo from quarantine.rechazo where linea is null order by 1") == [
        ("archivo_modificado",), ("sin_salto_final",)]
    assert salida("consumo_mensual.csv") == mensual
    assert metrics()["corrida"]["archivos"]["rechazados"] == 2


def test_reprocesar_el_maestro_nunca_vacia_consumos(maestros_limpios, datos_todos):
    assert correr(datos_todos).returncode == 0
    estado = _todo(maestros_limpios)
    frontera_id = cambiar_banda(datos_todos, "9.0", "300.0")
    assert correr(datos_todos).returncode == 0
    assert consultar(maestros_limpios, "select min_kwh from core.dim_frontera "
                                       "where frontera_id = %s", frontera_id) == [(9,)]
    assert _todo(maestros_limpios)[2:4] == estado[2:4]
