#!/bin/sh
set -eu

api_pid=""
worker_pid=""
payment_worker_pid=""

is_running() {
  pid="$1"

  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi

  if [ -r "/proc/$pid/stat" ]; then
    state="$(awk '{ print $3 }' "/proc/$pid/stat" 2>/dev/null || true)"
    [ "$state" != "Z" ]
    return $?
  fi

  return 0
}

shutdown() {
  echo "Stopping ledger processes..."

  if [ -n "$api_pid" ] && is_running "$api_pid"; then
    kill "$api_pid"
  fi

  if [ -n "$worker_pid" ] && is_running "$worker_pid"; then
    kill "$worker_pid"
  fi

  if [ -n "$payment_worker_pid" ] && is_running "$payment_worker_pid"; then
    kill "$payment_worker_pid"
  fi

  wait || true
}

wait_for_processes() {
  while :; do
    if ! is_running "$api_pid"; then
      wait "$api_pid"
      return $?
    fi

    if [ -n "$worker_pid" ] && ! is_running "$worker_pid"; then
      wait "$worker_pid"
      return $?
    fi

    if [ -n "$payment_worker_pid" ] && ! is_running "$payment_worker_pid"; then
      wait "$payment_worker_pid"
      return $?
    fi

    sleep 1
  done
}

trap 'shutdown; exit 143' INT TERM

echo "Running database migrations..."
poetry run alembic upgrade head

if [ "${HOLD_EXPIRATION_WORKER_ENABLED:-true}" = "true" ]; then
  echo "Starting hold expiration worker..."
  poetry run python -m ledger.hold_expiration_worker &
  worker_pid="$!"
else
  echo "Hold expiration worker disabled."
fi

if [ "${PAYMENT_EVENT_CONSUMER_ENABLED:-true}" = "true" ]; then
  echo "Starting payment event consumer..."
  poetry run python -m ledger.payment_consumer &
  payment_worker_pid="$!"
else
  echo "Payment event consumer disabled."
fi

echo "Starting ledger API..."
poetry run fastapi run --host 0.0.0.0 --port 8000 ledger/main.py &
api_pid="$!"

set +e
wait_for_processes
exit_code="$?"
set -e

shutdown
exit "$exit_code"
