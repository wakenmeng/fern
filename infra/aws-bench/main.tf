# Short-lived load-test capacity next to the live run (make bench-up / bench-down, docs/DEPLOY.md).
# Its own state, so creating and destroying it never touches the live cluster, database or Redis:
# everything live is only looked up by name.
#   - a Postgres the size a ~1M-MAU neobank would start on (Multi-AZ, like live), for fern_bench
#   - a tainted node group for the bench API copies and the load generator

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.95" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "run_id" {
  type    = string
  default = "live-1"
}

variable "db_class" {
  type    = string
  default = "db.m7g.2xlarge" # 8 vCPU, 32 GiB
}

variable "node_type" {
  type    = string
  default = "c7g.4xlarge" # 16 vCPU: room for the 7-CPU load generator next to API pods
}

variable "node_count" {
  type    = number
  default = 2
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "fern", run_id = var.run_id, purpose = "bench", managed_by = "terraform" }
  }
}

locals {
  live = "fern-${var.run_id}"
  name = "fern-${var.run_id}-bench"
}

data "aws_eks_cluster" "live" {
  name = local.live
}

data "aws_eks_node_groups" "live" {
  cluster_name = local.live
}

data "aws_eks_node_group" "live" {
  cluster_name    = local.live
  node_group_name = tolist(data.aws_eks_node_groups.live.names)[0]
}

# The live nodes' security group: the live Redis and this database admit it.
data "aws_security_group" "node" {
  filter {
    name   = "tag:Name"
    values = ["${local.live}-node"]
  }
}

# ---------------------------------------------------------------- Postgres

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_security_group" "db" {
  name   = "${local.name}-db"
  vpc_id = data.aws_eks_cluster.live.vpc_config[0].vpc_id

  ingress {
    description     = "Postgres from EKS nodes"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [data.aws_security_group.node.id]
  }
}

resource "aws_db_instance" "bench" {
  identifier              = local.name
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = var.db_class
  allocated_storage       = 100
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "fern_bench"
  username                = "fern"
  password                = random_password.db.result
  multi_az                = true
  db_subnet_group_name    = local.live
  vpc_security_group_ids  = [aws_security_group.db.id]
  publicly_accessible     = false
  skip_final_snapshot     = true
  deletion_protection     = false
  backup_retention_period = 0
  apply_immediately       = true
}

# ---------------------------------------------------------------- nodes

# Same security groups as the live nodes (so pods reach Redis, this database and the cluster).
resource "aws_launch_template" "bench" {
  name_prefix            = "${local.name}-"
  vpc_security_group_ids = [data.aws_security_group.node.id, data.aws_eks_cluster.live.vpc_config[0].cluster_security_group_id]
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
}

resource "aws_eks_node_group" "bench" {
  cluster_name    = local.live
  node_group_name = local.name
  node_role_arn   = data.aws_eks_node_group.live.node_role_arn
  subnet_ids      = data.aws_eks_node_group.live.subnet_ids
  ami_type        = "AL2023_ARM_64_STANDARD"
  instance_types  = [var.node_type]

  scaling_config {
    min_size     = var.node_count
    max_size     = var.node_count
    desired_size = var.node_count
  }
  launch_template {
    id      = aws_launch_template.bench.id
    version = aws_launch_template.bench.latest_version
  }
  labels = { "fern/bench" = "true" }
  taint {
    key    = "fern/bench"
    value  = "true"
    effect = "NO_SCHEDULE"
  }
}

output "database_url" {
  value     = "postgresql://fern:${random_password.db.result}@${aws_db_instance.bench.address}:5432/fern_bench?sslmode=require"
  sensitive = true
}
