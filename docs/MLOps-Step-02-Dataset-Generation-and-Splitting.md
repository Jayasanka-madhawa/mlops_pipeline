# Step 2 — Synthetic dataset generation and splitting

## Purpose

Before training, we need examples that connect scan features to a known quality label. In this project we generated 6,000 synthetic examples, split them into training, validation and test datasets, and saved a training-only reference for drift monitoring.

This chapter explains the dataset design used in our exercise. The implementation below is a consolidated equivalent of that design, not a retrieved copy of your current local script. You have already generated and used the dataset: do not overwrite it while evaluating or serving the existing model. A changed dataset should be treated as a new dataset version.

## 1. Define the prediction problem

Our task is binary classification:

> Given four numerical scan-quality features, estimate whether the scan is acceptable.

For each row:

- **X** is the ordered vector of four feature values.
- **y** is the observed label, `0` for poor and `1` for acceptable.
- **scan_id** identifies the example; it is not an input to the model.

| Column | Type | Range | Role |
|---|---|---|---|
| `scan_id` | String | Unique identifier | Traceability only |
| `brightness` | Float | 0–1 | Input feature |
| `motion` | Float | 0–1 | Input feature |
| `face_visibility` | Float | 0–1 | Input feature |
| `signal_quality` | Float | 0–1 | Input feature |
| `acceptable` | Integer | 0 or 1 | Classification target |

An illustrative row might contain brightness `0.5`, motion `0.1`, face visibility `0.95`, signal quality `0.9`, and label `1`. The label is not guaranteed by those features because our generator intentionally includes uncertainty.

These are simulated, dimensionless measurements. We did not implement camera capture, face detection, rPPG extraction or a clinically validated quality score. A real application would need precisely defined feature-extraction methods and independently obtained quality labels.

## 2. Generate feature values with Beta distributions

We used NumPy's random generator with seed 42:

```python
rng = np.random.default_rng(42)
```

The seed supports reproducible generation when the implementation, dependency behavior and sequence of random draws remain the same. A seed alone is not a full dataset version.

A Beta distribution produces values between 0 and 1. Its two shape parameters control where values concentrate. Its theoretical mean is:

```text
mean = alpha / (alpha + beta)
```

| Feature | Distribution | Theoretical mean | Intended simulation |
|---|---|---:|---|
| Brightness | Beta(5, 5) | 0.500 | Most scans have moderate brightness |
| Motion | Beta(2, 8) | 0.200 | Most scans have limited movement |
| Face visibility | Beta(9, 2) | 0.818 | Faces are usually visible |
| Signal quality | Beta(5, 2) | 0.714 | Signals are usually reasonably clear |

Generation:

```python
brightness = rng.beta(5, 5, size=6000)
motion = rng.beta(2, 8, size=6000)
face_visibility = rng.beta(9, 2, size=6000)
signal_quality = rng.beta(5, 2, size=6000)
```

Finite samples will not have exactly the theoretical averages. We later observed training-reference means of approximately `0.5029`, `0.2023`, `0.8199`, and `0.7137` respectively.

The generator samples the four features independently. Real scan features can be correlated: movement may reduce signal quality, for example. This simplification limits how representative the exercise is of a real application.

## 3. Turn features into a quality probability

### Account for brightness extremes

Brightness is not simply “higher is better.” Very dark and very bright scans can both be undesirable. We defined:

```python
lighting_quality = 1 - 2 * np.abs(brightness - 0.5)
```

| Brightness | Lighting quality |
|---:|---:|
| 0.0 | 0.0 |
| 0.25 | 0.5 |
| 0.5 | 1.0 |
| 0.75 | 0.5 |
| 1.0 | 0.0 |

`lighting_quality` is an internal variable used by the synthetic label generator. We did not save it as one of the four model input features. The classifier must learn the relationship between brightness and quality from examples.

### Calculate a latent score

```python
score = (
    2.0 * lighting_quality
    - 4.0 * motion
    + 2.5 * face_visibility
    + 3.0 * signal_quality
    - 4.5
)
```

The coefficients encode our artificial data-generating assumptions:

- Better lighting, visibility and signal quality raise the score.
- More movement lowers it.
- The intercept of `-4.5` shifts the overall acceptance tendency.

These are hand-chosen coefficients, not parameters learned by the Random Forest. They do not encode verified clinical relationships.

### Convert the score to a probability

```python
probability = 1 / (1 + np.exp(-score))
```

This sigmoid transformation maps any real-valued score to a probability between 0 and 1. A score of 0 gives probability 0.5; positive scores give probabilities above 0.5.

For the illustrative inputs `0.5, 0.1, 0.95, 0.9`:

```text
lighting_quality = 1
score = 2 - 0.4 + 2.375 + 2.7 - 4.5 = 2.175
probability ≈ 0.898
```

The generator therefore considers this example likely to be acceptable.

## 4. Sample labels instead of applying a fixed cutoff

We created labels with:

```python
acceptable = rng.binomial(1, probability)
```

For a row whose probability is 0.898, the label is 1 with probability 0.898 and 0 otherwise. We did not use `probability >= 0.5` to generate the dataset labels.

This creates irreducible uncertainty: even a model that knew the generating probability exactly could not predict every sampled label correctly. That helps explain why a functioning training pipeline need not achieve near-perfect accuracy.

Distinguish three objects:

| Object | Source | Purpose |
|---|---|---|
| Generating probability | Our synthetic formula | Sample labels when constructing the dataset |
| Observed label | Bernoulli draw | Train and evaluate the classifier |
| Predicted probability | Trained Random Forest | Estimate acceptability for an input |

The serving threshold of 0.5 is introduced later to turn the model's predicted probability into a decision. It is separate from label generation.

## 5. Split the dataset before fitting the model

Our split sizes were:

| Split | Share | Rows | Use |
|---|---:|---:|---|
| Training | 60% | 3,600 | Fit model parameters |
| Validation | 20% | 1,200 | Compare models or tune choices |
| Test | 20% | 1,200 | Final evaluation of the selected version |

We used two stratified splits:

```python
train, remaining = train_test_split(
    dataset,
    test_size=0.4,
    stratify=dataset["acceptable"],
    random_state=42,
)

validation, test = train_test_split(
    remaining,
    test_size=0.5,
    stratify=remaining["acceptable"],
    random_state=42,
)
```

`stratify` approximately preserves the class proportions in each split. It does not balance the classes to 50/50 or prevent every form of leakage.

Random splitting suits this exercise because examples are generated independently. Real scan data may require participant-level grouping so scans from the same person do not appear in both training and test data. A time-based split may be needed when evaluating future operating conditions.

## 6. Save a training-only drift reference

We saved:

```python
train[FEATURES].to_csv(
    ROOT / "data" / "reference_features.csv",
    index=False,
)
```

The reference contains **3,600 rows and four feature columns**. It excludes IDs and labels.

Later, the drift monitor compares each feature in this reference against the same feature in recent production predictions. For example, training brightness values are compared with production brightness values—not with model outputs or target labels.

Using training inputs establishes a baseline for the population the model learned from. We keep this baseline fixed for the release instead of silently replacing it with changing production data. A model trained on a new dataset needs a deliberately selected matching reference.

Our per-feature KS checks detect changes in individual feature distributions. They do not detect every possible change in relationships between features, and they do not measure predictive accuracy.

## 7. Consolidated generation implementation

This code shows the complete design for a fresh project. Preserve your existing CSVs for the current trained release.

```python
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]


def main():
    rng = np.random.default_rng(42)
    count = 6000

    brightness = rng.beta(5, 5, size=count)
    motion = rng.beta(2, 8, size=count)
    face_visibility = rng.beta(9, 2, size=count)
    signal_quality = rng.beta(5, 2, size=count)

    lighting_quality = 1 - 2 * np.abs(brightness - 0.5)
    score = (
        2 * lighting_quality
        - 4 * motion
        + 2.5 * face_visibility
        + 3 * signal_quality
        - 4.5
    )
    probability = 1 / (1 + np.exp(-score))
    acceptable = rng.binomial(1, probability)

    dataset = pd.DataFrame({
        "scan_id": [f"train-{i:06d}" for i in range(count)],
        "brightness": brightness,
        "motion": motion,
        "face_visibility": face_visibility,
        "signal_quality": signal_quality,
        "acceptable": acceptable,
    })

    train, remaining = train_test_split(
        dataset,
        test_size=0.4,
        stratify=dataset["acceptable"],
        random_state=42,
    )
    validation, test = train_test_split(
        remaining,
        test_size=0.5,
        stratify=remaining["acceptable"],
        random_state=42,
    )

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for name, frame in [
        ("train", train),
        ("validation", validation),
        ("test", test),
    ]:
        frame.to_csv(data_dir / f"{name}.csv", index=False)
        print(f"{name}: {len(frame)} rows")

    train[FEATURES].to_csv(
        data_dir / "reference_features.csv",
        index=False,
    )


if __name__ == "__main__":
    main()
```

The original `train-` ID prefix applies to all generated rows before splitting; it does not mean every row belongs to the training split. Split membership is determined by the output file.

## 8. Inspect existing data without regenerating it

From the project root, this read-only check verifies sizes, feature values, label values, disjoint IDs and reference alignment:

```bash
conda run -n mlops python -c '
from pathlib import Path
import numpy as np
import pandas as pd

root = Path("data")
features = ["brightness", "motion", "face_visibility", "signal_quality"]
expected = {"train": 3600, "validation": 1200, "test": 1200}
frames = {}

for name, count in expected.items():
    frame = pd.read_csv(root / f"{name}.csv")
    assert len(frame) == count, f"Unexpected size: {name}"
    assert frame["scan_id"].is_unique
    assert frame["scan_id"].notna().all()
    assert frame["acceptable"].isin([0, 1]).all()
    values = frame[features].to_numpy()
    assert np.isfinite(values).all()
    assert ((values >= 0) & (values <= 1)).all()
    frames[name] = frame
    print(name, "rows:", len(frame), "acceptable fraction:", frame["acceptable"].mean())

ids = {name: set(frame["scan_id"]) for name, frame in frames.items()}
assert ids["train"].isdisjoint(ids["validation"])
assert ids["train"].isdisjoint(ids["test"])
assert ids["validation"].isdisjoint(ids["test"])

reference = pd.read_csv(root / "reference_features.csv")
pd.testing.assert_frame_equal(reference, frames["train"][features])
print("Dataset structure and reference checks passed")
'
```

This checks structure and provenance relationships, not whether the synthetic assumptions represent real users. It does not fit a model or use test labels to choose a model.

## 9. Leakage and reproducibility boundaries

- Do not include `acceptable` or `scan_id` in the model input matrix.
- Any learned imputation, scaling or encoding added later must be fitted on training data only and reused at serving time. Our bounded synthetic numeric features need no categorical encoding, and our Random Forest does not require standardization.
- Keep test data out of hyperparameter and threshold selection. Repeated tuning based on test results turns that set into another validation set.
- Preserve the exact CSVs, generator code, seed and dependency versions. Our training script later records SHA-256 hashes for the training and validation files; hashes identify bytes but do not store or restore the data.
- API input validation checks valid ranges and field names. It cannot guarantee that production feature-extraction semantics match training semantics.

## Completion criteria

At the end of this step we have three disjoint datasets, a documented four-feature schema, probabilistically generated binary labels, and a fixed training reference for drift monitoring.

The next chapter is **Step 3 — Random Forest training, validation metrics, MLflow experiment tracking and versioned model artifacts**.
