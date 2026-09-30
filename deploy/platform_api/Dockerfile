FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_API_HOST=0.0.0.0 \
    PLATFORM_API_PORT=22350

WORKDIR /app

COPY requirements-postgres.txt requirements-risk-state.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check \
    -r requirements-postgres.txt -r requirements-risk-state.txt

COPY postgres ./postgres
COPY platform_api ./platform_api
# Signals expose the theoretical-outcome disclaimer at import time.
COPY outcome_attribution.py ./outcome_attribution.py
# Redis-first broker reads (BROKER_VIEW_REDIS_URL); populated by broker_view.service.
COPY broker_view ./broker_view
# PlatformControlApi exposes Trade Manager mode as part of the unified API
# startup path. Keep the domain package in the API image so import-time
# construction cannot crash with ModuleNotFoundError.
COPY trade_management ./trade_management
# The API image carries only the V2 policy/authority validators and stores; it never runs the
# execution consumer/worker. Runtime authority is resolved from PostgreSQL, not the policy file.
COPY execution_v2/__init__.py execution_v2/risk.py execution_v2/risk_policy_store.py execution_v2/authority_store.py execution_v2/canary_windows.py ./execution_v2/
COPY orchestration/config/v2_execution_risk_policy.json ./orchestration/config/v2_execution_risk_policy.json

USER 65532:65532
EXPOSE 22350
CMD ["python", "-m", "platform_api.signals"]
