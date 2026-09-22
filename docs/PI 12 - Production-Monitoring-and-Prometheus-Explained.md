# Production Monitoring and Prometheus — Our Project

**Our monitoring checks whether the API is working properly and whether incoming scan data has changed. Prometheus collects the monitoring numbers, and Grafana displays them and evaluates our alert.**

## 1. What each component does

| Component | Responsibility |
|---|---|
| FastAPI instrumentation | Measures requests, errors, and processing time |
| SQLite prediction log | Stores individual inputs and predictions |
| Drift monitor | Compares recent inputs with training reference data |
| Prometheus | Collects and stores metrics over time |
| Grafana | Queries metrics, displays charts, and evaluates alerts |

The API and drift monitor run in Kubernetes. Prometheus and Grafana run in Docker Compose. Prometheus joins the Kind Docker network to reach the Kubernetes application.

## 2. Monitoring a prediction request

Suppose the API receives:

```json
{
  "brightness": 0.5,
  "motion": 0.1,
  "face_visibility": 0.95,
  "signal_quality": 0.9
}
```

It validates the input, prepares the features, runs the model, and returns a prediction. Around this work, instrumentation records:

| Metric | What it tells us |
|---|---|
| `scan_api_requests_total` | Prediction request count, grouped by HTTP status |
| `scan_api_duration_seconds` | Time spent handling a prediction request on the server |
| `scan_inference_duration_seconds` | Time spent preparing features and performing inference |
| `scan_db_write_duration_seconds` | Time spent attempting to save a prediction |
| `scan_prediction_log_failures_total` | Number of failed prediction-log writes |

The request and failure counts are **counters**: they accumulate events during a process's lifetime. The duration metrics are **histograms**: they record counts, total duration, and bucket counts for timing analysis.

For example, if request latency rises while inference time stays stable, database logging or other request-handling work may be responsible. These server timings do not include the client's full network round trip.

## 3. Why we save predictions in SQLite

Metrics summarize activity. The prediction log stores individual records for analysis.

| Saved field | Purpose |
|---|---|
| Prediction ID | Identifies the request's prediction |
| Timestamp | Selects records within a time window |
| Model version | Identifies the model that produced the result |
| Four input features | Enables input-drift analysis |
| Predicted class and probability | Records the model output |
| Threshold | Explains how the class was selected |
| Inference time | Records processing time for that prediction |

The API writes SQLite records on the persistent volume. The drift monitor reads them through its read-only mount.

**If logging fails, the API still returns the prediction**, but increments the logging-failure counter. Successful requests therefore do not guarantee complete monitoring data.

Prometheus does not store these individual prediction rows or read SQLite. It stores the numeric metrics exposed by our processes.

## 4. How the drift monitor works

The monitor loads `reference_features.csv`, containing the **3,600 training reference records**.

Approximately every **30 seconds**, it:

1. Reads predictions from the most recent **five minutes**.
2. Filters them to the configured model run ID.
3. Checks that at least **100 records** are available.
4. Compares each input feature with its reference distribution.
5. Publishes the results as metrics.

| Feature | Training reference example | Recent-input example |
|---|---|---|
| Brightness | Mostly moderate brightness | Mostly dark scans |
| Motion | Usually low movement | Much more movement |

Such changes might come from lighting, user behavior, devices, or upstream feature processing. The drift flag identifies a change; it does not identify its cause by itself.

## 5. How we decide whether drift occurred

We use the **two-sample Kolmogorov–Smirnov test**, or **KS test**, for each feature. It compares distributions, not just averages.

Our rule is:

```text
Flag drift when:

p-value < 0.0125
AND
KS score >= 0.10
```

- **KS score:** measures the largest difference between the two empirical cumulative distributions.
- **p-value:** measures evidence against the assumption that both samples come from the same distribution.
- **0.0125:** equals `0.05 / 4`, accounting for the four feature tests within one evaluation.

These are our project's chosen thresholds, not universal settings. Repeated evaluations over time can still produce false alarms, and overlapping windows are not independent observations.

## 6. Metrics published by the drift monitor

| Metric | Meaning |
|---|---|
| `scan_drift_ks_score{feature="brightness"}` | Distribution difference for brightness |
| `scan_drift_flag{feature="brightness"}` | `1` if flagged; `0` otherwise after a completed evaluation |
| `scan_drift_window_samples` | Records in the current window |
| `scan_drift_ready` | Whether data was sufficient and the evaluation completed |
| `scan_drift_last_success_timestamp_seconds` | Time of the last completed drift evaluation |
| `scan_drift_check_errors_total` | Errors encountered during monitoring checks |

The current-state measurements are gauges; the error count is a counter.

When there are too few records, the monitor sets readiness to zero and its drift results to unavailable values. **Insufficient data is not the same as no drift.**

A Kubernetes-ready monitor only means its metrics port is reachable. It can still report `scan_drift_ready = 0`.

## 7. How Prometheus receives the metrics

Both processes expose a `/metrics` endpoint. These endpoints return text containing metric names and values. For example:

```text
scan_api_requests_total{status="200"} 300
scan_drift_flag{feature="brightness"} 1
scan_drift_window_samples 300
```

These illustrative lines come from the two different endpoints. They mean:

- 300 successful prediction requests have been counted.
- Brightness is currently flagged for drift.
- The current drift window contains 300 records.

Prometheus periodically fetches this text. This is called **scraping**: Prometheus pulls metrics from the applications.

Our configuration in `monitoring/prometheus.yml` is:

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: "scan-quality-api"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["mlops-control-plane:30081"]

  - job_name: "scan-drift-monitor"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["mlops-control-plane:30082"]
```

| Address | Destination |
|---|---|
| `mlops-control-plane:30081/metrics` | API container through the Kubernetes Service |
| `mlops-control-plane:30082/metrics` | Drift-monitor container through the Kubernetes Service |

Prometheus reaches these NodePorts through the shared Kind Docker network. Metrics scraping does not depend on a terminal running `kubectl port-forward`.

**Prometheus does not run predictions, read SQLite, or calculate the KS test. It collects the results exposed by our code.**

## 8. Prometheus stores values over time

For each metric and combination of labels, Prometheus stores timestamped samples. These form a **time series**.

An example request counter:

| Time | Successful request counter |
|---|---:|
| 10:00:00 | 300 |
| 10:00:05 | 320 |
| 10:00:10 | 345 |

Labels identify which series a sample belongs to. Examples include the HTTP `status`, input `feature`, scrape `job`, and target `instance`.

Prometheus stores its history in the `prometheus_data` Docker volume. Recreating the container preserves that volume's data, subject to configured retention. Application counters can reset when their process restarts; Prometheus rate functions account for observed counter resets.

## 9. Grafana queries Prometheus

Grafana connects to:

```text
http://prometheus:9090
```

It uses **PromQL**, Prometheus's query language, to calculate values for charts.

### Requests per second

```promql
sum(rate(scan_api_requests_total{job="scan-quality-api"}[1m]))
```

This estimates the average per-second request rate over the last minute. The raw counter itself is a cumulative count, not requests per second.

### Average API latency in milliseconds

```promql
1000 *
sum(rate(scan_api_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_api_duration_seconds_count{job="scan-quality-api"}[1m]))
```

This divides the duration rate by the request-count rate. Without recent observations, the result may be unavailable rather than a meaningful zero.

### Current drift flags

```promql
scan_drift_flag{job="scan-drift-monitor"}
```

Other panels can show error rates, inference time, logging failures, sample count, and KS scores. A metric being available does not mean a panel for it has already been created.

## 10. How our Grafana drift alert works

The **Scan input drift** alert checks that:

1. At least one feature is flagged.
2. The monitor is ready for drift evaluation.
3. Its last successful evaluation is less than **90 seconds old**.

Our query is:

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

Grafana tests whether the result is above `0.5`, evaluating every **30 seconds**. The condition must remain true for the configured **one-minute pending period** before firing.

The freshness condition prevents an old drift result from being treated as current. If the query produces no data, Grafana's configured no-data handling applies. Missing or stale monitoring data is not evidence that the inputs are healthy.

This query is designed for our single drift monitor. If we add replicas, we must revisit label matching so readiness and freshness belong to the same monitor as the drift result.

**The alert can show Firing in Grafana, but external email or Slack notifications are not configured yet.**

## 11. What does UP mean?

At http://localhost:9090/targets, a target showing **UP** means its latest metrics scrape succeeded.

It does not prove that:

- Predictions are correct.
- Every prediction was logged.
- Enough records exist for drift evaluation.
- The drift monitor's latest calculation succeeded.

Use target health alongside the application and drift metrics.

## 12. What monitoring does not tell us yet

**Input drift does not automatically mean prediction accuracy declined.**

To measure real performance, we need actual labels:

```text
Prediction: acceptable
Actual reviewed outcome: poor
```

We would join the actual outcome to the saved prediction using its prediction ID, then calculate precision, recall, and other performance metrics by model version.

Our project does not currently implement:

- Labeled production-performance monitoring.
- Automatic retraining.
- Automatic rollback.
- External alert notifications.
- Comprehensive Kubernetes CPU, memory, and restart dashboards.

## 13. What to do when monitoring detects a problem

| Observation | Investigation or action |
|---|---|
| Request latency rises | Compare inference and database timing; inspect logs and resource pressure |
| API errors increase after release | Inspect the release and consider restoring the previous image through Git |
| Prediction-log failures increase | Check storage, database access, and missing monitoring records |
| Input drift is flagged | Check input quality, devices, user behavior, and upstream feature processing |
| Drift data is stale or insufficient | Check monitor errors, traffic volume, logging success, and model-version filtering |
| Labeled model performance declines | Investigate data and code changes, then evaluate whether retraining is needed |

## 14. Current verification status

Both Prometheus targets have been confirmed **UP** against Kubernetes. The drift alert was tested earlier in the local setup.

The remaining end-to-end Kubernetes check is to send normal traffic, confirm charts and drift readiness, then send deliberately drifted traffic and confirm the Grafana alert fires.

**Our code measures behavior and calculates drift. Prometheus stores the resulting metrics. Grafana makes them visible and evaluates the alert.**
