"""fuentes.leer para `factura` (JSON anidado): generado_en/periodo a nivel de archivo, huecos de estructura."""
import json
from datetime import date, datetime
from decimal import Decimal

from apoyo import BOGOTA
from cauce.nucleo import fuentes


def _factura(i="F-1", frontera="CO1234567890", **kw):
    base = {"factura_id": i, "frontera_id": frontera,
            "cliente": {"nit": "900.000.001-2", "razon_social": "Acme SAS", "email_facturacion": "a@example.com"},
            "consumo_facturado_kwh": 36041.62, "tarifa_cop_kwh": 497.23, "valor_total_cop": 17920977.0,
            "componentes": [{"concepto": c, "valor_cop_kwh": 1.5} for c in "GTDCPR"],
            "metodo_pago": {"tipo": "debito_automatico", "cuenta_enmascarada": "****9001"}}
    base.update(kw)
    return base


def _archivo(facturas, **kw):
    doc = {"periodo": "2026-01", "emisor": {"nombre": "Cauce", "nit": "800.000.001-1"},
           "generado_en": "2026-01-05T08:00:00-05:00", "facturas": facturas}
    doc.update(kw)
    return json.dumps(doc).encode()


def test_lee_facturas_con_periodo_y_generado_en_del_archivo():
    r = fuentes.leer("factura", "facturacion_2026-01.json", _archivo([_factura("F-1"), _factura("F-2")]))
    assert r.formato == "json" and r.archivo_rechazado is None
    assert r.conteos == {"leidas": 2, "cargadas": 2, "rechazadas": 0}
    assert [x.linea for x in r.registros] == [1, 2]  # posicion en `facturas`
    c = r.registros[0].campos
    assert c["periodo"] == date(2026, 1, 1)  # primer dia del mes
    assert c["generado_en"] == datetime(2026, 1, 5, 8, tzinfo=BOGOTA)
    assert c["consumo_facturado_kwh"] == Decimal("36041.62")  # Decimal exacto, no float
    assert [x["concepto"] for x in c["componentes"]] == list("GTDCPR")


def test_factura_repetida_en_el_archivo_gana_la_ultima_posicion_y_la_otra_va_a_cuarentena():
    r = fuentes.leer("factura", "f.json", _archivo([_factura("F-1", tarifa_cop_kwh=1), _factura("F-2"),
                                                    _factura("F-1", tarifa_cop_kwh=2)]))
    assert [(x.linea, x.campos["tarifa_cop_kwh"]) for x in r.registros if x.campos["factura_id"] == "F-1"] == [
        (3, Decimal(2))]
    assert [(x.linea, x.motivo) for x in r.rechazos] == [(1, "factura_duplicada")]
    campos = r.rechazos[0].campos
    assert campos is not None and campos["factura_id"] == "F-1"
    assert r.conteos == {"leidas": 3, "cargadas": 2, "rechazadas": 1}


def test_filas_invalidas_van_a_cuarentena_con_motivo_sin_tumbar_el_archivo():
    malas = [_factura("F-2", frontera="XX"), _factura(""), _factura("F-4", valor_total_cop="mucho"),
             _factura("F-5", componentes=[{"concepto": "Z", "valor_cop_kwh": 1}]),
             _factura("F-6", extra="no catalogado"), "no es objeto"]
    r = fuentes.leer("factura", "f.json", _archivo([_factura("F-1"), *malas]))
    assert [(x.linea, x.motivo) for x in r.rechazos] == [
        (2, "frontera_id_invalido"), (3, "campo_obligatorio_vacio"), (4, "valor_no_numerico"),
        (5, "concepto_desconocido"), (6, "estructura_invalida"), (7, "estructura_invalida")]
    assert r.conteos == {"leidas": 7, "cargadas": 1, "rechazadas": 6}


def test_archivo_ilegible_se_rechaza_entero():
    for datos, motivo in [(b"{no json", "formato_desconocido"), (b"[]", "formato_desconocido"),
                          (b"\xff\xfe\x00", "no_decodifica"), (b'{"a":1}', "formato_desconocido"),
                          (_archivo([], periodo="2026-13"), "formato_desconocido"),
                          (_archivo([], generado_en="ayer"), "formato_desconocido"),
                          (_archivo("x"), "formato_desconocido"),
                          (b'{"periodo": NaN}', "formato_desconocido"),
                          (b"[" * 100000, "formato_desconocido")]:
        r = fuentes.leer("factura", "f.json", datos)
        assert r.archivo_rechazado == motivo and r.registros == () and r.conteos["leidas"] == 0, datos[:20]
