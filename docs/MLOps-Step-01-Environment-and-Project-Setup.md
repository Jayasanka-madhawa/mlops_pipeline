# Step 1 — Environment and project setup

## Purpose and scope

This chapter documents the foundation of our scan-quality MLOps project: how we organized the repository, selected a Python interpreter, installed dependencies, and verified the execution environment. Model training starts in Step 2.

The project is an educational simulation inspired by scan-quality workflows. It uses synthetic numerical features, not video processing or a clinically validated NervoScan model.

Our initial architecture keeps training and evaluation on your Mac. Later stages add a FastAPI prediction service, SQLite prediction logging, MLflow tracking, Prometheus metrics, Grafana dashboards, and a separate drift-monitor process.

## 1. Project directory and execution context

Your project directory is:

```text
/Users/jayasanka/Documents/mlops_pipeline
```

Run project commands from this directory:

```bash
cd /Users/jayasanka/Documents/mlops_pipeline
```

The working directory and Python environment are separate concepts:

- The working directory determines where relative paths such as `training/train.py` are resolved.
- The Python environment determines which interpreter and installed packages execute that script.

Changing directory does not activate Conda. Activating Conda does not change directory.

## 2. Repository structure

We initially created these directories:

```bash
mkdir -p training app monitoring tests data artifacts
```

| Directory | Responsibility | Files added in later steps |
|---|---|---|
| `training/` | Dataset generation, local training, offline evaluation | `generate_data.py`, `train.py`, `evaluate_test.py` |
| `app/` | Request validation, model serving, prediction storage and API metrics | `main.py`, `prediction_log.py`, `metrics.py` |
| `monitoring/` | Traffic simulation, drift detection and monitoring configuration | `simulate_traffic.py`, `check_drift.py`, `drift_monitor.py`, `prometheus.yml` |
| `tests/` | API behavior and small load tests | `test_api.py`, `load_test.py` |
| `data/` | Dataset splits, reference features and runtime prediction records | `train.csv`, `validation.csv`, `test.csv`, `reference_features.csv`, `predictions.db` |
| `artifacts/` | Locally saved model versions and evaluation reports | `<run_id>/model.joblib`, `<run_id>/metadata.json`, `<run_id>/test_evaluation.json` |

These are organizational boundaries, not separate deployed services. For example, `training/` contains scripts that terminate after completing a task, whereas the API and automated drift monitor later run continuously.

### Robust file paths inside scripts

Our Python scripts use:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data_path = ROOT / "data" / "train.csv"
```

For `training/train.py`, `parents[0]` is `training/` and `parents[1]` is the project root. This makes internal data paths independent of the shell's current directory. The shell still needs a valid path to locate the script itself.

## 3. Create an isolated Python environment

We ran:

```bash
conda create -n mlops python=3.12 -y
conda activate mlops
```

| Argument | Meaning |
|---|---|
| `create` | Create an environment with its own interpreter and packages |
| `-n mlops` | Name the environment `mlops` |
| `python=3.12` | Request Python's 3.12 release series |
| `-y` | Accept the package installation plan |

The installation in your session resolved to Python **3.12.14**. The command does not pin that exact patch release, so running it later may resolve differently.

Your environment was created at:

```text
/Users/jayasanka/miniconda3/envs/mlops
```

This isolates Python dependencies from the Conda `base` environment. It is not a container: programs still share your Mac's operating system, filesystem and network ports.

## 4. How activation selects Python

Activating `mlops` adjusts the current shell environment, including its executable search path. The shell then finds the environment's Python before the base interpreter.

We verified it with:

```bash
python --version
which python
```

Your output was:

```text
Python 3.12.14
/Users/jayasanka/miniconda3/envs/mlops/bin/python
```

For a more direct check:

```bash
python -c "import sys; print(sys.executable)"
```

The interpreter path is stronger evidence than the prompt label alone.

### The Conda warning we encountered

Conda reported that it could not register the environment in:

```text
/Users/jayasanka/.conda/environments.txt
```

Environment creation and activation nevertheless succeeded, as shown by the interpreter path. That registration warning was separate from whether Python could run. We did not need to recreate the environment to continue the project.

## 5. Install project dependencies

The initial installation command was:

```bash
python -m pip install numpy pandas scikit-learn joblib mlflow fastapi "uvicorn[standard]" prometheus-client requests pytest httpx scipy
```

Using `python -m pip` ensures that pip belongs to the interpreter selected by `python`. A standalone `pip` executable can point at a different environment if shell paths are misconfigured.

| Package | Role in our project |
|---|---|
| `numpy` | Synthetic sampling, numerical arrays and load-test statistics |
| `pandas` | CSV processing, feature tables and SQL query results |
| `scikit-learn` | Random Forest training and evaluation metrics |
| `joblib` | Save and load the local trained model artifact |
| `mlflow` | Experiment parameters, metrics, run IDs and model artifacts |
| `fastapi` | HTTP endpoints and request handling |
| `uvicorn` | ASGI server that runs FastAPI |
| `prometheus-client` | Counters, histograms, gauges and metrics exposition |
| `requests` | Simulated prediction traffic and load-test HTTP calls |
| `pytest` | Execute automated API tests |
| `httpx` | HTTP client used by FastAPI's TestClient |
| `scipy` | Two-sample KS test for numerical feature drift |

Pydantic is installed through FastAPI's dependency chain; our application imports it directly for input validation.

Installing `prometheus-client` does not install the Prometheus server or Grafana. Those services are added later through Docker Compose. Likewise, installing FastAPI does not start an API server.

## 6. Record dependency versions

We initially captured installed Python distributions with:

```bash
python -m pip freeze > requirements.txt
```

This records resolved package versions, including transitive dependencies, from the currently selected environment. It is useful evidence of what was installed, but is not a complete cross-platform environment lock: it does not capture the full Conda environment, OS libraries or container base image.

An unpinned installation command can resolve different package versions in the future. Preserve the version snapshot alongside the source used for each release.

Later, for Docker serving, we created a smaller `requirements-serving.txt` from the `mlops` environment. That distinction is intentional: training and development need tools such as MLflow and pytest that the initial serving image does not use. The serving file pins selected top-level packages, not every transitive dependency.

## 7. Why the environment matters for saved models

We encountered a concrete compatibility problem later:

| Operation | Environment | scikit-learn version |
|---|---|---|
| Model training | `mlops` | `1.9.1` |
| Initial API tests and test evaluation | `base` | `1.8.0` |

Loading the model under `base` emitted `InconsistentVersionWarning`. Eight API tests passed, but passing tests did not establish that cross-version model loading was reliable. The serving-consistency test compares two uses of the same loaded artifact, so it cannot prove compatibility with the original training runtime.

The correction was to select the original environment explicitly:

```bash
conda run -n mlops python -m pytest tests/test_api.py -q
conda run -n mlops python training/evaluate_test.py
```

These commands belong to later stages; they are shown here to explain why Step 1 is important. `conda run -n mlops` selects the environment even if the prompt still displays `(base)`.

Check the interpreter and model library together:

```bash
conda run -n mlops python -c "import sys, sklearn; print(sys.executable); print(sklearn.__version__)"
```

For this saved model, we expect the `mlops` interpreter and scikit-learn `1.9.1`. That version is specific to our recorded model, not a recommendation to use it for every project.

## 8. Shell variables and application configuration

A later step introduced `MODEL_RUN_ID`, which selects the artifact directory loaded by the API:

```bash
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
```

The API reads it using:

```python
import os

run_id = os.environ["MODEL_RUN_ID"]
```

This model ID was created after training; it was not needed to initialize the project.

An exported variable is inherited by programs launched from that shell. It does not automatically appear in another terminal tab, and updating a shell variable does not change the environment of a process that is already running.

That explains the `KeyError: 'MODEL_RUN_ID'` we encountered when running the API or tests from another terminal.

Two separate requirements must hold:

1. Select the correct Python environment.
2. Supply the configuration that the program needs.

Conda activation does not automatically define `MODEL_RUN_ID`. A `.env` file also does not automatically populate Python's `os.environ`; a tool must load it. In our later Docker setup, Compose reads `.env` for configuration interpolation, while the image stores the selected model ID during its build.

## 9. Working with multiple terminals

| Terminal purpose | Required setup |
|---|---|
| Training or evaluation | Project directory and `mlops` environment |
| MLflow server | Project directory and `mlops` environment |
| FastAPI server | Project directory, `mlops` environment and `MODEL_RUN_ID` |
| Drift monitor | Project directory, `mlops` environment and `MODEL_RUN_ID` |
| API tests | Project directory, `mlops` environment and `MODEL_RUN_ID` |
| Docker Compose commands | Project directory and a running Docker engine; Conda activation is not required |

Local long-running processes occupy their terminals. Docker Compose later runs services in background containers instead.

## 10. Step 1 verification

From the project directory:

```bash
conda activate mlops

python -c "import sys; print(sys.executable)"

python -m pip check

python -c "import numpy, pandas, sklearn, joblib, mlflow, fastapi, uvicorn, prometheus_client, requests, pytest, httpx, scipy; print('Project imports OK')"
```

Interpretation:

- The interpreter path should point to `envs/mlops/bin/python`.
- `pip check` checks declared dependency compatibility; it does not test model behavior or native-library compatibility.
- Successful imports show that the main Python packages are available.
- No server is started, no dataset is generated and no model is trained by these checks.

## Completion criteria

Step 1 is complete when the project directories exist, the selected interpreter is the `mlops` interpreter, required imports succeed, and dependency versions have been recorded.

The next chapter is **Step 2 — Synthetic dataset generation, feature definitions, labels and train/validation/test splitting**.
