# MLflow in our scan-quality MLOps project

## 1. Where we use MLflow

We use MLflow during local training and validation in `training/train.py` to record experiments and save model artifacts.

Our implementation uses **experiment tracking and model logging**. We have not implemented a model-registry approval workflow, automatic model promotion, deployment through MLflow, or production prediction monitoring through MLflow.

| Component | Responsibility in our project |
|---|---|
| scikit-learn | Train the Random Forest and calculate predictions |
| MLflow | Record parameters, validation metrics, metadata and model artifacts |
| joblib | Save the separate local model copy used by FastAPI |
| FastAPI | Load the local model and serve predictions |
| SQLite prediction database | Store production-like inputs and prediction records |
| Prometheus and Grafana | Monitor service metrics and drift results |
| Docker Compose | Run services and select application releases |

The MLflow tracking server does not train the model for us. The local Python training process fits the model, then sends experiment information to the server.

## 2. Why we use it

Suppose we train one model with 200 trees and another with 300 trees. MLflow keeps separate runs, allowing us to compare their parameters and validation results and retrieve the associated artifacts.

It helps answer:

- Which configuration produced this result?
- Which data files were used?
- Which saved model belongs to this run?
- How does this candidate compare with an earlier candidate?
- Which training run produced the model used by a prediction?

Tracking supports reproducibility, but does not automatically guarantee it. We must also preserve the actual data, source code, environment and relevant configuration.

## 3. Start the local tracking server

From the project directory, in a separate terminal:

```bash
conda activate mlops
cd /Users/jayasanka/Documents/mlops_pipeline

mlflow server \
  --host 127.0.0.1 \
  --port 5001 \
  --backend-store-uri sqlite:///mlflow.db \
  --artifacts-destination ./mlartifacts
```

Open `http://127.0.0.1:5001` in your browser.

| Setting | Purpose |
|---|---|
| `127.0.0.1` | Listen locally on the Mac |
| Port `5001` | Tracking API and browser UI |
| `mlflow.db` | SQLite backend for experiment and run metadata |
| `mlartifacts/` | Server-side destination for logged artifact files |

Relative paths are resolved from the server's working directory. Starting from another directory can create a different database and artifact location.

Our project has two different SQLite databases: `mlflow.db` for MLflow metadata, and `data/predictions.db` for prediction records. They are not interchangeable.

## 4. Core concepts

| Concept | Our example | Meaning |
|---|---|---|
| Experiment | `scan-quality` | Group of related training runs |
| Run name | `random-forest-v1` | Human-readable label for one execution |
| Run ID | `d9571388a61f45369e4f709878e8246b` | Unique identifier for your successful execution |
| Parameter | `max_depth=6` | Configuration value |
| Metric | `validation_accuracy=0.6383` | Numeric evaluation result |
| Tag | `dataset_type=synthetic` | Descriptive metadata |
| Artifact | Saved model or `metadata.json` | File associated with the run |

Run names can repeat. Run IDs distinguish executions. Our `candidate=v1` tag is a descriptive label, not a registered-model version or approval status.

## 5. All MLflow integration points

### Imports

```python
import mlflow
import mlflow.sklearn
from mlflow.models import infer_signature
```

`mlflow.sklearn` provides scikit-learn model packaging. `infer_signature` derives the input/output schema from examples.

### Connect to the server and select the experiment

```python
mlflow.set_tracking_uri("http://127.0.0.1:5001")
mlflow.set_experiment("scan-quality")
```

The first call selects the tracking destination. The second selects the experiment, creating it if it does not already exist.

### Start a run

```python
with mlflow.start_run(run_name="random-forest-v1") as run:
    # Fit, evaluate and log within this context.
    ...
```

This groups subsequent logging under one execution. Normal context completion finishes the run; an exception can leave a failed run containing whatever was successfully logged before the error.

### Log configuration

```python
mlflow.log_params(parameters)

mlflow.log_params({
    "training_rows": len(train),
    "validation_rows": len(validation),
    "classification_threshold": 0.5,
})
```

The `parameters` dictionary contains our Random Forest configuration:

```python
parameters = {
    "n_estimators": 200,
    "max_depth": 6,
    "min_samples_leaf": 10,
    "random_state": 42,
    "n_jobs": -1,
}
```

These values document model settings and dataset sizes; logging them does not train the estimator.

### Log tags and dataset identities

```python
mlflow.set_tags({
    "candidate": "v1",
    "dataset_type": "synthetic",
    "train_sha256": file_hash(train_path),
    "validation_sha256": file_hash(validation_path),
})
```

Our helper computes a hash of each file's exact bytes:

```python
def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
```

A hash identifies a file version but does not upload or restore the data. CSV formatting or row-order changes can alter the hash even when the same examples are present.

### Log validation metrics

```python
mlflow.log_metrics(metrics)
```

The dictionary contains validation accuracy, precision, recall, F1, ROC AUC, log loss and majority-class baseline accuracy.

Your successful run reported:

| Metric | Value |
|---|---:|
| Validation accuracy | 0.6383 |
| Baseline accuracy | 0.5917 |
| Validation precision | 0.6540 |
| Validation recall | 0.8254 |
| Validation F1 | 0.7298 |
| Validation ROC AUC | 0.6737 |
| Validation log loss | 0.6306 |

MLflow stores the numbers we calculate. It does not decide whether these are sufficient for deployment.

### Log the trained model

```python
mlflow.sklearn.log_model(
    sk_model=model,
    name="scan_quality_model",
    signature=infer_signature(X_train, model.predict(X_train)),
    input_example=X_train.head(3),
    skops_trusted_types=["sklearn.tree._tree.Tree"],
)
```

| Argument | Purpose |
|---|---|
| `sk_model` | Fitted scikit-learn estimator |
| `name` | Name for the logged model |
| `signature` | Input/output schema inferred from example data |
| `input_example` | Small example showing expected inputs |
| `skops_trusted_types` | Specific type trusted during the installed serializer's model-saving check |

Our signature describes four-feature inputs and the output from `model.predict()`. It does not describe the full later FastAPI JSON response or enforce the API's 0–1 input bounds. Pydantic handles those bounds separately.

### Use the run ID for local artifacts

```python
output_dir = ROOT / "artifacts" / run.info.run_id
```

We also store `run.info.run_id` inside `metadata.json`. The API later returns that value as `model_version`.

### Log the metadata file

```python
mlflow.log_artifact(str(metadata_path))
```

This uploads our metadata JSON to the active run's artifact storage. It is separate from the local file remaining in the project directory.

## 6. Complete training example with the MLflow calls in context

This consolidates the training implementation already supplied in the project, including the serialization fix. It is documentation, not a request to rerun training or overwrite your current file. Running it creates a new training run and new local artifacts.

```python
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
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
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
    train_path = ROOT / "data" / "train.csv"
    validation_path = ROOT / "data" / "validation.csv"

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
            "validation_accuracy": accuracy_score(y_validation, predictions),
            "validation_precision": precision_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_recall": recall_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_f1": f1_score(
                y_validation, predictions, zero_division=0
            ),
            "validation_roc_auc": roc_auc_score(y_validation, probabilities),
            "validation_log_loss": log_loss(y_validation, probabilities),
        }

        majority_class = int(y_train.mode().iloc[0])
        metrics["baseline_accuracy"] = accuracy_score(
            y_validation,
            np.full(len(y_validation), majority_class),
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
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        mlflow.log_artifact(str(metadata_path))

        print("\nValidation results")
        for name, value in metrics.items():
            print(f"{name}: {value:.4f}")
        print(f"\nRun ID: {run.info.run_id}")
        print(f"Model saved in: {output_dir}")


if __name__ == "__main__":
    main()
```

### Threshold consistency note

This preserves the original validation use of `model.predict()`. The later API applies `probability >= metadata['threshold']`. They can differ at an exact 0.5 tie. For a future model version, use the same explicit threshold rule in validation, final evaluation and serving, and compute the positive-probability column through `model.classes_` as the API does.

## 7. Which operations belong to which library?

| Code | Owner | Effect |
|---|---|---|
| `model.fit(...)` | scikit-learn | Train the model |
| `model.predict_proba(...)` | scikit-learn | Produce probabilities |
| `accuracy_score(...)` | scikit-learn | Calculate an evaluation metric |
| `mlflow.log_metrics(...)` | MLflow | Store calculated metrics |
| `mlflow.sklearn.log_model(...)` | MLflow integration | Package and log the estimator |
| `joblib.dump(...)` | joblib | Save our local serving copy |
| `metadata_path.write_text(...)` | Python standard library | Write local metadata JSON |
| `mlflow.log_artifact(...)` | MLflow | Upload that JSON to run storage |

The training script uses explicit logging; it does not call `mlflow.autolog()`.

## 8. Why there are two saved model locations

| Location | Use |
|---|---|
| MLflow-managed model artifacts | Experiment inspection and retaining the logged candidate |
| `artifacts/<run_id>/model.joblib` | Direct local loading by our FastAPI service |

They are saved from the same fitted estimator during the successful execution, but use separate packaging paths. We did not implement a byte-level integrity check linking the local copy to the MLflow artifact.

The API startup code reads:

```python
run_id = os.environ["MODEL_RUN_ID"]
model_dir = ROOT / "artifacts" / run_id
model = joblib.load(model_dir / "model.joblib")
```

There is no tracking-server call in this loading path. The run ID provides traceability, while the local artifact provides the executable model.

A future design could download an approved artifact from a registry or artifact store during release preparation. That is not implemented in this version.

## 9. The model-saving error we fixed

Training reached MLflow's model-saving operation and failed with:

```text
Untrusted types found in the file: ['sklearn.tree._tree.Tree']
```

We added:

```python
skops_trusted_types=["sklearn.tree._tree.Tree"]
```

This was appropriate for the specific freshly trained model created by our own script. It was not a blanket approval for unknown artifacts or every type reported by a serializer.

The first failed execution had already created a run and logged earlier information. We reran training after the fix and obtained the successful run ID used by the API. Merely seeing a run in the UI does not prove its artifact-saving steps finished.

Because local joblib saving occurs after MLflow model logging, this failure prevented the later local artifact writes in that attempt.

## 10. Inspect and compare runs

In the local UI:

1. Open the `scan-quality` experiment.
2. Select the intended run, checking its ID and completion status.
3. Inspect parameters, validation metrics and tags.
4. Inspect the logged model and metadata artifacts.
5. Compare candidates using the same validation data and metrics appropriate to the task.

A higher accuracy alone is not a sufficient release decision. For our scan-quality classifier, accepting poor scans may be costly, so precision, recall, thresholds and the confusion matrix also matter.

Do not tune repeatedly on final test results. Candidate comparisons belong on validation data; the held-out test set should evaluate the selected version.

## 11. What is and is not captured automatically in our project

| Item | Current behavior |
|---|---|
| Random Forest parameters | Explicitly logged |
| Validation metrics | Explicitly logged |
| Dataset hashes | Explicitly logged as tags |
| Complete datasets | Not uploaded by these calls |
| Model and example inputs | Logged through scikit-learn integration |
| Local metadata JSON | Uploaded as an artifact |
| Source-control commit | Not explicitly recorded by our script |
| Complete immutable environment | Not guaranteed by this workflow |
| Final held-out test report | Saved locally by `evaluate_test.py`; not automatically logged to MLflow |
| Production predictions | Stored in SQLite, not MLflow |
| Drift scores and live latency | Prometheus/Grafana, not MLflow |
| Approval and promotion | Not implemented |

Do not confuse recording a seed and a dataset hash with fully reproducing an experiment. The actual data bytes, implementation, library versions and environment still need to be preserved.

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| Cannot connect to tracking URI | MLflow server running on port 5001 in the expected environment |
| Experiment appears empty | Correct tracking server and server working directory |
| Multiple runs have the same name | Compare unique run IDs; reruns create separate executions |
| Run exists but model is missing | Run status and errors before artifact logging completed |
| Untrusted tree-type error | Apply the specific trusted-type fix for the model we created |
| scikit-learn version warning | Use the training-compatible `mlops` environment |
| API fails despite MLflow being available | Check local artifact directory and `MODEL_RUN_ID`; API loads locally |

The scikit-learn version mismatch and the serializer trust error are different problems. Trusting a type does not fix an incompatible package version.

## 13. Interview explanation

> We used MLflow to track local training experiments. Each run recorded model parameters, validation metrics, dataset hashes and model artifacts. We saved the run ID with the serving metadata, so API predictions could be traced back to the training experiment. In the first version, FastAPI loaded a local joblib artifact, while Prometheus and Grafana handled live monitoring. Registry approvals and automated promotion were not yet implemented.

## References used during the project

- [MLflow scikit-learn API](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.sklearn.html)
- [MLflow documentation](https://mlflow.org/docs/latest/)
- [scikit-learn model persistence and compatibility](https://scikit-learn.org/stable/model_persistence.html)

The examples document our installed project behavior. Check compatibility before changing MLflow or model-library versions.
