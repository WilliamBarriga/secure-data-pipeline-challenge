# Bucket `auditoria`: espejo de la Bitacora fuera de Postgres.
#   corridas/   -> un objeto por Corrida que escribe ingesta.
#   cloudtrail/ -> reservado para el trail propuesto (no implementado).
#
# Recorte de alcance (2026-11-10, DECISIONS.md): Object Lock COMPLIANCE y CloudTrail con data
# events de `datos` se recortan a `no_implementado`. Se documentan como control
# propuesto en infra/README.md y NO se escriben en HCL.
#
# Lo que si hay: versionado, SSE-KMS, Block Public Access, BucketOwnerEnforced,
# politica que niega sin TLS / sin SSE-KMS y que niega borrar versiones (s3.tf),
# e ingesta sin DeleteObject (iam.tf). Sin lifecycle: no se expira nada.

resource "aws_s3_bucket" "auditoria" {
  bucket = "${var.prefijo}-auditoria-${local.cuenta}"
}
