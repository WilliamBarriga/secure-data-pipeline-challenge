provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}

data "aws_caller_identity" "actual" {}

data "aws_partition" "actual" {}

locals {
  cuenta    = data.aws_caller_identity.actual.account_id
  particion = data.aws_partition.actual.partition
  raiz      = "arn:${local.particion}:iam::${local.cuenta}:root"
  via_s3    = "s3.${var.region}.amazonaws.com"
}
