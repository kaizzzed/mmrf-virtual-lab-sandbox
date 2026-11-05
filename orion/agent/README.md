# ORION agent (run controller)

The ORION Workstream 2 run controller: plain Python on the `bedrock-agentcore` SDK's `AGUIApp`
(RFA rev 25, A2 and A3). It serves `POST /invocations` (AG-UI events over SSE), `GET /ping`
and `/ws` on port 8080, and speaks the wire contract in `../contracts/README.md`.

Day 1 AM scope: the controller (detached run task, lease, run store), the planner IR, and the
compiler rules C-SCOPE, C-VOCAB, C-PANEL and C-UNTESTED. The planner and executor are stubs.

## Layout

| Path | What it holds |
| --- | --- |
| `orion_agent/app.py` | `AGUIApp` wiring: the entrypoint (second parameter named `context`) and a synchronous ping |
| `orion_agent/controller.py` | Action dispatch, the detached run task, the run registry, cancel, heartbeats |
| `orion_agent/run_store.py` | Run record (`orion.run-record/v1`), thread index, atomic writes, the single writer per run |
| `orion_agent/lease.py` | Lease creation, expiry and the renewal loop |
| `orion_agent/models.py` | The wire contract as Pydantic models: RunView, 15 RunStatus, 9 StepStatus, 22 codes, every frame, the request actions, pinned viz inputs and `pinned_hash` |
| `orion_agent/events.py` | AG-UI event builders and the 32 KB snapshot trimming |
| `orion_agent/ir.py` | The planner IR (`PlannerOutput = Plan \| Clarify \| Unsupported`) and its structured-output schema |
| `orion_agent/catalog.py` | The 15 catalog apps, their R3 role, named tools, row reasons |
| `orion_agent/vocab.py`, `vocab_supplement.json` | C-VOCAB: fields, values, operators, Elasticsearch-only fields, ProteinPaint twins |
| `orion_agent/compiler.py` | IR to signed plan |
| `orion_agent/stubs.py` | Day 1 AM planner and executor stubs |
| `orion_agent/settings.py` | Environment settings (added to keep env parsing out of the controller) |
| `tests/` | `unittest` suites. They import third-party packages, so they stay out of the repo's stdlib-only `tests/` |

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
  -e ORION_VLAB_ORIGIN=http://identity:8080 -w /app orion-agent:dev \
  python -m unittest discover -s tests -v
```

Add `-v $PWD/.sources/frontend/config/gen3/analysisTools.json:/analysisTools.json:ro` to run the
catalog check against the fork. Type checking is not part of the image; it was run with
`pip install mypy==1.18.2 && MYPYPATH=tests mypy --strict orion_agent tests` in a throwaway container.

## Run in mode L

```sh
docker run -d --rm --name orion-agent --network mmrf-synthetic_default -p 127.0.0.1:8081:8080 \
  -v $PWD/config:/config:ro -v $PWD/orion/agent/.orion-runs:/data/runs orion-agent:dev
curl -s http://127.0.0.1:8081/ping
curl -sN http://127.0.0.1:8081/invocations -H 'content-type: application/json' -d @ask.json
```

Publish the port on 127.0.0.1 only. `.dev/*.sse` holds real streams captured this way, one per contract outcome (for example `ask-j1-all-cases.sse`, `ask-busy.sse`, `cancel-orphan-interrupted.sse`); the frontend's opt-in `liveStream.unit.test.ts` replays them when `ORION_LIVE_SSE_DIR` points at that folder.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ORION_RUN_DIR` | `/data/runs` | Run records (`runs/<runId>.json`) and thread indexes (`threads/<threadId>.json`) |
| `ORION_FIELDS_PATH` | `/config/cohort-fields.json` | The sandbox vocabulary, read-only; the app will not start without it |
| `ORION_PLANNER` | `stub` | Only `stub` until Day 1 PM |
| `ORION_EXECUTOR` | `stub` | Only `stub` until Day 1 PM |
| `ORION_DEFAULT_SCOPE` | `active_cohort` | Scope when the question names none (Open question 1), recorded as an assumption; or `all_cases` |
| `ORION_VLAB_ORIGIN` | `http://identity:8080` | The gateway the tools will call; today only the live tests use it |
| `ORION_HEARTBEAT_S` | `15` | `CUSTOM orion.heartbeat` cadence while an invocation streams |
| `ORION_LEASE_RENEW_S` | `30` | Lease renewal cadence while Planning, Executing or Answering |
| `ORION_LEASE_TTL_S` | `180` | A lease expires this long after its last renewal |

## Stubbed until Day 1 PM

- **Planner.** A question that mentions a Venn diagram ends Unsupported with
  `UNSUPPORTED_ANALYSIS` and Set Operations as the nearest app (design eval E18). Otherwise a
  question that mentions survival gets the J1 IR (survival split by CGS risk category, no
  `base_cohort`), and anything else ends Unsupported with `UNSUPPORTED_ANALYSIS` and no app.
- **Executor.** It makes no data call and invents no number: every step ends Skipped with
  `NOT_IMPLEMENTED`, and the run ends Limited with a limitation that says the executor lands Day 1 PM.
  It builds no viz spec, and it does not yet replace `plan.scope.caseCount` with the count
  observed by the step that sizes the scope (the fingerprint for all cases, `cases_query` for
  a filtered scope), so the plan keeps the browser's count or null.
- **Compiler.** Only survival split by a categorical field and cohort sizes compile to steps.
  Top mutated genes, differential expression and survival by cohort groups stop with
  `NOT_IMPLEMENTED`, naming the app that runs that analysis in `nearestApps`; a Clarify output
  stops with `NOT_IMPLEMENTED` too. C-CASEIDS, C-SPLIT, C-SAVE, C-FIT, C-GUARD and
  C-DEFAULTS are not applied yet. The session ledger (Day 2) is an optional argument that the
  controller does not pass yet.
- **Actions.** `clarify_answer`, `consent`, `confirm`, `edit_plan`, `retry_step` and `viz_report`
  return `RUN_ERROR NOT_IMPLEMENTED`.
- **Storage.** Local volume only; S3 comes with modes A and H.

## Run record and the view route

`runs/<runId>.json` holds `schema`, `threadId`, `runId`, `revision`, `status`, `code`, `question`,
`lease`, `request`, `plannerOutput`, `planVersions` (compiled plans with step arguments),
`steps`, `calls`, `facts` and `answer`. The RunView a snapshot carries is the record with
`plan` = the last `planVersions` entry without `steps`, `limitations` and `scope.clauses`, and `observed`,
`limitations`, `interpretation`, `hypotheses`, `withheldReason` and `vizSpecs` from `answer`.
Snapshots trim long step summaries to stay under 32 KB; the record keeps the full text.

## Contract behaviour worth knowing

- **Rejections.** `INVALID_INPUT`, `RUN_BUSY`, `RUN_NOT_FOUND` and `NOT_IMPLEMENTED` reject the
  invocation before it touches the store: no run record or thread index is created or changed.
  With valid ids the stream is `RUN_STARTED` then that `RUN_ERROR`. Invalid thread or run ids get
  a lone `RUN_ERROR INVALID_INPUT` with no `RUN_STARTED`, because a `RUN_STARTED` carrying those
  ids would fail the contract's own frame schema. Every other `RUN_ERROR` code means the run ended Failed.
- `RUN_STARTED` carries only `threadId` and `runId`, as the contract lists; it does not declare
  `protocolVersion`.
- A `cancel` invocation joins the running run's stream. It sees `Stopping` at once, then the
  run's own snapshots and `RUN_FINISHED Cancelled`. It drops a `STEP_FINISHED` whose start it did
  not see; the snapshot carries that row.
- **Interrupted.** `cancel` of a run whose controller died (expired lease) records it
  Interrupted, sends its snapshot right after `RUN_STARTED` and ends with `RUN_FINISHED`
  `{"status": "Interrupted", "code": "RUN_INTERRUPTED"}`. A later `cancel` of that run gets the
  same snapshot and `RUN_FINISHED` and changes nothing. An `ask` that finds the thread's active run with an
  expired lease records that run Interrupted, sends its snapshot right after `RUN_STARTED`, and
  then streams the new run; readers apply each snapshot by `snapshot.runId`.
- **Scope.** C-SCOPE copies `caseCount` from `cohortContext` (null means "count pending") and
  names an unfiltered active cohort "All cases", whatever the browser calls it.
- **Limitations** always serialize `nearestApps` (`[{appId, title}]`, titles from the catalog).
  An Unsupported limitation lists the planner's `nearest_app_ids` there and leaves `unblock`
  null, since the card already offers Open on this cohort for the first app; the others are
  empty except the compiler's stubbed analyses.
- **Viz specs** carry `pinned` (a union on `kind`: `survival`, `oncomatrix`, `deDirect`,
  `deInput`) and validate that `launcher` equals `pinned.kind` and that `pinnedHash` equals
  `models.pinned_hash(pinned)`. The hash is the sha256 of the contract's canonical JSON; it
  raises `PinnedNumberError` for a decimal outside 1e-4 ≤ |x| < 1e15 or an integer beyond
  2^53 − 1, and prints an integral float as an integer, as JavaScript does. An absent optional
  field (`volcano` and each of its fields) is omitted; the models refuse one written as null,
  because the browser would hash that null.
  `tests/test_contract.py` reproduces every vector in `../contracts/fixtures/viz-pinned-vectors.json`.
- An `ask` question is capped at 4,000 characters, and a reused `runId` is `INVALID_INPUT`.
