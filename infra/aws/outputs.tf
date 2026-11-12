output "cluster_name" {
  value = module.eks.cluster_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "database_url" {
  value     = "postgresql://fern:${random_password.db.result}@${aws_db_instance.fern.address}:5432/fern?sslmode=require"
  sensitive = true
}

output "redis_url" {
  value = "redis://${aws_elasticache_cluster.fern.cache_nodes[0].address}:6379/0"
}
