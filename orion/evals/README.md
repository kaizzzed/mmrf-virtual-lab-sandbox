# ORION eval harness (E1–E24)

The eval harness and the independent oracle of RFA rev 25, "Observability and evaluation". It
drives the agent through the wire contract (`../contracts/README.md` v1.2), checks every eval in
the design's table against an oracle that reads the local stack with its own requests and its own
arithmetic, and writes a JSON and a Markdown report.

It is a black box: nothing here imports `orion_agent`. The contract's canonical JSON, `defSha256`,
`pinnedHash`, the fingerprint and `format_fact` are reimplemented in `orion_evals/canonical.py`
and `factformat.py` and tested against the shared vectors in `../contracts/fixtures`. It needs
only the Python standard library, so it runs on `orion-agent:dev` as it is.

## Run it

Everything runs in a container on the Compose network `mmrf-synthetic_default`, from the sandbox
repo root, with `export PATH=$HOME/.docker/bin:$PATH`.

**Unit tests** (no engine needed: they replay the frozen fixtures through the check functions and
drive the scenarios against a fake agent). The live oracle test runs when the stack is up and
records what it computed under `reports/`; the fork mount checks the six reference prompts against
the fork's starters; the agent mount validates every request the harness builds against the frozen
`orion_agent.models`.

```sh
docker run --rm --network mmrf-synthetic_default \
  -v $PWD/orion/evals:/evals -v $PWD/orion/contracts:/contracts:ro \
  -v $PWD/orion/agent:/agent:ro -e ORION_AGENT_SRC=/agent \
  -v $PWD/.sources/frontend:/fork:ro \
  -w /evals -e PYTHONPATH=/evals:/evals/tests orion-agent:dev \
  python -m unittest discover -s tests -v
```

**Type check** (`orion-agent:evals` is `orion-agent:dev` plus `mypy==1.18.2`):

```sh
printf 'FROM orion-agent:dev\nRUN pip install --no-cache-dir mypy==1.18.2\n' | docker build -t orion-agent:evals -
docker run --rm -v $PWD/orion/evals:/evals -w /evals -e MYPYPATH=/evals:/evals/tests orion-agent:evals \
  mypy --strict orion_evals tests
```

**The oracle alone** (no agent): computes and records J1, J1 in the race cohort, J2, J3 with its
DE table, and E9's small group.

```sh
docker run --rm --network mmrf-synthetic_default -v $PWD/orion/evals:/evals -w /evals orion-agent:dev \
  python -m orion_evals oracle
```

**The evals, mode L, in one command:** `orion/scripts/evals-mode-l.sh agent` runs E1–E24 against
the Compose `orion-agent`, starting the four E11 fault agents itself and removing them afterwards;
`evals-mode-l.sh bff` sends the same turns through the fork's BFF behind the identity gateway and
reads records from its routes; `evals-mode-l.sh e20-lease` runs E20 with its live lease expiry (the
harness and its own agent share one container, so `--restart-cmd` can kill the controller). Extra
arguments go to `run`. The results and their meaning are in `orion/notes/mode-l-status.md`.

**Stage 2, Bedrock, in one command:** `orion/scripts/evals-bedrock.sh` rebuilds and recreates the
Compose `orion-agent` with the planner and composer on Bedrock (`orion-local.sh up --bedrock`), runs
E1–E24 against it (5 repeats, E22's held-out paraphrases included), sums the run records' model calls
and tokens (`usage`) and measures NFR-01 timings (`timings --mode warm`). `evals-bedrock.sh evals
--repeats 1` is the cheap pass; `timings --mode cold` and `--mode cold-warmup` idle past the cache
TTL before each ask. Results: `orion/notes/bedrock-evals.md`. Every ask calls Bedrock.

- `python -m orion_evals timings` drives the agent like the browser (`orion_evals/timings.py`) and
  reports per ask the plan-visible time (the gate's firstPlanS), the "Writing interpretation" wait
  (first snapshot with Observed to the first with Interpretation) and each model call's time, tokens
  and cache use, from the run record. `--mode warm|cold|cold-warmup`, `--rounds`, `--journeys
  J1:open-ended,J3:tool-named`, `--idle-s` (default 330, past the 5-minute TTL). Writes
  `reports/timings-<mode>-<time>.json`.
- `python -m orion_evals usage reports/orion-evals-<time>.json` sums the `modelCalls` of every run
  the report drove (each run once): calls per role, later calls (repairs, plans after a
  clarification, re-composes), outcomes, models, efforts, times and tokens.

**The evals, mode L, by hand** (mode L is up with `orion/scripts/orion-local.sh up`). The agent's run volume is mounted read-only so record-based
checks can run:

```sh
docker run --rm --network mmrf-synthetic_default \
  -v $PWD/orion/evals:/evals -v mmrf-synthetic_orion-runs:/data/runs:ro \
  -v $PWD/.sources/frontend:/fork:ro -v $PWD/orion/agent/tests:/agent-tests:ro -v $PWD/orion/scripts:/orion-scripts:ro \
  -w /evals orion-agent:dev python -m orion_evals run \
    --agent-url http://orion-agent:8080/invocations --run-dir /data/runs \
    --frontend-root /fork --agent-tests-root /agent-tests --e2e-script /orion-scripts/e2e-mode-l.mjs
```

`--e2e-script` names the browser E2E (`orion/scripts/e2e-mode-l.mjs`): a check only a browser can
make, such as E3/E4's "a chat-made cohort opens in Cohort Builder", is covered only by the E2E step
that opens Cohort Builder and reads the cohort's filter and case count (scenario `j2`), never by a
unit test's name. Without the flag it is "not covered".

Without the Compose service (or to test the working tree), start a throwaway agent from the current
code on the stack's network and point the harness at it; a bind-mounted run directory doubles as the
record source (the harness reads it even before the first run writes `runs/`):

```sh
mkdir -p /tmp/orion-runs/main && chmod 777 /tmp/orion-runs/main
docker run -d --name orion-agent-evals --network mmrf-synthetic_default \
  -v $PWD/orion/agent:/app:ro -v $PWD/config:/config:ro -v $PWD/orion/golden:/golden:ro \
  -v /tmp/orion-runs/main:/data/runs -e ORION_MODE=L -e ORION_VLAB_ORIGIN=http://identity:8080 orion-agent:dev
docker run --rm --network mmrf-synthetic_default -v $PWD/orion/evals:/evals:ro -v /tmp/orion-runs:/runs:ro \
  -v $PWD/.sources/frontend:/fork:ro -v $PWD/orion/agent/tests:/agent-tests:ro -v $PWD/orion/scripts:/orion-scripts:ro \
  -w /evals orion-agent:dev \
  python -m orion_evals run --agent-url http://orion-agent-evals:8080/invocations --run-dir /runs/main \
    --frontend-root /fork --agent-tests-root /agent-tests --e2e-script /orion-scripts/e2e-mode-l.mjs
docker rm -f orion-agent-evals
```

For E11, start one more agent per fault the same way (`-e ORION_FAULTS=de_run:timeout`, its own run
directory) and pass `--fault-agent de_run:timeout=http://<name>:8080/invocations@/runs/<dir>`.

- `--evals E1,E7` runs a subset (default: all 24). `--repeats` (default 5, E16) repeats each
  reference prompt. E20 waits `--pause-wait-s` (default 300) at the save pause.
- `--target bff --bff-origin http://frontend:3000` sends the same turns through the fork's
  `POST /api/orion/agui` and reads records from its record and view routes
  (`--record-source bff`).
- **E11 faults** need an agent started with `ORION_FAULTS`. Each fault is its own process, so run
  them one at a time: `ORION_FAULTS=de_run:timeout orion/scripts/orion-local.sh up`, then
  `run --evals E11 --fault-agent de_run:timeout=http://orion-agent:8080/invocations@/data/runs`;
  repeat for `planner:refuse`, `composer:refuse` and `cohort_count:empty`. A fault with no agent is
  reported not run.
- **E20's lease expiry** needs the controller killed mid-run: pass `--restart-cmd` with an
  operator command the harness may run (it waits `--lease-wait-s`, default 200 s, for the 180 s
  lease). Without it the case is reported as covered by an agent test that names
  `RUN_INTERRUPTED`, or not covered.
- **E22** needs the real planner (`ORION_PLANNER=bedrock`). With any other planner, read from the
  run records' `environment.planner` (or `--planner`), it is reported "needs Bedrock planner",
  never as a pass.
- The oracle reads `--oracle-origin` (default `$ORION_VLAB_ORIGIN` or `http://identity:8080`).
  Locally it sends no credential. For a hosted gateway, `--access-file` names the repo's access
  file (`generated/aws/access.json`), which is read at run time and never printed or logged.

The command exits 1 when any eval fails. Reports go to `reports/` (gitignored).

## What a result means

Each eval reports `pass`, `fail` or `not-run`, decided by its gating checks: fail if any failed;
otherwise not run if any could not run (no agent, no record source, no oracle, a browser check
with no frontend test); otherwise pass. Non-gating checks (the contract extras on every run:
stream contract, plan before the first call, chips, fact traceability, the counted set, viz parity,
the confirmation card's chips) are reported but decide only the gates.

Every eval quotes its row's "Passes when" from the design's table and the R4 acceptance check of
each FR it implements. Check sources: `agent` (the stream), `record` (the run record), `oracle`,
`simulated browser`, `browser` (frontend-test coverage) and `harness`.

**Gates** (R4 success metrics) are scored over all runs: 6 of 6 reference prompts, phrasing parity
(E1–E6 signatures and E22), Interpretation present (E16), 9 of 9 edge cases (E7–E10, E11's
timeout, refusal and empty count, E17, E18), 100% of the counted set visible, 100% of numbers
cited, zero writes without confirmation, the plan p95 within 5 s over at least 20 asks, and J3
count accuracy.

**Simulated browser.** The harness has no browser. For Create it follows the contract's steps in
an in-memory cohort store (`orion_evals/simstore.py`): reuse by `defSha256`, a save ledger keyed
by the nonce, the FilterSet the contract specifies, unique names, and the read-back with
`defSha256` recomputed from the stored filters. Reports label these checks "simulated browser".
Browser-only checks (the viewer mounts, Enter saves nothing, Open on this cohort, the Could not
plan card, Stopped by you, hand-off disabled after Don't save, thread restore) are recorded as
"covered by frontend test <file>" when that test exists in the fork (`orion_evals/frontend.py`).

## The oracle

`orion_evals/oracle.py` uses its own HTTP client and its own requests, and none of the tools' code
or golden bodies.

| Journey | The oracle computes | How |
| --- | --- | --- |
| J1 | n, events, the log-rank p, medians per CGS group | Per-case clinical fields from `/analysis/v0/cases/` (vital status, days to death, last follow-up); its own Mantel-Cox test (`stats.py`); its own Kaplan-Meier. The engine's p is fetched only to corroborate |
| J2 | cohort size, denominator, per-gene mutated counts, the top N and ties at the cutoff | The race cohort's case ids, each panel gene's `gene_cases` over all cases, intersected in code. ProteinPaint's ranking corroborates |
| J3 | mutation-tested, mutated, not detected, outside, samples, RNA-seq-eligible counts, the top 10 | Set arithmetic in code; the KRAS dictionary term's sample counts corroborate. Samples, eligibility and the DE table come from ProteinPaint (no second engine) with the oracle's own group names; thresholds, ranking and ties are the oracle's |

`stats.py` is tested against textbook values: chi-square critical values and the Freireich 6-MP
trial (log-rank 16.79, expected 19.25 and 10.75, medians 23 and 8 weeks).

## Prompts

`prompts.json` holds every question the evals ask: the six reference prompts (equal to the fork's
`REFERENCE_PROMPTS`), the held-out paraphrases (E22), the edge and context questions and the
out-of-kind questions (E23). C-31 requires that none appear among the planner's few-shot examples.
`python -m orion_evals prompts --check-agent /agent/orion_agent/model/eval_prompts.py` lists the
ones the agent's exclusion list lacks (it reads that file with `ast`, never imports it).

## Layout

| Path | What it holds |
| --- | --- |
| `orion_evals/client.py` | One POST per turn, SSE read, BFF errors, record sources (run dir, BFF routes) |
| `orion_evals/sse.py`, `wire.py` | The wire format; request builders, statuses, screen words |
| `orion_evals/session.py` | A thread driven like the browser: views by revision, pending args, Create, a second action while a turn streams |
| `orion_evals/simstore.py` | The simulated browser's cohort store |
| `orion_evals/canonical.py`, `factformat.py` | Canonical JSON, `defSha256`, FilterSets, `pinnedHash`, the fingerprint, `format_fact` |
| `orion_evals/oracle.py`, `stats.py` | The oracle and its statistics |
| `orion_evals/checks.py`, `extract.py`, `evalchecks.py` | Check primitives, journey values read from chips, the check function of each eval |
| `orion_evals/scenarios.py`, `evals.py` | Drivers, and E1–E24 as data plus a run function each |
| `orion_evals/runner.py`, `report.py`, `cli.py` | Running, gates, environment, reports, the command line |
| `orion_evals/timings.py` | Stage 2: NFR-01 timings through the agent, and a report's model calls and tokens |
| `tests/` | Unit tests (fixture replay, fake agent, local HTTP server, fake gateway) and the live oracle test |
