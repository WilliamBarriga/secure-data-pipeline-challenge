# ===========================================================================
# cauce: contrato de ejecucion (brief §9)
#
# Secuencia del operador: datos -> setup -> up -> run -> stream -> test -> down.
# Los nombres de estos targets y lo que dejan en out/ y logs/ son el contrato:
# se puede cambiar como se implementan, no como se llaman ni que producen.
# El resto de los targets (lint, e2e, auditar, infra-*) son herramientas.
# ===========================================================================

.PHONY: datos setup up run stream test down clean credenciales e2e lint infra-validar infra-lock auditar auditar-salidas

# RUNTIME=docker (defecto, ADR 0002) corre la app en el contenedor `app`;
# RUNTIME=local usa el .venv del host. Mismo contrato de rutas en ambos:
# data/ ro; out/ logs/ estado/ rw; .env vive en estado/.env.
RUNTIME ?= docker
VENV_STAMP := .venv/.requirements.sha256
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# --env-file solo si estado/.env ya existe (lo evalua el shell al ejecutar).
COMPOSE = docker compose $$(test -f estado/.env && echo --env-file estado/.env)

ifeq ($(RUNTIME),local)
PY = env PYTHONPATH=src CAUCE_RAIZ=. .venv/bin/python
else ifeq ($(RUNTIME),docker)
PY = $(COMPOSE) --profile app run --rm --no-deps --user "$$(id -u):$$(id -g)" app python
else
$(error RUNTIME debe ser docker o local)
endif
CAUCE = $(PY) -m cauce

# El operador puede borrar out/ y logs/; si los recrea Docker quedan de root.
PREPARAR_DIRS = mkdir -p out logs estado

## Datos sinteticos en data/ (tools/sinteticos.py, solo stdlib). Aborta si data/ ya tiene archivos. Flags en ARGS:
## make datos ARGS="--forzar" | ARGS="--mes 2026-05" | ARGS="--corromper data/consumos/consumos_2026-01.csv".
## VERDAD=ruta escribe el manifiesto de Verdad plantada (fuera de data/). CAUCE_DATA=ruta cambia el directorio de
## datos aca y en run/stream (docker lo monta en /app/data); sin ella es data/.
## Corre antes de `make setup`: en docker construye la imagen si falta; en local usa el python3 del host.
DATOS = $${CAUCE_DATA:-data}
DIR_VERDAD = $(dir $(abspath $(VERDAD)))
datos:
	mkdir -p "$(DATOS)" $(if $(VERDAD),"$(DIR_VERDAD)")
ifeq ($(RUNTIME),docker)
	docker image inspect cauce-app:local >/dev/null 2>&1 || $(COMPOSE) --profile app build app
	$(COMPOSE) --profile app run --rm --no-deps --user "$$(id -u):$$(id -g)" \
	  $(if $(VERDAD),-v "$(DIR_VERDAD):/app/verdad") \
	  datos python tools/sinteticos.py data $(if $(VERDAD),--verdad "verdad/$(notdir $(VERDAD))") $(ARGS)
else
	python3 tools/sinteticos.py "$(DATOS)" $(if $(VERDAD),--verdad "$(abspath $(VERDAD))") $(ARGS)
endif

## Instala dependencias. Idempotente: la imagen se reconstruye con cache y el
## .venv se instala solo si cambio el hash de requirements.txt + requirements-dev.txt.
setup:
ifeq ($(RUNTIME),docker)
	$(COMPOSE) --profile app build app
endif
	@h=$$(cat requirements.txt requirements-dev.txt | sha256sum | cut -d' ' -f1); \
	if [ -x .venv/bin/python ] && [ "$$(cat $(VENV_STAMP) 2>/dev/null)" = "$$h" ]; then \
	  echo "setup: .venv al dia"; \
	elif python3 -m venv .venv && .venv/bin/pip install -q --require-hashes -r requirements.txt -r requirements-dev.txt; then \
	  echo "$$h" > $(VENV_STAMP); echo "setup: .venv listo"; \
	elif [ "$(RUNTIME)" = "docker" ]; then \
	  echo "setup: aviso: no se pudo crear .venv (best-effort)"; \
	else echo "setup: no se pudo crear .venv"; exit 1; fi

## Reemplaza los TODO de password de credenciales.yaml por valores aleatorios
## (make credenciales ROTAR=1 los reescribe todos). Unico lugar con credenciales.yaml rw.
credenciales:
	$(PREPARAR_DIRS)
ifeq ($(RUNTIME),docker)
	$(COMPOSE) --profile app run --rm --no-deps --user "$$(id -u):$$(id -g)" \
	  credenciales python -m cauce credenciales $(if $(ROTAR),--rotar)
else
	$(CAUCE) credenciales $(if $(ROTAR),--rotar)
endif

## Levanta la infraestructura local y espera a que este lista. Si falta credenciales.yaml lo genera
## (como `make credenciales`; el archivo vacio evita que Docker cree un directorio al montarlo); si existe no lo toca.
up:
	$(PREPARAR_DIRS)
	@test -e credenciales.yaml || : > credenciales.yaml
	@test -s credenciales.yaml || $(MAKE) --no-print-directory credenciales
	$(CAUCE) preparar
	$(COMPOSE) up -d --wait postgres redpanda
	$(CAUCE) bootstrap

## Ejecuta el pipeline batch completo de punta a punta (idempotente).
## Al terminar deben existir: out/consumo_mensual.csv, out/dq_report.json,
## out/metrics.json (de esta Corrida) y logs/pipeline.log.
run:
	$(PREPARAR_DIRS)
	$(CAUCE) run

## Procesa el stream y termina solo (< 300 s). Deja out/alertas.json y la subclave `stream` de metrics.json.
## producir: crea mediciones.raw si falta (auto-create apagado) y corre tools/producer.py --rate 0, cortado a 30 s;
## stream: consume desde earliest hasta el high watermark de Kafka (offset final por particion) tomado despues de
## producir (assign, sin consumer group), con tope de 210 s conexion incluida. Cota total en src/cauce/cli.py.
stream:
	$(PREPARAR_DIRS)
	$(CAUCE) producir
	$(CAUCE) stream

## Recrea cauce_test con el mismo DDL y corre unit + integracion + e2e.
test:
	$(PREPARAR_DIRS)
	$(CAUCE) bootstrap --database cauce_test --recrear
	$(CAUCE) clasificacion --database cauce_test
	$(PY) -m pytest tests

## Lint con ruff (ruff.toml). Fuera de `make test` a proposito: el estilo no tumba el contrato.
lint:
	$(PY) -m ruff check --no-cache src tests tools

## Auditorias de dependencias y de salidas. Fuera de `make test`: `auditar` necesita red (consulta la base de
## vulnerabilidades de PyPI) y `auditar-salidas` mira el out/ y logs/ reales que dejaron `make run` y `make stream`.
## auditar: pip-audit sobre los requirements*.txt fijados con hashes, sin instalar nada (--disable-pip).
auditar:
	$(PY) -m pip_audit --disable-pip --require-hashes --progress-spinner off -r requirements.txt -r requirements-dev.txt

## auditar-salidas: pii.detectar_estricto archivo por archivo; exit 1 nombrando archivo y tipo, nunca el valor.
auditar-salidas:
	$(CAUCE) auditar-salidas out logs

## Contrato completo desde cero con RUNTIME=docker, sin out/ ni logs/ (ver tests/e2e/contrato.sh).
e2e:
	bash tests/e2e/contrato.sh

## Apaga la infraestructura. Conserva volumenes, .env, out/ y logs/.
down:
	$(COMPOSE) --profile app down --remove-orphans

## Borra artefactos generados: volumenes, out/, logs/ y estado/.env.
clean:
	$(COMPOSE) --profile app down -v --remove-orphans
	rm -rf out logs estado/.env estado/.env.tmp

## Terraform de brief §8 (infra/, no se despliega). Fuera de `make test`: el init
## baja el provider AWS (necesita red), pero no pide credenciales ni backend.
## Imagen pinneada por digest del indice multi-arquitectura (1.17.2).
TERRAFORM_IMAGEN = hashicorp/terraform:1.17.2@sha256:6e9c1e647c31a960dc9b59e13272f9111d90010c10b717f77ffb324767660475
TERRAFORM = docker run --rm --user "$$(id -u):$$(id -g)" -e TF_DATA_DIR=/tmp/tf -e TF_IN_AUTOMATION=1 \
  -v "$(CURDIR)/infra:/infra" -w /infra --entrypoint sh $(TERRAFORM_IMAGEN) -c

## Exige el lock versionado (init lo usa en readonly), fmt -check, init sin backend y validate.
infra-validar:
	$(TERRAFORM) 'test -s .terraform.lock.hcl && terraform fmt -check -diff && terraform init -backend=false -input=false -lockfile=readonly && terraform validate'

## Regenera infra/.terraform.lock.hcl con hashes de las plataformas usuales.
infra-lock:
	$(TERRAFORM) 'terraform providers lock -platform=linux_amd64 -platform=linux_arm64 -platform=darwin_amd64 -platform=darwin_arm64 -platform=windows_amd64'
