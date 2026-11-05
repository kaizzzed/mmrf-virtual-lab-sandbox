#!/usr/bin/env bash
# ORION mode L on the local sandbox stack (RFA rev 25, A4 "Deployment").
#
# Layers orion/compose.orion.yaml on the sandbox's compose.yaml (Compose project mmrf-synthetic):
# adds the `orion-agent` service (stub planner and composer, in-process tools against
# http://identity:8080, run records on the volume `orion-runs`) and gives the frontend the
# read-only volume plus ORION_MODE=L, ORION_AGENT_URL and ORION_RUN_DIR.
#
# Usage, from anywhere in the sandbox checkout:
#   orion/scripts/orion-local.sh up [--no-build] [--skip-frontend-build]
#   orion/scripts/orion-local.sh down [--volumes]
#   orion/scripts/orion-local.sh logs [compose logs options] [service...]
#   orion/scripts/orion-local.sh status
#   orion/scripts/orion-local.sh config
#   orion/scripts/orion-local.sh help
#
#   up      Builds the agent image (and the frontend image from .sources/frontend, the fork),
#           then starts orion-agent and recreates the frontend with the ORION settings, and waits
#           until both are healthy. The rest of the sandbox stack starts if it is not running.
#           --no-build uses the images already built; --skip-frontend-build builds only the agent.
#   down    Stops and removes only the orion-agent container, then recreates the frontend from
#           compose.yaml alone, so the sandbox runs as it did before `up`. The rest of the stack
#           keeps running. --volumes also deletes the volume mmrf-synthetic_orion-runs, which
#           holds every local run record (no undo).
#   logs    `docker compose logs` for orion-agent by default; extra arguments pass through
#           (for example: logs -f --since 10m, or logs frontend).
#   status  Container states, health, and the agent's /ping answer.
#   config  Validates the merged Compose file and prints the orion-agent and frontend services
#           without interpolation (`${VAR}` left as written), so exported AWS credentials are never
#           printed: the three AWS keys show as null.
#
# Environment (all optional):
#   ORION_AGENT_PORT    Host port for the agent on 127.0.0.1 (default 8081). Never exposed beyond
#                       localhost: the local gateway is unauthenticated.
#   ORION_PLANNER, ORION_COMPOSER   `stub` by default; `bedrock` needs Bedrock, below.
#   ORION_EXECUTOR, ORION_DE_LAUNCHER, ORION_DEFAULT_SCOPE   Passed to the agent when set.
#   ORION_FAULTS        E11 fault injection, such as de_run:timeout,composer:refuse (mode L only).
#
# Bedrock (a real planner or composer) is off unless you provide credentials. This script never
# calls AWS and never reads ~/.aws. Export short-term credentials and the model settings in your
# shell first, for example with your own `aws configure export-credentials --format env`, then:
#   ORION_PLANNER=bedrock ORION_COMPOSER=bedrock AWS_REGION=us-east-1 \
#     orion/scripts/orion-local.sh up --no-build
# Compose passes AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN,
# ORION_BEDROCK_CLIENT, ORION_BEDROCK_REGION, ORION_PLANNER_MODEL, ORION_COMPOSER_MODEL and
# ORION_FALLBACK_MODEL only when they are set (the agent's model/config.py has the defaults).
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../.." && pwd)
base_file="$repo_root/compose.yaml"
orion_file="$repo_root/orion/compose.orion.yaml"
project_volume="mmrf-synthetic_orion-runs"

if ! command -v docker >/dev/null 2>&1 && [ -x "$HOME/.docker/bin/docker" ]; then
  PATH="$HOME/.docker/bin:$PATH"
fi

die() {
  printf 'orion-local: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"
}

compose() {
  docker compose --project-directory "$repo_root" -f "$base_file" -f "$orion_file" "$@"
}

compose_base() {
  docker compose --project-directory "$repo_root" -f "$base_file" "$@"
}

require_files() {
  [ -f "$base_file" ] || die "compose.yaml not found at $base_file"
  [ -f "$orion_file" ] || die "orion/compose.orion.yaml not found at $orion_file"
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
}

# A Bedrock planner or composer: refuse to start without a region and credentials, rather than
# letting the first turn fail inside the container.
check_bedrock() {
  local planner=${ORION_PLANNER:-stub} composer=${ORION_COMPOSER:-stub} role
  for role in "$planner" "$composer"; do
    case $role in
      stub | bedrock) ;;
      *) die "ORION_PLANNER and ORION_COMPOSER take stub or bedrock, not $role" ;;
    esac
  done
  if [ "$planner" = stub ] && [ "$composer" = stub ]; then
    return 0
  fi
  [ -n "${AWS_REGION:-}${ORION_BEDROCK_REGION:-}" ] \
    || die "ORION_PLANNER=$planner ORION_COMPOSER=$composer needs AWS_REGION or ORION_BEDROCK_REGION"
  [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ] \
    || die "a Bedrock planner or composer needs short-term AWS credentials exported in this shell"
}

cmd_up() {
  local build=1 frontend_build=1
  while [ $# -gt 0 ]; do
    case $1 in
      --no-build) build=0 ;;
      --skip-frontend-build) frontend_build=0 ;;
      *) die "up: unknown option $1" ;;
    esac
    shift
  done
  check_bedrock
  compose config --quiet
  if [ "$build" = 1 ]; then
    if [ "$frontend_build" = 1 ]; then
      compose build orion-agent frontend
    else
      compose build orion-agent
    fi
  fi
  # --no-build: never rebuild the sandbox's own images as a side effect of starting ORION.
  compose up --detach --no-build --wait orion-agent frontend
  cmd_status
}

cmd_down() {
  local volumes=0
  while [ $# -gt 0 ]; do
    case $1 in
      --volumes) volumes=1 ;;
      *) die "down: unknown option $1" ;;
    esac
    shift
  done
  compose rm --stop --force orion-agent
  # Recreate the frontend from compose.yaml alone: no ORION env, no run-record volume.
  compose_base up --detach --no-build --no-deps --force-recreate frontend
  if [ "$volumes" = 1 ]; then
    if docker volume inspect "$project_volume" >/dev/null 2>&1; then
      docker volume rm "$project_volume"
    fi
  fi
}

cmd_logs() {
  # A bare word is a service name, unless it is the value of an option that takes one.
  local has_service=0 takes_value=0 arg
  for arg in "$@"; do
    if [ "$takes_value" = 1 ]; then
      takes_value=0
      continue
    fi
    case $arg in
      --since | --until | --tail | -n | --index) takes_value=1 ;;
      -*) ;;
      *) has_service=1 ;;
    esac
  done
  if [ "$has_service" = 1 ]; then
    compose logs "$@"
  else
    compose logs "$@" orion-agent
  fi
}

cmd_status() {
  compose ps orion-agent frontend
  local id
  id=$(compose ps --quiet orion-agent)
  if [ -z "$id" ]; then
    printf 'orion-agent is not running\n'
    return 0
  fi
  printf 'orion-agent health: %s\n' "$(docker inspect --format '{{.State.Health.Status}}' "$id")"
  docker exec "$id" python -c \
    "import urllib.request; print('ping:', urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=3).read().decode())"
}

cmd_config() {
  compose config --quiet
  # --no-interpolate: the AWS keys are listed without a value, so an interpolated print would show
  # the shell's exported secret key and session token.
  compose config --no-interpolate orion-agent frontend
}

main() {
  local command=${1:-help}
  [ $# -gt 0 ] && shift
  case $command in
    help | -h | --help) usage; return 0 ;;
  esac
  require_files
  case $command in
    up) cmd_up "$@" ;;
    down) cmd_down "$@" ;;
    logs) cmd_logs "$@" ;;
    status) cmd_status ;;
    config) cmd_config ;;
    *) usage >&2; die "unknown command $command" ;;
  esac
}

main "$@"
