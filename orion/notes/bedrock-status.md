# ORION stage 2 (Bedrock only): status

As of 2026-11-05. The sandbox is on `orion/bedrock`, stacked on `orion/mode-l`, at 1742abf plus the
working tree; nothing is committed. The frontend fork is unchanged at 3fc0e6a. The agent version is
0.2.0, with prompts `orion-prompts@4`. Stage 2 means:

- the agent still runs locally in Compose (mode L), with in-process tools against
  `http://identity:8080` and run records on the volume `mmrf-synthetic_orion-runs`;
- the planner and the composer are Claude Opus 5.5 on Amazon Bedrock;
- the models are the inference profiles `us.anthropic.claude-opus-5-5`, with refusal fallback
  `us.anthropic.claude-opus-5`, in `us-east-1`;
- the client is `AsyncAnthropicBedrock` on bedrock-runtime, as the role `WSParticipantRole`;
- the planner runs at effort `low` and the composer at effort `medium`, with a cached system
  prefix and the 5-minute TTL.

The stubs stay the Compose default. Bedrock is opt-in. All data is synthetic.

**Verdict: stage 2 works end to end in the real Assistant app.** Three checks back this:

- **Browser end to end, Bedrock mode:** 15 of 15 scenarios and 118 checks pass in headless
  Chromium. That covers the 11 stage 1 scenarios, plus four that only real models make
  meaningful.
- **Browser end to end, stub mode:** the original 11 of 11 scenarios and 72 checks still pass,
  both with this script and with the stage 1 script as committed.
- **Evals:** after this step's composer change, the Bedrock evals pass 24 of 24 with all nine
  gates at 5 repeats (`orion-evals-20261105T170844Z`, plan p95 4.52 s over 94 asks).

One defect turned up and is fixed: the composer's follow-up chips needed a clarifying question. One
gap is measured and left for a decision: the first question in a fresh tab waits for the warm-up
turn.

**These Bedrock results predate the review round** (below). That round changed the model client,
the verifier and the request encoding in `model/prompts.py`, and no Bedrock run has been made
since. The final verification found the profile's credentials expired, so the Bedrock evals, the
timings and the Bedrock E2E are still to be rerun (see "Final verification").

## Starting and stopping Bedrock mode

From the sandbox root, with the AWS profile `sandbox` signed in:

```sh
orion/scripts/orion-local.sh up --bedrock    # rebuild and recreate only orion-agent on Bedrock
orion/scripts/orion-local.sh status          # "planner: bedrock, composer: bedrock"
orion/scripts/orion-local.sh up --stub       # back to the stubs: recreate only orion-agent, no AWS variable
```

What `up --bedrock` does:

1. It reads the profile's short-term credentials with `aws configure export-credentials` and
   never prints them.
2. It checks them with one `sts get-caller-identity`, which prints only the role name.
3. It sets `ORION_PLANNER=bedrock`, `ORION_COMPOSER=bedrock`, `AWS_REGION=us-east-1` and
   `ORION_BEDROCK_REGION=us-east-1`.
4. It rebuilds and recreates only `orion-agent`. `--no-build` reuses the image. The frontend and
   the rest of the stack keep running.

`up --stub` clears every AWS and Bedrock variable, whatever the shell exports.

The manual equivalent, which never echoes the credentials:

```sh
export PATH=$HOME/.docker/bin:$PATH
eval "$(aws configure export-credentials --profile sandbox --format env)"
export AWS_REGION=us-east-1 ORION_BEDROCK_REGION=us-east-1 ORION_PLANNER=bedrock ORION_COMPOSER=bedrock
docker compose -f compose.yaml -f orion/compose.orion.yaml up -d --no-deps --build orion-agent
```

To go back by hand, unset `ORION_PLANNER`, `ORION_COMPOSER` and the `AWS_*` variables, then run the
same `up`. Never run `compose config` without `--no-interpolate`: it would print the exported
secrets. `orion-local.sh config` passes that flag.

The credentials expire. The agent ran in Bedrock mode from 17:06 UTC on 2026-11-05. For the review
round below it was rebuilt with `up --stub` at about 18:18 UTC, and the final verification
recreated it in stub mode at 18:23 UTC, so it now runs the stubs and holds no AWS credential;
`up --bedrock` starts Bedrock mode again. With Bedrock, once the credentials
expire, every model call fails:

- a planner failure ends the run Failed;
- a composer failure leaves the answer Observed only, "Interpretation withheld: …".

Refresh the profile and run `up --bedrock` again. `orion/scripts/evals-bedrock.sh check` tests the
credentials and the agent's roles without a model call. Every Bedrock call costs money.

## What works with the real models, as the browser sees it

Each row is a scenario of `orion/scripts/e2e-mode-l.mjs` run with `ORION_E2E_MODEL=bedrock`, in a
fresh browser profile. Each one also checks that the page raised no error. The stage 1 table in
`mode-l-status.md` describes the cards and words. Only the model's part differs here.

| Scenario | With the Bedrock planner and composer |
| --- | --- |
| `j1-open`, `j1-named` | J1 in both phrasings. The plan's signature equals the stub planner's (`c0fd0272…`) and its rows are the same. The steps run live; the survival card mounts live with parity; the answer has its four parts in order; Observed equals the record. **Model text:** the Interpretation has 2 to 4 sentences and Hypotheses 2 or 3; every sentence cites facts of this run, and each cited number shows with its chip. No digit appears outside a chip except the contract's own terms (the evals' cited-numbers rule), and the screen equals the record. The record names the bedrock roles, and every ModelCallRecord is on `us.anthropic.claude-opus-5-5`. "Writing interpretation" shows with Observed and Limitations, before the Interpretation. The Evidence Inspector works as in stage 1. **The first follow-up chip** (the composer's own, such as "Top mutations in CGS high-risk cases") plans with no clarifying question and runs to Complete, through the save pause when its cohort is new |
| `j2` | J2: signature `8f75d253…`, the confirmation card with focus, no default button, Create, the cohort list, the live OncoMatrix card, the model text, the hand-off and Back. The composer's first chip runs to Complete. Cohort Builder then shows the saved cohort with 334 cases |
| `j3` | J3: signature `3841cfff…`, eligible counts on the card before Create, both cohorts saved, the live DE card, "server cache: same entry as s9 de_run", and the model text |
| `edit-stop`, `dont-save`, `e21`, `restore` | As in stage 1, with the model's answer checked on each Complete. In `restore`, the reload lands while the composer writes. The reopened thread follows the run to Complete. The restored page's warm-up turn meets "session busy" (409) until the run ends, and then goes through |
| `follow` | The Top N edit at the save pause, then E14: J1 in the active race cohort signs the stub's plan (`97e8fd30…`), with groups 98 and 236. The composer's chip runs as the thread's second run. Switching threads works |
| `edges` | E7's consent card (Cancel: no data call). E10: one question with two field options, then Complete with model text. E8 is Limited and E17 Unsupported, with no call and no numbers. Every record shows exactly one planner call, or two for E10 (the question, then the plan) |
| `e18` | Unsupported, "Nearest app: Set Operations", Open on this cohort, Back |
| `writing` (new) | J1 typed after the warm-up. The "Planning" placeholder card shows with Stop, then the plan, then "Writing interpretation". At that point the answer card holds Observed and Limitations only, steps s2–s5 are Done, Stop is live, and focus stays in the composer. Then all four parts appear. Observed does not change when the Interpretation arrives. The live region says "Writing interpretation." and then "Answer ready: Complete." |
| `stop-writing` (new) | Stop while "Writing interpretation" shows. "Stopping" shows, then "Stopped by you". Observed and Limitations stay unchanged, the answer reads "Interpretation withheld: you stopped the run", and there are no chips. s2–s5 stay Done. The live region says "Answer ready: Stopped by you." and Send comes back. The record is Cancelled, with no Interpretation, and the composer call `v1.composer.c1` is recorded as `cancelled` |
| `stop-planning` (new) | Stop about one second into "Planning". "Stopping", then "Stopped by you". The plan card reads "No plan was made before the run ended." and there is no answer card. The record is Cancelled, with no plan version and no tool call, and the planner call is recorded as `cancelled` |
| `recompose` (new) | J2 to Complete, then **Edit settings**, Top N 5, **Apply**, with the run at rest. Plan version 2 ("Replaces version 1"): s2 and the save are "Reused · from version 1", and no second save card appears. "Writing interpretation" shows again, and a new composer call (`v2.composer.c1`) writes the version 2 answer. The answer says it replaces the version 1 answer. The record has one planner call (the edit made none), composer calls for both versions, five ranked genes and one confirmation. It keeps the version 1 answer as superseded |

## The browser end to end in both modes

```sh
node orion/scripts/e2e-mode-l.mjs                            # stub agent (the default)
ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs    # Bedrock agent
ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs j1 writing    # a subset
```

`ORION_E2E_MODEL` must match the running agent. A mismatch fails the first record check, and the
message names both.

**What stays the same.** Both modes run every structural check: screen words, plan rows, cards,
chips, the four parts in order, record contents, no write before Create, focus, and the live
region. Observed and Limitations are code-written, so their wording is checked in both modes.

**What both modes gained:**

- The plan check compares the plan's signature with the stub planner's, for J1, J2, J3 and E14
  (NFR-04 in the browser). The signature check is folded into the existing plan check, so the stub
  run still counts 72 checks.
- A failure names the script line, since an `expect` message is lost outside the Playwright
  runner.
- ProteinPaint's teardown message, the bare console error "Failed to fetch" logged while the
  scenario's own reload or Back tears the old page down, is kept apart as `unloadErrors` in
  `summary.json`. Any other console error in that window still fails the scenario.
- `summary.json` records the model mode and the browser's timings. Each run also writes
  `summary-<model>-<UTC stamp>.json`, which no later run deletes. Before this, only the one
  `summary.json` existed, and every run replaced it.

**What Bedrock mode changes.** Each check of the stub composer's wording is replaced:

- **Its follow-up chip labels, and the plans those chips make.** The first chip of the answer must
  be the record's own, with its question in its title. It must plan without a clarifying question
  and run to Complete. A save pause on the way is answered with Create.
- **Its text.** The model's text must exist, pass the verifier, cite facts of the run, show one
  chip per cited value, and equal the record.

Each Complete run also checks that "Writing interpretation" came after Observed and before the
Interpretation. Waits are longer (`RUN_TIMEOUT` 180 s), and the four new scenarios run. In stub
mode those four are listed as skipped.

| Run | Agent | Result |
| --- | --- | --- |
| Stage 1 script as committed (1742abf) | stub | 11 of 11 scenarios, 72 checks |
| This script, `ORION_E2E_MODEL` unset | stub | 11 of 11 scenarios, 72 checks; 4 skipped (`writing`, `stop-writing`, `stop-planning`, `recompose`) |
| This script, `ORION_E2E_MODEL=bedrock`, before the composer fix | Bedrock, prompts @3 | 14 of 14, 116 checks. The `j1-open` chip asked "Which field defines high risk?" first; `stop-planning` did not exist yet |
| Same, after the fix | Bedrock, prompts @4 | 13 of 14. `restore` failed only on ProteinPaint's "Failed to fetch" from the page the reload tore down (see "Fixed") |
| **Same, final** | **Bedrock, prompts @4** | **15 of 15 scenarios, 118 checks**, in about 6 minutes |

The final Bedrock run made 39 recorded model calls over 22 runs: 22 planner calls ok, 15 composer
calls ok, and 2 cancelled by the Stop scenarios. It also made 38 unrecorded warm-up calls, 2 for
each of the 19 warm-up turns (every fresh tab, new thread or reload sends one). There was no
re-compose, no fallback and no refusal. The recorded calls used 16,073 output tokens, 38,182
uncached input tokens and 352,931 cache-read tokens. Screenshots and `summary.json` go to
`orion/notes/screenshots/e2e/` (gitignored).

What can still be checked: the model calls and tokens above can be recomputed from the run records
on `mmrf-synthetic_orion-runs`. Those are the 22 runs created between 16:57:31 and 17:03:12 UTC,
and every call in them ran on `us.anthropic.claude-opus-5-5`. The scenario and check counts of
the Bedrock runs, and the browser timings below, cannot be checked. The script then kept only one
`summary.json`, and the stub run at 17:06:34 UTC replaced the Bedrock one. The script now also
keeps a `summary-<model>-<UTC stamp>.json` per run.

## Timings in the browser (NFR-01, Open questions 10 and 12)

These are the browser's own clock, from the click to the screen, over the three full Bedrock runs.
"Plan" means the planner's first card: the plan, or a question card.

| What | Measured |
| --- | --- |
| Plan visible, warm (asked after the warm-up turn ended: typed questions and follow-up chips) | 2.7–4.0 s, p50 3.3 s, 8 asks; all within the 5 s **Proposal** |
| Plan visible, first question in a fresh tab (the starter clicked as the page loads, the warm-up turn still running) | 7.2–10.8 s, p50 7.9 s, 18 asks; **all 18 over 5 s** (see Known limits 1) |
| The warm-up turn the Assistant sends on opening | 4.3 and 5.3 s (two measurements; in the agent log the planner's warm-up takes about 3 s and the composer's about 5 s, in parallel) |
| A plan-card edit (compiled, no model call) to "Plan, version 2" | 0.1 s |
| "Writing interpretation" until the Interpretation: J1 | 8.4–10.8 s, p50 9.1 s (12 answers) |
| The same: J2 / J3 | 7.7–10.4 s / 11.2–20.6 s (3 each) |
| The same: follow-up chips | 7.7–20.6 s, plus one 31.1 s with a re-compose (V-NUM: the number word "one") |
| The same: the edit's version 2 answer | 9.5–9.6 s |
| Stop while writing or planning | "Stopping", then "Stopped by you"; the aborted call is recorded as `cancelled` 0.25–1.05 s after it started |

From the final run's records, measured by the agent: planner calls 3.2 s at p50 and 6.1 s at most
(22 calls); composer calls 8.8 s at p50 and 22.7 s at most (15 calls).

The evals measure plan-visible time at the agent, after the warm-up: p95 4.52 s over 94 asks in
the 5-repeat run with `orion-prompts@4`. Composer calls took 9.5 s at p50 and 22.2 s at p95 over 88
calls.

## Fixed in this step

1. **The composer's follow-up chips needed a clarifying question** (`model/prompts.py`, prompts
   `orion-prompts@4`).
   - A chip is asked as a new question. The planner's rule 6 (E10) says a risk word without its
     classification is ambiguous between the CGS risk category and the ISS stage.
   - Every J1 answer on All cases (5 of 5) wrote a chip such as "Top mutated genes in High risk":
     8 of 37 chips before the fix, one or two per such answer. Clicking it asked "Which field
     defines high risk?" after a CGS answer, and picking ISS would have answered another question
     (FR-19: thread context).
   - Composer rule 9 now says a chip stands alone: a group is named by its field, and a risk group
     always names its classification ("CGS high-risk cases").
   - Afterwards, 138 chips over the E2E runs and an eval pass were written. No chip's question
     carries a bare risk word; five labels shorten "CGS high-risk vs standard-risk", and their
     questions are fully qualified. Every chip run since planned with no question, in 3.0–4.0 s
     when no warm-up turn was running.
   - New test: `ComposerPromptTest.test_follow_ups_stand_alone`, which fails on the old prompt.
   - The E2E now requires the first chip to plan without a question.
   - The verifier, the contract and every eval gate are unchanged. The rule names no eval prompt
     (C-31; the prefix test passes).
2. **The browser E2E against real models** (`orion/scripts/e2e-mode-l.mjs`). This is the model
   mode, plus four new scenarios, signature checks and timings, as above.
   - It also fixes the E10 pass message, which counted the options after the card had closed
     ("0 field options"). It now reports the count it checked (2).
   - A teardown artifact from ProteinPaint was not an ORION error, and is now kept apart. When the
     `restore` scenario reloads while the composer writes, the DE viewer is loading on the old page.
     The reload aborts its request, and ProteinPaint's DE plot logs `console.error("Failed to
     fetch")` 19 ms after the reload starts and before the new document commits (seen in 3 of 7
     runs of `restore`). The script now keeps that one message apart from page errors when it is
     logged between the script's own reload or Back and the next commit: the exact text "Failed to
     fetch", from a source that is not ORION's. The first version of this fix kept every console
     error of that window apart, in both modes. That was narrowed in the review round below, so
     every other console error fails the scenario again, as in stage 1.
3. **Docs:** the agent README's stage 2 section (the chip rule and the Bedrock E2E), the demo
   script (the Bedrock E2E command, the warm-up advice, and the model's own chips), and the E2E
   script's header.

## Evals after the composer change

- **1 repeat** (`orion-evals-20261105T164252Z`): 24 pass, 0 fail, 0 not run. All nine gates pass,
  E22's held-out paraphrases included. Plan p95 was 4.65 s over 34 asks; cited numbers 33 of 33
  runs; zero writes without confirmation, 44 of 44.
- **5 repeats** (`orion-evals-20261105T170844Z`): 24 pass, 0 fail, 0 not run. All nine gates
  pass:
  - E16 over 30 runs;
  - phrasing parity, E22's 9 paraphrases included;
  - cited numbers in 93 of 93 runs;
  - zero writes without confirmation, 104 of 104;
  - plan p95 4.52 s over 94 asks.

  There were 190 model calls, all ok, all on `us.anthropic.claude-opus-5-5`, with no fallback.
  0 of 88 answers were re-composed (2 of 88 with `@3`). `bedrock-evals.md` has the details.

## Review round (after this step)

A review of the stage 2 working tree found these, and each one now has an offline test that fails
on the code before it. No model call was made, and no prompt text changed (`orion-prompts@4`).

- **A refusal whose fallback call then fails** (`model/client.py`). For a non-streaming call,
  anthropic 2.5.0's refusal fallback returns the failed hop's response. The SDK then raised that
  status, so the run ended Failed `INTERNAL_ERROR` instead of `PLANNER_DECLINED`, or Observed only
  with no code instead of `MODEL_DECLINED`. The run also stayed pinned to the fallback that had
  failed. The client now returns the refusal ("… declined …, and the fallback model could not be
  reached") and lifts the pin, so the run's next call starts at Claude Opus 5.5. The
  `PLANNER_DECLINED` limitation now carries the planner's own reason. Before, it always read "both
  models declined".
- **A Stop during the SDK's retry backoff** wrote the same callId twice, and the controller's
  stand-in entry named the default model. The client now records the aborted attempt itself, with
  the model it was going to call. The controller's stand-in, used for roles outside the scope, now
  numbers after the scope's calls and takes the role's model and effort.
- **Non-SDK exceptions.** With no AWS credential, request signing raises a plain `RuntimeError`.
  It now becomes a `ModelRoleError` of kind `credentials`, with the attempt recorded. A planner
  failure ends the run Failed, a composer failure leaves Observed only, and the warm-up reports an
  error.
- **Attempt numbers** count the attempts on each model within a call. A fallback's first attempt
  was numbered 2 when the refusal came on the SDK's retry. `config.py` now gives the true worst
  case: the fallback shares the SDK's retry budget, so 3 × 30 s = 90 s for the planner.
- **Bedrock's 408 and 424** (the model timed out, or the model failed) now map to `timeout` and
  `server_error`, not `rejected`.
- **V-SEC.** A clinical word inside a vocabulary value (the regimen
  "Daratumumab, lenalidomide, dexamethasone") was exempt in every run. It is now exempt only when
  the run itself names that value. Even then, the text must not frame it as advice ("Consider
  <regimen> for …", "get <regimen>", "may do well on <regimen>"). Follow-up chips now take V-SEC's
  causal and certainty check too ("Which genes drive survival?").
- **A cohort name the question does not state** is dropped from the planner's output, so code
  templates name the cohort (A2: the planner returns codes and ids).
- **E12.** A `<` that could start a tag is written as `\u003c` inside the request's JSON, so no
  question, cohort name or fact label can close `<request>`. A `<` before a space or a digit
  ("adjusted p < 0.05") is kept, so every eval request is byte-identical.
- **Checks.** The agent suite passes 748 tests, mypy `--strict` is clean, and the fixtures are up to
  date. The stub browser E2E passes 11 of 11 scenarios with 72 checks
  (`summary-stub-20261105T181856Z.json`). The Bedrock E2E and evals were not rerun.
- **Docs.** `bedrock-evals.md` now reports J3's composer time per run: about 20 s with the shipped
  prompts, not 11.6 s. It also limits the NFR-01 result to asks that do not overlap the warm-up,
  and says which numbers come from artifacts and which from ad hoc probes. The E2E's teardown
  exemption and its summary files are as described above.

## Final verification (2026-11-05, 18:21–18:30 UTC)

The final check of stage 2 ran on the review round's working tree. The fork is unchanged at
3fc0e6a, so the frontend was not rebuilt.

- **Offline gates, all green:**
  - the agent suite passes 748 tests, with 6 live tests skipped;
  - mypy `--strict` is clean on 94 files;
  - `build_fixtures.py --mirror /fork --check` reports all fixtures up to date;
  - the evals unit tests pass 119, with 1 skipped, and mypy `--strict` is clean on 35 files;
  - the infra tests pass 11, and mypy `--strict` is clean;
  - the fork's jest gives 917 passed and 2 skipped;
  - `tsc` shows only the 5 known svg errors, none under ORION, and eslint is clean.
- **Stub mode, all green.** orion-agent was recreated with the stubs at 18:23 UTC.
  - The browser E2E passes 11 of 11 scenarios with 72 checks, and the 4 Bedrock-only scenarios
    are skipped (`summary-stub-20261105T182419Z.json`).
  - `evals-mode-l.sh agent` gives 23 pass, 0 fail, and E22 not run
    (`orion-evals-20261105T182435Z`), as in stage 1.
- **Bedrock mode: not run.** The profile `sandbox`'s credentials had expired. `sts
  get-caller-identity` answered ExpiredToken at 18:21 and again at 18:30 UTC, so no model call was
  made. The review round changed the model client, the verifier and the request encoding, so these
  are still to run after a refresh:
  - the full Bedrock evals, at 5 repeats with E16 and E22;
  - the warm timings;
  - the Bedrock browser E2E.

  `orion/scripts/evals-bedrock.sh run` does the first two. Then run
  `ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs`.

## Known limits

1. **The first question in a fresh tab waits for the warm-up turn (NFR-01).** The Assistant sends
   `warmup` when it opens, and the design allows one invocation in flight per session, warm-up
   included. A question asked in the first ~5 s waits for it, so the plan shows 7–11 s after the
   click instead of about 3 s. This happened in all 18 such asks. The same happens after a reload
   or browser Back, since each mounts a new controller and warms again.
   - The evals miss this because they ask after the warm-up.
   - A user who reads the starters for a few seconds does not see it. A presenter clicking at
     once does, so the demo script now says to wait about 5 s.
   - Options, for the team to decide:
     - (a) the browser cancels a still-running warm-up when a question is sent. The agent's
       warm-up task is cancelled when its stream closes; a cold cache cost nothing measurable in
       stage 2's timings, but a schema's first-use grammar compile, 9–12 s, would then land on
       that question;
     - (b) the agent ends the warm-up turn once the planner's warm-up returns, and finishes the
       composer's in the background (about 2 s saved, still over 5 s);
     - (c) accept it and keep the advice.
   - Nothing was changed.
2. **Edits are disabled while the run's own turn streams.** The fork disables every resume
   control, Apply included, while a turn for the run is in flight. That covers Executing and
   Answering in the tab that asked. The design's F2 table lists "edit a setting" for Executing,
   and E24's scope switch while J1 runs is credited by the harness, not the browser. Answering
   offers only Stop in F2, so `recompose` edits at rest after Complete; that edit re-composes, as
   shown. With in-process tools Executing lasts under 2 s, so this was not changed. It predates
   stage 2.
3. **Model text varies from run to run.** The E2E checks structure, verification, citations and
   equality with the record, never the wording. The new chip check depends on the composer
   following rule 9. It held in every run after the fix, but a model regression would show as a
   `j1-open`, `j2` or `follow` failure naming the chip.
4. **Chips can ask for more genes than the panel holds** ("Which 10 panel genes are mutated most
   often in CGS high-risk cases?"). The run says so honestly ("Only 6 genes ranked, fewer than the
   10 asked for."), but the chip reads oddly. This is left as is.
5. **Re-compose causes are only in the agent log** (`re-compose after V-NUM interpretation[1]: the
   number word 'one' …`). The record keeps no verifier finding, and adding one needs a contract
   change.
6. **Warm-up calls are not recorded.** About half of a Bedrock E2E run's calls are warm-ups. They
   appear in the agent log ("warmup: [('planner', 'ok'), ('composer', 'ok')]") and in Bedrock's
   own metering, never in a run record.
7. **Console noise that is not an error.** A reload during "Writing interpretation" shows 409
   responses on `/api/orion/agui`: the restored page's warm-up meets the run's session lease and
   retries, by design. ProteinPaint may log "Failed to fetch" for a viewer request the reload
   aborts.
8. **Carried over from the eval step:**
   - a first planner attempt can stall until the 30 s timeout: twice in about 250 calls there,
     none in this step's E2E runs;
   - the design doc's A4 still names `AnthropicBedrockMantle` and the base model ids, while the
     code uses bedrock-runtime and the `us.*` inference profiles;
   - process cold start (an AgentCore microVM) cannot be measured locally.

## Rerunning it

All commands run from the sandbox root, with `export PATH=$HOME/.docker/bin:$PATH`.

- **Bedrock E2E:** `orion/scripts/orion-local.sh up --bedrock`, then
  `ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs`. It takes about 6 minutes and makes
  about 80 Bedrock calls. Expect "15 of 15 scenarios passed".
- **Stub E2E:** `orion/scripts/orion-local.sh up --stub`, then `node orion/scripts/e2e-mode-l.mjs`.
  It takes about 1 minute and makes no AWS call. Expect "11 of 11 scenarios passed; 72 checks;
  4 skipped".
- **Bedrock evals:** `orion/scripts/evals-bedrock.sh evals --repeats 1` (about 15 minutes) or
  `orion/scripts/evals-bedrock.sh evals` (5 repeats). See `orion/notes/bedrock-evals.md`.
- **Offline gates, after the review round below:**
  - the agent suite passes 748 tests, with 6 live tests skipped;
  - mypy `--strict` is clean on 94 files;
  - `build_fixtures --check` reports all fixtures up to date;
  - the fork (unchanged) passes jest with 917 passed and 2 skipped, `tsc` shows only the 5 known
    svg errors (none under ORION), and eslint is clean.

Leave the agent in Bedrock mode only while the credentials are fresh. `up --stub` is the safe
default for anyone else who uses the stack.
