"""fuentes.leer para frontera y limite: nucleo puro por la interfaz (bytes -> Resultado)."""
from datetime import date
from decimal import Decimal

import pytest

from apoyo import H_FRONTERA, H_LIMITE
from cauce.nucleo import fuentes

FILA_F = "CO0000000014,frt00012,CLI9001,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n"
OTRA_F = "CO0000000001,frt00001,CLI9002,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n"


def _f(*filas, header=H_FRONTERA):
    return fuentes.leer("frontera", "fronteras.csv", (header + "".join(filas)).encode())


def _l(*filas, header=H_LIMITE):
    return fuentes.leer("limite", "limites_frontera.csv", (header + "".join(filas)).encode())


def test_frontera_valida_tipa_los_campos():
    r = _f(FILA_F)
    assert r.archivo_rechazado is None and r.rechazos == () and r.formato == "csv"
    (reg,) = r.registros
    assert reg.linea == 2
    assert reg.campos == {"frontera_id": "CO0000000014", "codigo_sic": "frt00012", "cliente_id": "CLI9001",
                          "mercado": "No Regulado", "operador_red": "Red Andina", "nivel_tension": 3,
                          "fecha_registro": date(2024, 8, 3), "estado": "ACTIVA"}
    assert dict(r.conteos) == {"leidas": 1, "cargadas": 1, "rechazadas": 0}
    assert r.lineas == ((2, FILA_F.rstrip("\n")),)


def test_limite_valido_usa_decimal():
    r = _l("CO0000000014,8.0,200.0\n")
    assert r.registros[0].campos == {"frontera_id": "CO0000000014", "min_kwh": Decimal("8.0"),
                                     "max_kwh": Decimal("200.0")}


@pytest.mark.parametrize("fila,motivo", [
    ("COXX,frt00012,CLI9001,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n", "frontera_id_invalido"),
    ("CO0000000014,sic1,CLI9001,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n", "codigo_sic_invalido"),
    ("CO0000000014,frt00012,,No Regulado,Red Andina,3,2024-08-03,ACTIVA\n", "campo_obligatorio_vacio"),
    ("CO0000000014,frt00012,CLI9001,No Regulado,Red Andina,tres,2024-08-03,ACTIVA\n", "nivel_tension_invalido"),
    ("CO0000000014,frt00012,CLI9001,No Regulado,Red Andina,3,2024-13-45,ACTIVA\n", "fecha_invalida"),
    ("CO0000000014,frt00012,CLI9001,No Regulado,Red Andina,3,20240803,ACTIVA\n", "fecha_invalida"),
])
def test_frontera_fila_invalida_va_a_cuarentena_con_motivo_de_catalogo(fila, motivo):
    r = _f(OTRA_F, fila)
    assert len(r.registros) == 1
    (rech,) = r.rechazos
    assert (rech.linea, rech.motivo, rech.payload) == (3, motivo, fila.rstrip("\n"))
    assert dict(r.conteos) == {"leidas": 2, "cargadas": 1, "rechazadas": 1}


@pytest.mark.parametrize("fila,motivo", [
    ("CO0000000014,abc,200\n", "valor_no_numerico"),
    ("CO0000000014,1e3,200\n", "valor_no_numerico"),
    ("CO0000000014,NaN,200\n", "valor_no_numerico"),
    ("CO0000000014,,200\n", "valor_no_numerico"),
    ("CO0000000014,-1,200\n", "banda_invalida"),
    ("CO0000000014,300,200\n", "banda_invalida"),
    ("X,8,200\n", "frontera_id_invalido"),
])
def test_limite_fila_invalida(fila, motivo):
    r = _l(fila)
    assert r.registros == () and [x.motivo for x in r.rechazos] == [motivo]


def test_duplicados_dentro_del_archivo_van_a_cuarentena():
    mismo_sic = FILA_F.replace("CO0000000014", "CO0000000002")
    r = _f(FILA_F, FILA_F, mismo_sic)
    assert [x.motivo for x in r.rechazos] == ["duplicado_en_archivo", "codigo_sic_duplicado"]
    assert len(r.registros) == 1


def test_entrada_hostil_es_dato_no_instruccion():
    fila = "CO0000000014,frt00012,\"CLI'; DROP TABLE core.dim_frontera;--\",M,O,3,2024-08-03,A\n"
    r = _f(fila)
    assert r.registros[0].campos["cliente_id"] == "CLI'; DROP TABLE core.dim_frontera;--"


@pytest.mark.parametrize("datos,motivo", [
    (b"", "formato_desconocido"),
    (b"a,b,c\n1,2,3\n", "formato_desconocido"),
    ((H_FRONTERA + "CO0000000014,frt00012\n").encode(), "numero_campos"),
    ((H_FRONTERA + FILA_F.rstrip("\n")).encode(), "sin_salto_final"),
    ((H_FRONTERA + 'CO0000000014,"frt00012,CLI9001,M,O,3,2024-08-03,A\n').encode(), "comilla_sin_cerrar"),
    ((H_FRONTERA + FILA_F).encode() + b"\x81", "no_decodifica"),
    ((H_FRONTERA + FILA_F.replace("Red Andina", "Red\x00Andina")).encode(), "caracter_nulo"),
])
def test_archivo_corrupto_se_rechaza_entero(datos, motivo):
    r = fuentes.leer("frontera", "fronteras.csv", datos)
    assert r.archivo_rechazado == motivo
    assert r.registros == ()
    assert [(x.linea, x.motivo) for x in r.rechazos] == [(None, motivo)]
    assert dict(r.conteos) == {"leidas": 0, "cargadas": 0, "rechazadas": 0}


def test_acepta_bom_y_crlf():
    datos = b"\xef\xbb\xbf" + H_LIMITE.replace("\n", "\r\n").encode() + b"CO0000000014,8.0,200.0\r\n"
    r = fuentes.leer("limite", "x.csv", datos)
    assert r.archivo_rechazado is None and len(r.registros) == 1


def test_fuente_sin_parser_aun_es_error_explicito():
    with pytest.raises(NotImplementedError):
        fuentes.leer("stream", "c.json", b"{}")


# --- MotivoRechazoArchivo: cada motivo nace con su categoria y su codigo_error ---------------------------
@pytest.mark.parametrize("motivo", list(fuentes.MotivoRechazoArchivo))
def test_todo_motivo_de_rechazo_de_archivo_tiene_un_codigo_error_del_catalogo(motivo):
    from cauce.bordes import bitacora  # solo aca: el resto del archivo prueba nucleo puro

    assert motivo.codigo_error in bitacora.CODIGOS_ERROR


@pytest.mark.parametrize(("motivo", "desenlace", "codigo"), [
    ("no_decodifica", "archivo_corrupto:no_decodifica", "archivo_corrupto"),
    ("caracter_nulo", "archivo_corrupto:caracter_nulo", "archivo_corrupto"),
    ("comilla_sin_cerrar", "archivo_corrupto:comilla_sin_cerrar", "archivo_corrupto"),
    ("numero_campos", "archivo_corrupto:numero_campos", "archivo_corrupto"),
    ("sin_salto_final", "archivo_corrupto:sin_salto_final", "archivo_corrupto"),
    ("formato_desconocido", "formato_desconocido", "archivo_corrupto"),
    ("archivo_modificado", "archivo_modificado", "archivo_modificado"),
])
def test_motivo_del_desenlace_y_codigo_error_por_categoria(motivo, desenlace, codigo):
    m = fuentes.MotivoRechazoArchivo(motivo)
    assert (m.motivo_desenlace, m.codigo_error) == (desenlace, codigo)
    rechazo = fuentes.rechazar_archivo(m).rechazos[0]
    assert rechazo.motivo == motivo and type(rechazo.motivo) is str  # Cuarentena guarda el str plano
