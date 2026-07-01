"""
Local Sample Data Generator — PayGuard
=================================================
Generates a small synthetic PaySim-style dataset for local development.
Original project used the full PaySim Kaggle dataset (6.3M rows) stored on S3.
This script generates an equivalent (smaller) dataset locally so the whole
pipeline can run with `docker compose up` and no cloud account.

Output: ml/data/paysim_features.parquet
"""

import numpy as np
import pandas as pd

RNG = np.random.default_rng(42)
N_ROWS = 20_000
FRAUD_RATE = 0.03

TXN_TYPES = ["PAYMENT", "TRANSFER", "CASH_OUT", "DEBIT", "CASH_IN"]


def generate_row(is_fraud: bool, step: int) -> dict:
    if is_fraud:
        txn_type = RNG.choice(["TRANSFER", "CASH_OUT"])
        amount = RNG.uniform(100_000, 500_000)
        old_balance_org = amount
        new_balance_orig = 0.0
    else:
        txn_type = RNG.choice(TXN_TYPES, p=[0.45, 0.2, 0.2, 0.1, 0.05])
        amount = float(RNG.lognormal(mean=9, sigma=2))
        old_balance_org = RNG.uniform(amount, amount * 3)
        new_balance_orig = max(old_balance_org - amount, 0)

    old_balance_dest = RNG.uniform(0, 100_000)
    new_balance_dest = RNG.uniform(0, 200_000)

    return {
        "step": step,
        "amount": round(amount, 2),
        "oldbalanceOrg": round(old_balance_org, 2),
        "newbalanceOrig": round(new_balance_orig, 2),
        "oldbalanceDest": round(old_balance_dest, 2),
        "newbalanceDest": round(new_balance_dest, 2),
        "transaction_type": txn_type,
        "isFraud": int(is_fraud),
    }


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    df["amount_log"] = np.log1p(df["amount"])
    df["balance_diff_orig"] = df["newbalanceOrig"] - df["oldbalanceOrg"]
    df["balance_diff_dest"] = df["newbalanceDest"] - df["oldbalanceDest"]
    df["hour_of_day"] = df["step"] % 24
    df["day_of_week"] = (df["step"] // 24) % 7
    df["is_transfer"] = (df["transaction_type"] == "TRANSFER").astype(int)
    df["is_cash_out"] = (df["transaction_type"] == "CASH_OUT").astype(int)
    df["amount_to_balance_ratio"] = np.where(
        df["oldbalanceOrg"] > 0, df["amount"] / df["oldbalanceOrg"], 0.0
    )
    df["balance_utilization"] = df["amount_to_balance_ratio"].clip(upper=1.0)
    df["zero_balance_after_txn"] = (df["newbalanceOrig"] == 0).astype(int)
    df["large_transaction_flag"] = (df["amount"] > 200_000).astype(int)
    return df


def main():
    n_fraud = int(N_ROWS * FRAUD_RATE)
    n_legit = N_ROWS - n_fraud

    rows = []
    for i in range(n_legit):
        rows.append(generate_row(False, step=int(RNG.integers(1, 744))))
    for i in range(n_fraud):
        rows.append(generate_row(True, step=int(RNG.integers(1, 744))))

    df = pd.DataFrame(rows).sample(frac=1, random_state=42).reset_index(drop=True)
    df = engineer_features(df)

    out_path = "ml/data/paysim_features.parquet"
    df.to_parquet(out_path, index=False)
    print(f"Wrote {len(df):,} rows ({df['isFraud'].sum():,} fraud) to {out_path}")


if __name__ == "__main__":
    main()
