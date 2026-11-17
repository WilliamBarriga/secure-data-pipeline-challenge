# Tres llaves, equivalentes a las del cifrado local:
#   kms_s3   -> SSE-KMS de los buckets (en local: cifrado del volumen / out/).
#   kms_pii  -> KEK del envelope de PII (en local: la KEK de estado/.env).
#   kms_bidx -> HMAC_256 del blind index (en local: la llave HMAC de estado/.env).
#
# Key policies de menor privilegio: la cuenta (root) solo administra, nunca usa;
# sin `kms:*`, asi una politica IAM no puede delegar uso. El uso se concede aca,
# rol por rol. Las acciones de administracion son las del statement
# "Allow access for Key Administrators" de la policy por defecto de AWS.

locals {
  kms_admin = [
    "kms:Create*",
    "kms:Describe*",
    "kms:Enable*",
    "kms:List*",
    "kms:Put*",
    "kms:Update*",
    "kms:Revoke*",
    "kms:Disable*",
    "kms:Get*",
    "kms:Delete*",
    "kms:TagResource",
    "kms:UntagResource",
    "kms:ScheduleKeyDeletion",
    "kms:CancelKeyDeletion",
    "kms:RotateKeyOnDemand",
  ]
}

# Statement comun de las tres key policies (va primero en cada una): la cuenta solo
# administra. Uno solo, para que las tres llaves no diverjan.
data "aws_iam_policy_document" "kms_administracion" {
  statement {
    sid       = "AdministracionCuenta"
    actions   = local.kms_admin
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [local.raiz]
    }
  }
}

# --- kms_s3 ---------------------------------------------------------------

data "aws_iam_policy_document" "kms_s3" {
  source_policy_documents = [data.aws_iam_policy_document.kms_administracion.json]

  # ingesta escribe (GenerateDataKey) y lee (Decrypt) objetos, solo a traves de S3.
  statement {
    sid       = "IngestaViaS3"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.ingesta.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = [local.via_s3]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.cuenta]
    }
  }

  # analista y auditor solo leen, solo a traves de S3.
  statement {
    sid       = "LecturaViaS3"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.analista.arn, aws_iam_role.auditor.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = [local.via_s3]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:CallerAccount"
      values   = [local.cuenta]
    }
  }
}

resource "aws_kms_key" "kms_s3" {
  description             = "${var.prefijo}: SSE-KMS de los buckets datos y auditoria"
  key_usage               = "ENCRYPT_DECRYPT"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms_s3.json
}

resource "aws_kms_alias" "kms_s3" {
  name          = "alias/${var.prefijo}-s3"
  target_key_id = aws_kms_key.kms_s3.key_id
}

# --- kms_pii (KEK del envelope) -------------------------------------------

data "aws_iam_policy_document" "kms_pii" {
  source_policy_documents = [data.aws_iam_policy_document.kms_administracion.json]

  # Solo ingesta: pide una DEK por valor (GenerateDataKey) y la desenvuelve (Decrypt).
  statement {
    sid       = "IngestaEnvelope"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.ingesta.arn]
    }
  }
}

resource "aws_kms_key" "kms_pii" {
  description             = "${var.prefijo}: KEK del envelope de PII (solo ingesta)"
  key_usage               = "ENCRYPT_DECRYPT"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms_pii.json
}

resource "aws_kms_alias" "kms_pii" {
  name          = "alias/${var.prefijo}-pii"
  target_key_id = aws_kms_key.kms_pii.key_id
}

# --- kms_bidx (HMAC del blind index) --------------------------------------

data "aws_iam_policy_document" "kms_bidx" {
  source_policy_documents = [data.aws_iam_policy_document.kms_administracion.json]

  # Solo ingesta: el blind index se recalcula y se compara, no hace falta VerifyMac.
  statement {
    sid       = "IngestaBlindIndex"
    actions   = ["kms:GenerateMac"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.ingesta.arn]
    }
  }
}

# Las llaves HMAC no admiten rotacion automatica ni bajo demanda (ver README):
# rotar = llave nueva + recalcular el blind index. Riesgo aceptado y documentado.
resource "aws_kms_key" "kms_bidx" {
  description              = "${var.prefijo}: HMAC del blind index (solo ingesta)"
  key_usage                = "GENERATE_VERIFY_MAC"
  customer_master_key_spec = "HMAC_256"
  enable_key_rotation      = false
  deletion_window_in_days  = 30
  policy                   = data.aws_iam_policy_document.kms_bidx.json
}

resource "aws_kms_alias" "kms_bidx" {
  name          = "alias/${var.prefijo}-bidx"
  target_key_id = aws_kms_key.kms_bidx.key_id
}
