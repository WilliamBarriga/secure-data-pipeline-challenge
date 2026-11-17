"""Seam: cauce.clasificacion.Clasificacion (politica / verificar), nucleo puro sin I/O."""
import pytest

from cauce.aplicacion import corrida
from cauce.nucleo import clasificacion
from cauce.nucleo.clasificacion import Clasificacion, ClasificacionIncompleta

MIN = """
version: 1
campos:
  cliente.cliente_id:
    nivel: Interno
    fisicas: [core.dim_cliente.cliente_id, analytics.v.cliente_id]
  cliente.nit:
    nivel: Restringido-PII
    bidx: true
    aliases: [nit_cliente]
    fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx]
  cliente.observaciones:
    nivel: Restringido-PII
    tratamiento: enmascarar_texto_libre
  factura.cliente.nit:
    nivel: Restringido-PII
    descartar: [core]
  factura.metodo_pago.cuenta_enmascarada:
    nivel: Confidencial
    descartar: [raw, core]
  componente_tarifa.nombre:
    nivel: Público
tecnicas:
  Interno: [core.dim_cliente.key_version]
"""
CATALOGO = {"cliente": {"cliente_id", "nit_cliente", "observaciones"}, "factura": {"cliente.nit"}}
COLUMNAS = {
    "core.dim_cliente.cliente_id": "text", "core.dim_cliente.nit_cifrado": "bytea",
    "core.dim_cliente.nit_bidx": "bytea", "core.dim_cliente.key_version": "integer",
    "analytics.v.cliente_id": "text",
}


def _cls(texto=MIN):
    return Clasificacion.desde_texto(texto)


def _causas(exc):
    return {(c.codigo, c.nombres) for c in exc.value.causas}


def test_minimo_valido_pasa_verificar():
    _cls().verificar(CATALOGO, COLUMNAS)


def test_politica_sale_del_nivel_y_los_overrides():
    c = _cls()
    nit = c.politica("cliente.nit")
    assert (nit.nivel, nit.cifrar, nit.bidx, nit.en_analytics, nit.valor_en_logs) == \
        ("Restringido-PII", True, True, False, False)
    obs = c.politica("cliente.observaciones")
    assert obs.enmascarar and not obs.cifrar  # el original nunca persiste, ni cifrado
    assert c.politica("factura.cliente.nit").descartar == ("core",)
    assert c.politica("factura.metodo_pago.cuenta_enmascarada").descartar == ("raw", "core")
    cid = c.politica("cliente.cliente_id")
    assert (cid.cifrar, cid.en_analytics, cid.valor_en_logs) == (False, True, True)
    pub = c.politica("componente_tarifa.nombre")
    assert (pub.en_analytics, pub.valor_en_logs) == (True, True)


def test_confidencial_va_en_claro_pero_ni_analytics_ni_logs():
    p = _cls().politica("factura.metodo_pago.cuenta_enmascarada")
    assert (p.cifrar, p.en_analytics, p.valor_en_logs) == (False, False, False)


def test_campo_desconocido_en_politica():
    with pytest.raises(clasificacion.CampoNoClasificado):
        _cls().politica("cliente.nuevo")


def test_campo_de_resuelve_alias_y_nombre_canonico():
    c = _cls()
    assert c.campo_de("cliente", "nit_cliente") == "cliente.nit"
    assert c.campo_de("cliente", "cliente_id") == "cliente.cliente_id"
    assert c.campo_de("factura", "cliente.nit") == "factura.cliente.nit"
    with pytest.raises(clasificacion.CampoNoClasificado):
        c.campo_de("cliente", "telefono")


@pytest.mark.parametrize("cambio", [
    ("nivel: Restringido-PII\n    bidx: true", "nivel: Secreto\n    bidx: true"),   # nivel fuera del catalogo
    ("version: 1", "version: 2"),
    ("  cliente.cliente_id:\n    nivel: Interno", "  cliente.cliente_id:\n    sin_nivel: Interno"),
    ("descartar: [core]", "descartar: [analytics]"),
    ("    bidx: true", "    bidx: true\n    inventado: 1"),
    ("fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx]",
     "fisicas: [core.dim_cliente.nit_cifrado, nit_bidx]"),                          # fisica mal formada
    ("fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx]",
     "fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.cliente_id]"),       # fisica duplicada
    ("fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx]",
     "fisicas: [core.dim_cliente.nit_cifrado, public.dim_cliente.nit_bidx]"),       # fisica fuera de las ZONAS
    ("  cliente.cliente_id:", "  cliente:"),                                        # nombre sin tramo
])
def test_esquema_invalido(cambio):
    viejo, nuevo = cambio
    assert viejo in MIN
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls(MIN.replace(viejo, nuevo))
    assert e.value.codigo_error == "clasificacion_incompleta"
    assert {c.codigo for c in e.value.causas} == {"esquema_invalido"}


def test_yaml_no_mapeo_o_roto_es_esquema_invalido():
    for texto in ("- lista", "campos: [", "!!python/object/apply:os.system ['id']"):
        with pytest.raises(ClasificacionIncompleta):
            _cls(texto)


def test_bidx_exige_restringido_pii():
    with pytest.raises(ClasificacionIncompleta):
        _cls(MIN.replace("cliente.cliente_id:\n    nivel: Interno",
                         "cliente.cliente_id:\n    nivel: Interno\n    bidx: true"))


def test_alias_repetido_en_la_misma_fuente_es_esquema_invalido():
    with pytest.raises(ClasificacionIncompleta):
        _cls(MIN.replace("[nit_cliente]", "[cliente_id]"))


def test_header_de_fuente_sin_clasificar():
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls().verificar({**CATALOGO, "cliente": CATALOGO["cliente"] | {"telefono"}}, COLUMNAS)
    assert _causas(e) == {("header_sin_clasificar", ("cliente.telefono",))}


def test_columna_fisica_sin_mapear():
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls().verificar(CATALOGO, {**COLUMNAS, "raw.cliente.nueva": "text"})
    assert _causas(e) == {("columna_sin_mapear", ("raw.cliente.nueva",))}


def test_fisica_declarada_que_no_existe():
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls().verificar(CATALOGO, {k: v for k, v in COLUMNAS.items() if k != "core.dim_cliente.nit_bidx"})
    assert _causas(e) == {("fisica_inexistente", ("core.dim_cliente.nit_bidx",))}


def test_restringido_pii_con_columna_text_en_claro():
    texto = MIN.replace("fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx]",
                        "fisicas: [core.dim_cliente.nit_cifrado, core.dim_cliente.nit_bidx, raw.x.nit]")
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls(texto).verificar(CATALOGO, {**COLUMNAS, "raw.x.nit": "text"})
    assert _causas(e) == {("pii_en_claro", ("raw.x.nit",))}
    # tipos de texto con otro nombre tambien cuentan
    with pytest.raises(ClasificacionIncompleta):
        _cls(texto).verificar(CATALOGO, {**COLUMNAS, "raw.x.nit": "character varying(20)"})
    # bytea cifrado esta bien
    _cls(texto).verificar(CATALOGO, {**COLUMNAS, "raw.x.nit": "bytea"})


def test_columna_de_analytics_mayor_que_interno():
    texto = MIN.replace("[core.dim_cliente.cliente_id, analytics.v.cliente_id]", "[core.dim_cliente.cliente_id]") \
        .replace("componente_tarifa.nombre:\n    nivel: Público",
                 "componente_tarifa.nombre:\n    nivel: Confidencial\n    fisicas: [analytics.v.nombre]")
    columnas = {k: v for k, v in COLUMNAS.items() if k != "analytics.v.cliente_id"} | {"analytics.v.nombre": "text"}
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls(texto).verificar(CATALOGO, columnas)
    assert _causas(e) == {("analytics_excede_interno", ("analytics.v.nombre",))}


def test_tecnicas_tambien_cuentan_para_analytics():
    texto = MIN.replace("tecnicas:\n  Interno: [core.dim_cliente.key_version]",
                        "tecnicas:\n  Interno: [core.dim_cliente.key_version]\n  Confidencial: [analytics.v.derivada]")
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls(texto).verificar(CATALOGO, {**COLUMNAS, "analytics.v.derivada": "numeric"})
    assert _causas(e) == {("analytics_excede_interno", ("analytics.v.derivada",))}


def test_mensaje_lleva_nombres_y_nunca_valores():
    with pytest.raises(ClasificacionIncompleta) as e:
        _cls(MIN.replace("nivel: Interno", "nivel: SECRETO-DE-PRUEBA", 1))
    assert "SECRETO-DE-PRUEBA" not in str(e.value)
    assert "clasificacion_incompleta" in str(e.value)


def test_el_yaml_real_es_valido_y_cubre_las_claves_de_out_y_logs():
    c = corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO)
    for campo in ("dq_report.filas_leidas", "metrics.filas_leidas", "logs.evento", "stream.medidor.serial",
                  "bitacora.ejecutor", "quarantine.payload_cifrado", "cliente.nit",
                  "factura.cliente.email_facturacion"):
        c.politica(campo)
    assert c.politica("bitacora.ejecutor").nivel == "Confidencial"
    assert c.politica("quarantine.payload_cifrado").nivel == "Restringido-PII"
    assert c.politica("cliente.nit").bidx
    assert c.politica("factura.metodo_pago.cuenta_enmascarada").descartar == ("raw", "core")
    assert c.politica("factura.cliente.nit").descartar == ("core",)
    assert c.politica("cliente.observaciones").enmascarar


def test_niveles_logs_cubre_todas_las_claves_del_catalogo_de_eventos():
    from cauce.bordes import observabilidad
    niveles = corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO).niveles_logs()
    claves = {k for _, permitidas in observabilidad.CATALOGO.values() for k in permitidas}
    assert claves <= set(niveles)


def test_niveles_logs_toma_los_campos_logs():
    texto = MIN.replace("tecnicas:", "  logs.motivo:\n    nivel: Confidencial\ntecnicas:")
    assert _cls(texto).niveles_logs() == {"motivo": "Confidencial"}


def test_el_catalogo_de_fuentes_esta_totalmente_clasificado():
    from cauce.nucleo import fuentes
    c = corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO)
    for fuente, headers in fuentes.CATALOGO.items():
        for h in headers:
            c.campo_de(fuente, h)


def test_el_nucleo_no_lee_archivos_y_el_borde_traduce_el_archivo_ilegible(tmp_path):
    assert not hasattr(clasificacion, "cargar")
    with pytest.raises(ClasificacionIncompleta) as e:
        corrida.leer_clasificacion(tmp_path / "no_existe.yaml")
    assert "esquema_invalido" in str(e.value)
    assert corrida.leer_clasificacion(clasificacion.RUTA_POR_DEFECTO).politica("cliente.nit").bidx
