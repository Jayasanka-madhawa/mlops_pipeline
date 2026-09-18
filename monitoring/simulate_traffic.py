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

# Different seed from training: fresh samples.
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