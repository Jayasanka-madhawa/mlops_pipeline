import hashlib
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

ROOT = Path(__file__).resolve().parents[1]
run_id = os.environ["MODEL_RUN_ID"]
model_dir = ROOT / "artifacts" / run_id

metadata = json.loads((model_dir / "metadata.json").read_text())
model = joblib.load(model_dir / "model.joblib")

test_path = ROOT / "data" / "test.csv"
test = pd.read_csv(test_path)
train = pd.read_csv(ROOT / "data" / "train.csv")

X = test[metadata["features"]]
y = test["acceptable"]

positive_index = list(model.classes_).index(1)
probabilities = model.predict_proba(X)[:, positive_index]
predictions = (probabilities >= metadata["threshold"]).astype(int)

# Determine the baseline class from training data only.
majority_class = int(train["acceptable"].mode().iloc[0])
baseline = np.full(len(y), majority_class)

metrics = {
    "test_accuracy": accuracy_score(y, predictions),
    "test_baseline_accuracy": accuracy_score(y, baseline),
    "test_precision": precision_score(y, predictions, zero_division=0),
    "test_recall": recall_score(y, predictions, zero_division=0),
    "test_f1": f1_score(y, predictions, zero_division=0),
    "test_roc_auc": roc_auc_score(y, probabilities),
    "test_log_loss": log_loss(y, probabilities, labels=[0, 1]),
}

matrix = confusion_matrix(y, predictions, labels=[0, 1])

report = {
    "model_run_id": run_id,
    "test_rows": len(test),
    "test_sha256": hashlib.sha256(test_path.read_bytes()).hexdigest(),
    "threshold": metadata["threshold"],
    "metrics": {name: float(value) for name, value in metrics.items()},
    "confusion_matrix": matrix.tolist(),
}

report_path = model_dir / "test_evaluation.json"
report_path.write_text(json.dumps(report, indent=2))

for name, value in metrics.items():
    print(f"{name}: {value:.4f}")

print("\nConfusion matrix: rows=actual, columns=predicted")
print("Class order: 0=poor, 1=acceptable")
print(matrix)
print(f"\nReport saved: {report_path}")