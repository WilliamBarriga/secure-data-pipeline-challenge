"""fuentes.leer para `cliente` (ISO-8859-1): casos B7, toda fila termina cargada o en Cuarentena."""
import csv
import io

from cauce.nucleo import fuentes

H = ["cliente_id", "razon_social", "nit", "email_contacto", "celular_contacto", "direccion", "ciudad",
     "departamento", "representante_legal", "observaciones"]


def _csv(filas, codificacion="iso-8859-1"):
    b = io.StringIO(newline="")
    w = csv.writer(b, lineterminator="\n")
    w.writerow(H)
    w.writerows(filas)
    return b.getvalue().encode(codificacion)


def _fila(i="CLI9001", **kw):
    base = dict(zip(H, [i, "Acme SAS", "900123456-8", "a@example.com", "3990234567", "Calle 1", "Cali",
                        "Valle", "Ana Inventada", "Sin novedades."], strict=True))
    base.update(kw)
    return [base[h] for h in H]


def test_lee_latin1_con_acentos():
    r = fuentes.leer("cliente", "clientes.csv", _csv([_fila(razon_social="Cañería Ñandú S.A.")]))
    assert r.registros[0].campos["razon_social"] == "Cañería Ñandú S.A."
    assert r.conteos == {"leidas": 1, "cargadas": 1, "rechazadas": 0}


def test_comillas_anidadas_salto_de_linea_largo_y_sql_se_cargan_como_datos():
    obs = ['dijo "hola, que tal" y se fue', "linea uno\nlinea dos", "x" * 100_000,
           "'); DROP TABLE core.dim_cliente; --"]
    filas = [_fila(f"CLI{i}", observaciones=o) for i, o in enumerate(obs)]
    r = fuentes.leer("cliente", "clientes.csv", _csv(filas))
    assert r.conteos == {"leidas": 4, "cargadas": 4, "rechazadas": 0}
    assert [x.campos["observaciones"] for x in r.registros] == obs
    assert [x.linea for x in r.registros] == [2, 3, 5, 6]  # el salto citado ocupa 2 lineas fisicas


def test_filas_invalidas_van_a_cuarentena_con_sus_campos_y_motivo():
    filas = [_fila("CLI1"), _fila("CLI1"), _fila("", razon_social="X"), _fila("CLI3", razon_social=" "),
             _fila("CLI4", nit="abc")]
    r = fuentes.leer("cliente", "clientes.csv", _csv(filas))
    assert [x.motivo for x in r.rechazos] == ["duplicado_en_archivo", "campo_obligatorio_vacio",
                                              "campo_obligatorio_vacio", "nit_invalido"]
    assert all(x.payload and x.campos is not None for x in r.rechazos)  # crudo + campos siempre (custodia decide)
    assert r.conteos == {"leidas": 5, "cargadas": 1, "rechazadas": 4}
    assert [n for n, _ in r.lineas] == [2, 3, 4, 5, 6]  # leer ya no sabe de PII: siempre llena lineas


def test_nit_en_los_cuatro_formatos_es_valido():
    filas = [_fila(f"C{i}", nit=n) for i, n in enumerate(
        ["999999999-4", "999.999.999-4", "NIT 999999999-4", "9999999994"])]
    assert fuentes.leer("cliente", "clientes.csv", _csv(filas)).conteos["cargadas"] == 4


def test_archivo_corrupto_se_rechaza_entero():
    assert fuentes.leer("cliente", "c.csv", b"a,b\n1,2\n").archivo_rechazado == "formato_desconocido"
    sin_salto = _csv([_fila()]).rstrip(b"\n")
    assert fuentes.leer("cliente", "c.csv", sin_salto).archivo_rechazado == "sin_salto_final"


def test_repr_del_resultado_no_expone_valores_de_fila():
    """El Resultado de `leer` trae crudo y campos: un repr (traceback, print) no los muestra."""
    r = fuentes.leer("cliente", "clientes.csv", _csv([_fila(), _fila("CLI9002", nit="")]))
    assert r.registros and r.rechazos
    texto = repr(r)
    for valor in ("900123456-8", "a@example.com", "3990234567", "Ana Inventada"):
        assert valor not in texto
