"""pii.enmascarar: PII en texto libre -> marcadores tipados; el original no sobrevive."""
import pytest

from cauce.nucleo import pii
from cauce.nucleo.pii import TipoPII


@pytest.mark.parametrize("texto,esperado,tipos", [
    ("Enviar a cartera2@empresa.example.com ya", "Enviar a [EMAIL] ya", [TipoPII.CORREO]),
    ("Contacto: juan.perez+x@empresa.com.test", "Contacto: [EMAIL]", [TipoPII.CORREO]),
    ("tel 3990234567", "tel [CELULAR]", [TipoPII.CELULAR]),
    ("cel +57 399 023 4567 ok", "cel [CELULAR] ok", [TipoPII.CELULAR]),
    ("cel 399-023-4567", "cel [CELULAR]", [TipoPII.CELULAR]),
    ("CC 12.345.678 del rep", "[CEDULA] del rep", [TipoPII.CEDULA]),
    ("cedula: 1234567", "[CEDULA]", [TipoPII.CEDULA]),
    ("doc 87654321 pendiente", "doc [CEDULA] pendiente", [TipoPII.CEDULA]),  # fallback >= 7 digitos
    ("Sin novedades.", "Sin novedades.", []),
    ("piso 12 y 123456", "piso 12 y 123456", []),  # menos de 7 digitos no es PII
])
def test_enmascara_por_tipo(texto, esperado, tipos):
    r = pii.enmascarar(texto)
    assert r.texto == esperado
    assert list(r.hallazgos) == tipos


def test_varios_hallazgos_en_orden_de_aparicion():
    r = pii.enmascarar("a@example.com / 3990234567 / CC 1234567")
    assert r.texto == "[EMAIL] / [CELULAR] / [CEDULA]"
    assert list(r.hallazgos) == [TipoPII.CORREO, TipoPII.CELULAR, TipoPII.CEDULA]


def test_resultado_no_contiene_el_original():
    r = pii.enmascarar("x 3990998877 y 5551234567")
    assert "3990998877" not in r.texto and "5551234567" not in r.texto
