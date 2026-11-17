"""`bitacora.registrar_evento` rechaza en Python lo que el CHECK de `audit.bitacora_corrida` rechazaria."""
import uuid

import pytest

from cauce.bordes import bitacora

IDENT = bitacora.Identidad(corrida_id=uuid.uuid4(), ejecutor="u@h", comando="run", version_codigo="v1")


class ConSinUso:
    def cursor(self):
        raise AssertionError("no debe llegar a la base")


@pytest.mark.parametrize("kw", [{"evento": "otra_cosa"}, {"resultado": "quiza"},
                                {"codigo_error": "valor con Juan Perez"}])
def test_valor_fuera_de_catalogo_falla_antes_de_la_base(kw):
    args = {"evento": "corrida_fin", **kw}
    evento = args.pop("evento")
    with pytest.raises(ValueError) as e:
        bitacora.registrar_evento(ConSinUso(), IDENT, evento, **args)
    assert "Juan Perez" not in str(e.value)  # el mensaje nombra el campo, nunca el valor


def test_el_catalogo_de_eventos_y_resultados_es_el_del_ddl():
    assert {e.value for e in bitacora.Evento} == {"corrida_inicio", "etapa_fin", "archivo_cargado",
                                                  "archivo_omitido", "archivo_rechazado", "corrida_fin"}
    assert {r.value for r in bitacora.Resultado} == {"ok", "parcial", "fallo"}
