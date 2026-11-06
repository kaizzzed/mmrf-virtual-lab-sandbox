# ORION stage 2 (Bedrock only): status

As of 2026-11-06, 01:55 UTC. The last section, "Final proof on the final code (2026-11-06,
00:54–01:43 UTC)", is the latest check: every offline gate, the stub runs and the Bedrock runs, on
the code as it stands. The sandbox is on `orion/bedrock`, stacked on `orion/mode-l`, at bb7c6e0.
That commit holds stage 2 and its review round; it is committed locally, not tagged or pushed.
The fixes of the second and third reviews are in the working tree, not committed:

- `orion/agent`: `model/prompts.py`, `model/eval_prompts.py`, `model/verifier.py`,
  `model/lexicon.py` and their tests;
- `orion/evals`: `orion_evals/timings.py` and its tests;
- `orion/scripts/e2e-mode-l.mjs`, these notes and the agent README.

"Second review: C-31 and V-SEC fixes" and "Third review: V-SEC on text about a regimen" have the
detail. `orion/contracts` is as committed. The working tree's `orion/infra`, `helm`,
`sources.json` and `terraform` changes are stage 3's, not stage 2's. The frontend fork is
unchanged at 3fc0e6a. The agent version is 0.2.0,
with prompts `orion-prompts@5`. Stage 2 means:

- the agent still runs locally in Compose (mode L), with in-process tools against
  `http://identity:8080` and run records on the volume `mmrf-synthetic_orion-runs`;
- the planner and the composer are Claude Opus 5.5 on Amazon Bedrock;
- the models are the inference profiles `us.anthropic.claude-opus-5-5`, with refusal fallback
  `us.anthropic.claude-opus-5`, in `us-east-1`;
- the client is `AsyncAnthropicBedrock` on bedrock-runtime, as the role `WSParticipantRole`;
- the planner runs at effort `low` and the composer at effort `medium`, with a cached system
  prefix and the 5-minute TTL.

The stubs stay the Compose default. Bedrock is opt-in. All data is synthetic.

**Verdict: stage 2 works end to end on Bedrock with the final code, the second and third
reviews' fixes included: 24 of 24 evals and all nine gates in the 5-repeat run, and 15 of 15
browser scenarios. NFR-01's plan p95 passed there at 4.55 s, but it has failed in two of five
5-repeat runs, each time on slow Bedrock answers, so it stays a decision for the team.** These
checks back the verdict. All of them ran on the working tree as it stands, with `orion-prompts@5`
("Final proof on the final code", the last section):

- **Evals, Bedrock, 5 repeats:** 24 of 24 pass and 9 of 9 gates pass
  (`orion-evals-20261106T010531Z`, 01:05–01:36 UTC on 2026-11-06), the plan-visible p95 at 4.55 s
  over 94 asks. No answer was re-composed and no sentence was dropped (`bedrock-evals.md`, "Final
  proof on the final code"). The second review's code had passed the same way (`220702Z`, p95
  4.64 s), after a first attempt that met a Bedrock connection outage (`213319Z`, 22 of 24).
- **Browser end to end, Bedrock mode:** 15 of 15 scenarios and 118 checks pass in headless
  Chromium (`summary-bedrock-20261106T014241Z.json`, 01:36–01:42 UTC). That covers the 11 stage 1
  scenarios, plus four that only real models make meaningful.
- **Browser end to end, stub mode:** 11 of 11 scenarios and 72 checks, with the 4 Bedrock-only
  scenarios recorded as skipped (`summary-stub-20261106T005720Z.json`). `evals-mode-l.sh agent`
  on the stubs gives 23 pass and E22 not run (`orion-evals-20261106T005741Z`).
- **Offline:** the agent suite passes 764 tests (6 live tests skipped), mypy `--strict` is clean
  on 94 files, the fixtures are up to date, and the evals unit tests pass 120.

None of the three review rounds' stricter rules rejected model text in an eval. After the first
round no V-SEC finding, dropped chip or changed plan showed up in about 190 Bedrock runs. After the
second, none showed up in the 105 runs of the rerun, whose 4 re-composes were 3 V-NUM number
words and 1 malformed placeholder. After the third, none showed up in the 105 eval runs and 22
E2E runs of the final proof, which had no re-compose at all. Before the first round, one defect
turned up and was fixed: the composer's follow-up chips needed a clarifying question. One gap is
measured and left for a decision: the first question in a fresh tab waits for the warm-up turn.

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

The credentials expire. The agent ran the stubs from about 18:18 UTC for the review round. At
18:58 UTC on 2026-11-05, `up --bedrock` recreated it in Bedrock mode, with the credentials of the
refreshed profile. The Docker engine restart at 19:59 UTC stopped it with the rest of the stack.
At 20:40 UTC, `up --bedrock --no-build` started only orion-agent again, in Bedrock mode. Compose
restarted the existing 18:58 container rather than recreating it (`docker inspect`: Created
18:58:21 UTC, StartedAt 20:40:14 UTC, the same config hash), so the agent kept the credentials
exported at 18:58, not fresher ones. The rest of the stack was restarted at 20:51 UTC. At
21:23:41 UTC, `up --bedrock` rebuilt orion-agent from the working tree and recreated the
container (Created 21:23:41 UTC) with credentials exported then, for the runs under "After the
C-31 and V-SEC fixes". Compose recreates the container only when its image or configuration changes, and the
exported credentials are part of its environment: after a profile refresh, `up --bedrock`
recreates it, and the container's Created time (`docker inspect`) shows whether it did. `up --stub` goes back to the stubs. With Bedrock,
once the credentials expire, every model call fails:

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
| Same, final before the review round | Bedrock, prompts @4 | 15 of 15 scenarios, 118 checks, in about 6 minutes |
| Stub run in the review round's final verification (bb7c6e0's tree) | stub | 11 of 11 scenarios, 72 checks (`summary-stub-20261105T182419Z.json`); the 4 Bedrock-only scenarios were not run (stub mode; the console said so, that summary does not) |
| Stub run after the C-31 and V-SEC fixes (working tree, `orion-prompts@5`), 21:22:45–21:23:26 UTC | stub | 11 of 11 scenarios, 72 checks; 4 skipped, now recorded in the summary's `skipped` (`summary-stub-20261105T212326Z.json`) |
| Same, after the review round (bb7c6e0), 19:44:45–19:51:32 UTC | Bedrock, prompts @4 | 15 of 15 scenarios, 118 checks, in about 7 minutes; no `unloadErrors` in any scenario (`summary-bedrock-20261105T195132Z.json`) |
| Same, after the C-31 and V-SEC fixes (working tree), 22:00:19–22:06:37 UTC | Bedrock, prompts @5 | 15 of 15 scenarios, 118 checks, in about 6 minutes (`summary-bedrock-20261105T220637Z.json`); no `unloadErrors` in any scenario |
| Stub run of the final proof (the final working tree), 00:56:38–00:57:20 UTC on 2026-11-06 | stub | 11 of 11 scenarios, 72 checks; 4 skipped (`summary-stub-20261106T005720Z.json`) |
| **Same, the final proof (the final working tree, the third review's verifier), 01:36:44–01:42:41 UTC** | **Bedrock, prompts @5** | **15 of 15 scenarios, 118 checks**, in about 6 minutes (`summary-bedrock-20261106T014241Z.json`); no `unloadErrors` in any scenario |

**On the final code.** The Bedrock E2E made 39 recorded model calls over 22 runs (created
01:36:52 to 01:42:20 UTC). 22 planner calls and 15 composer calls were ok, and the two Stop
scenarios cancelled one planner call (after 1.04 s) and one composer call (after 0.23 s). Every
call ran on `us.anthropic.claude-opus-5-5`, attempt 1, with no re-compose, no fallback and no
refusal. The recorded calls used 15,318 output tokens, 37,846 uncached input tokens and 353,107
cache-read tokens, with no cache write. Planner calls took 2.84 s at p50 and 3.86 s at most;
composer calls 8.91 s at p50 and 20.22 s at most. These figures come from the run records
(`orion/evals/reports/e2e-calls-20261106T014241Z.txt`). The agent log
(`agent-log-e2e-bedrock-20261106T014241Z.txt`) shows 19 warm-up turns, all ok, and no re-compose,
drop or error line. Its 75 bedrock-runtime requests were all HTTP 200: the 37 completed calls and
2 per warm-up turn, so no call was retried.

**After the C-31 and V-SEC fixes.** The Bedrock E2E made 39 recorded model calls over 22 runs
(created 22:00:25 to 22:06:15 UTC). 22 planner calls and 15 composer calls were ok, and the two Stop
scenarios cancelled one planner call (after 1.03 s) and one composer call (after 0.41 s). Every call
ran on `us.anthropic.claude-opus-5-5`, with no re-compose, no fallback and no refusal. The recorded
calls used 15,942 output tokens, 38,429 uncached input tokens and 353,107 cache-read tokens, with no
cache write. Planner calls took 3.21 s at p50 and 4.31 s at most; composer calls 9.07 s at p50 and
21.36 s at most. These figures come from the run records. The agent log
(`orion/evals/reports/agent-log-e2e-bedrock-20261105T220637Z.txt`) shows 19 warm-up turns, all
ok, and no re-compose, drop or error line.

**After the review round.** The Bedrock E2E made 39 recorded model calls over 22 runs (created
19:44:52 to 19:51:08 UTC). 22 planner calls and 15 composer calls were ok. The two Stop scenarios
cancelled one planner call (after 1.03 s) and one composer call (after 0.58 s). Every call ran on
`us.anthropic.claude-opus-5-5`, with no re-compose, no fallback and no refusal. The agent log shows
19 warm-up turns, all ok: about 38 unrecorded calls, one per role per turn if none was retried
(the log names each role's outcome, not the model that answered or a retry). The recorded calls used 18,561 output
tokens, 38,001 uncached input tokens and 352,931 cache-read tokens, with no cache write. Planner
calls took 3.06 s at p50 and 5.77 s at most; composer calls took 10.80 s at p50 and 22.73 s at most.
These figures come from the run records and from the agent-log excerpt
`orion/evals/reports/agent-log-e2e-bedrock-20261105T195132Z.txt`. The review round's narrowed
teardown exemption held: `restore` passed, and no scenario logged a teardown message.

**Before the review round**, the final Bedrock run made 39 recorded model calls over 22 runs: 22 planner calls ok, 15 composer
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

These are the browser's own clock, from the click to the screen. "Plan" means the planner's first
card: the plan, or a question card.

**On the final code**, from `summary-bedrock-20261106T014241Z.json` (one full run):

- **Plan shown, asked after the warm-up turn:** 2.6, 3.0 and 3.4 s (a typed question and two
  chips).
- **Plan shown, a warm-up turn in flight:** 7.1–8.5 s (6 asks; Known limits 1). The warm-up turn
  itself took 4.6 s.
- **"Writing interpretation":**
  - J1: 8.2–9.9 s (4 answers: open-ended, tool-named, in the race cohort, typed);
  - J2: 7.1 s; J3: 11.7 s;
  - the three chips: 8.1–9.8 s;
  - the edit's version 2 answer: 7.2 s.

**After the C-31 and V-SEC fixes**, from `summary-bedrock-20261105T220637Z.json` (one full run):
the plan showed after 2.6, 3.7 and 3.7 s when asked after the warm-up turn (a typed question and
two chips), and after 7.5–8.8 s (6 asks) when a warm-up turn was in flight (Known limits 1).
"Writing interpretation" lasted 8.1–9.3 s for J1 (4 answers: open-ended, tool-named, in the race cohort, typed), 7.5 s for J2, 20.0 s for J3,
8.8–9.9 s for the three chips and 8.1 s for the edit's version 2 answer.

**After the review round**, from `summary-bedrock-20261105T195132Z.json` (one full run):

| What | Measured |
| --- | --- |
| Plan visible, warm (asked after the warm-up turn ended: a typed question and two follow-up chips) | 2.9, 3.1 and 3.4 s; all 3 within the 5 s **Proposal** |
| Plan visible, a question asked while a warm-up turn was running (asked as a page or a new thread opened; one of them a chip) | 7.2–9.1 s, p50 8.1 s, 6 asks; **all 6 over 5 s** (Known limits 1, unchanged) |
| A plan-card edit (compiled, no model call) to "Plan, version 2" | 0.1 s |
| "Writing interpretation" until the Interpretation: J1 | 8.4–11.1 s (4 answers: open-ended, tool-named, in the race cohort, typed) |
| The same: J2 / J3 | 7.0 s / 22.7 s |
| The same: follow-up chips | 9.0, 15.3 and 16.4 s (no re-compose) |
| The same: the edit's version 2 answer | 8.8 s |

**Before the review round**, over its three full Bedrock runs. No artifact of these survives
("The browser end to end in both modes"):

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

From that final run's records, measured by the agent: planner calls 3.2 s at p50 and 6.1 s at
most (22 calls); composer calls 8.8 s at p50 and 22.7 s at most (15 calls).

The evals measure plan-visible time at the agent, after the warm-up. Before the review round it
was p95 4.52 s over 94 asks in the 5-repeat run with `orion-prompts@4`, and composer calls took
9.5 s at p50 and 22.2 s at p95 over 88 calls. After it, the p95 was 5.96 s over 94 asks
(`190523Z`, a failed gate, from slow Bedrock answers), and composer calls took 9.99 s at p50 and
23.05 s at p95 over 89 calls. The warm timings after it gave a p95 of 4.61 s over 24 asks.

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
3. **Docs:** the agent README's stage 2 section (the chip rule and the Bedrock E2E) and the E2E
   script's header.

## Evals after the composer change (before the review round)

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

  There were 192 model calls (`usage-20261105T170844Z.json`, which counts E3's reuse run since
  the `usage` fix), all ok, all on `us.anthropic.claude-opus-5-5`, with no fallback.
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
  (`summary-stub-20261105T181856Z.json`). The Bedrock E2E and evals were rerun after a credential
  refresh; see "Bedrock rerun after the review round".
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
  - The browser E2E passes 11 of 11 scenarios with 72 checks
    (`summary-stub-20261105T182419Z.json`). The 4 Bedrock-only scenarios were not run in stub
    mode; that showed in the console output only, since the summary then had no `skipped` field.
  - `evals-mode-l.sh agent` gives 23 pass, 0 fail, and E22 not run
    (`orion-evals-20261105T182435Z`), as in stage 1.
- **Bedrock mode: not run then.** The profile `sandbox`'s credentials had expired. `sts
  get-caller-identity` answered ExpiredToken at 18:21 and again at 18:30 UTC, so no model call was
  made. The full Bedrock evals, the warm timings and the Bedrock browser E2E ran after the
  profile was refreshed; see the next section.

## Bedrock rerun after the review round (2026-11-05, 18:58–20:40 UTC)

The user refreshed the profile `sandbox`. `evals-bedrock.sh check` then reached sts (role
`WSParticipantRole`), and `orion-local.sh up --bedrock` rebuilt and recreated orion-agent from bb7c6e0
at 18:58 UTC ("planner: bedrock, composer: bedrock"). No AWS resource was created or changed, and
no code, prompt, verifier, contract, check or gate changed. `bedrock-evals.md` ("After the review
round") has the details.

| Step | Time (UTC) | Artifact | Result |
| --- | --- | --- | --- |
| Evals subset, 1 repeat: E1–E6, E10, E12, E15, E16, E22 | 18:59–19:05 | `orion-evals-20261105T185904Z` | 11 of 11 pass; no re-compose; plan p95 4.54 s over 18 asks (not gated: under 20) |
| Evals, 5 repeats (`evals-bedrock.sh run`) | 19:05–19:37 | `orion-evals-20261105T190523Z`, `usage-20261105T190523Z.json` | **24 of 24 pass; 8 of 9 gates.** Plan p95 **5.96 s** over 94 asks: **fail** (4.52 s before the round). 193 calls (`usage` with E3's reuse run), all ok; 1 of 88 answers re-composed (V-NUM, "one") |
| Warm timings, 4 rounds (the same `run`) | 19:37–19:44 | `timings-warm-20261105T194406Z.json` | 24 of 24 Complete; plan p50 3.16 s, p95 4.61 s, 24 of 24 within 5 s; 2 J3 answers re-composed (V-NUM, "one" and "two") |
| Browser E2E, `ORION_E2E_MODEL=bedrock` | 19:44–19:51 | `summary-bedrock-20261105T195132Z.json` | **15 of 15 scenarios, 118 checks** |
| Evals re-measure, 5 repeats (`evals-bedrock.sh evals`) | 19:55–19:59 | none | stopped by the Docker engine restart below; E1–E4 had run, with no report written |

What the rerun shows:

- **The review round broke nothing.** Every eval and every browser scenario passed. Every plan
  signed the stub planner's plan. The stricter V-SEC (the run-scoped regimen exemption, the
  advice framing and the causal check on chips), the planner's cohort-name rule and the `<`
  escaping rejected no model text and changed no plan. The 3 re-composes were all V-NUM number
  words, a cause that predates the round. The refusal and error paths the round changed did not
  run, because no call was refused, failed or retried; their offline tests cover them. The two
  Stop scenarios each cancelled one call and recorded it as `cancelled`, as before.
- **The plan p95 gate failed once, on Bedrock latency.** The planner requests and plans were the
  same as before the round: 15,856 against 15,857 uncached input tokens over 102 calls, the same
  cache reads, and the same signatures and output sizes. Seven first attempts took 5.2 to 11.6 s between
  19:10 and 19:28 UTC, against a median of about 3.4 s, and the gate tolerates 4 asks over 5 s in
  94. The warm timings right after it, and the browser E2E, were back to normal. This is not a
  code regression, and no code was changed for it. Whether NFR-01 holds after the round still
  needs the re-measure.
- **The timings held.** The plan takes 7–11 s when the first question meets the warm-up and
  about 3 s otherwise, and J1's "Writing interpretation" takes about 8–15 s.

**The Docker engine restart.** At 19:59:52 UTC, a change to Docker Desktop's resource settings
(CPUs, memory and swap) restarted the Docker engine. Docker Desktop's own log
(`~/Library/Containers/com.docker.docker/Data/log/host/com.docker.backend.log`) shows the settings
change, then "engine stop requested". Nothing in this rerun touched those settings.
That stopped every container of the stack: orion-agent, identity, guppy, Elasticsearch, analysis,
ProteinPaint and the frontend. The re-measure's harness was killed with them (exit 137). Its last run
record stays "Answering". This rerun may restart only orion-agent, so at 20:40 UTC it ran
`orion-local.sh up --bedrock --no-build`. Compose restarted the 18:58 container instead of
recreating it, so its credentials stayed the ones exported at 18:58. The rest of the stack was
restarted at 20:51 UTC (not by this rerun).

What was left then is done or decided below: the stack was restarted at 20:51 UTC, and the
re-measure ran after the second review's fixes ("Second review: C-31 and V-SEC fixes"). How NFR-01
is measured or met stays with the team (`bedrock-evals.md`, Open issues); the 5 s target is
unchanged.

## Second review: C-31 and V-SEC fixes (2026-11-05, 21:05–22:45 UTC)

A second review of stage 2 confirmed the findings below. Each code fix has an offline test that
fails on the code before it; `bedrock-evals.md` ("After the C-31 and V-SEC fixes") has the detail.
No eval gate, verifier rule or contract rule was loosened, and no eval prompt or held-out
paraphrase went into prompt text.

- **C-31, two planner examples** (`model/prompts.py`, `orion-prompts@5`). A clustering example
  asked E23's first out-of-kind question in other words, and the injected-name example held E12's
  question with one word inserted. They are now "Show a histogram of age at diagnosis"
  (Unsupported, Clinical Data Analysis) and a top-mutated-genes question on the injected-name
  saved cohort. The guard (`model/eval_prompts.py`) now also refuses an example that touches an
  E23 or E18 topic or embeds an eval prompt with a word or two changed, and both old examples fail
  it.
- **V-SEC, the run-scoped regimen exemption** (`model/verifier.py`). A regimen the run uses is a
  name only where the text uses it as one (followed by " (ORION)" or a group word, in a list of
  groups, "cases receiving …", "in …", "between … and", "whose <field> is …"); any other mention
  fails as in a run that does not use it, and a sentence that names it still fails on a treatment
  judgement. This closes the advice that slipped past the old list ("<regimen> should be used in
  these cases"), the re-match inside "<regimen> (ORION)", and the false positive on "cases
  receiving <regimen>".
- **Tooling.** `evals-bedrock.sh usage` counts E3's reuse run, which only a check's evidence
  names, and the browser E2E writes its skipped scenarios into its summary.
- **Notes.** The call totals (44, 193 and 369 after the first round; 192 and 195 before it), the
  label of `140440Z` (the final `@3` run, not the last run before the round), the J1 median for
  115-token plans (2.9 s), the stub E2E's skipped count, the 20:40 container (restarted, not
  recreated, so the 18:58 credentials) and the warm-up calls' model (not recorded) are corrected,
  and the agent README no longer says every gate passed at 4.19 s.

| Step | Time (UTC) | Artifact | Result |
| --- | --- | --- | --- |
| Offline gates, first pass | 21:15–21:20 | none (console) | agent 753 tests OK (6 skipped), mypy clean, fixtures up to date, evals 120 OK |
| Stub mode: `up --stub`, stub browser E2E | 21:22–21:23 | `summary-stub-20261105T212326Z.json` | 11 of 11 scenarios, 72 checks, 4 skipped recorded in the summary |
| `up --bedrock` (container created 21:23:41) | 21:23 | none | "planner: bedrock, composer: bedrock" |
| Evals subset, 1 repeat: E1–E6, E10, E12, E17, E18, E22, E23 | 21:24–21:29 | `orion-evals-20261105T212405Z`, `usage-20261105T212405Z.json` | 12 of 12 pass; 45 calls, all ok; no re-compose; plan p95 5.04 s over 16 asks (not gated: under 20) |
| Evals, 5 repeats, on that build | 21:29–21:31 | none | stopped after 7 J1 runs (14 calls, all ok) to rebuild with the last V-SEC change |
| Offline gates on the final tree, `up --bedrock` (container created 21:32:56) | 21:31–21:33 | none (console) | as above, all green |
| Evals, 5 repeats (`evals-bedrock.sh evals`) | 21:33–21:59 | `orion-evals-20261105T213319Z`, `usage-20261105T213319Z.json`, `agent-log-20261105T213319Z.txt` | 22 of 24; 7 of 9 gates. A Bedrock connection outage (21:47:36–21:49:47) failed 15 runs before any plan: E20 and E22 failed, and with E22 phrasing parity. Plan p95 7.67 s over 78 asks: fail |
| Browser E2E, `ORION_E2E_MODEL=bedrock` | 22:00–22:06 | `summary-bedrock-20261105T220637Z.json`, `agent-log-e2e-bedrock-20261105T220637Z.txt` | **15 of 15 scenarios, 118 checks** |
| **Evals, 5 repeats, rerun** | **22:07–22:39** | **`orion-evals-20261105T220702Z`**, `usage-20261105T220702Z.json`, `agent-log-20261105T220702Z.txt` | **24 of 24 pass, 9 of 9 gates; plan p95 4.64 s over 94 asks: pass.** 196 calls, all ok; 4 of 89 answers re-composed (3 V-NUM, 1 V-REF), no V-SEC finding |

The outage was outside the stack: every failed call ended after about 4 s with
`APIConnectionError: Connection error.`, Docker logged no event, and the runs before and after it
planned normally. Its effect is shown, not removed: `213319Z` stays a recorded 22 of 24, and the
rerun on the same container is the result. During the rerun someone opened the Assistant through
the frontend (two warm-up turns and one J2 run at 22:20–22:21 UTC); those calls are not counted.

This step made 489 recorded Bedrock calls (45, 14, 195, 39 and 196), all on
`us.anthropic.claude-opus-5-5` with no fallback, plus the E2E's 19 warm-up turns. It created or
changed no AWS resource; its only AWS calls were Bedrock runtime calls and `sts
get-caller-identity`. orion-agent was left running in Bedrock mode (the container created at
21:32:56 UTC); `up --stub` returns it to the stubs.

## Known limits

1. **The first question in a fresh tab waits for the warm-up turn (NFR-01).** The Assistant sends
   `warmup` when it opens, and the design allows one invocation in flight per session, warm-up
   included. A question asked in the first ~5 s waits for it, so the plan shows 7–11 s after the
   click instead of about 3 s. This happened in all 18 such asks before the review round, in all
   6 after it (7.2–9.1 s), and in all 6 of the final proof (7.1–8.5 s,
   `summary-bedrock-20261106T014241Z.json`). The same happens after a reload or browser Back,
   since each mounts a new controller and warms again.
   - The evals miss this because they ask after the warm-up.
   - A user who reads the starters for a few seconds does not see it; someone who asks at once
     does. Waiting about 5 s after opening the Assistant avoids it.
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
   change. For the rerun after the review round, the relevant log lines are saved beside the
   reports (`orion/evals/reports/agent-log-*.txt`).
6. **Warm-up calls are not recorded.** About half of a Bedrock E2E run's calls are warm-ups. They
   appear in the agent log ("warmup: [('planner', 'ok'), ('composer', 'ok')]") and in Bedrock's
   own metering, never in a run record.
7. **Console noise that is not an error.** A reload during "Writing interpretation" shows 409
   responses on `/api/orion/agui`: the restored page's warm-up meets the run's session lease and
   retries, by design. ProteinPaint may log "Failed to fetch" for a viewer request the reload
   aborts.
8. **The plan-visible p95 depends on Bedrock's latency at the time (NFR-01).** Over five 5-repeat
   runs of practically the same planner requests it was:
   - 4.52 s (pass, `170844Z`);
   - 5.96 s (fail, `190523Z`: seven first attempts took 5.2–11.6 s in a 20-minute window);
   - 7.67 s over 78 asks (fail, `213319Z`: slow first attempts, then a connection outage);
   - 4.64 s (pass, `220702Z`);
   - 4.55 s (pass, `010531Z`, the final code: 2 asks over 5 s).

   The gate tolerates 4 asks over 5 s in 94, and J3's plans take about 4 s at p50 even when
   Bedrock is fast. The target is not changed; how to meet it is for the team to decide
   (`bedrock-evals.md`, Open issues).
9. **Carried over from the eval step:**
   - a first planner attempt can stall until the 30 s timeout: twice in about 250 calls there,
     none in this step's E2E runs, none in about 190 planner calls after the review round, and
     none in the 125 that answered in the final proof (slowest 7.04 s);
   - the design doc's A4 still names `AnthropicBedrockMantle` and the base model ids, while the
     code uses bedrock-runtime and the `us.*` inference profiles;
   - process cold start (an AgentCore microVM) cannot be measured locally.

## Rerunning it

All commands run from the sandbox root, with `export PATH=$HOME/.docker/bin:$PATH`.

- **Bedrock E2E:** `orion/scripts/orion-local.sh up --bedrock`, then
  `ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs`. It takes about 6 to 7 minutes and
  makes about 80 Bedrock calls (after each review round: 39 recorded calls and 19 warm-up turns
  of two calls each, if none was retried). Expect "15 of 15 scenarios passed; 118 checks".
- **Stub E2E:** `orion/scripts/orion-local.sh up --stub`, then `node orion/scripts/e2e-mode-l.mjs`.
  It takes about 1 minute and makes no AWS call. Expect "11 of 11 scenarios passed; 72 checks;
  4 skipped"; the summary file lists the skipped scenarios under `skipped`.
- **Bedrock evals:** `orion/scripts/evals-bedrock.sh evals --repeats 1` (about 15 minutes) or
  `orion/scripts/evals-bedrock.sh evals` (5 repeats). See `orion/notes/bedrock-evals.md`.
- **Offline gates on the final code** (2026-11-06, 00:54–00:56 UTC, the final proof's working
  tree; `orion/evals/reports/final-proof-console-20261106T0054Z.txt`):
  - the agent suite passes 764 tests, with 6 live tests skipped (753 after the second review);
  - mypy `--strict` is clean on 94 files;
  - `build_fixtures --check` reports all fixtures up to date;
  - the evals unit tests pass 120 (2 skip without the `orion/scripts` mount), and mypy `--strict`
    is clean on 35 files;
  - the fork is unchanged since the review round, when it passed jest with 917 passed and 2
    skipped, `tsc` showed only the 5 known svg errors (none under ORION), and eslint was clean;
    it was not rerun.

Leave the agent in Bedrock mode only while the credentials are fresh. `up --stub` is the safe
default for anyone else who uses the stack.

## Final verification (2026-11-05, 22:47–22:57 UTC)

This was the last check of stage 2 before the third review, run on the working tree as it stood
then: bb7c6e0 plus the second review's fixes. Every gate passed, so no code changed. The only edits in this step are this
section and the "As of" line at the top. The code under `orion/agent`, `orion/evals` and
`orion/scripts` is the same code that ran in the 5-repeat rerun `orion-evals-20261105T220702Z`;
only the two READMEs changed after that build (21:32:56 UTC). The fork is unchanged at 3fc0e6a,
so the frontend was not rebuilt and its jest, `tsc` and eslint were not rerun.

| Step | Time (UTC) | Artifact | Result |
| --- | --- | --- | --- |
| `aws sts get-caller-identity --profile sandbox` | 22:47 | none | valid, role `WSParticipantRole` |
| Agent suite (`unittest discover -s tests`) | 22:47–22:48 | none (console) | 753 tests OK, 6 live tests skipped |
| mypy `--strict` on `orion_agent` and `tests` | 22:48 | none (console) | clean, 94 files |
| `build_fixtures.py --mirror /fork --check` | 22:48 | none (console) | all fixtures up to date |
| Evals unit tests (README command, then `-v` to name the skips) | 22:48 | `oracle-live-test-20261105T224812Z.json`, `…224824Z.json` | 120 OK, 2 skipped (`orion/scripts` not mounted) |
| Evals unit tests with `-v $PWD/orion/scripts:/orion-scripts:ro` | 22:48 | `oracle-live-test-20261105T224835Z.json` | 120 OK, none skipped |
| `orion-local.sh up --stub` | 22:48 | none | "planner: stub, composer: stub" |
| Stub browser E2E (`node orion/scripts/e2e-mode-l.mjs`) | 22:48–22:49 | `summary-stub-20261105T224936Z.json` | 11 of 11 scenarios, 72 checks, 4 skipped (the Bedrock-only ones) |
| `evals-mode-l.sh agent` (stubs) | 22:49–22:55 | `orion-evals-20261105T224946Z` | 23 pass, 0 fail, E22 not run |
| `orion-local.sh up --bedrock` (container created 22:55:21) | 22:55 | none | "planner: bedrock, composer: bedrock" |
| `evals-bedrock.sh evals --evals E1 --repeats 1` | 22:55–22:56 | `orion-evals-20261105T225540Z`, `usage-20261105T225540Z.json` | E1 passes. Both phrasings end Complete with the same signature, and every oracle number matches. 4 Bedrock calls, all HTTP 200 on `us.anthropic.claude-opus-5-5`: no fallback, no re-compose. The open-ended plan was visible after 3.0 s (not gated: one ask) |

The usage file counts 1 run and 2 calls because E1's report lists only the open-ended run. The
tool-named run's planner and composer calls appear only in the agent log.

This step's only AWS calls were two `sts get-caller-identity` calls (one above, one from
`evals-bedrock.sh`'s check) and the 4 bedrock-runtime calls. It did not create or change any AWS
resource. It did not touch stage 3's working-tree changes (`orion/infra`, `helm`, `sources.json`,
`terraform/.terraform.lock.hcl`) or its deployed resources. orion-agent was left running in
Bedrock mode, in the container created at 22:55:21 UTC; the final proof replaced it.

## Third review: V-SEC on text about a regimen (offline, 2026-11-06)

**In short.** The second review's V-SEC change was a regression. It let through advice that
bb7c6e0 rejected, such as "Try <regimen> and see whether the mutated cases respond" and
"Consider <regimen> (ORION) for these cases". It also let through benefit claims about a group
described by its regimen, such as "Patients receiving <regimen> may live longer". All of these now
fail, along with about 130 rewordings found later, because a text about a regimen may now use only
plain descriptive words. Every case of the review's probe fails: 42 of 42 and 10 of 10, where
bb7c6e0 failed 28 and 10 (`orion/evals/reports/vsec-review-probe-20261106T0115Z.json`). Tests
added: 11, so the agent suite went from 753 to 764.

- `VSecRegimenTextTest`: 6 tests over 131 bypasses, 87 other advice cases, 47 descriptive texts
  and 17 borderline texts.
- 3 more V-SEC tests on the review's findings.
- 2 C-31 guard tests.

A first fix restored the advice checks around the name and added judgement words. The review's
skeptics then attacked that fix, and found about 130 advice and treatment-judgement sentences it
kept, nearly all in a run that names a regimen: a described group
("Patients receiving <regimen> may thrive", "… may outlive the others"), a population ("It may be
worth enrolling the mutated cases in <regimen>"), a group word ("Assign the mutated cases to the
<regimen> arm"), a whole cohort name ("Use <regimen> (ORION) in the mutated cases"), plus regimen
short names ("Use VRd for the mutated cases"), brand names, care words ("Add maintenance …") and
drug names hidden by a zero-width space or a Cyrillic letter, which bb7c6e0 kept too. The cause
was the same each time: once a regimen read as a name, only closed lists of judgement and advice
words could fail the sentence. The fix (`model/verifier.py`, `model/lexicon.py`) inverts that. A
text that names a regimen the run uses, every model text of a run that names one, and any text
with a treatment noun ("regimen") may use only plain descriptive words
(`lexicon.DESCRIPTIVE_WORDS`), and code reads the frames those words can still build (the regimen
offered to or tried on a group, cases placed in its group, its effect, a survival comparison that
names no comparison group). A regimen is a name only where the text uses it as the name of a
group, never as the answer ("It is <regimen>"), alone, or as the bare object of a verb. Regimen
short names are regimen values, brand names and care words are clinical words, and V-ENT, V-NUM
and V-SEC read text as a reader sees it (`lexicon.canonical_text`).

The reported false positives that only describe the data now pass ("<regimen> was the most common
regimen among the cases", "Cases receiving <regimen> may have longer survival than the remaining
cases", "… with no advantage for either"). Those that judge the regimen or describe care stay
failing, each with a descriptive wording that passes: "VRd" or "KRd" as another group (a regimen
value the run does not use), monitoring, switching, response, progression and adverse events, a
negated benefit ("no sign of a survival advantage for …"), "choice" and "chosen", and a survival
comparison with no comparison group. C-31's guard topics now also catch further rewordings of
E23, E18, E12 and E17 (expression subgroups, "embed", "in both KRAS groups", "number of patients
in my cohort", "be treated with"); no example matched them. The planner's Clinical advice example
("What dose of lenalidomide should my patient take?") stays: E17 asks which drug a patient with a
named mutation should get, prompts.json holds no paraphrase of it, and the guard does not match it.

The review itself made no model call, and the prompts stay `orion-prompts@5`. The change reaches
only text that names a regimen or a treatment noun, the new clinical words, and look-alike
spellings, which no eval or E2E scenario writes. Offline gates (00:30–00:45 UTC): the agent suite
passes 764 tests (6 live tests skipped), mypy `--strict` is clean on 94 files,
`build_fixtures.py --check` reports all fixtures up to date, and the evals unit tests pass 120
with `orion/scripts` mounted.

`VSecRegimenTextTest` (`tests/test_verifier.py`) fails 171 times on bb7c6e0's verifier: 84
bypasses and 63 other advice cases pass there, and 15 descriptive texts fail there
(`orion/evals/reports/vsec-tests-on-bb7c6e0-20261106T0115Z.txt`). 129 of its 131 bypasses had
passed the first fix's verifier in the skeptics' recorded probes. Those outputs are in the session
scratchpad, not with the reports.

The final proof below ran this code on the stubs and on Bedrock. No model text was rejected.

## Final proof on the final code (2026-11-06, 00:54–01:43 UTC)

This is the last check of stage 2. It ran on the working tree as it stands: bb7c6e0 plus the
second and third reviews' fixes, with `orion-prompts@5`. Every gate passed, so no code, prompt,
verifier rule, contract rule, check or gate changed. This step edited only these notes,
`bedrock-evals.md` and the agent README.

`orion-local.sh up --stub` and then `up --bedrock` rebuilt orion-agent from the working tree. One
sha256 over every `.py` and `.json` file of `orion_agent` was `677e75c8f7799f7b` in the working
tree and in both containers. The fork is unchanged at 3fc0e6a, so the frontend was not rebuilt
and its jest, `tsc` and eslint were not rerun. The console results of every step are saved in
`orion/evals/reports/final-proof-console-20261106T0054Z.txt`.

| Step | Time (UTC) | Artifact | Result |
| --- | --- | --- | --- |
| Agent suite (`unittest discover -s tests`) | 00:54:33–00:54:53 | `final-proof-console-20261106T0054Z.txt` | 764 tests OK, 6 live tests skipped |
| mypy `--strict` on `orion_agent` and `tests` | 00:55 | the same | clean, 94 files |
| `build_fixtures.py --mirror /fork --check` | 00:55 | the same | all fixtures up to date |
| Evals unit tests with `-v $PWD/orion/scripts:/orion-scripts:ro`; evals mypy `--strict` | 00:55:36–00:55:50 | the same, `oracle-live-test-20261106T005540Z.json` | 120 OK, none skipped; mypy clean on 35 files |
| `orion-local.sh up --stub` (container created 00:56:09) | 00:56 | the same | "planner: stub, composer: stub" |
| Stub browser E2E (`node orion/scripts/e2e-mode-l.mjs`) | 00:56:38–00:57:20 | `summary-stub-20261106T005720Z.json` | 11 of 11 scenarios, 72 checks, 4 skipped (the Bedrock-only ones) |
| `evals-mode-l.sh agent` (stubs) | 00:57:41–01:03:00 | `orion-evals-20261106T005741Z` | 23 pass, 0 fail, E22 not run |
| `orion-local.sh up --bedrock` (container created 01:05:05) | 01:05 | the same | role `WSParticipantRole`; "planner: bedrock, composer: bedrock" |
| **Evals, Bedrock, 5 repeats** (`evals-bedrock.sh evals`) | **01:05:31–01:36:25** | **`orion-evals-20261106T010531Z`**, `usage-20261106T010531Z.json`, `agent-log-20261106T010531Z.txt`, `plan-times-20261106T010531Z.txt` | **24 of 24 pass, 9 of 9 gates; plan p95 4.55 s over 94 asks: pass.** 192 calls, all ok at attempt 1; 0 of 89 answers re-composed; no V-SEC finding |
| V-SEC checks against bb7c6e0 (offline, while the evals ran) | 01:09–01:11 | `vsec-tests-on-bb7c6e0-20261106T0115Z.txt`, `vsec-review-probe-20261106T0115Z.json` | `VSecRegimenTextTest` fails 171 times on bb7c6e0's verifier; the review's 52 probe cases all fail on the final verifier; the C-31 guard catches every synonym the review listed |
| **Browser E2E, `ORION_E2E_MODEL=bedrock`** | **01:36:44–01:42:41** | **`summary-bedrock-20261106T014241Z.json`**, `agent-log-e2e-bedrock-20261106T014241Z.txt`, `e2e-calls-20261106T014241Z.txt` | **15 of 15 scenarios, 118 checks**; 39 recorded calls, no re-compose |

The V-SEC checks ran in short containers with no network, a few seconds each, during the evals.
They were not near either slow ask (01:23 and 01:30 UTC). During the E2E, only read-only
containers ran, to count the eval report's calls from the run records.

What the final proof shows:

- **The third review's verifier rejected no model text.** None of the 89 eval answers or the 15
  E2E answers was re-composed, and neither agent-log excerpt has a V-SEC finding or a dropped
  sentence. Every plan signed the same plan as in `220702Z`, and the stub planner's plan for J1,
  J2 and J3. No eval or scenario names a regimen, so the descriptive-word check itself did not run
  on Bedrock. The rules every text takes did run on every answer: the new clinical words, the
  regimen abbreviations and `lexicon.canonical_text`.
- **NFR-01 passed again: p95 4.55 s over 94 asks.** Two asks were over 5 s, at 5.08 and 7.05 s.
  Each was one slow first planner attempt, with the same request, output and signature as the
  other runs of its paraphrase (`bedrock-evals.md`, "Final proof on the final code"). Over five
  5-repeat runs the gate passed three times and failed twice, so Known limits 8 still holds. The
  target is unchanged.
- **Bedrock calls:** 192 recorded for the evals and 39 for the E2E (37 completed, 2 cancelled by
  the Stop scenarios). The E2E's 19 warm-up turns made 38 more calls, which are not recorded. The
  agent log shows 267 bedrock-runtime responses, all HTTP 200 from
  `us.anthropic.claude-opus-5-5`, with no retry and no fallback.
- **Other AWS calls:** two `sts get-caller-identity` (`up --bedrock` and the `evals` check). No
  AWS resource was created or changed.

This step did not touch stage 3's working-tree changes (`orion/infra`, `helm`, `sources.json`,
`terraform/.terraform.lock.hcl`) or its deployed resources. orion-agent is left running in Bedrock
mode: the container created at 01:05:05 UTC holds the credentials exported then. `up --stub`
returns it to the stubs.

## Fourth check: treatment-defined runs get no model text (2026-11-06, offline)

Two fresh skeptics attacked the allow-list V-SEC above (467 and more probes, offline, no model calls). Advice still got
through when written only in descriptive words, or carried by a pronoun or ellipsis from an earlier sentence, for
example "<regimen> could be the plan for the mutated cases.", "Match the mutated cases to <regimen>." and "It may be
the next step for the mutated cases." A word-level check cannot close this: each round narrows it, none ends it.

The fix is structural. `CodeVerifier.treatment_defined` (the verifier's existing `treatment_run` flag: the run's own
scope, cohorts or settings name a regimen or a therapy value) is checked in `model/answer.write_answer` before the
composer is called. Such a run makes no composer call and ends ObservedOnly, with the reason "this run compares
groups defined by a treatment, so ORION writes no interpretation of it" and a `CLINICAL_ADVICE` limitation. Observed
and Limitations are still written by code. Every other run is unchanged, and the word-level V-SEC still applies to
it. No eval or browser E2E scenario defines a run by a treatment, so no measured result changes, and no model call
was made for this fix. Tests: `test_model_answer.TreatmentRunTest` (fails without the guard) and
`test_verifier.TreatmentDefinedTest`. Agent suite 767 OK (6 live tests skipped), mypy clean, fixtures current.
