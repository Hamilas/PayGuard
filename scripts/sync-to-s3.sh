#!/bin/bash
# =============================================================================
# sync-to-s3.sh — Run from Mac after every code change
# Usage: ./scripts/sync-to-s3.sh
# =============================================================================

set -e

AWS_REGION="us-east-2"
S3_BUCKET=$(aws ssm get-parameter \
    --name "/mlops/s3-code-bucket" \
    --region "$AWS_REGION" \
    --query 'Parameter.Value' \
    --output text)

echo "Syncing code to s3://$S3_BUCKET/code/"

aws s3 sync . "s3://$S3_BUCKET/code/" \
    --region "$AWS_REGION" \
    --exclude "*.pyc" \
    --exclude ".env" \
    --exclude "__pycache__/*" \
    --exclude ".terraform/*" \
    --exclude "*.tfstate*" \
    --exclude ".git/*" \
    --exclude ".venv/*" \
    --exclude "*.egg-info/*" \
    --exclude ".DS_Store" \
    --delete

echo "Sync complete → s3://$S3_BUCKET/code/"
echo "   Run ./scripts/spinup.sh to apply infrastructure"