from datetime import timedelta
from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float64, Int64, String

transaction = Entity(name="transaction", join_keys=["nameOrig"])

source = FileSource(
    path="/home/ec2-user/payguard-mlops/ml/data/paysim_features.parquet",
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
