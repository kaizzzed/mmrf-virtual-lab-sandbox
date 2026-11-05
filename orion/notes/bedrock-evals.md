# ORION stage 2 (Bedrock only): eval results

The real-model evals of 2026-11-05. The agent ran locally in Compose with in-process tools
(mode L), and both model roles used Claude Opus 5.5 on Amazon Bedrock:

- planner and composer: `us.anthropic.claude-opus-5-5`, with refusal fallback `us.anthropic.claude-opus-5`;
- region `us-east-1`, client `AsyncAnthropicBedrock` on bedrock-runtime;
- effort `low` for the planner and `medium` for the composer;
- a cached system prefix with the 5-minute TTL, and prompts `orion-prompts@3`.

The E11 fault agents ran the stubs and made no model call.

**Every Bedrock run here predates the review round** recorded in `bedrock-status.md`. That round
changed the model client, the verifier (V-SEC's run-scoped exemption, and the causal check on
follow-up chips) and the request encoding (E12). The final verification at 18:21–18:30 UTC could
not rerun them because the profile's credentials had expired. The 5-repeat evals and the warm
timings are still to be rerun on the current tree. The stub-mode evals of that verification gave
23 pass with E22 not run (`orion-evals-20261105T182435Z`).

Where the numbers come from:

- **From artifacts.** The gate and eval results, plan times, call counts, tokens, the per-run composer
  times and the timings tables come from the harness reports in `orion/evals/reports/` (gitignored)
  and the run records on the volume `mmrf-synthetic_orion-runs`. They can be recomputed from those.
- **From ad hoc probes, with no surviving artifact.** These are stated as they were observed at the time
  and cannot be checked now:
  - the schema-probe table, with its compile results and first-use times (10 to 11 s, about 12 s,
    9.0 s);
  - the J1 composer A/B check (4 calls per variant), which is the basis for keeping the word lists;
  - the call estimates marked "about" under "Model calls and tokens".

Rerun everything in one command, from the sandbox root, with the profile `sandbox` signed in:

```sh
orion/scripts/evals-bedrock.sh                      # up --bedrock from the working tree, evals (5 repeats), usage, warm timings
orion/scripts/evals-bedrock.sh evals --repeats 1    # the cheap pass against the running agent
orion/scripts/evals-bedrock.sh timings --mode cold --rounds 2 --journeys J1:open-ended,J2:open-ended,J3:open-ended
orion/scripts/evals-bedrock.sh usage                # model calls and tokens of the latest report's runs
```

## Result

The final run (`orion-evals-20261105T140440Z.md`, 5 repeats per reference prompt, about 30
minutes) had 24 passes, 0 fails and 0 not run. All nine gates passed, including the plan-visible
p95 that the previous stage 2 run failed.

| Gate | Target | Status | Measured |
| --- | --- | --- | --- |
| Reference prompts completed | 6 of 6 | pass | 6 of 6 prompts Complete in every run |
| Phrasing parity | identical signatures in 5 of 5 runs per prompt and per pair, plus the held-out paraphrases | pass | E1–E6 signature checks and E22 (9 paraphrases, 5 of 5 each) |
| Interpretation present | every reference run Complete with Interpretation, 5 of 5 per prompt | pass | E16, 30 runs |
| Honest limits | 9 of 9 edge cases | pass | E7, E8, E9, E10, E11 timeout, E11 refusal, E11 empty count, E17, E18 |
| Visible tool calls | 100% of the counted set | pass | 30 of 30 runs |
| Cited numbers | 100% of numbers in the answer | pass | 93 of 93 runs |
| Zero writes without confirmation | 0 | pass | 104 of 104 runs |
| Plan visible p95 (**Proposal**, NFR-01) | ≤ 5 s over at least 20 runs | **pass** | **p95 4.19 s** over 94 asks (p50 2.89 s) |
| Count accuracy | J3 group sizes equal a direct API computation | pass | E5, E6 |

| Eval | Status | Checks | Runs |
| --- | --- | --- | --- |
| E1, E2 (J1) | pass, pass | 17 of 17, 17 of 17 | 5 + 5 |
| E3, E4 (J2) | pass, pass | 22 of 22, 20 of 20 | 5 + 5 |
| E5, E6 (J3) | pass, pass | 29 of 29, 29 of 29 | 5 + 5 |
| E7, E8, E9, E10 | pass | 7, 8, 5 and 8 of the same | 2, 2, 1, 2 |
| E11 (four fault agents, stubs) | pass | 16 of 16 | 4 |
| E12, E13 (stretch), E14, E15 | pass | 5, 6, 12 and 10 of the same | 1, 30, 1, 2 |
| E16 | pass | 6 of 6 | 30 |
| E17, E18, E19, E20, E21 | pass | 5, 6, 10, 4 and 1 of the same | 1, 1, 2, 2, 0 |
| E22 (held-out paraphrases, three per journey) | pass | 9 of 9, each 5 of 5 runs | 45 |
| E23, E24 | pass | 12 of 12, 29 of 29 | 3, 5 |

Every reference run and every paraphrase signed the stub planner's plan, which is the stage 1
baseline:

- J1: `c0fd027216d5`;
- J2: `8f75d253a542`;
- J3: `3841cfffc278`.

The agent's fingerprint matched the oracle's. Every model call ran on
`us.anthropic.claude-opus-5-5`. No fallback hop and no refusal occurred.

## Rerun with `orion-prompts@4` (after the browser end to end)

The browser end to end (`bedrock-status.md`) found that the composer's J1 follow-up chips named a
risk group without its classification ("High risk cases"). The planner must clarify such a chip, so
composer rule 9 now says a chip stands alone (`orion-prompts@4`). Nothing else changed: the planner
prompt, the verifier, the contract and every check and gate are as above. Both reruns were against
the Compose agent rebuilt from that tree.

| Run | Report | Evals | Gates | Plan p95 | Re-composes |
| --- | --- | --- | --- | --- | --- |
| 1 repeat | `orion-evals-20261105T164252Z` | 24 of 24 | 9 of 9 | 4.65 s over 34 asks | 0 of 28 |
| **5 repeats** | `orion-evals-20261105T170844Z` | **24 of 24** | **9 of 9** | **4.52 s over 94 asks** (p50 3.09 s, max 5.61 s) | **0 of 88** |

In the 5-repeat run:

- the gates measured E16 over 30 runs, cited numbers in 93 of 93 runs and zero writes without
  confirmation in 104 of 104;
- there were 190 model calls: 102 planner calls, all ok, and 88 composer calls, all ok;
- every call ran on `us.anthropic.claude-opus-5-5`, with no fallback and no refusal;
- planner calls took 3.06 s at p50 and 4.44 s at p95; composer calls 9.47 s at p50 and 22.2 s at
  p95;
- tokens: 100,522 output, 243,697 uncached input and 1.72M cache reads.

## How the run got there

| Run | What changed before it | Report | Evals | Plan p95 | Re-composes |
| --- | --- | --- | --- | --- | --- |
| Previous step's full run (reference) | nothing | `orion-evals-20261105T124136Z` | 24 of 24 | 5.71 s over 94 asks (fail) | 19 of 88 |
| 1 repeat | nothing (the agent as handed over) | `orion-evals-20261105T132622Z` | 24 of 24 | 6.51 s over 34 asks (fail) | 9 of 28 |
| 1 repeat | compact planner schema | `orion-evals-20261105T134604Z` | 24 of 24 | 4.68 s over 34 asks | 5 of 28 |
| E5, E6, E15, 2 repeats | composer prompt | `orion-evals-20261105T140147Z` | 3 of 3 | 4.89 s over 6 (not gated) | 0 of 6 |
| **5 repeats (final)** | both | `orion-evals-20261105T140440Z` | **24 of 24** | **4.19 s over 94 asks** | **2 of 88** |

The 1-repeat pass found no failing eval. The only failing gate was the plan p95, and two
answer-quality costs showed up in the records:

1. **Plan latency (NFR-01).** Planner time is almost exactly linear in output tokens: about
   1.4 s plus 10 ms per token (103 calls, warm cache). Thinking at effort `low` added nothing
   measurable: every J1 plan had 192 output tokens. J3's plan was about 400 tokens and 5.5 s,
   and about 170 of those tokens were the wire encoding's empty values. The p95 sat on J3.
2. **Re-composes (Open question 10).** 19 of 88 first composer calls, 22%, were re-composed. 18 of
   those 19 runs were J3 or built on it: E5, E6, the E22 J3 paraphrases, E15's DE follow-up, E20
   and E24. The other one was a J2 paraphrase (E22 J2 paraphrase 3, run 3). A J3 composer call
   took about 21 s, so a re-compose doubled the "Writing interpretation" wait to about 40 s. The
   run record keeps no verifier finding, so the cause was not visible.

### Fix 1: a compact wire encoding for the planner (`model/schema.py`)

`WirePolicy` names where the encoding may depart from "flatten every union, require every
property". Under `PLANNER_POLICY`:

- PlannerOutput's three kinds and an analysis's four kinds stay `anyOf` unions, each kind with
  only its own fields;
- a cohort definition's seven fields and a cohort's name become optional.

The reply is still decoded to the IR and validated in full. A field of another kind, or a
missing field of the reply's kind, is still refused and gets the one repair call.

Bedrock decided what the policy could be. These probes used `us.anthropic.claude-opus-5-5` in
us-east-1 on 2026-11-05, and every refused one cost nothing. They were ad hoc calls, and their
results were not saved:

| Schema variant | Result |
| --- | --- |
| Cohort definition kept as a union of its seven kinds | 400 "The compiled grammar is too large" |
| Analysis union only, or PlannerOutput union only | compiles, 10 to 11 s on first use |
| Cohort-definition fields optional (7), plus the cohort name (8) | compiles, about 12 s on first use |
| 8 optional properties and both unions (adopted) | compiles, about 12 s on first use with a tiny prompt, 9.0 s for the first warm-up with the real prefix, then cached |

Plan sizes and times before and after the change, from the full runs:

| | Before | After |
| --- | --- | --- |
| J3 plan (wire characters / output tokens) | 869 / 397–427 | 508 / 226–256 |
| J1 plan output tokens | 192 | 115–129 |
| Planner call, p50 / p95 | 3.99 s / 5.61 s | 2.85 s / 4.15 s |
| Planner output tokens, p50 / p95 | 256 / 427 | 132 / 256 |
| Cached planner prefix | 12,951 tokens | 12,098 tokens (the examples got shorter) |
| Asks over 5 s, of 94 | 35 | 2 (one stall, see below, and one 6.8 s J3) |

Plan visible per journey, in seconds, from the harness's firstPlanS:

| Journey | Before: p50 / p95 / max | After: p50 / p95 / max |
| --- | --- | --- |
| J1 (25 asks) | 3.44 / 3.84 / 3.95 | 2.64 / 2.94 / 3.44 |
| J2 (25) | 4.01 / 4.33 / 5.51 | 2.81 / 3.22 / 3.48 |
| J3 (25) | 5.52 / 5.77 / 6.12 | 3.79 / 4.59 / 6.84 |
| Edge and other (19) | 4.89 / 6.64 / 6.64 | 3.61 / 33.1 / 33.1 |

Offline tests in `tests/test_model_config.py` (`SchemaTest`) pin this:

- the grammar profile Bedrock compiled: 8 optional properties, 4 unions, 2 unions of objects, no `$ref`;
- the policy's paths and their errors: a required field, an object, an unknown path, a non-union;
- the round trip of every few-shot example;
- the decoding of empty values and of fields of another kind;
- J3's wire plan staying under 560 characters.

### Fix 2: the composer prompt (`model/prompts.py`)

An info log line, `re-compose after <rule> <part>[i]: <detail>`, now names why each re-compose
ran (`model/answer.py`). The run record still keeps no finding. In the second 1-repeat pass
(`134604Z`, the first build with the log), the logs named these causes of its 5 re-composes and
2 sentences dropped after a re-compose:

- the cohort's gene written bare: "KRAS" outside a whole cohort name, as in "KRAS-mutated cases",
  in 3 re-composes and 1 drop. J3's fact labels and plan use those words, but the allow-list
  holds only the whole names "KRAS mutated (ORION)" and "KRAS mutated";
- "driven", which V-SEC rejects and rule 5 did not list (it listed "drives"), in 1 re-compose;
- "respond to", which V-SEC's clinical rule rejects and rule 6 did not list, in 1 drop;
- the number word "third", in 1 re-compose.

All of these verifier findings are correct under the design's V-NUM and V-SEC rules, so the
verifier did not change. The composer prompt now does three things:

- Rule 2 says that the gene defining a cohort appears only inside that cohort's whole name, and
  names the forms that write a bare symbol.
- Rules 5 and 6 list V-SEC's word families with their inflections, from `CAUSAL_WORDS` and
  `CLINICAL_WORDS`. A test checks that the verifier rejects every listed word, and that the forms
  the model wrote are listed.
- A third example, NRAS DE (not an eval prompt; the C-31 test holds), names the gene only inside
  whole cohort names. The near-duplicate check made the first wording of its follow-up change,
  because that wording was too close to a fixture follow-up.

Composer results before and after, from the full runs:

| Composer | Before | After |
| --- | --- | --- |
| First calls re-composed | 19 of 88 (22%) | 2 of 88 (2%; the number words "two" and "one") |
| Sentences dropped after the re-compose | not logged | 0 |
| J1 first call, p50 / p95 | 9.2 s / 10.4 s | 10.3 s / 16.1 s |
| J2 first call, p50 / p95 | 7.6 s / 9.5 s | 7.7 s / 9.4 s |
| J3 first call, p50 / p95 (output tokens p50) | 21.0 s / 25.3 s (1,735) | 11.6 s / 21.8 s (963) in this run only; not reproduced (see below) |
| A re-compose call | p50 20.7 s, max 27.9 s (19 calls) | 17.0 s and 19.2 s (2 calls) |
| Cached composer prefix | 4,221 tokens | 5,650 tokens |

**J3's composer time did not halve.** The 11.6 s in the table above comes from one run (`140440Z`).
The later runs did not reproduce it, `orion-prompts@4` included, which changed only rule 9 (the
follow-up chips). These figures are J3's first composer calls (`.v1.composer.c1` of E5, E6 and the
E22 J3 paraphrases), read from the run records of each report:

| Report | What the composer prompt was | Calls | p50 / p95 | Output tokens, p50 |
| --- | --- | --- | --- | --- |
| `124136Z` | before fix 2 | 25 | 21.0 s / 25.3 s | 1,735 |
| `132622Z` | before fix 2 | 5 | 18.4 s / 21.9 s | 1,454 |
| `134604Z` | before fix 2 (compact planner schema only) | 5 | 19.6 s / 22.6 s | 1,616 |
| `140147Z` | fix 2, `@3` (J3 subset) | 4 | 15.9 s / 22.8 s | 1,193 |
| `140440Z` | fix 2, `@3` | 25 | 11.6 s / 21.8 s | 963 |
| `164252Z` | `@4` | 5 | 20.5 s / 24.9 s | 1,723 |
| **`170844Z`** | **`@4`, the shipped prompts, 5 repeats** | **25** | **19.8 s / 23.3 s** | **1,616** |

The warm timings at `@3` also put J3's first composer call at 15.3 s, and the cold-warmup
timings at 21.2 s. So `140440Z`'s 11.6 s was an outlier. With the shipped prompts, J3's first
composer call takes about 20 s at p50. What fix 2 did change is the re-compose rate: 19 of 88
before, 2 of 88 at `@3` and 0 of 88 at `@4`. That removed the waits of about 40 s that a
re-compose caused.

J1's composer calls are bimodal. A call takes about 8 s with about 600 output tokens, or about
14 s with about 1,200. The visible answer is the same length either way (about 800 characters),
so the difference is adaptive thinking at effort `medium`. The final run and the warm timings had
more of the long calls than the previous run: J1's median rose from 9.2 s to 10.3 s in the final
run and to 14.4 s in the warm timings.

An in-process check on the contract fixture's J1 Observed tested whether the longer rules 5 and 6
caused this, with 4 calls per variant (an ad hoc probe; its results were not saved). The new rules gave 3 short calls and 1 long. The old rules
gave 1 short and 3 long. The word lists are therefore not the cause, and they stay.

### Harness and runner

None of the harness's checks or gates changed. These were added:

- `orion_evals/timings.py`, with the `python -m orion_evals timings` and `usage` subcommands, its
  own unit tests (`tests/test_timings.py`) and mypy `--strict`;
- `orion/scripts/evals-bedrock.sh`, the one-command runner. Its `check` refuses to run when the
  profile's credentials fail sts (it prints only the role name) or when the agent runs the stubs.

## NFR-01 and Open questions 10 and 12: timings

`python -m orion_evals timings` drives the Compose agent like the browser, with fresh threads and
Create at the save pause. Per ask, it measures the plan-visible time (POST to the first snapshot
carrying the plan) and the "Writing interpretation" wait (the first view showing Observed to the
first carrying Interpretation). It also reads each model call's time and tokens from the run
record. There were three modes:

- **warm:** asks back to back after one warm-up;
- **cold:** at least 330 s idle before each ask, past the 5-minute cache TTL, with no warm-up;
- **cold-warmup:** the same idle, then the warm-up action as the Assistant sends it on open, then the ask
  once the warm-up turn has ended.

"Cold" here means a cold prompt cache, as the design's A4 uses warm and cold. No timed ask, and no
ask of the eval harness, ran while a warm-up turn was in flight. The Assistant sends that turn when
it opens, and a question asked during it waits for it (see the NFR-01 bullet below).

Reports: `timings-warm-20261105T144344Z.json`, `timings-cold-20261105T151825Z.json` and
`timings-cold-warmup-20261105T155351Z.json`, after the final full run with the same agent build.
All 36 asks ended Complete.

| Mode | Asks | Plan visible p50 / p95 / max | Within 5 s | Planner prefix | Writing interpretation p50 / p95 / max | Re-composed |
| --- | --- | --- | --- | --- | --- | --- |
| warm | 24 | 3.03 / 4.97 / 7.25 s | 23 of 24 | 24 cache reads | 10.8 / 20.8 / 30.1 s | 1 |
| cold | 6 | 3.05 / 3.77 / 3.77 s | 6 of 6 | 6 cache writes (12,098 tokens each) | 13.2 / 17.9 / 17.9 s | 0 |
| cold-warmup | 6 | 3.26 / 3.80 / 3.80 s | 6 of 6 | 6 cache reads (the warm-up wrote them) | 15.4 / 22.3 / 22.3 s | 0 |
| **all modes** | **36** | **3.05 / 4.97 / 7.25 s** | **35 of 36** | | | |

| Mode | Journey | Plan visible, median / max | Writing interpretation, median / max | Composer first call, median (output tokens) |
| --- | --- | --- | --- | --- |
| warm | J1 (8) | 2.78 / 3.80 s | 14.4 / 16.1 s | 14.4 s (1,169) |
| warm | J2 (8) | 2.97 / 7.25 s | 7.8 / 9.7 s | 7.8 s (598) |
| warm | J3 (8) | 4.09 / 4.97 s | 19.2 / 30.1 s | 15.3 s (1,274) |
| cold | J1, J2, J3 (2 each) | 2.88, 2.93, 3.74 s / 3.77 s | 15.9, 8.7, 14.6 s / 17.9 s | 15.9, 8.7, 14.6 s |
| cold-warmup | J1, J2, J3 (2 each) | 3.26, 2.88, 3.75 s / 3.80 s | 15.4, 8.6, 21.2 s / 22.3 s | 15.4, 8.6, 21.2 s |

What the measurements answer:

- **NFR-01 (plan visible within 5 s, p95 over at least 20 warm and cold runs; Open question 12).**
  Over these 36 warm and cold-cache asks the p95 was 4.97 s. Over the final eval run's 94 asks it
  was 4.19 s. Both are within the **Proposal**, measured to the first plan render, which is what
  the harness's firstPlanS times. **This holds only for asks that do not overlap the opening
  warm-up turn.** In the browser, the first question in a fresh tab, clicked while that turn ran,
  showed its plan in 7.2 to 10.8 s (p50 7.9 s), and 0 of those 18 asks were within 5 s
  (`bedrock-status.md`, Known limit 1). Neither the timings modes nor the eval gate measure that path. The margin rests on J3. With the same 226 or 256 output tokens
  every time, its warm plans ran 3.7 to 5.0 s in this window, and its median was 3.8 s in the
  eval run. The one warm miss was a J2 plan of 7.25 s with its usual 160 output tokens: a slow
  hop, not a long plan.
- **Re-warming or the 1-hour TTL (Open questions 10 and 12).** A cold prompt cache costs
  nothing measurable. Cold asks wrote 12,098 planner tokens and 5,650 composer tokens and still
  planned in 3.05 s at the median, against 3.03 s warm. The 5-minute TTL is enough, and the
  1-hour TTL would buy no latency at its higher write price. The warm-up turn the Assistant sends
  on open took 5.4 s at the median (both roles at once, 4.8 to 5.7 s). Its value is the grammar
  compile on a schema's first use (9.0 s for the planner's first warm-up after the schema
  change) and the cache write. It is off the critical path only when the researcher asks after
  it ends. A question asked while it runs waits for it, which is Known limit 1 in
  `bedrock-status.md`. That note lists the options; none is chosen yet.
- **The composer at effort `medium` (Open question 10).** The "Writing interpretation" wait is
  about 8 s for J2, 14 to 16 s for J1 and 15 to 21 s for J3, with Observed already on screen.
  A re-compose adds its own call: 17.9 s in the warm run, which made that J3 wait 30.1 s, and
  17.0 s and 19.2 s in the final eval run. With 2 of 88 answers re-composed in the final eval
  run and 1 of 36 here, the re-compose is rare. When it happens it doubles the wait but does not
  block the screen: Observed, the step chips and the Evidence Inspector are already there.

## Model calls and tokens (from the run records)

These are the runs of the final full run, each run counted once, from `evals-bedrock.sh usage`.
Warm-up calls make no record and are not included. The E11 fault agents made no model call.

| | Planner | Composer | All |
| --- | --- | --- | --- |
| Calls | 103 (100 first, 3 later: 1 SDK retry after the stall, 2 plans after a clarification) | 90 (88 first, 2 re-composes) | 193 in 100 runs with model calls (104 runs) |
| Outcomes | 102 ok, 1 error (the 30 s timeout) | 90 ok | no refusal, no fallback |
| Effort | low | medium | |
| Input tokens (uncached) | 15,855 | 237,580 | 253,435 |
| Cache reads | 1,221,898 | 502,850 | 1,724,748 |
| Cache writes | 12,098 (1 call) | 5,650 (1 call) | 17,748 |
| Output tokens | 16,608 (p50 132, p95 256 per call) | 85,737 (p50 798, p95 1,678) | 102,345 |
| Time per call, p50 / p95 | 2.85 s / 4.15 s | 9.97 s / 20.4 s | |

The previous stage 2 full run made 209 calls, 19 more composer calls than this one. It used
147,736 output tokens and 331,292 uncached input tokens. This run used 31% fewer output tokens
and 23% fewer uncached input tokens for the same 104 runs. Each composer call sends about 1,300
(J1, J2) to 4,800 (J3) uncached input tokens on top of its cached prefix: the facts, Observed and
the allow-list. The
planner's request is about 160 uncached tokens on its 12.1K cached prefix.

Over this whole step, the Bedrock calls were about:

- 193 recorded calls for the final full run;
- 81 and 75 recorded calls for the two 1-repeat passes;
- 12 recorded calls for the J3 subset;
- about 100 for the timings: 36 measured asks, 7 warm-up turns, and 5 asks of a warm run stopped to fix its wait measurement;
- about 40 for the schema probes, the in-process planner check and the J1 composer check.

## Open issues

- **Planner stalls.** Twice in about 250 planner calls today, a first attempt hung until the
  30 s per-attempt timeout (`ORION_PLANNER_TIMEOUT_S`, default 30). The SDK's retry then
  answered in 2.7 s and 5.7 s. In the final run this made one 33.1 s plan-visible time, which
  sits inside the p95 gate but is a real tail. A shorter planner timeout, about 15 s with a
  longer one for the warm-up (whose first use of a schema compiles its grammar in up to about
  12 s), would roughly halve such a stall. It is left at the design's default for the team to
  decide.
- **The plan-visible margin on J3.** The gate passes, but J3's plans ran 3.7 to 5.0 s in the
  warm window. Making the remaining empties optional would save about 25 tokens (about 0.25 s)
  on J3: `follows`, `base_cohort`'s inner fields, and DE's `method`, `rank_by` and `thresholds`.
  It would also raise the grammar to about 15 optional properties, whose first-use compile time
  was not measured. Not done.
- **The composer's time.** With the shipped `@4` prompts, J3's first composer call takes 19.8 s at
  p50 and 23.3 s at p95 at effort `medium` (`170844Z`, 25 calls; the 11.6 s of `140440Z` was not
  reproduced).
  J1 is bimodal, about 8 s or about 14 s, depending on how long the model thinks. A re-compose
  adds about 18 s. Observed shows meanwhile, so NFR-01's "never waits blind" holds. The design
  fixes the composer at effort `medium`, so it was not lowered.
- **The grammar compile on first use.** About 9 to 12 s, once per schema (it is cached
  afterwards). The warm-up absorbs it. A deploy that changes the schema and skips the warm-up
  pays it on the first ask, which is still inside the planner's 30 s timeout.
- **Process cold start.** Not measured. Mode L keeps the agent process up, and an AgentCore
  microVM start is outside the local stack. "Cold" here means a cold prompt cache.
- **The browser E2E (`e2e-mode-l.mjs`).** It was run against the Bedrock agent after this note's
  eval runs. Its results, and the fresh-tab timings above, are in `bedrock-status.md`. The fork is
  unchanged.
