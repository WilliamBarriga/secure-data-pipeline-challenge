"""fuentes.leer('consumo'): formato de enero, Hora local, B4 y Archivo corrupto."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from apoyo import BOGOTA, H_CONSUMO
from cauce.nucleo import fuentes

F = "CO0000000011"


def base(conteos):
    """Solo leidas/cargadas/rechazadas (los contadores B tienen sus propias pruebas)."""
    return {k: conteos[k] for k in ("leidas", "cargadas", "rechazadas")}


def leer(*filas, encabezado=H_CONSUMO, sufijo="\n"):
    texto = encabezado + "\n".join(filas) + sufijo if filas else encabezado
    return fuentes.leer("consumo", "consumos_2026-01.csv", texto.encode())


def fila(valor, ts="2026-01-01 00:00:00", version="1", calidad="OK"):
    return f"{F},{ts},{valor},{version},{calidad}"


def test_fila_valida_trae_hora_local_decimal_y_version():
    r = leer(fila("275.283"))
    assert r.archivo_rechazado is None and r.formato == "csv"
    assert base(r.conteos) == {"leidas": 1, "cargadas": 1, "rechazadas": 0}
    (reg,) = r.registros
    assert reg.linea == 2
    assert reg.campos == {"frontera_id": F, "ts_hora": datetime(2026, 1, 1, tzinfo=BOGOTA),
                          "consumo_kwh": Decimal("275.283"), "version_lectura": 1, "calidad": "OK",
                          "mwh_convertida": False}
    assert reg.campos["ts_hora"].utcoffset() == timedelta(hours=-5)
    assert r.lineas == ((2, fila("275.283")),)


@pytest.mark.parametrize("valor", ["0", "0.000", "1000000", "1000000.000", "999999.999"])
def test_b4_bordes_validos(valor):
    r = leer(fila(valor))
    assert len(r.registros) == 1 and not r.rechazos
    assert r.registros[0].campos["consumo_kwh"] == Decimal(valor)


@pytest.mark.parametrize("valor, motivo", [
    ("", "consumo_nulo"), ("   ", "consumo_nulo"), ("null", "consumo_nulo"), ("NaN", "consumo_nulo"),
    ("-0.001", "consumo_negativo"), ("-5", "consumo_negativo"),
    ("1000000.001", "consumo_outlier"), ("9999999", "consumo_outlier"),
    ("abc", "valor_no_numerico"), ("1e5", "valor_no_numerico"), ("1.5.2", "valor_no_numerico"),
    ("Infinity", "valor_no_numerico"),
])
def test_b4_rechaza_con_motivo_y_fila_cruda(valor, motivo):
    r = leer(fila(valor))
    assert [(x.linea, x.motivo, x.payload) for x in r.rechazos] == [(2, motivo, fila(valor))]
    assert not r.registros
    assert base(r.conteos) == {"leidas": 1, "cargadas": 0, "rechazadas": 1}


@pytest.mark.parametrize("ts, motivo", [
    ("2026-01-01 00:30:00", "timestamp_fuera_de_hora"),
    ("2026-01-01 00:00:15", "timestamp_fuera_de_hora"),
    ("2026-13-01 00:00:00", "timestamp_invalido"),
    ("ayer", "timestamp_invalido"),
    ("", "timestamp_invalido"),
])
def test_timestamp_invalido_o_fuera_de_hora_en_punto(ts, motivo):
    r = leer(fila("10", ts=ts))
    assert [x.motivo for x in r.rechazos] == [motivo]


def test_version_o_frontera_invalida_va_a_cuarentena():
    r = leer(fila("10", version="x"), "COXX,2026-01-01 00:00:00,10,1,OK")
    assert [x.motivo for x in r.rechazos] == ["version_invalida", "frontera_id_invalido"]


def test_filas_buenas_y_malas_se_separan_y_cuentan():
    r = leer(fila("1", ts="2026-01-01 00:00:00"), fila("-1", ts="2026-01-01 01:00:00"),
             fila("2", ts="2026-01-01 02:00:00"))
    assert [x.linea for x in r.registros] == [2, 4] and [x.linea for x in r.rechazos] == [3]
    assert base(r.conteos) == {"leidas": 3, "cargadas": 2, "rechazadas": 1}


def test_duplicados_de_llave_no_se_rechazan_en_fuentes():
    r = leer(fila("1"), fila("1"), fila("2", version="2", calidad="CORREGIDA"))
    assert len(r.registros) == 3  # B3 se resuelve en almacen


@pytest.mark.parametrize("encabezado", [
    "frontera_id,timestamp_utc,consumo_kwh,version_lectura,calidad_lectura\n",  # separador de febrero con coma
    "frontera_id;timestamp,consumo_kwh;version_lectura;calidad_lectura\n",      # separadores mezclados
    "frontera_id,timestamp,kwh,version_lectura,calidad_lectura\n",              # marzo sin unidad
    "otra,cosa\n",
    "frontera_id,timestamp,consumo_kwh,version_lectura\n",
])
def test_encabezado_fuera_del_catalogo_rechaza_el_archivo(encabezado):
    r = leer(encabezado=encabezado)
    assert r.archivo_rechazado == "formato_desconocido"
    assert [(x.linea, x.motivo) for x in r.rechazos] == [(None, "formato_desconocido")]
    assert base(r.conteos) == {"leidas": 0, "cargadas": 0, "rechazadas": 0}


@pytest.mark.parametrize("datos, motivo", [
    (b"\xff\xfe\x00" + b"\x81" * 3, "no_decodifica"),
    ((H_CONSUMO + F + ",2026-01-01 00:00:00,1,1\n").encode(), "numero_campos"),
    ((H_CONSUMO + F + ",2026-01-01 00:00:00,1,1,OK,extra\n").encode(), "numero_campos"),
    ((H_CONSUMO + F + ',"2026-01-01 00:00:00,1,1,OK\n').encode(), "comilla_sin_cerrar"),
    ((H_CONSUMO + fila("1")).encode(), "sin_salto_final"),
    (b"", "formato_desconocido"),
])
def test_archivo_corrupto_se_rechaza_entero(datos, motivo):
    r = fuentes.leer("consumo", "consumos_2026-01.csv", datos)
    assert r.archivo_rechazado == motivo
    assert not r.registros and r.conteos["leidas"] == 0


def test_bom_y_crlf_se_toleran():
    r = fuentes.leer("consumo", "c.csv", ("﻿" + H_CONSUMO.replace("\n", "\r\n") + fila("3") + "\r\n").encode())
    assert len(r.registros) == 1 and r.lineas == ((2, fila("3")),)


HF = "frontera_id;timestamp_utc;consumo_kwh;version_lectura;calidad_lectura\r\n"
HM = "frontera_id,timestamp,kwh,unidad,version_lectura,calidad_lectura\n"


def leer_feb(*filas):
    return fuentes.leer("consumo", "consumos_2026-02.csv", (HF + "".join(f + "\r\n" for f in filas)).encode())


def test_febrero_separador_punto_y_coma_coma_decimal_utc_y_crlf():
    r = leer_feb(f"{F};2026-02-01T05:00:00Z;163,795;1;OK", f"{F};2026-02-01T06:00:00Z;-1,5;1;OK",
                 f"{F};2026-02-01T07:00:00Z;;1;OK")
    assert r.archivo_rechazado is None and r.formato == "csv"
    assert base(r.conteos) == {"leidas": 3, "cargadas": 1, "rechazadas": 2}
    (reg,) = r.registros
    assert reg.campos["consumo_kwh"] == Decimal("163.795") and reg.campos["mwh_convertida"] is False
    assert reg.campos["ts_hora"] == datetime(2026, 2, 1, 0, tzinfo=BOGOTA)  # 05:00Z = 00:00 Bogota
    assert reg.campos["ts_hora"].utcoffset() == timedelta(hours=-5)  # Hora local, igual que enero
    assert [x.motivo for x in r.rechazos] == ["consumo_negativo", "consumo_nulo"]
    assert r.lineas[0] == (2, f"{F};2026-02-01T05:00:00Z;163,795;1;OK")


@pytest.mark.parametrize("valor", ["163.795", "1.000,5", "1,2,3"])
def test_febrero_solo_acepta_coma_decimal(valor):
    r = leer_feb(f"{F};2026-02-01T05:00:00Z;{valor};1;OK")
    assert [x.motivo for x in r.rechazos] == ["valor_no_numerico"]


@pytest.mark.parametrize("ts", ["2026-02-01 05:00:00", "2026-02-01T05:00:00", "2026-02-01T05:30:00Z", "x"])
def test_febrero_timestamp_debe_ser_utc_en_punto(ts):
    r = leer_feb(f"{F};{ts};1,0;1;OK")
    assert [x.motivo for x in r.rechazos] == [
        "timestamp_fuera_de_hora" if ts.endswith("30:00Z") else "timestamp_invalido"]


def leer_marzo(*filas):
    return fuentes.leer("consumo", "consumos_2026-03.csv", (HM + "".join(f + "\n" for f in filas)).encode())


def test_marzo_mwh_se_convierte_a_kwh_antes_de_b4():
    r = leer_marzo(f"{F},2026-03-01 00:00:00,126.985,kWh,1,OK", f"{F},2026-03-01 01:00:00,0.407969,MWh,1,OK",
                   f"{F},2026-03-01 02:00:00,1000.001,MWh,1,OK", f"{F},2026-03-01 03:00:00,,MWh,1,OK")
    assert [(g.campos["consumo_kwh"], g.campos["mwh_convertida"]) for g in r.registros] == [
        (Decimal("126.985"), False), (Decimal("407.969"), True)]
    assert [x.motivo for x in r.rechazos] == ["consumo_outlier", "consumo_nulo"]  # 1.000.001 kWh > maximo


@pytest.mark.parametrize("unidad", ["Wh", "GWh", "kwh", "", "MWH"])
def test_unidad_desconocida_va_a_cuarentena(unidad):
    r = leer_marzo(f"{F},2026-03-01 00:00:00,5,{unidad},1,OK")
    assert [(x.linea, x.motivo) for x in r.rechazos] == [(2, "unidad_desconocida")]
    assert not r.registros and base(r.conteos) == {"leidas": 1, "cargadas": 0, "rechazadas": 1}


def test_abril_y_correccion_comparten_el_formato_de_enero():
    texto = H_CONSUMO.replace("\n", "\r\n") + fila("480.481", version="2", calidad="CORREGIDA") + "\r\n"
    r = fuentes.leer("consumo", "consumos_2026-04_correccion.csv", texto.encode())
    assert [(g.campos["version_lectura"], g.campos["calidad"]) for g in r.registros] == [(2, "CORREGIDA")]


@pytest.mark.parametrize("datos, motivo", [
    ((HF + f"{F};2026-02-01T05:00:00Z;1,5;1\r\n").encode(), "numero_campos"),
    ((HF + f'{F};"2026-02-01T05:00:00Z;1,5;1;OK\r\n').encode(), "comilla_sin_cerrar"),
    ((HF + f"{F};2026-02-01T05:00:00Z;1,5;1;O").encode(), "sin_salto_final"),  # truncado conserva los campos
    ((HM + f"{F},2026-03-01 00:00:00,1,kWh,1,OK\n" + f"{F},2026-03-01 01:00:00,1,kWh,1,O").encode(),
     "sin_salto_final"),
    (HM.encode() + b"\xff\x81", "no_decodifica"),
])
def test_corrupcion_en_los_otros_formatos(datos, motivo):
    r = fuentes.leer("consumo", "c.csv", datos)
    assert r.archivo_rechazado == motivo and not r.registros and r.conteos["leidas"] == 0
    assert [(x.linea, x.motivo) for x in r.rechazos] == [(None, motivo)]


def test_cp1252_estricto_si_no_es_utf8_valido():
    texto = (H_CONSUMO + fila("1", calidad="OK") + "\n" + F + ",2026-01-01 01:00:00,2,1,caf\xe9\n").encode("latin-1")
    r = fuentes.leer("consumo", "c.csv", texto)
    assert r.archivo_rechazado is None and [g.campos["calidad"] for g in r.registros] == ["OK", "café"]


# --- contadores B por archivo (los guarda almacen en raw.archivo_conteo) ----------------------------------
def test_conteos_b_por_archivo_son_cubos_disjuntos():
    r = leer(fila(""), fila("null", "2026-01-01 01:00:00"), fila("-1", "2026-01-01 02:00:00"),
             fila("2000000", "2026-01-01 03:00:00"), fila("abc", "2026-01-01 04:00:00"),
             fila("5", "2026-01-01 05:00:00"))
    assert dict(r.conteos) == {"leidas": 6, "cargadas": 1, "rechazadas": 5, "nulos": 2, "negativos": 1,
                               "outlier": 1, "no_numerico": 1}


def test_conteo_mwh_incluye_la_fila_convertida_que_se_rechaza_por_outlier():
    r = leer_marzo(f"{F},2026-03-01 00:00:00,2,MWh,1,OK", f"{F},2026-03-01 01:00:00,5,kWh,1,OK")
    assert r.conteos["mwh_convertidas"] == 1 and not r.rechazos
    r = leer_marzo(f"{F},2026-03-01 00:00:00,1001,MWh,1,OK")  # 1.001.000 kWh
    assert [x.motivo for x in r.rechazos] == ["consumo_outlier"]
    assert r.conteos["mwh_convertidas"] == 1 and r.conteos["outlier"] == 1


def test_los_contadores_b_que_produce_leer_son_los_que_declara_fuentes():
    """`fotos` lee los contadores B por `fuentes.CONTADORES_B`: un nombre nuevo o renombrado aca se veria."""
    r = leer_marzo(f"{F},2026-03-01 00:00:00,,kWh,1,OK", f"{F},2026-03-01 01:00:00,-1,kWh,1,OK",
                   f"{F},2026-03-01 02:00:00,1001,MWh,1,OK", f"{F},2026-03-01 03:00:00,abc,kWh,1,OK")
    assert set(r.conteos) - {"leidas", "cargadas", "rechazadas"} == set(fuentes.CONTADORES_B)
