import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

ROOT = Path(__file__).resolve().parents[1]

FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]

parser = argparse.ArgumentParser()
parser.add_argument("--start", required=True)
parser.add_argument("--end", required=True)
parser.add_argument("--expected-count", type=int, default=300)
args = parser.parse_args()

reference = pd.read_csv(ROOT / "data" / "reference_features.csv")

db_path = ROOT / "data" / "predictions.db"
if not db_path.exists():
    raise SystemExit("Prediction database does not exist.")

with sqlite3.connect(db_path) as connection:
    production = pd.read_sql_query(
        """
        SELECT brightness, motion, face_visibility, signal_quality
        FROM predictions
        WHERE timestamp >= ? AND timestamp < ?
        """,
        connection,
        params=(args.start, args.end),
    )

print(f"Reference records: {len(reference)}")
print(f"Production records: {len(production)}")

if len(production) != args.expected_count:
    raise SystemExit(
        f"Expected {args.expected_count} saved records. "
        "Check the time window, other traffic and logging failures."
    )

if len(production) < 100:
    raise SystemExit("Use at least 100 records for this exercise.")

for name, data in [("reference", reference), ("production", production)]:
    if not np.isfinite(data[FEATURES].to_numpy(dtype=float)).all():
        raise SystemExit(f"Missing or non-finite values in {name} data.")

# Account for testing four features in this window.
p_threshold = 0.05 / len(FEATURES)

# Example practical threshold; calibrate for a real application.
ks_threshold = 0.10

results = []

for feature in FEATURES:
    result = ks_2samp(reference[feature], production[feature])

    flagged = (
        result.pvalue < p_threshold
        and result.statistic >= ks_threshold
    )

    results.append({
        "feature": feature,
        "reference_mean": reference[feature].mean(),
        "production_mean": production[feature].mean(),
        "ks_score": result.statistic,
        "p_value": result.pvalue,
        "result": "DRIFT FLAG" if flagged else "NO FLAG",
    })

report = pd.DataFrame(results)

print(f"\nRule: p < {p_threshold:.4f} AND KS >= {ks_threshold:.2f}")
print(report.to_string(index=False, float_format=lambda x: f"{x:.4g}"))