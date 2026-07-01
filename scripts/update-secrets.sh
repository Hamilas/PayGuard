# =============================================================================
# update-secrets.sh — Push new API keys to SSM
# Usage: ./scripts/update-secrets.sh
# =============================================================================
# #!/bin/bash
# Uncomment and fill before use — never commit with real values
#
# AWS_REGION="us-east-2"
#
# aws ssm put-parameter \
#   --name "/mlops/anthropic-api-key" \
#   --value "YOUR_ANTHROPIC_KEY" \
#   --type SecureString --overwrite \
#   --region "$AWS_REGION"
#
# aws ssm put-parameter \
#   --name "/mlops/google-api-key" \
#   --value "YOUR_GOOGLE_KEY" \
#   --type SecureString --overwrite \
#   --region "$AWS_REGION"
#
# aws ssm put-parameter \
#   --name "/mlops/openai-api-key" \
#   --value "YOUR_OPENAI_KEY" \
#   --type SecureString --overwrite \
#   --region "$AWS_REGION"
#
# aws ssm put-parameter \
#   --name "/mlops/perplexity-api-key" \
#   --value "YOUR_PERPLEXITY_KEY" \
#   --type SecureString --overwrite \
#   --region "$AWS_REGION"
#
# echo "API keys updated in SSM"