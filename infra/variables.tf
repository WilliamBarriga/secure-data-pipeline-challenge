variable "region" {
  description = "Region de AWS de buckets y llaves (SSE-KMS exige la llave en la misma region que el bucket)."
  type        = string
  default     = "us-east-1"
}

variable "prefijo" {
  description = "Prefijo de nombres. Los buckets le suman la cuenta para ser globalmente unicos."
  type        = string
  default     = "cauce"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,20}$", var.prefijo))
    error_message = "prefijo: minusculas, digitos y guiones, de 2 a 21 caracteres."
  }
}

variable "tags" {
  description = "Tags por defecto de todos los recursos."
  type        = map(string)
  default = {
    proyecto   = "cauce-app"
    gestionado = "terraform"
  }
}
