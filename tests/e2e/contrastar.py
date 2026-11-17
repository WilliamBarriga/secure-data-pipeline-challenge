"""Contrasta las salidas del contrato (`out/`) con la Verdad plantada del generador (solo stdlib; lo usa
tests/e2e/contrato.sh dentro del contenedor `app`). Imprime solo nombres y conteos, nunca valores de fila.

Uso: python tests/e2e/contrastar.py VERDAD.json DIR_OUT [--alertas]
Exit 1 si alguna salida no coincide.
"""
import csv
import io
import json
import sys
from pathlib import Path


def contrastar(verdad: dict, out: Path, alertas: bool) -> list[str]:
    """Nombres de las salidas que no coinciden con la Verdad plantada ([] si todas coinciden)."""
    fallas = []
    dq = json.loads((out / "dq_report.json").read_text(encoding="utf-8"))
    fallas += [f"dq_report.{k}" for k in sorted(set(dq) | set(verdad["dq_report"]))
               if dq.get(k) != verdad["dq_report"].get(k)]
    filas = list(csv.reader(io.StringIO((out / "consumo_mensual.csv").read_text(encoding="utf-8"))))
    esperadas = [[f["frontera_id"], f["mes"], f["consumo_kwh"], str(f["horas_con_dato"])]
                 for f in verdad["consumo_mensual"]]
    if filas[1:] != esperadas:
        fallas.append(f"consumo_mensual.csv ({len(filas) - 1} filas, esperadas {len(esperadas)})")
    if alertas:
        emitidas = json.loads((out / "alertas.json").read_text(encoding="utf-8"))
        if emitidas != verdad["alertas"]:
            fallas.append(f"alertas.json ({len(emitidas)} Alertas, esperadas {len(verdad['alertas'])})")
    return fallas


def main(argv: list[str]) -> int:
    verdad = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    fallas = contrastar(verdad, Path(argv[1]), "--alertas" in argv[2:])
    for f in fallas:
        print(f"contrastar: no coincide con la Verdad plantada: {f}", file=sys.stderr)
    if not fallas:
        print(f"contrastar: out/ coincide con la Verdad plantada ({len(verdad['dq_report'])} llaves de dq_report, "
              f"{len(verdad['consumo_mensual'])} filas de consumo_mensual"
              + (f", {len(verdad['alertas'])} Alertas)" if "--alertas" in argv[2:] else ")"))
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
