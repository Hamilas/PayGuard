# =============================================================================
# Fraud Detection MLOps — Ephemeral Infrastructure
# =============================================================================
# Apply every morning. Destroy every night.
# Resources: SSH key pair, Spot instance, auto-assigned public IP
#
# Usage:
#   Morning:  cd terraform/ephemeral && terraform apply
#   Evening:  cd terraform/ephemeral && terraform destroy
#
# Shortcut scripts (from project root):
#   ./scripts/spinup.sh    ← sync code to S3 then apply
#   ./scripts/teardown.sh  ← destroy only (persistent untouched)
# =============================================================================

terraform {
  required_version = ">= 1.5"

  backend "local" {
    path = ".terraform-state/ephemeral.tfstate"
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "fraudshield-mlops"
      Environment = "dev"
      ManagedBy   = "terraform"
      Lifecycle   = "ephemeral"
    }
  }
}

# =============================================================================
# VARIABLES
# =============================================================================

variable "region" {
  description = "AWS region — must match persistent module"
  type        = string
  default     = "us-east-2"
}

variable "spot_max_price" {
  description = "Max spot price for t3.2xlarge. Check current price before setting."
  type        = string
  default     = "0.12" # On-demand is ~$0.3328/hr. Spot typically ~$0.08-0.12/hr.
}

variable "ssh_key_path" {
  description = "Local path where the auto-generated private key will be written"
  type        = string
  default     = "~/.ssh/mlops-ephemeral-key.pem"
}

variable "bootstrap_timeout" {
  description = "Max time to wait for bootstrap.sh to complete. Increase if pip installs are slow."
  type        = string
  default     = "30m"
}

# =============================================================================
# DATA SOURCES — Read persistent module outputs via AWS resource lookups
# =============================================================================
# We use tag-based data sources instead of terraform_remote_state.
# This keeps the two modules fully decoupled — no shared state file.

data "aws_caller_identity" "current" {}

# Fetch your current public IP at apply time — no static value in tfvars
data "http" "my_ip" {
  url = "https://checkip.amazonaws.com"
}

locals {
  my_cidr = "${chomp(data.http.my_ip.response_body)}/32"
}

# Security group for the K3s node
data "aws_security_group" "k3s_node" {
  filter {
    name   = "tag:Name"
    values = ["mlops-k3s-node"]
  }
}

# Public subnet for the spot instance
data "aws_subnet" "public_1" {
  filter {
    name   = "tag:Name"
    values = ["mlops-public-1"]
  }
}

# IAM instance profile
data "aws_iam_instance_profile" "mlops_ec2" {
  name = "mlops-ec2-profile"
}

# Latest Amazon Linux 2023 AMI — always fresh, no hardcoded AMI IDs
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# =============================================================================
# SSH KEY PAIR — Auto-generated, auto-deleted
# =============================================================================
# On apply:   generates RSA-4096 key → uploads public to AWS → writes private to ~/.ssh/
# On destroy: deletes aws_key_pair from AWS + local_file removes ~/.ssh/mlops-ephemeral-key.pem
#
# The private key is stored in Terraform state (encrypted at rest for local backend).
# For a team setup, move to S3 backend with SSE + KMS.

resource "tls_private_key" "mlops" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "mlops" {
  key_name   = "mlops-ephemeral-key-${formatdate("YYYYMMDD-hhmmss", timestamp())}"
  public_key = tls_private_key.mlops.public_key_openssh
  tags       = { Name = "mlops-ephemeral-key" }

  # Prevent key name churn on every plan — name is fixed at creation
  lifecycle {
    ignore_changes = [key_name]
  }
}

# Write private key to ~/.ssh/ on your Mac
# local_file automatically DELETES this file on terraform destroy
resource "local_file" "private_key" {
  content         = tls_private_key.mlops.private_key_pem
  filename        = pathexpand(var.ssh_key_path)
  file_permission = "0600" # SSH refuses to use keys with open permissions
}

# =============================================================================
# SPOT INSTANCE REQUEST
# =============================================================================

resource "aws_spot_instance_request" "k3s" {
  ami                  = data.aws_ami.al2023.id
  instance_type        = "t3.2xlarge"  # 8 vCPU / 32GB RAM
  spot_price           = var.spot_max_price
  wait_for_fulfillment = true           # Block until AWS fulfills the request
  spot_type            = "one-time"

  subnet_id                   = data.aws_subnet.public_1.id
  vpc_security_group_ids      = [data.aws_security_group.k3s_node.id]
  iam_instance_profile        = data.aws_iam_instance_profile.mlops_ec2.name
  key_name                    = aws_key_pair.mlops.key_name
  associate_public_ip_address = true  # Auto-assigned public IP — changes on each apply

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 50   # 50GB: OS (8) + K3s images (~10) + data + logs
    iops                  = 3000 # gp3 baseline — no extra cost
    throughput            = 125
    delete_on_termination = true
    encrypted             = true
  }

  # Minimal user_data — just tag the underlying instance (AWS spot limitation)
  # # All real setup is done by bootstrap.sh via remote-exec
  user_data = <<-USERDATA
    #!/bin/bash
    # Spot instance started — waiting for remote-exec provisioner
    echo "$(date): Instance started, waiting for bootstrap via remote-exec" >> /var/log/pre-bootstrap.log
  USERDATA

  tags = {
    Name      = "mlops-k3s-node"
    SpotGroup = "mlops"
  }

  timeouts {
    create = "10m"
  }
}

# Tag the underlying EC2 instance (spot requests and instances have separate tags in AWS)
resource "aws_ec2_tag" "k3s_instance_name" {
  resource_id = aws_spot_instance_request.k3s.spot_instance_id
  key         = "Name"
  value       = "mlops-k3s-node"

  depends_on = [aws_spot_instance_request.k3s]
}

# =============================================================================
# BOOTSTRAP PROVISIONER
# =============================================================================
# Runs bootstrap.sh on the remote instance via SSH.
# Uses null_resource so bootstrap can depend_on spot instance
# If bootstrap fails, run: terraform taint null_resource.bootstrap
# then terraform apply to re-run just the provisioner.

resource "null_resource" "bootstrap" {

  # Re-run bootstrap if the instance ID changes (new spot instance)
  triggers = {
    instance_id = aws_spot_instance_request.k3s.spot_instance_id
  }

  connection {
    type        = "ssh"
    host        = aws_spot_instance_request.k3s.public_ip
    user        = "ec2-user"
    private_key = tls_private_key.mlops.private_key_pem  # In-memory — never touches disk here
    timeout     = "5m"   # Wait up to 5 min for SSH to become available
  }

  # Step 1: Upload bootstrap.sh from your Mac to the instance
  provisioner "file" {
    source      = "${path.root}/../../bootstrap/bootstrap.sh"
    destination = "/tmp/bootstrap.sh"
  }

  # Step 2: Execute bootstrap.sh
  # set -e is already inside bootstrap.sh so any failure exits with non-zero
  provisioner "remote-exec" {
    inline = [
      "chmod +x /tmp/bootstrap.sh",
      "sudo /tmp/bootstrap.sh",
      # Verify the completion marker bootstrap.sh writes at the very end
      "test -f /var/log/bootstrap-complete && echo '✅ Bootstrap verified complete' || (echo '❌ Bootstrap did not complete' && exit 1)",
    ]

    connection {
      type        = "ssh"
      host        = aws_spot_instance_request.k3s.public_ip
      user        = "ec2-user"
      private_key = tls_private_key.mlops.private_key_pem
      timeout     = var.bootstrap_timeout
    }
  }
}

# =============================================================================
# OUTPUTS
# =============================================================================

output "platform_urls" {
  value = <<-URLS

  ════════════════════════════════════════════════════════════
  ✅  MLOps Platform is UP
  ════════════════════════════════════════════════════════════
  Instance ID:  ${aws_spot_instance_request.k3s.spot_instance_id}
  Public IP:    ${aws_spot_instance_request.k3s.public_ip}  ← changes daily

  📊 Grafana:   http://${aws_spot_instance_request.k3s.public_ip}:32000
                admin / YOUR_GRAFANA_PASSWORD

  🔬 MLflow:    http://${aws_spot_instance_request.k3s.public_ip}:32001
                (ready ~3 min after boot)

  🔑 SSH:
    ssh -i ~/.ssh/mlops-ephemeral-key.pem ec2-user@${aws_spot_instance_request.k3s.public_ip}

  ════════════════════════════════════════════════════════════
  URLS
}

output "instance_id" {
  value = aws_spot_instance_request.k3s.spot_instance_id
}

output "public_ip" {
  value       = aws_spot_instance_request.k3s.public_ip
  description = "Auto-assigned public IP — changes on every apply"
}

output "ssh_key_path" {
  value = pathexpand(var.ssh_key_path)
}

output "ssh_command" {
  value = "ssh -i ${pathexpand(var.ssh_key_path)} ec2-user@${aws_spot_instance_request.k3s.public_ip}"
}