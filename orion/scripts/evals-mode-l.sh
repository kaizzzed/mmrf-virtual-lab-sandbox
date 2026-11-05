#!/usr/bin/env bash
# ORION mode L evals (E1–E24) against the live local Compose stack, in one command
# (orion/evals/README.md has the harness itself; orion/notes/mode-l-status.md the results).
#
# Usage, from anywhere in the sandbox checkout, with mode L up (orion/scripts/orion-local.sh up):
#   orion/scripts/evals-mode-l.sh agent [harness args...]
#   orion/scripts/evals-mode-l.sh bff [harness args...]
#   orion/scripts/evals-mode-l.sh e20-lease [harness args...]
#
#   agent      Every eval against the Compose orion-agent (POST /invocations), records read from the
#              volume mmrf-synthetic_orion-runs. E11 runs against four throwaway fault agents
#              (ORION_FAULTS de_run:timeout, planner:refuse, composer:refuse, cohort_count:empty),
#              started from the Compose-built image and removed on exit.
#   bff        The same turns through the fork's BFF (POST /api/orion/agui, behind the identity
#              gateway, which supplies the session cookie), records from the BFF's record and view
#              routes. E11 still uses the fault agents directly.
#   e20-lease  E20 with its live lease-expiry case: the harness and its own mode L agent share one
#              container, so the harness's --restart-cmd can kill -9 the controller mid-run and start
#              a fresh one; the harness then waits for the 180 s lease to lapse and expects
#              Interrupted (RUN_INTERRUPTED). It never touches the Compose services. About 9 minutes.
#
# Extra arguments go to `python -m orion_evals run` (for example --evals E1,E7 or --repeats 1).
# Reports land in orion/evals/reports/ (gitignored). The exit code is the harness's: 1 when an eval
# fails. Nothing here calls AWS: the planner and composer are the stubs, so E22 reports
# "needs Bedrock planner".
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 && [ -x "$HOME/.docker/bin/docker" ]; then
  PATH="$HOME/.docker/bin:$PATH"
fi

AGENT_IMAGE=mmrf-sandbox/orion-agent:local
HARNESS_IMAGE=orion-agent:dev
NETWORK=mmrf-synthetic_default
FAULTS=(de_run:timeout planner:refuse composer:refuse cohort_count:empty)
# The agent's mode L settings, as orion/compose.orion.yaml gives the orion-agent service.
AGENT_ENV=(
  -e ORION_MODE=L -e ORION_RUN_DIR=/data/runs -e ORION_FIELDS_PATH=/config/cohort-fields.json
  -e ORION_GOLDEN_DIR=/golden -e ORION_VLAB_ORIGIN=http://identity:8080 -e ORION_TOOLS=inprocess
  -e ORION_PLANNER=stub -e ORION_COMPOSER=stub -e ORION_DEFAULT_SCOPE=active_cohort
)

die() {
  printf 'evals-mode-l: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"
}

# ------------------------------------------------------------------ inside the e20-lease container
# Started (or, after kill -9 of the running one, restarted) from /app, then waited for on /ping.
inside_agent() {
  if [ "${1:-}" = restart ] && [ -f /tmp/agent.pid ]; then
    kill -9 "$(cat /tmp/agent.pid)" 2>/dev/null || true
    sleep 1
  fi
  (cd /app && exec python -m orion_agent.app >>/tmp/agent.log 2>&1) &
  echo $! >/tmp/agent.pid
  local i=0
  until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2)" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
      tail -20 /tmp/agent.log >&2
      die "the agent did not start"
    fi
    sleep 1
  done
  printf 'agent up (pid %s)\n' "$(cat /tmp/agent.pid)"
}

inside_lease() {
  inside_agent
  cd /evals
  exec python -m orion_evals run --evals E20 --agent-url http://127.0.0.1:8080/invocations --run-dir /data/runs \
    --frontend-root /fork --agent-tests-root /agent-tests --restart-cmd "bash /orion-scripts/evals-mode-l.sh _agent restart" "$@"
}

# ------------------------------------------------------------------------------------- on the host
repo_root() {
  local dir
  dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
  [ -f "$dir/compose.yaml" ] && [ -d "$dir/orion/evals" ] || die "run this from the sandbox checkout"
  printf '%s\n' "$dir"
}

require_stack() {
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
  docker network inspect "$NETWORK" >/dev/null 2>&1 || die "network $NETWORK not found: is the sandbox stack up?"
  docker image inspect "$AGENT_IMAGE" >/dev/null 2>&1 || die "$AGENT_IMAGE not built: run orion/scripts/orion-local.sh up"
  docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1 || die "$HARNESS_IMAGE not built: docker build -t $HARNESS_IMAGE orion/agent"
}

fault_name() {
  printf 'orion-eval-fault-%s\n' "$(printf '%s' "$1" | tr ':_' '--')"
}

stop_faults() {
  local fault
  for fault in "${FAULTS[@]}"; do
    docker rm -f "$(fault_name "$fault")" >/dev/null 2>&1 || true
  done
}

# Starts the four fault agents, each with its own run directory under $1, and waits for health.
start_faults() {
  local runs=$1 fault name healthy i=0
  stop_faults
  for fault in "${FAULTS[@]}"; do
    name=$(fault_name "$fault")
    mkdir -p "$runs/$name"
    chmod 777 "$runs/$name"
    docker run -d --name "$name" --network "$NETWORK" -v "$runs/$name:/data/runs" "${AGENT_ENV[@]}" \
      -e "ORION_FAULTS=$fault" "$AGENT_IMAGE" >/dev/null
  done
  until healthy=$(docker ps --filter name=orion-eval-fault- --filter health=healthy -q | wc -l | tr -d ' ') && [ "$healthy" = "${#FAULTS[@]}" ]; do
    i=$((i + 1))
    [ "$i" -le 60 ] || die "the fault agents did not turn healthy"
    sleep 2
  done
}

run_harness() {
  local root=$1 target=$2
  shift 2
  local runs="$root/orion/evals/reports/fault-runs"
  local args=(--frontend-root /fork --agent-tests-root /agent-tests --e2e-script /orion-scripts/e2e-mode-l.mjs)
  local fault name
  for fault in "${FAULTS[@]}"; do
    name=$(fault_name "$fault")
    args+=(--fault-agent "$fault=http://$name:8080/invocations@/fault-runs/$name")
  done
  if [ "$target" = bff ]; then
    args+=(--target bff --bff-origin http://identity:8080 --record-source bff)
  else
    args+=(--agent-url http://orion-agent:8080/invocations --run-dir /data/runs)
  fi
  trap stop_faults EXIT
  start_faults "$runs"
  docker run --rm --network "$NETWORK" \
    -v "$root/orion/evals:/evals" -v mmrf-synthetic_orion-runs:/data/runs:ro -v "$runs:/fault-runs:ro" \
    -v "$root/.sources/frontend:/fork:ro" -v "$root/orion/agent/tests:/agent-tests:ro" -v "$root/orion/scripts:/orion-scripts:ro" \
    -w /evals "$HARNESS_IMAGE" python -m orion_evals run "${args[@]}" "$@"
}

run_lease() {
  local root=$1
  shift
  docker run --rm --network "$NETWORK" \
    -v "$root/orion/evals:/evals" -v "$root/orion/scripts:/orion-scripts:ro" \
    -v "$root/.sources/frontend:/fork:ro" -v "$root/orion/agent/tests:/agent-tests:ro" "${AGENT_ENV[@]}" \
    --entrypoint bash "$AGENT_IMAGE" /orion-scripts/evals-mode-l.sh _lease "$@"
}

main() {
  local command=${1:-help}
  [ $# -gt 0 ] && shift
  case $command in
    _agent) inside_agent "$@" ;;
    _lease) inside_lease "$@" ;;
    agent | bff)
      local root
      root=$(repo_root)
      require_stack
      run_harness "$root" "$command" "$@"
      ;;
    e20-lease)
      local root
      root=$(repo_root)
      require_stack
      run_lease "$root" "$@"
      ;;
    help | -h | --help) usage ;;
    *)
      usage >&2
      die "unknown command $command"
      ;;
  esac
}

main "$@"
