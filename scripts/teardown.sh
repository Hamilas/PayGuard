# =============================================================================
# teardown.sh — End of day: destroy ephemeral infra only
# Usage: ./scripts/teardown.sh
# =============================================================================
# #!/bin/bash
# set -e
#
# echo "Tearing down ephemeral infrastructure..."
# echo "   (Persistent resources: RDS, S3, SSM, EIP — untouched)"
# echo ""
#
# cd terraform/ephemeral
# terraform destroy -auto-approve
# cd ../..
#
# echo ""
# echo "Spot instance terminated. No overnight charges."
# echo "   Persistent resources still running (~\$0.50/day)"
# echo ""
# echo "   Tomorrow: ./scripts/spinup.sh"