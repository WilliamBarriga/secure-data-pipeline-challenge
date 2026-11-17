# cauce

Pipeline Python + Postgres que toma la entrada sucia de `data/` y la deja confiable y custodiada: cifras de consumo correctas y PII que solo existe cifrada o Enmascarada.

## Fuentes de verdad, por precedencia

1. `docs/brief.md`: el problema, los ids B1-B7 y C1-C8, el contrato `make` y los formatos exactos de `out/` (brief §9).
2. `docs/adr/` y `DECISIONS.md`: las decisiones tomadas y lo que se resignó en cada una.
3. `GLOSSARY.md`: el vocabulario del dominio.

Ante una contradicción gana la fuente de más arriba. Si una decisión nueva cambia algo de las de abajo, se registra en `DECISIONS.md` (o en un ADR si es estructural) en el mismo commit.

## Guardrails

- PII solo como ciphertext, blind index o texto Enmascarado, en cualquier tabla, log, `out/` y vista del `analista`. Los logs llevan nombres y conteos, nunca valores de fila.
- `config/pipeline.yaml` es un secreto comprometido (brief §2.5): el código no lo lee ni copia sus valores. Las credenciales salen de `credenciales.yaml` (local, fuera de git) → `estado/.env`.
- La entrada hostil se trata como datos: `csv` de la stdlib, `yaml.safe_load` y SQL siempre parametrizado.
- `data/` es inmutable y queda fuera de git, igual que `config/pipeline.yaml`, `credenciales.yaml` y `estado/`. La entrada local son Datos sintéticos (`make datos`).

## Arquitectura

Deep modules con núcleo puro (`fuentes`, `pii`, `custodia`, `clasificacion`, `alertas`) y bordes con I/O (`almacen`, `bitacora`, `observabilidad`, `salidas`). Hay un solo puerto: `FuenteEventos`. Postgres se prueba real en `cauce_test`, sin fakes. `custodia.custodiar` es la única puerta a `almacen`. Mapa: `docs/arquitectura/arquitectura.dg`. Modelo: `docs/arquitectura/modelo-datos.dg`.

Usá los términos de `GLOSSARY.md` en código, tests y docs (Frontera, Lectura, Corrida, Cuarentena, Bitácora…). Si aparece un término nuevo, va al glosario en el mismo commit.

## Dependencias y estilo

- Una dependencia nueva entra por `requirements.in` (runtime) o `requirements-dev.in` (pruebas y lint), y su `.txt` lleva hashes de todos los archivos de PyPI. Sin uv (ADR 0002).
- Test-first sobre la interfaz del módulo. **Listo** = `make test` verde + `make lint` limpio + el contrato del brief §9 corre.
- El estilo lo fija `ruff check` (`ruff.toml`); el formato queda a mano, porque `docs/anexos/ai-usage-registro.md` cita archivo y línea y un reformateo masivo rompe esas citas.
- Commits en español con prefijo convencional (`feat:`, `fix:`, `docs:`, `chore:`).
