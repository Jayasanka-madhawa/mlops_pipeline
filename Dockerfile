FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements-serving.txt .
RUN pip install --no-cache-dir -r requirements-serving.txt

COPY app/ app/
COPY monitoring/drift_monitor.py monitoring/drift_monitor.py

ARG MODEL_RUN_ID
COPY artifacts/${MODEL_RUN_ID}/ artifacts/${MODEL_RUN_ID}/
COPY data/reference_features.csv reference_features.csv

ENV MODEL_RUN_ID=${MODEL_RUN_ID}

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8001"]