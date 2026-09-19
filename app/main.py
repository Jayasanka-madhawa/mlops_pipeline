import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import joblib
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

import logging
from datetime import datetime, timezone
from time import perf_counter

from app.prediction_log import init_db, save_prediction

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import (
    REQUESTS,
    API_DURATION,
    INFERENCE_DURATION,
    DB_DURATION,
    LOG_FAILURES,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]




@asynccontextmanager
async def lifespan(app: FastAPI):
    # Select an explicit model version when starting the API.
    run_id = os.environ["MODEL_RUN_ID"]
    model_dir = ROOT / "artifacts" / run_id

    # Load the model and its matching metadata once at startup.
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

    init_db()


    yield


app = FastAPI(
    title="Scan Quality API",
    lifespan=lifespan,
)


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
        "application_version": "v2",
    }


@app.post("/predict")
def predict(scan: ScanInput):
    started = perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()

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
    prediction_id = str(uuid4())

    # Includes feature preparation and prediction, excludes DB writing.
    inference_ms = (perf_counter() - started) * 1000
    INFERENCE_DURATION.observe(inference_ms / 1000)

    record = {
        "prediction_id": prediction_id,
        "timestamp": timestamp,
        "model_version": metadata["run_id"],
        **scan.model_dump(),
        "prediction": prediction,
        "probability": probability,
        "threshold": threshold,
        "inference_ms": inference_ms,
    }

    try:
        with DB_DURATION.time():
            save_prediction(record)
    except Exception:
        LOG_FAILURES.inc()
        logger.exception(
            "Failed to store prediction %s", prediction_id
        )

    return {
        "prediction_id": prediction_id,
        "model_version": metadata["run_id"],
        "prediction": prediction,
        "label": metadata["class_labels"][str(prediction)],
        "acceptable_probability": probability,
        "threshold": threshold,
        "inference_ms": inference_ms,
    }

@app.middleware("http")
async def monitor_prediction_requests(request: Request, call_next):
    # Exclude health checks, docs and metrics scraping.
    if request.url.path != "/predict":
        return await call_next(request)

    started = perf_counter()
    status = "500"

    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        REQUESTS.labels(status=status).inc()
        API_DURATION.observe(perf_counter() - started)


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(
        content=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )