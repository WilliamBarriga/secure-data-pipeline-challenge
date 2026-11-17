output "buckets" {
  description = "Nombres de los buckets datos y auditoria."
  value       = { for nombre, b in local.buckets : nombre => b.id }
}

output "llaves_kms" {
  description = "ARN de las llaves kms_s3, kms_pii y kms_bidx."
  value = {
    kms_s3   = aws_kms_key.kms_s3.arn
    kms_pii  = aws_kms_key.kms_pii.arn
    kms_bidx = aws_kms_key.kms_bidx.arn
  }
}

output "roles" {
  description = "ARN de los roles ingesta, analista y auditor."
  value = {
    ingesta  = aws_iam_role.ingesta.arn
    analista = aws_iam_role.analista.arn
    auditor  = aws_iam_role.auditor.arn
  }
}
