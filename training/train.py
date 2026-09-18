import hashlib
import json
from pathlib import Path

import joblib
import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd

from mlflow.models import infer_signature
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    log_loss,
)

ROOT = Path(__file__).resolve().parents[1]

FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    train_path = ROOT / "data/train.csv"
    validation_path = ROOT / "data/validation.csv"

    train = pd.read_csv(train_path)
    validation = pd.read_csv(validation_path)

    X_train = train[FEATURES]
    y_train = train["acceptable"]

    X_validation = validation[FEATURES]
    y_validation = validation["acceptable"]

    parameters = {
        "n_estimators": 200,
        "max_depth": 6,
        "min_samples_leaf": 10,
        "random_state": 42,
        "n_jobs": -1,
    }

    mlflow.set_tracking_uri("http://127.0.0.1:5001")
    mlflow.set_experiment("scan-quality")

    with mlflow.start_run(run_name="random-forest-v1") as run:
        model = RandomForestClassifier(**parameters)
        model.fit(X_train, y_train)

        predictions = model.predict(X_validation)
        probabilities = model.predict_proba(X_validation)[:, 1]

        metrics = {
            "validation_accuracy": accuracy_score(
                y_validation, predictions
            ),
            "validation_precision": precision_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_recall": recall_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_f1": f1_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_roc_auc": roc_auc_score(
                y_validation, probabilities
            ),
            "validation_log_loss": log_loss(
                y_validation, probabilities
            ),
        }

        # Compare against always predicting the training majority class.
        majority_class = int(y_train.mode().iloc[0])
        baseline_predictions = np.full(
            len(y_validation), majority_class
        )
        metrics["baseline_accuracy"] = accuracy_score(
            y_validation, baseline_predictions
        )

        mlflow.log_params(parameters)
        mlflow.log_params({
            "training_rows": len(train),
            "validation_rows": len(validation),
            "classification_threshold": 0.5,
        })

        mlflow.set_tags({
            "candidate": "v1",
            "dataset_type": "synthetic",
            "train_sha256": file_hash(train_path),
            "validation_sha256": file_hash(validation_path),
        })

        mlflow.log_metrics(metrics)

        mlflow.sklearn.log_model(
            sk_model=model,
            name="scan_quality_model",
            signature=infer_signature(X_train, model.predict(X_train)),
            input_example=X_train.head(3),
            skops_trusted_types=["sklearn.tree._tree.Tree"],
        )

        # Keep each run in its own folder to avoid overwriting models.
        output_dir = ROOT / "artifacts" / run.info.run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        joblib.dump(model, output_dir / "model.joblib")

        metadata = {
            "run_id": run.info.run_id,
            "candidate": "v1",
            "features": FEATURES,
            "target": "acceptable",
            "class_labels": {"0": "poor", "1": "acceptable"},
            "threshold": 0.5,
            "metrics": metrics,
        }

        metadata_path = output_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )
        mlflow.log_artifact(str(metadata_path))

        print("\nValidation results")
        for name, value in metrics.items():
            print(f"{name}: {value:.4f}")

        print(f"\nRun ID: {run.info.run_id}")
        print(f"Model saved in: {output_dir}")


if __name__ == "__main__":
    main()