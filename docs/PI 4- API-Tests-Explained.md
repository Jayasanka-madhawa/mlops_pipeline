# How Our API Tests Work

**Our API tests check that FastAPI works correctly with the selected model before Jenkins builds the Docker image.**

## 1. Jenkins starts the tests

```bash
conda run -n mlops python -m pytest tests/test_api.py -q \
  --junitxml=test-results/api-tests.xml
```

`MODEL_RUN_ID` is already set by the pipeline, so the API loads that run's model from the Jenkins workspace. The release-bundle stage has copied the model and supporting files there before testing begins.

| Command part | Meaning |
|---|---|
| `conda run -n mlops` | Use the project's Python environment |
| `python -m pytest` | Run the test runner |
| `tests/test_api.py` | Run the API test file |
| `-q` | Show compact output |
| `--junitxml=...` | Save a report Jenkins can display |

## 2. Tests create a temporary API client

We use FastAPI's `TestClient`. Conceptually:

```python
with TestClient(app) as client:
    response = client.get("/health")
```

It sends requests directly to the application inside the test process. **No separate Uvicorn server or open network port is needed.**

Entering the client context runs the application's startup logic, including loading the model. Leaving the context runs its shutdown logic.

## 3. What do our eight tests check?

| Test | What it verifies |
|---|---|
| Health | `/health` responds successfully and identifies the selected model |
| Prediction consistency | API predictions match predictions calculated directly using the model |
| Invalid inputs × 4 | Invalid feature values are rejected |
| Missing feature | An incomplete request is rejected |
| Logging failure | A prediction still returns if saving the record fails |

The invalid-input test is parameterized: the same test logic runs for four input cases. That is why the suite reports eight tests in total.

## 4. How do we check predictions?

We take sample rows from the validation data and calculate predictions in two ways:

1. **Offline:** call the model directly.
2. **Through the API:** send the same features to `/predict`.

Then compare the probabilities and predicted classes, using the configured threshold.

For example, both routes receive the same brightness, motion, face visibility, and signal quality values. Their probabilities should agree within the test's numerical tolerance, and their class decisions should match.

This catches errors such as **wrong feature order, incorrect threshold handling, or incorrect response values**.

It checks serving consistency—not whether the model is accurate against real labels. Model quality is evaluated separately using labeled data.

## 5. How do we test failures?

### Invalid or missing inputs

A test sends a bad request and checks for a validation response such as HTTP `422`.

This verifies that the API enforces its input requirements before passing unsuitable data to the model.

### Logging failure

We make the logging function deliberately raise an exception. The test checks that:

- The API still returns a prediction.
- The logging-failure metric increases.

This verifies our intended behavior: a failed prediction-log write should not discard an otherwise successful prediction.

### Database isolation

Database initialization and normal prediction writes are mocked in these tests, so the tests do not add predictions to your real database.

The trained model is real; the database operations are replaced for the test. Actual container logging is checked later by the smoke test.

## 6. What does Jenkins do with the result?

- **All pass:** proceeds to build the Docker image.
- **Any fail:** stops the later stages.
- **JUnit XML report:** displays passed and failed tests in the Jenkins interface.

The Jenkinsfile publishes the report with:

```groovy
post {
    always {
        junit 'test-results/api-tests.xml'
    }
}
```

This attempts to publish the report even if tests fail, provided the report was generated.

## 7. API tests versus other checks

| Check | Main question |
|---|---|
| API tests | Does the application handle requests correctly with this model? |
| Model evaluation | How well does the model predict known labels? |
| Container smoke test | Does the built Docker image start, predict, and save a record? |
| Load test | What throughput and latency do we see under concurrent requests? |

Our current Jenkins pipeline runs the API tests and container smoke test. The separate load-test script is not currently a Jenkins stage.

## 8. Running the API tests manually

From your project directory, select a model whose artifacts and validation data are available:

```bash
conda activate mlops
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
python -m pytest tests/test_api.py -q
```

Replace the ID when testing another model. Use the `mlops` environment to avoid the scikit-learn version mismatch previously encountered in the base environment.

These tests run against the application in the test process, not against your currently deployed Kubernetes API.
