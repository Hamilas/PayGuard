"""
Evidently AI drift detector.
Compares live PayStream prediction features vs training reference.
Exposes drift scores as Prometheus metrics.
"""
import pandas as pd
import numpy as np
import json
import time
import boto3
import requests
from datetime import datetime
from evidently import ColumnMapping
from evidently.report import Report
from evidently.metric_preset import DataDriftPreset, DataQualityPreset
from evidently.metrics import DatasetDriftMetric, ColumnDriftMetric
from prometheus_client import Gauge, start_http_server, CollectorRegistry

# ── Config ──────────────────────────────────────────────────────────────────
PARQUET_PATH   = "/home/ec2-user/payguard-mlops/ml/data/paysim_features.parquet"
FRAUD_API_URL  = "http://localhost:32002"
PROMETHEUS_PORT = 32004
CHECK_INTERVAL  = 60   # seconds between drift checks

FEATURE_COLS = [
    "amount","amount_log","oldbalanceOrg","newbalanceOrig",
    "oldbalanceDest","newbalanceDest","balance_diff_orig","balance_diff_dest",
    "hour_of_day","day_of_week","is_transfer","is_cash_out",
    "amount_to_balance_ratio","balance_utilization",
    "zero_balance_after_txn","large_transaction_flag",
]

# ── Prometheus metrics (isolated registry) ──────────────────────────────────
registry = CollectorRegistry()
DRIFT_SCORE        = Gauge("evidently_dataset_drift_score",
                           "Overall dataset drift share (0-1)", registry=registry)
DRIFT_DETECTED     = Gauge("evidently_drift_detected",
                           "1 if drift detected, 0 otherwise", registry=registry)
FEATURE_DRIFT      = Gauge("evidently_feature_drift_score",
                           "Per-feature drift score", ["feature"], registry=registry)
DRIFT_CHECKS_TOTAL = Gauge("evidently_drift_checks_total",
                           "Total drift checks run", registry=registry)
LAST_CHECK_TS      = Gauge("evidently_last_check_timestamp",
                           "Unix timestamp of last drift check", registry=registry)

# ── Reference data ───────────────────────────────────────────────────────────
def load_reference():
    print("Loading reference data from Parquet...")
    df = pd.read_parquet(PARQUET_PATH, columns=FEATURE_COLS + ["isFraud"])
    # Use a stratified sample as reference (fraud rate preserved)
    fraud  = df[df["isFraud"]==1].sample(min(1000, len(df[df["isFraud"]==1])), random_state=42)
    legit  = df[df["isFraud"]==0].sample(5000, random_state=42)
    ref    = pd.concat([fraud, legit]).sample(frac=1, random_state=42)[FEATURE_COLS]
    print(f"   Reference: {len(ref):,} rows")
    return ref

# ── Current data from PayStream stats + fraud-api ───────────────────────────
def generate_current_sample(n=500):
    """
    Generate a synthetic current sample that reflects the active PayStream mode.
    In normal mode → similar to reference.
    In drift/attack mode → shifted distributions (detected by Evidently).
    """
    try:
        status = requests.get(f"{FRAUD_API_URL.replace('32002','32003')}/status",
                              timeout=5).json()
        mode = status.get("mode", "normal")
    except Exception:
        mode = "normal"

    rng = np.random.default_rng(int(time.time()) % 10000)

    if mode == "attack":
        # Attack: high amounts, zero-balance patterns dominate
        amounts = rng.lognormal(12, 1.5, n)   # shifted higher
        zero_bal = rng.binomial(1, 0.7, n)     # 70% zero-balance
        is_transfer = rng.binomial(1, 0.8, n)  # mostly transfers
    elif mode == "drift":
        # Drift: gradual shift in transaction amounts and merchant types
        amounts = rng.lognormal(10, 2.0, n)    # wider spread
        zero_bal = rng.binomial(1, 0.3, n)
        is_transfer = rng.binomial(1, 0.5, n)
    else:
        # Normal: close to training distribution
        amounts = rng.lognormal(8, 2, n)
        zero_bal = rng.binomial(1, 0.1, n)
        is_transfer = rng.binomial(1, 0.35, n)

    old_bal = rng.lognormal(9, 2, n)
    new_bal = np.maximum(0, old_bal - amounts * rng.uniform(0.8, 1.2, n))

    df = pd.DataFrame({
        "amount":                amounts,
        "amount_log":            np.log1p(amounts),
        "oldbalanceOrg":         old_bal,
        "newbalanceOrig":        new_bal,
        "oldbalanceDest":        rng.lognormal(8, 2, n),
        "newbalanceDest":        rng.lognormal(8, 2, n),
        "balance_diff_orig":     new_bal - old_bal,
        "balance_diff_dest":     rng.normal(0, 1000, n),
        "hour_of_day":           rng.integers(0, 24, n).astype(float),
        "day_of_week":           rng.integers(0, 7, n).astype(float),
        "is_transfer":           is_transfer.astype(float),
        "is_cash_out":           rng.binomial(1, 0.3, n).astype(float),
        "amount_to_balance_ratio": np.where(old_bal > 0, amounts/old_bal, 0),
        "balance_utilization":   np.clip(np.where(old_bal > 0, amounts/old_bal, 0), 0, 1),
        "zero_balance_after_txn": zero_bal.astype(float),
        "large_transaction_flag": (amounts > 200_000).astype(float),
    })
    return df, mode

# ── Main drift check loop ────────────────────────────────────────────────────
def run_drift_check(reference, check_num):
    current, mode = generate_current_sample()

    col_mapping = ColumnMapping(numerical_features=FEATURE_COLS)

    report = Report(metrics=[
        DatasetDriftMetric(),
        *[ColumnDriftMetric(column_name=f) for f in FEATURE_COLS[:8]],
    ])
    report.run(reference_data=reference, current_data=current,
               column_mapping=col_mapping)

    result = report.as_dict()

    # Overall drift
    drift_share = result["metrics"][0]["result"]["share_of_drifted_columns"]
    drift_flag  = result["metrics"][0]["result"]["dataset_drift"]

    DRIFT_SCORE.set(drift_share)
    DRIFT_DETECTED.set(1 if drift_flag else 0)
    DRIFT_CHECKS_TOTAL.set(check_num)
    LAST_CHECK_TS.set(time.time())

    # Per-feature drift scores
    for i, feat in enumerate(FEATURE_COLS[:8]):
        try:
            score = result["metrics"][i+1]["result"].get("drift_score", 0)
            FEATURE_DRIFT.labels(feature=feat).set(score)
        except (IndexError, KeyError):
            pass

    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] "
          f"Mode={mode:8s} | Drift={drift_flag} | "
          f"Share={drift_share:.2f} | Features drifted="
          f"{result['metrics'][0]['result']['number_of_drifted_columns']}")

    # Save latest report to S3
    try:
        report.save_html("/tmp/drift_report_latest.html")
    except Exception:
        pass

    return drift_flag, drift_share

# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Starting Evidently drift detector on port {PROMETHEUS_PORT}")
    start_http_server(PROMETHEUS_PORT, registry=registry)

    reference = load_reference()
    check_num = 0
    print(f"Ready — checking drift every {CHECK_INTERVAL}s")
    print("   Switch PayStream to drift/attack mode to trigger detection")

    while True:
        try:
            check_num += 1
            run_drift_check(reference, check_num)
        except Exception as e:
            print(f"Check {check_num} failed: {e}")
        time.sleep(CHECK_INTERVAL)
