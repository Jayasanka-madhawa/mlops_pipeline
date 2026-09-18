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
    "scan_drift_ks_score",
    "KS distribution difference",
    ["feature"],
)
DRIFT_FLAG = Gauge(
    "scan_drift_flag",
    "1 means drift flagged, 0 means no flag",
    ["feature"],
)
SAMPLES = Gauge(
    "scan_drift_window_samples",
    "Prediction count in the current window",
)
READY = Gauge(
    "scan_drift_ready",
    "1 when the latest check has enough valid data",
)
LAST_SUCCESS = Gauge(
    "scan_drift_last_success_timestamp_seconds",
    "Time of the last successful drift calculation",
)
ERRORS = Counter(
    "scan_drift_check_errors_total",
    "Failed monitoring checks",
)


def clear_results():
    READY.set(0)
    for feature in FEATURES:
        # Missing results must not look like confirmed zero drift.
        KS_SCORE.labels(feature=feature).set(float("nan"))
        DRIFT_FLAG.labels(feature=feature).set(float("nan"))


def read_window():
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=WINDOW_MINUTES)

    # Read-only: do not silently create an empty database.
    with sqlite3.connect(
        f"{DB_PATH.as_uri()}?mode=ro",
        uri=True,
        timeout=5,
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
            params=(
                start.isoformat(),
                now.isoformat(),
                MODEL_RUN_ID,
            ),
        )


def check_drift(reference):
    production = read_window()
    SAMPLES.set(len(production))

    if len(production) < MIN_SAMPLES:
        clear_results()
        logger.info(
            "Waiting for data: %s/%s recent predictions",
            len(production),
            MIN_SAMPLES,
        )
        return

    if not np.isfinite(
        production[FEATURES].to_numpy(dtype=float)
    ).all():
        raise ValueError("Invalid production feature values")

    # Calculate everything before publishing the results.
    results = {}
    for feature in FEATURES:
        test = ks_2samp(reference[feature], production[feature])
        flagged = (
            test.pvalue < P_THRESHOLD
            and test.statistic >= KS_THRESHOLD
        )
        results[feature] = (float(test.statistic), int(flagged))

    for feature, (score, flagged) in results.items():
        KS_SCORE.labels(feature=feature).set(score)
        DRIFT_FLAG.labels(feature=feature).set(flagged)

    LAST_SUCCESS.set_to_current_time()
    READY.set(1)

    flagged_features = [
        feature
        for feature, (_, flagged) in results.items()
        if flagged
    ]
    logger.info(
        "Samples=%s | Flagged=%s",
        len(production),
        flagged_features or "none",
    )


def main():
    reference_path = Path(
        os.environ.get(
            "REFERENCE_PATH",
            str(ROOT / "data" / "reference_features.csv"),
        )
    )
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