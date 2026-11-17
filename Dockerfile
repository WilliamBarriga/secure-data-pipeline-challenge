# python:3.13-slim fijado por digest (ADR 0002).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
# dev (pytest, ruff) tambien: `make test` y `make lint` corren en este contenedor.
RUN pip install --require-hashes -r requirements.txt -r requirements-dev.txt

COPY src ./src
COPY sql ./sql
COPY tools ./tools
COPY tests ./tests
COPY pytest.ini ruff.toml pyrightconfig.json Makefile Dockerfile docker-compose.yml data_classification.yaml* ./
COPY postgres ./postgres
# Se ejecuta con --user uid:gid del host; el rootfs es de solo lectura.
CMD ["python", "-m", "cauce", "--help"]
