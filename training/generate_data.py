from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def generate_scans(n_samples=6000, seed=42):
    rng = np.random.default_rng(seed)

    brightness = rng.beta(5, 5, n_samples)
    motion = rng.beta(2, 8, n_samples)
    face_visibility = rng.beta(9, 2, n_samples)
    signal_quality = rng.beta(5, 2, n_samples)

    # Brightness near the middle is preferable to either extreme.
    lighting_quality = 1 - 2 * np.abs(brightness - 0.5)

    # Synthetic relationship between inputs and scan acceptability.
    score = (
        2.0 * lighting_quality
        - 4.0 * motion
        + 2.5 * face_visibility
        + 3.0 * signal_quality
        - 4.5
    )

    probability = 1 / (1 + np.exp(-score))

    # Random sampling adds uncertainty instead of a perfect fixed rule.
    acceptable = rng.binomial(1, probability)

    return pd.DataFrame({
        "scan_id": [f"train-{i:06d}" for i in range(n_samples)],
        "brightness": brightness,
        "motion": motion,
        "face_visibility": face_visibility,
        "signal_quality": signal_quality,
        "acceptable": acceptable,
    })


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    data = generate_scans()

    train, remaining = train_test_split(
        data,
        test_size=0.4,
        random_state=42,
        stratify=data["acceptable"],
    )

    validation, test = train_test_split(
        remaining,
        test_size=0.5,
        random_state=42,
        stratify=remaining["acceptable"],
    )

    for name, frame in [
        ("train", train),
        ("validation", validation),
        ("test", test),
    ]:
        path = DATA_DIR / f"{name}.csv"
        frame.to_csv(path, index=False)

        print(
            f"{name}: {len(frame)} scans | "
            f"acceptable: {frame['acceptable'].mean():.1%}"
        )

    # Reference inputs for later data-drift monitoring.
    features = [
        "brightness",
        "motion",
        "face_visibility",
        "signal_quality",
    ]

    train[features].to_csv(
        DATA_DIR / "reference_features.csv",
        index=False,
    )

    print(f"\nFiles saved in: {DATA_DIR}")


if __name__ == "__main__":
    main()