FROM python:3.12-slim

RUN pip install --no-cache-dir boto3

COPY orchestrator.py /app/orchestrator.py

WORKDIR /app
ENTRYPOINT ["python3", "orchestrator.py"]
