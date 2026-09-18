import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

URL = "http://127.0.0.1:8001/predict"
REQUEST_COUNT = 100
CONCURRENCY = 5

rng = np.random.default_rng(2027)

payloads = [
    {
        "brightness": float(rng.beta(5, 5)),
        "motion": float(rng.beta(2, 8)),
        "face_visibility": float(rng.beta(9, 2)),
        "signal_quality": float(rng.beta(5, 2)),
    }
    for _ in range(REQUEST_COUNT)
]


def send(payload):
    started = time.perf_counter()

    try:
        response = requests.post(URL, json=payload, timeout=10)
        status = str(response.status_code)
    except requests.RequestException as error:
        status = type(error).__name__

    duration_ms = (time.perf_counter() - started) * 1000
    return status, duration_ms


started = time.perf_counter()

with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
    results = list(pool.map(send, payloads))

elapsed = time.perf_counter() - started
statuses = Counter(status for status, _ in results)
successful_times = [
    duration for status, duration in results if status == "200"
]

print(f"Requests: {REQUEST_COUNT}")
print(f"Concurrency: {CONCURRENCY}")
print(f"Results: {dict(statuses)}")
print(f"Elapsed: {elapsed:.2f} seconds")
print(f"Successful throughput: {len(successful_times) / elapsed:.2f} req/s")

if successful_times:
    print(f"Average latency: {np.mean(successful_times):.2f} ms")
    print(f"P95 latency: {np.percentile(successful_times, 95):.2f} ms")

if len(successful_times) != REQUEST_COUNT:
    raise SystemExit("Load check failed: some requests were unsuccessful.")