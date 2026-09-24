FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_API_HOST=0.0.0.0 \
    PLATFORM_API_PORT=22350

WORKDIR /app

COPY requirements-postgres.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements-postgres.txt

COPY postgres ./postgres
COPY platform_api ./platform_api
# execution_v2/risk.py + the baseline policy file: needed by platform_api/v2_risk.py's read/save
# routes (mission CLAUDE-V2-RISK-EXECUTION-CONSOLE). No execution_v2 runtime code (worker.py,
# runtime/) is copied - this image never runs the execution_v2 consumer/worker, only reads its
# risk policy module for validation and the file-based baseline fallback.
COPY execution_v2/__init__.py execution_v2/risk.py execution_v2/risk_policy_store.py ./execution_v2/
COPY orchestration/config/v2_execution_risk_policy.json ./orchestration/config/v2_execution_risk_policy.json

USER 65532:65532
EXPOSE 22350
CMD ["python", "-m", "platform_api.signals"]
