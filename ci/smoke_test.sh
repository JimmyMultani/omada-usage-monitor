#!/usr/bin/env bash
# Builds the image and runs it against a fake Omada API, checking that it
# polls, raises a (dry-run) alert, serves Prometheus metrics that promtool
# accepts, runs as the documented non-root user, reports healthy, and reports
# failed polls in its metrics once the API goes away. A second container
# checks that a compose-style `user:` override works too.
set -euo pipefail

image=omada-usage-monitor:smoke
name=omada-usage-monitor-smoke
# Like a Synology deployment: a NAS user and its group, with /data owned by them.
other_user=1028:100
other_name=omada-usage-monitor-smoke-other-user
port=18043
ci_dir=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$ci_dir")

docker build -t "$image" "$root"

python3 "$ci_dir/fake_omada.py" "$port" &
fake_pid=$!
cleanup() {
  docker rm -f "$name" "$other_name" > /dev/null 2>&1 || true
  kill "$fake_pid" 2> /dev/null || true
}
trap cleanup EXIT

start_monitor() {
  local container=$1
  shift
  docker run -d --name "$container" "$@" \
    --add-host host.docker.internal:host-gateway \
    --health-interval 5s --health-start-period 5s \
    -e OMADA_BASE_URL="http://host.docker.internal:$port" \
    -e OMADA_CLIENT_ID=smoke -e OMADA_CLIENT_SECRET=smoke \
    -e OMADA_OMADAC_ID=smoke -e OMADA_SITE_ID=smoke \
    -e POLL_INTERVAL_SECONDS=5 \
    -e METRICS_PORT=9877 \
    -v "$root/thresholds.example.json:/config/thresholds.json:ro" \
    "$image" > /dev/null
}

start_monitor "$name"
start_monitor "$other_name" --user "$other_user" --tmpfs "/data:uid=${other_user%:*},gid=${other_user#*:}"

fail() {
  echo "FAIL: $1"
  for container in "$name" "$other_name"; do
    echo "--- $container logs:"
    docker logs "$container"
  done
  exit 1
}

logs_contain() {
  # Not a pipe: grep -q exiting early would fail it under pipefail.
  grep -q "$2" <<< "$(docker logs "$1" 2>&1)"
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
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$1")" = healthy ]
}

user_of() {
  echo "$(docker exec "$1" id -u):$(docker exec "$1" id -g)"
}

wait_for "a successful poll" logs_contain "$name" "polled 2 clients"
wait_for "a sustained upload alert" logs_contain "$name" "Smoke Camera.*has been uploading"
wait_for "the camera in /metrics" metrics_match \
  '^omada_client_upload_bytes_total{mac="02-00-00-00-00-01",name="Smoke Camera",vlan="20"} [1-9]'
wait_for "a healthy status" is_healthy "$name"

# Prometheus's own parser and linter, rather than our idea of the format.
# promtool accepts empty input, so make sure there's something to check.
metrics=$(scrape)
grep -q '^omada_client_upload_bytes_total{' <<< "$metrics" || fail "/metrics has no client metrics: $metrics"
if ! promtool_output=$(docker run --rm -i --entrypoint promtool prom/prometheus:v3.15.0 check metrics <<< "$metrics" 2>&1); then
  fail "promtool check metrics: $promtool_output"
fi
echo "ok: promtool accepts /metrics"

user=$(user_of "$name")
[ "$user" = 10001:10001 ] || fail "expected to run as 10001:10001, got $user"
if logs_contain "$name" "poll failed"; then fail "a poll failed"; fi
if logs_contain "$name" "Problems in the thresholds file"; then fail "thresholds.example.json has problems"; fi

# The user: override container, started alongside the first, has had time to poll.
wait_for "a successful poll as $other_user" logs_contain "$other_name" "polled 2 clients"
wait_for "a healthy status as $other_user" is_healthy "$other_name"
user=$(user_of "$other_name")
[ "$user" = "$other_user" ] || fail "expected to run as $other_user, got $user"
if logs_contain "$other_name" "poll failed"; then fail "a poll failed as $other_user"; fi

# Last: take the controller away and check failures reach /metrics.
kill "$fake_pid"
wait_for "failed polls in /metrics" metrics_match '^omada_usage_monitor_polls_total{result="failure"} [1-9]'
wait_for "consecutive failures in /metrics" metrics_match '^omada_usage_monitor_consecutive_poll_failures [1-9]'
if metrics_match '^omada_client_'; then fail "client metrics still served after a failed poll"; fi
echo "ok: client metrics dropped after a failed poll"

echo "smoke test passed"
