"""
MLOps Alert Webhook — Day-8
Receives Prometheus Alertmanager POST requests and triggers
LangGraph investigations automatically.

Endpoint: POST /webhook/alert
Health:   GET  /health
Status:   GET  /status
"""

import os
import json
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
)
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webhook")

_cumulative_cost = [0.0]  # mutable container
app = Flask(__name__)

# ── Prometheus metrics (try/except handles restart re-registration) ──────────
try:
    INVESTIGATIONS_TOTAL = Counter(
        "mlops_investigations_total",
        "Total agent investigations", ["alert_name", "status"])
    INVESTIGATION_COST = Gauge(
        "mlops_investigation_cost_dollars",
        "Cost of last investigation in dollars", ["alert_name"])
    INVESTIGATION_COST_CUMULATIVE = Gauge(
        "mlops_investigation_cost_cumulative_dollars",
        "Cumulative investigation cost")
    INVESTIGATION_DURATION = Gauge(
        "mlops_investigation_duration_seconds",
        "Duration of last investigation", ["alert_name"])
    TRIAGE_PATH = Counter(
        "mlops_triage_path_total",
        "Triage path taken", ["path"])
    PHI3_ACCURACY = Gauge(
        "mlops_phi3_accuracy",
        "Phi3 lifetime accuracy (0.0-1.0)")
    CHROMADB_INCIDENTS = Gauge(
        "mlops_chromadb_incidents_stored",
        "Number of incidents in ChromaDB")
    GATE_PASS = Counter(
        "mlops_gate_pass_total",
        "Confidence gate pass/fail counts", ["gate", "verdict"])
except ValueError:
    # Already registered from previous import — retrieve from registry
    from prometheus_client import REGISTRY as _R
    INVESTIGATIONS_TOTAL        = _R._names_to_collectors.get("mlops_investigations_total")
    INVESTIGATION_COST          = _R._names_to_collectors.get("mlops_investigation_cost_dollars")
    INVESTIGATION_COST_CUMULATIVE = _R._names_to_collectors.get("mlops_investigation_cost_cumulative_dollars")
    INVESTIGATION_DURATION      = _R._names_to_collectors.get("mlops_investigation_duration_seconds")
    TRIAGE_PATH                 = _R._names_to_collectors.get("mlops_triage_path_total")
    PHI3_ACCURACY               = _R._names_to_collectors.get("mlops_phi3_accuracy")
    CHROMADB_INCIDENTS          = _R._names_to_collectors.get("mlops_chromadb_incidents_stored")
    GATE_PASS                   = _R._names_to_collectors.get("mlops_gate_pass_total")

# ── Prometheus metrics (try/except handles restart re-registration) ──────────
try:
    INVESTIGATIONS_TOTAL = Counter(
        "mlops_investigations_total",
        "Total agent investigations", ["alert_name", "status"])
    INVESTIGATION_COST = Gauge(
        "mlops_investigation_cost_dollars",
        "Cost of last investigation in dollars", ["alert_name"])
    INVESTIGATION_COST_CUMULATIVE = Gauge(
        "mlops_investigation_cost_cumulative_dollars",
        "Cumulative investigation cost")
    INVESTIGATION_DURATION = Gauge(
        "mlops_investigation_duration_seconds",
        "Duration of last investigation", ["alert_name"])
    TRIAGE_PATH = Counter(
        "mlops_triage_path_total",
        "Triage path taken", ["path"])
    PHI3_ACCURACY = Gauge(
        "mlops_phi3_accuracy",
        "Phi3 lifetime accuracy (0.0-1.0)")
    CHROMADB_INCIDENTS = Gauge(
        "mlops_chromadb_incidents_stored",
        "Number of incidents in ChromaDB")
    GATE_PASS = Counter(
        "mlops_gate_pass_total",
        "Confidence gate pass/fail counts", ["gate", "verdict"])
except ValueError:
    # Already registered from previous import — retrieve from registry
    from prometheus_client import REGISTRY as _R
    INVESTIGATIONS_TOTAL        = _R._names_to_collectors.get("mlops_investigations_total")
    INVESTIGATION_COST          = _R._names_to_collectors.get("mlops_investigation_cost_dollars")
    INVESTIGATION_COST_CUMULATIVE = _R._names_to_collectors.get("mlops_investigation_cost_cumulative_dollars")
    INVESTIGATION_DURATION      = _R._names_to_collectors.get("mlops_investigation_duration_seconds")
    TRIAGE_PATH                 = _R._names_to_collectors.get("mlops_triage_path_total")
    PHI3_ACCURACY               = _R._names_to_collectors.get("mlops_phi3_accuracy")
    CHROMADB_INCIDENTS          = _R._names_to_collectors.get("mlops_chromadb_incidents_stored")
    GATE_PASS                   = _R._names_to_collectors.get("mlops_gate_pass_total")

# ── In-memory investigation tracker ──────────────────────────────────────────
# Tracks active and completed investigations for /status endpoint
investigations: dict = {}   # fingerprint → {status, started_at, alert_name}
investigations_lock = threading.Lock()

# ── Redis dedup (same logic as alert_intake node) ─────────────────────────────
def get_redis():
    try:
        import redis as redis_lib
        r = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "10.43.86.87"),
            port=6379,
            decode_responses=True,
            socket_timeout=2,
        )
        r.ping()
        return r
    except Exception as e:
        log.warning(f"Redis unavailable: {e}")
        return None

def fingerprint(alert_name: str, severity: str) -> str:
    return hashlib.md5(f"{alert_name}:{severity}".encode()).hexdigest()[:16]

def is_duplicate(fp: str) -> bool:
    r = get_redis()
    if r is None:
        return False
    return r.exists(f"dedup:{fp}") == 1

def set_dedup(fp: str, ttl: int = 300):
    r = get_redis()
    if r:
        r.setex(f"dedup:{fp}", ttl, "1")

# ── Investigation runner (background thread) ──────────────────────────────────
def run_investigation(alert_name: str, severity: str, fp: str):
    """Runs in a background thread — calls the LangGraph agent."""
    log.info(f"Starting investigation: {alert_name} [{severity}]")

    with investigations_lock:
        investigations[fp] = {
            "status": "running",
            "alert_name": alert_name,
            "severity": severity,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
        }

    try:
        # Import agent here (inside thread) to avoid circular import at module load
        import sys
        sys.path.insert(0, "/home/ec2-user/payguard-mlops/agents")
        from mlops_agent import run_investigation as run_agent

        result = run_agent(alert_name, severity)

        with investigations_lock:
            investigations[fp]["status"] = "completed"
            investigations[fp]["finished_at"] = datetime.now(timezone.utc).isoformat()
            investigations[fp]["result"] = str(result)[:500]

        log.info(f"Investigation complete: {alert_name}")

        # ── Record Prometheus metrics ─────────────────────────────────────────
        try:
            INVESTIGATIONS_TOTAL.labels(
                alert_name=alert_name, status="completed").inc()

            # cost — directly from AgentState key
            cost = float(result.get("total_cost", 0)) if isinstance(result, dict) else 0.0
            INVESTIGATION_COST.labels(alert_name=alert_name).set(cost)
            _cumulative_cost[0] += cost
            INVESTIGATION_COST_CUMULATIVE.set(_cumulative_cost[0])
            log.info(f"   Cost: ${cost:.4f} | cumulative: ${_cumulative_cost[0]:.4f}")

            # triage path
            triage_path = (result.get("triage_path", "UNKNOWN")
                           if isinstance(result, dict) else "UNKNOWN") or "UNKNOWN"
            TRIAGE_PATH.labels(path=triage_path).inc()

            # phi3 accuracy — from Redis retrospective log
            _r = get_redis()
            if _r:
                import json as _json
                _entries = [_json.loads(x) for x in
                            _r.lrange("triage_retrospective_log", 0, -1)]
                _correct = sum(1 for e in _entries if e.get("agreed", False))
                _total   = len(_entries)
                if _total > 0:
                    PHI3_ACCURACY.set(_correct / _total)
                CHROMADB_INCIDENTS.set(_total)

            # gate verdicts
            if isinstance(result, dict):
                gate1 = "PASS" if float(result.get("triage_confidence", 0)) >= 0.70 else "FAIL"
                inv_rep = result.get("investigator_report", {})
                synth_conf = float(inv_rep.get("confidence", 0)) if inv_rep else 0.0
                gate2 = "PASS" if synth_conf >= 0.70 else "FAIL"
                pfm = result.get("post_fix_monitor_result", {})
                gate3 = str(pfm.get("verdict", "SKIPPED")) if pfm else "SKIPPED"
                for gate, verdict in [("gate1", gate1), ("gate2", gate2), ("gate3", gate3)]:
                    if verdict:
                        GATE_PASS.labels(gate=gate, verdict=verdict).inc()

            # duration
            started = investigations.get(fp, {}).get("started_at", "")
            if started:
                t0  = datetime.fromisoformat(started)
                dur = (datetime.now(timezone.utc) - t0).total_seconds()
                INVESTIGATION_DURATION.labels(alert_name=alert_name).set(dur)

        except Exception as me:
            log.warning(f"Metrics update failed: {me}")

    except Exception as e:
        log.error(f"Investigation failed: {e}", exc_info=True)
        with investigations_lock:
            investigations[fp]["status"] = "failed"
            investigations[fp]["finished_at"] = datetime.now(timezone.utc).isoformat()
            investigations[fp]["result"] = str(e)[:300]
        INVESTIGATIONS_TOTAL.labels(
            alert_name=alert_name, status="failed").inc()
        INVESTIGATIONS_TOTAL.labels(
            alert_name=alert_name, status="failed").inc()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus scrape endpoint — exposes agent investigation metrics."""
    from flask import Response
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/status", methods=["GET"])
def status():
    with investigations_lock:
        summary = {
            "total":     len(investigations),
            "running":   sum(1 for v in investigations.values() if v["status"] == "running"),
            "completed": sum(1 for v in investigations.values() if v["status"] == "completed"),
            "failed":    sum(1 for v in investigations.values() if v["status"] == "failed"),
            "investigations": dict(list(investigations.items())[-10:]),  # last 10
        }
    return jsonify(summary), 200


@app.route("/webhook/alert", methods=["POST"])
def receive_alert():
    """
    Alertmanager payload format:
    {
      "alerts": [
        {
          "status": "firing",
          "labels": {"alertname": "DataDriftDetected", "severity": "warning"},
          "annotations": {"summary": "...", "description": "..."}
        }
      ]
    }
    """
    payload = request.get_json(silent=True)
    if not payload:
        log.warning("Empty or non-JSON payload received")
        return jsonify({"error": "invalid payload"}), 400

    alerts = payload.get("alerts", [])
    log.info(f"Received {len(alerts)} alert(s) from Alertmanager")

    accepted = []
    skipped  = []

    for alert in alerts:
        if alert.get("status") != "firing":
            skipped.append({"reason": "not_firing", "alert": alert.get("labels", {})})
            continue

        labels    = alert.get("labels", {})
        alert_name = labels.get("alertname", "UnknownAlert")
        severity   = labels.get("severity", "warning")
        fp         = fingerprint(alert_name, severity)

        log.info(f"   Alert: {alert_name} [{severity}]  fp={fp}")

        # Redis dedup check
        if is_duplicate(fp):
            log.info(f"   DUPLICATE — already investigating {alert_name}, skipping")
            skipped.append({"reason": "duplicate", "alert_name": alert_name, "fp": fp})
            continue

        # Check if already running in memory
        with investigations_lock:
            if fp in investigations and investigations[fp]["status"] == "running":
                skipped.append({"reason": "already_running", "alert_name": alert_name})
                continue

        # Set dedup lock
        set_dedup(fp, ttl=300)

        # Fire background investigation
        t = threading.Thread(
            target=run_investigation,
            args=(alert_name, severity, fp),
            daemon=True,
            name=f"inv-{fp[:8]}",
        )
        t.start()

        accepted.append({"alert_name": alert_name, "severity": severity, "fp": fp})
        log.info(f"   Investigation started (thread: inv-{fp[:8]})")

    return jsonify({
        "status":   "accepted",
        "accepted": accepted,
        "skipped":  skipped,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 202


@app.route("/webhook/test", methods=["POST"])
def test_webhook():
    """
    Fire a synthetic alert without going through Alertmanager.
    POST /webhook/test
    Body: {"alert_name": "DataDriftDetected", "severity": "warning"}
    """
    body = request.get_json(silent=True) or {}
    alert_name = body.get("alert_name", "DataDriftDetected")
    severity   = body.get("severity", "warning")
    fp         = fingerprint(alert_name, severity)

    log.info(f"TEST alert: {alert_name} [{severity}]")

    t = threading.Thread(
        target=run_investigation,
        args=(alert_name, severity, fp),
        daemon=True,
        name=f"test-{fp[:8]}",
    )
    t.start()

    return jsonify({
        "status":     "test_started",
        "alert_name": alert_name,
        "severity":   severity,
        "fp":         fp,
    }), 202


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 5001))
    log.info(f"MLOps webhook starting on port {port}")
    log.info(f"   POST /webhook/alert  — Alertmanager endpoint")
    log.info(f"   POST /webhook/test   — Manual test trigger")
    log.info(f"   GET  /health         — Liveness check")
    log.info(f"   GET  /status         — Investigation tracker")
    app.run(host="0.0.0.0", port=port, threaded=True)