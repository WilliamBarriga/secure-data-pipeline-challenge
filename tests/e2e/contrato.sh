#!/usr/bin/env bash
# e2e del contrato con RUNTIME=docker partiendo sin out/ ni logs/ y sin data/ real: genera Datos sinteticos con
# semilla fija en un temporal (CAUCE_DATA) y contrasta out/ contra su Verdad plantada (fuera de ese temporal).
# Requiere Docker y credenciales.yaml con passwords (make credenciales). Uso: make e2e
set -euo pipefail
cd "$(dirname "$0")/../.."
export RUNTIME=docker

paso() { printf '\n== %s\n' "$*"; }

TRABAJO="$(mktemp -d)"
trap 'rm -rf -- "${TRABAJO:?}"' EXIT
export CAUCE_DATA="$TRABAJO/data"
mkdir -p "$TRABAJO/verdad" "$TRABAJO/run1"

# out/ contra la Verdad plantada, en el contenedor `app` (solo nombres y conteos)
contrastar() {
  docker compose $(test -f estado/.env && echo --env-file estado/.env) --profile app run --rm --no-deps \
    --user "$(id -u):$(id -g)" -v "$TRABAJO/verdad:/trabajo:ro" \
    app python tests/e2e/contrastar.py /trabajo/verdad.json out "$@"
}

paso "estado inicial limpio (clean dos veces: idempotente)"
make clean
make clean
[ ! -e out ] && [ ! -e logs ] && [ ! -e estado/.env ]

paso "Datos sinteticos con semilla fija en CAUCE_DATA y su Verdad plantada fuera de ese directorio"
make datos VERDAD="$TRABAJO/verdad/verdad.json"
[ -s "$TRABAJO/verdad/verdad.json" ] && [ -z "$(find "$CAUCE_DATA" -name 'verdad*')" ]

paso "secuencia exacta del brief §9, con sus topes (run 900 s, stream 300 s); el segundo run deja out/ igual"
make setup && make up && timeout 900 make run && cp out/consumo_mensual.csv out/dq_report.json "$TRABAJO/run1/" \
  && timeout 900 make run && cmp out/consumo_mensual.csv "$TRABAJO/run1/consumo_mensual.csv" \
  && cmp out/dq_report.json "$TRABAJO/run1/dq_report.json" && timeout 300 make stream \
  && make test && make down \
  || { echo "contrato: la secuencia exacta no termino en 0" >&2; exit 1; }  # set -e no corta dentro de un &&
cp out/alertas.json "$TRABAJO/alertas.contrato.json"

paso "out/ y logs/ los creo el usuario del host, no root"
[ "$(stat -c %u out)" = "$(id -u)" ] && [ "$(stat -c %u logs)" = "$(id -u)" ]
[ "$(stat -c %a estado/.env)" = "600" ]

paso "el secreto heredado nunca estuvo en el historial"
[ -z "$(git log --all --oneline -- config/pipeline.yaml)" ]

paso "down conserva volumenes, .env, out/ y logs/ (segundo down: idempotente)"
make down
[ -f estado/.env ] && [ -d out ] && [ -d logs ]
proyecto="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr 'A-Z' 'a-z')}"
[ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$proyecto")" ]

paso "up tras down reutiliza el volumen; run otra vez y out/ coincide con la Verdad plantada"
make up
timeout 900 make run
contrastar --alertas

paso "stream re-ejecutado con el topico crecido da el mismo alertas.json"
timeout 300 make stream
cmp out/alertas.json "$TRABAJO/alertas.contrato.json"
contrastar --alertas

paso "clean borra todo (dos veces)"
make clean
make clean
[ ! -e out ] && [ ! -e logs ] && [ ! -e estado/.env ]
echo "contrato OK"
