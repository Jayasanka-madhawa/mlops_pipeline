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