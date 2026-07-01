from airflow import DAG
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.empty import EmptyOperator
from datetime import datetime, timedelta
import logging

default_args = {
    "owner": "mlops",
    "retries": 0,
    "email_on_failure": False,
}

QUALITY_GATES = {"roc_auc": 0.95, "precision": 0.80, "recall": 0.90, "f1": 0.85}

FEATURE_COLS = [
    "amount","amount_log","oldbalanceOrg","newbalanceOrig",
    "oldbalanceDest","newbalanceDest","balance_diff_orig","balance_diff_dest",
    "hour_of_day","day_of_week","is_transfer","is_cash_out",
    "amount_to_balance_ratio","balance_utilization",
    "zero_balance_after_txn","large_transaction_flag",
]

def prepare_data(**context):
    import boto3, pandas as pd, numpy as np, os
    log = logging.getLogger(__name__)

    log.info("Downloading PaySim from S3...")
    s3 = boto3.client("s3", region_name="us-east-2")
    s3.download_file("mlops-raw-data-YOUR_SUFFIX", "paysim/PS_log.csv", "/tmp/PS_log.csv")

    log.info("Engineering features...")
    df = pd.read_csv("/tmp/PS_log.csv")
    df["event_timestamp"]         = pd.Timestamp("2026-01-01") + pd.to_timedelta(df["step"], unit="h")
    df["amount_log"]              = np.log1p(df["amount"])
    df["balance_diff_orig"]       = df["newbalanceOrig"] - df["oldbalanceOrg"]
    df["balance_diff_dest"]       = df["newbalanceDest"] - df["oldbalanceDest"]
    df["hour_of_day"]             = df["step"] % 24
    df["day_of_week"]             = (df["step"] // 24) % 7
    df["is_transfer"]             = (df["type"] == "TRANSFER").astype(int)
    df["is_cash_out"]             = (df["type"] == "CASH_OUT").astype(int)
    df["transaction_type"]        = df["type"]
    df["amount_to_balance_ratio"] = np.where(df["oldbalanceOrg"] > 0, df["amount"] / df["oldbalanceOrg"], 0.0)
    df["balance_utilization"]     = df["amount_to_balance_ratio"].clip(0, 1)
    df["zero_balance_after_txn"]  = (df["newbalanceOrig"] == 0).astype(int)
    df["large_transaction_flag"]  = (df["amount"] > 200_000).astype(int)

    cols = FEATURE_COLS + ["isFraud"]
    os.makedirs("/tmp/mlops_data", exist_ok=True)
    out = "/tmp/mlops_data/paysim_features.parquet"
    df[cols].to_parquet(out, index=False)
    log.info(f"Saved {len(df):,} rows → {out}")
    os.remove("/tmp/PS_log.csv")

def train_xgboost(**context):
    import mlflow, mlflow.xgboost
    import xgboost as xgb
    import pandas as pd, numpy as np
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (roc_auc_score, precision_score,
                                 recall_score, f1_score, average_precision_score)
    log = logging.getLogger(__name__)

    MLFLOW_URI   = "http://mlflow.mlflow.svc.cluster.local:5000"
    PARQUET_PATH = "/tmp/mlops_data/paysim_features.parquet"

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("fraud_detection_xgboost")

    log.info("Loading features...")
    df    = pd.read_parquet(PARQUET_PATH, columns=FEATURE_COLS + ["isFraud"])
    fraud = df[df["isFraud"] == 1]
    legit = df[df["isFraud"] == 0].sample(100_000, random_state=42)
    df_s  = pd.concat([fraud, legit]).sample(frac=1, random_state=42)
    X     = df_s[FEATURE_COLS]
    y     = df_s["isFraud"]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y)

    neg, pos         = (y_train == 0).sum(), (y_train == 1).sum()
    scale_pos_weight = neg / pos
    log.info(f"scale_pos_weight: {scale_pos_weight:.1f}")

    params = {
        "n_estimators": 300, "max_depth": 6, "learning_rate": 0.1,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "scale_pos_weight": scale_pos_weight,
        "random_state": 42, "n_jobs": -1, "eval_metric": "aucpr",
    }

    with mlflow.start_run(run_name="xgboost_v1") as run:
        mlflow.log_params({**params, "train_size": len(X_train), "n_features": len(FEATURE_COLS)})
        model = xgb.XGBClassifier(**params)
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

        y_pred  = model.predict(X_test)
        y_proba = model.predict_proba(X_test)[:, 1]
        metrics = {
            "roc_auc":       round(roc_auc_score(y_test, y_proba), 4),
            "avg_precision": round(average_precision_score(y_test, y_proba), 4),
            "precision":     round(precision_score(y_test, y_pred), 4),
            "recall":        round(recall_score(y_test, y_pred), 4),
            "f1":            round(f1_score(y_test, y_pred), 4),
        }
        mlflow.log_metrics(metrics)
        from mlflow.models.signature import infer_signature
        mlflow.xgboost.log_model(
            model, "model",
            registered_model_name="fraud_detection_xgboost",
            signature=infer_signature(X_train, y_proba),
            input_example=X_train.iloc[:5],
        )
        run_id = run.info.run_id
        for k, v in metrics.items():
            log.info(f"  {k}: {v}")

    context["ti"].xcom_push(key="metrics", value=metrics)
    context["ti"].xcom_push(key="run_id",  value=run_id)
    log.info(f"XGBoost training complete — run_id: {run_id}")

def quality_gate(**context):
    log     = logging.getLogger(__name__)
    metrics = context["ti"].xcom_pull(key="metrics", task_ids="train_xgboost")
    log.info("Quality gate checks:")
    all_pass = True
    for metric, threshold in QUALITY_GATES.items():
        val    = metrics.get(metric, 0)
        passed = val >= threshold
        log.info(f"  {metric}: {val:.4f} >= {threshold} → {'PASS' if passed else 'FAIL'}")
        if not passed:
            all_pass = False
    return "promote_to_staging" if all_pass else "quality_gate_failed"

def promote_to_staging(**context):
    import mlflow
    log = logging.getLogger(__name__)
    mlflow.set_tracking_uri("http://mlflow.mlflow.svc.cluster.local:5000")
    client   = mlflow.tracking.MlflowClient()
    versions = client.get_latest_versions("fraud_detection_xgboost")
    latest   = sorted(versions, key=lambda v: int(v.version))[-1]
    client.transition_model_version_stage(
        name="fraud_detection_xgboost", version=latest.version,
        stage="Staging", archive_existing_versions=True)
    metrics = context["ti"].xcom_pull(key="metrics", task_ids="train_xgboost")
    client.update_model_version(
        name="fraud_detection_xgboost", version=latest.version,
        description=f"XGBoost v{latest.version} — ROC-AUC={metrics['roc_auc']}, F1={metrics['f1']}. Auto-promoted by Airflow.")
    log.info(f"fraud_detection_xgboost v{latest.version} → Staging")

with DAG(
    dag_id="model_training",
    default_args=default_args,
    description="XGBoost training: S3 → feature engineering → train → quality gate → MLflow Staging",
    schedule_interval=None,
    start_date=datetime(2026, 3, 1),
    catchup=False,
    tags=["mlops", "training", "xgboost"],
) as dag:

    start   = EmptyOperator(task_id="start")
    prep    = PythonOperator(task_id="prepare_data",       python_callable=prepare_data)
    train   = PythonOperator(task_id="train_xgboost",      python_callable=train_xgboost)
    gate    = BranchPythonOperator(task_id="quality_gate", python_callable=quality_gate)
    promote = PythonOperator(task_id="promote_to_staging", python_callable=promote_to_staging)
    failed  = EmptyOperator(task_id="quality_gate_failed")
    end     = EmptyOperator(task_id="end", trigger_rule="none_failed_min_one_success")

    start >> prep >> train >> gate >> [promote, failed] >> end
