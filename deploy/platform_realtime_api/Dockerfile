FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORM_REALTIME_HOST=0.0.0.0 \
    PLATFORM_REALTIME_PORT=22352

WORKDIR /app

COPY requirements-postgres.txt requirements-messaging.txt requirements-realtime.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check \
    -r requirements-postgres.txt \
    -r requirements-messaging.txt \
    -r requirements-realtime.txt

COPY postgres ./postgres
COPY platform_api ./platform_api

USER 65532:65532
EXPOSE 22352
CMD ["python", "-m", "platform_api.realtime"]
