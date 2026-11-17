# PII cifrada desde raw y minimizada en un modelo estrella por zonas

El modelo vive en Postgres con un esquema por zona (`raw`, `quarantine`, `crypto`, `core`, `analytics`, `audit`) y un modelo estrella en `core` con llaves naturales y dimensiones SCD1. Las columnas Restringido-PII se cifran en la aplicación **antes** de llegar a cualquier tabla, incluida `raw` y `quarantine`: quien lea un dump como superusuario no debe encontrar PII en claro en ninguna zona, no solo en `core`. Además minimizamos: los datos de cliente embebidos en cada factura (NIT, razón social, correo) no se guardan en `core` porque el cliente se resuelve por `frontera_id → dim_frontera.cliente_id`, y de `observaciones` solo se conserva la versión enmascarada; el original se descarta. El enmascarado ocurre en la aplicación antes de la primera escritura: el original de `observaciones` no existe en ninguna zona, ni cifrado (`raw.cliente`, `quarantine.rechazo.payload_cifrado` y `core.dim_cliente` reciben solo el texto enmascarado).

## Considered Options

- **Raw en claro y cifrar al pasar a core**: más simple de depurar, pero deja la PII legible en la zona más grande y viola C3 al leer como superusuario.
- **Guardar el original de `observaciones` cifrado**: permite re-enmascarar si mejora el detector, a cambio de custodiar PII que nadie consulta. Se descartó por Ley 1581 (finalidad y minimización); re-enmascarar se hace re-procesando el archivo fuente.

## Consequences

- La idempotencia no puede comparar ciphertext (cambia en cada corrida): los upserts van por llave natural o blind index.
- Depurar filas rechazadas de clientes o facturas exige descifrar con la KEK; las de consumo quedan legibles porque no traen PII.
- `observaciones` no se cifra en ninguna zona: ya enmascarada no es Restringido-PII. Si el detector falla, la PII residual queda en claro; por eso el detector es conservador (enmascara de más antes que de menos).
- Los nombres de los componentes de tarifa (G, T, D, C, P, R) son una lectura del estándar CREG, no vienen en los datos.
