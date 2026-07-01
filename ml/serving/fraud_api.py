"""
Fraud Detection FastAPI Serving Layer
======================================
Loads the XGBoost model from MLflow Staging registry.
Exposes /predict, /health, /readiness, /metrics endpoints.
Instrumented with Prometheus for real-time observability.
"""

import time
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    Counter, Histogram, Gauge, generate_latest,
    CONTENT_TYPE_LATEST, CollectorRegistry
)

# Isolated registry — avoids "Duplicated timeseries" on pod restarts
# because each new process gets a clean registry with no prior state
REGISTRY = CollectorRegistry(auto_describe=True)
from pydantic import BaseModel, Field, field_validator

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("fraud_api")

# =============================================================================
# PROMETHEUS METRICS
# Uses an isolated CollectorRegistry per process — eliminates duplicate
# timeseries errors on pod restarts without needing try/except guards.
# =============================================================================
REQUEST_COUNT = Counter(
    "fraud_api_requests_total",
    "Total API requests",
    ["path", "status"],   # "path" not "endpoint" — avoids Prometheus PodMonitor label collision
    registry=REGISTRY
)

REQUEST_LATENCY = Histogram(
    "fraud_api_request_duration_seconds",
    "API request latency",
    ["path"],             # same rename
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
    registry=REGISTRY
)

FRAUD_PREDICTIONS = Counter(
    "fraud_predictions_total",
    "Total fraud predictions",
    ["result"],
    registry=REGISTRY
)

FRAUD_PROBABILITY_HIST = Histogram(
    "fraud_probability_distribution",
    "Distribution of fraud probability scores",
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    registry=REGISTRY
)

MODEL_INFO = Gauge(
    "fraud_model_info",
    "Loaded model metadata",
    ["model_name", "model_version", "model_stage"],
    registry=REGISTRY
)

PREDICTION_ERRORS = Counter(
    "fraud_api_prediction_errors_total",
    "Total prediction errors",
    registry=REGISTRY
)

# =============================================================================
# FEATURE COLUMNS (must match training order exactly)
# =============================================================================
FEATURE_COLS = [
    "amount", "amount_log",
    "oldbalanceOrg", "newbalanceOrig",
    "oldbalanceDest", "newbalanceDest",
    "balance_diff_orig", "balance_diff_dest",
    "hour_of_day", "day_of_week",
    "is_transfer", "is_cash_out",
    "amount_to_balance_ratio", "balance_utilization",
    "zero_balance_after_txn", "large_transaction_flag",
]

# =============================================================================
# GLOBAL MODEL STATE
# =============================================================================
class ModelState:
    model = None
    model_name: str = ""
    model_version: str = ""
    model_stage: str = ""
    loaded_at: float = 0.0

state = ModelState()

# =============================================================================
# PREDICTION HISTORY (in-memory, bounded ring buffer)
# Powers the /stats endpoint added for PayGuard — gives reviewers
# a quick view of recent scoring activity and model behaviour without
# needing to query Prometheus/Grafana.
# =============================================================================
from collections import deque

MAX_HISTORY = int(os.getenv("PREDICTION_HISTORY_SIZE", "200"))
prediction_history: deque = deque(maxlen=MAX_HISTORY)

# =============================================================================
# MODEL LOADER
# Loads from MLflow registry by stage ("Staging").
# Using stage rather than version means a model promotion automatically
# updates the serving layer without redeployment — just restart the pod.
# =============================================================================
def load_model(
    tracking_uri: str = "http://mlflow:5000",
    model_name: str = "fraud_detection_xgboost",
    stage: str = "Staging"
):
    log.info(f"Loading model: {model_name} @ {stage} from {tracking_uri}")
    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()

    versions = client.get_latest_versions(model_name, stages=[stage])
    if not versions:
        raise RuntimeError(f"No model found: {model_name} @ {stage}")

    v = versions[0]
    model_uri = f"models:/{model_name}/{stage}"

    state.model = mlflow.pyfunc.load_model(model_uri)
    state.model_name    = model_name
    state.model_version = v.version
    state.model_stage   = stage
    state.loaded_at     = time.time()

    # Set Prometheus gauge so Grafana shows which version is serving
    MODEL_INFO.labels(
        model_name=model_name,
        model_version=v.version,
        model_stage=stage
    ).set(1)

    log.info(f"Model loaded: {model_name} v{v.version} ({stage})")

# =============================================================================
# FASTAPI APP — lifespan handles startup/shutdown cleanly
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    tracking_uri = os.getenv(
        "MLFLOW_TRACKING_URI",
        "http://mlflow:5000"
    )
    model_name = os.getenv("MODEL_NAME", "fraud_detection_xgboost")
    stage      = os.getenv("MODEL_STAGE", "Staging")

    try:
        load_model(tracking_uri, model_name, stage)
    except Exception as e:
        log.error(f"Failed to load model: {e}")
        # Don't crash on startup — /health will return unhealthy
        # and Kubernetes readiness probe will hold traffic until recovered

    yield  # App runs here

    log.info("Shutting down fraud detection API")

app = FastAPI(
    title="PayGuard: Fraud Detection API",
    description="Real-time fraud scoring with XGBoost, loaded from MLflow registry",
    version="1.0.0",
    lifespan=lifespan
)

# =============================================================================
# REQUEST LOGGING MIDDLEWARE
# Logs method, path, status code and latency for every request — useful for
# debugging and for the /stats endpoint's "recent activity" view.
# =============================================================================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    log.info(
        f"{request.method} {request.url.path} -> {response.status_code} "
        f"({elapsed_ms:.1f}ms)"
    )
    return response

# =============================================================================
# REQUEST / RESPONSE SCHEMAS
# =============================================================================
VALID_TXN_TYPES = {"TRANSFER", "CASH_OUT", "PAYMENT", "DEBIT", "CASH_IN"}

class Transaction(BaseModel):
    """Raw PayStream transaction event — mirrors PaySim feature space"""
    transaction_id:   str   = Field(..., min_length=1, description="Unique transaction ID")
    amount:           float = Field(..., gt=0, description="Transaction amount")
    oldbalanceOrg:    float = Field(default=0.0, ge=0)
    newbalanceOrig:   float = Field(default=0.0, ge=0)
    oldbalanceDest:   float = Field(default=0.0, ge=0)
    newbalanceDest:   float = Field(default=0.0, ge=0)
    transaction_type: str   = Field(default="TRANSFER",
                                    description="TRANSFER, CASH_OUT, PAYMENT, DEBIT, CASH_IN")
    step:             int   = Field(default=1, ge=0, description="Hour step in simulation")
    threshold:        float = Field(default=0.5, ge=0.0, le=1.0,
                                     description="Classification threshold")

    @field_validator("transaction_type")
    @classmethod
    def validate_transaction_type(cls, v: str) -> str:
        v_upper = v.upper()
        if v_upper not in VALID_TXN_TYPES:
            raise ValueError(
                f"transaction_type must be one of {sorted(VALID_TXN_TYPES)}, got '{v}'"
            )
        return v_upper

class PredictionResponse(BaseModel):
    transaction_id:   str
    is_fraud:         bool
    fraud_probability: float
    model_name:       str
    model_version:    str
    inference_time_ms: float
    features_computed: dict

class HealthResponse(BaseModel):
    status:        str
    model_loaded:  bool
    model_name:    str
    model_version: str
    uptime_seconds: float

# =============================================================================
# FEATURE ENGINEERING
# Replicates the exact same transformations used during training.
# Keeping this in the serving layer (vs. Feast online store) is a deliberate
# simplification for the portfolio project — in production you'd fetch
# pre-computed features from Feast to eliminate training-serving skew.
# =============================================================================
def engineer_features(txn: Transaction) -> pd.DataFrame:
    amount_log = np.log1p(txn.amount)
    balance_diff_orig = txn.newbalanceOrig - txn.oldbalanceOrg
    balance_diff_dest = txn.newbalanceDest - txn.oldbalanceDest
    hour_of_day = txn.step % 24
    day_of_week = (txn.step // 24) % 7
    is_transfer = int(txn.transaction_type == "TRANSFER")
    is_cash_out = int(txn.transaction_type == "CASH_OUT")
    amount_to_balance_ratio = (
        txn.amount / txn.oldbalanceOrg if txn.oldbalanceOrg > 0 else 0.0
    )
    balance_utilization = min(amount_to_balance_ratio, 1.0)
    zero_balance_after_txn = int(txn.newbalanceOrig == 0)
    large_transaction_flag = int(txn.amount > 200_000)

    return pd.DataFrame([{
        "amount":                  txn.amount,
        "amount_log":              amount_log,
        "oldbalanceOrg":           txn.oldbalanceOrg,
        "newbalanceOrig":          txn.newbalanceOrig,
        "oldbalanceDest":          txn.oldbalanceDest,
        "newbalanceDest":          txn.newbalanceDest,
        "balance_diff_orig":       balance_diff_orig,
        "balance_diff_dest":       balance_diff_dest,
        "hour_of_day":             hour_of_day,
        "day_of_week":             day_of_week,
        "is_transfer":             is_transfer,
        "is_cash_out":             is_cash_out,
        "amount_to_balance_ratio": amount_to_balance_ratio,
        "balance_utilization":     balance_utilization,
        "zero_balance_after_txn":  zero_balance_after_txn,
        "large_transaction_flag":  large_transaction_flag,
    }], columns=FEATURE_COLS)

# =============================================================================
# ENDPOINTS
# =============================================================================
@app.post("/predict", response_model=PredictionResponse)
async def predict(txn: Transaction, request: Request):
    """
    Score a transaction for fraud probability.
    Called by PayStream for every synthetic transaction before processing.
    """
    start = time.perf_counter()

    if state.model is None:
        REQUEST_COUNT.labels(path="/predict", status="503").inc()
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        features = engineer_features(txn)
        raw_pred = state.model.predict(features)

        # MLflow pyfunc returns different shapes depending on the flavor
        if hasattr(raw_pred, '__len__') and len(raw_pred.shape) > 1:
            fraud_prob = float(raw_pred[0][1])   # sklearn-style [[p0, p1]]
        else:
            fraud_prob = float(raw_pred[0])       # xgboost pyfunc returns proba directly

        # Clamp to [0, 1]
        fraud_prob = max(0.0, min(1.0, fraud_prob))
        is_fraud   = fraud_prob >= txn.threshold

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Update Prometheus metrics
        REQUEST_COUNT.labels(path="/predict", status="200").inc()
        REQUEST_LATENCY.labels(path="/predict").observe(elapsed_ms / 1000)
        FRAUD_PREDICTIONS.labels(result="fraud" if is_fraud else "legitimate").inc()
        FRAUD_PROBABILITY_HIST.observe(fraud_prob)

        prediction_history.append({
            "transaction_id":    txn.transaction_id,
            "is_fraud":          is_fraud,
            "fraud_probability": round(fraud_prob, 6),
            "amount":            txn.amount,
            "transaction_type":  txn.transaction_type,
            "inference_time_ms": round(elapsed_ms, 3),
            "timestamp":         time.time(),
        })

        return PredictionResponse(
            transaction_id=txn.transaction_id,
            is_fraud=is_fraud,
            fraud_probability=round(fraud_prob, 6),
            model_name=state.model_name,
            model_version=state.model_version,
            inference_time_ms=round(elapsed_ms, 3),
            features_computed=features.iloc[0].to_dict()
        )

    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        REQUEST_COUNT.labels(path="/predict", status="500").inc()
        PREDICTION_ERRORS.inc()
        log.error(f"Prediction error for {txn.transaction_id}: {e}")
        raise HTTPException(status_code=500, detail="prediction failed — see server logs")


@app.get("/health", response_model=HealthResponse)
async def health():
    """Liveness probe — always returns 200 if process is alive"""
    REQUEST_COUNT.labels(path="/health", status="200").inc()
    return HealthResponse(
        status="healthy" if state.model is not None else "degraded",
        model_loaded=state.model is not None,
        model_name=state.model_name or "none",
        model_version=state.model_version or "none",
        uptime_seconds=round(time.time() - state.loaded_at, 1) if state.loaded_at else 0.0
    )


@app.get("/readiness")
async def readiness():
    """
    Kubernetes readiness probe — returns 503 until model is loaded.
    Kubernetes will not send traffic to the pod until this returns 200.
    """
    if state.model is None:
        raise HTTPException(status_code=503, detail="Model not ready")
    return {"status": "ready", "model": state.model_name, "version": state.model_version}


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint — Prometheus polls this every 15s"""
    return PlainTextResponse(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/model-info")
async def model_info():
    """Returns current model metadata — useful for debugging and dashboards"""
    if state.model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {
        "model_name":    state.model_name,
        "model_version": state.model_version,
        "model_stage":   state.model_stage,
        "loaded_at":     state.loaded_at,
        "feature_cols":  FEATURE_COLS,
        "threshold":     0.5,
    }


@app.get("/stats")
async def stats(limit: int = 50):
    """
    Recent prediction activity + summary statistics.
    Added for PayGuard — gives a quick operational view of
    model behaviour without needing Prometheus/Grafana running.
    """
    limit = max(1, min(limit, MAX_HISTORY))
    history = list(prediction_history)[-limit:]

    total = len(prediction_history)
    fraud_count = sum(1 for h in prediction_history if h["is_fraud"])
    avg_latency = (
        sum(h["inference_time_ms"] for h in prediction_history) / total
        if total else 0.0
    )
    avg_prob = (
        sum(h["fraud_probability"] for h in prediction_history) / total
        if total else 0.0
    )

    return {
        "total_scored":          total,
        "fraud_flagged":         fraud_count,
        "fraud_rate_percent":    round(fraud_count / total * 100, 2) if total else 0.0,
        "avg_inference_time_ms": round(avg_latency, 3),
        "avg_fraud_probability": round(avg_prob, 6),
        "history_capacity":      MAX_HISTORY,
        "recent_predictions":    list(reversed(history)),
    }


# =============================================================================
# ENTRYPOINT
# =============================================================================
if __name__ == "__main__":
    uvicorn.run(
        "fraud_api:app",
        host="0.0.0.0",
        port=8000,
        workers=2,
        log_level="info"
    )
