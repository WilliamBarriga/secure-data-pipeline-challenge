import pytest

from cauce.nucleo.pii import TipoPII, detectar_estricto, enmascarar_estricto


@pytest.mark.parametrize("texto, esperado", [
    ("contacto juan.perez@example.org hoy", (TipoPII.CORREO,)),
    ("cartera3@empresa.com.co", (TipoPII.CORREO,)),
    ("tel +57 399 023 4567", (TipoPII.CELULAR,)),
    ("tel +573990234567", (TipoPII.CELULAR,)),
    ("tel 399 023 4567", (TipoPII.CELULAR,)),
    ("tel 399-023-4567", (TipoPII.CELULAR,)),
    ("CC 1.234.567", (TipoPII.CEDULA,)),
    ("C.C. 12345678", (TipoPII.CEDULA,)),
    ("cedula No. 1234567890", (TipoPII.CEDULA,)),
    ("cédula #987654321", (TipoPII.CEDULA,)),
    ("a@b.test y CC 1234567 y 399 055 1234", (TipoPII.CORREO, TipoPII.CEDULA, TipoPII.CELULAR)),
    # falsos positivos que NO deben marcarse
    ("frontera CO3990000001", ()),            # parece celular 3XXXXXXXXX pegado a letras
    ("frontera_id=CO1234567890", ()),
    ("corrida 20260101123456", ()),            # corrida larga de digitos sin prefijo: sin fallback de 7
    ("1234567", ()),
    ("3990234567", ()),                        # celular pelado: sin separador ni +57
    ("filas_leidas=1200 archivo consumos_2026-06.csv", ()),
])
def test_detectar_estricto(texto, esperado):
    assert detectar_estricto(texto) == esperado


def test_enmascarar_estricto_usa_tokens_tipados_sin_fragmentos():
    texto, tipos = enmascarar_estricto("mail a@b.test tel +57 399 023 4567 CC 1234567")
    assert texto == "mail [EMAIL] tel [CELULAR] [CEDULA]" or "[EMAIL]" in texto
    assert "a@b.test" not in texto and "4567" not in texto and "1234567" not in texto
    assert set(tipos) == {TipoPII.CORREO, TipoPII.CELULAR, TipoPII.CEDULA}
    assert detectar_estricto(texto) == ()


# Un corrida_id (uuid4) cuyo primer o ultimo bloque es "cc" + digitos se leia como "CC <cedula>":
# metrics.json no se escribia y `make run` salia 2 al azar (hallado en `make e2e`).
@pytest.mark.parametrize("uuid_", [
    "cc345678-e04f-4f00-8000-000000000000",
    "00000000-0000-4000-8000-cc1234567890",
    "CC345678-E04F-4F00-8000-000000000000",
])
def test_un_uuid_no_es_pii(uuid_):
    linea = f'{{"corrida_id": "{uuid_}", "evento": "fin"}}'
    assert detectar_estricto(linea) == ()
    assert enmascarar_estricto(linea) == (linea, ())


def test_la_pii_junto_a_un_uuid_se_sigue_detectando():
    texto, tipos = enmascarar_estricto("cc345678-e04f-4f00-8000-000000000000 CC 1234567")
    assert texto == "cc345678-e04f-4f00-8000-000000000000 [CEDULA]"
    assert tipos == (TipoPII.CEDULA,)
