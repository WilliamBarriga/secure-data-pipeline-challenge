"""Facturacion (B6) con `ingerir` contra Postgres real (cauce_test): huerfanas, duplicadas, minimizacion, PII."""
import json
import uuid

import pytest

from apoyo import CLASIF, F_CONSUMO, H_CLIENTE, H_FRONTERA, filas
from cauce.aplicacion.ingesta_archivo import ContextoCarga, ingerir
from cauce.bordes import almacen, bitacora, fotos
from cauce.nucleo import custodia

FRONTERA, OTRA, FANTASMA, FANTASMA2 = F_CONSUMO, "CO2227285749", "CO9990000001", "CO9990000002"
NIT, RAZON = "900.000.001-2", "Razon Secreta Facturacion SAS"
EMAIL, CUENTA = "facturacion.secreta@example.com", "****9001"
PII = (NIT, RAZON, EMAIL, CUENTA)


@pytest.fixture()
def ctx(maestros_limpios):
    con = almacen.conectar(maestros_limpios, maestros_limpios.ingesta, "cauce_test")
    custodio = custodia.Custodio(maestros_limpios.kek, maestros_limpios.k_bidx)
    almacen.abrir_dek(con, custodio)
    yield ContextoCarga(con, bitacora.identidad(uuid.uuid4(), "run"), CLASIF, custodio)
    con.close()


def _factura(i, frontera=FRONTERA, tarifa=500.5):
    return {"factura_id": i, "frontera_id": frontera,
            "cliente": {"nit": NIT, "razon_social": RAZON, "email_facturacion": EMAIL},
            "consumo_facturado_kwh": 1000.25, "tarifa_cop_kwh": tarifa, "valor_total_cop": 500000.0,
            "componentes": [{"concepto": c, "valor_cop_kwh": 10.5} for c in "GTDCPR"],
            "metodo_pago": {"tipo": "debito_automatico", "cuenta_enmascarada": CUENTA}}


def _ingerir(ctx, tmp_path, nombre, facturas, generado="2026-01-05T08:00:00-05:00", periodo="2026-01",
             fuente="factura"):
    texto = json.dumps({"periodo": periodo, "emisor": {"nombre": "Cauce", "nit": "800.000.001-1"},
                        "generado_en": generado, "facturas": facturas})
    ruta = tmp_path / nombre
    ruta.write_text(texto)
    return ingerir(ctx, fuente, nombre, ruta)


def _maestros(ctx, tmp_path, cliente="CLI9001"):
    (tmp_path / "f.csv").write_text(H_FRONTERA + f"{FRONTERA},frt00001,{cliente},Regulado,Emcali,2,2020-01-01,activa\n"
                                                 f"{OTRA},frt00002,{cliente},Regulado,Emcali,2,2020-01-01,activa\n")
    ingerir(ctx, "frontera", "fronteras.csv", tmp_path / "f.csv")
    (tmp_path / "c.csv").write_text(H_CLIENTE + f"{cliente},Acme SAS,900123456-8,uno@example.com,3990112233,"
                                                "Calle 1,Cali,Valle,Ana Inventada,Sin novedades.\n")
    ingerir(ctx, "cliente", "clientes.csv", tmp_path / "c.csv")


def test_factura_valida_llega_a_core_con_cliente_resuelto_y_componentes(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    d = _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1")])
    assert d.tipo == "cargado" and d.conteos["leidas"] == 1 and d.conteos["cargadas"] == 1
    generado = filas(ctx.con, "select '2026-01-05 08:00:00-05'::timestamptz")[0][0]
    assert filas(ctx.con, "select factura_id, frontera_id, cliente_id, periodo::text, generado_en, "
                          "consumo_facturado_kwh, tarifa_cop_kwh, valor_total_cop, metodo_pago_tipo "
                          "from core.fact_factura")[0][:5] == ("F-1", FRONTERA, "CLI9001", "2026-01-01", generado)
    assert filas(ctx.con, "select concepto, valor_cop_kwh::float from core.fact_factura_componente order by 1") == [
        (c, 10.5) for c in "CDGPRT"]
    assert filas(ctx.con, "select count(*) from core.dim_componente_tarifa") == [(6,)]
    assert filas(ctx.con, "select count(*) from quarantine.rechazo") == [(0,)]


def test_cliente_id_queda_nulo_si_el_cliente_de_la_frontera_no_esta_en_el_maestro(ctx, tmp_path):
    _maestros(ctx, tmp_path, cliente="CLI9001")
    filas(ctx.con, "update core.dim_frontera set cliente_id = 'CLI-NO-EXISTE' where frontera_id = %s", FRONTERA)
    _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1")])
    assert filas(ctx.con, "select cliente_id from core.fact_factura") == [(None,)]


def test_huerfana_va_a_cuarentena_cifrada_no_llega_a_core_y_cuenta_fronteras_distintas(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    d = _ingerir(ctx, tmp_path, "facturacion_2026-01.json",
                 [_factura("F-1"), _factura("F-2", FANTASMA), _factura("F-3", FANTASMA), _factura("F-4", FANTASMA2)])
    assert d.conteos["leidas"] == 4 and d.conteos["cargadas"] == 1 and d.conteos["rechazadas"] == 3
    assert d.conteos["rechazadas_por_motivo"] == {"frontera_huerfana": 3}
    assert filas(ctx.con, "select factura_id from core.fact_factura") == [("F-1",)]
    assert filas(ctx.con, "select linea, motivo, payload is null, payload_cifrado is not null, key_version "
                          "from quarantine.rechazo order by linea") == [(2, "frontera_huerfana", True, True, 1),
                                                                    (3, "frontera_huerfana", True, True, 1),
                                                                    (4, "frontera_huerfana", True, True, 1)]
    dq = json.loads(fotos.materializar_salidas(ctx.con)["dq_report.json"])
    assert dq["fronteras_huerfanas_facturacion"] == 2  # Fronteras distintas, no Facturas (3)


def test_huerfanas_de_varios_archivos_cuentan_la_frontera_una_vez(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1", FANTASMA)])
    _ingerir(ctx, tmp_path, "facturacion_2026-02.json", [_factura("F-2", FANTASMA)],
             "2026-02-05T08:00:00-05:00", "2026-02")
    assert json.loads(fotos.materializar_salidas(ctx.con)["dq_report.json"])[
               "fronteras_huerfanas_facturacion"] == 1


def test_factura_id_repetido_en_el_archivo_gana_la_ultima_posicion(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    d = _ingerir(ctx, tmp_path, "facturacion_2026-01.json",
                 [_factura("F-1", tarifa=1.0), _factura("F-1", tarifa=2.0)])
    assert filas(ctx.con, "select tarifa_cop_kwh::float from core.fact_factura") == [(2.0,)]
    assert filas(ctx.con, "select linea, motivo from quarantine.rechazo") == [(1, "factura_duplicada")]
    assert d.conteos["rechazadas_por_motivo"] == {"factura_duplicada": 1}


def test_entre_archivos_gana_el_mayor_generado_en_y_la_perdedora_va_a_cuarentena(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    a = _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1", tarifa=1.0)])
    _ingerir(ctx, tmp_path, "facturacion_2026-02.json", [_factura("F-1", tarifa=2.0)],
             "2026-02-05T08:00:00-05:00", "2026-02")
    assert filas(ctx.con, "select tarifa_cop_kwh::float, periodo::text from core.fact_factura") == [(2.0, "2026-02-01")]
    # la perdedora es la fila del archivo viejo, con su linea y archivo
    assert filas(ctx.con, "select archivo_id, linea, motivo, payload_cifrado is not null from quarantine.rechazo") == [
        (a.archivo_id, 1, "factura_duplicada", True)]


def test_llegada_fuera_de_orden_la_nueva_pierde_si_su_generado_en_es_menor(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    _ingerir(ctx, tmp_path, "facturacion_2026-02.json", [_factura("F-1", tarifa=2.0)],
             "2026-02-05T08:00:00-05:00", "2026-02")
    d = _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1", tarifa=1.0)])
    assert filas(ctx.con, "select tarifa_cop_kwh::float from core.fact_factura") == [(2.0,)]
    assert d.conteos["cargadas"] == 0 and d.conteos["rechazadas_por_motivo"] == {"factura_duplicada": 1}
    assert filas(ctx.con, "select count(*) from quarantine.rechazo") == [(1,)]


def test_empate_de_generado_en_gana_el_ultimo_por_nombre(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    mismo = "2026-01-05T08:00:00-05:00"
    _ingerir(ctx, tmp_path, "facturacion_b.json", [_factura("F-1", tarifa=2.0)], mismo)
    _ingerir(ctx, tmp_path, "facturacion_a.json", [_factura("F-1", tarifa=1.0)], mismo)
    assert filas(ctx.con, "select tarifa_cop_kwh::float from core.fact_factura") == [(2.0,)]  # 'b' > 'a'
    _ingerir(ctx, tmp_path, "facturacion_c.json", [_factura("F-1", tarifa=3.0)], mismo)
    assert filas(ctx.con, "select tarifa_cop_kwh::float from core.fact_factura") == [(3.0,)]
    assert filas(ctx.con, "select count(*) from quarantine.rechazo where motivo = 'factura_duplicada'") == [(2,)]


def test_archivo_de_facturacion_modificado_se_rechaza_entero(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1", tarifa=1.0)])
    d = _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1", tarifa=9.0), _factura("F-2")])
    assert d.tipo == "rechazado" and d.motivo == "archivo_modificado"
    assert filas(ctx.con, "select factura_id, tarifa_cop_kwh::float from core.fact_factura") == [("F-1", 1.0)]


def test_reingerir_es_idempotente(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    for _ in range(2):
        _ingerir(ctx, tmp_path, "facturacion_2026-01.json", [_factura("F-1"), _factura("F-2", FANTASMA)])
    assert filas(ctx.con, "select (select count(*) from raw.factura), (select count(*) from core.fact_factura), "
                          "(select count(*) from quarantine.rechazo)") == [(2, 1, 1)]


def test_canario_de_pii_ningun_valor_de_facturacion_esta_en_claro_en_ninguna_tabla(ctx, tmp_path, maestros_limpios):
    _maestros(ctx, tmp_path)
    _ingerir(ctx, tmp_path, "facturacion_2026-01.json",
             [_factura("F-1"), _factura("F-2", FANTASMA), _factura("F-3"), _factura("F-3")])
    ctx.con.commit()
    super_ = almacen.conectar(maestros_limpios, maestros_limpios.superusuario, "cauce_test")
    try:
        with super_.cursor() as cur:
            cur.execute("select table_schema || '.' || table_name from information_schema.tables "
                        "where table_schema in ('raw','quarantine','core','analytics','audit','crypto') "
                        "and table_type = 'BASE TABLE'")
            tablas = [t for (t,) in cur.fetchall()]
            for t in tablas:
                cur.execute(f"select t::text from {t} t")  # fila entera como texto; bytea sale en hex
                volcado = "\n".join(r[0] for r in cur.fetchall())
                for valor in PII:
                    assert valor not in volcado, t
                    assert valor.encode().hex() not in volcado, t
    finally:
        super_.close()
    # la cuenta tampoco esta en raw: se descarta antes de cualquier escritura
    assert filas(ctx.con, "select count(*) from raw.factura "
                          "where payload::text like '%%cuenta_enmascarada%%'") == [(0,)]



def test_rechazo_id_de_huerfanas_y_perdedoras_sigue_el_orden_de_archivo_y_linea(ctx, tmp_path):
    _maestros(ctx, tmp_path)
    viejo = _ingerir(ctx, tmp_path, "facturacion_2026-01.json",
                     [_factura("F-3"), _factura("F-1"), _factura("F-2")])
    _ingerir(ctx, tmp_path, "facturacion_2026-02.json",
             [_factura("F-9", FANTASMA2), _factura("F-2"), _factura("F-8", FANTASMA), _factura("F-1"),
              _factura("F-7", FANTASMA)], "2026-02-05T08:00:00-05:00")
    assert [(a == viejo.archivo_id, linea, motivo) for a, linea, motivo in filas(
        ctx.con, "select archivo_id, linea, motivo from quarantine.rechazo order by rechazo_id")] == [
        (False, 1, "frontera_huerfana"), (False, 3, "frontera_huerfana"), (False, 5, "frontera_huerfana"),
        (True, 2, "factura_duplicada"), (True, 3, "factura_duplicada")]
