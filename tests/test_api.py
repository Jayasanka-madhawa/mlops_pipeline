import os
import sqlite3
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.main as api


@pytest.fixture
def client(monkeypatch):
    # Avoid writing test records into the monitoring database.
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
    # Use validation examples, preserving the held-out test set.
    rows = pd.read_csv(
        api.ROOT / "data" / "validation.csv"
    ).head(10)

    metadata = api.app.state.metadata
    model = api.app.state.model
    features = metadata["features"]

    positive_index = list(model.classes_).index(1)
    expected = model.predict_proba(rows[features])[:, positive_index]

    for (_, row), probability in zip(rows.iterrows(), expected):
        response = client.post(
            "/predict",
            json=row[features].to_dict(),
        )

        assert response.status_code == 200
        result = response.json()
        label = int(probability >= metadata["threshold"])

        assert result["acceptable_probability"] == pytest.approx(
            float(probability)
        )
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