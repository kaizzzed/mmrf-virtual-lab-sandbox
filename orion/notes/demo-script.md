# ORION demo script: J1, J2 and J3 in mode L

The day-2 demo of RFA rev 25 on the local Compose stack (mode L: the agent in the `orion-agent`
container, in-process tools against `http://identity:8080`, run records on the `orion-runs`
volume). It covers the three reference journeys, the Stop, Don't save and edit moments, a fallback
for each cut item, and the reset between rehearsals. Screen words are the design's (F2 "Run and
step states", the storyboard and the interaction rules).

All data is synthetic. Every answer ends with the notice "Synthetic sandbox — all data is
fabricated and has no clinical meaning." Say so once at the start.

Section 7 runs the same demo on the hosted stack in mode A (AgentCore on AWS, through
CloudFront). Its status is in `mode-a-status.md`.

## 0. Before the demo (15 minutes before)

1. **Stack up.** The sandbox stack (`mmrf-synthetic`) is healthy, then mode L is up:
   `orion/scripts/orion-local.sh up`, then `orion/scripts/orion-local.sh status` shows
   `orion-agent` healthy. Planner and composer: the stubs by default
   (`ORION_PLANNER=stub ORION_COMPOSER=stub`, no AWS). The stub planner answers the six starter
   prompts; typed paraphrases need the Bedrock planner: `orion/scripts/orion-local.sh up --bedrock`
   (the sandbox profile's short-term credentials, Claude Opus 5.5 in us-east-1; `status` then
   reads "planner: bedrock, composer: bedrock"). Pick one setup and rehearse in it.
2. **Rehearsal gate.** Two checks, both against the running stack:

   - The three open-ended journeys through the eval harness (agent direct), then read the report
     (`orion/evals/reports/orion-evals-*.md`):

     ```sh
     orion/scripts/evals-mode-l.sh agent --evals E1,E3,E5 --repeats 1
     ```

     E1, E3 and E5 should not fail. A fail names the check (a count against the oracle, a missing
     card); fix it or take the fallback for that journey (section 5) before going on stage. Each
     reference journey leaves a run (and J2 and J3 their cohorts in the simulated browser only).
     In the Bedrock setup, `orion/scripts/evals-bedrock.sh evals --evals E1,E3,E5,E22 --repeats 1`
     first checks the credentials and that the agent runs on Bedrock, and E22 adds the held-out
     paraphrases; results of the full Bedrock run are in `orion/notes/bedrock-evals.md`.
   - The real app in a headless browser, every moment of this script (about 1 minute):

     ```sh
     node orion/scripts/e2e-mode-l.mjs
     ```

     It should end "11 of 11 scenarios passed". Its screenshots, one per beat, are in
     `orion/notes/screenshots/e2e/` (gitignored); each scenario uses a fresh browser profile, so
     your own browser is untouched. In the Bedrock setup run
     `ORION_E2E_MODEL=bedrock node orion/scripts/e2e-mode-l.mjs` instead (about 6 minutes and 80
     model calls): it should end "15 of 15 scenarios passed", Stop while "Planning" and while
     "Writing interpretation" included (`orion/notes/bedrock-status.md`).
3. **Today's numbers.** `python -m orion_evals oracle` (same container) prints the values the demo
   must show. On 2026-11-05 (fingerprint `80ac2c93…`) the oracle computed:

   | Journey | Oracle values |
   | --- | --- |
   | J1 | High risk 339 cases, 299 events; Standard risk 661 cases, 300 events; log-rank p 7.1e-60; Kaplan–Meier medians 1,147 and 3,315 days |
   | J1 on the race cohort (E14) | High risk 98 cases, 82 events; Standard risk 236 cases, 110 events; p 7.1e-14 |
   | J2 | 334 cases; KRAS 148 of 334, NRAS 113, DIS3 100; no tie at the cutoff |
   | J3 | 1,000 mutation-tested; 480 KRAS-mutated; 520 KRAS not detected; 0 outside; RNA-seq eligible 520 and 480; 16 of 7,684 genes past the thresholds; top 10 BRAF, ETV4, JUN, DUSP6, FOS, TP53, MKI67, PCNA, MAPK1, MYC |

   If the fingerprint differs, the stack's data changed: use the new numbers.
4. **Browser.** A fresh private window, at least 720 px wide (narrower, the cards show a summary
   instead of the live chart). Open `http://localhost:3080/?app=Assistant`. The banner and the six
   starters show ("Try a reference question"); the composer's scope chip reads
   `Scope: All cases · 1,000 cases`.
5. **Second tab for evidence.** Keep the latest eval report open in another tab for the "how do we
   know" questions (gates, oracle values, run ids).

Do not narrate the direction of a fold change ("higher in KRAS mutated") yet: the catalog has not
verified ProteinPaint's sign against the expression data (TOOLS.md open issue). Read the numbers.

## 1. J1: survival by CGS risk (about 3 minutes)

| # | Click or type | What the screen shows |
| --- | --- | --- |
| 1 | Click the starter **J1 · open-ended** ("Compare survival of CGS high-risk vs. CGS standard-risk patients"), then Enter | The question in the thread. Send turns into **Stop**. The run header reads "Planning". With Bedrock, open the Assistant about 5 s before the first question: a question asked while the opening warm-up turn runs waits for it, and the plan then takes 7–11 s instead of about 3 s |
| 2 | Wait | The **plan card**, "Plan, version 1": the scope line `Scope: All cases · 1,000 cases` (the count carries the chip "Row 1 · s1 fixture_fingerprint"), then row 1 "Count both CGS risk groups" (Cohort Builder counts filter cohorts) and row 2 "Compare survival: Kaplan–Meier, split by CGS risk category" (Correlation Plot runs survival comparisons). Assumptions: "No cohort named: using the active cohort, All cases" and "High risk vs. Standard risk from CGS risk category". The header reads "Running". Point out: the plan was on screen before the first call (FR-05) |
| 3 | Watch the rows | Rows tick off with durations: s1 (the fixture fingerprint, folded under row 1), s2 "CGS risk category = High risk" → "339 cases", s3 → "661 cases", s4 "2 groups, Kaplan–Meier" → "2 curves, log-rank p reported", s5 "2 curves" → "2 medians" |
| 4 | Look at the viz card | **Overall survival by CGS risk category**: the native Correlation Plot survival chart, live ("Live · ready in …" in the card header), two curves with group labels, "Pinned to All cases · cites s4". The parity badge reads "Parity: matches s4 survival_compare". Caption under the chart: "The chart's log-rank table shows p = 0; the answer's p-value comes from Gen3 Analysis." The card lists what to set in Correlation Plot to match it (cohort, survival variable, overlay). Hover a curve; click **Expand** to widen it in place |
| 5 | Read the answer card | Header "Writing interpretation", then "Complete". **Observed**: "High risk: 339 cases, 299 events." "Standard risk: 661 cases, 300 events." "Log-rank p = 7.1e-60 (Gen3 Analysis)." "Median overall survival: High risk 1,147 days; Standard risk 3,315 days (Kaplan–Meier median derived by ORION from the engine's curves)." Then **Interpretation**, **Hypotheses and next steps** (the follow-up chips "Survival by gender" and "Top mutated genes in this cohort", and the link "Open Cohort Comparison"; with the Bedrock composer the text, chips and links are the model's own, such as "Top mutated genes in CGS high-risk cases" and "Survival by ISS stage", and "Writing interpretation" lasts about 8–15 s), **Limitations** ("The survival chart shows this p-value as p = 0…", "The platform reports no hazard ratio…"), and the notice |
| 6 | Click the chip beside **339** ("Row 1 · s2 cohort_count") | The Evidence Inspector opens on its heading, with "Row 1 · s2 cohort_count": the step, the fact (value, pointer, call, engine and the value found in the call), the other facts from this step, and the call ("Attempt 1 · ok · … · in_process") with its request and output. Show **Export the run record as JSON**. Esc closes it and puts focus back on the chip |

Say: every number on screen is a chip that names its row and step; the eval harness checked these
counts against its own computation from per-case clinical data and its own log-rank test.

### The Stop moment (on J1)

Stop is visible only while something takes time. Locally the tools answer in under 2 s, and the
stubs answer at once, so stop where a model call runs, or during J3's `de_run`:

- **With the Bedrock composer:** ask J1 again in a new thread (**New thread**), and press **Stop**
  (or Esc in the composer) while the header reads "Writing interpretation". The header reads
  "Stopping" at once, then "Stopped by you". The finished rows stay, labelled partial; Observed and
  Limitations stay; the answer reads "Interpretation withheld: you stopped the run".
- **With the Bedrock planner:** press **Stop** while the header reads "Planning". "Stopping", then
  "Stopped by you"; no plan is shown and no tool ran.
- **With both stubs:** only J3's `de_run` runs long enough, and only on a cache miss. Ask J3, and at
  "Waiting for you" rename "KRAS mutated (ORION)" to a name not used before (the edit moment of J2,
  below): ProteinPaint then has no cached result for the new group name and `de_run` takes about
  1.5 s instead of 0.6 s. Click **Create** and press **Stop** as row 6 reads "Running". The button
  reads "Stopping", then the header "Stopped by you"; rows 1–4 read "Reused · from version 1", row 5
  "Done", and `s9 de_run` "Skipped · Not run: stopped" (its aborted call is in the record, "CANCELLED:
  stopped"). The answer keeps Observed and reads "Interpretation withheld: you stopped the run". If
  the click is late the run completes instead; show the E2E screenshot `edit-stop-02-end.png` or
  E24's "Stop during de_run" line in the eval report.

## 2. J2: top mutated genes, one save (about 3 minutes)

| # | Click or type | What the screen shows |
| --- | --- | --- |
| 1 | **New thread**, click the starter **J2 · open-ended** ("Top 3 most frequently mutated genes in Black or African American patients"), Enter | The plan card: `Scope: All cases · 1,000 cases`; row 1 "Find Black or African American cases" (Gen3 Analysis API lists case IDs), row 2 "Rank mutated genes: top 3" (OncoMatrix ranks mutated genes), row 3 "Save 1 cohort" (Cohort Builder saves cohorts after your click) |
| 2 | Wait | Rows 1–2 tick off ("334 case IDs, 1 page", "3 genes", "Top 3, no ties"). Row 3 reads "Needs you"; the header reads **"Waiting for you"** |
| 3 | Look at the confirmation card | "Save 1 cohort", focus on its heading, not on a button: "Nothing is saved until you choose Create. Don't save continues with unsaved groups." One cohort: "Black or African American (ORION)", its definition "Race = black or african american", its case count **334** with the chip "Row 1 · s2 cases_query", an unchecked box "Make Black or African American (ORION) the active cohort after saving", and **Rename**. Buttons **Create** and **Don't save**; no default button (Enter on the card does nothing). Point out: nothing has been saved (FR-07) |
| 4 | Click **Create** | Row 3 runs, then "Done": "1 cohort saved and read back". The **OncoMatrix** card "Top mutated genes: Black or African American" shows KRAS, NRAS and DIS3, live, with "Parity: matches s3 top_mutated_genes"; it lists the settings to re-apply in OncoMatrix (the cohort and the genes in order) |
| 5 | Read the answer | **Observed**: "The cohort has 334 cases." "The most frequently mutated genes are KRAS (148 of 334 cases), NRAS (113) and DIS3 (100)." **Limitations**: "Mutation data covers the six-gene panel only." Interpretation and hypotheses, the follow-up chip "Survival by KRAS mutation", an "Open Mutation Frequency" link |
| 6 | Open the cohort selector in the cohort bar | "Black or African American (ORION)" is in the cohort list beside "Cohort", like any saved cohort; the active cohort did not change |
| 7 | Click **Make active and open** on the OncoMatrix card (it reads "Open with this cohort" when the cohort is already active) | The full OncoMatrix opens in the same tab with the saved cohort active (the cohort bar reads "334 CASES") and a notice "Active cohort: Black or African American (ORION)." with **Undo** (the card listed the settings to re-apply). Browser **Back** returns to the thread, restored |

If the race cohort was already saved (a rehearsal leftover), the card says
"Already saved as <name>" and Create reuses it: no duplicate. Mention it if it happens.

### The edit moment (on J2, at the pause)

Ask J2 again in a new thread. At "Waiting for you", click **Rename** beside the cohort, type a new
name and click **Use this name** (Enter in the name field does nothing). The plan card reads
**"Plan, version 2"** with "Replaces version 1: an edit made this plan; finished steps whose inputs
did not change are reused."; the pending save of version 1 is voided and a new confirmation card
appears with the new name. Step reuse (stretch 13) landed: rows 1–2 read "Reused · from version 1".
Then click **Create**. The same edit works from the plan card: **Edit settings**, Top N 3 → 5,
**Apply** makes version 2, which ranks five genes (KRAS, NRAS, DIS3, MYC, BRAF).

## 3. J3: differential expression by KRAS status, two saves (about 5 minutes)

This is the storyboard of F2 ("J3, open-ended phrasing"), beat by beat.

| Beat | Click or type | What the screen shows |
| --- | --- | --- |
| 1. Ask | **New thread**, click the starter **J3 · open-ended** ("Differential expression between patients with and without a KRAS mutation, top 10 genes"), Enter | The question; the scope chip `Scope: All cases · 1,000 cases`; Send becomes Stop |
| 2. Plan | Wait | A scope line above six rows, each naming its tool: 1 "Find KRAS-mutated cases", 2 "Find mutation-tested cases", 3 "Subtract to get KRAS not detected", 4 "Check RNA-seq-eligible group sizes", 5 "Save 2 cohorts", 6 "Run DE: Wilcoxon, rank by adjusted p". Assumptions include the default scope, "Without a mutation means mutation-tested cases without it; cases without mutation data are left out and counted.", "Method: Wilcoxon rank-sum", "Genes ranked by adjusted p-value", the viewer's thresholds and the control group. Read steps start at once |
| 3. Read steps | Look at row 3 (each step's **Show JSON** opens its full step) | Rows 1–3 tick off: "480 case IDs", "1,000 case IDs, 1 page", "All 480 inside the mutation-tested cases", "520 case IDs". Row 3 shows both input counts and the difference, computed in code (FR-10) |
| 4. Pre-analysis | Watch row 4 | "520 and 480 samples", then "520 and 480 RNA-seq eligible". Nothing to approve here; a group under 10 would stop the run before any save (that is eval E9) |
| 5. Confirm | Look at the card | Header **"Waiting for you"**; row 5 "Save 2 cohorts" reads "Needs you". The card: "KRAS not detected (ORION)" with 520 cases and 520 eligible, and "KRAS mutated (ORION)" with 480 and 480, every count a chip, an unchecked make-active box each (only one can be ticked). Click **Create** |
| 6. Chart | Watch | Row 5 "2 cohorts saved and read back"; row 6 runs. The native DE viewer **Differential expression: KRAS mutated vs. KRAS not detected**, already run with Wilcoxon on the two saved cohorts, "Parity: matches s9 de_run", and the caption "Table sorted by fold change. Sort by adjusted p-value to match the answer." Open **Viewer requests**: "POST /termdb/DE … server cache: same entry as s9 de_run" (the chart read the very result the answer cites). Open the p-value table and sort by adjusted p |
| 7. Answer | Read | Header "Writing interpretation", then "Complete". **Observed**: "Mutation-tested cases: 1,000; KRAS-mutated: 480; KRAS not detected: 520." "Groups: KRAS not detected (ORION) with 520 RNA-seq-eligible samples (control), and KRAS mutated (ORION) with 480 (case)." The method line. "16 of 7,684 tested genes passed the viewer's thresholds." "Top-ranked genes by adjusted p-value: BRAF (log2FC -1.78, adjusted p 5.9e-131); ETV4 (log2FC -1.77, adjusted p 4.6e-130); JUN …; MYC (log2FC -1.83, adjusted p 2.9e-86)." Limitations: the six-gene panel line. Click a chip: "Row 6 · s10 rank_top_n" in the Evidence Inspector |
| 8. Follow up | Point at the chips | "Show the top 20" and "Survival by KRAS status". Optionally switch the active cohort in the cohort bar, then type "Show the top 5 instead": the plan names both scopes, the thread's KRAS cohorts and the new active cohort, with one switch chip (E15) |

Say: the group sizes and the top 10 equal what the eval oracle computed with its own requests and
set arithmetic (the J3 count-accuracy gate).

### The Don't save moment (on J3)

**New thread**, J3 again. At "Waiting for you", click **Don't save**. Row 5 reads "Skipped · Not
saved: you chose Don't save"; DE runs on the unsaved groups and the same answer appears with
the limitation "You chose Don't save, so no cohort was saved: the chart uses the unsaved groups and
Open with this cohort is off." The card's **Open with this cohort** is disabled, with that line
beside it. The cohort list is unchanged.

### A named tool that does not fit (optional, 1 minute)

Type "Use OncoMatrix to plot survival by CGS risk category". The header reads "Needs your choice";
the card "Use Correlation Plot instead of OncoMatrix?" takes focus on its heading and says
"Nothing has run yet. The plan above shows what Correlation Plot would do."; row 2 names both
tools ("Correlation Plot runs survival comparisons; OncoMatrix, which you named, ranks mutated
genes"). Click **Cancel**: "Stopped by you", every row "Skipped · Not run: you declined the
substitution", no data call. Or **Use Correlation Plot**: the J1 steps run.

## 4. Questions the audience may ask

| Question | Show |
| --- | --- |
| "How do you know the numbers are right?" | The eval report: E1–E6 oracle rows (oracle against observed), the oracle section, the gates |
| "What if it cannot answer?" | Type "Show a Venn diagram of the two KRAS cohorts": "This question is not supported": "ORION cannot run this analysis. It compares survival, ranks mutated genes, runs differential expression and counts cohorts.", "Nearest app: Set Operations" and **Open on this cohort** (it opens Set Operations in the same tab; Back restores the thread). Or "Which drug should a KRAS-mutated patient get?": "Not supported", "ORION gives no clinical or treatment advice.", no data call. A gene outside the panel (IDH1) ends "Stopped by a limitation" with what would unblock it |
| "What did it actually call?" | Any step row → the Evidence Inspector → Export the run record |
| "Does the same question give the same plan?" | The report's signature checks (5 of 5 per prompt, both phrasings). With the stub planner this holds for the six starters and the edge questions only: typed paraphrases need the Bedrock planner (E22, not run in mode L) |
| "Is the app tested end to end?" | `orion/notes/mode-l-status.md`: the browser E2E (`orion/scripts/e2e-mode-l.mjs`) and the eval reports, agent direct and through the BFF |

## 5. Fallbacks for each cut item

The design's cut list, in order, and what the demo does if that cut was taken.

| Cut or gap | Demo fallback |
| --- | --- |
| Cut 1: Gateway → in-process tools | Mode L already runs in-process tools; nothing changes on screen. Mention that modes A and H put the same handlers behind the Gateway Lambda |
| Cut 2a: direct DE mount → DEinput fallback (`ORION_DE_LAUNCHER=deInput`) | At beat 6 the DE card opens with both saved cohorts as its groups and pre-analysis shown; its caption asks you to choose Wilcoxon and click Run. Click **Run**: the viewer computes the same table. After Don't save the card stays the direct form and hand-off is off |
| Cut 2b: → a static image | The DE card shows its frozen top-10 table (Gene, log2FC, Adjusted p, all chips) and the note; show the full viewer with **Open with this cohort** |
| Cut 3: mode A → mode H | Same script in the browser; the tools read AWS while the viewers read the local stack, and each card compares the local fingerprint and freezes, saying why, if they differ |
| A viewer fails to mount | The card keeps its title, caption and frozen table; say the chart is the platform's own and open it with **Open with this cohort** |
| Bedrock unavailable (or the credentials expired) | Restart with the stubs (`orion/scripts/orion-local.sh up --stub`, which recreates only the agent) and use the starters. If only the composer refuses, answers read "Interpretation withheld: …" with Observed intact: say that is the designed behaviour (E11) |
| Stretch 1 not built (Open in tool carries settings, FR-22) | The card lists the settings to re-apply; read them out when the full tool opens |
| Stretch 13 not built (step reuse) | An edit runs every step again; the rows show "Running" instead of "Reused" |
| The agent will not start | Show the latest eval report and a record in the Evidence Inspector's export; do not improvise answers |

## 6. Reset between rehearsals

1. **Threads and the browser's cohorts.** Close the private window. Threads live in that tab's
   sessionStorage (`orion:assistant:v1`) and the saved cohorts in that browser's state, so a new
   private window starts clean. In a normal window instead: delete the demo's cohorts (names ending
   "(ORION)") from the cohort list in Cohort Builder, then clear the site data for
   `localhost:3080`.
2. **Run records.** `orion/scripts/orion-local.sh down --volumes` removes `orion-agent` and the
   volume `mmrf-synthetic_orion-runs`; `orion/scripts/orion-local.sh up` starts it empty. Keep the
   volume instead if you want to reopen earlier runs in the Evidence Inspector. Never `down` the
   sandbox stack itself.
3. **Check.** `orion/scripts/orion-local.sh status` is healthy, the oracle fingerprint is
   unchanged (`python -m orion_evals oracle`), and a new private window shows the six starters
   with `Scope: All cases · 1,000 cases`.

## 7. Mode A: the same demo on AWS

Mode A is the hosted sandbox with ORION on. Each piece runs in AWS:

- the agent runs in the AgentCore Runtime `orion_agent` (version 4 since 2026-11-06 05:36 UTC,
  endpoint `orion_live`);
- each data call goes through the AgentCore Gateway `orion-vlab` to the Lambda
  `orion-vlab-tools`, which reads this same hosted stack through CloudFront;
- run records are in S3;
- the planner and composer are Claude Opus 5.5 on Bedrock, as in the Bedrock setup of section 0.

The screens, words, numbers and beats are those of sections 1–3. Only the waits differ. What was
verified, and the timings below, are in `mode-a-status.md`. The resources stay up until the user
runs `mode-a-teardown.md`.

### Before the demo (15 minutes before)

1. **Is it up?** These are read-only operator checks with the user's AWS profile. The stack itself
   runs on its own roles, so expired operator credentials do not stop it.

   ```sh
   aws bedrock-agentcore-control get-agent-runtime-endpoint --region us-east-1 \
     --agent-runtime-id orion_agent-RE0ngY6qRl --endpoint-name orion_live --query '{status: status, live: liveVersion}'
   ```

   Expect READY at live version 4.
2. **Open it in your own browser.** Use a fresh private window, at least 720 px wide, at the
   sandbox's `https_url` (`terraform -chdir=terraform output -raw https_url`) followed by
   `/?app=Assistant`.
   - The site asks for the team login. Only you type it.
   - The login is in `generated/aws/access.json`. Open that file only on your own screen, never
     while sharing, and never paste it into a chat.
   - You should see the banner and the six starters, with the scope chip reading
     `Scope: All cases · 1,000 cases`.
3. **Rehearse one question through the real path.** Ask J1.
   - The plan card shows.
   - The rows tick off one by one.
   - "Writing interpretation" appears before the Interpretation.

   That is the check that CloudFront streams the answer and does not buffer it. The user made it
   on 2026-11-05. An automated rehearsal runs through the operator's loopback port-forward, never
   during the demo (`mode-a-status.md`, "How to rerun everything"):
   `SANDBOX_ORIGIN=http://127.0.0.1:3081 ORION_E2E_MODEL=bedrock ORION_E2E_MODE=A node
   orion/scripts/e2e-mode-l.mjs j1-open j3`.
4. **Today's numbers** are the oracle values of section 0, step 3. The hosted stack's fingerprint
   `80ac2c93…` matched the oracle in mode A (`orion-evals-20261106T040956Z`).
5. **No deploys on demo day.**
   - `aws_sandbox.py deploy` re-runs Helm with `values.json` alone, which turns ORION off.
   - A new Runtime version makes its first call wait about 9.5 s.
   - If one was unavoidable, ask one rehearsal question after it.

### Waits to expect

| Moment | Expect (final mode A run, 2026-11-06, Runtime version 3) |
| --- | --- |
| Opening the Assistant, or **New thread** | A warm-up turn of about 5 s runs in the background. Each thread is its own Runtime session |
| A question asked in those first 5 s | The plan shows after 8–11.5 s: the question waits for the warm-up |
| A question asked after that (a typed question, a chip) | The plan shows after about 4 s |
| A plan-card edit (version 2) | 0.3 s |
| "Writing interpretation", J1 or J2 | 7–11 s |
| "Writing interpretation", J3 | About 22 s |
| J3's `de_run` (row 6) | 1–3 s |
| A cold microVM (rare: 3 of 91 eval asks, all while a second test run shared the Runtime) | 7–9 s more before the plan |

The habit that removes most of the wait: after opening the Assistant or clicking **New thread**,
talk for five seconds (the scope chip, the starters), then click the starter.

The 5 s plan target (NFR-01) is a **Proposal**, and mode A misses it for a thread's first question
(`mode-a-status.md`, Known limits 1). Do not promise it.

### What to show, on top of sections 1–3

- **The same three journeys.** The numbers are the same, read from the same fixture.
- **Where it ran.** Click a chip (for example "Row 1 · s2 cohort_count"), then **Export the run
  record as JSON**. The record's `environment` reads mode `A` and `toolTransport` `gateway`.
  - Server reads such as `cohort_count` went through the Gateway (transport `gateway`).
  - The four computed steps (set operations, subset check, ranking, the Kaplan–Meier median) ran
    inside the agent (`in_process`).
  - `save_cohorts` is the browser's own (`client`), and runs only inside Create.
- **Say it in one sentence:** the agent runs in Amazon Bedrock AgentCore Runtime. Every data call
  goes through AgentCore Gateway to a Lambda that reads this same hosted sandbox, and every run is
  recorded in S3.
- **Stop works as in mode L.** Press Stop while "Writing interpretation" (about 8 s, so easy to
  hit) or while "Planning" (about 4 s). Stop during J3's `de_run` (1–3 s) is tight: prefer the
  writing moment.
- **Optional: a reload.** Reload at J3's save pause or during `de_run`. The thread restores and
  the run finishes.

### What to avoid

- **Running the evals or the browser E2E against the hosted stack during the demo.** They share
  the Runtime. All three cold microVMs of the final evals happened while a second test run was
  going.
- **Showing the team login** (`generated/aws/access.json`, `aws_sandbox.py credentials`), or
  typing it on a shared screen.
- **Using the operator's loopback port-forward (`127.0.0.1:3081`) for the audience.** It needs
  `kubectl` and the EKS API allowlist, which a changed public IP breaks. It also dropped one
  connection in testing. The audience uses CloudFront, which is not IP-restricted.
- **Continuing a thread left idle for more than 15 minutes.** The Runtime recycles an idle
  session's microVM after 900 s, so the next question pays a fresh start (not measured). Click
  **New thread** and wait five seconds instead.
- **Narrating the direction of a fold change**, as in mode L.
- **Reading a treatment question's "Interpretation withheld" as a failure.** Since version 4, a
  run whose groups are defined by a treatment ends Observed only, with the reason that it compares
  groups defined by a treatment. That is by design (`mode-a-status.md`, "The version 4 smoke").

### If mode A fails on stage

| Symptom | Fallback |
| --- | --- |
| The hosted site does not load, or answers fail (Failed, or "Interpretation withheld" on every answer) | Switch to mode L on the local stack (sections 0–3, `http://localhost:3080/?app=Assistant`), in the setup you rehearsed |
| A viewer fails to mount | As in section 5: the card keeps its frozen table; open the full tool with **Open with this cohort** |
| "How do we know it works on AWS?" | `mode-a-status.md`: 15 of 15 browser scenarios and 118 checks, and the evals (22 pass, E11 and E20 not run, the plan gate's miss), on Runtime version 3; version 4 had a smoke test. The beat screenshots in `orion/notes/screenshots/e2e/` are from that mode A run (2026-11-06, 04:09–04:19 UTC) |

### Reset between rehearsals

- **The browser.** Close the private window. As in mode L, threads live in that tab's
  sessionStorage and saved cohorts in that browser, so a new private window starts clean.
- **The records.** Run records stay in S3 and expire after 30 days. There is nothing to reset, and
  nothing to delete.
