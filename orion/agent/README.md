# ORION agent (run controller and engine)

The ORION Workstream 2 run controller: plain Python on the `bedrock-agentcore` SDK's `AGUIApp`
(RFA rev 25, A2 and A3). It serves `POST /invocations` (AG-UI events over SSE), `GET /ping`
and `/ws` on port 8080, and speaks the wire contract in `../contracts/README.md` (v1.2). The tool
and step catalog it runs is `../contracts/TOOLS.md` with `orion_agent/tools/schemas.py`.

A run is the controller's state machine (A3): plan (one planner call), compile (code), execute the
signed plan step by step (code), render Observed and Limitations from templates (code), compose
Interpretation (one composer call) and verify it (code). Every number on screen is a fact from a
step; the model names numbers only through fact placeholders.

## Layout

| Path | What it holds |
| --- | --- |
| `orion_agent/app.py` | Settings to a wired controller (`build_controller`), the `AGUIApp` entrypoint and a synchronous ping |
| `orion_agent/controller.py` | Every action and the contract's checks 1 to 8; the detached run task; planning, the save, clarify and consent pauses; edits as new plan versions; Answering; the run registry, heartbeats and leases |
| `orion_agent/executor.py` | The step engine (`Engine`, `interfaces.Executor`): StepRef resolution, pre- and post-guards, the call budget, Stop, the save pause and its read-back check; the four pure functions; input hashes and step reuse; the plan's run cohorts |
| `orion_agent/extractor.py` | One call's output to facts with JSON pointers, callIds, hashes and engines (catalog fact specs) |
| `orion_agent/observed.py` | The Observed renderer (`TemplateRenderer`): Observed sentences, limitation lines and viz specs from the catalog's templates; the TEXT_MESSAGE markdown |
| `orion_agent/ledger.py` | The session ledger (`StoreLedger`), built from the thread's run records |
| `orion_agent/run_store.py` | Run record (`orion.run-record/v1`), thread index, `RunStore` (mode L, atomic rename) and `S3RunStore` (modes A and H), the single writer per run |
| `orion_agent/lease.py` | Lease creation, expiry and the renewal loop |
| `orion_agent/events.py` | AG-UI event builders (the `save_cohorts` tool call and TEXT_MESSAGE in 8,192-character deltas) and the 32 KB snapshot trimming |
| `orion_agent/settings.py`, `ids.py` | Environment settings; 128-bit run and thread ids |
| `orion_agent/models.py`, `interfaces.py` | The wire contract as Pydantic models; the internal interfaces between controller, engine and model roles |
| `orion_agent/ir.py`, `compiler.py`, `vocab.py`, `catalog.py` | The planner IR; the compiler (IR to signed plan, its edits, the clarify and Unsupported cards) |
| `orion_agent/tools/` | The tool contract (`base.py`), the catalog (`schemas.py`) and the server tools and their ToolClient (`client.py`: `make_tool_client`) |
| `orion_agent/model/` | The planner, composer and verifier (`factory.py`: `make_planner`, `make_composer`, `make_verifier`), the model call scope, the answer writer and warm-up |
| `orion_agent/stubs.py` | The Day 1 stub planner and executor (`ORION_PLANNER=stub` through the model factory; `ORION_EXECUTOR=stub`) |
| `tests/` | `unittest` suites. `test_engine_*.py` drive the controller and engine through the real AGUIApp with fakes at the edges (`test_engine_fakes.py`) |

## Build

The host Python is unsuitable, so everything runs in containers. The image builds natively for
arm64 on Apple silicon, which AgentCore Runtime requires; on an amd64 host use
`docker buildx build --platform linux/arm64`.

```sh
export PATH=$HOME/.docker/bin:$PATH
docker build -t orion-agent:dev orion/agent
```

## Test

From the sandbox repo root, with the local stack up (the live vocabulary tests skip without it):

```sh
docker run --rm --network mmrf-synthetic_default \
  -v $PWD/orion/agent:/app -v $PWD/config:/config:ro -v $PWD/orion/contracts:/contracts:ro \
  -v $PWD/.sources/frontend/config/gen3/analysisTools.json:/analysisTools.json:ro \
  -e ORION_VLAB_ORIGIN=http://identity:8080 -w /app orion-agent:dev \
  python -m unittest discover -s tests -v
```

The engine suites compare each journey's stream with its fixture's shape (`test_engine_fakes.shape`:
events in order, statuses, step rows, fact ids, Limitations codes, pauses, cohorts): J1 to Complete,
J1 Partial then `retry_step`, J1 Stop, J3 to the save pause, Create, Don't save, Rename at the
pause and the DE fallback. Type checking is not part of the image; run
`pip install mypy==1.18.2 && MYPYPATH=tests mypy --strict orion_agent tests` in a throwaway container.

## Run in mode L

```sh
docker run -d --rm --name orion-agent --network mmrf-synthetic_default -p 127.0.0.1:8081:8080 \
  -v $PWD/config:/config:ro -v $PWD/orion/agent/.orion-runs:/data/runs orion-agent:dev
curl -s http://127.0.0.1:8081/ping
curl -sN http://127.0.0.1:8081/invocations -H 'content-type: application/json' -d @ask.json
```

Publish the port on 127.0.0.1 only. A missing factory module, or mode A or H without a bucket,
stops startup with a message naming the setting that needs it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORION_MODE` | `L` | `L`: records under `ORION_RUN_DIR`, tools in-process. `A`, `H`: records in S3 (`ORION_RUN_BUCKET`) |
| `ORION_RUN_DIR` | `/data/runs` | Mode L: run records (`runs/<runId>.json`) and thread indexes (`threads/<threadId>.json`) |
| `ORION_RUN_BUCKET`, `ORION_RUN_PREFIX` | none, `orion/` | Modes A and H: `s3://<bucket>/<prefix>runs/<runId>.json` and `threads/`; required in A and H |
| `AWS_REGION` | `us-east-1` | The S3 client's region (modes A and H) |
| `ORION_PLANNER`, `ORION_COMPOSER` | `stub` | `stub` or `bedrock`, through `orion_agent.model.factory` |
| `ORION_EXECUTOR` | `engine` | `engine` (the step engine) or `stub` (Day 1: every step Skipped `NOT_IMPLEMENTED`) |
| `ORION_TOOLS` | `inprocess` | `inprocess` or `gateway`; the record's `environment.toolTransport` is the tool client's own |
| `ORION_DE_LAUNCHER` | `deDirect` | The DE card: the direct mount, or the DEinput fallback (cut 2) when both groups are saved |
| `ORION_FIELDS_PATH` | `/config/cohort-fields.json` | The sandbox vocabulary, read-only; the app will not start without it |
| `ORION_DEFAULT_SCOPE` | `active_cohort` | Scope when the question names none (Open question 1), recorded as an assumption; or `all_cases` |
| `ORION_VLAB_ORIGIN` | `http://identity:8080` | The identity gateway the in-process tools call |
| `ORION_HEARTBEAT_S` | `15` | `CUSTOM orion.heartbeat` cadence while an invocation streams |
| `ORION_LEASE_RENEW_S`, `ORION_LEASE_TTL_S` | `30`, `180` | Lease renewal cadence while Planning, Executing or Answering; expiry after the last renewal |
| `ORION_STOP_GRACE_S` | `5` | After Stop, the executor's time to return before the controller cancels it (at most `STOP_GRACE_S`) |
| `ORION_WARMUP_TIMEOUT_S` | `30` | How long a `warmup` turn waits for the model roles to warm their cached prefixes |
| `ORION_FAULTS`, `ORION_TEST_RUNTIME` | none, `0` | Fault injection (E11), honoured only in mode L or a test Runtime (contract "Fault injection") |

The tools and model packages read their own variables as well (`ORION_GOLDEN_DIR`, the Gateway
URL, model ids); see their modules.

## How a run executes

- **Plan first.** The plan snapshot (scope line, rows, Queued steps, run cohorts) is sent before
  any data call. A compile-time stop makes no data call. A Clarify from the planner pauses
  Clarifying with a card from code templates; a second one ends Limited `AMBIGUOUS_REQUEST`. A
  named tool that does not fit pauses AwaitingConsent with no data call.
- **Steps.** STEP_STARTED, then a snapshot with the step Running; a finished step's row, facts and
  cohorts arrive in the next snapshot (the next step's start, a pause or the end), as the fixtures
  show. StepRefs resolve against earlier calls; a filtered scope is resolved once (C-23). Server
  reads go through the ToolClient (one retry inside it, shown as Retrying); the four pure
  functions run in-process; every attempt is a CallRecord with callId `<runId>.v<n>.<step>.c<k>`,
  k continuing across `retry_step` reruns. At most `MAX_TOOL_CALLS_PER_RUN` (20) calls per plan version.
- **Guards.** A pre-guard (a survival group under 10 cases, a filter ProteinPaint cannot read, more
  than 4,000 DE samples) skips its step with its code. A stop post-guard (a zero count, more than
  10,000 ids, fewer than 10 eligible samples) keeps the step's facts and skips its dependents
  ("Not run: s2 found no cases"); the run ends Limited. A failed read skips its dependents
  ("Not run: s4 failed") and the run ends Partial. Report guards (`NOT_SUBSET`, `FEWER_THAN_N`,
  `TIES_AT_CUTOFF`) add their line and the run goes on.
- **The save.** The write step pauses AwaitingConfirmation with the pending payload (definitions,
  `defSha256`, counts and eligible counts with their facts, `reuse`) and sends it as the
  `save_cohorts` tool call. Create resumes with the write step Running, checks every read-back
  (cohortId, name, `defSha256`), records the nonce, and runs DE on the read-back names; a
  mismatch ends Failed `WRITE_VERIFY_FAILED`. Don't save skips it `WRITE_SKIPPED` and DE runs on
  the proposed names. A resent confirm replays the recorded outcome (E19).
- **Answering.** Observed, Limitations and viz specs are rendered from templates and sent at once
  (status Answering); then the composer and the verifier run (`orion_agent.model.answer` when
  installed: one re-compose after a first failure, failing sentences dropped). Complete carries all
  four parts; ObservedOnly carries `withheldReason`. The TEXT_MESSAGE follows the last snapshot.
- **Stop.** Stopping at once; the in-flight planner, tool or composer call is aborted and recorded
  (outcome `cancelled`, or a CallRecord whose error starts `CANCELLED`); open and queued steps are
  Skipped "Not run: stopped"; Cancelled with Observed for the finished steps.
- **Edits.** Version n+1 is compiled with no model call (the compiler's `edit`). On a live run the
  edit waits for the running step at its boundary (heartbeats only) or aborts it when its input
  changes; the edit stream starts with the applied snapshot (Executing, version n+1, `superseded`,
  Reused steps with their facts, a voided nonce). Reuse compares input hashes over the tool, its
  version, its args (StepRefs and step ids replaced by the referenced step's hash) and `dependsOn`.
- **Leases.** A record in a leased status with a lapsed lease and no live task here is recorded as
  of its expiry by the next turn that touches it: Interrupted (`RUN_INTERRUPTED`), or Cancelled
  when it was Stopping.

## Codes (33)

| Kind | Codes |
| --- | --- |
| Limitations and errors (v1) | `NAMED_TOOL_MISMATCH`, `OUT_OF_PANEL`, `INELIGIBLE_GROUP`, `TOOL_FAILED`, `MODEL_DECLINED`, `PLANNER_DECLINED`, `UNSUPPORTED_ANALYSIS`, `CLINICAL_ADVICE`, `OUT_OF_SCOPE`, `EMPTY_COHORT`, `WRITE_SKIPPED`, `RUN_INTERRUPTED`, `WRITE_VERIFY_FAILED`, `VIEWER_P_ROUNDED`, `UNSUPPORTED_FIELD`, `UNKNOWN_FIELD`, `UNKNOWN_VALUE` |
| v1.2 guards and coverage | `AMBIGUOUS_REQUEST`, `TOO_MANY_IDS`, `NOT_SUBSET`, `FEWER_THAN_N`, `TIES_AT_CUTOFF`, `PANEL_COVERAGE`, `STAT_UNAVAILABLE`, `REQUEST_TOO_LARGE`, `CALL_BUDGET`, `KG_NOT_CONNECTED` |
| Rejections | `INVALID_INPUT`, `NOT_IMPLEMENTED`, `RUN_BUSY`, `RUN_NOT_FOUND`, `STALE_ACTION` |
| Controller | `INTERNAL_ERROR` |

A rejection writes no record; with valid ids its stream is `RUN_STARTED` then that `RUN_ERROR`
(invalid ids get a lone `RUN_ERROR INVALID_INPUT`). Every other `RUN_ERROR` code means the run
ended Failed (`PLANNER_DECLINED`, `WRITE_VERIFY_FAILED`, `INTERNAL_ERROR`).

## Run record and the view route

`runs/<runId>.json` (contract "Run record") holds the ask as received, `plannerOutput`,
`planVersions` (compiled plans with step arguments), `planHistory` (cause, IR and edit of each
version), `modelCalls`, `steps`, `calls` (every attempt with request and response hashes, full
outputs and provenance), `facts` (with callId, hashes and engine), `cohorts` (with definitions),
`pending`, `confirmations`, `vizReports` (the latest per viz), `actions` (accepted, repeat,
rejected), `ledger`, `answer`, `superseded` and `runView`. It is rewritten on every transition with
a rising `revision`; reports and action-log entries that change no view field keep the revision.
The snapshot is the record's view fitted to 32 KB (`events.fit_view`); the record never trims.
