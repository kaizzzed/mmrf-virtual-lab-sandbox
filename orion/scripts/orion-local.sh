#!/usr/bin/env bash
# ORION mode L on the local sandbox stack (RFA rev 25, A4 "Deployment").
#
# Layers orion/compose.orion.yaml on the sandbox's compose.yaml (Compose project mmrf-synthetic):
# adds the `orion-agent` service (stub planner and composer by default, in-process tools against
# http://identity:8080, run records on the volume `orion-runs`) and gives the frontend the
# read-only volume plus ORION_MODE=L, ORION_AGENT_URL and ORION_RUN_DIR.
#
# Usage, from anywhere in the sandbox checkout:
#   orion/scripts/orion-local.sh up [--no-build] [--skip-frontend-build]
#   orion/scripts/orion-local.sh up --bedrock [--no-build] [--profile NAME]
#   orion/scripts/orion-local.sh up --stub [--no-build]
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
#   up --bedrock
#           Stage 2, "Bedrock only": the same local agent with the real planner and composer,
#           Claude Opus 5.5 on Amazon Bedrock (us.anthropic.claude-opus-5-5, refusal fallback
#           us.anthropic.claude-opus-5, region us-east-1). Reads the short-term credentials of the
#           AWS profile `sandbox` (--profile NAME, or ORION_AWS_PROFILE) with
#           `aws configure export-credentials`, never printing them, checks them with one
#           `sts get-caller-identity` (it prints the role name only), sets ORION_PLANNER and
#           ORION_COMPOSER to bedrock and AWS_REGION and ORION_BEDROCK_REGION to us-east-1, then
#           rebuilds and recreates only orion-agent (--no-build skips the build). The frontend and
#           the rest of the stack are not touched. The credentials live only in this script's
#           process and the container's environment; they expire, and then every model call fails
#           (the answer is Observed only, or the run ends Failed): refresh the profile and run
#           `up --bedrock` again. Bedrock calls cost money.
#   up --stub
#           Back to the stub planner and composer: recreates only orion-agent with no AWS variable
#           and no Bedrock setting, whatever the shell exports.
#   down    Stops and removes only the orion-agent container, then recreates the frontend from
#           compose.yaml alone, so the sandbox runs as it did before `up`. The rest of the stack
#           keeps running. --volumes also deletes the volume mmrf-synthetic_orion-runs, which
#           holds every local run record (no undo).
#   logs    `docker compose logs` for orion-agent by default; extra arguments pass through
#           (for example: logs -f --since 10m, or logs frontend).
#   status  Container states, health, the agent's planner and composer, and its /ping answer.
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
#   ORION_AWS_PROFILE   The profile `up --bedrock` reads (default sandbox).
#
# Bedrock without --bedrock: plain `up` never calls AWS and never reads ~/.aws. It passes what
# your shell exports, so with your own short-term credentials exported:
#   ORION_PLANNER=bedrock ORION_COMPOSER=bedrock AWS_REGION=us-east-1 \
#     orion/scripts/orion-local.sh up --no-build
# Compose passes AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN and the
# model settings (ORION_BEDROCK_CLIENT, ORION_BEDROCK_REGION, ORION_PLANNER_MODEL,
# ORION_COMPOSER_MODEL, ORION_FALLBACK_MODEL, the efforts, timeouts, retries, max tokens and cache
# TTL of orion/agent/orion_agent/model/config.py) only when they are set.
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

# Exports the profile's short-term credentials into this process (never printed) and checks them.
use_bedrock() {
  local profile=$1 creds arn
  command -v aws >/dev/null 2>&1 || die "up --bedrock needs the AWS CLI"
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION AWS_PROFILE
  creds=$(aws configure export-credentials --profile "$profile" --format env 2>/dev/null) \
    || die "could not read credentials of the AWS profile $profile; sign in or refresh it"
  case $creds in
    *AWS_ACCESS_KEY_ID=*) ;;
    *) die "the AWS profile $profile gave no credentials" ;;
  esac
  eval "$creds"
  unset creds
  export AWS_REGION=us-east-1 ORION_BEDROCK_REGION=us-east-1 ORION_PLANNER=bedrock ORION_COMPOSER=bedrock
  arn=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) \
    || die "the credentials of the AWS profile $profile are expired or invalid; refresh them and run up --bedrock again"
  case $arn in
    arn:aws:sts::*:assumed-role/*) arn=${arn#*:assumed-role/}; printf 'orion-local: AWS role %s\n' "${arn%%/*}" ;;
    *) printf 'orion-local: AWS identity checked\n' ;;
  esac
}

# Clears every Bedrock and AWS variable, so Compose starts the agent with the stubs.
use_stub() {
  unset ORION_PLANNER ORION_COMPOSER AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION \
    AWS_REGION ORION_BEDROCK_CLIENT ORION_BEDROCK_REGION ORION_PLANNER_MODEL ORION_COMPOSER_MODEL ORION_FALLBACK_MODEL
}

cmd_up() {
  local build=1 frontend_build=1 roles="" profile=${ORION_AWS_PROFILE:-sandbox}
  while [ $# -gt 0 ]; do
    case $1 in
      --no-build) build=0 ;;
      --skip-frontend-build) frontend_build=0 ;;
      --bedrock) roles=bedrock ;;
      --stub) roles=stub ;;
      --profile)
        [ $# -ge 2 ] || die "up: --profile needs a profile name"
        profile=$2
        shift
        ;;
      *) die "up: unknown option $1" ;;
    esac
    shift
  done
  case $roles in
    bedrock) use_bedrock "$profile" ;;
    stub) use_stub ;;
  esac
  check_bedrock
  compose config --quiet
  if [ -n "$roles" ]; then
    # Only the agent changes: the frontend and the rest of the stack keep running as they are.
    if [ "$build" = 1 ]; then
      compose build orion-agent
    fi
    compose up --detach --no-deps --no-build --wait orion-agent
    cmd_status
    return 0
  fi
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
  # Two named variables only: the container's environment also holds AWS credentials.
  printf 'orion-agent planner: %s, composer: %s\n' \
    "$(docker exec "$id" sh -c 'printf %s "${ORION_PLANNER:-stub}"')" \
    "$(docker exec "$id" sh -c 'printf %s "${ORION_COMPOSER:-stub}"')"
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
