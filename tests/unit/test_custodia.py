"""custodia: Custodio (AES-256-GCM envelope + bidx) y custodiar (unica puerta a almacen)."""
import base64

import pytest

from apoyo import CLASIF
from cauce.nucleo import custodia, fuentes
from cauce.nucleo.fuentes import Rechazo, Registro, Resultado

KEK = base64.b64encode(b"k" * 32).decode()
KBIDX = base64.b64encode(b"b" * 32).decode()


def _custodio(kek=KEK, kbidx=KBIDX):
    c = custodia.Custodio(kek, kbidx)
    c.abrir(1, custodia.nueva_dek(kek, 1))
    return c


def test_ida_y_vuelta_y_nonce_aleatorio():
    c = _custodio()
    a = c.cifrar("core.dim_cliente", "nit_cifrado", "CLI1", "valor")
    b = c.cifrar("core.dim_cliente", "nit_cifrado", "CLI1", "valor")
    assert a != b and b"valor" not in a
    assert c.descifrar("core.dim_cliente", "nit_cifrado", "CLI1", a) == "valor"


@pytest.mark.parametrize("tabla,col,llave", [("raw.cliente", "nit_cifrado", "CLI1"),
                                             ("core.dim_cliente", "otra", "CLI1"),
                                             ("core.dim_cliente", "nit_cifrado", "CLI2")])
def test_aad_distinta_falla(tabla, col, llave):
    c = _custodio()
    blob = c.cifrar("core.dim_cliente", "nit_cifrado", "CLI1", "valor")
    with pytest.raises(custodia.ErrorDescifrado):
        c.descifrar(tabla, col, llave, blob)


def test_kek_distinta_no_desenvuelve_la_dek():
    envuelta = custodia.nueva_dek(KEK, 1)
    otra = custodia.Custodio(base64.b64encode(b"z" * 32).decode(), KBIDX)
    with pytest.raises(custodia.KekInvalida):
        otra.abrir(1, envuelta)


def test_bidx_determinista_normalizado_y_dependiente_de_la_llave():
    c = _custodio()
    formatos = ["900123456-8", "900.123.456-8", "NIT 900123456-8", "9001234568"]
    assert len({c.indice(f) for f in formatos}) == 1
    assert c.indice("900123456-8") != c.indice("900123457-5")
    otra = _custodio(kbidx=base64.b64encode(b"c" * 32).decode())
    assert c.indice("900123456-8") != otra.indice("900123456-8")


CAMPOS = {"cliente_id": "CLI1", "razon_social": "Acme SAS", "nit": "900.123.456-8",
          "email_contacto": "a@example.com", "celular_contacto": "3990234567",
          "direccion": "Calle 1 # 2-3", "ciudad": "Cali", "departamento": "Valle",
          "representante_legal": "Ana Inventada", "observaciones": "tel 3990876543 y b@example.com"}


def _resultado(campos=CAMPOS, rechazo=None):
    rech = (rechazo,) if rechazo else ()
    return Resultado((Registro(2, campos),), rech, "csv",
                     {"leidas": 1 + len(rech), "cargadas": 1, "rechazadas": len(rech)},
                     ((2, "crudo con 3990234567"),))


def test_custodiar_no_deja_pii_en_claro_para_cada_campo_restringido():
    """Parametrizado desde el yaml: ningun campo Restringido-PII sale en claro."""
    c = _custodio()
    reg = custodia.custodiar(_resultado(), CLASIF, c, fuente="cliente").registros[0]
    vistos = repr(reg.raw) + repr(reg.core)
    restringidos = [h for h in fuentes.CATALOGO["cliente"]
                    if CLASIF.politica(CLASIF.campo_de("cliente", h)).nivel == "Restringido-PII"]
    assert len(restringidos) == 6
    for h in restringidos:
        assert CAMPOS[h] not in vistos
    assert reg.core["observaciones_enmascarada"] == "tel [CELULAR] y [EMAIL]"
    assert reg.raw is not None
    assert reg.raw["observaciones_enmascarada"] == reg.core["observaciones_enmascarada"]
    assert "observaciones" not in reg.core
    assert reg.core["nit_bidx"] == c.indice(CAMPOS["nit"])
    assert c.descifrar("core.dim_cliente", "nit_cifrado", "CLI1", reg.core["nit_cifrado"]) == CAMPOS["nit"]
    assert c.descifrar("raw.cliente", "nit_cifrado", "CLI1", reg.raw["nit_cifrado"]) == CAMPOS["nit"]
    assert reg.core["key_version"] == 1


def test_custodiar_descarta_el_texto_crudo_de_fuentes_con_pii():
    out = custodia.custodiar(_resultado(), CLASIF, _custodio(), fuente="cliente")
    assert out.lineas == ()


def test_custodiar_cuenta_clientes_con_pii_y_cifra_rechazos_sin_original():
    c = _custodio()
    rech = Rechazo(3, "campo_obligatorio_vacio", "", campos={**CAMPOS, "cliente_id": "CLI3", "razon_social": ""})
    out = custodia.custodiar(_resultado(rechazo=rech), CLASIF, c, fuente="cliente")
    assert out.conteos["clientes_con_pii"] == 2  # CLI1 aceptado + CLI3 rechazado
    r = out.rechazos[0]
    assert r.payload is None and r.payload_cifrado and r.key_version == 1
    texto = c.descifrar("quarantine.rechazo", "payload_cifrado", "3", r.payload_cifrado)
    assert "3990876543" not in texto and "b@example.com" not in texto and "[CELULAR]" in texto
    sin = {**CAMPOS, "observaciones": "Sin novedades."}
    out2 = custodia.custodiar(Resultado((Registro(2, sin),), (), "csv",
                                        {"leidas": 1, "cargadas": 1, "rechazadas": 0}, ()),
                              CLASIF, c, fuente="cliente")
    assert out2.conteos["clientes_con_pii"] == 0


def test_fuente_sin_pii_pasa_igual_y_resultado_custodiado_no_se_construye_afuera():
    r = fuentes.leer("limite", "l.csv", b"frontera_id,min_kwh,max_kwh\nCO1234567890,1,2\n")
    out = custodia.custodiar(r, CLASIF, _custodio(), fuente="limite")
    assert out.registros[0].core["frontera_id"] == "CO1234567890" and out.registros[0].raw is None
    with pytest.raises(TypeError):
        custodia.ResultadoCustodiado(fuente="x", registros=(), rechazos=(), conteos={}, lineas=(),
                                     archivo_rechazado=None, key_version=1)


def test_custodia_decide_pii_rechazo_con_crudo_nunca_sale_en_claro():
    """leer devuelve crudo + campos siempre; con PII segun la Clasificacion el crudo no sale de custodiar."""
    crudo = "CLI3,Acme,900.123.456-8,a@example.com,3990234567"
    rech = Rechazo(3, "campo_obligatorio_vacio", crudo, campos={**CAMPOS, "cliente_id": "CLI3"})
    entero = Rechazo(None, "formato_desconocido", crudo)  # sin campos: igual no sale el crudo
    r = _resultado(rechazo=rech)
    r = Resultado(r.registros, (rech, entero), r.formato, r.conteos, r.lineas)
    out = custodia.custodiar(r, CLASIF, _custodio(), fuente="cliente")
    assert all(x.payload is None for x in out.rechazos)
    assert out.rechazos[0].payload_cifrado and out.rechazos[0].key_version == 1
    assert out.rechazos[1].payload_cifrado is None
    assert crudo not in repr(out.rechazos) and out.lineas == ()


def test_fuente_sin_pii_conserva_payload_crudo_y_lineas_en_rechazos():
    r = fuentes.leer("limite", "l.csv", b"frontera_id,min_kwh,max_kwh\nCO1234567890,1,2\nCO1234567890,1,2\n")
    out = custodia.custodiar(r, CLASIF, _custodio(), fuente="limite")
    (rech,) = out.rechazos
    assert rech.payload == "CO1234567890,1,2" and rech.payload_cifrado is None
    assert out.lineas == r.lineas and out.lineas
