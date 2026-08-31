# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# GrooveMap catalog API performance test container
#
# Lightweight container that sequentially times all API query endpoints
# and produces a performance report. Not included in docker-compose —
# built and run manually.
#
# See README.md for usage instructions.

FROM python:3.14.7-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

ARG BUILD_DATE
ARG BUILD_VERSION=0.1.0
ARG VCS_REF

RUN case "${VCS_REF}" in *[!0-9a-f]*|"") exit 1 ;; esac && \
    [ "${#VCS_REF}" -eq 40 ]

LABEL org.opencontainers.image.title="catalog-api-performance" \
      org.opencontainers.image.description="Sequential performance test runner for catalog API query endpoints" \
      org.opencontainers.image.authors="Robert Wlodarczyk <robert@simplicityguy.com>" \
      org.opencontainers.image.url="https://groovemap.music" \
      org.opencontainers.image.documentation="https://github.com/groovemap-music/catalog-api/blob/main/performance/README.md" \
      org.opencontainers.image.source="https://github.com/groovemap-music/catalog-api" \
      org.opencontainers.image.vendor="GrooveMap" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.14.5-slim"

COPY .build/private/groovemap_runtime-0.1.0-py3-none-any.whl /wheels/
COPY .build/requirements.txt /wheels/
RUN pip install --no-cache-dir --require-hashes -r /wheels/requirements.txt && \
    pip install --no-cache-dir --no-deps /wheels/groovemap_runtime-0.1.0-py3-none-any.whl && \
    rm -rf /wheels

# Create non-root user and directories
RUN groupadd -r perftest && \
    useradd -r -l -g perftest -m -s /bin/sh perftest && \
    mkdir -p /app /results && \
    chown -R perftest:perftest /app /results

WORKDIR /app

COPY --chown=perftest:perftest performance/run_perftest.py performance/entrypoint.sh ./
COPY --chown=perftest:perftest COMMERCIAL-LICENSING.md CONTRIBUTING.md LICENSE NOTICE THIRD_PARTY_NOTICES.md /licenses/
RUN chmod +x entrypoint.sh

USER perftest

VOLUME ["/results", "/config"]

ENTRYPOINT ["/app/entrypoint.sh"]
