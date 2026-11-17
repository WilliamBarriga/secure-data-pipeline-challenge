#!/usr/bin/env python3
"""Publica las Mediciones de un ndjson en un topico de Kafka/Redpanda, en el orden del archivo.

Es el lado productor de `make stream`: `python -m cauce producir` lo invoca con `--rate 0` y una cota de
tiempo. Cada linea no vacia es un evento; viaja como JSON con `frontera_id` como llave, asi el broker
mantiene el orden por Frontera dentro de su particion. Un evento sin `frontera_id` de texto viaja sin llave.

    python3 tools/producer.py --topic mediciones.raw --bootstrap localhost:9092
    python3 tools/producer.py --rate 500        # eventos por segundo; 0 = sin pausa
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from kafka import KafkaProducer
except ImportError:
    print("producer: falta kafka-python (esta en requirements.txt; corre `make setup`)", file=sys.stderr)
    raise

ARCHIVO_POR_DEFECTO = Path(__file__).resolve().parent.parent / "data" / "eventos" / "mediciones_stream.ndjson"


def _argumentos(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="publica un ndjson de Mediciones en un topico")
    p.add_argument("--archivo", type=Path, default=ARCHIVO_POR_DEFECTO)
    p.add_argument("--topic", default=os.getenv("KAFKA_TOPIC", "mediciones.raw"))
    p.add_argument("--bootstrap", default=os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
    p.add_argument("--rate", type=int, default=1000, help="eventos por segundo (0 = sin pausa)")
    return p.parse_args(argv)


def _eventos(archivo: Path):
    with archivo.open(encoding="utf-8") as fh:
        for linea in fh:
            if linea.strip():
                yield json.loads(linea)


def main(argv: list[str] | None = None) -> int:
    args = _argumentos(argv)
    pausa = 1.0 / args.rate if args.rate > 0 else 0.0
    productor = KafkaProducer(bootstrap_servers=args.bootstrap)
    publicados = 0
    for evento in _eventos(args.archivo):
        frontera = evento.get("frontera_id") if isinstance(evento, dict) else None
        llave = frontera.encode("utf-8") if isinstance(frontera, str) and frontera else None
        productor.send(args.topic, key=llave, value=json.dumps(evento).encode("utf-8"))
        publicados += 1
        if pausa:
            time.sleep(pausa)
    productor.flush()
    productor.close()
    print(f"{publicados} eventos publicados en {args.topic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
