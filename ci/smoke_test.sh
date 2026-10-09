#!/usr/bin/env bash
# Builds the image and runs it against a fake Omada API, checking that it
# polls, raises a (dry-run) alert, serves Prometheus metrics that promtool
# accepts, runs as the documented non-root user, reports healthy, and reports
# failed polls in its metrics once the API goes away.
set -euo pipefail

image=omada-usage-monitor:smoke
name=omada-usage-monitor-smoke
port=18043
ci_dir=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$ci_dir")

docker build -t "$image" "$root"

python3 "$ci_dir/fake_omada.py" "$port" &
fake_pid=$!
cleanup() {
  docker rm -f "$name" > /dev/null 2>&1 || true
  kill "$fake_pid" 2> /dev/null || true
}
trap cleanup EXIT

docker run -d --name "$name" \
  --add-host host.docker.internal:host-gateway \
  --health-interval 5s --health-start-period 5s \
  -e OMADA_BASE_URL="http://host.docker.internal:$port" \
  -e OMADA_CLIENT_ID=smoke -e OMADA_CLIENT_SECRET=smoke \
  -e OMADA_OMADAC_ID=smoke -e OMADA_SITE_ID=smoke \
  -e POLL_INTERVAL_SECONDS=5 \
  -e METRICS_PORT=9877 \
  -v "$root/thresholds.example.json:/config/thresholds.json:ro" \
  "$image" > /dev/null

fail() {
  echo "FAIL: $1"
  docker logs "$name"
  exit 1
}

logs_contain() {
  # Not a pipe: grep -q exiting early would fail it under pipefail.
  grep -q "$1" <<< "$(docker logs "$name" 2>&1)"
}

wait_for() {
  local description=$1
  shift
  for _ in $(seq 60); do
    if "$@"; then
      echo "ok: $description"
      return
    fi
    sleep 2
  done
  fail "timed out waiting for $description"
}

scrape() {
  docker exec "$name" wget -qO- http://127.0.0.1:9877/metrics
}

metrics_match() {
  grep -q "$1" <<< "$(scrape)"
}

is_healthy() {
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ]
}

wait_for "a successful poll" logs_contain "polled 2 clients"
wait_for "a sustained upload alert" logs_contain "Smoke Camera.*has been uploading"
wait_for "the camera in /metrics" metrics_match \
  '^omada_client_upload_bytes_total{mac="02-00-00-00-00-01",name="Smoke Camera",vlan="20"} [1-9]'
wait_for "a healthy status" is_healthy

# Prometheus's own parser and linter, rather than our idea of the format.
if ! promtool_output=$(scrape | docker run --rm -i --entrypoint promtool prom/prometheus:v3.15.0 check metrics 2>&1); then
  fail "promtool check metrics: $promtool_output"
fi
echo "ok: promtool accepts /metrics"

user=$(docker exec "$name" id -u):$(docker exec "$name" id -g)
[ "$user" = 10001:10001 ] || fail "expected to run as 10001:10001, got $user"
if logs_contain "poll failed"; then fail "a poll failed"; fi
if logs_contain "Problems in the thresholds file"; then fail "thresholds.example.json has problems"; fi

# Last: take the controller away and check failures reach /metrics.
kill "$fake_pid"
wait_for "failed polls in /metrics" metrics_match '^omada_usage_monitor_polls_total{result="failure"} [1-9]'
wait_for "consecutive failures in /metrics" metrics_match '^omada_usage_monitor_consecutive_poll_failures [1-9]'

echo "smoke test passed"
