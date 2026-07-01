#!/bin/bash
# =============================================================================
# MLOps Startup Health Check
# =============================================================================
# Run once after every SSH in to ensure K3s networking is stable.
# Fixes the recurring local-path-provisioner + service network issue
# caused by K3s iptables rules not fully propagating after cold start.
#
# Usage: bash scripts/startup-health.sh
# =============================================================================

set -e

echo "=============================================="
echo "MLOps Startup Health Check"
echo "Timestamp: $(date)"
echo "=============================================="

# -----------------------------------------------------------------------------
# 1. Restart K3s to rebuild iptables + service network
# -----------------------------------------------------------------------------
echo ""
echo "Step 1: Restarting K3s to ensure clean networking..."
sudo systemctl restart k3s
sleep 90

# Wait for node Ready
echo "   Waiting for node Ready..."
for i in {1..12}; do
    if kubectl get nodes | grep -q "Ready"; then
        echo "   Node Ready"
        break
    fi
    sleep 10
done

# -----------------------------------------------------------------------------
# 2. Ensure storage directory exists
# -----------------------------------------------------------------------------
echo ""
echo "Step 2: Ensuring storage directory..."
sudo mkdir -p /var/lib/rancher/k3s/storage
sudo chmod 755 /var/lib/rancher/k3s/storage
echo "   Storage directory ready"

# -----------------------------------------------------------------------------
# 3. Restart all crashing pods
# -----------------------------------------------------------------------------
echo ""
echo "Step 3: Restarting crashing pods..."

kubectl delete pod -n kube-system -l app=local-path-provisioner \
    --ignore-not-found
kubectl delete pod -n monitoring -l app.kubernetes.io/name=kube-state-metrics \
    --ignore-not-found
kubectl delete pod -n monitoring -l app.kubernetes.io/name=prometheus-operator \
    --ignore-not-found
kubectl delete pod -n mlflow -l app=mlflow \
    --ignore-not-found

echo "   Pods restarted"

# -----------------------------------------------------------------------------
# 4. Wait for pods to stabilize
# -----------------------------------------------------------------------------
echo ""
echo "Step 4: Waiting 90s for pods to stabilize..."
sleep 90

# -----------------------------------------------------------------------------
# 5. Final health check
# -----------------------------------------------------------------------------
echo ""
echo "Step 5: Final health check..."
echo ""
kubectl get pods -A --no-headers | awk '{print $1, $2, $4}' | column -t

echo ""
# MLflow health
if curl -s http://localhost:32001/health &>/dev/null; then
    echo "MLflow: healthy"
else
    echo "MLflow: still starting (pip install ~2 min)"
fi

PUBLIC_IP=$(curl -s --max-time 10 ifconfig.me)
echo ""
echo "=============================================="
echo "Startup health check complete"
echo "   Grafana → http://${PUBLIC_IP}:32000"
echo "   MLflow  → http://${PUBLIC_IP}:32001"
echo "=============================================="