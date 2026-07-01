"""
PayStream: Synthetic Payment Transaction Simulator
=====================================================
Generates realistic PaySim-style transactions and scores each one
against the FastAPI fraud detection endpoint in real time.

Traffic Controller modes (set via /mode endpoint or MODE env var):
  normal   - 98% legit, 2% fraud   (baseline)
  drift    - shifts transaction type distribution toward TRANSFER/CASH_OUT
  attack   - burst of obvious fraud patterns (high amount, empty accounts)
  degraded - injects missing/bad features to test data quality gates

Run: python paystream.py
UI:  http://<host>:8001  (traffic controller dashboard)
"""

import asyncio
import logging
import os
import random
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import (
    Counter, Histogram, Gauge, generate_latest,
    CONTENT_TYPE_LATEST, REGISTRY
)
from fastapi.responses import PlainTextResponse

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("paystream")

# =============================================================================
# PROMETHEUS METRICS
# =============================================================================
def _metric(cls, name, desc, labels=None, **kw):
    try:
        return cls(name, desc, labels, **kw) if labels else cls(name, desc, **kw)
    except ValueError:
        return REGISTRY._names_to_collectors.get(name) or \
               REGISTRY._names_to_collectors.get(name + "_total")

TXN_GENERATED   = _metric(Counter, "paystream_transactions_total",
                           "Transactions generated", ["mode", "type"])
TXN_FRAUD_SENT  = _metric(Counter, "paystream_fraud_sent_total",
                           "Fraud transactions sent")
TXN_LEGIT_SENT  = _metric(Counter, "paystream_legit_sent_total",
                           "Legit transactions sent")
API_LATENCY     = _metric(Histogram, "paystream_api_latency_seconds",
                           "FastAPI call latency",
                           buckets=[0.005,0.01,0.025,0.05,0.1,0.25,0.5,1.0])
FRAUD_DETECTED  = _metric(Counter, "paystream_fraud_detected_total",
                           "Transactions flagged as fraud by API")
FRAUD_MISSED    = _metric(Counter, "paystream_fraud_missed_total",
                           "Known fraud NOT flagged by API")
FALSE_POSITIVES = _metric(Counter, "paystream_false_positives_total",
                           "Legit transactions flagged as fraud")
CURRENT_RPS     = _metric(Gauge, "paystream_current_rps",
                           "Current transactions per second")
FRAUD_RATE      = _metric(Gauge, "paystream_fraud_rate_percent",
                           "Current fraud injection rate percent")
MODE_GAUGE      = _metric(Gauge, "paystream_mode",
                           "Current traffic mode (0=normal,1=drift,2=attack,3=degraded)",
                           ["mode_name"])

# =============================================================================
# TRAFFIC CONTROLLER STATE
# =============================================================================
@dataclass
class TrafficState:
    mode: str          = "normal"
    rps: float         = 2.0       # transactions per second
    running: bool      = False
    total_sent: int    = 0
    fraud_sent: int    = 0
    fraud_detected: int = 0
    false_positives: int = 0
    errors: int        = 0
    start_time: float  = field(default_factory=time.time)
    history: deque     = field(default_factory=lambda: deque(maxlen=25))

state = TrafficState()

MODE_INFO = {
    "normal":   {"label": "Normal",   "desc": "Baseline traffic, 98% legit payments, 2% fraud. What a healthy day looks like."},
    "drift":    {"label": "Drift",    "desc": "Shifts the transaction mix toward larger TRANSFER/CASH_OUT amounts, simulating a change in customer behaviour. Tests whether the model's fraud rate stays stable under distribution shift."},
    "attack":   {"label": "Attack",   "desc": "Bursts of obvious fraud: full-balance drains to fresh zero-balance accounts. Fraud rate should spike sharply and detection rate should stay high."},
    "degraded": {"label": "Degraded", "desc": "Injects edge-case data (zero amounts, extreme values) to test how the API handles malformed/unusual inputs, not fraud detection accuracy."},
}

# =============================================================================
# TRANSACTION GENERATORS
# Each mode generates transactions with different statistical properties
# to trigger different platform responses.
# =============================================================================
FRAUD_API_URL = os.getenv("FRAUD_API_URL", "http://fraud-api:8000")

def _account_id(prefix="C"):
    return f"{prefix}{random.randint(1_000_000, 9_999_999)}"

def make_normal_transaction() -> dict:
    """98% legit, 2% fraud, baseline PaySim distribution"""
    is_fraud = random.random() < 0.02
    txn_type = random.choice(["PAYMENT","PAYMENT","PAYMENT","TRANSFER","CASH_OUT","DEBIT"])

    if is_fraud and txn_type in ("TRANSFER", "CASH_OUT"):
        amount = random.uniform(100_000, 500_000)
        old_bal = amount
        new_bal = 0.0
    else:
        amount  = random.lognormvariate(9, 2)
        old_bal = random.uniform(amount, amount * 3)
        new_bal = old_bal - amount

    return {
        "transaction_id":  str(uuid.uuid4()),
        "amount":          round(amount, 2),
        "oldbalanceOrg":   round(old_bal, 2),
        "newbalanceOrig":  round(max(new_bal, 0), 2),
        "oldbalanceDest":  round(random.uniform(0, 100_000), 2),
        "newbalanceDest":  round(random.uniform(0, 200_000), 2),
        "transaction_type": txn_type,
        "step":            int(time.time() / 3600) % 744,
        "_is_fraud":       is_fraud,  # ground truth (stripped before API call)
    }

def make_drift_transaction() -> dict:
    """
    Drift mode: shifts distribution toward TRANSFER/CASH_OUT.
    Simulates a change in merchant category mix or customer behaviour.
    Evidently AI should detect this distribution shift and trigger retraining.
    """
    is_fraud = random.random() < 0.05   # slightly elevated fraud rate
    # Heavy skew toward fraud-prone transaction types
    txn_type = random.choice(["TRANSFER","TRANSFER","TRANSFER","CASH_OUT","CASH_OUT","PAYMENT"])

    amount  = random.uniform(50_000, 400_000)  # larger amounts during drift
    old_bal = random.uniform(amount * 0.8, amount * 1.5)
    new_bal = old_bal - amount if not is_fraud else 0.0

    return {
        "transaction_id":  str(uuid.uuid4()),
        "amount":          round(amount, 2),
        "oldbalanceOrg":   round(old_bal, 2),
        "newbalanceOrig":  round(max(new_bal, 0), 2),
        "oldbalanceDest":  0.0,
        "newbalanceDest":  round(amount * random.uniform(0.5, 1.0), 2),
        "transaction_type": txn_type,
        "step":            int(time.time() / 3600) % 744,
        "_is_fraud":       is_fraud,
    }

def make_attack_transaction() -> dict:
    """
    Attack mode: burst of obvious fraud patterns.
    All transactions drain the source account via TRANSFER to a fresh
    zero-balance destination, the clearest fraud signal in PaySim.
    Grafana fraud rate panel should spike visibly.
    """
    amount = random.uniform(200_000, 800_000)
    return {
        "transaction_id":  str(uuid.uuid4()),
        "amount":          round(amount, 2),
        "oldbalanceOrg":   round(amount, 2),   # account exactly matches amount
        "newbalanceOrig":  0.0,                 # drained to zero
        "oldbalanceDest":  0.0,                 # fresh destination account
        "newbalanceDest":  0.0,                 # dest doesn't update (fraud signal)
        "transaction_type": "TRANSFER",
        "step":            int(time.time() / 3600) % 744,
        "_is_fraud":       True,
    }

def make_degraded_transaction() -> dict:
    """
    Degraded mode: injects edge cases, zero amounts, extreme values,
    unusual balance states. Tests data quality checks and model robustness.
    """
    degradation = random.choice(["zero_amount", "extreme_amount",
                                  "negative_balance", "normal"])
    if degradation == "zero_amount":
        amount, old_bal = 0.01, 1000.0
    elif degradation == "extreme_amount":
        amount, old_bal = 9_999_999.0, 9_999_999.0
    elif degradation == "negative_balance":
        amount, old_bal = 5000.0, 0.0
    else:
        amount  = random.lognormvariate(9, 2)
        old_bal = random.uniform(amount, amount * 3)

    return {
        "transaction_id":  str(uuid.uuid4()),
        "amount":          round(amount, 2),
        "oldbalanceOrg":   round(old_bal, 2),
        "newbalanceOrig":  round(max(old_bal - amount, 0), 2),
        "oldbalanceDest":  0.0,
        "newbalanceDest":  0.0,
        "transaction_type": random.choice(["TRANSFER","CASH_OUT","PAYMENT"]),
        "step":            int(time.time() / 3600) % 744,
        "_is_fraud":       False,
    }

GENERATORS = {
    "normal":   make_normal_transaction,
    "drift":    make_drift_transaction,
    "attack":   make_attack_transaction,
    "degraded": make_degraded_transaction,
}

MODE_VALUES = {"normal": 0, "drift": 1, "attack": 2, "degraded": 3}

# =============================================================================
# TRANSACTION SENDER
# Calls the FastAPI /predict endpoint and updates Prometheus metrics.
# =============================================================================
async def send_transaction(client: httpx.AsyncClient):
    generator = GENERATORS.get(state.mode, make_normal_transaction)
    txn = generator()
    ground_truth = txn.pop("_is_fraud", False)
    txn_type     = txn["transaction_type"]

    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{FRAUD_API_URL}/predict",
            json=txn,
            timeout=5.0
        )
        latency = time.perf_counter() - start

        API_LATENCY.observe(latency)
        TXN_GENERATED.labels(mode=state.mode, type=txn_type).inc()
        state.total_sent += 1

        if resp.status_code == 200:
            result = resp.json()
            predicted_fraud = result.get("is_fraud", False)
            outcome = "correct"

            if ground_truth:
                state.fraud_sent += 1
                TXN_FRAUD_SENT.inc()
                if predicted_fraud:
                    state.fraud_detected += 1
                    FRAUD_DETECTED.inc()
                else:
                    FRAUD_MISSED.inc()
                    outcome = "missed_fraud"
                    log.warning(f"MISSED FRAUD: {txn['transaction_id']} "
                                f"prob={result.get('fraud_probability',0):.3f}")
            else:
                TXN_LEGIT_SENT.inc()
                if predicted_fraud:
                    state.false_positives += 1
                    FALSE_POSITIVES.inc()
                    outcome = "false_positive"

            state.history.appendleft({
                "transaction_id":  txn["transaction_id"],
                "amount":          txn["amount"],
                "type":            txn_type,
                "ground_truth":    ground_truth,
                "predicted_fraud": predicted_fraud,
                "fraud_probability": result.get("fraud_probability", 0),
                "outcome":         outcome,
                "latency_ms":      round(latency * 1000, 2),
                "mode":            state.mode,
                "timestamp":       time.time(),
            })
        else:
            state.errors += 1
            log.error(f"API error {resp.status_code}: {txn['transaction_id']}")

    except Exception as e:
        state.errors += 1
        log.error(f"Send error: {e}")

# =============================================================================
# TRAFFIC LOOP: runs as a background asyncio task
# =============================================================================
async def traffic_loop():
    log.info(f"Traffic loop starting, mode={state.mode} rps={state.rps}")
    async with httpx.AsyncClient() as client:
        while state.running:
            await send_transaction(client)
            CURRENT_RPS.set(state.rps)
            # Update mode gauge
            for m, v in MODE_VALUES.items():
                MODE_GAUGE.labels(mode_name=m).set(1 if m == state.mode else 0)
            await asyncio.sleep(1.0 / state.rps)

_traffic_task: Optional[asyncio.Task] = None

# =============================================================================
# FASTAPI APP: Traffic Controller UI + API
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _traffic_task
    state.running = True
    _traffic_task = asyncio.create_task(traffic_loop())
    log.info("PayStream started")
    yield
    state.running = False
    if _traffic_task:
        _traffic_task.cancel()
    log.info("PayStream stopped")

app = FastAPI(title="PayStream Traffic Controller", lifespan=lifespan)

MODE_COLORS = {"normal": "#22c55e", "drift": "#f59e0b", "attack": "#ef4444", "degraded": "#64748b"}
OUTCOME_STYLE = {
    "correct":        ("#22c55e", "CORRECT"),
    "missed_fraud":   ("#ef4444", "MISSED FRAUD"),
    "false_positive": ("#f59e0b", "FALSE POSITIVE"),
}

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Traffic Controller dashboard, static shell; all live data loads via JS from /status"""
    import json as _json
    mode_info_json = _json.dumps(MODE_INFO)
    mode_colors_json = _json.dumps(MODE_COLORS)

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>PayStream | PayGuard Traffic Generator</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: 'Inter', system-ui, sans-serif; background: #0a0f1a; color: #f1f5f9;
            margin: 0; padding: 0 0 3rem 0; }}
    a {{ color: #60a5fa; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    header {{ background: #0d1424; border-bottom: 1px solid #1e293b; padding: 0 2rem; height: 64px;
              display: flex; align-items: center; gap: 1rem; position: sticky; top: 0; z-index: 10; }}
    .logo {{ width: 36px; height: 36px; background: #ef4444; border-radius: 8px; display: flex;
             align-items: center; justify-content: center; flex-shrink: 0; }}
    .brand {{ font-size: 1.2rem; font-weight: 700; letter-spacing: -0.5px; }}
    .brand .accent {{ color: #ef4444; }}
    .brand-sub {{ color: #64748b; font-size: 0.8rem; margin-left: 10px; border-left: 1px solid #1e293b; padding-left: 10px; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 2rem; }}
    .card {{ background: #0d1424; border: 1px solid #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }}
    .intro {{ color: #94a3b8; line-height: 1.7; font-size: 0.9rem; }}
    .intro strong {{ color: #e2e8f0; }}
    h2 {{ color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px;
          margin: 0 0 1rem 0; font-weight: 600; }}
    .status-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 1.5rem; flex-wrap: wrap; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; animation: pulse 1.8s ease-out infinite; }}
    @keyframes pulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0.55); }}
      70% {{ box-shadow: 0 0 0 8px rgba(34,197,94,0); }}
      100% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0); }}
    }}
    .badge {{ padding: 4px 14px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.5px; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
    .stat-val {{ font-size: 1.75rem; font-weight: 700; color: #60a5fa; }}
    .stat-lbl {{ font-size: 0.7rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 6px; line-height: 1.4; }}
    .mode-desc {{ color: #94a3b8; font-size: 0.85rem; border-left: 3px solid #ef4444; padding: 10px 14px;
                  background: #0a0f1a; border-radius: 6px; margin-top: 0.75rem; }}
    .btn-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 0.75rem; }}
    .mode-btn {{ padding: 10px 20px; border-radius: 8px; border: 1px solid #1e293b; cursor: pointer;
                 font-weight: 700; font-size: 0.8rem; letter-spacing: 0.5px; background: transparent; color: #94a3b8;
                 transition: all 0.15s; }}
    .mode-btn.active {{ color: #0a0f1a; }}
    .rps-row {{ display: flex; align-items: center; gap: 10px; margin-top: 0.75rem; }}
    input[type=number] {{ background: #0a0f1a; border: 1px solid #1e293b; color: #e2e8f0; padding: 8px 12px;
                           border-radius: 6px; width: 90px; font-size: 0.875rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
    th {{ text-align: left; color: #475569; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px;
          padding: 8px 10px; border-bottom: 1px solid #1e293b; }}
    td {{ padding: 7px 10px; border-bottom: 1px solid #0f172a; font-family: monospace; }}
    tr.flash td {{ animation: flash 1.4s ease-out; }}
    @keyframes flash {{ from {{ background: rgba(96,165,250,0.2); }} to {{ background: transparent; }} }}
    footer {{ margin-top: 2rem; color: #475569; font-size: 0.75rem; text-align: center; }}
    footer a {{ color: #475569; }}
    .links {{ color: #475569; font-size: 0.75rem; margin-top: 1rem; }}
  </style>
</head>
<body>
  <header>
    <div class="logo">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 1L3 5v6c0 5.25 3.75 10.15 9 11.35C17.25 21.15 21 16.25 21 11V5L12 1z"/>
        <polyline points="9 12 11 14 15 10"/>
      </svg>
    </div>
    <span class="brand">Pay<span class="accent">Guard</span></span>
    <span class="brand-sub">PayStream: traffic generator</span>
    <a href="http://localhost:8023" style="margin-left:auto;font-size:0.8rem">← Back to PayGuard dashboard</a>
  </header>

  <main>
    <div class="card intro">
      This page is <strong>not</strong> the fraud-detection UI, that's the
      <a href="http://localhost:8023">PayGuard dashboard</a>. This is the <strong>traffic generator</strong>
      feeding it fake transactions: it invents a transaction, sends it to the Fraud API for scoring, and
      records whether the model got it right. Change the mode below to control what kind of traffic gets
      generated, and watch the log to see each transaction and its outcome as it happens.
    </div>

    <div class="card">
      <div class="status-row">
        <div class="dot" id="live-dot" style="background:#22c55e"></div>
        <span id="mode-badge" class="badge">—</span>
        <span style="color:#64748b;font-size:0.85rem">RPS: <strong id="rps-val" style="color:#e2e8f0">—</strong></span>
        <span style="color:#64748b;font-size:0.85rem">· Uptime: <strong id="uptime-val" style="color:#e2e8f0">—</strong></span>
      </div>
      <div id="mode-desc" class="mode-desc">Loading…</div>
    </div>

    <div class="kpi-grid">
      <div class="card"><div class="stat-val" id="stat-total">—</div><div class="stat-lbl">Total Sent</div></div>
      <div class="card"><div class="stat-val" id="stat-fraud-rate" style="color:#ef4444">—</div><div class="stat-lbl">Fraud Rate<br>(% actually fraud)</div></div>
      <div class="card"><div class="stat-val" id="stat-detect-rate" style="color:#22c55e">—</div><div class="stat-lbl">Detection Rate<br>(% caught)</div></div>
      <div class="card"><div class="stat-val" id="stat-fp" style="color:#f59e0b">—</div><div class="stat-lbl">False Positives</div></div>
      <div class="card"><div class="stat-val" id="stat-errors" style="color:#a78bfa">—</div><div class="stat-lbl">API Errors</div></div>
    </div>

    <div class="card">
      <h2>Traffic Mode: click to change what kind of transactions are generated</h2>
      <div class="btn-row" id="mode-buttons"></div>
      <h2 style="margin-top:1.5rem">Speed</h2>
      <div class="rps-row">
        <input type="number" id="rps-input" min="0.1" max="20" step="0.5" value="2">
        <button class="mode-btn" style="background:#3b82f6;color:#fff;border-color:#3b82f6" onclick="setRps()">Set RPS</button>
      </div>
    </div>

    <div class="card">
      <h2><span id="live-dot2" class="dot" style="display:inline-block;background:#22c55e;width:6px;height:6px;margin-right:6px"></span>Live Transaction Log</h2>
      <table>
        <thead><tr><th>Time</th><th>Type</th><th>Amount</th><th>Ground Truth</th><th>Fraud Prob</th><th>Outcome</th><th>Latency</th></tr></thead>
        <tbody id="log-body"><tr><td colspan="7" style="color:#475569">Loading…</td></tr></tbody>
      </table>
    </div>

    <div class="links">
      <a href="/metrics">Prometheus metrics</a> &nbsp;|&nbsp;
      <a href="/status">JSON status</a> &nbsp;|&nbsp;
      <a href="http://localhost:8020/docs">Fraud API docs</a> &nbsp;|&nbsp;
      <a href="http://localhost:3100/d/payguard-fraud-api">Grafana dashboard</a>
    </div>

    <footer>
      Rayen Lassoued | <a href="https://github.com/Hamilas" target="_blank">github.com/Hamilas</a> |
      <a href="https://www.linkedin.com/in/lassoued-rayen/" target="_blank">https://www.linkedin.com/in/lassoued-rayen/</a>
    </footer>
  </main>

  <script>
    const MODE_INFO = {mode_info_json};
    const MODE_COLORS = {mode_colors_json};
    let seenIds = new Set();
    let currentMode = null;

    function renderModeButtons() {{
      const container = document.getElementById('mode-buttons');
      container.innerHTML = '';
      for (const [key, info] of Object.entries(MODE_INFO)) {{
        const btn = document.createElement('button');
        btn.className = 'mode-btn' + (key === currentMode ? ' active' : '');
        btn.textContent = info.label;
        btn.style.borderColor = MODE_COLORS[key];
        if (key === currentMode) btn.style.background = MODE_COLORS[key];
        btn.onclick = () => setMode(key);
        container.appendChild(btn);
      }}
    }}

    async function setMode(mode) {{
      await fetch(`/mode?mode=${{mode}}`, {{ method: 'POST' }});
      refresh();
    }}

    async function setRps() {{
      const rps = document.getElementById('rps-input').value;
      await fetch(`/rps?rps=${{rps}}`, {{ method: 'POST' }});
      refresh();
    }}

    async function refresh() {{
      try {{
        const res = await fetch('/status');
        const s = await res.json();
        currentMode = s.mode;

        const color = MODE_COLORS[s.mode] || '#64748b';
        const badge = document.getElementById('mode-badge');
        badge.textContent = s.mode.toUpperCase();
        badge.style.background = color;
        badge.style.color = '#0a0f1a';
        document.getElementById('mode-desc').textContent = (MODE_INFO[s.mode] || {{}}).desc || '';
        document.getElementById('rps-val').textContent = s.rps;
        document.getElementById('rps-input').value = s.rps;
        document.getElementById('uptime-val').textContent = Math.round(s.uptime_seconds) + 's';

        const fraudRate = s.total_sent > 0 ? (s.fraud_sent / s.total_sent * 100) : 0;
        const detectRate = s.fraud_sent > 0 ? (s.fraud_detected / s.fraud_sent * 100) : 0;
        document.getElementById('stat-total').textContent = s.total_sent.toLocaleString();
        document.getElementById('stat-fraud-rate').textContent = fraudRate.toFixed(1) + '%';
        document.getElementById('stat-detect-rate').textContent = detectRate.toFixed(1) + '%';
        document.getElementById('stat-fp').textContent = s.false_positives;
        document.getElementById('stat-errors').textContent = s.errors;

        renderModeButtons();

        const outcomeStyle = {{ correct: ['#22c55e','CORRECT'], missed_fraud: ['#ef4444','MISSED FRAUD'], false_positive: ['#f59e0b','FALSE POSITIVE'] }};
        const tbody = document.getElementById('log-body');
        if (!s.recent_history || s.recent_history.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="7" style="color:#475569">No transactions yet, waiting for traffic loop…</td></tr>';
        }} else {{
          tbody.innerHTML = s.recent_history.map(h => {{
            const [color, label] = outcomeStyle[h.outcome] || ['#8694B0', h.outcome];
            const isNew = !seenIds.has(h.transaction_id);
            seenIds.add(h.transaction_id);
            const t = new Date(h.timestamp * 1000).toLocaleTimeString();
            return `<tr class="${{isNew ? 'flash' : ''}}">
              <td>${{t}}</td><td>${{h.type}}</td><td>$${{h.amount.toLocaleString(undefined,{{minimumFractionDigits:2}})}}</td>
              <td>${{h.ground_truth ? 'FRAUD' : 'legit'}}</td><td>${{(h.fraud_probability*100).toFixed(1)}}%</td>
              <td style="color:${{color}};font-weight:700">${{label}}</td><td>${{h.latency_ms.toFixed(1)}}ms</td>
            </tr>`;
          }}).join('');
        }}
      }} catch (e) {{
        document.getElementById('mode-badge').textContent = 'OFFLINE';
      }}
    }}

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>"""

@app.post("/mode")
async def set_mode(mode: str = "normal"):
    if mode not in GENERATORS:
        return JSONResponse({"error": f"Unknown mode: {mode}"}, status_code=400)
    old_mode = state.mode
    state.mode = mode
    log.info(f"Mode changed: {old_mode} → {mode}")
    FRAUD_RATE.set({"normal": 2, "drift": 5, "attack": 95, "degraded": 0}[mode])
    return JSONResponse({"mode": mode, "previous": old_mode})

@app.post("/rps")
async def set_rps(rps: float = 2.0):
    rps = max(0.1, min(20.0, rps))
    state.rps = rps
    CURRENT_RPS.set(rps)
    return JSONResponse({"rps": rps})

@app.get("/status")
async def status():
    uptime = time.time() - state.start_time
    return {
        "mode":           state.mode,
        "rps":            state.rps,
        "running":        state.running,
        "total_sent":     state.total_sent,
        "fraud_sent":     state.fraud_sent,
        "fraud_detected": state.fraud_detected,
        "false_positives": state.false_positives,
        "errors":         state.errors,
        "uptime_seconds": round(uptime, 1),
        "fraud_api_url":  FRAUD_API_URL,
        "recent_history": list(state.history),
    }

@app.get("/metrics")
async def metrics():
    return PlainTextResponse(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST
    )

if __name__ == "__main__":
    uvicorn.run("paystream:app", host="0.0.0.0", port=8001,
                workers=1, log_level="info")
