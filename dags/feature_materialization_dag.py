"""
DAG 1: Feature Materialization Pipeline
Runs daily — reads PaySim Parquet from S3, engineers features,
materializes to Redis online store via Feast.
"""
from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import logging

default_args = {
    "owner": "mlops",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": False,
}

def download_and_engineer_features(**context):
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

    feature_cols = [
        "nameOrig", "event_timestamp", "isFraud",
        "amount", "amount_log", "oldbalanceOrg", "newbalanceOrig",
        "oldbalanceDest", "newbalanceDest", "balance_diff_orig", "balance_diff_dest",
        "hour_of_day", "day_of_week", "transaction_type", "is_transfer", "is_cash_out",
        "amount_to_balance_ratio", "balance_utilization",
        "zero_balance_after_txn", "large_transaction_flag",
    ]
    os.makedirs("/tmp/feast_data", exist_ok=True)
    out = "/tmp/feast_data/paysim_features.parquet"
    df[feature_cols].to_parquet(out, index=False)
    log.info(f"Saved {len(df):,} rows → {out}")
    os.remove("/tmp/PS_log.csv")
    return out

def materialize_to_feast(**context):
    import subprocess, os
    log = logging.getLogger(__name__)

    # Copy parquet to expected path
    os.makedirs("/home/airflow/feast_data", exist_ok=True)
    import shutil
    shutil.copy("/tmp/feast_data/paysim_features.parquet",
                "/home/airflow/feast_data/paysim_features.parquet")

    # Get Redis IP from K8s
    import subprocess
    result = subprocess.run(
        ["python3", "-c",
         "import kubernetes as k8s; "
         "k8s.config.load_incluster_config(); "
         "v1 = k8s.client.CoreV1Api(); "
         "svc = v1.read_namespaced_service('redis-master', 'feast'); "
         "print(svc.spec.cluster_ip)"],
        capture_output=True, text=True
    )
    redis_ip = result.stdout.strip()
    log.info(f"Redis IP: {redis_ip}")

    # Write feature_store.yaml
    import boto3
    s3 = boto3.client("s3", region_name="us-east-2")
    yaml_content = f"""project: fraud_detection
registry: s3://mlops-feature-store-YOUR_SUFFIX/feast/registry.pb
provider: aws
online_store:
  type: redis
  connection_string: "{redis_ip}:6379"
offline_store:
  type: file
entity_key_serialization_version: 2
"""
    os.makedirs("/home/airflow/feast_repo", exist_ok=True)
    with open("/home/airflow/feast_repo/feature_store.yaml", "w") as f:
        f.write(yaml_content)

    # Copy feature definitions
    import shutil, os
    # Write inline since we can't mount from host
    feature_def = '''
from datetime import timedelta
from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float64, Int64, String

transaction = Entity(name="transaction", join_keys=["nameOrig"])

source = FileSource(
    path="/home/airflow/feast_data/paysim_features.parquet",
    timestamp_field="event_timestamp",
)

transaction_features = FeatureView(
    name="transaction_features", entities=[transaction], ttl=timedelta(days=90),
    schema=[
        Field(name="amount", dtype=Float64), Field(name="amount_log", dtype=Float64),
        Field(name="oldbalanceOrg", dtype=Float64), Field(name="newbalanceOrig", dtype=Float64),
        Field(name="oldbalanceDest", dtype=Float64), Field(name="newbalanceDest", dtype=Float64),
        Field(name="balance_diff_orig", dtype=Float64), Field(name="balance_diff_dest", dtype=Float64),
        Field(name="hour_of_day", dtype=Int64), Field(name="day_of_week", dtype=Int64),
        Field(name="transaction_type", dtype=String),
        Field(name="is_transfer", dtype=Int64), Field(name="is_cash_out", dtype=Int64),
    ], source=source,
)

account_velocity_features = FeatureView(
    name="account_velocity_features", entities=[transaction], ttl=timedelta(days=90),
    schema=[
        Field(name="amount_to_balance_ratio", dtype=Float64),
        Field(name="balance_utilization", dtype=Float64),
        Field(name="zero_balance_after_txn", dtype=Int64),
        Field(name="large_transaction_flag", dtype=Int64),
    ], source=source,
)
'''
    with open("/home/airflow/feast_repo/fraud_features.py", "w") as f:
        f.write(feature_def)

    # Run feast materialize
    from feast import FeatureStore
    store = FeatureStore(repo_path="/home/airflow/feast_repo")
    store.apply([])  # re-register
    end_date   = datetime.utcnow()
    start_date = end_date - timedelta(days=365)
    store.materialize(start_date=start_date, end_date=end_date)
    log.info("Features materialized to Redis")

with DAG(
    dag_id="feature_materialization",
    default_args=default_args,
    description="Daily feature materialization: S3 → Feast → Redis",
    schedule_interval="0 1 * * *",  # 1am daily
    start_date=datetime(2026, 3, 1),
    catchup=False,
    tags=["mlops", "feast", "features"],
) as dag:

    t1 = PythonOperator(
        task_id="download_and_engineer_features",
        python_callable=download_and_engineer_features,
    )

    t2 = PythonOperator(
        task_id="materialize_to_feast",
        python_callable=materialize_to_feast,
    )

    t1 >> t2
