# =============================================================================
# Fraud Detection MLOps — Persistent Infrastructure
# =============================================================================
# Apply ONCE. Never destroy unless the project ends.
# Resources: VPC, RDS, S3 (x5), ECR (x3), IAM, SSM seeds
#
# Usage:
#   cd terraform/persistent
#   terraform init
#   terraform apply
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.0"
    }
  }
  backend "local" {
    path = ".terraform-state/persistent.tfstate"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "fraudshield-mlops"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

# =============================================================================
# VARIABLES
# =============================================================================

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

# =============================================================================
# DATA SOURCES
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region"          "current" {}

# Fetch your current public IP at apply time — no static value in tfvars
data "http" "my_ip" {
  url = "https://checkip.amazonaws.com"
}

# =============================================================================
# LOCALS
# =============================================================================

locals {
  # Auto-fetched public IP — no static value in tfvars
  my_cidr = "${chomp(data.http.my_ip.response_body)}/32"

  # Convenience aliases used in IAM policy ARNs
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # Bucket name suffix — project start date instead of AWS account ID.
  # Avoids leaking account number in bucket names while keeping names stable.
  name_prefix = "mlops"
  suffix      = "YOUR_SUFFIX"

  # All 5 project S3 bucket names — referenced as local.buckets.<key>
  buckets = {
    code_sync     = "${local.name_prefix}-code-sync-${local.suffix}"
    mlflow        = "${local.name_prefix}-mlflow-artifacts-${local.suffix}"
    feature_store = "${local.name_prefix}-feature-store-${local.suffix}"
    ge_results    = "${local.name_prefix}-ge-results-${local.suffix}"
    raw_data      = "${local.name_prefix}-raw-data-${local.suffix}"
  }
}

# =============================================================================
# NETWORKING
# =============================================================================

resource "aws_vpc" "mlops" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = "mlops-vpc" }
}

resource "aws_internet_gateway" "mlops" {
  vpc_id = aws_vpc.mlops.id
  tags   = { Name = "mlops-igw" }
}

# Public subnets — spot instance lives here
resource "aws_subnet" "public_1" {
  vpc_id                  = aws_vpc.mlops.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = false
  tags                    = { Name = "mlops-public-1", Tier = "public" }
}

resource "aws_subnet" "public_2" {
  vpc_id                  = aws_vpc.mlops.id
  cidr_block              = "10.0.2.0/24"
  availability_zone       = "${var.region}b"
  map_public_ip_on_launch = false
  tags                    = { Name = "mlops-public-2", Tier = "public" }
}

# Private subnets — RDS lives here (no internet access)
resource "aws_subnet" "private_1" {
  vpc_id            = aws_vpc.mlops.id
  cidr_block        = "10.0.10.0/24"
  availability_zone = "${var.region}a"
  tags              = { Name = "mlops-private-1", Tier = "private" }
}

resource "aws_subnet" "private_2" {
  vpc_id            = aws_vpc.mlops.id
  cidr_block        = "10.0.11.0/24"
  availability_zone = "${var.region}b"
  tags              = { Name = "mlops-private-2", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.mlops.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.mlops.id
  }
  tags = { Name = "mlops-public-rt" }
}

resource "aws_route_table_association" "public_1" {
  subnet_id      = aws_subnet.public_1.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_2" {
  subnet_id      = aws_subnet.public_2.id
  route_table_id = aws_route_table.public.id
}

# =============================================================================
# SECURITY GROUPS
# =============================================================================

resource "aws_security_group" "k3s_node" {
  name        = "mlops-k3s-node"
  description = "K3s single-node - SSH, K3s API, app NodePorts, internal K8s traffic"
  vpc_id      = aws_vpc.mlops.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.my_cidr]
  }

  ingress {
    description = "K3s API"
    from_port   = 6443
    to_port     = 6443
    protocol    = "tcp"
    cidr_blocks = [local.my_cidr]
  }

  ingress {
    description = "Grafana NodePort"
    from_port   = 32000
    to_port     = 32000
    protocol    = "tcp"
    cidr_blocks = [local.my_cidr]
  }

  ingress {
    description = "MLflow NodePort"
    from_port   = 32001
    to_port     = 32001
    protocol    = "tcp"
    cidr_blocks = [local.my_cidr]
  }

  # Future app NodePorts: Airflow (32002), Feast (32003), PayStream (32004), Agents (32005)
  ingress {
    description = "FastAPI fraud detection serving"
    from_port   = 32002
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [local.my_cidr]
  }

  ingress {
  from_port   = 32003
  to_port     = 32003
  protocol    = "tcp"
  cidr_blocks = [local.my_cidr]
  description = "PayStream traffic controller"
}

ingress {
  from_port   = 32080
  to_port     = 32080
  protocol    = "tcp"
  cidr_blocks = [local.my_cidr]
  description = "Airflow webserver UI"
}

  ingress {
    description = "Flannel VXLAN"
    from_port   = 8472
    to_port     = 8472
    protocol    = "udp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  ingress {
    description = "kubelet API"
    from_port   = 10250
    to_port     = 10250
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "mlops-k3s-node" }
}

resource "aws_security_group" "rds" {
  name        = "mlops-rds"
  description = "RDS PostgreSQL - inbound only from K3s node SG"
  vpc_id      = aws_vpc.mlops.id

  ingress {
    description     = "PostgreSQL from K3s node"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.k3s_node.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "mlops-rds" }
}

# =============================================================================
# RDS — PostgreSQL 15
# =============================================================================

resource "random_password" "rds" {
  length           = 24
  special          = true
  override_special = "!#$%^&*()-_=+"
  # Note: [] and {} intentionally excluded — they break PostgreSQL URL parsing
}

resource "aws_db_subnet_group" "mlops" {
  name        = "mlops-rds-subnet-group"
  subnet_ids  = [aws_subnet.private_1.id, aws_subnet.private_2.id]
  description = "MLOps RDS - private subnets across 2 AZs"
  tags        = { Name = "mlops-rds-subnet-group" }
}

resource "aws_db_parameter_group" "postgres15" {
  name        = "mlops-postgres15"
  family      = "postgres15"
  description = "MLOps PostgreSQL 15 - logging enabled for slow query visibility"

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  tags = { Name = "mlops-postgres15" }

  lifecycle {
    # Prevent destroy+recreate — RDS blocks deletion of parameter groups
    # attached to a running instance. Description changes would trigger this.
    ignore_changes = [description]
  }
}

resource "aws_db_instance" "mlops" {
  identifier = "mlops-postgres"

  engine         = "postgres"
  engine_version = "15"
  instance_class = "db.t3.micro"

  db_name  = "mlops"
  username = "YOUR_DB_USERNAME"
  password = random_password.rds.result

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.mlops.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres15.name

  multi_az                = false
  publicly_accessible     = false
  deletion_protection     = false
  skip_final_snapshot     = true
  backup_retention_period = 1

  tags = {
    Name    = "mlops-postgres"
    Purpose = "MLflow backend + Airflow metadata DB"
  }

  timeouts {
    create = "30m"
  }
}

# =============================================================================
# S3 BUCKETS (x5)
# =============================================================================

resource "aws_s3_bucket" "mlops" {
  for_each = local.buckets
  bucket   = each.value
  tags     = { Name = each.value, Purpose = each.key }
}

resource "aws_s3_bucket_versioning" "mlops" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.mlops[each.key].id

  versioning_configuration {
    status = contains(["code_sync", "mlflow"], each.key) ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "mlops" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.mlops[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "mlops" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.mlops[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
# ECR REPOSITORIES (x3)
# =============================================================================

resource "aws_ecr_repository" "repos" {
  for_each = toset(["fraud-detection-serving", "paystream", "agents"])
  name     = each.value

  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = each.value }
}

resource "aws_ecr_lifecycle_policy" "repos" {
  for_each   = aws_ecr_repository.repos
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# =============================================================================
# IAM — EC2 INSTANCE ROLE
# =============================================================================

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mlops_ec2" {
  name               = "mlops-ec2-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = { Name = "mlops-ec2-role" }
}

data "aws_iam_policy_document" "mlops_ec2" {

  statement {
    sid    = "S3BucketAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = flatten([
      for bucket in local.buckets : [
        "arn:aws:s3:::${bucket}",
        "arn:aws:s3:::${bucket}/*"
      ]
    ])
  }

  statement {
    sid    = "SSMParameterRead"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = [
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter/mlops/*"
    ]
  }

  statement {
    sid    = "ECRPull"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:DescribeRepositories",
      "ecr:ListImages",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPush"
    effect = "Allow"
    actions = [
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [for repo in aws_ecr_repository.repos : repo.arn]
  }

  statement {
    sid    = "CloudWatch"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "RDSDescribe"
    effect = "Allow"
    actions = ["rds:DescribeDBInstances"]
    resources = ["*"]
  }

  statement {
    sid    = "EC2Describe"
    effect = "Allow"
    actions = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "mlops_ec2" {
  name        = "mlops-ec2-policy"
  description = "MLOps EC2 policy - scoped to project S3 buckets and /mlops/* SSM params"
  policy      = data.aws_iam_policy_document.mlops_ec2.json
}

resource "aws_iam_role_policy_attachment" "mlops_ec2" {
  role       = aws_iam_role.mlops_ec2.name
  policy_arn = aws_iam_policy.mlops_ec2.arn
}

resource "aws_iam_role_policy_attachment" "ssm_agent" {
  role       = aws_iam_role.mlops_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "mlops_ec2" {
  name = "mlops-ec2-profile"
  role = aws_iam_role.mlops_ec2.name
  tags = { Name = "mlops-ec2-profile" }
}

# =============================================================================
# SSM PARAMETER STORE
# =============================================================================

resource "aws_ssm_parameter" "code_sync_bucket" {
  name  = "/mlops/code-sync-bucket"
  type  = "String"
  value = local.buckets.code_sync
  tags  = { Name = "mlops-code-sync-bucket" }
}

resource "aws_ssm_parameter" "s3_code_bucket_alias" {
  name  = "/mlops/s3-code-bucket"
  type  = "String"
  value = local.buckets.code_sync
  tags  = { Name = "mlops-s3-code-bucket-alias" }
}

resource "aws_ssm_parameter" "mlflow_artifacts_bucket" {
  name  = "/mlops/mlflow-artifacts-bucket"
  type  = "String"
  value = local.buckets.mlflow
  tags  = { Name = "mlops-mlflow-artifacts-bucket" }
}

resource "aws_ssm_parameter" "db_url" {
  name  = "/mlops/db-url"
  type  = "SecureString"
  value = "postgresql://YOUR_DB_USERNAME:${random_password.rds.result}@${aws_db_instance.mlops.address}:5432/mlops?sslmode=require"
  tags  = { Name = "mlops-db-url" }

  lifecycle {
    # Prevents Terraform from overwriting the manually URL-encoded password.
    # The value in SSM is the source of truth after first apply.
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "anthropic_api_key" {
  name  = "/mlops/anthropic-api-key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "google_api_key" {
  name  = "/mlops/google-api-key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "openai_api_key" {
  name  = "/mlops/openai-api-key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "perplexity_api_key" {
  name  = "/mlops/perplexity-api-key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
}

resource "aws_ssm_parameter" "env_file" {
  name = "/mlops/env-file"
  type = "SecureString"
  value = join("\n", [
    "ANTHROPIC_API_KEY=REPLACE_ME",
    "GOOGLE_API_KEY=REPLACE_ME",
    "OPENAI_API_KEY=REPLACE_ME",
    "PERPLEXITY_API_KEY=REPLACE_ME",
    "DB_URL=postgresql://YOUR_DB_USERNAME:${random_password.rds.result}@${aws_db_instance.mlops.address}:5432/mlops?sslmode=require",
    "MLFLOW_TRACKING_URI=http://localhost:32001",
    "FEAST_FEATURE_STORE_BUCKET=${local.buckets.feature_store}",
    "GE_RESULTS_BUCKET=${local.buckets.ge_results}",
    "RAW_DATA_BUCKET=${local.buckets.raw_data}",
    "CODE_SYNC_BUCKET=${local.buckets.code_sync}",
    "MLFLOW_ARTIFACTS_BUCKET=${local.buckets.mlflow}",
    "AWS_DEFAULT_REGION=${var.region}",
  ])
  lifecycle { ignore_changes = [value] }
  tags = { Name = "mlops-env-file" }
}

resource "aws_ssm_parameter" "kaggle_username" {
  name  = "/mlops/kaggle-username"
  type  = "String"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
  tags  = { Name = "mlops-kaggle-username" }
}

resource "aws_ssm_parameter" "kaggle_api_key" {
  name  = "/mlops/kaggle-api-key"
  type  = "SecureString"
  value = "REPLACE_ME"
  lifecycle { ignore_changes = [value] }
  tags  = { Name = "mlops-kaggle-api-key" }
}

resource "aws_ssm_parameter" "airflow_fernet_key" {
  name  = "/mlops/airflow-fernet-key"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  # tags = local.common_tags
}

resource "aws_ssm_parameter" "airflow_webserver_secret" {
  name  = "/mlops/airflow-webserver-secret"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  # tags = local.common_tags
}

resource "aws_ssm_parameter" "mlflow_db_url" {
  name  = "/mlops/mlflow-db-url"
  type  = "SecureString"
  value = "REPLACE_ME"

  lifecycle {
    ignore_changes = [value]
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "rds_endpoint" {
  value       = aws_db_instance.mlops.endpoint
  description = "RDS PostgreSQL endpoint (host:port)"
}

output "rds_address" {
  value       = aws_db_instance.mlops.address
  description = "RDS hostname (without port)"
}

output "rds_password" {
  value     = random_password.rds.result
  sensitive = true
}

output "s3_buckets" {
  value       = local.buckets
  description = "All project S3 bucket names"
}

output "ecr_repos" {
  value       = { for k, v in aws_ecr_repository.repos : k => v.repository_url }
  description = "ECR repository URLs"
}

output "k3s_sg_id" {
  value = aws_security_group.k3s_node.id
}

output "vpc_id" {
  value = aws_vpc.mlops.id
}

output "public_subnet_1_id" {
  value = aws_subnet.public_1.id
}

output "iam_instance_profile" {
  value = aws_iam_instance_profile.mlops_ec2.name
}

output "key_pair_note" {
  value = "SSH key pair is managed by terraform/ephemeral — auto-generated on apply, auto-deleted on destroy."
}

output "next_steps" {
  value = <<-INFO

  ════════════════════════════════════════════════════════════
  ✅  Persistent infrastructure ready
  ════════════════════════════════════════════════════════════
  RDS endpoint:   ${aws_db_instance.mlops.endpoint}
  S3 buckets:     mlops-*-YOUR_SUFFIX (no account ID in names)
  Public IP:      assigned fresh on each terraform apply (ephemeral)

  Next steps:
  1. Empty old buckets + apply to rename:
       terraform apply

  2. Re-sync code to new bucket:
       cd ../.. && ./scripts/sync-to-s3.sh

  3. Spin up the instance:
       cd terraform/ephemeral && terraform apply
  ════════════════════════════════════════════════════════════
  INFO
}