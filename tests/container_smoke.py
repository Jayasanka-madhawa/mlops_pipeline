import json
import sqlite3
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

expected_model = sys.argv[1]
base_url = "http://127.0.0.1:8001"


def get_json(path):
    with urlopen(base_url + path, timeout=2) as response:
        return json.load(response)


# Wait up to approximately 60 seconds for startup.
deadline = time.monotonic() + 60

while True:
    try:
        health = get_json("/health")
        break
    except (URLError, TimeoutError):
        if time.monotonic() >= deadline:
            raise RuntimeError("API did not become ready")
        time.sleep(1)

assert health["status"] == "ok"
assert health["model_version"] == expected_model

payload = {
    "brightness": 0.5,
    "motion": 0.1,
    "face_visibility": 0.95,
    "signal_quality": 0.9,
}

request = Request(
    base_url + "/predict",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urlopen(request, timeout=10) as response:
    assert response.status == 200
    result = json.load(response)

assert result["model_version"] == expected_model
assert 0 <= result["acceptable_probability"] <= 1
assert result["prediction"] == int(
    result["acceptable_probability"] >= result["threshold"]
)
assert result["label"] == {
    0: "poor",
    1: "acceptable",
}[result["prediction"]]

# Verify that a successful response was actually stored.
connection = sqlite3.connect(
    "file:/app/data/predictions.db?mode=ro",
    uri=True,
)
try:
    row = connection.execute(
        """
        SELECT model_version, prediction, probability
        FROM predictions
        WHERE prediction_id = ?
        """,
        (result["prediction_id"],),
    ).fetchone()
finally:
    connection.close()

assert row is not None, "Prediction was not saved"
assert row[0] == expected_model
assert row[1] == result["prediction"]
assert abs(row[2] - result["acceptable_probability"]) < 1e-12

print("Container smoke test passed")
print(json.dumps(result, indent=2))