import mlflow, mlflow.sklearn, pandas as pd, numpy as np, warnings
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, precision_score, recall_score, f1_score, average_precision_score
warnings.filterwarnings("ignore")

MLFLOW_URI      = "http://localhost:32001"
PARQUET_PATH    = "/home/ec2-user/payguard-mlops/ml/data/paysim_features.parquet"
FEATURE_COLS = [
    "amount","amount_log","oldbalanceOrg","newbalanceOrig",
    "oldbalanceDest","newbalanceDest","balance_diff_orig","balance_diff_dest",
    "hour_of_day","day_of_week","is_transfer","is_cash_out",
    "amount_to_balance_ratio","balance_utilization",
    "zero_balance_after_txn","large_transaction_flag",
]
TARGET_COL = "isFraud"

mlflow.set_tracking_uri(MLFLOW_URI)
mlflow.set_experiment("fraud_detection_baseline")

df = pd.read_parquet(PARQUET_PATH, columns=FEATURE_COLS + [TARGET_COL])
fraud   = df[df[TARGET_COL] == 1]
legit   = df[df[TARGET_COL] == 0].sample(100_000, random_state=42)
df_s    = pd.concat([fraud, legit]).sample(frac=1, random_state=42)
X       = df_s[FEATURE_COLS]
y       = df_s[TARGET_COL]
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

models = {
    "logistic_regression": LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
    "random_forest":       RandomForestClassifier(n_estimators=100, class_weight="balanced", max_depth=10, random_state=42, n_jobs=-1),
}

for name, model in models.items():
    print(f"\nRunning: {name}")
    with mlflow.start_run(run_name=name):
        mlflow.log_params({"model_type": name, "train_size": len(X_train), "n_features": len(FEATURE_COLS)})
        mlflow.log_params(model.get_params())
        model.fit(X_train, y_train)
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
        mlflow.sklearn.log_model(model, "model",
            registered_model_name=f"fraud_detection_{name}",
            signature=infer_signature(X_train, y_proba),
            input_example=X_train.iloc[:5])
        for k, v in metrics.items():
            print(f"   {k}: {v}")
        print(f"   Run ID: {mlflow.active_run().info.run_id}")

print("\nBaseline experiment complete")
