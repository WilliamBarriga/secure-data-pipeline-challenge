"""custodia.custodiar para `factura`: cifra/descarta lo que dice data_classification.yaml, nunca PII en claro."""
import base64
import json

from apoyo import CLASIF, RAIZ
from cauce.aplicacion import corrida
from cauce.nucleo import custodia, fuentes

KEK = base64.b64encode(b"k" * 32).decode()
KBIDX = base64.b64encode(b"b" * 32).decode()
PII = ["900.000.001-2", "Acme SAS Confidencial", "lucia@example.com", "****9001"]


def _custodio():
    c = custodia.Custodio(KEK, KBIDX)
    c.abrir(1, custodia.nueva_dek(KEK, 1))
    return c


def _factura(i="F-1"):
    return {"factura_id": i, "frontera_id": "CO1234567890",
            "cliente": {"nit": PII[0], "razon_social": PII[1], "email_facturacion": PII[2]},
            "consumo_facturado_kwh": 100.5, "tarifa_cop_kwh": 497.23, "valor_total_cop": 50000.0,
            "componentes": [{"concepto": c, "valor_cop_kwh": 1.5} for c in "GTDCPR"],
            "metodo_pago": {"tipo": "debito_automatico", "cuenta_enmascarada": PII[3]}}


def _archivo(facturas):
    doc = {"periodo": "2026-01", "emisor": {"nombre": "Cauce", "nit": "800.000.001-1"},
           "generado_en": "2026-01-05T08:00:00-05:00", "facturas": facturas}
    return fuentes.leer("factura", "f.json", json.dumps(doc).encode())


def _custodiar(facturas, custodio=None):
    return custodia.custodiar(_archivo(facturas), CLASIF, custodio or _custodio(), fuente="factura")


def _raw(registro: custodia.RegistroCustodiado):
    """Las columnas raw de un registro de factura (la factura siempre tiene tabla raw)."""
    assert registro.raw is not None
    return registro.raw


def _todo(r) -> str:
    """Todo lo que custodia devuelve, como texto (bytes en hex), para buscar PII en claro."""
    partes = [repr(x.raw) + repr(x.core) for x in r.registros] + [repr(x.payload) + repr(x.payload_cifrado)
                                                                 for x in r.rechazos]
    return "".join(partes)


def test_raw_lleva_el_subobjeto_cliente_cifrado_y_descarta_cuenta_enmascarada():
    c = _custodio()
    r = _custodiar([_factura()], c)
    raw = _raw(r.registros[0])
    payload = json.loads(raw["payload"])
    assert "cuenta_enmascarada" not in raw["payload"] and "metodo_pago" in payload
    assert payload["metodo_pago"] == {"tipo": "debito_automatico"}
    assert isinstance(payload["cliente"], str)  # el subobjeto entero, cifrado en base64
    abierto = json.loads(c.descifrar("raw.factura", "payload.cliente", "F-1", base64.b64decode(payload["cliente"])))
    assert abierto == {"nit": PII[0], "razon_social": PII[1], "email_facturacion": PII[2]}
    assert payload["componentes"][0] == {"concepto": "G", "valor_cop_kwh": "1.5"}
    assert raw["frontera_id"] == "CO1234567890" and raw["key_version"] == 1


def test_core_no_lleva_datos_de_cliente_ni_cuenta():
    r = _custodiar([_factura()])
    core = r.registros[0].core
    assert set(core) == {"factura_id", "frontera_id", "periodo", "generado_en", "consumo_facturado_kwh",
                         "tarifa_cop_kwh", "valor_total_cop", "metodo_pago_tipo", "componentes"}
    assert core["metodo_pago_tipo"] == "debito_automatico"
    assert [x for x in core["componentes"]] == [(c, __import__("decimal").Decimal("1.5")) for c in "GTDCPR"]


def test_ruta_ausente_va_como_none_y_todas_las_filas_tienen_las_mismas_columnas():
    sin_tipo = {**_factura("F-2"), "metodo_pago": {"cuenta_enmascarada": PII[3]}}
    r = _custodiar([_factura("F-1"), sin_tipo])
    a, b = (x.core for x in r.registros)
    assert list(a) == list(b)
    assert a["metodo_pago_tipo"] == "debito_automatico" and b["metodo_pago_tipo"] is None


def test_ningun_valor_de_pii_sale_en_claro_ni_en_rechazos():
    r = _custodiar([_factura("F-1"), _factura("F-1"), {**_factura("F-2"), "extra": 1}])
    assert len(r.rechazos) == 2
    dup = next(x for x in r.rechazos if x.motivo == "factura_duplicada")
    assert dup.payload is None and dup.payload_cifrado and dup.key_version == 1
    huerfano = next(x for x in r.rechazos if x.motivo == "estructura_invalida")
    assert huerfano.payload is None and huerfano.payload_cifrado is None  # fila malformada: sin cuerpo
    texto = _todo(r)
    for valor in PII:
        assert valor not in texto and valor.encode().hex() not in texto


def test_el_cuerpo_de_cuarentena_se_abre_con_la_kek_y_no_trae_la_cuenta():
    c = _custodio()
    r = _custodiar([_factura("F-1"), _factura("F-1")], c)
    cifrado = r.rechazos[0].payload_cifrado
    assert cifrado is not None
    cuerpo = json.loads(c.descifrar("quarantine.rechazo", "payload_cifrado", "1", cifrado))
    assert cuerpo["cliente"] == {"nit": PII[0], "razon_social": PII[1], "email_facturacion": PII[2]}
    assert "cuenta_enmascarada" not in json.dumps(cuerpo)
    # tambien el payload cifrado por factura (para huerfanas y duplicadas cruzadas) de raw
    raw = _raw(r.registros[0])
    assert c.descifrar("quarantine.rechazo", "payload_cifrado", "2", raw["payload_cifrado"]) == json.dumps(
        cuerpo | {}, ensure_ascii=False, sort_keys=True)


def test_la_politica_sale_del_yaml_no_del_codigo():
    """Si el yaml deja de descartar la cuenta, custodia la conserva: nada hardcodeado."""
    texto = (RAIZ / "data_classification.yaml").read_text().replace("descartar: [raw, core]       #",
                                                                    "#       ")
    sin_descarte = corrida.Clasificacion.desde_texto(texto)
    r = custodia.custodiar(_archivo([_factura()]), sin_descarte, _custodio(), fuente="factura")
    assert PII[3] in _raw(r.registros[0])["payload"] and r.registros[0].core["metodo_pago_tipo"]
