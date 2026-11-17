terraform {
  required_version = "~> 1.17"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
  }

  # Sin backend: `make infra-validar` corre `terraform init -backend=false`.
  # Opcion de produccion: estado en S3 cifrado con el bloqueo nativo de S3
  # (use_lockfile; el bloqueo con DynamoDB esta deprecado), en una cuenta
  # distinta de la que corre el pipeline. Ver infra/README.md.
  #
  # backend "s3" {
  #   bucket       = "<prefijo>-tfstate-<cuenta>"
  #   key          = "cauce/infra.tfstate"
  #   region       = "us-east-1"
  #   encrypt      = true
  #   kms_key_id   = "<arn de una llave dedicada al estado>"
  #   use_lockfile = true
  # }
}
