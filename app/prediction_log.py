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