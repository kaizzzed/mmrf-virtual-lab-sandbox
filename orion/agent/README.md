# ORION agent (run controller and engine)

New to ORION? Start with [ORION_QUICKSTART.md](../../ORION_QUICKSTART.md).

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
| `orion_agent/executor.py` | The step engine (`Engine`, `interfaces.Executor`): StepRef resolution, pre- and post-guards (`tools/guards.py`), the call budget, Stop, the save pause and its read-back check; the executor functions (`tools/functions.py`) and every attempt's CallRecord (`tools/records.py`); step reuse by the compiler's input hashes; the plan's run cohorts |
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
| `orion_agent/model/` | The planner, composer and verifier (`factory.py`: `make_planner`, `make_composer`, `make_verifier`, by `Settings.planner` and `Settings.composer`), the one Bedrock client (`client.py`: AsyncAnthropicBedrock on bedrock-runtime with the `us.anthropic.*` inference profiles by default, refusal fallback and per-attempt records), the structured-output schemas in the wire encoding Bedrock compiles (`schema.py`), the model call scope, the answer writer and warm-up |
| `orion_agent/stubs.py` | The Day 1 stub planner and executor (`ORION_PLANNER=stub` through the model factory; `ORION_EXECUTOR=stub`) |
| `tests/` | `unittest` suites. `test_engine_*.py` drive the controller and engine through the real AGUIApp with fakes at the edges (`test_engine_fakes.py`); `test_e2e_live.py` drives the mode L app (`build_controller`) against the live stack; `test_integration.py` holds the integration regressions |

## Build

The host Python is unsuitable, so everything runs in containers. The image builds natively for
arm64 on Apple silicon, which AgentCore Runtime requires; on an amd64 host use
`docker buildx build --platform linux/arm64`.

```sh
export PATH=$HOME/.docker/bin:$PATH
docker build -t orion-agent:dev orion/agent
```

The Dockerfile is multi-stage and the image runs as uid 10001 (`orion`); the dependency pins are
`requirements.txt` (the Bedrock SDK included, though mode L makes no model call). A plain build
carries the golden request bodies as `orion_agent/tools/golden_snapshot.json`, which the tools read
when `ORION_GOLDEN_DIR` (`/golden`) is absent; the Compose override and the AgentCore build bake
`/golden` and `/config/cohort-fields.json` from the named build contexts `golden` and
`sandbox-config` (see the Dockerfile). `python -m orion_agent.tools.goldens check /golden` exits 1
when the snapshot and `orion/golden` differ.

## Test

From the sandbox repo root, with the local stack up (the live vocabulary and tool tests skip
without it). The tests run in the runtime image as its non-root user; the golden mount lets the
five golden-file tests run (they skip without it):

```sh
docker run --rm --network mmrf-synthetic_default \
  -v $PWD/orion/agent:/app -v $PWD/config:/config:ro -v $PWD/orion/contracts:/contracts:ro \
  -v $PWD/orion/golden:/golden:ro \
  -v $PWD/.sources/frontend/config/gen3/analysisTools.json:/analysisTools.json:ro \
  -e ORION_VLAB_ORIGIN=http://identity:8080 -w /app orion-agent:dev \
  python -m unittest discover -s tests -v
```

The engine suites compare each journey's stream with its fixture's shape (`test_engine_fakes.shape`:
events in order, statuses, step rows, fact ids, Limitations codes, pauses, cohorts): J1 to Complete,
J1 Partial then `retry_step`, J1 Stop, J3 to the save pause, Create, Don't save, Rename at the
pause and the DE fallback; `test_contract.py` also checks the J2 fixtures' shapes against the same
wiring.

`test_e2e_live.py` is mode L end to end: `app.build_controller` from Settings (stub planner, step
engine over the InProcessToolClient, stub composer, code verifier) driven through
`POST /invocations` against the live stack. J1 (both phrasings), J2 and J3 run to Complete (J2 and
J3 through their save pause and a simulated browser's Create, J3's DE on the read-back names), plus
Don't save, Stop during a step, an edit at the save pause, a resent confirm, `retry_step`, clarify,
consent and Unsupported. Each stream passes `support.check_stream` and keeps its fixture's shape,
the facts equal the stack's own answers (raw HTTP in the test, apart from the tool code), and the
run record is read back from disk. It skips when `ORION_VLAB_ORIGIN` does not answer. Alone:

```sh
docker run --rm --network mmrf-synthetic_default -v $PWD/orion/agent:/app -v $PWD/config:/config:ro \
  -v $PWD/orion/contracts:/contracts:ro -e PYTHONPATH=/app/tests:/app -e ORION_VLAB_ORIGIN=http://identity:8080 \
  -w /app/tests orion-agent:dev python -m unittest test_e2e_live -v
```

`test_bedrock_live.py` is stage 2 live and opt-in: skipped unless `ORION_LIVE_BEDROCK=1`, so the
suite above makes no model call. With short-term credentials it calls Bedrock: one planner call on
J1 (its plan must compile to the stub planner's signature), the composer, verifier and one
re-compose on the recorded J1 Observed (must end Complete), each role's warm-up, and J1 to
Complete through `POST /invocations`. `ORION_LIVE_BEDROCK_JOURNEYS=all` adds J1's second
phrasing, and J2 and J3 in both phrasings through Create. Turns go to an in-process app, or to the
Compose agent with `ORION_LIVE_AGENT_URL` and `ORION_LIVE_RUN_DIR`; the module docstring has the
command. Credentials go to `docker run` by name (`-e AWS_ACCESS_KEY_ID`), never by value.

Type checking is not part of the image. The image's virtualenv belongs to root, so install
mypy in a throwaway container as root:

```sh
docker run --rm -u 0 -v $PWD/orion/agent:/app -w /app -e MYPYPATH=tests orion-agent:dev \
  sh -c 'pip install -q mypy==1.18.2 && mypy --strict --cache-dir=/tmp/mypy orion_agent tests'
```

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
URL; `ORION_BEDROCK_CLIENT`, `ORION_BEDROCK_REGION`, `ORION_PLANNER_MODEL`, `ORION_COMPOSER_MODEL`,
`ORION_FALLBACK_MODEL` and the effort, timeout and token settings in `model/config.py`); see their
modules. The role kinds come from `ORION_PLANNER` and `ORION_COMPOSER` through Settings. The Bedrock
defaults are the bedrock-runtime inference profiles `us.anthropic.claude-opus-5-5` (planner and
composer) and `us.anthropic.claude-opus-5` (refusal fallback) in `us-east-1`; the base ids fail on
demand there. `ORION_TOOLS` is the only tool-transport switch: `ORION_TOOL_TRANSPORT` is read by
nothing (the Compose override no longer sets it; Terraform still does, harmlessly).

## Stage 2: the planner and composer on Bedrock

The agent still runs locally in Compose with in-process tools; only the two model roles change.
From the sandbox root:

```sh
orion/scripts/orion-local.sh up --bedrock    # the real planner and composer
orion/scripts/orion-local.sh status          # "planner: bedrock, composer: bedrock"
orion/scripts/orion-local.sh up --stub       # back to the stubs
```

`up --bedrock` reads the short-term credentials of the AWS profile `sandbox` (`--profile NAME`)
with `aws configure export-credentials`, never printing them, checks them with one
`sts get-caller-identity` (it prints only the role name), sets `ORION_PLANNER=bedrock`,
`ORION_COMPOSER=bedrock`, `AWS_REGION` and `ORION_BEDROCK_REGION` to `us-east-1`, and rebuilds and
recreates only `orion-agent`; the frontend keeps running. `up --stub` recreates the agent with no
AWS variable at all. The stub stays the Compose default. The credentials expire; after that every
model call fails (the planner's failure ends the run Failed `INTERNAL_ERROR`, the composer's leaves
the answer Observed only) until the profile is refreshed and `up --bedrock` runs again. Bedrock
calls cost money.

What the roles send (`model/client.py`, the same client in mode A, where AgentCore supplies the
credentials): `client.beta.messages.create` on bedrock-runtime (InvokeModel) with
`us.anthropic.claude-opus-5-5`, one cached system block (`cache_control` ephemeral, TTL 5m or 1h),
one user message, and `output_config` with the role's effort (`low`, `medium`) and a JSON schema.
The SDK sends `anthropic_version` 2026-10-21 and the refusal fallback middleware's beta
`fallback-credit-2026-07-01` in the body; Bedrock accepts both, and the fallback model
`us.anthropic.claude-opus-5` takes the same request. Bedrock answers with the base model name
(`claude-opus-5-5`), so the ModelCallRecords take the model from the request: the inference
profile that was called. A refusal whose fallback call then fails is still a decline
(`PLANNER_DECLINED`, or Observed only with `MODEL_DECLINED` for the composer), and the run's pin
to the fallback is lifted. The fallback runs inside one of the SDK's attempts, so it shares the
retry budget: a planner call takes at most 3 × 30 s with the defaults (`model/config.py`).

Bedrock refuses the IR's schema as Pydantic writes it ("The compiled grammar is too large"), so
`model/schema.py` sends a wire encoding: definitions expanded in place, each tagged union of
objects flattened into one object whose `kind` is an enum, and every property required, with an
empty value (`""`, `0`, `[]`, or a list of at most one object) where a kind leaves it unset. Every
empty value is an output token, and planner latency is about 1.4 s plus 10 ms per output token, so
the planner's `PLANNER_POLICY` departs from that where the grammar can afford it: PlannerOutput's
three kinds and an analysis's four stay small unions (each kind with only its own fields), and a
cohort definition's seven fields and a cohort's name are optional (8 optional properties; the
cohort definition kept as a union is refused, and 21 optional properties take about 25 s to
compile). J3's plan shrinks from about 400 output tokens to about 240, and its plan-visible time
from about 5.6 s to about 4 s. The planner's prompt shows its examples in that encoding, and the
reply is decoded back to the IR before the full IR validation, which still enforces every
constraint and refuses a field of another kind or a missing one. `tests/test_model_config.py` pins
both schemas' grammar profiles to what Bedrock compiled and keeps J3's wire plan short.

Two rules the stub never exercised were sharpened for the real models. The planner's prompt says
that a risk word without its classification (CGS, ISS) is ambiguous and asks once (FR-04, E10),
and that a follow-up does not inherit an earlier answer's scope (FR-19, E15). The verifier's
allow-list keeps a vocabulary term that contains a digit ("t(4;14): Present") only when the run
itself names it (its scope, a cohort name or a plan setting), as the contract's "Numbers on screen"
allows; any other such term in model text is re-composed, then dropped (V-NUM).

No few-shot example gives an eval away (C-31). `model/eval_prompts.py` lists every eval,
reference and fixture prompt, the held-out paraphrases and out-of-kind questions included, and
`tests/test_model_prompts.py` refuses an example that equals or nearly equals one, holds one with a
word or two changed (runs of n − 1 to n + 2 of its words, at least 4), or touches the topic of an
out-of-kind or Venn question (clustering and heatmaps, protein domains, UMAP and other embeddings,
set overlaps), and keeps Unsupported examples off those evals' apps. `orion-prompts@5` replaced the
two stage 1 examples that this caught: a clustering question (E23's topic) and E12's question with
one word inserted. V-SEC reads a regimen that the run itself uses (a regimen-defined cohort) as a
name only where the text uses it as the name of a group ("the <regimen> (ORION) cohort", "cases
receiving <regimen>", "in <regimen>", "<regimen> and the remaining cases"), never as the answer
("It is <regimen>"), alone, or as the bare object of a verb ("Consider <regimen> and …"); any other
mention is checked as in a run that does not use it. A sentence that names it, every model text
of such a run, and any text with a treatment noun ("regimen") may use only the plain descriptive
words in `lexicon.DESCRIPTIVE_WORDS` (sizes, members, and comparisons of what the run measured
with a named group), so a benefit, outcome or advice claim fails whatever its wording ("may
thrive", "may outlive the others", "Begin … on <regimen>"); the verifier also reads the frames
that descriptive words can still build (the regimen offered to or tried on a group, cases placed
in its group, a survival comparison with no comparison group). Regimen short names ("VRd"),
brand names and care words ("maintenance") are clinical words, and the rules read text as a
reader sees it (no zero-width characters or look-alike letters). The guard's topics also cover
E12's question (the size of "my cohort") and E17's (a drug for a patient with a named mutation),
and their common rewordings.

**The V-SEC fix of the third review.** The second review's name rule let through advice that
bb7c6e0 rejected, such as "Try <regimen> and see whether the mutated cases respond" and
"Consider <regimen> (ORION) for these cases". It also let through benefit claims about a
described group ("Patients receiving <regimen> may live longer"). With the descriptive-word rule
above, all 52 of the review's probe cases fail, and so do about 130 later rewordings. Tests added:
`tests/test_verifier.py` `VSecRegimenTextTest` (131 bypasses, 87 other advice cases, 47
descriptive texts that pass, 17 borderline texts that stay failing), 3 more V-SEC tests and 2
C-31 guard tests, so the suite went from 753 to 764 tests. `VSecRegimenTextTest` fails 171 times
on bb7c6e0's verifier.

The composer's prompt names V-SEC's word families with their inflections (`CAUSAL_WORDS`,
`CLINICAL_WORDS`: "driven", "respond to" were the forms that cost re-composes) and says that the gene
that defines a cohort appears only inside the cohort's whole name ("KRAS mutated (ORION)", never
"KRAS-mutated cases"); a third example shows it. `model/answer.py` logs the failed rules of every
re-compose, since the run record keeps no verifier finding. A follow-up chip is asked as a new
question, so rule 9 (`orion-prompts@4`) makes each chip stand alone: a risk group names its
classification ("CGS high-risk cases"). Before that rule, every J1 answer's chip read "High risk
cases", which the planner's rule 6 must clarify (CGS risk category or ISS stage), so a click on a
chip of a CGS answer asked "Which field defines high risk?".

The browser end to end runs against this agent too:
`ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs` keeps every structural check of the
stub run, checks the model's text (verified, citing facts, equal to the record) where the stub run
checks its wording, and adds Stop while planning and while writing, and a plan-card edit that
re-composes. Results, browser timings and limits: `orion/notes/bedrock-status.md`.

Measured on 2026-11-06 through the Compose agent (us-east-1), `orion/scripts/evals-bedrock.sh
evals`, 5 repeats, with `orion-prompts@5` and the current verifier, the third review's fix
included (`orion-evals-20261106T010531Z`). All 24 evals pass, E22's held-out paraphrases
included, and all nine gates pass. The plan-visible p95 was 4.55 s over 94 asks against the 5 s
**Proposal** (it was 5.71 s before the compact planner schema). None of the 89 answers was
re-composed, and no sentence was dropped. The browser end to end passes 15 of 15 scenarios
against the same build (`summary-bedrock-20261106T014241Z.json`).

That gate depends on Bedrock's latency in the window. Over five 5-repeat runs of practically the
same planner requests it read:

- 4.52 s (pass, `@4`, before the first review round);
- 5.96 s (fail, `@4`, after it);
- 7.67 s over 78 asks (fail, `@5`, a run that also met a Bedrock connection outage and failed E20
  and E22);
- 4.64 s (pass, `@5`, the second review's code);
- 4.55 s (pass, the final code).

How NFR-01 is measured or met is still for the team to decide.

Planner calls take about 1.4 s plus 10 ms per output token at effort `low` (12.1K cached prefix
tokens; J3's plan is about 240 tokens and 4 s, J1's about 120 and 2.7 s). Composer calls take
about 8 s for J2, about 8 or 14 s for J1 (adaptive thinking is bimodal there), and about 20 s for
J3 at effort `medium` (20.5 s at p50 over 25 calls in the final run; one earlier run's 11.6 s was
not reproduced). Re-composes were 0 of 89 answers in the final run and 4 of 89 in the second
review's `@5` run, all of those for V-NUM number words or a malformed placeholder (19 of 88 before
the composer prompt change). A cold prompt cache adds nothing measurable to the plan time. A
question asked while the Assistant's opening warm-up turn runs waits for it, so its plan shows
after 7 to 11 s in the browser. The evals do not measure that path
(`orion/notes/bedrock-status.md`, Known limit 1). Timings warm and cold, call counts and tokens:
`orion/notes/bedrock-evals.md` ("Final proof on the final code" for these runs).

## Mode A: on AgentCore Runtime

The same image runs as the AgentCore Runtime `orion_agent`. It is built for arm64 with the named
contexts and deployed by `orion/infra` (`../infra/README.md`). The Runtime sets `ORION_MODE=A`,
records in S3 and `ORION_TOOLS=gateway`. Each thread is its own Runtime session
(`orion-<threadId>`). The deployed version is 4, image `97926b08c368` (stage 3's commits
rebased onto `orion/bedrock` `bf0a8b7`, the treatment-run guard; the stage 4 browser E2E passed 17
of 17 scenarios on it, and the evals were not rerun). The results below ran on version 3, image
`4609db670295`.

Two Gateway rules came from the first live calls:

- **Null-valued arguments are left out.** The Gateway's tool schema cannot say "or null", and it
  refused an explicit null. Every nullable Input field defaults to None, and the recorded request
  keeps its nulls.
- **Credentials resolve once.** The SigV4 signer resolves its credentials once and signs with the
  same refreshable credentials. A signing failure is a retryable `HTTP_ERROR`.

Results: `orion/notes/mode-a-status.md`. In brief, on version 3:

- the browser end to end passes 15 of 15 scenarios with 118 checks;
- the evals give 22 pass, with E11 and E20's lease expiry not run (they need a test Runtime);
- the plan p95 gate fails at 8.08 s, mostly on Bedrock planner time.

Teardown: `orion/notes/mode-a-teardown.md`.

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
  (status Answering); then the composer and the verifier run (`orion_agent.model.answer`: one
  re-compose after a first failure, failing sentences dropped). Complete carries all four parts,
  its Open links the compiler's hand-offs (`CompiledPlan.open_apps`) and then the composer's;
  ObservedOnly carries `withheldReason`. The TEXT_MESSAGE follows the last snapshot.
- **Stop.** Stopping at once; the in-flight planner, tool or composer call is aborted and recorded
  (outcome `cancelled`, or a CallRecord whose error starts `CANCELLED` and whose provenance keeps
  the request it had in flight); open and queued steps are Skipped "Not run: stopped"; Cancelled
  with Observed for the finished steps.
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
