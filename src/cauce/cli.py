"""python -m cauce <preparar|bootstrap|credenciales|run|producir|stream|auditar-salidas>.
Rutas y Kafka: `entorno.resolver(os.environ)`, una vez por proceso; los runners reciben lo que necesitan."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

from cauce import config, entorno
from cauce.aplicacion import corrida, corrida_batch, corrida_stream
from cauce.arranque import bootstrap as _bootstrap
from cauce.arranque import credenciales, preparar
from cauce.bordes import fuente_eventos, salidas
from cauce.bordes.observabilidad import Observabilidad, RelojSistema
from cauce.nucleo import fuentes

# contrato unico: toda excepcion de dominio expone `codigo_error` (catalogo de bitacora.CODIGOS_ERROR)

# Cota de `make stream` < 300 s (brief §9): asegurar_topico <= 20 s + producer <= 30 s (3795 eventos a --rate 0
# tardan ~2 s) + stream <= tope de FuenteKafka (210 s, conexion incluida) + arranque, clasificacion y escrituras.
TIMEOUT_PRODUCER_S = 30.0


def _producir(e: entorno.Entorno) -> int:
    """Asegura el topico (auto-create apagado) y corre `tools/producer.py --rate 0` hasta terminar,
    con el ndjson de CAUCE_DATA/data. Si el producer pasa de TIMEOUT_PRODUCER_S se corta y falla (exit 1)."""
    creado = fuente_eventos.asegurar_topico(e.kafka_bootstrap, e.kafka_topico)
    print(f"producir: topico {e.kafka_topico} {'creado' if creado else 'ya existia'}")
    (archivo,) = fuentes.ORIGEN_STREAM.rutas(e.data)
    # los avisos de deprecacion de kafka-python son ruido en la salida de `make stream`
    comando = [sys.executable, "-W", "ignore::DeprecationWarning", str(e.raiz / "tools" / "producer.py"),
               "--rate", "0", "--topic", e.kafka_topico, "--bootstrap", e.kafka_bootstrap, "--archivo", str(archivo)]
    try:
        return subprocess.run(comando, check=False, timeout=TIMEOUT_PRODUCER_S).returncode
    except subprocess.TimeoutExpired:
        print(f"cauce producir: timeout de {TIMEOUT_PRODUCER_S:g} s del producer; revisa Redpanda (`make up`)",
              file=sys.stderr)
        return 1


def _auditar_salidas(directorios: list[str], raiz: Path) -> int:
    """`make auditar-salidas`: 0 sin PII, 1 con hallazgos (archivo y tipos, nunca el valor), 2 si falta un
    directorio. Corre sobre lo ya escrito: no necesita base ni estado/.env."""
    rutas = [Path(d) for d in directorios] or [raiz / "out", raiz / "logs"]
    try:
        hallazgos = salidas.auditar(rutas)
    except FileNotFoundError as e:
        print(f"cauce auditar-salidas: falta {e}; corre `make run` (y `make stream`) primero", file=sys.stderr)
        return 2
    for ruta, tipos in hallazgos.items():
        print(f"auditar-salidas: {ruta}: PII detectada ({', '.join(t.value for t in tipos)})", file=sys.stderr)
    if hallazgos:
        return 1
    print(f"auditar-salidas: {', '.join(map(str, rutas))} sin PII (detector estricto)")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cauce")
    sub = p.add_subparsers(dest="comando", required=True)
    c = sub.add_parser("credenciales", help="reemplaza los TODO de password en credenciales.yaml")
    c.add_argument("--rotar", action="store_true", help="reescribe todos los passwords")
    sub.add_parser("preparar", help="credenciales.yaml -> estado/.env (0600)")
    b = sub.add_parser("bootstrap", help="roles + DDL (idempotente por huella)")
    b.add_argument("--database")
    b.add_argument("--recrear", action="store_true", help="DROP + CREATE de la base (cauce_test)")
    k = sub.add_parser("clasificacion", help="check fail-fast de data_classification.yaml contra la base")
    k.add_argument("--database")
    sub.add_parser("run", help="Corrida batch")
    sub.add_parser("producir", help="asegura el topico y corre tools/producer.py --rate 0 hasta terminar")
    sub.add_parser("stream", help="Corrida del stream: consume hasta el high watermark de Kafka y termina")
    a = sub.add_parser("auditar-salidas", help="pii.detectar_estricto sobre out/ y logs/ ya escritos")
    a.add_argument("directorios", nargs="*", help="defecto: out logs (relativos a CAUCE_RAIZ)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    e = entorno.resolver(os.environ)
    try:
        if args.comando == "credenciales":
            texto = e.credenciales.read_text(encoding="utf-8") if e.credenciales.exists() else ""
            if not texto.strip():  # falta o vacio (`make up` lo deja vacio antes del bind mount)
                texto = credenciales.plantilla()
            nuevo = credenciales.rellenar_credenciales(texto, rotar=args.rotar)
            if nuevo != texto:
                # en sitio: credenciales.yaml puede ser un bind mount de un solo archivo
                with open(e.credenciales, "w", encoding="utf-8") as fh:
                    fh.write(nuevo)
            print("credenciales.yaml: credenciales " + ("rotadas" if args.rotar else "completas"))
            return 0
        if args.comando == "preparar":
            preparar.preparar(e.credenciales, e.env)
            print("estado/.env listo")
            return 0
        if args.comando == "producir":
            return _producir(e)
        if args.comando == "auditar-salidas":
            return _auditar_salidas(args.directorios, e.raiz)
        cfg = config.cargar(e.env)
        if args.comando == "bootstrap":
            r = _bootstrap.bootstrap(cfg, database=args.database, recrear=args.recrear)
            print(f"bootstrap: DDL {r.value}, roles al dia")
            return 0
        if args.comando == "clasificacion":
            if args.database:
                cfg = cfg.model_copy(update={"database": args.database})
            corrida.verificar_clasificacion(cfg, e.clasificacion)
            print("clasificacion: completa")
            return 0
        # un solo id por Corrida: el de la Bitacora y el de los logs. CAUCE_LOG: tests -> logs/test.log
        corrida_id = uuid.uuid4()
        obs = Observabilidad.abrir(args.comando, str(corrida_id), e.log, {}, RelojSistema())  # logs.*: clasificar()
        obs.instalar_excepthook()
        try:
            if args.comando == "stream":
                fuente = fuente_eventos.FuenteKafka(e.kafka_bootstrap, e.kafka_topico)
                return corrida_stream.correr_stream(cfg, obs, corrida_id, fuente, e.clasificacion, dir_datos=e.data)
            return corrida_batch.correr_batch(cfg, obs, corrida_id, e.clasificacion, dir_datos=e.data)
        finally:
            obs.liberar()
    except Exception as exc:
        if not isinstance(getattr(exc, "codigo_error", None), str):
            raise
        print(f"cauce {args.comando}: {exc}", file=sys.stderr)
        return 2
