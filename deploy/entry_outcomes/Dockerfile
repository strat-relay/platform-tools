FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /outcome-overlay

COPY requirements-postgres.txt /tmp/requirements-postgres.txt
RUN pip install --no-cache-dir --disable-pip-version-check \
    -r /tmp/requirements-postgres.txt

# This image carries only the two Context runner files updated for outcome
# projection. The init container copies these onto the existing runtime PVC;
# it does not replace the orchestrator, runner ledger, or other live files.
COPY context_structure_retrace_forward.py /outcome-overlay/
COPY context_structure_retrace_outcome_projector.py /outcome-overlay/
COPY strategy_report_format.py /outcome-overlay/

CMD ["python", "-c", "import psycopg; print('entry-outcome overlay ready')"]
