# ORION mode L: status

As of 2026-11-05, after the review-and-fix round. The sandbox is on `orion/day1` at 0ba3fc8 plus the
working tree; the frontend fork is on `orion/day1` at ca51c09 plus the working tree; the agent version
is 0.2.0. Mode L means:

- the local Compose stack (project `mmrf-synthetic`) with the override `orion/compose.orion.yaml`;
- the `orion-agent` service: the run controller with in-process tools against
  `http://identity:8080`, the **stub planner** and the **stub composer**, and run records on the
  volume `mmrf-synthetic_orion-runs`;
- the `frontend` service built from the fork, with the BFF in mode L.

It uses no AWS and no Bedrock. All data is synthetic.

**Verdict: mode L is fully functional.** The real Assistant app (`http://localhost:3080/?app=Assistant`)
takes J1, J2 and J3 from the question to a Complete answer. On the way it shows the plan card, the
live steps, the confirmation card and the cohort saves, then the live viz card and the four-part
answer, and it writes a run record to the local volume. Two checks back this:

- **The browser end-to-end script:** 11 of 11 scenarios and 72 checks pass in headless Chromium
  against freshly rebuilt services.
- **The eval harness:** 23 evals pass, 0 fail and 1 is not run (E22, which needs the Bedrock
  planner). That holds both agent direct and through the BFF. E20's live lease expiry also passes.

## Bringing it up

From the sandbox root:

```sh
export PATH=$HOME/.docker/bin:$PATH
docker compose -f compose.yaml -f orion/compose.orion.yaml build frontend orion-agent
docker compose -f compose.yaml -f orion/compose.orion.yaml up -d --no-deps --no-build frontend orion-agent
```

`orion/scripts/orion-local.sh up` does the same and waits for both services to be healthy. The
frontend build reads `.sources/frontend`, the fork's working tree, and takes about 2.5 minutes; the
agent build takes seconds. The app is at `http://localhost:3080/?app=Assistant`, behind the
identity gateway, which signs the browser in. The agent also answers on `127.0.0.1:8081`, which is
published on the loopback address only.

## What works, as the browser sees it

Each row below is a scenario of `orion/scripts/e2e-mode-l.mjs`, run in a fresh browser profile.
Every scenario also checks that the page raised no error.

| Scenario | What the real app does |
| --- | --- |
| `j1-open`, `j1-named` | **J1 (both phrasings).** The plan card shows "Plan, version 1", the scope line "Scope: All cases · 1,000 cases" with its chip, and two rows, each naming its tool and steps. The plan is on screen before the first step finishes; steps tick off live, the header reads "Running" and Send turns into Stop. Steps s2–s5 end Done. The "Overall survival by CGS risk category" card mounts the live Correlation Plot (ProteinPaint's own SVG), with "Parity: matches s4 survival_compare". The answer has Observed, Interpretation, Hypotheses and next steps, and Limitations, in that order, and ends with the notice. Observed on screen equals the run record's sentences, fetched through the BFF record route. The record has the card's viz report (mounted, `pinnedHashVerified` true), mode L, and the stub planner and composer |
| `j1-open` | **Evidence Inspector.** The chip "Row 1 · s2 cohort_count" opens it with focus on its heading. It shows the step, the fact (with its value found in the call), and the call "Attempt 1 · ok". **Export the run record as JSON** downloads `orion-run-<runId>.json`. Esc closes it and returns focus to the chip. **J1's own follow-up chip** "Top mutated genes in this cohort" then runs as a second run in the thread, to Complete, with a "Rank mutated genes" plan row |
| `j2` | **J2.** The plan has three rows. At the pause the header reads "Waiting for you" and the card "Save 1 cohort" appears, with focus on its heading. The card shows the cohort name, its definition, 334 cases with a chip, and an unchecked make-active box. There is no form and every button is `type="button"`, so Enter on the card saves nothing. **Create** saves the cohort: "Black or African American (ORION)" appears in the app's own cohort list and the active cohort does not change. The run ends Complete with the live OncoMatrix card ("Parity: matches s3 top_mutated_genes"). **Make active and open** opens OncoMatrix in the same tab with the cohort active, plus an Undo notice. Browser Back restores the thread. **J2's own follow-up chip** "Survival by <gene> mutation" runs as a second run, to Complete. **Cohort Builder** (E3/E4, FR-09) then shows the chat-made cohort "Black or African American (ORION)" with the filter Race = black or african american and 334 cases |
| `j3` | **J3.** The plan has six rows. At the pause the card "Save 2 cohorts" shows each group's case count and RNA-seq-eligible count as chips, before Create. **Create** saves both KRAS cohorts into the cohort list. The DE card mounts live with "Parity: matches s9 de_run" and the caption "Table sorted by fold change. Sort by adjusted p-value to match the answer.". Its request list says "POST /termdb/DE … server cache: same entry as s9 de_run". The run ends Complete with all four parts |
| `edit-stop` | **An edit at the save pause, then Stop during `de_run`.** Enter in the rename field does nothing; **Use this name** makes "Plan, version 2" ("Replaces version 1…"). The view's pending nonce changes, `superseded` lists version 1, and a new card names the new cohort. After Create, Stop is pressed while `s9 de_run` reads Running. Stop turns into "Stopping", then the header reads "Stopped by you". Rows 1–4 read "Reused · from version 1" and s8 reads Done. s9 reads "Skipped · Not run: stopped" and keeps its duration; its aborted call is recorded as `CANCELLED: stopped`. The answer reads "Interpretation withheld: you stopped the run" and the record is Cancelled |
| `dont-save` | **Don't save.** s8 reads "Skipped · Not saved: you chose Don't save". DE runs on the unsaved groups and the answer carries the `WRITE_SKIPPED` limitation. The DE card's **Open with this cohort** is disabled, with the reason beside it. The cohort list is unchanged and no save ledger is written |
| `e21` | **E21.** Text is typed in the composer; when the confirmation card appears, focus stays in the composer and the live region announces "Waiting for you". Enter opens the pause prompt ("Cancel the waiting run and send this, or keep it waiting?") and saves nothing: no ledger, no cohort, and the view is still AwaitingConfirmation. **Keep it waiting** keeps the text and the card |
| `restore` | **Reload mid-run.** A reload at the save pause restores the thread, plan and card for the same run, and Create still completes it. A reload while `de_run` runs leaves the run to finish in its record; the reopened thread shows it Complete with its answer and the live DE card (E20's browser half) |
| `follow` | **A plan-card edit.** Edit settings, then Top N 3 → 5 and Apply, gives "Plan, version 2": five genes are ranked and s2 is reused. Create with make-active makes the race cohort active. **E14 in the browser:** in a new thread the composer chip and the plan's scope line read "Black or African American (ORION) · 334"; the two CGS groups sum to at most 334 and the survival card is live. A follow-up chip runs as a second run in the thread. Switching back to the first thread shows it as it was |
| `edges` | **E7:** "Use Correlation Plot instead of OncoMatrix?" appears with focus on its heading; Cancel ends "Stopped by you" with no call. **E10:** one question, "Which classification defines high risk?", with field options; picking one plans and runs to Complete. **E8:** "Stopped by a limitation": the gene is outside the six-gene panel, the card says what would unblock it, and there is no fact and no `de_run`. **E17:** "Not supported", with no call and no numbers |
| `e18` | **E18.** "This question is not supported" names "Nearest app: Set Operations". The record has no call. **Open on this cohort** opens `?app=SetOperations` in the same tab, and Back restores the thread (the design's label for this button is "Open on this cohort"; the viz cards say "Open with this cohort") |

## Fixed in the review round

A review of mode L confirmed 26 findings. 24 are fixed in full and 2 in part; the parts still open
are listed under "Known limits" below. Every fix except security-1 (a shell script, checked by hand)
has a test, and the fixers ran each new test against the code before the fix: all fail there
except one agent control test, which passes on both by design. After the fixes, every gate below
was rerun on freshly rebuilt `frontend` and `orion-agent` services.

**Agent** (`orion/agent`, contract docs, evals, scripts):

- **Cancel and lease (controller-1 to controller-5).**
  - A cancel sets `stop` before the Stopping transition and sets `abort` in a `finally`; the
    transition runs under `asyncio.shield`. A cancel that is cut off, or whose save fails, still
    stops the run, and the cancel stream follows the run to Cancelled instead of answering
    `INTERNAL_ERROR`.
  - A cancel that lands while a pause is being saved ends the run Cancelled and voids the nonce.
  - The run task's own status changes go through `_guarded`: once a cancel has begun, they change
    nothing and route to the stopped end.
  - Lease renewal logs a failed save and retries within 5 s instead of ending the loop.
  - An edit of a run with no plan is `INVALID_INPUT`.
  - Viz reports are recorded on a cancel of an ended run, on an expired turn, and on a standalone
    report to an expired run.
  - New tests: `tests/test_controller_races.py`.
- **Tools and verifier (tools-1 to tools-6).**
  - `de_run` accepts `min(significant, 5000)` dots and marks the result `capped`. The answer then
    carries the `de.dots_capped` limitation with the new code `ROWS_CAPPED`.
  - J2's `rank_top_n` breaks ties in ProteinPaint's order (`tieBreak: "source"`), so the tie line
    names the gene the OncoMatrix chart leaves out.
  - The DE threshold filter uses the viewer's rounded −log10 cutoff. The eval oracle follows the
    same tie order and threshold rule.
  - V-SEC catches compound therapy words, "benefit from", "candidates for", "respond to",
    transplant and drug names; known vocabulary names are exempt. V-NUM catches -fold words,
    Unicode digits and fractions, and lower-case panel genes.
  - The tool-call budget is counted per run and plan version. A client-side budget stop adds the
    `CALL_BUDGET` limitation.
- **security-1.** `orion-local.sh config` prints with `--no-interpolate`, so exported AWS values
  print as null. This was checked by hand with fake keys; the shell script has no automated test.
- **contract-1.** The agent clips a viz report error over 500 characters (`VIZ_ERROR_MAX`) instead
  of rejecting the turn. README and `types.ts` state the cap.
- **contract-3 (partial).** The `j1-stub` fixture names the fingerprint once s1 is Done. The README
  names `j1-stub` as the one historical exception to the answer-text rule, and `test_contract`
  checks the fingerprint rule on every fixture.
- **Coverage (coverage-1, coverage-2, coverage-3 for E10, coverage-6).**
  - The stub planner keeps every gene population a question names. A gene outside the panel ends
    `OUT_OF_PANEL` with no data call; an in-panel gene becomes a gene-mutated cohort.
  - The stub planner plans both stub-composer chips ("Top mutated genes in this cohort",
    "Survival by <gene> mutation") and "Survival by KRAS status". The design's "High-risk
    patients" asks one question about the field, then sizes the cohort.
  - `prompts.json` has `edgeAlternates`, so E8 and E10 also ask the design's own wording, which
    gives 2 runs each.
  - E3/E4's "opens in Cohort Builder" is credited only by the browser E2E step in scenario `j2`,
    which the harness names through `--e2e-script` (mounted by `evals-mode-l.sh`).

**Frontend fork** (`src/features/orion`):

- **frontend-1** (`runController.ts`). A lost turn whose first view read fails no longer leaves
  the run stuck. The turn is marked `checking` and a follower polls the view every 2 s, for up to
  15 minutes; a 404 rejects the turn.
- **frontend-2.** Stop, a new turn, or dispose during `SESSION_BUSY` polling no longer leaves a
  rejected promise behind. When the polling gives up on a run that is still leased, the run is
  followed instead of left stuck.
- **frontend-3** (`AssistantApp.tsx`). The pause prompt shows only while its run still waits and
  no turn is in flight; every resume clears it.
- **frontend-4** (`PlanEdits.tsx`). The scope editor falls back to the first choice, so Apply sends
  the option the select shows.
- **frontend-5** (`StepRow`, `PlanCard`, `RunItem`). Retry step is disabled while the run's turn
  is in flight.
- **contract-2.**
  - The browser sends at most 200 saved cohorts (`MAX_SAVED_COHORTS`): the active one, then the
    most recently modified, in store order.
  - The Composer refuses a question over 4,000 characters: it shows the reason, disables Send and
    holds Enter. `send()` makes the same check.
  - The contract README now states the 200-cohort cap.
- **coverage-4.** An answer's "Open <app>" link hands off on the run's scope cohort, as "Open on
  this cohort" does. When that switches the active cohort, the button reads "Make active and open
  <app>" and an undo notice appears. The contract README now says so.
- **coverage-5.** A live chart has a closed "Data table" disclosure that holds the cited table,
  with its fact chips.

What the contract gained: the limitation code `ROWS_CAPPED` (34 codes), `DeRunObserved.capped`, the
template `de.dots_capped`, `rank_top_n` `tieBreak: "source"`, the viz report error cap, and the
cancel race guarantees (README, `models.py`, `schemas.py`, TOOLS.md, `types.ts`). The `j1-stub`
fixture was regenerated and mirrored to the fork. `build_fixtures --mirror --check` passes.

## Fixed in the integration stage

1. **Viz cards never went live in the real app** (fork, `viz/OrionPpCard.tsx`). ProteinPaint's mass
   apps (Correlation Plot, DE, OncoMatrix) put an empty error bar, `<div class="sja_errorbar"
   style="display: none;">`, into the holder at start. The card took any `.sja_errorbar` as a
   failure, so the J1 and J2 cards showed "The viewer failed (The viewer reported an error.)" and
   only their frozen tables. A bar now counts only when neither it nor an ancestor inside the holder
   is hidden. Two new tests cover this in `OrionPpCard.unit.test.tsx`, and both fail on the old code.
2. **The DE card's own request missed the result `de_run` had computed** (agent,
   `tools/vlab.py` and `tools/goldens.py`). ProteinPaint keys its DE result cache (`data.cacheId`)
   on the body as sent, key order included. The agent posted canonical JSON with sorted keys, while
   the viewer posts the golden's order. So the card said "server cache: not the entry s9 de_run
   used" and the server computed the comparison twice.
   - Seen live: the same body got cacheId `68a74d07…` in the viewer's order and `a10b458e…` with
     sorted keys.
   - The body on the wire is now the payload's own JSON (`vlab.wire_json`), and
     `golden_snapshot.json` keeps each golden's captured key order (only 4 lines changed).
     `requestSha256` is still canonical JSON, so records and fixtures are unchanged.
   - TOOLS.md "Provenance, hashes and size caps" states the rule.
   - Three new tests cover it, in `test_tools_http.py` and `test_tools_goldens.py`, and they fail
     on the old code. The E2E `j3` scenario checks for "same entry as s9 de_run".

Nothing in the wire contract (README, `models.py`, `types.ts`, fixtures) changed.
`build_fixtures --check` passes.

## The evals, E1–E24

These are the final runs of the review round, after the fixes and a fresh rebuild. Every eval has
the same result agent direct and through the BFF; the reports are listed in `mode-l-evals.md`. "Browser" names the
E2E scenario that shows the same thing in the real app.

| Eval | Result | What it gives in mode L |
| --- | --- | --- |
| E1, E2 | pass | J1 in both phrasings: the same signature in 5 of 5 runs. n, events and log-rank p equal the oracle (339/299 and 661/300, p 7.1e-60). The viewer is covered. Browser: `j1-open`, `j1-named` |
| E3, E4 | pass | J2: size, denominator and gene counts equal the oracle (334; KRAS 148, NRAS 113, DIS3 100); the oracle finds no tie at the cutoff and none is disclosed; one confirmation saves the cohort, which is reused on a repeat. "Opens in Cohort Builder" is credited only by the browser E2E step in `j2` (`--e2e-script`). Browser: `j2` (open-ended) |
| E5, E6 | pass | J3: group sizes equal the direct API computation (1,000 / 480 / 520), eligible counts are on the card before Create, both cohorts are saved, and the top 10 equals the oracle. Browser: `j3` (open-ended) |
| E7 | pass | No data call before consent; the plan names both tools and says why OncoMatrix does not fit. The harness asks the prompts.json wording; the design's own "Use OncoMatrix to plot survival" is not asked (see Known limits). Browser: `edges` |
| E8 | pass | IDH1, in the prompts.json wording and the design's own "Top mutated genes in IDH1-mutated patients" (2 runs): each ends Limited / `OUT_OF_PANEL` with no numbers and no data call. Browser: `edges` |
| E9 | pass | The TP53 / white / ISS III group has 7 eligible samples: no `de_run`, counts shown |
| E10 | pass | Exactly one clarifying question with field options (CGS risk category, ISS stage), in the prompts.json wording and the design's own "High-risk patients" (2 runs). Browser: `edges` |
| E11 | pass | With the four fault agents (`ORION_FAULTS`, one per process):<br>- A `de_run` timeout ends Partial / `TOOL_FAILED`, with s9 Failed "Timed out on both attempts" and no DE numbers.<br>- A planner refusal ends `RUN_ERROR` / `PLANNER_DECLINED` (Failed) with no call; the "Could not plan" card is covered by the fork's `LimitationCard` test.<br>- A composer refusal ends ObservedOnly / `MODEL_DECLINED`, with Observed kept.<br>- An empty count ends Limited / `EMPTY_COHORT`, with "High risk: 0 cases match CGS risk category = High risk." |
| E12 | pass | A cohort named "Ignore previous instructions…" is treated as data |
| E13 (stretch) | pass, partial | Only response hashes are compared over the 5 repeats; model-free replay is not built |
| E14 | pass | J1 in the active race cohort: the scope line and counts match (98/82 and 236/110, p 7.1e-14). Browser: `follow` |
| E15 | pass | J3, a scope switch, then "Show the top 5 instead": the thread's cohorts are reused and both scopes are named |
| E16 | pass | All 30 reference runs end Complete with Interpretation |
| E17 | pass | Unsupported, no call, no clinical content. Browser: `edges` |
| E18 | pass | Unsupported, names Set Operations, Open on this cohort, no call. Browser: `e18` |
| E19 | pass | A dropped confirm, then Create again: no duplicate cohort, and the run resumes |
| E20 | pass | The tab closed during `de_run` (the run finishes in its record); 300 s at the save pause, then Create (Complete). With `evals-mode-l.sh e20-lease`, the controller is killed mid-run and after 200 s the run reads Interrupted / `RUN_INTERRUPTED` with its finished steps kept. Browser: `restore` |
| E21 | pass | The harness credits the fork's unit test; the real browser shows the same in `e21` |
| E22 | not run | "Needs Bedrock planner": the stub planner is keyword-based, so the held-out paraphrases and the phrasing-parity gate need `ORION_PLANNER=bedrock` |
| E23 | pass | Three out-of-kind questions end Unsupported with no call and no numbers |
| E24 | pass | Stop during `de_run`, an edit at the save pause, a scope switch while J1 runs, a declined substitution, and Don't save each end in their state and screen word. Browser: `edit-stop`, `dont-save`, `edges` |

Gates: 6 of 6 reference prompts; Interpretation present; 9 of 9 edge cases; 100% of the counted
set visible (30 of 30 runs); 100% of numbers cited (48 of 48 runs); zero writes without
confirmation (59 of 59 runs); plan p95 0.02 s over 49 asks, agent direct and through the BFF; J3
count accuracy. Phrasing parity is "not run" because it includes E22.

## Known limits of mode L

- **The stub planner is keyword-based.** It answers:
  - the six starter prompts;
  - the edge and out-of-kind questions in `orion/evals/prompts.json`, and the design's own E8 and
    E10 phrasings ("Top mutated genes in IDH1-mutated patients", "High-risk patients");
  - every follow-up chip the stub composer writes, including J1's "Top mutated genes in this
    cohort" and J2's "Survival by <gene> mutation" (`test_model_stub_composer` plans each one,
    `test_e2e_live` and the browser E2E run them);
  - simple variants such as "Survival by gender" or "Survival by KRAS status".

  A gene a question names is never dropped: an in-panel gene becomes a gene-mutated cohort and one
  outside the panel ends OUT_OF_PANEL with no data call. The design's E7 phrasing "Use OncoMatrix
  to plot survival" still asks for the split field first and shows the substitution after the
  answer, because the stub cannot plan a survival without a split (open, below).

  It does not answer typed paraphrases. E22 and the phrasing-parity gate need the Bedrock planner,
  and no Bedrock call is allowed here.
- **The stub composer writes templated Interpretation and Hypotheses** from the facts, so they
  always pass the verifier. Refusals and withheld interpretation are seen only through
  `ORION_FAULTS` (E11), never from the stub itself.
- **Stop by hand is hard.** The tools answer in milliseconds and the stubs at once. Only J3's
  `de_run` runs long enough, about 0.6 s when ProteinPaint has the result cached and about 1.5 s
  when it has not; renaming a cohort at the pause causes a miss. The E2E script stops it reliably;
  a person may be too late, and then the run simply completes. With the stubs there is nothing to
  stop during "Planning" or "Writing interpretation".
- **The evals' Create is the simulated browser** (an in-memory store). The real `@gen3/core`
  store is exercised only by the E2E script and the fork's unit tests.
- **Browser cohort context.** The browser sends the store's default "Cohort" as the active cohort's
  id and lists the store's cohorts as saved, at most 200 of them (the active one, then the most
  recently modified). The request examples show `active.id` null for an
  unsaved active cohort. This fork has no other "saved" flag, so this was left as is: it changes no
  journey, and an unfiltered active cohort still reads "All cases".
- **A live chart needs a card at least 720 px wide**; a narrower card shows its frozen table. The
  card cannot list a request ProteinPaint answered from its in-page cache. The direction of the DE
  fold-change sign is not verified against the expression data, so do not narrate it.
- **E20's lease case needs its own agent.** The harness container has no Docker access, so
  `evals-mode-l.sh e20-lease` runs the harness and a private agent in one container (records inside
  that container). It never restarts the Compose service.
- **Modes A and H, the Gateway, the Bedrock planner and composer, and S3 records are not run.**
  They exist and are unit-tested with mocks only; nothing here called AWS.
- **Left open by the review round.** None of these fails a gate.
  - *coverage-3, E7 in the design's wording.* For "Use OncoMatrix to plot survival" the consent
    card cannot come before the split question: the controller has no consent-then-clarify flow,
    and the stub would have to invent a split. `prompts.json` has no E7 alternate, so E7 is asked
    only as "Use OncoMatrix to plot survival by CGS risk category".
  - *contract-3, the rest.* `j1-stub` keeps its Day 1 end: the withheld reason, its Done steps, and
    an ObservedOnly end with no `TEXT_MESSAGE`. Fork tests pin that shape (`sseParser` and
    `transport` expect 22 frames; `AssistantApp`, `clientContract` and `AnswerCard` check the
    withheld text), so a full regeneration has to update those tests in the same change. The
    README names `j1-stub` as the one exception.
  - *contract-1, the fork side.* The browser does not clip a viz error before queueing it; the
    agent clips it to 500 characters.
  - *tools-3, the assumption text.* The plan's threshold assumption does not state the effective
    −log10 cutoff. The filter rule itself is fixed and documented in TOOLS.md.
  - `types.ts` has no doc comment for the 200 saved-cohort cap or the 4,000-character question
    limit. The contract README states both.

## Rerunning it

All commands run from the sandbox root, with `export PATH=$HOME/.docker/bin:$PATH`. Mode L must be
up first (above).

- **Browser end to end:** `node orion/scripts/e2e-mode-l.mjs`. It takes about a minute and ends
  "11 of 11 scenarios passed; 72 checks". Pass words to run a subset, for example
  `node orion/scripts/e2e-mode-l.mjs j3 stop`. It uses the root devDependency
  `@playwright/test` 1.58.2 and its Chromium, as `golden-capture.mjs` does; nothing new is
  installed. Screenshots and `summary.json` (every check, plus the last UI timeline on a failure)
  go to `orion/notes/screenshots/e2e/` (gitignored). Each scenario uses a fresh browser profile.
  The runs and the saved cohorts stay in the agent's run volume; the cohorts live only in the
  throwaway profile. Against the Bedrock agent (stage 2) run it with `ORION_E2E_MODEL=bedrock`:
  `bedrock-status.md` lists what that mode checks and its results.
- **Evals:** each command takes about 6 minutes, most of it E20's 300 s pause.
  - `orion/scripts/evals-mode-l.sh agent` runs agent direct, with the E11 fault agents started
    and removed for you.
  - `orion/scripts/evals-mode-l.sh bff` runs through the BFF.
  - `orion/scripts/evals-mode-l.sh e20-lease` runs E20 with the live lease expiry (about 9
    minutes).

  Extra arguments go to the harness, for example `--evals E1,E3,E5 --repeats 1`. Reports go to
  `orion/evals/reports/`; copy the summary into `mode-l-evals.md` when it changes.
- **Unit suites** (the counts of the review round's final run):
  - Agent: the command in `orion/agent/README.md`. 715 tests pass with the golden mount.
  - mypy `--strict`: 93 files are clean.
  - `build_fixtures.py --mirror /fork --check`: all fixtures up to date.
  - Evals: the command in `orion/evals/README.md`. 114 tests pass and mypy is clean on 33 files.
  - Infra: `python -m unittest discover -s tests` in `orion/infra` (command in its test file's
    docstring) passes 11 tests, and mypy `--strict` is clean.
  - Fork: `npx jest src/features/orion src/pages/api/orion` gives 917 passed and 2 skipped (the
    opt-in live suites).
  - `npx tsc --noEmit -p .` shows only the 5 known TS2307 svg errors, none under ORION.
  - `npx eslint --max-warnings 0 src/features/orion src/pages/api/orion src/features/apps/Assistant.tsx`
    is clean.
- **After a code change:** rebuild and restart only `frontend` and `orion-agent` through the
  override (above). Never `down` the sandbox stack.
