# Step 3 — Model training, validation and MLflow tracking

## Purpose and scope

In this step we trained a Random Forest on the local Mac, evaluated it on the validation split, recorded the experiment in MLflow, and saved artifacts that the prediction API later loaded.

This chapter documents the implementation and results from our project. Training is already complete; commands are explanations and reproduction instructions, not a request to overwrite the existing release. Final test-set evaluation belongs to a later step.

## 1. Inputs and outputs

| Item | Location | Purpose |
|---|---|---|
| Training dataset | `data/train.csv` | 3,600 examples used to fit the model |
| Validation dataset | `data/validation.csv` | 1,200 examples used to evaluate this candidate |
| Training program | `training/train.py` | Fit, evaluate, track and save |
| MLflow backend | `mlflow.db` | SQLite experiment and run metadata |
| MLflow artifact destination | `mlartifacts/` | Files managed through the local tracking server |
| Local release artifacts | `artifacts/<run_id>/` | Model and metadata used by our API |

The training script does not read `data/test.csv`. This preserves the test set for final evaluation after model choices are fixed.

## 2. Start the tracking server

We ran the following from the project root in a separate terminal:

```bash
conda activate mlops

mlflow server \
  --host 127.0.0.1 \
  --port 5001 \
  --backend-store-uri sqlite:///mlflow.db \
  --artifacts-destination ./mlartifacts
```

Open the local UI at `http://127.0.0.1:5001`.

| Option | Meaning in our setup |
|---|---|
| `--host 127.0.0.1` | Listen on the Mac's loopback interface |
| `--port 5001` | Use port 5001 for the tracking API and UI |
| `--backend-store-uri` | Store experiment/run metadata in SQLite |
| `--artifacts-destination` | Specify the server's local artifact storage destination |

The relative storage paths are resolved from the tracking server's working directory. Starting it elsewhere can create a different database and artifact directory.

The tracking server records the experiment; it does not execute our training job. `python training/train.py` runs training as a separate local process and communicates with MLflow over HTTP.

## 3. Load data and enforce feature order

Our model uses four input columns:

```python
FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]

train = pd.read_csv(ROOT / "data" / "train.csv")
validation = pd.read_csv(ROOT / "data" / "validation.csv")

X_train = train[FEATURES]
y_train = train["acceptable"]
X_validation = validation[FEATURES]
y_validation = validation["acceptable"]
```

The resulting shapes are:

```text
X_train:       (3600, 4)
y_train:       (3600,)
X_validation:  (1200, 4)
y_validation:  (1200,)
```

`scan_id` is excluded because it is an identifier, not a scan measurement. `acceptable` is excluded from X because supplying the answer as a feature would leak the target.

Keeping the feature list explicit makes training and serving agree on column order. Our later API constructs its DataFrame using the feature list saved in metadata.

No scaling or imputation was fitted here. The generated data contains complete numeric values, and tree-based models do not require feature standardization for this task. If preprocessing is added, its fitted state must be saved with the model.

## 4. Choose and configure the Random Forest

We used:

```python
parameters = {
    "n_estimators": 200,
    "max_depth": 6,
    "min_samples_leaf": 10,
    "random_state": 42,
    "n_jobs": -1,
}

model = RandomForestClassifier(**parameters)
model.fit(X_train, y_train)
```

### How training works

With the classifier's default bootstrap behavior, each tree is trained using a sample drawn with replacement from the training rows. Each split considers a random subset of features and selects a split that reduces class impurity. A tree partitions feature space into leaves, each containing training examples with an estimated class distribution.

The forest averages the trees' class probabilities. Diversity between trees reduces the variance that a single decision tree can exhibit.

This is a reasonable first model for small numerical tabular data: it can learn nonlinear relationships such as moderate brightness being preferable to either extreme. We did not prove it was the best model by comparing a large candidate set.

### What the parameters control

| Parameter | Our value | Effect |
|---|---:|---|
| `n_estimators` | 200 | Number of trees; more trees increase compute and artifact size |
| `max_depth` | 6 | Limits tree depth and model complexity |
| `min_samples_leaf` | 10 | Prevents leaves based on very few training examples |
| `random_state` | 42 | Controls the estimator's random sampling for reproducibility |
| `n_jobs` | -1 | Allows parallel execution across available CPU resources |

`n_jobs=-1` is convenient for local training. It is not automatically optimal for a busy prediction service: concurrent requests can compete for CPUs. Our small load test later measures only one local workload.

## 5. Produce predictions and probabilities

Validation uses:

```python
predictions = model.predict(X_validation)
probabilities = model.predict_proba(X_validation)[:, 1]
```

In this binary model, classes are ordered `[0, 1]`, so column 1 is the probability assigned to acceptable scans. A more explicit class lookup, used later in serving, is:

```python
positive_index = list(model.classes_).index(1)
probabilities = model.predict_proba(X_validation)[:, positive_index]
```

A predicted probability is a model estimate, not a guarantee of quality. We did not separately calibrate these probabilities.

### Threshold detail in our implementation

We recorded a classification threshold of 0.5. The API later uses:

```python
predictions = (probabilities >= 0.5).astype(int)
```

The initial training script used `model.predict()` for validation labels. For binary classification these usually agree, but at an exact 0.5 tie, scikit-learn's class selection can choose class 0 while `>= 0.5` chooses class 1.

For future versions, use the same explicit threshold rule for validation, final evaluation and serving. This is a small consistency improvement, not a change already made to your local training script.

## 6. Establish a baseline

We compared the model with always predicting the most common training class:

```python
majority_class = int(y_train.mode().iloc[0])
baseline_predictions = np.full(len(y_validation), majority_class)
baseline_accuracy = accuracy_score(y_validation, baseline_predictions)
```

The majority class is determined from training data. The baseline is then evaluated on validation data, just like the model.

This prevents interpreting raw accuracy without context. An accuracy near 60% is less impressive if a constant prediction already achieves approximately 59%.

## 7. Interpret the actual validation results

Your successful run reported:

| Metric | Value | Interpretation |
|---|---:|---|
| Accuracy | 0.6383 | 63.83% of validation labels predicted correctly |
| Baseline accuracy | 0.5917 | 59.17% correct using the majority class |
| Precision | 0.6540 | Of accepted scans, 65.40% were truly acceptable in this synthetic dataset |
| Recall | 0.8254 | Accepted 82.54% of genuinely acceptable scans |
| F1 | 0.7298 | Harmonic mean of precision and recall |
| ROC AUC | 0.6737 | Modest ability to rank acceptable scans above poor scans |
| Log loss | 0.6306 | Evaluates probability quality; lower is better under the same evaluation conditions |

The accuracy improvement is approximately **4.67 percentage points**, not a 4.67% relative improvement.

Taking acceptable as the positive class:

| Outcome | Meaning |
|---|---|
| True positive | Acceptable scan accepted |
| False positive | Poor scan accepted |
| False negative | Acceptable scan rejected |
| True negative | Poor scan rejected |

Precision of 0.654 means about 34.6% of accepted validation scans were actually poor. This is a fraction of accepted scans; it is not the false-positive rate, whose denominator is all actual poor scans.

The synthetic label generator intentionally includes randomness. Perfect prediction is not expected, but label noise does not prove this model is optimal. We accepted this candidate for the infrastructure-learning exercise, not for a real clinical deployment.

## 8. Create an MLflow experiment and run

The training script configures:

```python
mlflow.set_tracking_uri("http://127.0.0.1:5001")
mlflow.set_experiment("scan-quality")

with mlflow.start_run(run_name="random-forest-v1") as run:
    # Train, evaluate and log inside this block.
    ...
```

| Concept | Role |
|---|---|
| Experiment | Groups related runs, here `scan-quality` |
| Run name | Human-readable label, here `random-forest-v1` |
| Run ID | Unique identifier for one execution |
| Parameter | Configuration such as tree count or training row count |
| Metric | Numeric measurement such as validation accuracy |
| Tag | Descriptive metadata such as dataset hash |
| Artifact | Saved file such as a model or metadata JSON |

Your successful run ID was:

```text
d9571388a61f45369e4f709878e8246b
```

A repeated run name does not identify the same execution. Re-running training creates a new run ID unless a run is explicitly resumed.

We logged estimator parameters, training and validation row counts, threshold, validation metrics and tags including `candidate=v1` and `dataset_type=synthetic`.

Our code did not register a named model version in a model registry or establish an approval workflow. Logging a model artifact and registering an approved release are distinct operations.

## 9. Record dataset hashes

We computed file hashes with:

```python
import hashlib


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
```

The hashes were recorded as `train_sha256` and `validation_sha256` tags.

A hash helps identify which exact file bytes were used. It does not store the dataset, and it changes if CSV formatting or row order changes even when the underlying examples are equivalent.

For full reproducibility we would also preserve the source revision, dependency versions, dataset files and relevant environment details. The current script does not automatically capture all of those as a complete immutable release manifest.

## 10. Log the model and understand the serialization fix

Our corrected model-logging call was:

```python
mlflow.sklearn.log_model(
    sk_model=model,
    name="scan_quality_model",
    signature=infer_signature(X_train, model.predict(X_train)),
    input_example=X_train.head(3),
    skops_trusted_types=["sklearn.tree._tree.Tree"],
)
```

### Signature and example

The inferred signature records the input schema and the output shape/type demonstrated by `model.predict()`. It does not define the full later FastAPI response, which also contains a probability, label text, threshold and version.

The input example stores a small sample to illustrate the expected model input. Our API's 0–1 bounds are enforced separately through Pydantic validation; they are not established by this signature call.

### Error we encountered

Training reached model logging, but the installed serialization stack refused to load the serialized tree type during its save-time check:

```text
Untrusted types found in the file: ['sklearn.tree._tree.Tree']
```

We added only that specific trusted type because the model was freshly created by our own training script. We did not blanket-trust every reported type or downgrade packages.

The failed attempt had already created an MLflow run and logged some information before reaching the failing operation. A visible run in the UI therefore did not mean the entire training-and-saving workflow had succeeded. We reran the script and used the successful run.

The direct `joblib` artifact discussed below is a separate serialization path. The trust argument on MLflow's call does not make arbitrary joblib files safe to load; use artifacts whose source you trust.

## 11. Save the local serving artifacts

After successful MLflow model logging, the script creates a run-specific directory:

```python
output_dir = ROOT / "artifacts" / run.info.run_id
output_dir.mkdir(parents=True, exist_ok=True)

joblib.dump(model, output_dir / "model.joblib")
```

It then writes metadata with:

```python
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
metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
mlflow.log_artifact(str(metadata_path))
```

| Artifact | Contents | Later consumer |
|---|---|---|
| `model.joblib` | Fitted estimator, trees and learned state | FastAPI |
| `metadata.json` | Feature order, class meanings, threshold, run ID and validation metrics | FastAPI and evaluation code |
| MLflow model artifact | Model representation and MLflow packaging information | Experiment inspection and potential MLflow loading |

Our API later loads the local joblib file; it does not fetch the model from MLflow on each request. After local artifacts exist, this API can serve without the tracking server running.

The metadata file links model behavior to a run, but does not itself contain the training data or a complete Python dependency lock.

## 12. Training execution and failure boundaries

For a new training execution, keep MLflow running and use another terminal:

```bash
conda activate mlops
python training/train.py
```

The sequence is:

1. Read training and validation CSVs.
2. Connect to the experiment and start a run.
3. Fit the Random Forest.
4. Compute validation predictions and metrics.
5. Log parameters, tags and metrics.
6. Log the MLflow model.
7. Save the local joblib artifact and metadata.
8. Print metrics and the output directory.

The initial model-saving error occurred at step 6, before local serving artifacts were written. This is why successful model fitting alone was not enough to proceed to deployment.

For the existing successful release, inspect files without retraining:

```bash
ls artifacts/d9571388a61f45369e4f709878e8246b
```

At this stage we expect `model.joblib` and `metadata.json`. The later final-evaluation step adds `test_evaluation.json`.

## 13. What this step did not do

We did not deploy an HTTP service, collect production labels, choose a clinical acceptance threshold, automate model approval, or prove production accuracy. We created a tracked, evaluated candidate with the files required for the serving step.

Later we also encountered scikit-learn `1.8.0` in the Conda base environment trying to load a model trained with `1.9.1`. That environment mismatch was fixed by using `mlops`; it was different from the serialization trust error described above.

## Completion criteria

- The training run completed successfully in the intended environment.
- The MLflow UI contains parameters, validation metrics and the model artifact.
- The local run directory contains the model and matching metadata.
- Validation performance has been compared with the baseline and its limitations understood.
- The test split remains reserved for final evaluation at this point in the workflow.

The next chapter is **Step 4 — FastAPI model serving, startup lifecycle, input validation and prediction responses**.

## References

These are the official references used during the project; installed-version behavior should be checked before changing dependencies.

- [MLflow scikit-learn integration](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.sklearn.html)
- [scikit-learn ROC AUC documentation](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.roc_auc_score.html)
- [scikit-learn model persistence and compatibility](https://scikit-learn.org/stable/model_persistence.html)
