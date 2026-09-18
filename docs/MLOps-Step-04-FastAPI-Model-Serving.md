# Step 4 — FastAPI model serving

## Purpose and scope

In Step 3, we created a saved Random Forest and metadata. In this step, we made that model accessible through an HTTP API, so another application can request predictions without running training code.

The API accepts four precomputed numerical features, not a scan video. It predicts synthetic scan quality, not heart rate or blood pressure.

This chapter documents the initial serving implementation. Your current `app/main.py` also contains prediction logging and monitoring added later. Do not replace that current file with the earlier minimal version shown here; it is included to explain the serving layer independently.

## 1. The serving contract

The client sends JSON to `POST /predict`:

```json
{
  "brightness": 0.5,
  "motion": 0.1,
  "face_visibility": 0.95,
  "signal_quality": 0.9
}
```

The response contains:

| Field | Type | Meaning |
|---|---|---|
| `prediction_id` | String | Unique identifier generated for this prediction |
| `model_version` | String | MLflow run ID identifying the loaded artifact |
| `prediction` | Integer | 0 for poor or 1 for acceptable |
| `label` | String | Human-readable class name |
| `acceptable_probability` | Float | Model estimate for class 1 |
| `threshold` | Float | Probability cutoff used for the decision |

Later we added `inference_ms` and stored prediction records. The initial serving implementation did not yet persist the prediction ID or expose operational metrics.

An HTTP 200 response means the API processed the request successfully. It does not mean the scan was classified as acceptable. A prediction of `poor` is still a successful API response.

## 2. Separate the server, application and model

| Component | Responsibility |
|---|---|
| Uvicorn | Listen for network requests and run the ASGI application |
| FastAPI | Route requests, validate input and serialize responses |
| Pydantic | Validate the JSON fields against the declared schema |
| pandas | Construct a named feature table in the training column order |
| scikit-learn estimator | Calculate predicted probabilities |
| joblib | Deserialize the saved model during startup |

Training is not part of this request path. Each request runs inference with an already fitted model.

MLflow also does not participate in every request. Our API loads the local joblib artifact directly; the MLflow server can be stopped after artifacts have been saved without preventing this serving implementation from working.

## 3. Select an explicit model artifact

We started the API with a specific model run ID:

```bash
conda activate mlops
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

The startup code resolves:

```python
ROOT = Path(__file__).resolve().parents[1]
run_id = os.environ["MODEL_RUN_ID"]
model_dir = ROOT / "artifacts" / run_id
```

For this release, the API reads:

```text
artifacts/d9571388a61f45369e4f709878e8246b/model.joblib
artifacts/d9571388a61f45369e4f709878e8246b/metadata.json
```

We deliberately select a run instead of automatically loading the newest directory. A new training run should not silently change the running service.

`MODEL_RUN_ID` is application configuration. It is not automatically set by activating Conda. An exported value exists in that terminal and its child processes; another terminal needs its own configuration.

## 4. Load the model through the startup lifecycle

We used FastAPI's `lifespan` hook:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    run_id = os.environ["MODEL_RUN_ID"]
    model_dir = ROOT / "artifacts" / run_id

    metadata = json.loads(
        (model_dir / "metadata.json").read_text()
    )
    model = joblib.load(model_dir / "model.joblib")

    if metadata["run_id"] != run_id:
        raise ValueError("Model version and metadata do not match")

    if list(model.feature_names_in_) != metadata["features"]:
        raise ValueError("Model features and metadata do not match")

    app.state.model = model
    app.state.metadata = metadata

    yield
```

The code before `yield` executes during startup, before the server accepts application requests. The application then serves requests until shutdown. Code after `yield`, if provided, would perform cleanup.

### Why load once?

Loading inside `/predict` would repeatedly read and deserialize the artifact. Instead, the model remains in process memory and is shared between requests handled by that process.

“Once” means once per worker process startup. Multiple Uvicorn workers load separate copies and use additional memory. Our first version uses one worker.

Changing the model file on disk does not automatically update the object already loaded in memory. The service must restart or implement an explicit reload mechanism. Our release process uses container replacement.

### Startup checks and their limits

The checks confirm that the selected run matches the metadata and that the feature names/order agree. They do not cryptographically verify that the model bytes belong to that run, nor do they validate every possible metadata error.

Only load trusted joblib artifacts. Deserialization is not a safe way to inspect arbitrary untrusted models.

## 5. Validate inputs before inference

Our input schema is:

```python
class ScanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    brightness: float = Field(ge=0, le=1)
    motion: float = Field(ge=0, le=1)
    face_visibility: float = Field(ge=0, le=1)
    signal_quality: float = Field(ge=0, le=1)
```

| Rule | Effect |
|---|---|
| No defaults | All four fields are required |
| `float` | Values must be interpretable as floating-point numbers |
| `ge=0`, `le=1` | Bounds are inclusive |
| `extra="forbid"` | Unexpected fields are rejected |
| `allow_inf_nan=False` | Non-finite numeric values are rejected |

Examples of invalid requests include a missing motion value, brightness of `-0.1`, motion of `1.1`, or an unexpected field.

We did not enable strict numeric typing. Depending on Pydantic's supported coercions, a numeric string such as `"0.5"` can be converted to a float. The schema should not be described as accepting only JSON numeric tokens.

FastAPI validates the request before executing the prediction function. Invalid inputs normally return HTTP 422 with details about the invalid fields.

Valid ranges do not prove semantic consistency. If training brightness represents normalized image luminance but production brightness represents a different measurement, values may pass validation while meaning something different. Preventing that training-serving mismatch requires a shared feature definition and, where applicable, shared preprocessing code.

## 6. Construct the feature table

The prediction function uses:

```python
features = pd.DataFrame(
    [scan.model_dump()],
    columns=metadata["features"],
)
```

`model_dump()` converts the validated Pydantic object to a dictionary. Wrapping it in a list creates one row. Passing `columns` enforces the feature order saved during training.

For a single request, the resulting shape is `(1, 4)`.

This also means the client's JSON key order does not affect the model input order. The metadata controls the order explicitly.

Our model was fitted using a pandas DataFrame, so it stores `feature_names_in_`. Named columns help detect accidental feature mismatches.

## 7. Calculate probabilities and apply the decision threshold

```python
acceptable_index = list(model.classes_).index(1)
probability = float(
    model.predict_proba(features)[0, acceptable_index]
)

threshold = metadata["threshold"]
prediction = int(probability >= threshold)
```

`predict_proba` returns one row per input and one column per class. Looking up class 1 avoids relying on an undocumented assumption about column position.

We convert NumPy values to ordinary Python `float` and `int` types for a straightforward JSON response.

With threshold 0.5:

| Estimated acceptable probability | Decision |
|---:|---|
| 0.30 | Poor |
| 0.50 | Acceptable |
| 0.80 | Acceptable |

The threshold is a decision rule separate from the learned estimator. Changing it changes the precision/recall trade-off and should be evaluated and versioned. We did not implement manual review or a third output class.

The exact tie rule is `>=`. The original training script used `model.predict()`, which can select class 0 for an exact probability tie. Future training versions should apply the same explicit threshold convention during validation and serving.

## 8. Return a traceable response

```python
return {
    "prediction_id": str(uuid4()),
    "model_version": metadata["run_id"],
    "prediction": prediction,
    "label": metadata["class_labels"][str(prediction)],
    "acceptable_probability": probability,
    "threshold": threshold,
}
```

The UUID allows us to identify one prediction. The run ID identifies the loaded model version. These are different identifiers: many predictions can share one model version.

The class-label dictionary uses string keys because JSON object keys are strings. That is why the lookup uses `str(prediction)`.

At this initial stage, returning a UUID does not create a durable record. We add SQLite storage in the next chapter. Similarly, a unique ID does not provide retry deduplication: retrying the same request creates another ID and prediction event.

We return a dictionary without a declared response model in this minimal implementation. A production API can add a Pydantic response schema to make the output contract explicit and enforce it.

## 9. Why the prediction endpoint uses `def`

The route is a normal synchronous function:

```python
@app.post("/predict")
def predict(scan: ScanInput):
    ...
```

The scikit-learn computation is synchronous. FastAPI runs synchronous endpoint functions in a thread pool so that this work is not executed directly on the asynchronous event loop.

This does not provide unlimited inference capacity. Model execution, request threads, CPU resources and the forest's internal parallelism still compete. Concurrency must be measured for the actual model and deployment environment.

The startup function is `async def` because it implements the lifespan context manager. That does not make `joblib.load()` itself asynchronous; it runs during startup before serving begins.

## 10. Initial serving implementation

This is the initial `app/main.py` before adding SQLite logging, timing metrics and middleware. Use it as a reference, not as a replacement for your current instrumented file.

```python
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import joblib
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_id = os.environ["MODEL_RUN_ID"]
    model_dir = ROOT / "artifacts" / run_id

    metadata = json.loads(
        (model_dir / "metadata.json").read_text()
    )
    model = joblib.load(model_dir / "model.joblib")

    if metadata["run_id"] != run_id:
        raise ValueError("Model version and metadata do not match")

    if list(model.feature_names_in_) != metadata["features"]:
        raise ValueError("Model features and metadata do not match")

    app.state.model = model
    app.state.metadata = metadata
    yield


app = FastAPI(title="Scan Quality API", lifespan=lifespan)


class ScanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    brightness: float = Field(ge=0, le=1)
    motion: float = Field(ge=0, le=1)
    face_visibility: float = Field(ge=0, le=1)
    signal_quality: float = Field(ge=0, le=1)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_version": app.state.metadata["run_id"],
    }


@app.post("/predict")
def predict(scan: ScanInput):
    model = app.state.model
    metadata = app.state.metadata

    features = pd.DataFrame(
        [scan.model_dump()],
        columns=metadata["features"],
    )

    acceptable_index = list(model.classes_).index(1)
    probability = float(
        model.predict_proba(features)[0, acceptable_index]
    )

    threshold = metadata["threshold"]
    prediction = int(probability >= threshold)

    return {
        "prediction_id": str(uuid4()),
        "model_version": metadata["run_id"],
        "prediction": prediction,
        "label": metadata["class_labels"][str(prediction)],
        "acceptable_probability": probability,
        "threshold": threshold,
    }
```

## 11. Start the server

From the project root:

```bash
conda activate mlops
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b

python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

| Command part | Meaning |
|---|---|
| `python -m uvicorn` | Run Uvicorn using the selected Python environment |
| `app.main` | Import the `main` module from the `app` directory |
| `:app` | Use the FastAPI object named `app` in that module |
| `--host 127.0.0.1` | Listen on the local loopback interface |
| `--port 8001` | Accept requests on port 8001 |

Later we used `0.0.0.0` when a Docker-hosted Prometheus needed to access a host-run API. Inside the final API container we also listen on `0.0.0.0`, while Compose publishes the port on the Mac's loopback address. The container bind address and host port-publishing address are separate settings.

Do not start a second server on port 8001 if your existing local or Docker service already uses it.

## 12. Verify the API contract

### Health

```bash
curl http://127.0.0.1:8001/health
```

Expected model version:

```json
{
  "status": "ok",
  "model_version": "d9571388a61f45369e4f709878e8246b"
}
```

Startup must complete before this endpoint is served. However, this simple check does not exercise prediction, validate accuracy, or check prediction-storage availability.

### Valid prediction

```bash
curl -i -X POST http://127.0.0.1:8001/predict \
  -H "Content-Type: application/json" \
  -d '{
    "brightness": 0.5,
    "motion": 0.1,
    "face_visibility": 0.95,
    "signal_quality": 0.9
  }'
```

Expect HTTP 200 and the documented response fields. The actual probability comes from the saved model; it is not the label generator's probability formula.

### Invalid prediction

```bash
curl -i -X POST http://127.0.0.1:8001/predict \
  -H "Content-Type: application/json" \
  -d '{
    "brightness": 0.5,
    "motion": 1.5,
    "face_visibility": 0.95,
    "signal_quality": 0.9
  }'
```

Expect HTTP 422 because motion exceeds 1. No model prediction should be executed for this invalid payload.

### Interactive documentation

Open `http://127.0.0.1:8001/docs`. FastAPI generates this interface from the declared endpoints and request schema.

These commands are manual smoke checks. Later, pytest compares API predictions with offline model predictions on validation examples and verifies invalid-input behavior automatically.

## 13. Troubleshooting encountered in our project

| Symptom | Cause or first check | Action |
|---|---|---|
| `KeyError: MODEL_RUN_ID` | Variable absent from the launching terminal | Export it in that terminal and restart |
| `InconsistentVersionWarning` | Model loaded with a different scikit-learn version | Use the `mlops` environment that trained the model |
| Model file not found | Incorrect run ID or incomplete training save | Inspect the corresponding artifact directory |
| Address already in use | Another process or container owns port 8001 | Identify and use or stop the existing service |
| HTTP 422 | Input schema validation failed | Read the response detail and correct the payload |
| Startup feature mismatch | Metadata feature order differs from the estimator | Restore matching artifacts instead of bypassing the check |

Do not interpret a successful import or HTTP health response as validation of the model's predictive quality.

## 14. Boundaries of this first API

This initial implementation has no authentication, rate limiting, video processing, asynchronous job queue or online model reload. Those are separate requirements, not implicit features of FastAPI.

Our app makes a classification decision; an external caller would decide what to do with it, such as requesting another scan. We did not build that frontend flow in this exercise.

## Completion criteria

- The API loads the selected model and matching metadata during startup.
- `/health` reports the intended model run ID.
- Valid requests return a probability and class decision.
- Invalid feature values are rejected before inference.
- Responses carry a prediction ID and model version for later traceability.

The next chapter is **Step 5 — SQLite prediction logging, failure handling and measuring logging overhead**.

## References used during the project

- [FastAPI lifespan and model-loading example](https://fastapi.tiangolo.com/advanced/events/)
- [Pydantic field definitions](https://docs.pydantic.dev/latest/concepts/fields/)
- [FastAPI tests that run the startup lifecycle](https://fastapi.tiangolo.com/advanced/testing-events/)
