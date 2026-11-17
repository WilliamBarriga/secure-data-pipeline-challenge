import os
from pathlib import Path

import pytest

from apoyo import Sinteticos, generar_sinteticos
from cauce import config
from cauce.arranque import bootstrap
from cauce.bordes import almacen

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _salidas_temporales(tmp_path_factory):
    """`make test` escribe logs a logs/test.log y out/ a un temporal (el e2e hereda el entorno)."""
    logs = RAIZ / "logs"
    logs.mkdir(exist_ok=True)
    previo = {k: os.environ.get(k) for k in ("CAUCE_LOG", "CAUCE_OUT")}
    os.environ["CAUCE_LOG"] = str(logs / "test.log")
    os.environ["CAUCE_OUT"] = str(tmp_path_factory.mktemp("out"))
    yield
    for k, v in previo.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session", autouse=True)
def sinteticos(tmp_path_factory) -> Sinteticos:
    """Datos sinteticos de la sesion, con semilla fija, en un temporal; `CAUCE_DATA` apunta ahi (nunca a `data/`).
    La Verdad plantada queda fuera de ese temporal de datos. Los tests que modifican archivos copian antes."""
    s = generar_sinteticos(tmp_path_factory.mktemp("sinteticos"))
    previo = os.environ.get("CAUCE_DATA")
    os.environ["CAUCE_DATA"] = str(s.data)
    yield s
    if previo is None:
        os.environ.pop("CAUCE_DATA", None)
    else:
        os.environ["CAUCE_DATA"] = previo


@pytest.fixture(scope="session")
def cauce_test():
    """Config apuntando a cauce_test, recreada con el mismo DDL. Falla (no salta) si la infra esta abajo."""
    ruta_env = Path(os.environ.get("CAUCE_ESTADO", RAIZ / "estado")) / ".env"
    try:
        cfg = config.cargar(ruta_env)
    except config.ErrorConfig as e:
        pytest.fail(f"{e}. `make test` necesita la infra: corre `make up` primero.", pytrace=False)
    try:
        bootstrap.bootstrap(cfg, database="cauce_test", recrear=True)
    except (bootstrap.ErrorBootstrap, almacen.ErrorConexion) as e:
        pytest.fail(f"{e}. `make test` necesita la infra: corre `make up` primero.", pytrace=False)
    return cfg.model_copy(update={"database": "cauce_test"})


@pytest.fixture()
def maestros_limpios(cauce_test):
    """cauce_test sin archivos, Cuarentena ni Fronteras (la Bitacora no se puede vaciar: queda, y es lo esperado)."""
    con = almacen.conectar(cauce_test, cauce_test.superusuario, "cauce_test")
    try:
        with con.cursor() as cur:
            cur.execute("TRUNCATE raw.archivo, quarantine.rechazo, core.dim_frontera, core.dim_cliente "
                        "RESTART IDENTITY CASCADE")
        con.commit()
    finally:
        con.close()
    return cauce_test
