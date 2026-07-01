"""
Local Training Script — PayGuard
============================================
Trains an XGBoost fraud classifier on the local synthetic PaySim sample
and registers it with the local MLflow server, promoting it to "Staging"
so fraud_api.py can load it on startup.

This is the local-dev equivalent of ml/training/baseline_experiment.py,
adapted to run against `mlflow` (docker-compose service) instead of
the AWS-hosted MLflow at http://localhost:32001.
"""

import os
import time
import warnings

import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
from mlflow.models.signature import infer_signature
from mlflow.tracking import MlflowClient
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
PARQUET_PATH = os.getenv("PARQUET_PATH", "ml/data/paysim_features.parquet")
MODEL_NAME = os.getenv("MODEL_NAME", "fraud_detection_xgboost")

FEATURE_COLS = [
    "amount", "amount_log", "oldbalanceOrg", "newbalanceOrig",
    "oldbalanceDest", "newbalanceDest", "balance_diff_orig", "balance_diff_dest",
    "hour_of_day", "day_of_week", "is_transfer", "is_cash_out",
    "amount_to_balance_ratio", "balance_utilization",
    "zero_balance_after_txn", "large_transaction_flag",
]
TARGET_COL = "isFraud"


class FraudProbaModel(mlflow.pyfunc.PythonModel):
    """
    Wraps the sklearn XGBClassifier so pyfunc.predict() returns the positive-class
    probability directly. mlflow.xgboost.log_model's pyfunc flavor calls the
    sklearn model's .predict() (hard 0/1 labels), NOT .predict_proba() — logging
    the raw classifier silently made every /predict response a hard label instead
    of a continuous fraud score, even though the signature was inferred from
    predict_proba output. This wrapper is what actually connects the two.
    """

    def __init__(self, sk_model):
        self.sk_model = sk_model

    def predict(self, context, model_input, params=None):
        return self.sk_model.predict_proba(model_input)[:, 1]


def main():
    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("fraud_detection_local")

    df = pd.read_parquet(PARQUET_PATH, columns=FEATURE_COLS + [TARGET_COL])
    X = df[FEATURE_COLS]
    y = df[TARGET_COL]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = XGBClassifier(
        n_estimators=150,
        max_depth=6,
        learning_rate=0.1,
        scale_pos_weight=(y_train == 0).sum() / max((y_train == 1).sum(), 1),
        eval_metric="aucpr",
        random_state=42,
    )

    with mlflow.start_run(run_name="xgboost_local") as run:
        mlflow.log_params({
            "model_type": "xgboost",
            "train_size": len(X_train),
            "n_features": len(FEATURE_COLS),
            **model.get_params(),
        })
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)[:, 1]

        metrics = {
            "roc_auc": round(roc_auc_score(y_test, y_proba), 4),
            "avg_precision": round(average_precision_score(y_test, y_proba), 4),
            "precision": round(precision_score(y_test, y_pred), 4),
            "recall": round(recall_score(y_test, y_pred), 4),
            "f1": round(f1_score(y_test, y_pred), 4),
        }
        mlflow.log_metrics(metrics)

        signature = infer_signature(X_train, y_proba)
        mlflow.pyfunc.log_model(
            "model",
            python_model=FraudProbaModel(model),
            registered_model_name=MODEL_NAME,
            signature=signature,
            input_example=X_train.iloc[:5],
        )

        print(f"Run ID: {run.info.run_id}")
        for k, v in metrics.items():
            print(f"  {k}: {v}")

    # Promote latest version to Staging so fraud_api can load it
    client = MlflowClient()
    for _ in range(10):
        versions = client.search_model_versions(f"name='{MODEL_NAME}'")
        if versions:
            break
        time.sleep(1)

    latest = max(versions, key=lambda v: int(v.version))
    client.transition_model_version_stage(
        name=MODEL_NAME, version=latest.version, stage="Staging", archive_existing_versions=True
    )
    print(f"Promoted {MODEL_NAME} v{latest.version} to Staging")


if __name__ == "__main__":
    main()
