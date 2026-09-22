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

USER 65532:65532
EXPOSE 22350
CMD ["python", "-m", "platform_api.signals"]
