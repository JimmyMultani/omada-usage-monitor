FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a

# Fixed, non-root UID/GID so bind-mounted data dirs can be chowned to match.
ARG APP_UID=10001
ARG APP_GID=10001
RUN addgroup -S -g "$APP_GID" monitor \
    && adduser -S -D -H -u "$APP_UID" -G monitor monitor \
    && mkdir -p /data /config \
    && chown monitor:monitor /data

WORKDIR /app
COPY monitor.py .

ENV DATA_DIR=/data \
    THRESHOLDS_PATH=/config/thresholds.json \
    PYTHONUNBUFFERED=1

VOLUME /data
USER monitor

# Healthy while the last successful poll is recent. The limit scales with
# POLL_INTERVAL_SECONDS so a longer interval doesn't read as unhealthy.
HEALTHCHECK --interval=1m --timeout=10s --start-period=5m --retries=3 \
    CMD python -c "import os, sys, time; limit = max(900, 3 * int(os.environ.get('POLL_INTERVAL_SECONDS', '300'))); sys.exit(time.time() - os.path.getmtime(os.path.join(os.environ.get('DATA_DIR', '/data'), 'heartbeat')) > limit)"

ENTRYPOINT ["python", "/app/monitor.py"]
