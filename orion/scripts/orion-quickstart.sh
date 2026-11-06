#!/usr/bin/env bash
# ORION quickstart: the local sandbox stack and the ORION Assistant in one command.
#
# Usage, from anywhere in the sandbox checkout:
#   orion/scripts/orion-quickstart.sh [--no-build]
#   orion/scripts/orion-quickstart.sh --bedrock [--profile NAME] [--no-build]
#
#   1. Checks that Docker is running.
#   2. Starts the sandbox stack with scripts/start.sh when http://127.0.0.1:3080 does not answer
#      (fetches the pinned sources, generates the synthetic data, builds and starts the services).
#      When the stack already answers, start.sh is skipped.
#   3. Starts ORION with `orion/scripts/orion-local.sh up`: the agent container and the frontend
#      with ORION on, using the stub planner and composer (no AWS, no model calls).
#   4. With --bedrock, switches the agent to Claude Opus 5.5 on Amazon Bedrock with
#      `orion/scripts/orion-local.sh up --bedrock`.
#   5. Waits until the Assistant answers, then prints its URL.
#
#   --bedrock       The real planner and composer. Needs the AWS CLI and a profile with Bedrock
#                   access to Claude Opus 5.5 in us-east-1. Credentials are never printed.
#                   Bedrock calls cost money.
#   --profile NAME  The AWS profile --bedrock reads (default: $ORION_AWS_PROFILE, else `sandbox`).
#   --no-build      Use the ORION images already built (passed to orion-local.sh up).
#   -h, --help      This help.
#
# Stop ORION with `orion/scripts/orion-local.sh down`; stop the stack with `docker compose down`.
# All data is synthetic.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/../.." && pwd)
local_sh="$script_dir/orion-local.sh"
probe_url="http://127.0.0.1:3080/"
assistant_probe="http://127.0.0.1:3080/?app=Assistant"
assistant_url="http://localhost:3080/?app=Assistant"

die() {
  printf 'orion-quickstart: %s\n' "$*" >&2
  exit 1
}

say() {
  printf 'orion-quickstart: %s\n' "$*"
}

usage() {
  sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"
}

answers() {
  curl -fs -o /dev/null --max-time 5 "$1"
}

bedrock=0
profile=""
build_args=()
while [ $# -gt 0 ]; do
  case $1 in
    --bedrock) bedrock=1 ;;
    --profile)
      [ $# -ge 2 ] || die "--profile needs a profile name"
      profile=$2
      shift
      ;;
    --no-build) build_args=(--no-build) ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option $1" ;;
  esac
  shift
done
[ -z "$profile" ] || [ "$bedrock" = 1 ] || die "--profile goes with --bedrock"

[ -f "$repo_root/compose.yaml" ] && [ -x "$local_sh" ] || die "run this from the sandbox checkout"
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"
if ! command -v docker >/dev/null 2>&1 && [ -x "$HOME/.docker/bin/docker" ]; then
  PATH="$HOME/.docker/bin:$PATH"
fi
command -v docker >/dev/null 2>&1 || die "Docker is not installed: install Docker Desktop"
docker info >/dev/null 2>&1 || die "Docker is not running: start Docker Desktop, then run this again"
if [ "$bedrock" = 1 ]; then
  command -v aws >/dev/null 2>&1 || die "--bedrock needs the AWS CLI"
fi

# 1. The sandbox stack, started the way the repository's README starts it.
if answers "$probe_url"; then
  say "the sandbox stack is up"
else
  say "starting the sandbox stack (scripts/start.sh; the first run builds every image)"
  bash "$repo_root/scripts/start.sh"
fi

# 2. ORION with the stubs, whatever the shell exports for the planner and composer.
say "starting ORION (stub planner and composer)"
(
  unset ORION_PLANNER ORION_COMPOSER
  "$local_sh" up ${build_args[@]+"${build_args[@]}"}
)

# 3. Optionally, the agent on Bedrock. The image was just built, so only the agent is recreated.
if [ "$bedrock" = 1 ]; then
  say "switching the agent to Claude Opus 5.5 on Amazon Bedrock"
  bedrock_args=(up --bedrock --no-build)
  [ -z "$profile" ] || bedrock_args+=(--profile "$profile")
  "$local_sh" "${bedrock_args[@]}"
fi

# 4. Wait for the Assistant page.
tries=0
until answers "$assistant_probe"; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || die "the Assistant did not answer at $assistant_url; see orion/scripts/orion-local.sh status and logs"
  sleep 2
done

if [ "$bedrock" = 1 ]; then
  say "ORION is up with Claude Opus 5.5 on Amazon Bedrock"
else
  say "ORION is up with the stub planner and composer (they answer only the built-in starter questions)"
fi
printf '\nOpen %s\n\n' "$assistant_url"
