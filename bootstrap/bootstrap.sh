#!/bin/bash
# =============================================================================
# Fraud Detection MLOps Pipeline — Bootstrap Script
# =============================================================================
# Version:   1.0
# Reuses:    Patterns from AI-Powered Self-Healing DevSecOps Pipeline
# Installs:  K3s, Helm, Docker, Python 3.11
# Deploys:   kube-prometheus-stack, Loki, MLflow
# Called by: Terraform remote-exec provisioner (NOT user_data)
# =============================================================================

set -e

exec > >(tee /var/log/bootstrap.log)
exec 2>&1

echo "=============================================="
echo "Fraud Detection MLOps Pipeline — Bootstrap"
echo "Timestamp: $(date)"
echo "=============================================="

# Project-wide constants
PROJECT_DIR="/home/ec2-user/payguard-mlops"
VENV_DIR="${PROJECT_DIR}/.venv"
REGION="us-east-2"

# =============================================================================
# SECTION A: SYSTEM SETUP
# Reused verbatim from AI-Powered Self-Healing DevSecOps Pipeline bootstrap.sh
# =============================================================================

# -----------------------------------------------------------------------------
# 1. System Update
# -----------------------------------------------------------------------------
echo ""
echo "Step 1: Updating system packages..."
sudo dnf update -y
echo "System packages updated"

# -----------------------------------------------------------------------------
# 2. Install Required Packages
# -----------------------------------------------------------------------------
echo ""
echo "Step 2: Installing required packages..."
sudo dnf install -y \
    wget \
    git \
    htop \
    jq \
    tar \
    gzip \
    unzip \
    python3.11 \
    python3.11-pip \
    python3.11-devel \
    gcc \
    make \
    postgresql15   # psql client — used to verify RDS connectivity
echo "Required packages installed"

# -----------------------------------------------------------------------------
# 3. Install Docker
# -----------------------------------------------------------------------------
echo ""
echo "Step 3: Installing Docker..."
sudo dnf install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker ec2-user
echo "Docker installed and running"
sudo docker --version

# -----------------------------------------------------------------------------
# 4. Configure System for K3s
# -----------------------------------------------------------------------------
echo ""
echo " Step 4: Configuring system for Kubernetes..."
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab || true

sudo tee /etc/modules-load.d/k3s.conf > /dev/null <<MODULES
br_netfilter
overlay
MODULES

sudo modprobe br_netfilter
sudo modprobe overlay

sudo tee /etc/sysctl.d/k3s.conf > /dev/null <<SYSCTL
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
SYSCTL

sudo sysctl --system
echo "System configured for Kubernetes"

# -----------------------------------------------------------------------------
# 5. Install K3s with Public IP (TLS only)
# -----------------------------------------------------------------------------
echo ""
echo "Step 5: Installing K3s..."

PRIVATE_IP=$(hostname -I | awk '{print $1}')
echo "   Private IP: $PRIVATE_IP"

echo "   Detecting Public IP via ifconfig.me..."
PUBLIC_IP=$(curl -s --max-time 10 ifconfig.me || echo "")

if [ -z "$PUBLIC_IP" ]; then
    echo "    ifconfig.me unreachable — falling back to private IP"
    PUBLIC_IP=$PRIVATE_IP
else
    echo "   Public IP (EIP): $PUBLIC_IP"
fi

echo "   Configuring K3s with:"
echo "     - Private IP: $PRIVATE_IP"
echo "     - Public IP:  $PUBLIC_IP"

sudo mkdir -p /etc/rancher/k3s

sudo tee /etc/rancher/k3s/config.yaml > /dev/null <<EOF
write-kubeconfig-mode: "0644"
disable:
  - traefik
tls-san:
  - "$PUBLIC_IP"
  - "$PRIVATE_IP"
  - "127.0.0.1"
  - "localhost"
# node-external-ip: "$PUBLIC_IP"
node-external-ip: "$PRIVATE_IP"
EOF

curl -sfL https://get.k3s.io | sh -

if sudo systemctl is-active --quiet k3s; then
    echo "K3s installed and running"
else
    echo "K3s failed to start"
    sudo systemctl status k3s
    exit 1
fi

# -----------------------------------------------------------------------------
# 6. Configure kubectl Access
# -----------------------------------------------------------------------------
echo ""
echo "Step 6: Configuring kubectl access..."

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

MAX_RETRIES=10
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if sudo kubectl cluster-info &>/dev/null; then
        echo "kubectl connected to K3s"
        break
    fi
    echo "   Waiting for kubectl... ($((RETRY_COUNT+1))/$MAX_RETRIES)"
    sleep 5
    RETRY_COUNT=$((RETRY_COUNT+1))
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "kubectl connection failed"
    exit 1
fi

sudo mkdir -p /home/ec2-user/.kube
sudo cp /etc/rancher/k3s/k3s.yaml /home/ec2-user/.kube/config
sudo chown -R ec2-user:ec2-user /home/ec2-user/.kube
sudo chmod 600 /home/ec2-user/.kube/config
echo "kubectl configured for ec2-user"

# -----------------------------------------------------------------------------
# 7. kubectl Aliases
# -----------------------------------------------------------------------------
echo ""
echo " Step 7: Adding kubectl aliases..."

cat <<'BASHRC' >> /home/ec2-user/.bashrc

# ── kubectl ────────────────────────────────────────────────────────────
alias k='kubectl'
alias kgp='kubectl get pods'
alias kgs='kubectl get services'
alias kgn='kubectl get nodes'
alias kga='kubectl get all -A'
alias kgpa='kubectl get pods -A'
alias kgpo='kubectl get pods -A -o wide'

# ── MLOps shortcuts ────────────────────────────────────────────────────
alias mlops-status='kubectl get pods -A --no-headers | grep -v -E "Running|Completed"'
alias mlops-logs='sudo journalctl -u mlops-agent -f'
alias mlops-monitor='tail -f /var/log/mlops-monitor.log'
alias mlops-alerts='tail -f /var/log/mlops-alerts.log'

# ── K3s shortcuts ───────────────────────────────────────────────────────
alias k3s-status='sudo systemctl status k3s'
alias k3s-logs='sudo journalctl -u k3s -f'

source <(kubectl completion bash)
complete -F __start_kubectl k

export KUBECONFIG=/home/ec2-user/.kube/config
export PROJECT_DIR=/home/ec2-user/payguard-mlops
export VENV_DIR=/home/ec2-user/payguard-mlops/.venv
BASHRC

echo "kubectl aliases added"

# -----------------------------------------------------------------------------
# 8. Wait for K3s Node Ready
# -----------------------------------------------------------------------------
echo ""
echo "Step 8: Waiting for K3s node to be ready..."

MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if sudo kubectl get nodes | grep -q "Ready"; then
        echo "Node is ready!"
        break
    fi
    echo "   Waiting for node... ($((RETRY_COUNT+1))/$MAX_RETRIES)"
    sleep 10
    RETRY_COUNT=$((RETRY_COUNT+1))
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo " Node did not become ready in time"
    exit 1
fi

echo ""
echo "Cluster:"
echo "───────────────────────────────────────────"
sudo kubectl get nodes -o wide

# -----------------------------------------------------------------------------
# 9. Install Helm
# -----------------------------------------------------------------------------
echo ""
echo "Step 9: Installing Helm..."
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

if command -v helm &>/dev/null; then
    echo "Helm installed"
    helm version
else
    echo "Helm installation failed"
    exit 1
fi

# -----------------------------------------------------------------------------
# 10. Install AWS CLI v2
# -----------------------------------------------------------------------------
echo ""
echo "Step 10: Installing AWS CLI v2..."
cd /tmp
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
sudo ./aws/install
rm -rf awscliv2.zip aws
echo "AWS CLI installed"
aws --version

# -----------------------------------------------------------------------------
# 11. Create Project Directory Structure
# -----------------------------------------------------------------------------
echo ""
echo "Step 11: Creating project directories..."

mkdir -p ${PROJECT_DIR}/{k8s,ml,agents,paystream,dags,monitoring,scripts,data,logs}
mkdir -p ${PROJECT_DIR}/k8s/{monitoring,mlflow,airflow,feast,serving,paystream,agents}
mkdir -p ${PROJECT_DIR}/ml/{data,features,training,serving}
sudo mkdir -p /var/lib/rancher/k3s/storage
sudo chmod 755 /var/lib/rancher/k3s/storage
sudo chown -R ec2-user:ec2-user ${PROJECT_DIR}
echo "Project directories created"

# -----------------------------------------------------------------------------
# 12. Install Python Packages
# -----------------------------------------------------------------------------
echo ""
echo "Step 12: Installing Python packages..."

python3.11 -m venv ${VENV_DIR}
${VENV_DIR}/bin/pip install --upgrade pip -q

echo "   Installing core ML packages..."
${VENV_DIR}/bin/pip install -q \
    xgboost==2.1.0 \
    scikit-learn==1.5.1 \
    imbalanced-learn==0.12.3 \
    pandas==2.2.2 \
    numpy==1.26.4

echo "   Installing infrastructure + serving packages..."
${VENV_DIR}/bin/pip install -q \
    boto3==1.35.0 \
    psycopg2-binary==2.9.9 \
    requests==2.32.3 \
    fastapi==0.115.0 \
    uvicorn==0.30.6 \
    pydantic==2.8.2 \
    prometheus-client==0.20.0 \
    redis==5.0.8 \
    mlflow==2.15.0

echo "Python packages installed"
echo "   Deferred to Day-specific requirements files:"
echo "     Day-2: feast, redis"
echo "     Day-3: great-expectations"
echo "     Day-4: evidently"
echo "     Day-5: langgraph, langchain, anthropic, openai, chromadb"

cat <<'VENVRC' >> /home/ec2-user/.bashrc

# Activate MLOps venv
source /home/ec2-user/payguard-mlops/.venv/bin/activate
VENVRC

# -----------------------------------------------------------------------------
# 13. System Optimizations
# -----------------------------------------------------------------------------
echo ""
echo " Step 13: Applying system optimizations..."
sudo tee -a /etc/security/limits.conf > /dev/null <<LIMITS
* soft nofile 65536
* hard nofile 65536
* soft nproc  4096
* hard nproc  4096
LIMITS
echo "System optimizations applied"

# -----------------------------------------------------------------------------
# 13.5. Wait for CoreDNS to Fully Initialize
# (Simple sleep — do NOT restart CoreDNS. This exact pattern is what works.)
# -----------------------------------------------------------------------------
echo ""
echo "Step 13.5: Waiting for CoreDNS to fully initialize..."
sleep 30

READY_STATUS=$(sudo kubectl get pods -n kube-system -l k8s-app=kube-dns \
  -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)

if [ "$READY_STATUS" = "true" ]; then
    echo "CoreDNS is ready"
else
    echo "CoreDNS still initializing — waiting 60 more seconds..."
    sleep 60
fi
echo "Proceeding with application deployment"

# =============================================================================
# SECTION B: SECRETS + CODE SYNC
# Same pattern as DevSecOps project — SSM first, S3 second, no credentials on disk
# =============================================================================

# -----------------------------------------------------------------------------
# 14. Fetch Secrets from SSM Parameter Store
# -----------------------------------------------------------------------------
echo ""
echo "Step 14: Fetching secrets from SSM Parameter Store..."
sleep 10   # Let IAM instance profile credentials propagate

# Full .env fetched as one SecureString (same pattern as DevSecOps project)
aws ssm get-parameter \
    --name "/mlops/env-file" \
    --with-decryption \
    --region ${REGION} \
    --query 'Parameter.Value' \
    --output text > ${PROJECT_DIR}/.env

chown ec2-user:ec2-user ${PROJECT_DIR}/.env
chmod 600 ${PROJECT_DIR}/.env
echo ".env fetched from SSM"

# Individual parameters needed at bootstrap time
CODE_SYNC_BUCKET=$(aws ssm get-parameter \
    --name "/mlops/code-sync-bucket" \
    --region ${REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "")

MLFLOW_ARTIFACTS_BUCKET=$(aws ssm get-parameter \
    --name "/mlops/mlflow-artifacts-bucket" \
    --region ${REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "")

DB_URL=$(aws ssm get-parameter \
    --name "/mlops/db-url" \
    --with-decryption \
    --region ${REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "")

echo "Bootstrap parameters fetched"

# -----------------------------------------------------------------------------
# 15. Pull Code from S3
# -----------------------------------------------------------------------------
echo ""
echo "Step 15: Pulling code from S3..."

if [ -n "$CODE_SYNC_BUCKET" ]; then
    aws s3 sync s3://${CODE_SYNC_BUCKET}/code/ \
        ${PROJECT_DIR}/ \
        --region ${REGION} \
        --exclude "*.pyc" \
        --exclude "__pycache__/*" \
        --exclude ".env"   # .env lives in SSM only — never in S3

    chown -R ec2-user:ec2-user ${PROJECT_DIR}/
    echo "Code pulled from S3 (s3://${CODE_SYNC_BUCKET}/code/)"
else
    echo " CODE_SYNC_BUCKET not set — skipping S3 sync (first run?)"
fi

# =============================================================================
# SECTION C: KUBERNETES DEPLOYMENT
# MLOps-specific — new for this project
# =============================================================================

# -----------------------------------------------------------------------------
# 16. Add Helm Repositories
# -----------------------------------------------------------------------------
echo ""
echo "Step 16: Adding Helm repositories..."

# Run as ec2-user so Helm config lands in the right home dir
sudo -u ec2-user helm repo add prometheus-community \
    https://prometheus-community.github.io/helm-charts
sudo -u ec2-user helm repo add grafana \
    https://grafana.github.io/helm-charts
sudo -u ec2-user helm repo add bitnami \
    https://charts.bitnami.com/bitnami
sudo -u ec2-user helm repo update

echo "Helm repositories ready"

# -----------------------------------------------------------------------------
# 17. Create Kubernetes Namespaces
# Idempotent: --dry-run=client + apply means safe to re-run
# -----------------------------------------------------------------------------
echo ""
echo "  Step 17: Creating Kubernetes namespaces..."

for NS in monitoring mlops mlflow airflow feast paystream agents; do
    sudo kubectl create namespace ${NS} --dry-run=client -o yaml | sudo kubectl apply -f -
    echo "   namespace/${NS}"
done

# -----------------------------------------------------------------------------
# 17.5. Pre-Helm preparation
# -----------------------------------------------------------------------------
# DO NOT restart K3s here — it breaks CoreDNS readiness and pod DNS resolution.
# K3s started cleanly in Step 5 and CoreDNS initialized in Step 13.5.
# All we need here is the storage directory for local-path-provisioner.
# -----------------------------------------------------------------------------
echo ""
echo " Step 17.5: Pre-Helm preparation..."

# Ensure local-path storage directory exists before any PVC is requested
# Also register it as a systemd pre-start so it survives K3s restarts
sudo mkdir -p /var/lib/rancher/k3s/storage
sudo chmod 755 /var/lib/rancher/k3s/storage

sudo mkdir -p /etc/systemd/system/k3s.service.d/
sudo tee /etc/systemd/system/k3s.service.d/storage.conf > /dev/null <<EOF
[Service]
ExecStartPre=/bin/mkdir -p /var/lib/rancher/k3s/storage
ExecStartPre=/bin/chmod 755 /var/lib/rancher/k3s/storage
EOF
sudo systemctl daemon-reload

# Re-apply kernel networking settings after CNI bridge is created
# br_netfilter must be active AFTER cni0 bridge exists for pod→service routing
# Without this, pods get i/o timeout reaching 10.43.0.1 (K3s API ClusterIP)
sudo modprobe br_netfilter
sudo sysctl -w net.bridge.bridge-nf-call-iptables=1
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=1
sudo sysctl -w net.ipv4.ip_forward=1
echo "Kernel networking settings re-applied"

echo "Step 17.5 complete — storage directory ready"

sleep 5
echo "============================================================================== "
echo "Skipping Steps 18-20 for now — will deploy apps in Day-2 with code from S3"
echo "============================================================================== "
sleep 5


# =============================================================================
# SECTION D: MONITORING + SYSTEMD
# Same pattern as DevSecOps project
# =============================================================================

# -----------------------------------------------------------------------------
# 21. Configure AWS SSM Agent
# -----------------------------------------------------------------------------
echo ""
echo "Step 21: Configuring SSM Agent..."
systemctl start amazon-ssm-agent
systemctl enable amazon-ssm-agent

if systemctl is-active --quiet amazon-ssm-agent; then
    echo "SSM Agent running"
else
    systemctl restart amazon-ssm-agent
    sleep 5
    systemctl is-active --quiet amazon-ssm-agent && \
        echo "SSM Agent restarted" || echo "SSM Agent failed"
fi

# -----------------------------------------------------------------------------
# 22. Create setup-after-s3-sync.sh
# Called manually after each code push to S3 to install new deps and restart services
# Mirrors setup-after-rsync.sh from DevSecOps project
# -----------------------------------------------------------------------------
cat > /home/ec2-user/setup-after-s3-sync.sh <<'SETUP'
#!/bin/bash
echo "=========================================="
echo "Post S3-sync setup for MLOps Pipeline"
echo "=========================================="

PROJECT_DIR="/home/ec2-user/payguard-mlops"
VENV_DIR="${PROJECT_DIR}/.venv"
REGION="us-east-2"

# Pull latest code from S3
CODE_SYNC_BUCKET=$(aws ssm get-parameter \
    --name "/mlops/code-sync-bucket" \
    --region ${REGION} \
    --query 'Parameter.Value' \
    --output text 2>/dev/null)

if [ -n "$CODE_SYNC_BUCKET" ]; then
    echo "Pulling latest code from s3://${CODE_SYNC_BUCKET}/code/ ..."
    aws s3 sync s3://${CODE_SYNC_BUCKET}/code/ \
        ${PROJECT_DIR}/ \
        --region ${REGION} \
        --exclude "*.pyc" \
        --exclude "__pycache__/*" \
        --exclude ".env"
    echo "Code synced"
fi

cd ${PROJECT_DIR}

# Update Python packages if requirements changed
if [ -f "requirements.txt" ]; then
    echo "Updating Python packages..."
    source ${VENV_DIR}/bin/activate
    pip install -r requirements.txt -q
    echo "Packages updated"
fi

# Restart MLOps agent if it exists
if systemctl is-active --quiet mlops-agent; then
    echo "Restarting MLOps agent..."
    sudo systemctl restart mlops-agent
    sleep 3
    sudo systemctl status mlops-agent --no-pager | head -5
fi

echo "=========================================="
echo "Sync complete"
echo "   Grafana: http://$(curl -s --max-time 10 ifconfig.me):32000"
echo "   MLflow:  http://$(curl -s --max-time 10 ifconfig.me):32001"
echo "=========================================="
SETUP

chown ec2-user:ec2-user /home/ec2-user/setup-after-s3-sync.sh
chmod +x /home/ec2-user/setup-after-s3-sync.sh

# -----------------------------------------------------------------------------
# 23. Bootstrap Health Monitor (systemd timer)
# Reused directly from DevSecOps project — adapted for MLOps services
# -----------------------------------------------------------------------------
echo ""
echo "Step 23: Setting up bootstrap health monitor..."

touch /var/log/mlops-monitor.log
touch /var/log/mlops-alerts.log
chown ec2-user:ec2-user /var/log/mlops-monitor.log /var/log/mlops-alerts.log

cat > /home/ec2-user/monitor-mlops.sh <<'MONITOR'
#!/bin/bash
LOG_FILE="/var/log/mlops-monitor.log"
ALERT_LOG="/var/log/mlops-alerts.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

log()   { echo "[${TIMESTAMP}] $1" >> ${LOG_FILE}; }
alert() {
    echo "[${TIMESTAMP}] ALERT: $1" >> ${ALERT_LOG}
    echo "[${TIMESTAMP}] ALERT: $1" >> ${LOG_FILE}
    logger -t mlops-monitor "ALERT: $1"
}

# K3s node health
if sudo kubectl get nodes | grep -q "Ready"; then
    log "K3s: Ready"
else
    alert "K3s node not Ready"
fi

# Core namespace pod counts
for NS in monitoring mlflow airflow feast paystream agents; do
    RUNNING=$(sudo kubectl get pods -n ${NS} --no-headers 2>/dev/null | grep -c "Running" || echo "0")
    TOTAL=$(sudo kubectl get pods -n ${NS} --no-headers 2>/dev/null | wc -l || echo "0")
    [ "${RUNNING}" -eq "${TOTAL}" ] && \
        log "${NS}: ${RUNNING}/${TOTAL} pods running" || \
        alert "${NS}: only ${RUNNING}/${TOTAL} pods running"
done

# MLOps agent (Week 3 onwards)
if systemctl is-enabled --quiet mlops-agent 2>/dev/null; then
    systemctl is-active --quiet mlops-agent && \
        log "mlops-agent: running" || \
        alert "mlops-agent: DOWN — attempting restart" && \
        sudo systemctl restart mlops-agent
fi

# Resource check
MEMORY_PCT=$(free | awk 'NR==2{printf "%.0f", $3*100/$2}')
DISK_PCT=$(df / | awk 'NR==2{print $5}' | tr -d '%')
[ "${MEMORY_PCT}" -gt 85 ] && alert "High memory: ${MEMORY_PCT}%" || log "Memory: ${MEMORY_PCT}%"
[ "${DISK_PCT}"   -gt 85 ] && alert "High disk: ${DISK_PCT}%"     || log "Disk: ${DISK_PCT}%"

log "─── monitor check complete ───"
MONITOR

chmod +x /home/ec2-user/monitor-mlops.sh
chown ec2-user:ec2-user /home/ec2-user/monitor-mlops.sh

# systemd service + timer (same pattern as DevSecOps project)
cat > /etc/systemd/system/mlops-monitor.service <<'SERVICE'
[Unit]
Description=MLOps Pipeline Health Monitor
[Service]
Type=oneshot
User=ec2-user
ExecStart=/home/ec2-user/monitor-mlops.sh
SERVICE

cat > /etc/systemd/system/mlops-monitor.timer <<'TIMER'
[Unit]
Description=Run MLOps monitor every 60 seconds
Requires=mlops-monitor.service
[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5
[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable mlops-monitor.timer
systemctl start mlops-monitor.timer
echo "Health monitor running (every 60s)"

# Logrotate (same as DevSecOps project)
cat > /etc/logrotate.d/mlops-monitor <<'LOGROTATE'
/var/log/mlops-monitor.log
/var/log/mlops-alerts.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 0644 ec2-user ec2-user
}
LOGROTATE

# =============================================================================
# SECTION E: RDS CONNECTIVITY CHECK
# Verifies persistent infrastructure is reachable from the ephemeral instance
# =============================================================================

# -----------------------------------------------------------------------------
# 24. Verify RDS Connectivity
# -----------------------------------------------------------------------------
echo ""
echo " Step 24: Verifying RDS connectivity..."

if [ -n "$DB_URL" ]; then
    # Extract host from postgresql://user:pass@host:port/db
    DB_HOST=$(echo "$DB_URL" | sed 's|.*@||' | cut -d':' -f1)

    MAX_RETRIES=5
    RETRY_COUNT=0
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        if bash -c "echo > /dev/tcp/${DB_HOST}/5432" 2>/dev/null; then
          echo "RDS reachable at ${DB_HOST}:5432"
          break
        fi
        echo "   Waiting for RDS... ($((RETRY_COUNT+1))/$MAX_RETRIES)"
        sleep 10
        RETRY_COUNT=$((RETRY_COUNT+1))
    done

    if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
        echo " RDS not reachable — check security group rules"
        echo "     Expected: K3s SG allowed on RDS SG port 5432"
    fi
else
    echo " DB_URL not set — skipping RDS check"
fi

# Wait for pods to stabilize before writing status
echo ""
echo "Waiting 60s for pods to stabilize..."
sleep 60

# =============================================================================
# SECTION F: SETUP INFO + MOTD
# Adapted from DevSecOps project
# =============================================================================

# -----------------------------------------------------------------------------
# 25. Create setup-info.txt
# -----------------------------------------------------------------------------
echo ""
echo "Step 25: Creating setup info..."

INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
INSTANCE_TYPE=$(curl -s http://169.254.169.254/latest/meta-data/instance-type)
AZ=$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone)

# ── REPLACE WITH THIS ─────────────────────────────────────────────────────────
# At Step 25, PUBLIC_IP is already set correctly from Step 5 via ifconfig.me.
# Just reuse it — no re-fetch needed.
DISPLAY_IP=$PUBLIC_IP
echo "   Public IP (EIP): $DISPLAY_IP"
# ─────────────────────────────────────────────────────────────────────────────

MONITORING_PODS=$(sudo kubectl get pods -n monitoring --no-headers 2>/dev/null | grep -c "Running" || echo "?")
MLFLOW_PODS=$(sudo kubectl get pods    -n mlflow     --no-headers 2>/dev/null | grep -c "Running" || echo "?")

# ── Resolve binary versions with full paths + fallbacks ──────────────────────
# Command substitutions inside heredocs inherit the script's PATH (root),
# but full paths make this robust regardless of who re-runs this block.
OS_RELEASE=$(cat /etc/system-release 2>/dev/null || \
             grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'"' -f2 || \
             echo "Unknown OS")

K3S_VERSION=$(sudo /usr/local/bin/k3s --version 2>/dev/null | head -1 || \
              /usr/local/bin/k3s --version 2>/dev/null | head -1 || \
              echo "k3s (version unknown)")

HELM_VERSION=$(/usr/local/bin/helm version --short 2>/dev/null || \
               sudo /usr/local/bin/helm version --short 2>/dev/null || \
               echo "helm (version unknown)")

PYTHON_VERSION=$(${VENV_DIR}/bin/python --version 2>/dev/null || \
                 python3.11 --version 2>/dev/null || \
                 echo "python (version unknown)")

DOCKER_VERSION=$(docker --version 2>/dev/null || echo "docker (version unknown)")
AWS_VERSION=$(aws --version 2>/dev/null || echo "aws-cli (version unknown)")
# ─────────────────────────────────────────────────────────────────────────────

cat > ${PROJECT_DIR}/setup-info.txt <<SETUPINFO
=======================================================
Fraud Detection MLOps Pipeline — Setup Information
=======================================================
Completed:     $(date)
Instance:      ${INSTANCE_ID} | ${INSTANCE_TYPE} | ${AZ}
Public IP:     ${DISPLAY_IP}
Private IP:    ${PRIVATE_IP}

Software:
  OS:          ${OS_RELEASE}
  K3s:         ${K3S_VERSION}
  Helm:        ${HELM_VERSION}
  Python:      ${PYTHON_VERSION}
  Docker:      ${DOCKER_VERSION}
  AWS CLI:     ${AWS_VERSION}

Cluster:
$(sudo kubectl get nodes 2>/dev/null)

Pods by Namespace:
$(sudo kubectl get pods -A --no-headers 2>/dev/null | awk '{print $1, $2, $4}' | column -t)

Access:
  Grafana:     http://${DISPLAY_IP}:32000   (admin / YOUR_GRAFANA_PASSWORD)
  MLflow:      http://${DISPLAY_IP}:32001   (ready ~3 min after boot)
  K3s API:     https://${DISPLAY_IP}:6443

S3 Buckets:
  Code sync:   s3://${CODE_SYNC_BUCKET}/code/
  MLflow:      s3://${MLFLOW_ARTIFACTS_BUCKET}/experiments/

Logs:
  Bootstrap:   /var/log/bootstrap.log
  Monitor:     /var/log/mlops-monitor.log
  Alerts:      /var/log/mlops-alerts.log

Quick Commands:
  kubectl get nodes
  kubectl get pods -A
  helm list -A
  source ${VENV_DIR}/bin/activate
  ~/setup-after-s3-sync.sh       ← run after every code push
  cat ${PROJECT_DIR}/setup-info.txt

Day-2 next steps:
  - Deploy Redis (Feast online store)
  - Configure Feast feature repo
  - Download + upload PaySim dataset to S3
  - Build feature engineering pipeline
=======================================================
SETUPINFO

chown ec2-user:ec2-user ${PROJECT_DIR}/setup-info.txt

# -----------------------------------------------------------------------------
# 26. MOTD
# -----------------------------------------------------------------------------
sudo tee /etc/motd > /dev/null <<MOTD

╔══════════════════════════════════════════════════════════════╗
║     Fraud Detection MLOps Pipeline — Dev Environment        ║
╚══════════════════════════════════════════════════════════════╝

Quick Status:
  kubectl get nodes
  kubectl get pods -A

Project:   ${PROJECT_DIR}
Python:    source ${VENV_DIR}/bin/activate

Access:
  Grafana:  http://${PUBLIC_IP}:32000  (admin / YOUR_GRAFANA_PASSWORD)
  MLflow:   http://${PUBLIC_IP}:32001  (ready ~3 min after boot)
  K3s API:  https://${PUBLIC_IP}:6443

Aliases:
  k / kgp / kgs / kgn / kga / kgpa
  k3s-status  k3s-logs  mlops-status  mlops-logs

After code push to S3:
  ~/setup-after-s3-sync.sh

Setup info:
  cat ${PROJECT_DIR}/setup-info.txt

──────────────────────────────────────────────────────────────

MOTD

# -----------------------------------------------------------------------------
# 27. Final Summary
# -----------------------------------------------------------------------------
echo ""
echo "=============================================="
echo "BOOTSTRAP COMPLETE!"
echo "=============================================="
echo ""
echo "Summary:"
echo "  • K3s:                Running (single-node)"
echo "  • Helm:               Installed"
echo "  • Docker:             Running"
echo "  • Python 3.11:        Configured (${VENV_DIR})"
echo "  • AWS CLI:            Installed"
echo "  • SSM Agent:          Running"
echo "  • Namespaces:         monitoring / mlops / mlflow / airflow / feast / paystream / agents"
echo "  • Prometheus:         Deployed (namespace: monitoring)"
echo "  • Grafana:            NodePort 32000"
echo "  • Alertmanager:       Deployed"
echo "  • Loki + Promtail:    Deployed"
echo "  • MLflow:             NodePort 32001 (ready in ~3 min)"
echo "  • Health Monitor:     systemd timer (60s interval)"
echo ""
echo "Access Points:"
echo "  Grafana → http://${PUBLIC_IP}:32000  (admin / YOUR_GRAFANA_PASSWORD)"
echo "  MLflow  → http://${PUBLIC_IP}:32001"
echo ""
echo " Bootstrap time: ~15-20 min"
echo ""
echo "Day-1 done. Day-2: Feast + Redis + PaySim data pipeline."
echo ""
echo "Timestamp: $(date)"
echo "=============================================="

sudo touch /var/log/bootstrap-complete
echo "$(date)" | sudo tee /var/log/bootstrap-complete