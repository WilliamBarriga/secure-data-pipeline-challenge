# Uso de IA en este proyecto

Resumen en cuatro preguntas. El registro completo, slice por slice y con cada error citado por archivo y línea, está en [`docs/anexos/ai-usage-registro.md`](docs/anexos/ai-usage-registro.md).

## 1. Herramientas y dónde las usé

| Herramienta | Para qué | Cuánto del resultado |
|---|---|---|
| Claude Code (agente principal) | Diseño con tickets, implementación test-first, merges y verificación del contrato `make` | ~95% del código y de los documentos, sumando los subagentes. Yo dirigí, decidí y revisé (sección 5) |
| Subagentes Claude, con la skill `implement-spec` | Un Sonnet coordinador repartía cada ticket a Sonnets que implementaban en worktrees aislados y a Haikus que revisaban en solo lectura. Lo mismo en las revisiones de arquitectura de la base y final: un candidato por agente, integrado y verificado por el agente principal | Incluido en el ~95% de arriba. Fue una parte grande del trabajo |
| Skills de Claude Code (`tdd`, `code-review`, `improve-codebase-architecture`, `wayfinder`) | Ciclo rojo-verde, code review Standards + Spec contra el ticket, búsqueda de deep modules | incluido arriba |

Sin IA: el tipado para pyright/pylance (`bitacora.Identidad` como `TypedDict`, `almacen.escalar` que falla con `LookupError` en vez de devolver `None`, los casts de los dobles de test). Lo escribí a mano antes de sumar ruff; el commit lo hizo Claude, por eso lleva su `Co-Authored-By`. El resto de los commits tiene código generado por la IA; lo mío está en las decisiones de la sección 5.

## 2. Prompts que movieron la aguja

```
/wayfinder tenemos este proyecto, creemos el .git inicial y lo subimos a un repo
privado en gh para manejar el mapa. ya hice una revision inicial de los datos aunque tenes
que revisarlos bien al explorar ten en cuenta que tienen distintos separadores. quiero 
que crees tambien una carpeta temporal docs/diagrams/ en la que vamos a tener el repo mio
publico de diagramas y ahi me vas explicando y vamos modelando la solucion a alto nivel.
Hay que tener en cuenta los requerimientos de seguridad
```

```
/improve-codebase-architecture antes de iniciar con la construccion del proyecto
con el mapa y los specs definidos, quiero que revises a fondo lo que se definio a ver si
hay alguna flaqueza antes de iniciar a construir y que nos pudimos haber saltado de las
revisiones mencionadas en el readme
```

**Qué obtuve:** con el primero salieron el mapa de tickets y los diagramas `docs/arquitectura/*.dg`. Con el segundo, una revisión pre-build que encontró 11 defectos (D1 a D11) y 6 candidatos de profundización, corregidos en los tickets antes de escribir código.

## 3. Qué generó la IA que cambié, y por qué

| Archivo | Qué propuso la IA | Qué quedó | Por qué |
|---|---|---|---|
| `requirements.txt` | `pip-compile --generate-hashes` | Script que toma los sha256 de la API JSON de PyPI, verificado con `--require-hashes` | `pip-compile` quedó colgado más de 15 min con `cryptography` |
| `Makefile` (`lint`) | Correr ruff con `uvx` y sacar pytest de la imagen | ruff y pytest con hash en `requirements-dev.txt`, instalados en la imagen | ADR 0002 prohíbe uv, y `make test` corre dentro de la imagen |
| `docker-compose.yml` (`credenciales`) | `-v` rw sobre un servicio que ya montaba `credenciales.yaml` ro | Servicio propio, el único con `credenciales.yaml` rw | El `-v` no pisa el mount ro (`Errno 30`); hay un test estático que lo vigila |

Hay más casos en el anexo, en "Propuestas de la IA que cambié" y en cada ticket.

## 4. Un error que cometió la IA y que tuve que corregir

**El Detector de Alertas dejaba que un evento hostil suprimiera uno real.** El Detector marcaba el `evento_id` como visto **antes** de validar el evento. Bastaba con que un evento inválido llegara primero con el id de uno real para que el real se descartara como duplicado: se perdía su Alerta y aparecía una Caída falsa. La IA había dado ese orden por bueno en el slice del Detector como "decisión de borde".

- **Cómo se detectó:** en el code review final, contrastando el código contra la historia de usuario de deduplicación del Detector, no con un test.
- **Corrección:** primero un test del ataque en rojo (`tests/unit/test_alertas.py:175`). Ahora solo un evento válido reserva su id (`src/cauce/nucleo/alertas.py:199`), y los inválidos se recuerdan aparte para que la alimentación doble no infle los descartes.
- **Residual**, en T1 de `THREAT_MODEL.md`: un evento bien formado que se adelanta con el id de uno real.

Otros dos con la misma lección (los tests verdes no bastaban):

- Un `corrida_id` (uuid4) que empieza con `cc` + dígitos se leía como cédula y bloqueaba `metrics.json` de forma intermitente. Salió en el contrato completo, no en los unit tests. Corrección en `src/cauce/nucleo/pii.py:36` y test en `tests/unit/test_pii_estricto.py:45`.
- `terraform validate` daba verde con `infra/` vacío: el test no podía fallar. Se corrigió antes de escribir el HCL (`Makefile:147`).

## 5. Qué NO le delegué a la IA, a propósito

- **Arquitectura y organización del código.** Deep modules con núcleo puro (`fuentes`, `pii`, `custodia`, `clasificacion`, `alertas`) y bordes con I/O; un solo puerto (`FuenteEventos`); la separación en `nucleo/`, `bordes/`, `aplicacion/` y `arranque/` y el orden de los archivos; Postgres real en los tests, sin fakes. La IA proponía; la forma final del sistema la decidí yo.
- **El criterio de tipado**, y también el código (ver arriba).
- **La primera lectura de `data/`**, antes de diseñar: ahí vi que los archivos traían separadores distintos (prompt 1). Después la IA la profundizó.
- **Alcance y recortes.** Qué entraba y qué se declaraba: mantener la cadena de hash de la Bitácora y `RUNTIME=local`, y pasar Object Lock y CloudTrail a `no_implementado` ([recorte de alcance](DECISIONS.md#decisiones-de-diseño-antes-de-construir)).
- **Qué propuesta de la IA se aplicaba.** De las revisiones elegí qué candidatos entraban (1, 2 y 4 de los 5 de la base). Al cierre decidí cambiar `metrics.json` a conteos de la base (Corrección registrada en `DECISIONS.md`).
- **Seguridad: decisiones conjuntas.** La IA me explicaba las opciones y sus riesgos, y yo decidía: no leer `config/pipeline.yaml` ni comparar contra él, que la DEK la cree `bootstrap` como superusuario y no `ingesta`, y que el lock de Corrida falle rápido en vez de esperar 600 s. Están en `DECISIONS.md` y en el anexo.
