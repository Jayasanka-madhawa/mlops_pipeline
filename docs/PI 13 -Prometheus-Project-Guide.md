# Prometheus in our scan-quality MLOps project

## 1. Where we use Prometheus and why

We use Prometheus to collect time-series measurements from the running API and drift monitor. It helps us determine whether the service is reachable, how quickly requests are processed, whether prediction logging fails, and whether input distributions have changed.

| Component | Responsibility |
|---|---|
| Python `prometheus-client` | Maintain counters, histograms and gauges in each process |
| API `/metrics` | Expose API measurements over HTTP |
| Drift-monitor metrics server | Expose drift results and monitor-health measurements on port 8002 |
| Prometheus server | Periodically scrape these endpoints and store historical samples |
| Grafana | Query Prometheus, draw dashboards and evaluate our drift alert |

Prometheus does not train models, save individual prediction records or compute our KS drift test. Our Python code performs those operations; Prometheus collects the resulting aggregate measurements.

## 2. What we monitor

### API metrics

| Metric | Type | Meaning |
|---|---|---|
| `scan_api_requests_total{status=...}` | Counter | Requests to `/predict`, separated by HTTP status |
| `scan_api_duration_seconds` | Histogram | API processing duration |
| `scan_inference_duration_seconds` | Histogram | Feature preparation and prediction duration |
| `scan_db_write_duration_seconds` | Histogram | Duration of attempts to save prediction records |
| `scan_prediction_log_failures_total` | Counter | Failed prediction writes |

### Drift-monitor metrics

| Metric | Type | Meaning |
|---|---|---|
| `scan_drift_ks_score{feature=...}` | Gauge | Latest distribution difference per feature |
| `scan_drift_flag{feature=...}` | Gauge | 1 if flagged, 0 if not flagged; NaN if unavailable |
| `scan_drift_window_samples` | Gauge | Number of recent records selected |
| `scan_drift_ready` | Gauge | Whether the latest check produced usable results |
| `scan_drift_last_success_timestamp_seconds` | Gauge | Time of the last successful drift calculation |
| `scan_drift_check_errors_total` | Counter | Failed checks since monitor startup |

Prometheus also creates the `up` metric for each target. A successful scrape gives `up=1`; a failed scrape gives `up=0`. A successful scrape does not prove that predictions or drift calculations are working correctly.

## 3. Understand the metric types

### Counter

A counter accumulates events and resets when the process restarts:

```python
REQUESTS.labels(status="200").inc()
LOG_FAILURES.inc()
```

Use `rate` to calculate events per second or `increase` to estimate events over a range. These functions account for observed counter resets.

### Histogram

A histogram groups duration observations into cumulative buckets and records their sum and count:

```python
INFERENCE_DURATION.observe(0.05)
```

This records 0.05 seconds, or 50 milliseconds. Exported series include:

```text
scan_inference_duration_seconds_bucket{le="0.1"}
scan_inference_duration_seconds_sum
scan_inference_duration_seconds_count
```

Buckets are cumulative: a 0.05-second observation contributes to every bucket whose upper bound is at least 0.05. Sum/count supports averages; buckets support approximate percentiles.

### Gauge

A gauge represents a current value that can rise or fall:

```python
SAMPLES.set(300)
READY.set(1)
KS_SCORE.labels(feature="motion").set(0.8339)
LAST_SUCCESS.set_to_current_time()
```

Unlike a counter, a gauge should not be interpreted as an accumulated event total.

## 4. Define the API metrics

Our `app/metrics.py` contains:

```python
from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "scan_api_requests_total",
    "Number of prediction requests",
    ["status"],
)
API_DURATION = Histogram(
    "scan_api_duration_seconds",
    "Prediction API processing time including database writing",
)
INFERENCE_DURATION = Histogram(
    "scan_inference_duration_seconds",
    "Feature preparation and model prediction time",
)
DB_DURATION = Histogram(
    "scan_db_write_duration_seconds",
    "Time spent attempting to save a prediction",
)
LOG_FAILURES = Counter(
    "scan_prediction_log_failures_total",
    "Number of predictions that could not be saved",
)
```

These objects register once when the module is imported. Creating a collector with the same name repeatedly can cause duplicate-registration errors. Request handlers should update the existing collectors rather than create new ones.

## 5. Instrument requests with middleware

In `app/main.py`, we import the metrics and HTTP helpers:

```python
from time import perf_counter
from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.metrics import (
    REQUESTS, API_DURATION, INFERENCE_DURATION,
    DB_DURATION, LOG_FAILURES,
)
```

After creating the FastAPI application:

```python
@app.middleware("http")
async def monitor_prediction_requests(request: Request, call_next):
    if request.url.path != "/predict":
        return await call_next(request)

    started = perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        REQUESTS.labels(status=status).inc()
        API_DURATION.observe(perf_counter() - started)
```

The middleware wraps downstream request handling. It captures invalid-input responses such as 422 as well as successful predictions. The default 500 records an unhandled failure when no response is returned.

Only the path is checked, so other HTTP methods sent to `/predict` can also be counted, for example as 405 responses. These metrics describe requests, not exclusively completed model predictions.

The timer ends when downstream response creation returns. For our JSON endpoint, it includes validation, inference and synchronous storage. It excludes complete network delivery and is not a full streamed-response timer.

We exclude `/health`, `/metrics` and documentation requests from these custom prediction metrics.

## 6. Measure inference and SQLite overhead

Inside the prediction function, after calculating `inference_ms`:

```python
INFERENCE_DURATION.observe(inference_ms / 1000)
```

Around the existing storage operation:

```python
try:
    with DB_DURATION.time():
        save_prediction(record)
except Exception:
    LOG_FAILURES.inc()
    logger.exception(
        "Failed to store prediction %s", prediction_id
    )
```

The API returns a prediction even if storage fails. Therefore HTTP 200 does not prove successful logging. The failure counter exposes this otherwise hidden problem.

The inference timer excludes database writing. The database timer measures the save attempt, including transaction handling. The middleware timer measures a broader portion of request processing.

These histograms do not necessarily have equal counts: an invalid request contributes to the API histogram without reaching inference or storage.

## 7. Expose `/metrics`

Our API route is:

```python
@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(
        content=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
```

`generate_latest()` serializes the registry's current values. Hiding the endpoint from OpenAPI does not secure it; it remains reachable on the application's network interface.

Inspect it locally:

```bash
curl -s http://127.0.0.1:8001/metrics
```

Example after one successful request:

```text
scan_api_requests_total{status="200"} 1.0
scan_inference_duration_seconds_count 1.0
scan_db_write_duration_seconds_count 1.0
scan_prediction_log_failures_total 0.0
```

Actual values depend on traffic since startup. Additional default Python metrics can appear, and some process metrics are platform-dependent.

## 8. Export drift results

The separate `monitoring/drift_monitor.py` process declares:

```python
from prometheus_client import Counter, Gauge, start_http_server

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
```

It starts its own HTTP metrics server:

```python
start_http_server(8002, addr="0.0.0.0")
```

After Python reads recent SQLite records and computes the KS tests, it publishes values:

```python
SAMPLES.set(len(production))

for feature, (score, flagged) in results.items():
    KS_SCORE.labels(feature=feature).set(score)
    DRIFT_FLAG.labels(feature=feature).set(flagged)

LAST_SUCCESS.set_to_current_time()
READY.set(1)
```

When there is insufficient data or a failed calculation, it sets readiness to zero and feature scores/flags to NaN. Errors also increment the error counter. This avoids presenting an unavailable result as zero drift.

The drift algorithm itself remains Python/SciPy code. The exporter only makes its results observable.

## 9. Configure Prometheus scraping

For the original setup, where both Python processes run on the Mac and Prometheus runs in Docker, `monitoring/prometheus.yml` is:

```yaml
global:
  scrape_interval: 5s

scrape_configs:
  - job_name: "scan-quality-api"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["host.docker.internal:8001"]

  - job_name: "scan-drift-monitor"
    metrics_path: "/metrics"
    static_configs:
      - targets: ["host.docker.internal:8002"]
```

After moving the API and monitor into the same Compose network as Prometheus, change the targets to:

```yaml
      - targets: ["api:8001"]
```

and:

```yaml
      - targets: ["drift-monitor:8002"]
```

These are alternatives for different runtime layouts, not four targets to enable simultaneously.

Every scrape requests the current exposed values. Prometheus adds labels such as `job` and `instance` and stores timestamped samples. The process is pull-based; our API does not send each event directly to the Prometheus server.

A five-second scrape interval does not mean a drift calculation runs every five seconds. Our drift loop calculates approximately every 30 seconds plus processing time, so multiple scrapes can observe the same calculation result.

## 10. Run the server and retain history

The Prometheus service in Compose uses:

```yaml
services:
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus

volumes:
  prometheus_data:
```

This is a service excerpt; preserve your existing API, monitor and Grafana definitions.

Start it:

```bash
docker compose up -d prometheus
```

After configuration edits, our simple workflow restarts it:

```bash
docker compose restart prometheus
```

The named volume preserves collected history across container replacement, provided the volume is retained. It does not preserve Python's in-memory counters when an API process restarts. Avoid `docker compose down -v` if you intend to retain monitoring volumes.

`latest` is convenient for the exercise but is a mutable image tag. Pin a tested version or digest for reproducible releases.

## 11. Verify collection

Open `http://localhost:9090/targets` and inspect both jobs.

Useful queries:

```promql
up{job="scan-quality-api"}
```

```promql
up{job="scan-drift-monitor"}
```

A target can be UP while the drift monitor has `scan_drift_ready=0`, because its HTTP exporter is reachable even when there are too few samples or its calculations fail.

Prometheus stores observations at scrape times. Events lost before any scrape are not guaranteed to appear in history; this is monitoring, not durable per-event auditing.

## 12. Grafana connection

Our Grafana Prometheus data-source URL is:

```text
http://prometheus:9090
```

Grafana queries from its container, so `prometheus` is the Compose service name. `localhost` inside that container would refer to Grafana's own container.

The browser uses `http://localhost:3000` for Grafana and `http://localhost:9090` for Prometheus. These host-facing addresses are different from container-to-container addresses.

## 13. Queries used in our dashboard

### Request rate

```promql
sum(rate(scan_api_requests_total{job="scan-quality-api"}[1m]))
```

This estimates requests per second over the last minute. It combines status series and handles observed counter resets.

### Average API processing time, milliseconds

```promql
1000 *
sum(rate(scan_api_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_api_duration_seconds_count{job="scan-quality-api"}[1m]))
```

### Average inference time, milliseconds

```promql
1000 *
sum(rate(scan_inference_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_inference_duration_seconds_count{job="scan-quality-api"}[1m]))
```

### Average database time, milliseconds

```promql
1000 *
sum(rate(scan_db_write_duration_seconds_sum{job="scan-quality-api"}[1m]))
/
sum(rate(scan_db_write_duration_seconds_count{job="scan-quality-api"}[1m]))
```

The sum-rate divided by count-rate gives average seconds per observation. Multiplication by 1,000 converts seconds to milliseconds.

No recent observations can result in undefined averages and chart gaps. That is different from a zero-duration request.

### Server errors per second

```promql
sum(rate(scan_api_requests_total{
  job="scan-quality-api", status=~"5.."
}[1m])) or vector(0)
```

The zero fallback helps when no server-error series has been created, but it can also conceal missing metrics. Always interpret this alongside target health.

### Logging failures since process restart

```promql
sum(scan_prediction_log_failures_total{job="scan-quality-api"})
```

### Feature drift and readiness

```promql
scan_drift_ks_score{job="scan-drift-monitor"}
```

```promql
scan_drift_flag{job="scan-drift-monitor"}
```

```promql
scan_drift_window_samples{job="scan-drift-monitor"}
```

```promql
scan_drift_ready{job="scan-drift-monitor"}
```

### Seconds since the last successful calculation

```promql
time() - scan_drift_last_success_timestamp_seconds{job="scan-drift-monitor"}
```

Before the first successful calculation, the timestamp gauge is zero, so this age is very large. Readiness helps interpret that state.

### Optional p95 extension

The existing histogram also supports an approximate API p95:

```promql
1000 * histogram_quantile(
  0.95,
  sum by (le) (
    rate(scan_api_duration_seconds_bucket{job="scan-quality-api"}[5m])
  )
)
```

This is bucket-based estimation of server processing time. It is not identical to the load script's percentile over individual client-observed durations.

## 14. How Prometheus supports our alert

Grafana evaluates this query against Prometheus:

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

It selects the maximum feature flag only when the monitor is ready and its latest successful result is younger than 90 seconds.

Grafana evaluates every 30 seconds and fires if the value stays above 0.5 for one minute. You verified the Firing state. External delivery was not configured; the displayed destination was `empty`.

This is a Grafana-managed alert. We did not configure a separate Prometheus rule file or a standalone Alertmanager for this workflow.

The query assumes one monitor. For multiple models or instances, preserve identity labels when matching flags, readiness and freshness; independent maxima could otherwise combine measurements from different monitors.

## 15. Important limitations

- Our API uses one worker. Multiple workers need an appropriate aggregation design or client multiprocess configuration; simply adding workers can make metrics inconsistent.
- Metric counters reset on process restart. Prometheus retains scraped history, not the old process's memory.
- Current metrics do not label the model or application release. Prediction records carry model identity, but historical metric attribution across releases is less explicit.
- Do not use scan IDs, UUIDs or arbitrary request payload values as metric labels. Their unbounded variety creates excessive time-series cardinality.
- No-data and stale-data states must not be presented as confirmed health.
- Prometheus does not establish model accuracy without suitable measurements derived from predictions and actual labels. Our first version does not collect those labels.
- Average API, inference and DB times can cover different request populations. Subtracting averages or percentiles is not necessarily an exact overhead measurement.

## 16. Troubleshooting

| Symptom | Check |
|---|---|
| Target DOWN | Process running, correct hostname, port and bind address |
| API metrics visible but Grafana empty | Data-source URL, selected query, time range and recent traffic |
| Drift exporter UP but no scores | At least 100 matching recent records, readiness, errors and freshness |
| Counters unexpectedly drop | API or monitor restarted |
| HTTP 200 but missing prediction rows | Logging-failure counter and API logs |
| No request series for status 500 | No such request may have occurred yet |
| Latency graph has gaps | No observations in the selected rate window or missing scrapes |
| Duplicate metric names | Repeated collector definitions or duplicate initialization |

Useful commands:

```bash
curl -s http://127.0.0.1:8001/metrics

docker compose ps

docker compose logs --tail=50 prometheus
```

When Python services run directly on the Mac, inspect their terminal logs. When they run in Compose, inspect `docker compose logs api drift-monitor`.

## 17. Interview explanation

> We instrumented the FastAPI service with Prometheus counters and histograms for request volume, response status, inference latency, API latency and prediction-logging failures. A separate drift-monitor process exposed gauges for KS scores, drift flags, sample counts and freshness. Prometheus scraped both services every five seconds, and Grafana queried those measurements for dashboards and a sustained-drift alert. Individual prediction records remained in SQLite, while training experiments were tracked in MLflow.

## Official references used during the project

- [Prometheus Python client](https://prometheus.github.io/client_python/)
- [Histogram instrumentation](https://prometheus.github.io/client_python/instrumenting/histogram/)
- [Gauge instrumentation](https://prometheus.github.io/client_python/instrumenting/gauge/)
- [Prometheus Docker installation](https://prometheus.io/docs/prometheus/latest/installation/)
- [FastAPI middleware](https://fastapi.tiangolo.com/tutorial/middleware/)
- [Grafana Prometheus data source](https://grafana.com/docs/grafana/latest/datasources/prometheus/configure/)
