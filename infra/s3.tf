# Bucket `datos`: espejo de data/ (prefijo entrada/, archivos hostiles) y de
# out/ (prefijo salidas/). Los prefijos no son objetos: existen porque IAM los
# usa como frontera (iam.tf). El bucket `auditoria` esta en auditoria.tf.
#
# Endurecimiento comun a ambos buckets (for_each sobre local.buckets):
# versionado, SSE-KMS con kms_s3 + bucket key, Block Public Access,
# BucketOwnerEnforced (sin ACLs) y politica que niega sin TLS o sin SSE-KMS.
# Sin server access logging: ver README (incompatible con SSE-KMS).

resource "aws_s3_bucket" "datos" {
  bucket = "${var.prefijo}-datos-${local.cuenta}"
}

resource "aws_s3_bucket_lifecycle_configuration" "datos" {
  bucket = aws_s3_bucket.datos.id

  rule {
    id     = "expirar-versiones-no-actuales"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.bucket]
}

locals {
  buckets = {
    datos     = aws_s3_bucket.datos
    auditoria = aws_s3_bucket.auditoria
  }
}

resource "aws_s3_bucket_versioning" "bucket" {
  for_each = local.buckets
  bucket   = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bucket" {
  for_each = local.buckets
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.kms_s3.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "bucket" {
  for_each = local.buckets
  bucket   = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "bucket" {
  for_each = local.buckets
  bucket   = each.value.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

data "aws_iam_policy_document" "bucket" {
  for_each = local.buckets

  statement {
    sid     = "NegarSinTLS"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      each.value.arn,
      "${each.value.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # Patron oficial: negar PutObject sin el header de llave KMS...
  statement {
    sid       = "NegarSinSSEKMS"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${each.value.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = ["true"]
    }
  }

  # ...y con una llave que no sea kms_s3.
  statement {
    sid       = "NegarOtraLlave"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${each.value.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.kms_s3.arn]
    }
  }

  # Solo `auditoria`: control compensatorio mientras Object Lock no esta (recorte de alcance).
  # Nadie borra versiones; un admin con s3:PutBucketPolicy puede quitarlo,
  # por eso no es WORM (ver README).
  dynamic "statement" {
    for_each = each.key == "auditoria" ? [1] : []
    content {
      sid       = "NegarBorrado"
      effect    = "Deny"
      actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      resources = ["${each.value.arn}/*"]
      principals {
        type        = "*"
        identifiers = ["*"]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "bucket" {
  for_each = local.buckets
  bucket   = each.value.id
  policy   = data.aws_iam_policy_document.bucket[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.bucket]
}
