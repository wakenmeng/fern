variable "region" {
  type    = string
  default = "us-west-2"
}

# Tags every resource, so one run can be found and torn down as a unit (DESIGN §11, 72 hours).
variable "run_id" {
  type    = string
  default = "live-1"
}

variable "node_count" {
  type    = number
  default = 2
}

variable "db_multi_az" {
  description = "Synchronous standby in a second AZ. Doubles the (small) RDS cost."
  type        = bool
  default     = true
}
