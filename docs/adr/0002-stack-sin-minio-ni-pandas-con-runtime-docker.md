# Stack del pipeline: sin MinIO ni pandas, app en Docker por defecto

El pipeline es un paquete Python plano (`python -m cauce run|stream`) sin orquestador. Corre por defecto dentro de un contenedor `app` (`python:3.13-slim` pinneado por digest, `--user uid:gid`) y como alternativa en un `.venv` del host (`RUNTIME=local`, Python ≥3.10). La lógica se reparte así:
- Python con `csv` de la stdlib: parseo, normalización, B1, B2, B4, B7, C2 y C3.
- SQL en Postgres: B3, B5, agregados y vistas de salida.

Las dependencias de runtime son solo psycopg2-binary, kafka-python, cryptography, PyYAML y pydantic; pytest y ruff van aparte en `requirements-dev.txt` (la imagen instala ambos porque `make test` corre en el contenedor). Van pinneadas con los hashes de PyPI de todos los archivos de cada versión (se planeó `pip-compile --generate-hashes`; quedó colgado con `cryptography` y se reemplazó por la API JSON de PyPI, ver `AI_USAGE.md` §3) y se instalan con `pip install --require-hashes`. No se usa uv. MinIO sale del compose.

## Considered Options

- **Mantener MinIO como landing raw**: `minio/minio:latest` ya no se puede bajar (distribución solo como fuente desde 2025-10, repo en mantenimiento). Ningún target del contrato (brief §9) lo usa y trae credenciales root `minioadmin`. La inmutabilidad del raw la dan `raw.archivo` (sha256) y, en la nube, S3 versionado con KMS en Terraform.
- **pandas o DuckDB para normalizar**: pandas infiere tipos y convierte `"null"`/`NA` a NaN sin avisar, y pierde el número de línea en filas multilínea. DuckDB hace sniff automático sobre entrada hostil. Ninguno hace falta para ~115K filas, y cada uno suma superficie para C7.
- **SQL puro en Postgres**: obliga a que el cifrado o el enmascarado pasen por el servidor, y eso se descartó en ADR 0001.
- **`.venv` del host como default**: es lo más parecido a la máquina del operador, pero `python3 -m venv` falla en Debian/Ubuntu sin `python3-venv`, y no conocemos la versión de Python de esa máquina. Docker elimina esa variable.

## Consequences

- `make setup` construye la imagen y además crea el `.venv` del host en modo best-effort. Sirve para `RUNTIME=local` y para correr herramientas del host sin la imagen. El Makefile antepone `.venv/bin` al `PATH`.
- La config apunta a `postgres:5432`/`redpanda:29092` dentro del contenedor y a `localhost` en el host, y entra solo por env.
- Los modelos pydantic usan `strict`, `frozen`, `extra="forbid"` y `hide_input_in_errors=True`. Sin esto último, un `ValidationError` filtraría PII al log. pydantic-settings queda fuera porque exige Python ≥3.11.
