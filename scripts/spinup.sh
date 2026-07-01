# =============================================================================
# spinup.sh — Start of day: sync code then apply ephemeral infra
# Usage: ./scripts/spinup.sh
# =============================================================================
# #!/bin/bash
# set -e
#
# echo "Starting MLOps Platform..."
# echo ""
#
# # 1. Sync latest code to S3
# echo "Step 1/2: Syncing code to S3..."
# ./scripts/sync-to-s3.sh
# echo ""
#
# # 2. Apply ephemeral Terraform
# echo "Step 2/2: Applying ephemeral infrastructure..."
# cd terraform/ephemeral
# terraform init -upgrade -reconfigure > /dev/null
# terraform apply -auto-approve
# cd ../..
#
# echo ""
# echo "============================================================"
# echo "Platform is up!"
# echo ""
# terraform -chdir=terraform/ephemeral output platform_urls
# echo "============================================================"