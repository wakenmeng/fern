# Fern on AWS for the 72-hour live window: EKS (Graviton) + RDS Postgres 16 + ElastiCache Redis + ECR.
# No NAT gateway and no load balancer: nodes sit in public subnets (no inbound rules), and traffic
# arrives through a Cloudflare Tunnel that cloudflared opens from inside the cluster (infra/k8s).

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "fern-${var.run_id}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.21"

  name                    = local.name
  cidr                    = "10.42.0.0/16"
  azs                     = local.azs
  public_subnets          = ["10.42.0.0/20", "10.42.16.0/20"]
  database_subnets        = ["10.42.128.0/24", "10.42.129.0/24"]
  map_public_ip_on_launch = true
  enable_nat_gateway      = false
  enable_dns_hostnames    = true

  create_database_subnet_group = true
  # No private subnets, so the database subnets need their own (local-only) route table.
  create_database_subnet_route_table = true
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.37"

  cluster_name                             = local.name
  cluster_version                          = "1.33"
  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  cluster_addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni    = {}
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = ["t4g.medium"]
      min_size       = var.node_count
      max_size       = var.node_count
      desired_size   = var.node_count
    }
  }
}

# ---------------------------------------------------------------- Postgres

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_security_group" "db" {
  name   = "${local.name}-db"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "Postgres from EKS nodes"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
  ingress {
    description     = "Redis from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
}

resource "aws_db_instance" "fern" {
  identifier             = local.name
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t4g.micro"
  allocated_storage      = 20
  storage_type           = "gp3"
  storage_encrypted      = true
  db_name                = "fern"
  username               = "fern"
  password               = random_password.db.result
  multi_az               = var.db_multi_az
  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  # A 72-hour demo: the final archive is a JSON snapshot (DESIGN §11), not a DB snapshot.
  skip_final_snapshot     = true
  deletion_protection     = false
  backup_retention_period = 1
  apply_immediately       = true
}

# ---------------------------------------------------------------- Redis (rate limits, AI budget)

resource "aws_elasticache_subnet_group" "fern" {
  name       = local.name
  subnet_ids = module.vpc.database_subnets
}

resource "aws_elasticache_cluster" "fern" {
  cluster_id         = local.name
  engine             = "redis"
  engine_version     = "7.1"
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1
  subnet_group_name  = aws_elasticache_subnet_group.fern.name
  security_group_ids = [aws_security_group.db.id]
}

# ---------------------------------------------------------------- Image registry

resource "aws_ecr_repository" "api" {
  name         = "fern-api"
  force_delete = true
  image_scanning_configuration {
    scan_on_push = true
  }
}
