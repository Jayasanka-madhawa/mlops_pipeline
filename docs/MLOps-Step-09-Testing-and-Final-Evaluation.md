# Step 9 — API testing, load checks and final model evaluation

## Purpose and scope

This step verifies three different properties before packaging a release:

1. The API behaves according to its contract.
2. The running service handles a small concurrent workload.
3. The selected model performs acceptably for our learning exercise on held-out examples.

These are separate checks. Passing API tests does not establish prediction quality. High accuracy does not establish reliable HTTP behavior. A brief local load test does not establish production capacity.

This chapter includes the implementation supplied during the project, the actual outputs you shared, and the limits of what those outputs establish. Do not overwrite existing files merely to repeat the documentation.

## 1. Select the correct environment and model

Our model was trained in the `mlops` Conda environment. The tests and offline evaluation need that environment plus an explicit model run ID:

```bash
conda activate mlops
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

Verify the interpreter and model library:

```bash
python -c "import sys, sklearn; print(sys.executable); print(sklearn.__version__)"
```

For the recorded artifact, training used scikit-learn `1.9.1`. Your base environment had `1.8.0`, which produced model-persistence warnings when it loaded the artifact.

To select the environment explicitly without relying on activation:

```bash
conda run -n mlops python -m pytest tests/test_api.py -q
conda run -n mlops python training/evaluate_test.py
```

The exported run ID is still required. Conda environment selection and model selection solve different configuration problems.

## 2. Test the API in process

We created `tests/test_api.py` using pytest and FastAPI's `TestClient`.

`TestClient` exercises the application without launching a network listener. Using it as a context manager runs the lifespan startup, which loads the real saved model and metadata.

| Dependency | Used in these tests |
|---|---|
| Saved model | Real artifact |
| Model metadata | Real file |
| Validation examples | Real local CSV |
| HTTP networking | In-process test client, not a real TCP connection |
| SQLite initialization | Mocked out |
| Prediction storage | Mocked |

These are application-level tests with real model integration and mocked persistence. They are not complete end-to-end deployment tests.

## 3. What the eight test cases cover

| Test | Number of cases | Assertion |
|---|---:|---|
| Health endpoint | 1 | HTTP 200 and selected model version |
| API/offline consistency | 1 | Ten validation examples produce matching probabilities and decisions |
| Invalid inputs | 4 | Bounds, invalid numeric content and unexpected fields return 422 |
| Missing feature | 1 | Missing motion is rejected before storage |
| Storage failure | 1 | Prediction still succeeds and failure counter increments |

The parameterized invalid-input test expands into four cases, bringing the total to eight.

The consistency test uses validation data rather than the reserved test split. Its purpose is to detect serving mistakes such as incorrect feature order or class-probability selection, not to estimate generalization accuracy.

## 4. API test implementation

```python
import os
import sqlite3
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.main as api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "init_db", lambda: None)
    monkeypatch.setattr(api, "save_prediction", Mock())

    with TestClient(api.app) as test_client:
        yield test_client


def valid_scan():
    return {
        "brightness": 0.5,
        "motion": 0.1,
        "face_visibility": 0.95,
        "signal_quality": 0.9,
    }


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["model_version"] == os.environ["MODEL_RUN_ID"]


def test_api_matches_offline_predictions(client):
    rows = pd.read_csv(api.ROOT / "data" / "validation.csv").head(10)
    metadata = api.app.state.metadata
    model = api.app.state.model
    features = metadata["features"]

    positive_index = list(model.classes_).index(1)
    expected = model.predict_proba(rows[features])[:, positive_index]

    for (_, row), probability in zip(rows.iterrows(), expected):
        response = client.post("/predict", json=row[features].to_dict())
        assert response.status_code == 200
        result = response.json()
        label = int(probability >= metadata["threshold"])

        assert result["acceptable_probability"] == pytest.approx(float(probability))
        assert result["prediction"] == label
        assert result["label"] == metadata["class_labels"][str(label)]
        assert result["model_version"] == os.environ["MODEL_RUN_ID"]

    assert api.save_prediction.call_count == 10


@pytest.mark.parametrize(
    "change",
    [
        {"brightness": -0.1},
        {"motion": 1.1},
        {"signal_quality": "invalid"},
        {"unexpected_field": 10},
    ],
)
def test_invalid_inputs(client, change):
    payload = valid_scan()
    payload.update(change)
    response = client.post("/predict", json=payload)
    assert response.status_code == 422
    api.save_prediction.assert_not_called()


def test_missing_feature(client):
    payload = valid_scan()
    del payload["motion"]
    assert client.post("/predict", json=payload).status_code == 422
    api.save_prediction.assert_not_called()


def test_logging_failure_still_returns_prediction(client):
    api.save_prediction.side_effect = sqlite3.OperationalError(
        "Simulated database failure"
    )
    before = api.LOG_FAILURES._value.get()

    response = client.post("/predict", json=valid_scan())

    assert response.status_code == 200
    assert api.LOG_FAILURES._value.get() == before + 1
```

### Why patch the API module?

`app.main` imports `save_prediction` into its own namespace. We patch `api.save_prediction`, which is the reference actually called by the endpoint. Patching only the original function in its defining module would not necessarily replace an already imported reference.

Each fixture invocation installs a fresh mock. Pytest restores the patches afterward. Tests therefore do not insert prediction rows into the real monitoring database.

The metric collector itself remains process-global, so the failure test compares the before and after values rather than assuming the counter starts at zero.

### Private metric attribute

The original test reads `LOG_FAILURES._value`, which is an implementation detail of the Prometheus client. It is convenient for the exercise but couples the test to client internals. A future refinement can inspect exported metric samples through the registry's public collection interface instead.

### Limits of consistency checks

Both offline and API predictions use the same loaded model object and library environment. This verifies request-to-model wiring; it cannot prove that the artifact behaves identically to the original training environment. That is why dependency-version matching remains necessary even when the test passes.

## 5. Run and interpret the API tests

From the project root:

```bash
python -m pytest tests/test_api.py -q
```

No Uvicorn server is required. Your separately running service, if any, is not the service being exercised by this command.

We encountered two setup problems:

### Missing model configuration

All eight cases initially failed during startup with:

```text
KeyError: 'MODEL_RUN_ID'
```

The assertions had not run yet. Exporting the variable in the test terminal resolved that setup error.

### Wrong Python environment

You then shared:

```text
8 passed, 16 warnings in 37.61s
```

The prompt showed `(base)`, and the warnings reported scikit-learn `1.8.0` loading estimators saved with `1.9.1`.

We corrected the commands to use `conda run -n mlops`. You later confirmed completion, but did not paste the corrected pytest output. The record therefore contains the initial pass-with-warnings output and your completion confirmation, not a separately displayed clean-run transcript.

## 6. Run a small concurrent load check

Unlike TestClient, the load script sends real HTTP requests to the running API on port 8001.

We selected:

```text
Requests: 100
Maximum concurrent client tasks: 5
```

Payloads follow the normal input distributions. They enter the real SQLite database, update API metrics and can contribute to the drift monitor's rolling window.

This is a short concurrency smoke check. It does not include a controlled warm-up, long steady-state run, stepped capacity search or a predefined latency service-level objective.

## 7. Load-test implementation

File: `tests/load_test.py`.

```python
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

URL = "http://127.0.0.1:8001/predict"
REQUEST_COUNT = 100
CONCURRENCY = 5
rng = np.random.default_rng(2027)

payloads = [
    {
        "brightness": float(rng.beta(5, 5)),
        "motion": float(rng.beta(2, 8)),
        "face_visibility": float(rng.beta(9, 2)),
        "signal_quality": float(rng.beta(5, 2)),
    }
    for _ in range(REQUEST_COUNT)
]


def send(payload):
    started = time.perf_counter()
    try:
        response = requests.post(URL, json=payload, timeout=10)
        status = str(response.status_code)
    except requests.RequestException as error:
        status = type(error).__name__

    duration_ms = (time.perf_counter() - started) * 1000
    return status, duration_ms


started = time.perf_counter()
with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
    results = list(pool.map(send, payloads))

elapsed = time.perf_counter() - started
statuses = Counter(status for status, _ in results)
successful_times = [
    duration for status, duration in results if status == "200"
]

print(f"Requests: {REQUEST_COUNT}")
print(f"Concurrency: {CONCURRENCY}")
print(f"Results: {dict(statuses)}")
print(f"Elapsed: {elapsed:.2f} seconds")
print(f"Successful throughput: {len(successful_times) / elapsed:.2f} req/s")

if successful_times:
    print(f"Average latency: {np.mean(successful_times):.2f} ms")
    print(f"P95 latency: {np.percentile(successful_times, 95):.2f} ms")

if len(successful_times) != REQUEST_COUNT:
    raise SystemExit("Load check failed: some requests were unsuccessful.")
```

Run while FastAPI is serving:

```bash
conda run -n mlops python tests/load_test.py
```

### What the timer measures

The per-request timer starts when a worker begins `send`, so it excludes waiting for a worker slot in the executor queue. It includes the HTTP call and response reading from the client's perspective.

The overall elapsed time covers the executor workload. Successful throughput is successful response count divided by that total duration.

The script computes latency statistics only for HTTP 200 responses and reports errors separately. It does not validate every response body's schema or record persistence. The API tests cover selected response behavior; database metrics and record checks cover persistence.

Each call uses `requests.post` directly, not a persistent session reused across calls. Connection setup is therefore part of this particular workload's behavior.

## 8. Actual load-test results

You shared:

| Measurement | Observed value |
|---|---:|
| Requests | 100 |
| Concurrency | 5 |
| HTTP 200 responses | 100 |
| Elapsed time | 1.04 seconds |
| Successful throughput | 96.45 requests/second |
| Mean latency | 51.07 ms |
| P95 latency | 87.55 ms |

Throughput uses the full-precision elapsed time internally, while the printed elapsed value is rounded. Minor differences when recomputing from the displayed duration are expected.

P95 means approximately 95% of the successful observed durations were at or below that value. It is not the slowest request and not a guarantee for future requests.

This output does not prove the API sustained 96 requests/second over a long period. Also, a client launched from the base environment does not determine the environment of an already running server; the server's launch environment must be checked independently.

Because logging failures are caught inside the API, all HTTP responses can be 200 even if some database writes fail. Inspect `scan_prediction_log_failures_total` and API logs alongside this result.

## 9. Evaluate the fixed model on the held-out test set

The final evaluation reads the saved model and metadata. It does not call `fit`, select hyperparameters or change the decision threshold.

Inputs:

| Input | Use |
|---|---|
| `model.joblib` | Selected fitted estimator |
| `metadata.json` | Features and threshold |
| `data/test.csv` | Held-out 1,200 examples |
| `data/train.csv` | Determine the majority-class baseline |

The baseline class comes from training labels, not the test majority. The model and baseline are then evaluated on the same test labels.

The evaluation explicitly applies `probability >= threshold`, matching the serving rule. This avoids relying on the estimator's exact tie behavior in `predict()`.

## 10. Evaluation implementation

File: `training/evaluate_test.py`.

```python
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
```

This writes a local report beside the model. It does not automatically log final test metrics back to MLflow. Re-running the script overwrites this report for the selected run; it is not an append-only evaluation history.

The test SHA-256 identifies exact CSV bytes. The report does not capture all dependency versions or the evaluator's source revision. Those are useful additions for stronger release provenance.

## 11. Interpret the results you shared

The pasted evaluation output was produced in the base environment with version warnings:

| Metric | Displayed result |
|---|---:|
| Test accuracy | 0.6483 |
| Baseline accuracy | 0.5917 |
| Precision | 0.6640 |
| Recall | 0.8211 |
| F1 | 0.7343 |
| ROC AUC | 0.6795 |
| Log loss | 0.6299 |

You later confirmed completion after being asked to rerun with `mlops`, but did not provide its metric output. These numbers are therefore documented as the displayed initial results, not independently confirmed corrected-environment values. Use the corrected report on your machine as the final release evidence.

For those displayed results, accuracy exceeded the baseline by approximately 5.66 percentage points. The validation accuracy was 0.6383, so the two reported estimates were fairly close; this alone does not prove absence of overfitting or establish clinical suitability.

## 12. Read the confusion matrix

The displayed matrix was:

```text
[[195 295]
 [127 583]]
```

Rows are actual labels and columns are predictions, with class order `[0, 1]`:

| Actual / Predicted | Poor (0) | Acceptable (1) |
|---|---:|---:|
| Poor (0) | 195 true negatives | 295 false positives |
| Acceptable (1) | 127 false negatives | 583 true positives |

Derived interpretations for this displayed matrix:

- Correct predictions: `195 + 583 = 778` out of 1,200.
- Accepted predictions: `295 + 583 = 878`.
- Precision: `583 / 878`, about 66.4%.
- Recall: `583 / (583 + 127)`, about 82.1%.
- Poor scans accepted: `295 / (195 + 295)`, about 60.2% of actual poor scans.

The last figure is the false-positive rate. It differs from `1 - precision`, which describes the poor fraction among accepted predictions.

These results support an infrastructure-learning exercise, not a decision that the model is safe for a real scan-quality workflow. Any real acceptance criteria must reflect the actual cost of accepting poor scans versus rejecting good ones.

## 13. Protect the test-set boundary

The held-out set provides an evaluation of the selected candidate under the dataset assumptions. If we repeatedly tune thresholds or parameters based on this test report, it becomes part of model selection and loses its role as an untouched final check.

Use validation data for iteration. If substantial development is guided by test outcomes, obtain a new independent final evaluation set for a meaningful final assessment.

These synthetic splits also do not establish robustness across real users, devices, environments or time. That would require representative data and appropriate group or temporal splitting.

## 14. Verification coverage and remaining gaps

| Check | What it supports | What it does not establish |
|---|---|---|
| Eight API cases | Selected contract behavior and model-serving consistency | Exhaustive input coverage or live networking |
| Mocked save failure | Intended response and error-counter behavior | Actual SQLite transaction reliability |
| 100-request load check | Brief operation at five concurrent tasks | Sustained capacity, resilience or an SLA |
| Held-out evaluation | Offline metrics for this fixed synthetic dataset | Production accuracy or drift impact |
| Environment correction | Avoid known model-library mismatch | A complete immutable dependency lock |

We did not configure an automated quality gate with domain-approved metric thresholds. Passing the scripts is not equivalent to an approved production release.

## Completion criteria

- Tests run using the intended interpreter and model artifacts.
- No model-library version mismatch is ignored.
- Valid requests and invalid requests behave as expected.
- The concurrent smoke check reports response outcomes and latency.
- Logging failures are assessed separately from HTTP success.
- Final evaluation uses the fixed model and threshold and saves a traceable report.

The next chapter is **Step 10 — Docker packaging, Compose deployment, release selection and rollback**.

## Official references used during implementation

- [FastAPI tests with lifespan execution](https://fastapi.tiangolo.com/advanced/testing-events/)
- [scikit-learn ROC AUC](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.roc_auc_score.html)
- [scikit-learn model persistence and version compatibility](https://scikit-learn.org/stable/model_persistence.html)
