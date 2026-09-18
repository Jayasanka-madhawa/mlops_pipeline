# Step 8 — Automated drift monitoring and Grafana alerts

## Purpose and scope

Step 7 checked two manually selected traffic windows. Step 8 removed the need to enter timestamps: a separate Python process periodically reads recent predictions, calculates feature drift, and exposes the results for Prometheus. Grafana evaluates a sustained-drift alert.

You verified that the alert reached **Firing**. This confirms the implemented monitoring and alert-evaluation path for our simulated shift; it does not establish a loss of predictive accuracy or confirm external notification delivery.

This guide documents the existing implementation. Code snippets explain it and should not be added a second time to your current project.

## 1. Runtime responsibilities

| Component | Responsibility |
|---|---|
| FastAPI | Validate inputs, predict, store prediction records |
| SQLite | Retain per-prediction inputs, outputs, timestamps and model IDs |
| Drift-monitor process | Read recent inputs, compare with reference, update drift metrics |
| Prometheus | Scrape and store those metrics |
| Grafana | Visualize metrics and evaluate the alert rule |

The drift calculation does not run inside `/predict`. The monitor reads the database independently and exposes metrics on port 8002.

A separate process removes KS computation from the request handler, but it still shares CPU, disk and SQLite resources with the API. It is not complete resource isolation.

## 2. Monitoring policy

We configured:

```python
WINDOW_MINUTES = 5
CHECK_INTERVAL = 30
MIN_SAMPLES = 100
P_THRESHOLD = 0.05 / 4
KS_THRESHOLD = 0.10
```

| Setting | Meaning |
|---|---|
| Five-minute window | Select predictions from the five minutes preceding the check |
| 30-second interval | Sleep 30 seconds after each completed check |
| 100 minimum samples | Skip drift calculation when insufficient recent records exist |
| p < 0.0125 | Four-feature Bonferroni-adjusted statistical threshold |
| KS >= 0.10 | Minimum observed distribution difference for a flag |

Because the loop sleeps after processing, the start-to-start interval is approximately check duration plus 30 seconds. This is a simple periodic loop, not a scheduler enforcing exact wall-clock execution times.

The sample floor and thresholds are teaching choices. They require calibration for a real application and do not constitute statistical power guarantees.

## 3. Select the model and matching reference

The monitor reads:

```python
MODEL_RUN_ID = os.environ["MODEL_RUN_ID"]
```

The initial reference was:

```text
data/reference_features.csv
```

Later, Docker packaging made the reference location configurable with `REFERENCE_PATH`, allowing each release image to contain its own reference file. The consolidated implementation below includes that later path configuration; omitting the environment variable preserves the original local behavior.

The model-version filter selects records from the intended release. It does not prove that the supplied reference file belongs to that model. In our implementation, this relationship is maintained by release packaging, not verified through a reference hash in model metadata.

The reference is loaded once at monitor startup. Changing the file on disk does not update the already loaded DataFrame; restart the process to use a new reference.

## 4. Read a rolling window

Each check calculates UTC boundaries:

```python
now = datetime.now(timezone.utc)
start = now - timedelta(minutes=WINDOW_MINUTES)
```

Then queries:

```sql
SELECT brightness, motion, face_visibility, signal_quality
FROM predictions
WHERE timestamp >= ?
  AND timestamp < ?
  AND model_version = ?;
```

This is a half-open window `[start, now)`. A prediction becomes irrelevant to the next calculation when its timestamp falls outside the recent window, even though its database row remains stored.

Unlike the manual checker, the automated monitor does not expect exactly 300 rows. It uses whatever matching recent population exists, provided at least 100 records are present.

It does not distinguish user traffic from simulations or load tests. All matching records in that window contribute to the calculation.

### Read-only connection

We open the database using:

```python
sqlite3.connect(
    f"{DB_PATH.as_uri()}?mode=ro",
    uri=True,
    timeout=5,
)
```

Read-only mode prevents accidental creation of an empty database at an incorrect path and prevents writes through that connection. It does not eliminate locking or disk contention.

As noted in Step 5, the SQLite connection context manager controls transactions but does not explicitly close the connection. The original implementation relies on object cleanup. Deterministic closure with `contextlib.closing` is a possible cleanup, not an already applied change.

## 5. Expose metrics for results and monitor health

| Metric | Type | Meaning |
|---|---|---|
| `scan_drift_ks_score{feature=...}` | Gauge | Current KS statistic for each feature |
| `scan_drift_flag{feature=...}` | Gauge | 1 if both drift conditions are met, otherwise 0 |
| `scan_drift_window_samples` | Gauge | Number of records selected in the current window |
| `scan_drift_ready` | Gauge | 1 if the latest check produced results from enough valid data |
| `scan_drift_last_success_timestamp_seconds` | Gauge | Unix timestamp of the last successful drift calculation |
| `scan_drift_check_errors_total` | Counter | Number of failed checks since process startup |

A gauge is suitable for values that can rise or fall. The `feature` label has a bounded set of four values.

The metrics do not include a model-version label in this version. The process is configured for one model, but changing that configuration at the same target does not create separate model-labeled time series. Release-aware historical dashboards would require additional metadata or labels.

## 6. Distinguish no drift from no usable result

Our monitor has three main outcomes:

| Outcome | Ready | Scores and flags | Sample count | Last-success timestamp |
|---|---:|---|---|---|
| Successful calculation | 1 | Fresh values | Current count | Updated |
| Fewer than 100 recent records | 0 | NaN | Current count | Unchanged |
| Check raises an exception | 0 | NaN | NaN | Unchanged |

We intentionally clear unavailable results to NaN, not zero. A zero flag means the rule did not detect drift; missing or invalid data does not justify that conclusion.

The last-success timestamp starts at zero. Before any successful calculation, `time() - last_success` is therefore very large. The readiness signal helps distinguish this state from a valid fresh result.

A process can be reachable while its calculations fail. Prometheus target **UP** alone is not enough; readiness, freshness and errors provide additional evidence.

## 7. Consolidated monitor implementation

File: `monitoring/drift_monitor.py`.

This version includes the optional `REFERENCE_PATH` support added for Docker. The drift algorithm and metric behavior match our original monitor.

```python
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from prometheus_client import Counter, Gauge, start_http_server
from scipy.stats import ks_2samp

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "predictions.db"
FEATURES = [
    "brightness",
    "motion",
    "face_visibility",
    "signal_quality",
]
MODEL_RUN_ID = os.environ["MODEL_RUN_ID"]

WINDOW_MINUTES = 5
CHECK_INTERVAL = 30
MIN_SAMPLES = 100
P_THRESHOLD = 0.05 / len(FEATURES)
KS_THRESHOLD = 0.10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("drift-monitor")

KS_SCORE = Gauge(
    "scan_drift_ks_score", "KS distribution difference", ["feature"]
)
DRIFT_FLAG = Gauge(
    "scan_drift_flag", "1 means drift flagged, 0 means no flag", ["feature"]
)
SAMPLES = Gauge(
    "scan_drift_window_samples", "Prediction count in the current window"
)
READY = Gauge(
    "scan_drift_ready", "1 when the latest check has enough valid data"
)
LAST_SUCCESS = Gauge(
    "scan_drift_last_success_timestamp_seconds",
    "Time of the last successful drift calculation",
)
ERRORS = Counter(
    "scan_drift_check_errors_total", "Failed monitoring checks"
)


def clear_results():
    READY.set(0)
    for feature in FEATURES:
        KS_SCORE.labels(feature=feature).set(float("nan"))
        DRIFT_FLAG.labels(feature=feature).set(float("nan"))


def read_window():
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=WINDOW_MINUTES)

    with sqlite3.connect(
        f"{DB_PATH.as_uri()}?mode=ro", uri=True, timeout=5
    ) as connection:
        return pd.read_sql_query(
            """
            SELECT brightness, motion, face_visibility, signal_quality
            FROM predictions
            WHERE timestamp >= ?
              AND timestamp < ?
              AND model_version = ?
            """,
            connection,
            params=(start.isoformat(), now.isoformat(), MODEL_RUN_ID),
        )


def check_drift(reference):
    production = read_window()
    SAMPLES.set(len(production))

    if len(production) < MIN_SAMPLES:
        clear_results()
        logger.info(
            "Waiting for data: %s/%s recent predictions",
            len(production), MIN_SAMPLES,
        )
        return

    if not np.isfinite(production[FEATURES].to_numpy(dtype=float)).all():
        raise ValueError("Invalid production feature values")

    results = {}
    for feature in FEATURES:
        test = ks_2samp(reference[feature], production[feature])
        flagged = test.pvalue < P_THRESHOLD and test.statistic >= KS_THRESHOLD
        results[feature] = (float(test.statistic), int(flagged))

    for feature, (score, flagged) in results.items():
        KS_SCORE.labels(feature=feature).set(score)
        DRIFT_FLAG.labels(feature=feature).set(flagged)

    LAST_SUCCESS.set_to_current_time()
    READY.set(1)

    flagged_features = [
        feature for feature, (_, flagged) in results.items() if flagged
    ]
    logger.info(
        "Samples=%s | Flagged=%s",
        len(production), flagged_features or "none",
    )


def main():
    reference_path = Path(os.environ.get(
        "REFERENCE_PATH",
        str(ROOT / "data" / "reference_features.csv"),
    ))
    reference = pd.read_csv(reference_path)

    if len(reference) < MIN_SAMPLES or not np.isfinite(
        reference[FEATURES].to_numpy(dtype=float)
    ).all():
        raise ValueError("Reference data is insufficient or invalid")

    clear_results()
    start_http_server(8002, addr="0.0.0.0")
    logger.info("Metrics available on port 8002")

    while True:
        try:
            check_drift(reference)
        except Exception:
            clear_results()
            SAMPLES.set(float("nan"))
            ERRORS.inc()
            logger.exception("Drift check failed")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
```

### Publication consistency

The script computes all four results before updating gauges. This avoids publishing a partially computed check when a calculation itself fails.

However, individual gauge updates are not an atomic snapshot. The HTTP metrics server can theoretically scrape while the result values are being updated. For this single-process teaching monitor, updates are brief; a stricter design would expose an immutable snapshot through a custom collector or coordinated locking.

During a check that stalls, previous readiness can remain 1 until the check finishes or raises. The freshness condition in the alert prevents indefinitely trusting old results.

## 8. Start and connect the monitor

For the original host-run setup:

```bash
conda activate mlops
export MODEL_RUN_ID=d9571388a61f45369e4f709878e8246b
python monitoring/drift_monitor.py
```

Prometheus configuration includes a second job:

```yaml
  - job_name: "scan-drift-monitor"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["host.docker.internal:8002"]
```

After moving the monitor into Compose, the target becomes `drift-monitor:8002` instead. Use the address for the actual deployment; do not scrape both copies unintentionally.

We restarted Prometheus after editing its configuration:

```bash
docker compose restart prometheus
```

Both jobs should appear at `http://localhost:9090/targets`. For the host-run monitor, its metrics are also visible locally at `http://localhost:8002/metrics`. The later Compose configuration does not publish monitor port 8002 to the host; Prometheus reaches it through the internal network.

## 9. Dashboard queries

We added these panels:

| Panel | PromQL | Suggested visualization |
|---|---|---|
| Drift score by feature | `scan_drift_ks_score{job="scan-drift-monitor"}` | Time series |
| Drift flags | `scan_drift_flag{job="scan-drift-monitor"}` | Stat |
| Recent predictions | `scan_drift_window_samples{job="scan-drift-monitor"}` | Stat |
| Monitor ready | `scan_drift_ready{job="scan-drift-monitor"}` | Stat |
| Seconds since successful check | `time() - scan_drift_last_success_timestamp_seconds{job="scan-drift-monitor"}` | Stat |

Use `{{feature}}` as the legend for feature-specific series.

Freshness normally remains near the checking interval when enough data is available, plus processing and scrape delay. An exact maximum of 30 seconds is not guaranteed.

A Stat panel configured to show the last non-null value over a long range can display an older value even after the current score becomes NaN. Pair feature panels with readiness and freshness, or use an instant query for the current state. Do not infer health from an old displayed zero.

## 10. Construct the alert query

Our Grafana-managed rule used an instant Prometheus query:

```promql
max(scan_drift_flag{job="scan-drift-monitor"})
and on()
(max(scan_drift_ready{job="scan-drift-monitor"}) == 1)
and on()
(
  time() -
  max(scan_drift_last_success_timestamp_seconds{job="scan-drift-monitor"})
  < 90
)
```

### Clause 1: any flagged feature

`max(scan_drift_flag)` reduces the four feature flags to one value. It is 1 if any feature is flagged and 0 if all available flags are zero.

### Clause 2: usable latest result

`max(scan_drift_ready) == 1` filters out results when readiness is not 1. Without the `bool` modifier, the comparison retains matching samples and removes non-matching ones; it does not return a universal 0/1 value for both cases.

### Clause 3: recent calculation

The last successful check must be younger than 90 seconds. This avoids treating old flags as fresh evidence if the loop stops progressing while its HTTP endpoint remains reachable.

### Why `and on()`?

These are vector set intersections, not arithmetic multiplication. The left-hand result survives only when the corresponding eligibility checks produce a matching series. `on()` explicitly matches on an empty label set after aggregation.

The query is designed for our one-monitor setup. With multiple independent monitors, separate `max` aggregations could combine a flag from one instance with readiness or freshness from another. A scaled design must preserve and match instance/model labels before aggregating.

## 11. Grafana rule configuration

| Setting | Our choice |
|---|---|
| Rule name | Scan input drift |
| Data source | Prometheus |
| Query type | Instant |
| Threshold | Query value above 0.5 |
| Folder | Scan monitoring |
| Evaluation group | scan-monitoring |
| Evaluation interval | 30 seconds |
| Pending period | 1 minute |
| Keep firing for | None |
| Missing-data behavior | No Data |
| Execution-error behavior | Error |
| Summary | Scan input drift detected for at least one minute. |

The pending period requires the condition to remain breached across evaluations before firing. It does not require a full new independent dataset at every evaluation: the rolling windows overlap substantially.

The three relevant cadences are independent:

- The monitor calculates approximately every 30 seconds plus computation time.
- Prometheus scrapes every five seconds.
- Grafana evaluates the alert every 30 seconds.

Consequently, an alert does not necessarily fire exactly 60 seconds after the first shifted request. There must first be enough recent data, a completed check, a scrape, and alert evaluations that sustain the condition.

## 12. Alert states and data expiry

| State or condition | Interpretation |
|---|---|
| Normal | Fresh, eligible query result is below the alert threshold |
| Pending | Drift condition is true but the pending duration has not elapsed |
| Firing | Drift condition has remained true long enough |
| No Data handling | The query provides no eligible result, for example because readiness is zero |
| Error handling | Grafana cannot successfully evaluate the query |

No Data is not equivalent to confirmed recovery. If traffic stops, the recent population eventually drops below 100 and the query becomes ineligible. That means monitoring lacks a usable current window; it does not prove input distributions returned to normal.

Grafana can surface missing data or evaluation errors as separate alert instances depending on its configured handling. Inspect rule health and instances rather than assuming a former drift alert becoming inactive means recovery.

A genuine healthy comparison requires a fresh, sufficiently large window whose feature flags are all zero.

## 13. What we verified

We generated fresh shifted traffic:

```bash
python monitoring/simulate_traffic.py --mode drifted --count 300
```

The monitor accumulated enough records, calculated flags, and exported results. You shared the Grafana rule page showing:

```text
Scan input drift
Firing
Evaluation interval: Every 30s
Scan input drift detected for at least one minute.
```

This verifies that the rule's threshold and duration conditions were met for the simulated input shift.

The page also showed notification routing to an `empty` destination. No real external recipient was configured, so we did not verify email, Slack or other message delivery. An alert firing and a notification being delivered are separate outcomes.

## 14. Recovery and controlled comparisons

If you want to demonstrate recovery, stop shifted traffic, allow those records to leave the five-minute window, and generate enough fresh normal traffic to keep the sample count above 100.

Immediately sending normal traffic after shifted traffic creates a mixed window. Its result depends on the mixture; it is not an isolated normal-only experiment.

When no traffic remains, expect insufficient data rather than a zero-drift claim. This is why sample count and readiness belong on the same dashboard as drift scores.

## 15. Failure handling and remaining limits

- Missing or invalid reference data prevents startup before the metrics server begins.
- Runtime database or calculation errors clear results, increment the error counter, and allow later retries through the main loop.
- A stopped process cannot update its own error counter; Prometheus target health detects the scrape failure.
- The freshness gate helps detect stalled calculations, but we did not add a separate monitor-health alert in the minimal version.
- All calculations are univariate. Joint feature shifts can go undetected.
- Repeated testing across windows needs additional false-alarm calibration beyond a four-feature Bonferroni correction.
- Recent recorded inputs may be biased if some prediction writes fail. Readiness based on stored-row count cannot establish complete population coverage.
- We do not automatically retrain or roll back after a flag. Investigation is the next action.

## Completion criteria

- A separate process automatically selects recent records for the configured model.
- Insufficient data and failed calculations are distinguishable from no drift.
- Prometheus collects drift and monitor-health metrics.
- Grafana displays scores, sample counts, readiness and freshness.
- The sustained-drift rule reaches Firing under the controlled shifted scenario.
- External notification delivery is understood to be unconfigured in this first version.

The next chapter is **Step 9 — API tests, concurrent load checks and final held-out model evaluation**.

## Official references used during implementation

- [Prometheus Python gauges](https://prometheus.github.io/client_python/instrumenting/gauge/)
- [SciPy two-sample KS test](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ks_2samp.html)
- [Grafana-managed alert rule configuration](https://grafana.com/docs/grafana/latest/alerting/alerting-rules/create-grafana-managed-rule/)
