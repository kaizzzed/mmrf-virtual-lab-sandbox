#!/usr/bin/env bash
# ORION stage 2 ("Bedrock only") evals in one command: the local Compose agent with in-process tools,
# the planner and composer on Claude Opus 5.5 on Amazon Bedrock (us.anthropic.claude-opus-5-5, refusal
# fallback us.anthropic.claude-opus-5, us-east-1). Results: orion/notes/bedrock-evals.md.
#
# Usage, from anywhere in the sandbox checkout (the sandbox stack up, AWS profile `sandbox` signed in):
#   orion/scripts/evals-bedrock.sh [run] [harness args...]
#   orion/scripts/evals-bedrock.sh evals [harness args...]
#   orion/scripts/evals-bedrock.sh timings [timing args...]
#   orion/scripts/evals-bedrock.sh usage [REPORT.json]
#   orion/scripts/evals-bedrock.sh check
#
#   run      Rebuilds and recreates orion-agent on Bedrock from the working tree
#            (`orion-local.sh up --bedrock`), then `evals`, `usage` and `timings --mode warm`.
#            About 45 minutes and 250 model calls with the default 5 repeats.
#   evals    `check`, then E1–E24 against the running agent (`evals-mode-l.sh agent`): 5 repeats per
#            reference prompt (E16), E22's held-out paraphrases (three per journey, 5 runs each), and
#            E11's four fault agents (stubs, no model call). Extra arguments go to the harness, for
#            example --repeats 1 for a cheap pass, or --evals E10,E15.
#   timings  `check`, then `python -m orion_evals timings` (NFR-01; Open questions 10 and 12):
#            plan-visible time and the "Writing interpretation" wait per ask, with each model call's
#            time and tokens. --mode warm (default), cold (idle past the cache TTL, no warm-up) or
#            cold-warmup (idle, then the warm-up action as the Assistant sends it); --rounds N.
#   usage    `python -m orion_evals usage`: model calls and tokens of an eval report's runs, from
#            their run records (the latest orion/evals/reports/orion-evals-*.json by default).
#   check    The sandbox profile's credentials (one sts call; only the role name is printed) and the
#            agent's roles: both must be bedrock. The credentials the running agent holds were
#            exported when it was started; after a refresh, `run` (or `orion-local.sh up --bedrock`)
#            hands the new ones to it.
#
# Every ask calls Bedrock and costs money. When the credentials expire, every model call fails (the
# planner's runs end Failed, the composer's answers Observed only): stop, refresh the profile, and
# `run` again. Credentials are never printed or written: they stay in orion-local.sh's process and
# the container's environment. Reports land in orion/evals/reports/ (gitignored); the exit code is
# the harness's (1 when an eval fails).
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 && [ -x "$HOME/.docker/bin/docker" ]; then
  PATH="$HOME/.docker/bin:$PATH"
fi

HARNESS_IMAGE=orion-agent:dev
NETWORK=mmrf-synthetic_default
RUNS_VOLUME=mmrf-synthetic_orion-runs
PROFILE=${ORION_AWS_PROFILE:-sandbox}

die() {
  printf 'evals-bedrock: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/d;s/^# \{0,1\}//;p;}' "${BASH_SOURCE[0]}"
}

repo_root() {
  local dir
  dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
  [ -f "$dir/compose.yaml" ] && [ -d "$dir/orion/evals" ] || die "run this from the sandbox checkout"
  printf '%s\n' "$dir"
}

# The Compose service's container (the E11 fault agents run the same image, so labels alone do not tell them apart).
agent_container() {
  local root=$1
  docker compose --project-directory "$root" -f "$root/compose.yaml" -f "$root/orion/compose.orion.yaml" ps --quiet orion-agent
}

# The profile's credentials still work (role name only), and the running agent plans and composes on Bedrock.
cmd_check() {
  local root=$1 arn id planner composer
  command -v aws >/dev/null 2>&1 || die "the AWS CLI is not on PATH"
  arn=$(aws sts get-caller-identity --profile "$PROFILE" --query Arn --output text 2>/dev/null) \
    || die "the credentials of the AWS profile $PROFILE are expired or invalid: refresh them, then run: $0 run"
  case $arn in
    arn:aws:sts::*:assumed-role/*) arn=${arn#*:assumed-role/}; printf 'evals-bedrock: AWS role %s\n' "${arn%%/*}" ;;
    *) printf 'evals-bedrock: AWS identity checked\n' ;;
  esac
  id=$(agent_container "$root")
  [ -n "$id" ] || die "orion-agent is not running: $0 run starts it on Bedrock"
  # Two named variables only: the container's environment also holds AWS credentials.
  planner=$(docker exec "$id" sh -c 'printf %s "${ORION_PLANNER:-stub}"')
  composer=$(docker exec "$id" sh -c 'printf %s "${ORION_COMPOSER:-stub}"')
  printf 'evals-bedrock: orion-agent planner %s, composer %s\n' "$planner" "$composer"
  [ "$planner" = bedrock ] && [ "$composer" = bedrock ] \
    || die "orion-agent runs the stubs: $0 run (or orion/scripts/orion-local.sh up --bedrock) first"
}

cmd_evals() {
  local root=$1
  shift
  cmd_check "$root"
  "$root/orion/scripts/evals-mode-l.sh" agent "$@"
}

cmd_timings() {
  local root=$1
  shift
  cmd_check "$root"
  docker run --rm --network "$NETWORK" -v "$root/orion/evals:/evals" -v "$RUNS_VOLUME:/data/runs:ro" \
    -w /evals "$HARNESS_IMAGE" python -m orion_evals timings "$@"
}

cmd_usage() {
  local root=$1 report=${2:-}
  if [ -z "$report" ]; then
    report=$(find "$root/orion/evals/reports" -maxdepth 1 -name 'orion-evals-*.json' | sort | tail -1)
    [ -n "$report" ] || die "no eval report in orion/evals/reports"
  fi
  [ -f "$report" ] || die "no report at $report"
  report=$(cd "$(dirname "$report")" && pwd)/$(basename "$report")
  case $report in
    "$root/orion/evals/reports/"*) ;;
    *) die "the report must be in orion/evals/reports" ;;
  esac
  docker run --rm -v "$root/orion/evals:/evals:ro" -v "$RUNS_VOLUME:/data/runs:ro" \
    -w /evals "$HARNESS_IMAGE" python -m orion_evals usage "/evals/reports/$(basename "$report")"
}

cmd_run() {
  local root=$1 status=0
  shift
  ORION_AWS_PROFILE=$PROFILE "$root/orion/scripts/orion-local.sh" up --bedrock
  cmd_evals "$root" "$@" || status=$?
  cmd_usage "$root"
  cmd_timings "$root" --mode warm --rounds 4
  return "$status"
}

main() {
  local command=${1:-run}
  [ $# -gt 0 ] && shift
  case $command in
    help | -h | --help) usage; return 0 ;;
  esac
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
  local root
  root=$(repo_root)
  case $command in
    run) cmd_run "$root" "$@" ;;
    evals) cmd_evals "$root" "$@" ;;
    timings) cmd_timings "$root" "$@" ;;
    usage) cmd_usage "$root" "$@" ;;
    check) cmd_check "$root" ;;
    -*) cmd_run "$root" "$command" "$@" ;;
    *)
      usage >&2
      die "unknown command $command"
      ;;
  esac
}

main "$@"
