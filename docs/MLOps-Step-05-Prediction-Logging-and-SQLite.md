# Step 5 — Prediction logging with SQLite

## Purpose and scope

Step 4 made predictions accessible through FastAPI. Step 5 made those predictions inspectable after the request finished by storing their inputs, outputs and model version.

We used a local SQLite database at `data/predictions.db`. Later, the drift monitor reads these records without asking the API to repeat inference.

This chapter describes our existing implementation. Code snippets are reference material; your current application already includes logging and the metrics introduced afterward.

## 1. Why prediction records are needed

An API response only tells the caller what happened to one request. It does not automatically create a durable history for monitoring.

We store records to answer questions such as:

- Are recent brightness and motion values different from training inputs?
- Which model produced a particular prediction?
- What probability and threshold produced the decision?
- How long did feature preparation and inference take?
- If a true label becomes available later, which prediction does it belong to?

The final question requires a future label-ingestion mechanism. Our first version generates prediction IDs but does not yet collect actual labels or compute production accuracy.

## 2. Separate prediction records from aggregate metrics

| Data | Example | Storage and use |
|---|---|---|
| Individual prediction record | One scan's four features, probability, class and version | SQLite; used for window-based drift analysis and investigation |
| Aggregate operational metric | Request count or inference-duration histogram | Exposed by the API; collected by Prometheus |
| Training experiment | Validation F1, estimator parameters and artifacts | MLflow |

These serve different purposes. Prometheus does not read the SQLite table in our architecture. A separate drift process reads the table, computes scores, and exposes aggregate metrics for Prometheus.

We do not use prediction IDs as Prometheus metric labels. That would create a new time series for every request instead of a bounded set of useful aggregate series.

## 3. Why SQLite fits the first version

SQLite is an embedded relational database: Python reads and writes a local file without a separate database server.

Advantages for our learning project include a small setup, SQL queries, transactional writes and a database file that survives an API restart.

This is not evidence that SQLite will handle an arbitrary production workload. Concurrent writes contend for database locks, and per-request disk access adds latency. Our short load test tested only a small local workload.

Our implementation uses the default SQLite journal configuration; we did not explicitly configure WAL mode. A separate read-only monitor still shares disk resources and can interact with database locking. Keeping drift calculations outside `/predict` removes their computation from the request path but does not eliminate all shared-resource contention.

## 4. The prediction schema

We created the following table:

```sql
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    model_version TEXT NOT NULL,
    brightness REAL NOT NULL,
    motion REAL NOT NULL,
    face_visibility REAL NOT NULL,
    signal_quality REAL NOT NULL,
    prediction INTEGER NOT NULL,
    probability REAL NOT NULL,
    threshold REAL NOT NULL,
    inference_ms REAL NOT NULL
);
```

| Column | Meaning |
|---|---|
| `prediction_id` | UUID generated for the prediction event |
| `timestamp` | UTC timestamp captured near the start of endpoint processing |
| `model_version` | Run ID of the loaded model |
| Four feature columns | Validated inputs used for inference |
| `prediction` | Binary class decision |
| `probability` | Estimated probability of class 1, acceptable |
| `threshold` | Decision cutoff used for this prediction |
| `inference_ms` | Feature preparation and prediction time, excluding storage |

The primary key prevents duplicate prediction IDs. It does not deduplicate retries: the endpoint generates a fresh UUID for each request, so retrying the same payload can create another row.

The table's `NOT NULL` constraints prevent missing database values. Bounds and allowed classes are mainly enforced by application behavior; this schema does not add SQL `CHECK` constraints for those properties.

`CREATE TABLE IF NOT EXISTS` initializes a missing table. It does not migrate an existing table when the application later changes its schema.

## 5. Implement the storage module

We created `app/prediction_log.py`:

```python
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "predictions.db"


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                model_version TEXT NOT NULL,
                brightness REAL NOT NULL,
                motion REAL NOT NULL,
                face_visibility REAL NOT NULL,
                signal_quality REAL NOT NULL,
                prediction INTEGER NOT NULL,
                probability REAL NOT NULL,
                threshold REAL NOT NULL,
                inference_ms REAL NOT NULL
            )
        """)


def save_prediction(record):
    with sqlite3.connect(DB_PATH, timeout=5) as connection:
        connection.execute("""
            INSERT INTO predictions VALUES (
                :prediction_id,
                :timestamp,
                :model_version,
                :brightness,
                :motion,
                :face_visibility,
                :signal_quality,
                :prediction,
                :probability,
                :threshold,
                :inference_ms
            )
        """, record)
```

### Parameter binding

The `:prediction_id` and other placeholders refer to dictionary keys. SQLite binds values separately from the SQL statement rather than constructing SQL by concatenating request values.

### Transactions

The connection context manager commits an open transaction when the block exits successfully and rolls it back if an exception escapes. A successful `save_prediction()` therefore includes the transaction commit, not only submitting the INSERT statement.

`timeout=5` controls how long SQLite waits for a database lock before raising an error. It is not an overall deadline for the API request.

### Connection-lifetime detail

Python's SQLite connection context manager manages transactions; it does not explicitly close the connection on leaving the block. Our original implementation relies on object cleanup afterward.

For deterministic closure, a future cleanup can use `contextlib.closing` around the connection and keep the inner transaction context:

```python
from contextlib import closing

with closing(sqlite3.connect(DB_PATH, timeout=5)) as connection:
    with connection:
        connection.execute(sql, record)
```

This is a suggested improvement, not a change already applied to your files. A future schema-safe improvement is to name the INSERT columns explicitly instead of relying on the table's column order.

## 6. Initialize storage during API startup

We imported the storage functions in `app/main.py`:

```python
from app.prediction_log import init_db, save_prediction
```

Then we called `init_db()` after loading and checking the model, before the lifespan function yields:

```python
app.state.model = model
app.state.metadata = metadata

init_db()

yield
```

This ensures the table exists before requests arrive. If initialization fails, startup fails; the API does not silently begin serving without completing its startup requirements.

That differs from our runtime handling: once the API has started, a failure to save an individual prediction is logged while the response can still succeed.

## 7. Capture timestamps and latency correctly

At the beginning of the endpoint, we added:

```python
started = perf_counter()
timestamp = datetime.now(timezone.utc).isoformat()
```

These clocks serve different purposes:

| Value | Clock | Use |
|---|---|---|
| `timestamp` | UTC wall clock | Filter records by a real-world time window |
| `started` | Monotonic performance counter | Measure elapsed duration |

`perf_counter()` is suitable for elapsed time because it is not interpreted as calendar time. It should not be stored as the event timestamp.

Our timestamp is captured inside the endpoint, after FastAPI's request validation. It is not the time the client captured the scan or necessarily the instant the HTTP connection arrived.

After preparing features and calculating the prediction, we compute:

```python
inference_ms = (perf_counter() - started) * 1000
```

This includes feature-table construction, model computation and small amounts of surrounding endpoint work. It excludes the later SQLite write and response delivery.

## 8. Assemble and persist the record

```python
prediction_id = str(uuid4())

record = {
    "prediction_id": prediction_id,
    "timestamp": timestamp,
    "model_version": metadata["run_id"],
    **scan.model_dump(),
    "prediction": prediction,
    "probability": probability,
    "threshold": threshold,
    "inference_ms": inference_ms,
}

try:
    save_prediction(record)
except Exception:
    logger.exception(
        "Failed to store prediction %s", prediction_id
    )
```

The response uses the same `prediction_id`, run ID, probability and threshold as this record. This links the caller's response to the database entry when the write succeeds.

The input schema forbids extra fields, so `scan.model_dump()` contains only our four declared input features.

## 9. Understand the failure policy

We chose to return a successful prediction even if its storage operation fails. The exception handler records the failure in application logs. Later, we also incremented a Prometheus counter.

This trades complete monitoring history for prediction availability:

- A valid prediction can reach the client even if its row is missing.
- HTTP 200 does not guarantee a successful log write.
- The API response does not currently include a storage-success field.
- We do not retry or buffer failed writes in the background.

This is a deliberate simplification for the exercise. Applications requiring durable audit records may need a different contract, a durable queue or a transactionally coordinated workflow.

The broad `except Exception` was used to keep logging failures from blocking predictions. It can also catch programming errors in the logging path, so logs and the failure counter must be monitored rather than treating every failure as harmless.

## 10. Does logging increase prediction latency?

Yes. Storage is synchronous in our implementation:

1. Validate request.
2. Prepare features and calculate prediction.
3. Write the prediction record and commit.
4. Build and return the response.

A useful approximate relationship is:

```text
API processing time ≈ validation/dispatch + inference + database write + other overhead
```

Client-observed latency additionally includes networking, server scheduling and response transfer.

The measured `inference_ms` will not reveal database overhead because its timer stops before storage begins. That is why we introduced separate timers later.

### Metrics added in the following step

```python
INFERENCE_DURATION.observe(inference_ms / 1000)

try:
    with DB_DURATION.time():
        save_prediction(record)
except Exception:
    LOG_FAILURES.inc()
    logger.exception(
        "Failed to store prediction %s", prediction_id
    )
```

| Metric | Measurement |
|---|---|
| `scan_inference_duration_seconds` | Feature preparation and inference duration |
| `scan_db_write_duration_seconds` | Duration of attempted storage, including transaction handling |
| `scan_api_duration_seconds` | Request processing measured around FastAPI's request handler |
| `scan_prediction_log_failures_total` | Number of failed prediction-storage attempts |

Prometheus durations are recorded in seconds; Grafana queries multiply by 1,000 to display milliseconds.

Comparing averages provides useful context, but subtracting two percentile values does not give the percentile of their per-request differences. Also, inference and database metrics describe prediction attempts that reached those operations, whereas API metrics can include validation errors that never ran inference.

## 11. Alternatives considered but not implemented

| Approach | Effect | Trade-off |
|---|---|---|
| Current synchronous SQLite write | Response waits for storage attempt | Simple, but storage latency is on the request path |
| In-process background task | Response can be sent before storage | Pending work can be lost if the process stops |
| Durable queue plus consumer | Request publishes an event; another process stores it | Adds publication latency, delivery semantics and operational complexity |

We deferred Kafka and background logging. A queue does not automatically provide exactly-once processing: retries and duplicate delivery must be considered, often using prediction IDs for deduplication.

## 12. Inspect the stored records

From the project root, with `mlops` active:

```bash
python - <<'PY'
import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

path = Path("data/predictions.db").resolve()

with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
    records = pd.read_sql_query(
        """
        SELECT prediction_id, timestamp, model_version,
               prediction, probability, inference_ms
        FROM predictions
        ORDER BY timestamp DESC
        LIMIT 5
        """,
        connection,
    )

print(records.to_string(index=False))
PY
```

This read-only connection fails if the database is missing instead of creating a new empty file.

A growing table confirms records are being saved, but matching the exact expected count within a simulation window is a stronger check. We later confirmed 300 stored rows for each controlled traffic run.

## 13. How the drift monitor selects records

The automated monitor later filters on timestamp and model version:

```sql
SELECT brightness, motion, face_visibility, signal_quality
FROM predictions
WHERE timestamp >= ?
  AND timestamp < ?
  AND model_version = ?;
```

Our timestamps are generated in the same UTC ISO format, making these textual comparisons suitable for this controlled dataset. Do not mix arbitrary timezone offsets or timestamp formats and assume text sorting still represents chronological order.

The model-version filter prevents combining predictions from different model releases into one release-specific window. The first implementation does not filter on a traffic-source column: load tests, simulations and other requests to the same API all enter the same table.

For this small dataset, we did not add an index on `(model_version, timestamp)`. An index and retention strategy become relevant as the table grows; avoid allowing each periodic check to scan an indefinitely expanding history.

## 14. Tests and storage boundaries

Our later API tests mock `save_prediction` and `init_db`. This prevents unit-level API tests from writing into the monitoring database.

A separate test simulates a SQLite error and confirms that:

- The API still returns HTTP 200.
- The prediction-logging failure counter increments.

Because persistence is mocked, those tests do not prove the SQLite insert, commit or concurrency behavior. Manual record inspection and live traffic checks exercise the actual storage path. The small load test also writes real records and must be assessed alongside the logging-failure counter.

## 15. Persistence across containers and rollback

In the Docker setup, the API mounts:

```yaml
volumes:
  - ./data:/app/data
```

The database lives in the host's project directory, not only in the container's writable layer. Replacing the API container therefore preserves prediction history.

The drift monitor uses a read-only mount of the same directory. It can inspect records but cannot intentionally update them through that mount.

Keeping the file does not guarantee schema compatibility with every future release. If a new release changes the schema incompatibly, rolling back the application image alone may not be sufficient. Our first version keeps the same schema throughout the release exercise.

## Completion criteria

- Startup creates the predictions table if needed.
- Valid requests produce records containing the same identifiers and outputs returned to the caller.
- Stored features can be retrieved by time window and model version.
- Storage failures are visible and do not silently masquerade as successful writes.
- Inference time is distinguished from database time and overall API time.

The next chapter is **Step 6 — Prometheus instrumentation, API middleware, metrics collection and Grafana dashboards**.
