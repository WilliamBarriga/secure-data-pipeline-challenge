"""alertas.Detector por su interfaz, alimentado por el adapter en memoria de FuenteEventos."""
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from apoyo import BOGOTA, NDJSON
from cauce.bordes.fuente_eventos import FuenteEnMemoria
from cauce.nucleo import fuentes
from cauce.nucleo.alertas import Alerta, Detector, TipoAlerta

F, G = "CO0000000014", "CO0000000012"
INICIO = datetime(2026, 6, 1, tzinfo=BOGOTA)
_ids = iter(range(10**9))


def ts(i: int) -> str:
    """Instante ISO del intervalo `i` de la Rejilla."""
    return (INICIO + timedelta(minutes=15 * i)).isoformat()


def ev(i: int, f: str = F, kwh: float | None = 10.0, evento_id: str | None = None) -> bytes:
    return json.dumps({"evento_id": evento_id or f"EV{next(_ids):07d}", "frontera_id": f, "ts_medicion": ts(i),
                       "kwh": kwh, "medidor": {"serial": "MD000000", "firmware": "1.0.0"}}).encode()


def dia(f: str = F, sin=(), **kw) -> list[bytes]:
    """Un día completo de la Frontera `f` salvo los intervalos de `sin`."""
    return [ev(i, f, **kw) for i in range(96) if i not in set(sin)]


def detectar(eventos, fronteras=(F,), banda=None) -> set[tuple[str, str, int]]:
    """Corre el Detector sobre el adapter en memoria; Alertas como (tipo, frontera, intervalo)."""
    d = Detector(fronteras=fronteras, max_kwh=banda or {f: Decimal("100") for f in fronteras})
    for e in FuenteEnMemoria(eventos).eventos():
        d.procesar(e)
    return {(a.tipo.value, a.frontera_id, int((a.ts - INICIO) / timedelta(minutes=15))) for a in d.cerrar().alertas}


def test_consumo_estrictamente_mayor_que_max_kwh_alerta_con_el_ts_de_la_medicion():
    eventos = dia(sin=range(7, 96)) + [ev(7, kwh=100.001), ev(8, kwh=100)] + dia(sin=range(9))
    assert detectar(eventos) == {("CONSUMO_FUERA_DE_BANDA", F, 7)}


def test_la_alerta_lleva_ts_con_offset_bogota():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("1")})
    for e in dia():
        d.procesar(e)
    a = d.cerrar().alertas[0]
    assert a == Alerta(TipoAlerta.CONSUMO_FUERA_DE_BANDA, F, INICIO)
    assert a.ts.isoformat() == "2026-06-01T00:00:00-05:00"


@pytest.mark.parametrize("ausentes,intervalos", [
    ((10, 11), set()),                              # racha de 2: no alerta
    ((10, 11, 12), {12}),                           # racha de 3: una, en el tercero
    (tuple(range(10, 19)), {12}),                   # racha de 9: una sola
    ((10, 11, 12, 14, 15, 16), {12, 16}),           # dos rachas separadas por un recibido: dos
    ((0, 1, 2, 3), {2}),                            # desde el intervalo 0 cuenta
    ((93, 94, 95), {95}),                           # hasta el 95: se detecta al cierre
    (tuple(range(96)), {2}),                        # Frontera que nunca reporta: 00:30
], ids=["racha_2", "racha_3", "racha_9", "dos_rachas", "desde_0", "hasta_95", "nunca_reporta"])
def test_caida_es_una_alerta_en_el_tercer_intervalo_ausente(ausentes, intervalos):
    assert detectar(dia(sin=ausentes)) == {("FRONTERA_SIN_REPORTE", F, i) for i in intervalos}


def test_nunca_reporta_alerta_a_las_00_30():
    d = Detector(fronteras=(F,), max_kwh={})
    assert [a.ts.isoformat() for a in d.cerrar().alertas] == ["2026-06-01T00:30:00-05:00"]


def test_retroactiva_es_ts_estrictamente_anterior_al_maximo_de_su_frontera():
    # el 20 llega al final: retroactiva en su ts; otro 95 (instante igual al máximo) es duplicado, no retroactiva
    eventos = [ev(i) for i in range(96) if i != 20] + [ev(20), ev(95)]
    assert detectar(eventos) == {("LECTURA_RETROACTIVA", F, 20)}


def test_instante_igual_al_maximo_es_duplicado_y_no_pasa_por_las_reglas():
    # Otra Medición con el ts del máximo de su Frontera es duplicado, aunque
    # traiga otro evento_id y kwh fuera de banda; se cuenta como duplicado
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=range(41, 96)) + [ev(40, kwh=500)] + dia(sin=range(41)):
        d.procesar(e)
    cierre = d.cerrar()
    assert (cierre.alertas, cierre.duplicados, dict(cierre.descartados)) == ((), 1, {})


def test_la_retroactiva_no_mueve_el_maximo():
    # 0..50, luego 10 (retro), luego 40 (retro contra 50, no contra 10)
    eventos = [ev(i) for i in range(51)] + [ev(10), ev(40)] + [ev(i) for i in range(51, 96)]
    assert detectar(eventos) == {("LECTURA_RETROACTIVA", F, 10), ("LECTURA_RETROACTIVA", F, 40)}


def test_retroactiva_fuera_de_banda_da_dos_alertas():
    eventos = [ev(i) for i in range(96)] + [ev(5, kwh=500)]
    assert detectar(eventos) == {("LECTURA_RETROACTIVA", F, 5), ("CONSUMO_FUERA_DE_BANDA", F, 5)}


def test_el_maximo_es_por_frontera():
    eventos = [ev(i, F) for i in range(96)] + [ev(i, G) for i in range(96)]
    assert detectar(eventos, fronteras=(F, G)) == set()


def test_llegada_tardia_cierra_el_hueco():
    eventos = [ev(i) for i in range(96) if i not in (10, 11, 12)] + [ev(11)]
    assert detectar(eventos) == {("LECTURA_RETROACTIVA", F, 11)}


QUITAR = object()


def variante(**cambios) -> bytes:
    """Evento del intervalo 11 de F (fuera de banda si fuera válido) con campos cambiados o quitados."""
    doc = {"evento_id": f"EV{next(_ids):07d}", "frontera_id": F, "ts_medicion": ts(11), "kwh": 999.0,
           "medidor": {"serial": "MD000000", "firmware": "1.0.0"}}
    doc.update(cambios)
    return json.dumps({k: v for k, v in doc.items() if v is not QUITAR}).encode()


@pytest.mark.parametrize("crudo,motivo", [
    (b"{roto", "json_invalido"),
    (b"[1, 2]", "json_invalido"),
    (b"\xff\xfe{}", "json_invalido"),
    (variante(evento_id=QUITAR), "evento_id_invalido"),
    (variante(evento_id=7), "evento_id_invalido"),
    (variante(kwh=None), "kwh_nulo"),
    (variante(kwh=QUITAR), "kwh_nulo"),
    (variante(kwh=-0.5), "kwh_negativo"),
    (variante(kwh="999"), "kwh_no_numerico"),
    (variante(kwh=True), "kwh_no_numerico"),
    (variante(kwh=float("nan")), "kwh_no_numerico"),
    (variante(kwh=float("inf")), "kwh_no_numerico"),
    (variante(frontera_id="CO0000000000"), "frontera_desconocida"),
    (variante(frontera_id=QUITAR), "frontera_desconocida"),
    (variante(ts_medicion="ayer"), "ts_invalido"),
    (variante(ts_medicion="2026-06-01T02:45:00"), "ts_invalido"),       # sin offset
    (variante(ts_medicion=QUITAR), "ts_invalido"),
    (variante(ts_medicion="2026-06-02T00:00:00-05:00"), "ts_fuera_de_rejilla"),
    (variante(ts_medicion="2026-05-31T23:45:00-05:00"), "ts_fuera_de_rejilla"),
    (variante(ts_medicion="2026-06-01T02:50:00-05:00"), "ts_no_alineado"),
    # Correccion: una clave fuera del esquema declarado de mediciones.raw invalida el evento
    (variante(nuevo="x"), "clave_desconocida"),
    (variante(medidor={"serial": "MD000000", "firmware": "1.0.0", "gps": "x"}), "clave_desconocida"),
    (variante(medidor="MD000000"), "clave_desconocida"),
])
def test_invalida_se_descarta_antes_de_las_reglas_y_se_cuenta_por_motivo(crudo, motivo):
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    eventos = dia(sin=(10, 11, 12, 13, 14, 15)) + [crudo]  # tras el 95: válida sería retroactiva y fuera de banda
    assert [d.procesar(e) for e in eventos][-1] == ()
    cierre = d.cerrar()
    # no alerta y no cuenta como reporte: la Caída 10..15 sigue entera
    assert [(a.tipo.value, a.ts) for a in cierre.alertas] == [("FRONTERA_SIN_REPORTE", INICIO + timedelta(minutes=180))]
    assert (cierre.leidos, cierre.duplicados, dict(cierre.descartados)) == (91, 0, {motivo: 1})


def test_ts_con_otro_offset_vale_por_su_instante_y_la_alerta_sale_en_bogota():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=(11,)) + [variante(ts_medicion="2026-06-01T07:45:00+00:00")]:
        d.procesar(e)
    cierre = d.cerrar()
    assert [a.ts.isoformat() for a in cierre.alertas] == ["2026-06-01T02:45:00-05:00"] * 2
    assert dict(cierre.descartados) == {}


def test_evento_id_repetido_se_deduplica():
    eventos = dia() + [ev(5, kwh=500, evento_id="DUP")]
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in [ev(0, evento_id="DUP")] + eventos[1:] + [eventos[-1]]:
        d.procesar(e)
    cierre = d.cerrar()
    assert cierre.alertas == () and (cierre.leidos, cierre.duplicados) == (98, 2)


def test_invalido_con_el_evento_id_de_uno_real_no_lo_suprime():
    # ataque: un inválido hostil llega antes con el evento_id del evento real fuera de banda y del
    # intervalo 11; el real tiene que contar como reporte (no hay Caída 10..12) y alertar igual
    real = ev(11, kwh=500, evento_id="REAL")
    hostil = variante(evento_id="REAL", kwh=None)
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=(10, 11, 12))[:10] + [hostil, real] + dia(sin=range(13)):
        d.procesar(e)
    cierre = d.cerrar()
    assert [(a.tipo.value, a.ts) for a in cierre.alertas] == [
        ("CONSUMO_FUERA_DE_BANDA", INICIO + timedelta(minutes=165))]
    assert (cierre.duplicados, dict(cierre.descartados)) == (0, {"kwh_nulo": 1})


def test_alimentacion_doble_no_cambia_alertas_ni_descartes():
    eventos = dia(sin=(10, 11, 12)) + [ev(5, kwh=500), b"{roto", variante(kwh=None)]
    simple, doble = (Detector(fronteras=(F,), max_kwh={F: Decimal("100")}) for _ in range(2))
    for e in eventos:
        simple.procesar(e)
    for e in eventos + eventos:
        doble.procesar(e)
    a, b = simple.cerrar(), doble.cerrar()
    assert a.alertas == b.alertas and len(a.alertas) == 3
    # el inválido con evento_id se cuenta una vez; el JSON roto no tiene evento_id y se cuenta cada vez
    assert dict(a.descartados) == {"json_invalido": 1, "kwh_nulo": 1}
    assert dict(b.descartados) == {"json_invalido": 2, "kwh_nulo": 1}
    assert b.duplicados == len(eventos) - 1


def test_cada_frontera_del_universo_se_juzga_por_separado():
    assert detectar(dia(F) + dia(G, sin=(40, 41, 42)), fronteras=(F, G)) == {("FRONTERA_SIN_REPORTE", G, 42)}


def test_con_margen_la_caida_se_confirma_por_marca_de_agua_y_no_se_retira():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")}, margen=2)
    emitidas = {i: d.procesar(e) for i, e in enumerate(dia(sin=(10, 11, 12)))}
    caida = Alerta(TipoAlerta.FRONTERA_SIN_REPORTE, F, INICIO + timedelta(minutes=180))
    # el 13 es el índice 10 de la lista: la marca de agua 15 (12 + margen + 1) es el índice 12
    assert [i for i, a in emitidas.items() if a] == [12] and emitidas[12] == (caida,)
    assert d.procesar(ev(11)) == (Alerta(TipoAlerta.LECTURA_RETROACTIVA, F, INICIO + timedelta(minutes=165)),)
    assert caida in d.cerrar().alertas  # ya no hay racha de 3 al cierre, pero la emitida queda


def test_las_alertas_del_stream_sintetico_son_la_verdad_plantada(sinteticos):
    # oraculo: la Verdad plantada del generador (Caidas, fuera de banda con kwh > max_kwh y retroactivas), nunca
    # un conteo fijo de una entrada concreta
    verdad = sinteticos.verdad
    maestro = fuentes.leer("frontera", "fronteras.csv", sinteticos.leer("maestros/fronteras.csv"))
    limites = fuentes.leer("limite", "limites_frontera.csv", sinteticos.leer("maestros/limites_frontera.csv"))
    d = Detector(fronteras=(r.campos["frontera_id"] for r in maestro.registros),
                 max_kwh={r.campos["frontera_id"]: r.campos["max_kwh"] for r in limites.registros})
    fuente = FuenteEnMemoria.desde_ndjson(sinteticos.leer(str(NDJSON)))
    for e in fuente.eventos():
        d.procesar(e)
    cierre = d.cerrar()
    assert cierre.por_tipo() == {"FRONTERA_SIN_REPORTE": verdad["stream"]["caidas"],
                                 "CONSUMO_FUERA_DE_BANDA": verdad["stream"]["fuera_de_banda"],
                                 "LECTURA_RETROACTIVA": verdad["stream"]["retroactivas"]}
    assert [{"tipo": a.tipo.value, "frontera_id": a.frontera_id, "ts": a.ts.isoformat()}
            for a in sorted(set(cierre.alertas), key=lambda a: (a.tipo.value, a.frontera_id, a.ts))
            ] == verdad["alertas"]  # la Verdad plantada ya viene ordenada por (tipo, frontera_id, ts)
    assert (cierre.leidos, cierre.duplicados, dict(cierre.descartados)) == (
        verdad["stream"]["eventos"], verdad["stream"]["duplicados"], verdad["stream"]["invalidos_por_motivo"])
    assert {a.ts.utcoffset() for a in cierre.alertas} == {timedelta(hours=-5)}


def test_medidor_vacio_o_ausente_no_es_clave_desconocida():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=(11,)) + [variante(medidor=QUITAR)]:
        d.procesar(e)
    assert dict(d.cerrar().descartados) == {}


def test_el_cierre_deriva_invalidos_y_validos_de_sus_conteos():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=(5,)) + [b"{roto", ev(5, kwh=-1), ev(5, kwh=None)] + [ev(0, evento_id="EVDUP")] * 2:
        d.procesar(e)
    cierre = d.cerrar()
    assert (cierre.leidos, cierre.invalidos, cierre.duplicados) == (100, 3, 1)
    assert cierre.validos == cierre.leidos - cierre.invalidos - cierre.duplicados == 96


def test_resumen_es_la_forma_unica_del_cierre_para_log_y_metricas():
    d = Detector(fronteras=(F,), max_kwh={F: Decimal("100")})
    for e in dia(sin=(10, 11, 12)) + [b"{roto"]:
        d.procesar(e)
    r = d.cerrar().resumen()
    assert list(r) == ["eventos_leidos", "invalidos", "invalidos_por_motivo", "duplicados", "alertas_por_tipo"]
    assert r == {"eventos_leidos": 94, "invalidos": 1, "invalidos_por_motivo": {"json_invalido": 1}, "duplicados": 0,
                 "alertas_por_tipo": {"FRONTERA_SIN_REPORTE": 1, "CONSUMO_FUERA_DE_BANDA": 0,
                                      "LECTURA_RETROACTIVA": 0}}
