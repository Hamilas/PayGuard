#!/bin/bash
# =============================================================================
# deploy-platform.sh — Run after every terraform apply + SSH in
# Usage: bash ~/payguard-mlops/scripts/deploy-platform.sh
# =============================================================================
set -e

REGION="us-east-2"
PUBLIC_IP=$(curl -s --max-time 10 ifconfig.me)

echo "=============================================="
echo "Platform Deployment"
echo "Timestamp: $(date)"
echo "=============================================="
echo ""

# =============================================================================
# STEP 1: Verify K3s cluster health + DNS
# =============================================================================
echo "Step 1: Verifying K3s cluster health..."

MAX_RETRIES=12
RETRY=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    if kubectl get nodes 2>/dev/null | grep -q "Ready"; then
        break
    fi
    echo "   Waiting for K3s node... ($((RETRY+1))/$MAX_RETRIES)"
    sleep 10
    RETRY=$((RETRY+1))
done

DNS_OK=$(kubectl run dns-test --image=busybox:1.28 --rm -it \
    --restart=Never --quiet \
    -- nslookup kubernetes.default 2>/dev/null | grep -c "Address" || echo "0")
if [ "$DNS_OK" -gt "0" ]; then
    echo "   Pod DNS resolution working"
else
    echo "    DNS test inconclusive — proceeding anyway"
fi

# =============================================================================
# STEP 2: Prometheus + Grafana
# =============================================================================
echo ""
echo "Step 2: Deploying kube-prometheus-stack..."

kubectl delete configmap kube-prometheus-stack-grafana-datasource \
    -n monitoring --ignore-not-found 2>/dev/null && \
    echo "   Stale Grafana datasource ConfigMap cleared" || true

helm upgrade --install kube-prometheus-stack \
    prometheus-community/kube-prometheus-stack \
    --namespace monitoring \
    --version 82.10.3 \
    --set prometheusOperator.admissionWebhooks.enabled=false \
    --set prometheusOperator.tls.enabled=false \
    --set grafana.adminUser=YOUR_GRAFANA_USERNAME \
    --set grafana.adminPassword=YOUR_GRAFANA_PASSWORD \
    --set grafana.service.type=NodePort \
    --set grafana.service.nodePort=32000 \
    --set grafana.persistence.enabled=false \
    --set prometheus.prometheusSpec.retention=7d \
    --no-hooks \
    --timeout 10m \
    --create-namespace 2>&1 | tail -3
echo "kube-prometheus-stack deployed"

echo "   Waiting for Grafana API..."
for i in 1 2 3 4 5; do
    RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
        http://localhost:32000/api/datasources \
        -u "admin:YOUR_GRAFANA_PASSWORD" 2>/dev/null)
    if [ "$RESULT" = "200" ]; then
        curl -s -X POST http://localhost:32000/api/datasources \
            -H "Content-Type: application/json" \
            -u "admin:YOUR_GRAFANA_PASSWORD" \
            -d '{
                "name": "Prometheus",
                "type": "prometheus",
                "url": "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
                "access": "proxy",
                "isDefault": true,
                "jsonData": {"timeInterval": "15s"}
            }' > /dev/null 2>&1 || true
        DS_ID=$(curl -s http://localhost:32000/api/datasources/name/Prometheus \
            -u "admin:YOUR_GRAFANA_PASSWORD" 2>/dev/null | \
            python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
        if [ -n "$DS_ID" ]; then
            curl -s -X PUT "http://localhost:32000/api/datasources/${DS_ID}" \
                -H "Content-Type: application/json" \
                -u "admin:YOUR_GRAFANA_PASSWORD" \
                -d '{
                    "name": "Prometheus",
                    "type": "prometheus",
                    "url": "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
                    "access": "proxy",
                    "isDefault": true,
                    "jsonData": {"timeInterval": "15s"}
                }' > /dev/null 2>&1 || true
        fi
        echo "   Prometheus datasource configured in Grafana"
        break
    fi
    echo "   Waiting for Grafana API... ($i/5)"
    sleep 15
done

# =============================================================================
# STEP 3: Loki + Promtail
# =============================================================================
echo ""
echo "Step 3: Deploying Loki + Promtail..."

helm upgrade --install loki grafana/loki-stack \
    --namespace monitoring \
    --version 2.10.2 \
    --set loki.auth_enabled=false \
    --set loki.persistence.enabled=true \
    --set loki.persistence.size=5Gi \
    --timeout 5m 2>&1 | tail -3
echo "Loki + Promtail deployed"

# =============================================================================
# STEP 4: MLflow
# =============================================================================
echo ""
echo "Step 4: Deploying MLflow..."

MLFLOW_RAW_URL=$(aws ssm get-parameter \
    --name "/mlops/mlflow-db-url" \
    --with-decryption --region "$REGION" \
    --query 'Parameter.Value' --output text)

RAW_URL=$(aws ssm get-parameter \
    --name "/mlops/db-url" \
    --with-decryption --region "$REGION" \
    --query 'Parameter.Value' --output text)

export MLFLOW_RAW_URL="$MLFLOW_RAW_URL"

FIXED_URL=$(python3 - << 'PYEOF'
import re, os
from urllib.parse import quote
url = os.environ.get('MLFLOW_RAW_URL', '')
base, _, query = url.partition('?')
match = re.match(r'(postgresql://[^:]+:)(.+?)(@.+)', base)
if match:
    prefix, password, suffix = match.groups()
    encoded = prefix + quote(password, safe='') + suffix
    print(encoded + ('?' + query if query else ''))
else:
    print(url)
PYEOF
)

MLFLOW_BUCKET=$(aws ssm get-parameter \
    --name "/mlops/mlflow-artifacts-bucket" \
    --region "$REGION" \
    --query 'Parameter.Value' --output text)

echo "   Checking MLflow DB schema compatibility..."
python3 - << PYEOF
import re, sys
from urllib.parse import urlparse, unquote
try:
    import psycopg2
    url = "$FIXED_URL"
    base = url.split('?')[0]
    p = urlparse(base)
    conn = psycopg2.connect(
        host=p.hostname, port=p.port or 5432,
        dbname=p.path.lstrip('/'),
        user=p.username,
        password=unquote(p.password),
        sslmode='require',
        connect_timeout=10
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'alembic_version'
        )
    """)
    alembic_exists = cur.fetchone()[0]
    if alembic_exists:
        cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
        row = cur.fetchone()
        revision = row[0] if row else None
        KNOWN_GOOD = '4465047574b1'
        if revision and revision != KNOWN_GOOD:
            print(f"    Wrong schema revision: {revision} — clearing MLflow tables")
            conn.autocommit = True
            mlflow_tables = [
                'datasets','inputs','input_tags','model_versions','trace_info',
                'trace_tags','trace_request_metadata','runs','experiments','tags',
                'metrics','params','experiment_tags','latest_metrics',
                'registered_models','registered_model_tags','model_version_tags',
                'registered_model_aliases'
            ]
            for t in mlflow_tables:
                try:
                    cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
                except Exception:
                    pass
            cur.execute("DELETE FROM alembic_version")
            print("   MLflow tables cleared — will reinitialize fresh")
        else:
            print(f"   MLflow DB schema OK (revision: {revision})")
    else:
        print("   ℹ  No existing schema — fresh initialization")
    conn.close()
except Exception as e:
    print(f"    DB check skipped: {e}")
PYEOF

kubectl delete secret mlflow-config -n mlflow --ignore-not-found 2>/dev/null
kubectl create secret generic mlflow-config \
    --namespace mlflow \
    --from-literal=db-url="${FIXED_URL}" \
    --from-literal=artifact-root="s3://${MLFLOW_BUCKET}/experiments"
echo "   mlflow-config secret created"

kubectl apply -f - << 'MANIFEST'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mlflow
  namespace: mlflow
  labels:
    app: mlflow
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mlflow
  template:
    metadata:
      labels:
        app: mlflow
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "5000"
    spec:
      initContainers:
      - name: wait-for-dns
        image: busybox:1.28
        command: ['sh', '-c',
          'until nslookup pypi.org > /dev/null 2>&1; do echo waiting for DNS; sleep 5; done']
      containers:
      - name: mlflow
        image: ghcr.io/mlflow/mlflow:v2.15.0
        command: ["/bin/bash", "-c"]
        args:
        - |
          pip install boto3 psycopg2-binary -q --no-cache-dir
          exec mlflow server \
            --host 0.0.0.0 \
            --port 5000 \
            --backend-store-uri "$DB_URL" \
            --default-artifact-root "$ARTIFACT_ROOT" \
            --serve-artifacts
        env:
        - name: DB_URL
          valueFrom:
            secretKeyRef:
              name: mlflow-config
              key: db-url
        - name: ARTIFACT_ROOT
          valueFrom:
            secretKeyRef:
              name: mlflow-config
              key: artifact-root
        - name: AWS_DEFAULT_REGION
          value: "us-east-2"
        ports:
        - containerPort: 5000
        resources:
          requests:
            cpu: 200m
            memory: 512Mi
          limits:
            cpu: 500m
            memory: 1Gi
        readinessProbe:
          httpGet:
            path: /health
            port: 5000
          initialDelaySeconds: 30
          periodSeconds: 10
          failureThreshold: 12
        livenessProbe:
          httpGet:
            path: /health
            port: 5000
          initialDelaySeconds: 60
          periodSeconds: 30
          failureThreshold: 3
---
apiVersion: v1
kind: Service
metadata:
  name: mlflow
  namespace: mlflow
spec:
  type: NodePort
  selector:
    app: mlflow
  ports:
  - port: 5000
    targetPort: 5000
    nodePort: 32001
MANIFEST
echo "MLflow deployed"

echo "   Waiting for MLflow to initialize (up to 3 min)..."
for i in $(seq 1 18); do
    if curl -s http://localhost:32001/health 2>/dev/null | grep -q "OK"; then
        echo "   MLflow healthy — proceeding"
        break
    fi
    echo "   MLflow starting... ($i/18)"
    sleep 10
done

# =============================================================================
# STEP 5: Redis (Feast online store)
# =============================================================================
echo ""
echo "Step 5: Deploying Redis (Feast online store)..."

helm upgrade --install redis bitnami/redis \
    --namespace feast \
    --set auth.enabled=false \
    --set master.resources.requests.cpu=100m \
    --set master.resources.requests.memory=128Mi \
    --set master.resources.limits.cpu=200m \
    --set master.resources.limits.memory=256Mi \
    --set replica.replicaCount=0 \
    --set master.persistence.enabled=false \
    --timeout 5m 2>&1 | tail -3

kubectl rollout status statefulset/redis-master -n feast --timeout=3m
echo "Redis deployed"

REDIS_IP=$(kubectl get svc redis-master -n feast \
    -o jsonpath='{.spec.clusterIP}')
mkdir -p /home/ec2-user/payguard-mlops/ml/features
cat > /home/ec2-user/payguard-mlops/ml/features/feature_store.yaml << EOF
project: fraud_detection
registry: s3://mlops-feature-store-YOUR_SUFFIX/feast/registry.pb
provider: aws
online_store:
  type: redis
  connection_string: "${REDIS_IP}:6379"
offline_store:
  type: file
entity_key_serialization_version: 2
EOF
echo "   feature_store.yaml updated with Redis IP: ${REDIS_IP}"

# =============================================================================
# STEP 6: Airflow
# =============================================================================
echo ""
echo " Step 6: Deploying Airflow..."

helm repo add apache-airflow https://airflow.apache.org 2>/dev/null || true
helm repo update apache-airflow 2>/dev/null | tail -1

FERNET_KEY=$(aws ssm get-parameter \
    --name "/mlops/airflow-fernet-key" \
    --with-decryption --region "$REGION" \
    --query 'Parameter.Value' --output text)
WEBSERVER_SECRET=$(aws ssm get-parameter \
    --name "/mlops/airflow-webserver-secret" \
    --with-decryption --region "$REGION" \
    --query 'Parameter.Value' --output text)
echo "   Airflow keys fetched from SSM"

DB_CONN_AIRFLOW=$(echo "$RAW_URL" | sed 's|postgresql://|postgresql+psycopg2://|')

echo "   Running Airflow DB reset (before Helm install)..."
kubectl run airflow-db-reset \
    --namespace airflow \
    --image=apache/airflow:2.9.3-python3.11 \
    --restart=Never \
    --env="AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=${DB_CONN_AIRFLOW}" \
    --env="AIRFLOW__CORE__EXECUTOR=LocalExecutor" \
    -- bash -c "airflow db reset --yes && echo 'Airflow DB reset complete'" \
    2>/dev/null

kubectl wait pod/airflow-db-reset -n airflow \
    --for=condition=Ready --timeout=60s 2>/dev/null || true

RETRY=0
while [ $RETRY -lt 18 ]; do
    STATUS=$(kubectl get pod airflow-db-reset -n airflow \
        --no-headers 2>/dev/null | awk '{print $3}')
    if [ "$STATUS" = "Completed" ] || [ "$STATUS" = "Succeeded" ]; then
        kubectl logs airflow-db-reset -n airflow 2>/dev/null | tail -3
        echo "   Airflow DB reset complete"
        break
    elif [ "$STATUS" = "Error" ] || [ "$STATUS" = "Failed" ]; then
        echo "    DB reset failed — checking logs..."
        kubectl logs airflow-db-reset -n airflow 2>/dev/null | tail -5
        break
    fi
    sleep 10
    RETRY=$((RETRY+1))
done
kubectl delete pod airflow-db-reset -n airflow --ignore-not-found 2>/dev/null || true

for SECRET in airflow-fernet-key airflow-webserver-secret-key; do
    kubectl delete secret "$SECRET" -n airflow --ignore-not-found 2>/dev/null || true
done

kubectl create secret generic airflow-fernet-key \
    --namespace airflow \
    --from-literal=fernet-key="${FERNET_KEY}" \
    --dry-run=client -o yaml | \
    kubectl annotate --local -f - \
        meta.helm.sh/release-name=airflow \
        meta.helm.sh/release-namespace=airflow \
        --dry-run=client -o yaml | \
    kubectl label --local -f - \
        app.kubernetes.io/managed-by=Helm \
        --dry-run=client -o yaml | \
    kubectl apply -f -

kubectl create secret generic airflow-webserver-secret-key \
    --namespace airflow \
    --from-literal=webserver-secret-key="${WEBSERVER_SECRET}" \
    --dry-run=client -o yaml | \
    kubectl annotate --local -f - \
        meta.helm.sh/release-name=airflow \
        meta.helm.sh/release-namespace=airflow \
        --dry-run=client -o yaml | \
    kubectl label --local -f - \
        app.kubernetes.io/managed-by=Helm \
        --dry-run=client -o yaml | \
    kubectl apply -f -
echo "   Airflow secrets pre-created with Helm labels"

helm upgrade --install airflow apache-airflow/airflow \
    --namespace airflow \
    --version 1.15.0 \
    --set executor=LocalExecutor \
    --set "data.metadataConnection.user=YOUR_DB_USERNAME" \
    --set "data.metadataConnection.pass=$(echo $RAW_URL | sed 's|postgresql://[^:]*:||' | sed 's|@.*||')" \
    --set "data.metadataConnection.host=YOUR_RDS_ENDPOINT.us-east-2.rds.amazonaws.com" \
    --set "data.metadataConnection.db=mlops" \
    --set "data.metadataConnection.port=5432" \
    --set "data.metadataConnection.sslmode=require" \
    --set fernetKey="${FERNET_KEY}" \
    --set webserverSecretKey="${WEBSERVER_SECRET}" \
    --set webserver.defaultUser.enabled=true \
    --set webserver.defaultUser.role=Admin \
    --set webserver.defaultUser.username=admin \
    --set webserver.defaultUser.password=YOUR_AIRFLOW_PASSWORD \
    --set webserver.defaultUser.email=admin@mlops.local \
    --set webserver.defaultUser.firstName=MLOps \
    --set webserver.defaultUser.lastName=Admin \
    --set webserver.service.type=NodePort \
    --set scheduler.resources.requests.memory=2Gi \
    --set scheduler.resources.limits.memory=4Gi \
    --set scheduler.resources.limits.cpu=2 \
    --set dags.persistence.enabled=true \
    --set dags.persistence.storageClassName=local-path \
    --set dags.persistence.size=1Gi \
    --set dags.persistence.accessMode=ReadWriteOnce \
    --set logs.persistence.enabled=false \
    --set flower.enabled=false \
    --set statsd.enabled=false \
    --set redis.enabled=false \
    --set postgresql.enabled=false \
    --set workers.replicas=0 \
    --set images.airflow.repository=apache/airflow \
    --set images.airflow.tag=2.9.3-python3.11 \
    --set '_pip_additional_requirements=' \
    --no-hooks \
    --timeout 10m 2>&1 | tail -5

kubectl patch svc airflow-webserver -n airflow \
    -p '{"spec": {"type": "NodePort", "ports": [{"port": 8080, "targetPort": 8080, "nodePort": 32080, "protocol": "TCP"}]}}' \
    2>/dev/null || true

echo "Airflow deployed"

# =============================================================================
# STEP 7b: Deploy serving layer (fraud-api + PayStream)
# =============================================================================
echo ""
echo "Step 7b: Deploying fraud-api + PayStream..."

kubectl apply -f /home/ec2-user/payguard-mlops/k8s/serving/fraud-api.yaml
kubectl apply -f /home/ec2-user/payguard-mlops/k8s/paystream/paystream.yaml

# Create source code ConfigMaps (pods mount these at runtime)
kubectl create configmap fraud-api-code \
    --from-file=fraud_api.py=/home/ec2-user/payguard-mlops/ml/serving/fraud_api.py \
    -n mlops --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
kubectl create configmap paystream-code \
    --from-file=paystream.py=/home/ec2-user/payguard-mlops/paystream/paystream.py \
    -n paystream --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

echo "   ConfigMaps created (fraud-api-code, paystream-code)"

# Wait for fraud-api (PayStream can lag — it waits for fraud-api internally)
echo "   Waiting for fraud-api rollout..."
kubectl rollout status deployment/fraud-api -n mlops --timeout=3m 2>/dev/null && \
    echo "   fraud-api ready" || echo "    fraud-api not ready yet — continuing"

echo "Serving layer deployed"

# =============================================================================
# STEP 7: Agentic layer Python packages
# Day-7 additions: ollama, flask, sentence-transformers, evidently, redis
# =============================================================================
echo ""
echo "Step 7: Installing agentic layer packages..."

# Fix venv ownership before installing (bootstrap runs as root)
sudo chown -R ec2-user:ec2-user /home/ec2-user/payguard-mlops/.venv

/home/ec2-user/payguard-mlops/.venv/bin/python3 -m pip install \
    "anthropic>=0.40.0" \
    "openai>=1.50.0" \
    "google-genai>=1.0.0" \
    "langgraph>=0.2.28" \
    "langchain-core>=0.3.0" \
    "chromadb==0.5.5" \
    "ollama" \
    "flask" \
    "sentence-transformers" \
    "evidently==0.4.30" \
    "redis>=5.0.0" \
    "requests==2.32.3" \
    "sniffio" "anyio" "httpx" \
    "plotly==5.18.0" "typing_inspect" "statsmodels" "ujson" "watchdog" \
    -q --no-cache-dir 2>&1 | tail -3

python3 -c "
import anthropic, openai, chromadb, ollama, flask, redis, evidently
from langgraph.graph import StateGraph
from google import genai
from sentence_transformers import SentenceTransformer
print('  anthropic:', anthropic.__version__)
print('  openai:', openai.__version__)
print('  chromadb:', chromadb.__version__)
print('  langgraph: OK')
print('  google-genai: OK')
print('  ollama: OK')
print('  flask: OK')
print('  redis: OK')
print('  evidently: OK')
print('  sentence-transformers: OK')
" && echo "Agentic packages installed" || echo " Some packages failed — check above"

# Pre-download sentence-transformers model to avoid HuggingFace pull on first agent run
echo "   Pre-downloading sentence-transformers model (all-MiniLM-L6-v2)..."
python3 -c "
from sentence_transformers import SentenceTransformer
import os
cache_dir = '/home/ec2-user/.cache/sentence_transformers'
os.makedirs(cache_dir, exist_ok=True)
model = SentenceTransformer('all-MiniLM-L6-v2', cache_folder='/home/ec2-user/.cache/sentence_transformers')
test = model.encode('test embedding')
print(f'   Embedder ready — vector dim: {len(test)}')
" 2>/dev/null && echo "Embedder pre-loaded" || echo " Embedder pre-load failed — will download on first agent run"

# Verify all 4 LLM API keys
echo "   Verifying LLM API keys..."
python3 - << 'PYEOF'
import boto3
ssm = boto3.client('ssm', region_name='us-east-2')
keys = {
    'anthropic-api-key':  'sk-ant',
    'google-api-key':     'AIza',
    'openai-api-key':     'sk-',
    'perplexity-api-key': 'pplx-',
}
all_ok = True
for param, prefix in keys.items():
    try:
        val = ssm.get_parameter(Name=f'/mlops/{param}', WithDecryption=True)['Parameter']['Value']
        ok = val.startswith(prefix) and val != f'{prefix}YOUR'
        print(f"  {'' if ok else ''} /mlops/{param}: {'present' if ok else 'PLACEHOLDER — update in SSM'}")
        if not ok:
            all_ok = False
    except Exception as e:
        print(f"  /mlops/{param}: missing ({e})")
        all_ok = False
print(f"\n  Models in use:")
print(f"    Claude:     claude-sonnet-4-6   (Investigator + Guardian)")
print(f"    Gemini:     gemini-2.5-flash    (Data Scientist)")
print(f"    OpenAI:     gpt-5.4-mini        (Operator)")
print(f"    Perplexity: sonar               (Researcher)")
PYEOF

# =============================================================================
# STEP 8: Deploy Ollama + Phi3 + ChromaDB (Day-7 agentic infrastructure)
# =============================================================================
echo ""
echo "Step 8: Deploying Ollama + ChromaDB (agentic layer)..."

# Create agents namespace
kubectl create namespace agents 2>/dev/null || echo "   namespace agents already exists"

# Deploy Ollama + ChromaDB from manifests
kubectl apply -f ~/payguard-mlops/k8s/agents/ollama.yaml
kubectl apply -f ~/payguard-mlops/k8s/agents/chromadb.yaml
echo "   Ollama + ChromaDB manifests applied"

# Wait for ChromaDB (fast — no model pull needed)
echo "   Waiting for ChromaDB..."
kubectl rollout status deployment/chromadb -n agents --timeout=3m 2>/dev/null && \
    echo "   ChromaDB ready" || echo "    ChromaDB not ready yet — continuing"

# Ollama init container pulls phi3:mini (~2.3GB) — wait up to 10 min
echo "   Waiting for Ollama + phi3:mini pull (up to 10 min)..."
OLLAMA_READY=0
for i in $(seq 1 60); do
    STATUS=$(kubectl get pod -n agents -l app=ollama \
        --no-headers 2>/dev/null | awk '{print $2}')
    if [ "$STATUS" = "1/1" ]; then
        echo "   Ollama ready (phi3:mini loaded)"
        OLLAMA_READY=1
        break
    fi
    # Show init container progress every 30s
    if [ $((i % 6)) -eq 0 ]; then
        INIT_STATUS=$(kubectl get pod -n agents -l app=ollama \
            --no-headers 2>/dev/null | awk '{print $3}')
        echo "   Ollama status: $INIT_STATUS ($i/60 × 10s)"
    fi
    sleep 10
done
if [ $OLLAMA_READY -eq 0 ]; then
    echo "    Ollama still initializing — phi3 pull may still be in progress"
    echo "      Check: kubectl logs -n agents -l app=ollama -c pull-phi3"
fi

# =============================================================================
# STEP 9: Patch ClusterIPs in mlops_agent.py
# CRITICAL: ClusterIPs change on every terraform apply.
# This step reads the new IPs from kubectl and patches the agent file.
# Without this, the agent cannot reach Redis, ChromaDB, or Ollama.
# =============================================================================
echo ""
echo "Step 9: Patching ClusterIPs in mlops_agent.py..."

REDIS_CLUSTER_IP=$(kubectl get svc redis-master -n feast \
    -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
CHROMA_CLUSTER_IP=$(kubectl get svc chromadb -n agents \
    -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
OLLAMA_CLUSTER_IP=$(kubectl get svc ollama -n agents \
    -o jsonpath='{.spec.clusterIP}' 2>/dev/null)

echo "   Redis ClusterIP:    ${REDIS_CLUSTER_IP}"
echo "   ChromaDB ClusterIP: ${CHROMA_CLUSTER_IP}"
echo "   Ollama ClusterIP:   ${OLLAMA_CLUSTER_IP}"

AGENT_FILE="/home/ec2-user/payguard-mlops/agents/mlops_agent.py"

if [ -f "$AGENT_FILE" ] && \
   [ -n "$REDIS_CLUSTER_IP" ] && \
   [ -n "$CHROMA_CLUSTER_IP" ] && \
   [ -n "$OLLAMA_CLUSTER_IP" ]; then

    python3 << PYEOF
import re

path = "$AGENT_FILE"
with open(path) as f:
    content = f.read()

redis_ip  = "${REDIS_CLUSTER_IP}"
chroma_ip = "${CHROMA_CLUSTER_IP}"
ollama_ip = "${OLLAMA_CLUSTER_IP}"

def patch_or_insert(text, var, value):
    """Replace existing line or insert after the first HOST = line."""
    pattern = rf'^{re.escape(var)}\s*=\s*"[^"]*"'
    new_line = f'{var:<18}= "{value}"'
    if re.search(pattern, text, re.MULTILINE):
        return re.sub(pattern, new_line, text, flags=re.MULTILINE)
    else:
        # Insert before REDIS_HOST as anchor
        return text.replace('REDIS_HOST', f'{new_line}\nREDIS_HOST', 1)

# Patch all three — using actual variable names from mlops_agent.py
content = re.sub(r'^REDIS_HOST\s*=\s*"[^"]*"',
    f'REDIS_HOST         = "{redis_ip}"', content, flags=re.MULTILINE)

content = patch_or_insert(content, 'CHROMA_HOST', chroma_ip)
content = patch_or_insert(content, 'OLLAMA_HOST', ollama_ip)
content = patch_or_insert(content, 'CHROMADB_HOST', chroma_ip)  # duplicate var — keep in sync

with open(path, "w") as f:
    f.write(content)

print("   ClusterIPs patched in mlops_agent.py")
PYEOF

    # Verify patches landed
    grep -E "REDIS_HOST|CHROMA_HOST|OLLAMA_HOST" "$AGENT_FILE" | head -3
else
    echo "    Could not patch ClusterIPs — missing IPs or agent file not found"
    echo "      Redis: ${REDIS_CLUSTER_IP}, ChromaDB: ${CHROMA_CLUSTER_IP}, Ollama: ${OLLAMA_CLUSTER_IP}"
fi

# =============================================================================
# STEP 10: Generate Parquet + run baseline experiment
# Day-7 fix: bucket is mlops-raw-data-YOUR_SUFFIX (not mlops-raw-data-395044244631)
# generate_parquet.py must run before baseline_experiment.py
# =============================================================================
echo ""
echo "Step 10: Generating Parquet + running baseline experiment..."

mkdir -p /home/ec2-user/payguard-mlops/ml/data

# Check if Parquet already exists (skip re-generation if present)
if [ -f "/home/ec2-user/payguard-mlops/ml/data/paysim_features.parquet" ]; then
    echo "   Parquet already exists — skipping download and generation"
else
    echo "   Downloading PaySim CSV from S3 (493MB)..."
    aws s3 cp s3://mlops-raw-data-YOUR_SUFFIX/paysim/PS_log.csv \
        /home/ec2-user/payguard-mlops/ml/data/PS_log.csv \
        --no-progress \
        --region "$REGION" && \
        echo "   CSV downloaded" || \
        echo "    CSV download failed — baseline experiment will be skipped"

    if [ -f "/home/ec2-user/payguard-mlops/ml/data/PS_log.csv" ]; then
        echo "   Generating paysim_features.parquet..."
        python3 /home/ec2-user/payguard-mlops/ml/data/generate_parquet.py && \
            echo "   Parquet generated" || \
            echo "    Parquet generation failed"
        # Remove raw CSV to save disk space (Parquet is 355MB, CSV is 493MB)
        rm -f /home/ec2-user/payguard-mlops/ml/data/PS_log.csv
        echo "   Raw CSV removed (disk space)"
    fi
fi

# Run baseline experiment (trains RF + LR, registers to MLflow)
if [ -f "/home/ec2-user/payguard-mlops/ml/data/paysim_features.parquet" ]; then
    echo "   Running baseline experiment..."
    python3 /home/ec2-user/payguard-mlops/ml/training/baseline_experiment.py \
        2>&1 | grep -E "roc_auc|Registered|Created version|complete|ERROR" | head -10
    echo "   Baseline experiment complete"

    # Promote RF to Staging so fraud-api can load it
    python3 << 'PYEOF'
import mlflow, warnings
warnings.filterwarnings("ignore")
mlflow.set_tracking_uri("http://localhost:32001")
client = mlflow.tracking.MlflowClient()
for model, version in [
    ("fraud_detection_random_forest", "1"),
    ("fraud_detection_logistic_regression", "1"),
]:
    try:
        versions = client.get_latest_versions(model)
        if versions:
            v = versions[-1].version
            client.set_registered_model_alias(model, "champion", v)
            print(f"  {model} v{v} → champion alias")
    except Exception as e:
        print(f"   {model}: {e}")
PYEOF
else
    echo "    Parquet not available — skipping baseline experiment"
    echo "      Run manually: python3 ~/payguard-mlops/ml/data/generate_parquet.py"
fi

# =============================================================================
# STEP 11: Final status
# =============================================================================
echo ""
echo "Waiting 60s for all pods to stabilize..."
sleep 60

# ── Deploy drift detector Endpoints bridge + ServiceMonitor ───────────────────
echo "Wiring drift detector → Prometheus scrape..."
HOST_IP=$(hostname -I | awk '{print $1}')

kubectl apply -f - <<EOF
---
apiVersion: v1
kind: Endpoints
metadata:
  name: drift-detector
  namespace: monitoring
  labels:
    app: drift-detector
subsets:
- addresses:
  - ip: ${HOST_IP}
  ports:
  - name: metrics
    port: 32004
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: drift-detector
  namespace: monitoring
  labels:
    app: drift-detector
spec:
  clusterIP: None
  ports:
  - name: metrics
    port: 32004
    protocol: TCP
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: drift-detector
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  selector:
    matchLabels:
      app: drift-detector
  endpoints:
  - port: metrics
    interval: 15s
    path: /metrics
EOF
echo "   Drift detector scrape target registered"

# ── Deploy MLOps alert rules ───────────────────────────────────────────────────
kubectl apply -f - <<'ALERTEOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: mlops-drift-alerts
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  groups:
  - name: mlops.drift
    interval: 30s
    rules:
    - alert: DataDriftDetected
      expr: evidently_dataset_drift_score > 0.7
      for: 1m
      labels:
        severity: warning
      annotations:
        summary: "Data drift detected"
        description: "drift_score={{ $value | humanizePercentage }}"
    - alert: FraudRateAnomaly
      expr: sum(rate(fraud_predictions_total{result="fraud"}[2m])) / sum(rate(fraud_predictions_total[2m])) > 0.30
      for: 2m
      labels:
        severity: critical
      annotations:
        summary: "Fraud rate anomaly"
        description: "fraud_rate={{ $value }}"
ALERTEOF
echo "   MLOps alert rules deployed"

# ── Autostart drift detector ──────────────────────────────────────────────────
echo "Starting drift detector..."
pkill -f drift_detector.py 2>/dev/null || true
nohup python3 /home/ec2-user/payguard-mlops/monitoring/drift_detector.py \
    > /tmp/drift_detector.log 2>&1 &
DRIFT_PID=$!
sleep 3
if kill -0 $DRIFT_PID 2>/dev/null; then
    echo "   Drift detector running (PID $DRIFT_PID)"
else
    echo "    Drift detector failed — check /tmp/drift_detector.log"
    tail -5 /tmp/drift_detector.log
fi

# ── Autostart webhook ─────────────────────────────────────────────────────────
echo "Starting MLOps webhook..."
pkill -f webhook.py 2>/dev/null || true
sleep 1
nohup python3 /home/ec2-user/payguard-mlops/agents/webhook.py \
    > /tmp/webhook.log 2>&1 &
WEBHOOK_PID=$!
sleep 3
if kill -0 $WEBHOOK_PID 2>/dev/null; then
    echo "   Webhook running (PID $WEBHOOK_PID) — http://0.0.0.0:5001"
else
    echo "    Webhook failed — check /tmp/webhook.log"
    tail -5 /tmp/webhook.log
fi

# ── Wire Alertmanager → webhook ───────────────────────────────────────────────
echo "Wiring Alertmanager → MLOps webhook..."
HOST_IP=$(hostname -I | awk '{print $1}')

cat > /tmp/alertmanager-mlops.yaml << AMEOF
global:
  resolve_timeout: 5m
inhibit_rules:
- equal: [namespace, alertname]
  source_matchers: [severity = critical]
  target_matchers: [severity =~ "warning|info"]
- equal: [namespace, alertname]
  source_matchers: [severity = warning]
  target_matchers: [severity = info]
- equal: [namespace]
  source_matchers: [alertname = InfoInhibitor]
  target_matchers: [severity = info]
- target_matchers: [alertname = InfoInhibitor]
receivers:
- name: "null"
- name: "mlops-webhook"
  webhook_configs:
  - url: "http://${HOST_IP}:5001/webhook/alert"
    send_resolved: false
    http_config:
      follow_redirects: true
    max_alerts: 10
route:
  group_by: [namespace]
  group_interval: 5m
  group_wait: 30s
  receiver: "null"
  repeat_interval: 12h
  routes:
  - matchers: [alertname = "Watchdog"]
    receiver: "null"
  - matchers:
    - alertname =~ "DataDriftDetected|FraudRateAnomaly|FraudAPIHighLatency|ModelAccuracyDegraded|FeaturePipelineStale|FraudAPIDown"
    receiver: "mlops-webhook"
    group_wait: 10s
    group_interval: 1m
    repeat_interval: 1h
templates:
- /etc/alertmanager/config/*.tmpl
AMEOF

kubectl create secret generic     alertmanager-kube-prometheus-stack-alertmanager     --from-file=alertmanager.yaml=/tmp/alertmanager-mlops.yaml     --namespace monitoring     --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart statefulset     alertmanager-kube-prometheus-stack-alertmanager -n monitoring

kubectl rollout status statefulset     alertmanager-kube-prometheus-stack-alertmanager     -n monitoring --timeout=60s 2>/dev/null

echo "   Alertmanager wired to http://${HOST_IP}:5001/webhook/alert"

# ── Deploy fraud-api Endpoints bridge (fraud_predictions_total → Prometheus) ──
echo "Wiring fraud-api → Prometheus scrape..."
FRAUD_POD_IP=$(kubectl get pod -n mlops -l app=fraud-api     -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)
if [ -n "$FRAUD_POD_IP" ]; then
    kubectl apply -f - << FRAUDEOF
---
apiVersion: v1
kind: Endpoints
metadata:
  name: fraud-api-metrics
  namespace: monitoring
  labels:
    app: fraud-api-metrics
subsets:
- addresses:
  - ip: ${FRAUD_POD_IP}
  ports:
  - name: metrics
    port: 8000
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: fraud-api-metrics
  namespace: monitoring
  labels:
    app: fraud-api-metrics
spec:
  clusterIP: None
  ports:
  - name: metrics
    port: 8000
    protocol: TCP
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: fraud-api-metrics
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: [monitoring]
  selector:
    matchLabels:
      app: fraud-api-metrics
  endpoints:
  - port: metrics
    interval: 15s
    path: /metrics
FRAUDEOF
    echo "   fraud-api-metrics scrape target registered (pod IP: ${FRAUD_POD_IP})"
else
    echo "    fraud-api pod not ready — fraud metrics may be delayed"
fi

# ── Deploy mlops-agent Endpoints bridge (webhook /metrics → Prometheus) ───────
echo "Wiring mlops-agent webhook → Prometheus scrape..."
kubectl apply -f - << AGENTEOF
---
apiVersion: v1
kind: Endpoints
metadata:
  name: mlops-agent
  namespace: monitoring
  labels:
    app: mlops-agent
subsets:
- addresses:
  - ip: ${HOST_IP}
  ports:
  - name: metrics
    port: 5001
    protocol: TCP
---
apiVersion: v1
kind: Service
metadata:
  name: mlops-agent
  namespace: monitoring
  labels:
    app: mlops-agent
spec:
  clusterIP: None
  ports:
  - name: metrics
    port: 5001
    protocol: TCP
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: mlops-agent
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: [monitoring]
  selector:
    matchLabels:
      app: mlops-agent
  endpoints:
  - port: metrics
    interval: 15s
    path: /metrics
AGENTEOF
echo "   mlops-agent scrape target registered (${HOST_IP}:5001)"

# ── Import agent Grafana dashboard ────────────────────────────────────────────
echo "Importing agent Grafana dashboard..."
DASH_FILE="/home/ec2-user/payguard-mlops/k8s/monitoring/agent-dashboard.json"
if [ -f "$DASH_FILE" ]; then
    DASH_RESULT=$(curl -s -X POST         "http://admin:YOUR_GRAFANA_PASSWORD@localhost:32000/api/dashboards/import"         -H "Content-Type: application/json"         -d "{"dashboard": $(cat $DASH_FILE), "overwrite": true, "folderId": 0}"         2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('status', d.get('message','?')))
" 2>/dev/null)
    echo "   Agent dashboard imported (${DASH_RESULT})"
else
    echo "    agent-dashboard.json not found at ${DASH_FILE}"
fi

echo ""
echo "Pod Status:"
kubectl get pods -A --no-headers | \
    grep -E "airflow|mlflow|monitoring|feast|agents" | \
    awk '{printf "%-12s %-55s %s\n", $1, $2, $4}'

echo ""
echo "=============================================="
echo "Platform deployment complete"
echo "   Grafana  → http://${PUBLIC_IP}:32000  (admin / YOUR_GRAFANA_PASSWORD)"
echo "   MLflow   → http://${PUBLIC_IP}:32001  (ready ~3 min after boot)"
echo "   Airflow  → http://${PUBLIC_IP}:32080  (admin / YOUR_AIRFLOW_PASSWORD)"
echo ""
echo "Add Loki datasource manually in Grafana:"
echo "   Connections → Data Sources → Add → Loki"
echo "   URL: http://loki.monitoring.svc.cluster.local:3100"
echo ""
echo "Redis:    ${REDIS_IP}:6379 (ClusterIP)"
echo "ChromaDB: ${CHROMA_CLUSTER_IP}:8000 (ClusterIP)"
echo "Ollama:   ${OLLAMA_CLUSTER_IP}:11434 (ClusterIP)"
echo ""
echo " Post-deploy steps:"
echo "   1. python3 ~/payguard-mlops/scripts/test_llm_apis.py"
echo "   2. cd ~/payguard-mlops/ml/features && feast apply  (if feast installed)"
echo "   3. nohup python3 ~/payguard-mlops/monitoring/drift_detector.py > /tmp/drift.log 2>&1 &"
echo "   4. python3 ~/payguard-mlops/agents/mlops_agent.py  (test agent)"
echo "=============================================="