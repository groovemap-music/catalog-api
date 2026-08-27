# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# GrooveMap catalog API performance test container
#
# Lightweight container that sequentially times all API query endpoints
# and produces a performance report. Not included in docker-compose —
# built and run manually.
#
# See README.md for usage instructions.

FROM python:3.14.5-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

LABEL org.opencontainers.image.title="GrooveMap catalog API performance test" \
      org.opencontainers.image.description="Sequential performance test runner for catalog API query endpoints" \
      org.opencontainers.image.source="https://github.com/groovemap-music/catalog-api" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

COPY .build/private/groovemap_runtime-0.1.0-py3-none-any.whl /wheels/
COPY .build/requirements.txt /wheels/
RUN pip install --no-cache-dir --require-hashes -r /wheels/requirements.txt && \
    pip install --no-cache-dir --no-deps /wheels/groovemap_runtime-0.1.0-py3-none-any.whl && \
    pip install --no-cache-dir psycopg2-binary==2.9.11 && \
    rm -rf /wheels

# Create non-root user and directories
RUN groupadd -r perftest && \
    useradd -r -l -g perftest -m -s /bin/sh perftest && \
    mkdir -p /app /results && \
    chown -R perftest:perftest /app /results

WORKDIR /app

COPY --chown=perftest:perftest performance/run_perftest.py performance/entrypoint.sh ./
RUN chmod +x entrypoint.sh

USER perftest

VOLUME ["/results", "/config"]

ENTRYPOINT ["/app/entrypoint.sh"]
