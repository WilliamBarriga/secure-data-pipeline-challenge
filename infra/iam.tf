# Roles equivalentes a C4 (ingesta / analista / auditor de Postgres).
# El uso de llaves KMS se concede en las key policies (kms.tf), no aca.
# Ningun rol tiene s3:DeleteObject ni s3:DeleteObjectVersion.

# --- ingesta: el pipeline, corriendo como tarea ECS -----------------------

data "aws_iam_policy_document" "confianza_ingesta" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    # Confused deputy: solo tareas de esta cuenta.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.cuenta]
    }
  }
}

resource "aws_iam_role" "ingesta" {
  name               = "${var.prefijo}-ingesta"
  description        = "Pipeline: lee entrada/, escribe salidas/ y auditoria/corridas/; usa las 3 llaves"
  assume_role_policy = data.aws_iam_policy_document.confianza_ingesta.json
}

data "aws_iam_policy_document" "ingesta" {
  statement {
    sid       = "LeerEntrada"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.datos.arn}/entrada/*"]
  }

  # GetObject en salidas/: `make stream` lee metrics.json para reemplazar solo la
  # subclave `stream`. Sobrescribir crea una version nueva; la anterior queda.
  statement {
    sid       = "EscribirSalidas"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.datos.arn}/salidas/*"]
  }

  statement {
    sid       = "ListarDatos"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.datos.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["entrada/*", "salidas/*"]
    }
  }

  # Solo PutObject: igual que tener solo EXECUTE de registrar_evento.
  statement {
    sid       = "RegistrarCorrida"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.auditoria.arn}/corridas/*"]
  }
}

resource "aws_iam_role_policy" "ingesta" {
  name   = "s3"
  role   = aws_iam_role.ingesta.id
  policy = data.aws_iam_policy_document.ingesta.json
}

# --- analista y auditor: personas de la cuenta, con MFA -------------------

data "aws_iam_policy_document" "confianza_humana" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [local.raiz]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role" "analista" {
  name               = "${var.prefijo}-analista"
  description        = "Solo lectura de datos/salidas/ (el equivalente de analytics.v_consumo_mensual)"
  assume_role_policy = data.aws_iam_policy_document.confianza_humana.json
}

data "aws_iam_policy_document" "analista" {
  statement {
    sid       = "LeerSalidas"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.datos.arn}/salidas/*"]
  }

  statement {
    sid       = "ListarSalidas"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.datos.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["salidas/*"]
    }
  }
}

resource "aws_iam_role_policy" "analista" {
  name   = "s3"
  role   = aws_iam_role.analista.id
  policy = data.aws_iam_policy_document.analista.json
}

resource "aws_iam_role" "auditor" {
  name               = "${var.prefijo}-auditor"
  description        = "Solo lectura del bucket auditoria y del historial de CloudTrail; nada en datos"
  assume_role_policy = data.aws_iam_policy_document.confianza_humana.json
}

data "aws_iam_policy_document" "auditor" {
  statement {
    sid       = "LeerAuditoria"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.auditoria.arn}/*"]
  }

  statement {
    sid       = "ListarAuditoria"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [aws_s3_bucket.auditoria.arn]
  }

  # LookupEvents no admite permisos a nivel de recurso: Resource "*" obligatorio.
  # Lee el Event history de 90 dias que CloudTrail lleva sin trail.
  statement {
    sid       = "HistorialCloudTrail"
    actions   = ["cloudtrail:LookupEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "auditor" {
  name   = "s3-cloudtrail"
  role   = aws_iam_role.auditor.id
  policy = data.aws_iam_policy_document.auditor.json
}
