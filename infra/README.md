# infra/: Terraform de brief §8 (no se despliega)

Describe cómo correría el pipeline en AWS con los equivalentes de C3 (cifrado), C4 (roles de menor privilegio) y C5 (Bitácora append-only). No se aplica: el criterio es que `terraform validate` pase.

```bash
make infra-validar   # fmt -check + init -backend=false (lock en readonly) + validate
make infra-lock      # regenera .terraform.lock.hcl (linux/darwin amd64+arm64, windows amd64)
```

Ambos corren en `hashicorp/terraform:1.17.2` pinneada por el digest del índice multi-arquitectura. No piden credenciales de AWS ni backend; el `init` sí necesita red para bajar el provider. Quedan fuera de `make test`.

## Layout

Un módulo raíz plano, sin módulos hijos (hay un solo despliegue, así que un módulo hijo sería un pass-through):

| Archivo | Qué tiene |
|---|---|
| `versions.tf` | Terraform `~> 1.17`, provider `hashicorp/aws ~> 6.72`; backend S3 comentado como opción de producción. |
| `providers.tf` | Provider con `default_tags`; cuenta, partición y ARN de root como locals. |
| `variables.tf` | `region`, `prefijo`, `tags`. |
| `kms.tf` | `kms_s3`, `kms_pii`, `kms_bidx` con sus key policies. |
| `s3.tf` | Bucket `datos` + endurecimiento común a los dos buckets. |
| `auditoria.tf` | Bucket `auditoria` (ver el recorte de alcance abajo). |
| `iam.tf` | Roles `ingesta`, `analista`, `auditor`. |
| `outputs.tf` | Nombres de buckets y ARN de llaves y roles. |
| `.terraform.lock.hcl` | Versionado, con hashes de 5 plataformas. |

## Equivalencias con lo local

| Local (Postgres + archivos) | AWS (este módulo) |
|---|---|
| `data/` (entrada hostil, ro) | `s3://<prefijo>-datos-<cuenta>/entrada/`; `ingesta` solo `GetObject`. |
| `out/` | `.../salidas/`; `ingesta` `GetObject`/`PutObject` (el stream relee `metrics.json`), `analista` solo `GetObject`. |
| `audit.bitacora_corrida` (append-only, `ingesta` solo EXECUTE de `registrar_evento`) | `s3://<prefijo>-auditoria-<cuenta>/corridas/`; `ingesta` solo `PutObject`; versionado y política que niega borrar versiones. |
| KEK del envelope en `estado/.env` | `kms_pii` (simétrica, con rotación); solo `ingesta`: `GenerateDataKey` + `Decrypt`. |
| Llave HMAC del blind index | `kms_bidx` (`HMAC_256`, `GENERATE_VERIFY_MAC`); solo `ingesta`: `GenerateMac`. |
| Cifrado de lo que queda en disco | `kms_s3`: SSE-KMS con bucket key en ambos buckets. |
| Rol `analista` (solo `analytics.v_consumo_mensual`) | Rol `analista`: solo `datos/salidas/*`, con MFA, sin `kms_pii` ni `kms_bidx`. |
| Rol `auditor` (solo la Bitácora) | Rol `auditor`: solo el bucket `auditoria` y `cloudtrail:LookupEvents`, con MFA; nada en `datos`. |

## Controles

- **Key policies de menor privilegio.** La cuenta (`:root`) recibe solo las acciones de administración del statement "Allow access for Key Administrators" de la policy por defecto de AWS, nunca `kms:*` ni acciones de uso. Por eso una política IAM no puede delegar uso: el uso se concede en la key policy, rol por rol. `kms_s3` solo se usa vía S3 (`kms:ViaService = s3.<region>.amazonaws.com`). Ventana de borrado de 30 días en las tres. Riesgo: `kms:Put*` deja a un administrador reescribir la policy y darse uso; en producción eso se cubre con una alarma sobre `PutKeyPolicy`.
- **Buckets.** Versionado, SSE-KMS con `kms_s3` y bucket key, Block Public Access completo y `BucketOwnerEnforced` (sin ACLs). La política de ambos niega todo lo que no venga por TLS (`aws:SecureTransport = false`), niega `PutObject` sin el header `x-amz-server-side-encryption-aws-kms-key-id` (el patrón de la guía de S3) y niega `PutObject` con otra llave que no sea `kms_s3`. Consecuencia: el cliente de `ingesta` tiene que mandar el ARN de `kms_s3` en cada `PutObject`.
- **Lifecycle.** `datos` expira versiones no actuales a los 90 días y aborta multiparts incompletos a los 7. `auditoria` no expira nada.
- **IAM.** `ingesta` la asume solo `ecs-tasks.amazonaws.com` con `aws:SourceAccount` = la cuenta (confused deputy). `analista` y `auditor` los asume la cuenta con `aws:MultiFactorAuthPresent = true`. `ListBucket` va limitado por `s3:prefix`. Ningún rol tiene `s3:DeleteObject` ni `s3:DeleteObjectVersion`.
- **Sin server access logging de S3.** El bucket destino de esos logs no puede usar SSE-KMS ni Object Lock (ver verificación 2). El reemplazo previsto era CloudTrail con data events, que el recorte de alcance deja fuera (abajo).

## Recortado por alcance: Object Lock y CloudTrail (`no_implementado`)

Decisión de alcance (ver la fila "Recorte de alcance" de [`DECISIONS.md`](../DECISIONS.md#decisiones-de-diseño-antes-de-construir)): Object Lock COMPLIANCE en `auditoria` y CloudTrail con data events de `datos` se documentan como control propuesto y **no** se escriben en HCL. Hoy `auditoria` no es WORM: un administrador con `s3:PutBucketPolicy` puede quitar la denegación de borrado. Lo que sí queda es el versionado, la denegación de borrar versiones, `ingesta` sin `DeleteObject` y, en lo local, la cadena de hash de la Bitácora.

Control propuesto, para cuando se implemente:

1. `auditoria` creado con `object_lock_enabled = true` y una `aws_s3_bucket_object_lock_configuration` en modo `COMPLIANCE` con retención por variable (`retencion_auditoria_dias`, 365 por defecto). En COMPLIANCE nadie, ni root, acorta la retención ni borra una versión protegida.
2. Un `aws_cloudtrail` multi-región con validación de integridad de logs, cifrado con `kms_s3` (statement extra para el servicio `cloudtrail.amazonaws.com` con `aws:SourceArn`), escribiendo en `auditoria/cloudtrail/`, con data events (`AWS::S3::Object`) del bucket `datos`.
3. Ajustar la política de `auditoria`: el statement que exige SSE-KMS tiene que aceptar también `AES256`, porque si se exige solo SSE-KMS junto con la validación de integridad, no se puede crear el trail (nota oficial en las buenas prácticas de CloudTrail, verificación 3). Además, sumar el statement que permite `s3:PutObject` a `cloudtrail.amazonaws.com` con `aws:SourceArn` del trail.
4. Antes de aplicar, probar en una cuenta sandbox que CloudTrail escribe en un bucket con retención por defecto (ver verificación 3: la documentación oficial no lo afirma).

## Verificado contra documentación oficial

1. **Versiones de Terraform y del provider AWS.** Terraform 1.17.2 es la versión actual según el checkpoint de HashiCorp; provider `hashicorp/aws` 6.72.0 (publicado el 2026-11-04) según el registry. Se pinnea `~> 1.17` y `~> 6.72`, y el lock fija 6.72.0 con hashes.
   - https://checkpoint-api.hashicorp.com/v1/check/terraform
   - https://releases.hashicorp.com/terraform/1.17.2/
   - https://registry.terraform.io/providers/hashicorp/aws/6.72.0
   - https://hub.docker.com/r/hashicorp/terraform (digest del índice `sha256:6e9c1e64...0475`, obtenido con `docker buildx imagetools inspect hashicorp/terraform:1.17.2`)
   - Backend S3, `use_lockfile` y `dynamodb_table` deprecado: https://developer.hashicorp.com/terraform/language/backend/s3

2. **El server access logging de S3 no admite SSE-KMS ni Object Lock con retención por defecto en el bucket destino.** Confirmado. La guía dice que el destino "must use Amazon S3 managed keys (SSE-S3)" y que si usa SSE-KMS S3 podría dejar logs cifrados con una llave inaccesible; también que "S3 buckets that have S3 Object Lock enabled can't be used as destination buckets for server access logs" y que el destino "must not have a default retention period configuration".
   - https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-server-access-logging.html
   - https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html
   - https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html

3. **Que CloudTrail puede escribir en un bucket con Object Lock.** **No confirmado por documentación oficial.** Ni la guía de CloudTrail ni las páginas de Object Lock lo afirman ni lo niegan. La única afirmación encontrada está en un artículo comunitario (community.aws), que no cuenta como fuente oficial. Lo oficial que sí aplica: Object Lock exige `Content-MD5` o un checksum en los uploads con retención explícita, y las buenas prácticas de CloudTrail advierten que exigir solo SSE-KMS en la política del bucket rompe la creación del trail con validación de integridad (hay que permitir también `AES256`). Esto refuerza el recorte de alcance: no se escribe en HCL algo que no se puede verificar.
   - https://docs.aws.amazon.com/awscloudtrail/latest/userguide/best-practices-security.html
   - https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-s3-bucket-policy-for-cloudtrail.html
   - https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html
   - https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html

4. **Las llaves HMAC no rotan automáticamente.** Confirmado. "Automatic key rotation is supported only on symmetric encryption KMS keys with key material that AWS KMS generates", y la rotación bajo demanda tampoco aplica: las HMAC están en la lista de "Neither automatic nor on-demand key rotation is supported", rotables solo a mano (llave nueva). Por eso `kms_bidx` tiene `enable_key_rotation = false`. Riesgo aceptado: rotarla implica recalcular todo el blind index.
   - https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys.html
   - https://docs.aws.amazon.com/kms/latest/developerguide/rotate-keys-manually.html

Otras fuentes usadas para el HCL:

- Acciones de administración y uso en key policies: https://docs.aws.amazon.com/kms/latest/developerguide/key-policy-default.html
- `kms:ViaService`: https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html
- Argumentos de `aws_kms_key` (`HMAC_256`, `GENERATE_VERIFY_MAC`, `deletion_window_in_days` de 7 a 30): https://registry.terraform.io/providers/hashicorp/aws/6.72.0/docs/resources/kms_key
- Política que exige SSE-KMS (condición `Null` sobre `s3:x-amz-server-side-encryption-aws-kms-key-id`): https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html#require-sse-kms
- `cloudtrail:LookupEvents` sin permisos a nivel de recurso (`Resource: "*"`) y los 90 días de Event history sin trail: https://docs.aws.amazon.com/service-authorization/latest/reference/list_awscloudtrail.html y https://docs.aws.amazon.com/awscloudtrail/latest/userguide/best-practices-security.html
- MFA en la confianza del rol: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_mfa_configure-api-require.html
- `aws:SourceAccount` contra el confused deputy: https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html

## Fuera de alcance

RDS (Postgres), MSK (Kafka) y ECS (la tarea que asume `ingesta`) no se declaran. Si se suman, `ingesta` necesitaría además `rds-db:connect`, y la Bitácora seguiría siendo la de Postgres, con S3 como espejo.
