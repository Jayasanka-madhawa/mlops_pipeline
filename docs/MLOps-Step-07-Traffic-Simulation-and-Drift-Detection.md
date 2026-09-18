# Step 7 — Traffic simulation and manual data-drift detection

## Purpose and scope

Step 6 monitored the prediction service. In Step 7, we checked whether the inputs arriving at the model differed from its training inputs.

We generated two controlled traffic windows, stored their predictions through the real API, and compared each window with the training reference using a two-sample Kolmogorov–Smirnov (KS) test.

This chapter records the scripts, statistical rule and actual results from our exercise. The automated five-minute monitor and Grafana drift alert are covered in Step 8.

## 1. Define what we are detecting

Data drift here means a change in the distribution of input features. We compare:

- **Reference:** 3,600 training rows in `data/reference_features.csv`.
- **Current sample:** 300 prediction records selected from `data/predictions.db` by a simulation's start and end timestamps.

We compare brightness with brightness, motion with motion, and so on. We do not compare feature values with target labels or predicted classes.

| Phenomenon | What changes | Does this check measure it? |
|---|---|---|
| Input drift | Distribution of model inputs | Yes, separately for each numeric feature |
| Prediction drift | Distribution of predicted scores or classes | No |
| Concept drift | Relationship between inputs and the true outcome | No |
| Performance degradation | Predictive quality measured against actual labels | No |

An input-drift flag is evidence for investigation, not proof of reduced accuracy. Without actual production labels, this check cannot calculate precision, recall or accuracy.

## 2. Why we needed varied requests

Earlier API smoke checks repeatedly sent the same four feature values. That is useful for checking the endpoint but creates a degenerate input distribution, unlike the training population.

For the drift exercise, we generated fresh samples rather than sending the same payload repeatedly. We used a different random seed from dataset generation so the simulation did not intentionally replay training examples.

The normal mode samples from the same theoretical distributions as training. Its individual observations and sample averages still differ because of sampling variation.

## 3. Normal and drifted scenarios

| Feature | Training and normal mode | Drifted mode | Intended effect |
|---|---|---|---|
| Brightness | Beta(5, 5), mean 0.5 | Beta(2, 8), mean 0.2 | Darker scans |
| Motion | Beta(2, 8), mean 0.2 | Beta(6, 4), mean 0.6 | More movement |
| Face visibility | Beta(9, 2), mean about 0.818 | Same distribution | No intentional shift |
| Signal quality | Beta(5, 2), mean about 0.714 | Same distribution | No intentional shift |

The model and API code are unchanged between scenarios. Only the distribution of requests changes.

This separation lets us demonstrate that a service can keep returning HTTP 200 while its operating inputs change substantially.

## 4. The traffic simulator

We created `monitoring/simulate_traffic.py`:

```python
import argparse
import time
from datetime import datetime, timezone

import numpy as np
import requests

parser = argparse.ArgumentParser()
parser.add_argument(
    "--mode",
    choices=["normal", "drifted"],
    default="normal",
)
parser.add_argument("--count", type=int, default=300)
args = parser.parse_args()

rng = np.random.default_rng(2026)

started = datetime.now(timezone.utc).isoformat()
print(f"Mode: {args.mode}")
print(f"Window start: {started}", flush=True)

successful = 0

with requests.Session() as session:
    for i in range(args.count):
        if args.mode == "normal":
            brightness = rng.beta(5, 5)
            motion = rng.beta(2, 8)
        else:
            brightness = rng.beta(2, 8)
            motion = rng.beta(6, 4)

        payload = {
            "brightness": float(brightness),
            "motion": float(motion),
            "face_visibility": float(rng.beta(9, 2)),
            "signal_quality": float(rng.beta(5, 2)),
        }

        response = session.post(
            "http://127.0.0.1:8001/predict",
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        successful += 1

        if (i + 1) % 50 == 0:
            print(f"Completed {i + 1}/{args.count}", flush=True)

        time.sleep(0.1)

finished = datetime.now(timezone.utc).isoformat()

print(f"\nSuccessful requests: {successful}")
print(f"Window start: {started}")
print(f"Window end:   {finished}")
```

### Execution behavior

The requests are sequential. Each iteration waits for the HTTP response, then sleeps for 0.1 seconds. Therefore, this is a traffic generator for statistical checking, not a concurrent stress test and not a precise 10-requests-per-second scheduler.

`requests.Session` supports connection reuse. `raise_for_status()` stops the script on an HTTP error; this implementation does not retry failed requests.

A successful response increments the local counter, but does not prove that SQLite stored the row. Our API deliberately returns predictions even when logging fails. We verify storage separately in the drift checker.

The fixed seed makes a repeated run of the same mode repeat its generated sequence under the same implementation and environment. That is useful for this controlled demonstration. A realistic long-running simulation should vary seeds or maintain generator state so it does not continuously replay the same batch.

Using the same seed for both modes does not guarantee identical face-visibility and signal-quality values across modes: different distribution sampling can consume random state differently. Their distributions, not their exact row values, are held constant.

## 5. Capture a bounded traffic window

We ran:

```bash
conda activate mlops
python monitoring/simulate_traffic.py --mode normal --count 300
```

The simulator records UTC timestamps before sending and after completing the batch. The API also stores UTC timestamps for each request.

Our SQL query uses a half-open interval:

```sql
WHERE timestamp >= ? AND timestamp < ?
```

That includes records at the start boundary and excludes the end boundary. Using a consistent boundary convention helps avoid double-counting when adjacent windows are used.

The original manual checker does not filter by model version or traffic source. We ran a controlled batch for one model. Concurrent traffic in the same window can contaminate the comparison or change the row count. The later automated monitor adds a model-version filter.

Both simulator and API ran on the same Mac, so clock differences were not a material issue in this exercise. Distributed systems need consistent clock handling and a clear event-time versus processing-time policy.

## 6. Compare distributions with the KS test

For a feature such as brightness, the two-sample KS statistic is the largest vertical difference between the empirical cumulative distribution functions of the reference and current sample:

```text
D = maximum over x of |F_reference(x) - F_current(x)|
```

An empirical cumulative distribution function gives the fraction of observed values less than or equal to a chosen value x.

The statistic D lies between 0 and 1:

- A small D means the two empirical cumulative distributions are close.
- A large D means there is at least one feature-value cutoff with a substantial difference in cumulative proportions.

A KS score of 0.7433 does not mean that 74.33% of requests are bad. It describes distributional separation, not prediction error.

SciPy returns a p-value as well. Under the null hypothesis that the samples come from the same continuous distribution, it quantifies how unusual a statistic at least as extreme as the observed one would be. It is not the probability that drift exists or the probability that the null hypothesis is true.

Our generated features are continuous, and samples are designed to be independent. Real repeated scans, rounded measurements and correlated sessions require more care when interpreting the test's statistical assumptions.

## 7. Use statistical and practical thresholds together

Our rule was:

```python
p_threshold = 0.05 / 4
ks_threshold = 0.10

flagged = p_value < p_threshold and ks_score >= ks_threshold
```

This yields `p < 0.0125` and `KS >= 0.10`.

Dividing 0.05 by four is a Bonferroni adjustment for checking four features within one window. The KS threshold additionally requires a minimum observed distribution difference.

Why use both?

- With large samples, even small differences can be statistically significant.
- With small samples, substantial changes may not produce strong statistical evidence.
- A practical threshold makes the alert sensitive to a chosen magnitude as well as the p-value.

The value 0.10 is a teaching choice, not a universal production boundary. Calibrate thresholds using realistic normal windows, known incidents, sample sizes and the cost of false alarms.

The four-feature adjustment does not control false alarms indefinitely across repeated time windows. Overlapping rolling windows are also correlated. These are considerations for the automated monitor in Step 8.

## 8. The manual checker

We created `monitoring/check_drift.py`:

```python
import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

ROOT = Path(__file__).resolve().parents[1]
FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]

parser = argparse.ArgumentParser()
parser.add_argument("--start", required=True)
parser.add_argument("--end", required=True)
parser.add_argument("--expected-count", type=int, default=300)
args = parser.parse_args()

reference = pd.read_csv(ROOT / "data" / "reference_features.csv")

db_path = ROOT / "data" / "predictions.db"
if not db_path.exists():
    raise SystemExit("Prediction database does not exist.")

with sqlite3.connect(db_path) as connection:
    production = pd.read_sql_query(
        """
        SELECT brightness, motion, face_visibility, signal_quality
        FROM predictions
        WHERE timestamp >= ? AND timestamp < ?
        """,
        connection,
        params=(args.start, args.end),
    )

print(f"Reference records: {len(reference)}")
print(f"Production records: {len(production)}")

if len(production) != args.expected_count:
    raise SystemExit(
        f"Expected {args.expected_count} saved records. "
        "Check the time window, other traffic and logging failures."
    )

if len(production) < 100:
    raise SystemExit("Use at least 100 records for this exercise.")

for name, data in [("reference", reference), ("production", production)]:
    if not np.isfinite(data[FEATURES].to_numpy(dtype=float)).all():
        raise SystemExit(f"Missing or non-finite values in {name} data.")

p_threshold = 0.05 / len(FEATURES)
ks_threshold = 0.10
results = []

for feature in FEATURES:
    result = ks_2samp(reference[feature], production[feature])
    flagged = (
        result.pvalue < p_threshold
        and result.statistic >= ks_threshold
    )

    results.append({
        "feature": feature,
        "reference_mean": reference[feature].mean(),
        "production_mean": production[feature].mean(),
        "ks_score": result.statistic,
        "p_value": result.pvalue,
        "result": "DRIFT FLAG" if flagged else "NO FLAG",
    })

report = pd.DataFrame(results)
print(f"\nRule: p < {p_threshold:.4f} AND KS >= {ks_threshold:.2f}")
print(report.to_string(index=False, float_format=lambda x: f"{x:.4g}"))
```

This script reads stored predictions and terminates after printing results. It does not run inside the prediction endpoint, change the model, retrain it or write drift values into Prometheus.

The minimum of 100 observations is a simple exercise guard. It is not a formal power analysis, and 100 records cannot guarantee that every important shift will be detected.

The original code checks that the database exists, but opens it using a normal connection. A read-only connection and deterministic connection closure are reasonable refinements, described in Step 5.

## 9. Actual normal-window results

Your normal batch covered:

```text
Start: 2026-09-18T17:42:54.433840+00:00
End:   2026-09-18T17:43:36.011769+00:00
```

You ran:

```bash
python monitoring/check_drift.py \
  --start "2026-09-18T17:42:54.433840+00:00" \
  --end "2026-09-18T17:43:36.011769+00:00" \
  --expected-count 300
```

The checker found 3,600 reference rows and 300 prediction records:

| Feature | Reference mean | Current mean | KS score | p-value | Result |
|---|---:|---:|---:|---:|---|
| Brightness | 0.5029 | 0.4977 | 0.04472 | 0.6231 | NO FLAG |
| Motion | 0.2023 | 0.1971 | 0.04917 | 0.5018 | NO FLAG |
| Face visibility | 0.8199 | 0.8296 | 0.05694 | 0.3202 | NO FLAG |
| Signal quality | 0.7137 | 0.7029 | 0.06472 | 0.1893 | NO FLAG |

No feature met both alert conditions. This was consistent with the intentionally unchanged generating distributions.

“No flag” does not establish that the distributions are exactly identical. It means this test and rule did not flag a difference in this sample.

## 10. Actual drifted-window results

You then ran:

```bash
python monitoring/simulate_traffic.py --mode drifted --count 300
```

The batch covered:

```text
Start: 2026-09-18T17:49:26.216116+00:00
End:   2026-09-18T17:50:07.515743+00:00
```

The corresponding checker command was:

```bash
python monitoring/check_drift.py \
  --start "2026-09-18T17:49:26.216116+00:00" \
  --end "2026-09-18T17:50:07.515743+00:00" \
  --expected-count 300
```

Observed results:

| Feature | Reference mean | Current mean | KS score | p-value | Result |
|---|---:|---:|---:|---:|---|
| Brightness | 0.5029 | 0.1940 | 0.7433 | 2.511e-155 | DRIFT FLAG |
| Motion | 0.2023 | 0.5884 | 0.8339 | 1.779e-208 | DRIFT FLAG |
| Face visibility | 0.8199 | 0.8238 | 0.03361 | 0.9043 | NO FLAG |
| Signal quality | 0.7137 | 0.7208 | 0.03583 | 0.8584 | NO FLAG |

Brightness shifted downward and motion upward, exactly as intended. The two unchanged distributions did not trigger the rule.

The very small p-values provide strong evidence of a distribution difference under the test assumptions. They do not quantify the business impact or accuracy loss.

## 11. The placeholder-timestamp error

The first attempt used literal placeholder strings:

```text
PASTE_NEW_START_TIMESTAMP
PASTE_NEW_END_TIMESTAMP
```

The checker returned zero records. That was a query-window error, not evidence that the simulator had failed to send requests or that SQLite had lost all records.

Replacing the placeholders with the actual timestamps returned all 300 records. No new traffic was needed.

Our original checker accepts arbitrary strings and does not validate timestamp syntax or ensure start is before end. A future improvement is to parse timezone-aware timestamps before querying and reject malformed input with a clear message.

The old commands above only find results if the original database records are still present. For a new simulation, use its newly printed timestamps.

## 12. What to investigate after a real flag

For a scan-quality application, possible causes include:

- Users scanning in darker environments.
- More movement because of a changed capture flow.
- A new camera or device population.
- Changes to preprocessing or feature normalization.
- A feature-extraction bug that still produces values within 0–1.
- Test or simulation traffic entering the monitored population.

Check the feature pipeline, affected devices or cohorts, recent releases and labeled performance before deciding on a response. A preprocessing bug should generally be corrected rather than accepted as a reason to retrain on corrupted inputs.

In our exercise, we know the cause because we intentionally changed two sampling distributions. In a real incident, the flag identifies a symptom, not a root cause.

## 13. Statistical and implementation limitations

### Marginal checks miss some joint changes

Each feature is tested independently. Relationships between features can change while every individual distribution remains similar. These checks would miss that kind of joint-distribution shift.

### Means are explanatory, not the test itself

The report prints means to help interpret direction, but the KS test uses the full empirical distributions. Distributions can have similar means and still differ in spread or shape.

### A fixed reference is a release assumption

The reference should be preserved with the model and its feature definitions. Silently replacing it with recent inputs can normalize away the change the monitor was intended to detect.

### Sample count is necessary but not sufficient

A count of exactly 300 confirms the expected number of selected rows in this controlled run. It does not prove those are the intended rows if unrelated concurrent traffic and missing records happen to offset each other. Explicit simulation IDs or traffic-source fields would improve attribution.

### This exercise is not a clinical performance study

The features and labels are synthetic. The successful experiment validates our monitoring mechanics for controlled shifts, not the suitability of any real scan-quality model.

## Completion criteria

- Normal and shifted requests pass through the actual API and storage path.
- The checker retrieves the intended window with the expected sample count.
- The normal window has no flags under the chosen rule.
- The shifted window flags brightness and motion.
- We distinguish input drift from prediction error and understand the test's limits.

The next chapter is **Step 8 — Automated rolling-window drift monitoring, Prometheus export and Grafana alert evaluation**.

## Reference

- [SciPy two-sample KS test: statistic, assumptions and p-value](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ks_2samp.html)
