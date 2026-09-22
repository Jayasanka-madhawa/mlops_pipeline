# Question: How does my API work during testing before we build the Docker image?

**The test starts your FastAPI application inside Python using `TestClient`.** It does not need Docker or a separately running server.

## Example

This simplified example shows how a test sends a prediction request:

```python
from fastapi.testclient import TestClient
from app.main import app


def test_prediction():
    with TestClient(app) as client:
        response = client.post(
            "/predict",
            json={
                "brightness": 0.5,
                "motion": 0.1,
                "face_visibility": 0.95,
                "signal_quality": 0.9,
            },
        )

        assert response.status_code == 200
        assert "prediction" in response.json()
```

The example assumes dependencies, model files, and `MODEL_RUN_ID` are available. Our actual test suite also mocks database operations; those fixtures are omitted here to keep the example focused.

## What happens step by step?

1. **Import `app`:** loads your FastAPI application code.
2. **Enter `with TestClient(app)`:** runs startup logic, loading the model selected by `MODEL_RUN_ID`.
3. **Call `client.post("/predict", ...)`:** passes the request directly into FastAPI within the test process.
4. **FastAPI processes it:** validates the input, runs your actual prediction function, and creates the response.
5. **Assertions check the response:** a failed assertion makes the test fail.
6. **Exit the context:** runs the application's shutdown logic.

**The API logic and model are real. The test client replaces the network connection.**

That is why you do not need to start a server with this command before these tests:

```bash
uvicorn app.main:app
```

## How does this work in our Jenkins pipeline?

Before testing, Jenkins checks out the application code, sets `MODEL_RUN_ID`, and copies the selected model bundle into its workspace.

It then runs:

```bash
conda run -n mlops python -m pytest tests/test_api.py -q
```

Python and the required packages are already available in the Mac agent's `mlops` Conda environment. Docker is not needed to execute this stage.

Our actual tests mock database initialization and prediction writes so they do not add test records to the real database. They still load the real model and exercise the API's request handling.

## Why test again after building the image?

| Test | What it checks |
|---|---|
| API tests with `TestClient` | Application behavior with the selected model |
| Container smoke test | The Docker image includes the required files and dependencies, starts successfully, predicts, and saves a record |

Passing the first tests does not prove that Docker packaging is correct. The later smoke test checks the actual built container.
