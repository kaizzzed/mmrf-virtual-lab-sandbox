# Open question 11: which cohort filters fail in the ProteinPaint bridge

RFA rev 25: A2 "Tools" (the `top_mutated_genes`, `sample_lists` and `de_*` guards), A4 "Security at
sandbox level" ("A filter fails silently", "A browser-supplied cohort filter is invalid or
unsupported"), C-23, and the Day 1 PM TL row. Probed on 2026-11-05 against the local `mmrf-synthetic`
stack (synthetic data, seed 42; ProteinPaint server 2.216.0) with read-only requests through the
identity gateway, `http://identity:8080`.

**Result.** The bridge reads every filter0 the compiler hands it under C-23 (case-ID lists of any
size up to 10,000 ids) and every clause filter on a field that the generated case records carry,
with `and` and `or` at any depth. It fails in two ways:

1. **Loudly (HTTP 400 "Invalid sandbox request")**: any field no case record carries, which is every
   Elasticsearch-only field (`available_variation_data`, `gene.*`, `summary.*`) and any unknown
   field; and any operator outside its list, such as the FilterSet spelling `excludes`.
2. **Silently**: a numeric field written with string values (`outcomes.censor_os` in `["1"]`)
   matches no case in the bridge, while Gen3 and Guppy coerce it and match 599. Nothing errors.

Two operators work only on one side: `not` and `exclude` are evaluated by the bridge but rejected by
Guppy (`Field path not found: in`, `Invalid operation "exclude"`), so a cohort using them cannot be
counted the same way on both engines. Gen3 also rejects the key `cases.case_id`, which the bridge
reads. The DE requests carry no cohort filter: their groups are sample-id lists, and pre-analysis
returns the same eligible counts whatever `filter0` holds (all cases, null, or 100 case IDs).

## How the bridge works

`identity/cohorts.mjs` `bridgePayload` evaluates `filter0` against the generated cases (`cases.json`)
and replaces it with a native sample list (sample n is case `SYNTH-CASE-000n`), merged with any
native `filter`. `matches` accepts the operators `and`, `or`, `not`, `in`, `exclude`, `=`, `!=`,
`>`, `>=`, `<` and `<=`, compares values with strict equality (so `"1"` is not `1`), strips a
`cases.` prefix, and throws for a field that no record carries; the gateway turns the throw into a
400. ProteinPaint requests are bridged whether they arrive as GET or POST.

## Probe table

Gen3 is Guppy's `_totalCount` for the same filter. Bridge samples is the number of samples
`getsamplelist` (the `sample_lists` request) returned; bridge genes is the number of genes
`topMutatedGenes` ranked. **Same** means the bridge's count equals Gen3's (or, for case-ID lists, the
number of real cases listed). The two guard columns are what `orion_agent/tools/guards.py` says
before any call: the tools' own structural check (`filter0_bridge_safe`, which also runs in the
Gateway Lambda) and the executor's check with the vocabulary (`bridge_safe_with_fields`).

| Filter0 form | Gen3 (Guppy) | Bridge samples | Bridge genes | ms | Same | Tool guard | Executor guard |
| --- | --- | --- | --- | --- | --- | --- | --- |
| all cases, `{and: []}` | 1000 | 1000 | 6 | 5 | yes | pass | pass |
| in, one value | 500 | 500 | 6 | 5 | yes | pass | pass |
| in, two values | 666 | 666 | 6 | 6 | yes | pass | pass |
| = | 500 | 500 | 6 | 5 | yes | pass | pass |
| != | 500 | 500 | 6 | 5 | yes | pass | pass |
| unwrapped clause (no and) | 500 | 500 | 6 | 5 | yes | pass | pass |
| range >= and < (age_at_index, days) | 446 | 446 | 6 | 5 | yes | pass | pass |
| range on a nested array (diagnoses.age_at_diagnosis >= 60) | 557 | 557 | 6 | 6 | yes | pass | pass |
| range on a sparse field (days_to_death < 1000) | 251 | 251 | 6 | 4 | yes | pass | pass |
| nested and/or | 216 | 216 | 6 | 5 | yes | pass | pass |
| or at the top | 551 | 551 | 6 | 5 | yes | pass | pass |
| CGS risk category | 339 | 339 | 6 | 5 | yes | pass | pass |
| multiselect (regimen_short_name) | 233 | 233 | 6 | 4 | yes | pass | pass |
| numeric enum as numbers (censor_os in [1]) | 599 | 599 | 6 | 4 | yes | pass | pass |
| numeric enum as strings (censor_os in ['1']) | 599 | 0 | 0 | 3 | **no** | pass | UNSUPPORTED_FIELD |
| text enum of digits (ecog in ['0']) | 190 | 190 | 6 | 4 | yes | pass | pass |
| project.project_id | 500 | 500 | 6 | 4 | yes | pass | pass |
| samples.sample_type | 1000 | 1000 | 6 | 7 | yes | pass | pass |
| files.experimental_strategy | 168 | 168 | 6 | 5 | yes | pass | pass |
| ES-only: available_variation_data | 1000 | HTTP 400 | HTTP 400 | 1 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| ES-only: gene.symbol | 480 | HTTP 400 | HTTP 400 | 1 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| ES-only: summary.experimental_strategies | 168 | HTTP 400 | HTTP 400 | 1 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| unknown field | error: Field path not found | HTTP 400 | HTTP 400 | 1 | **no** | pass | UNSUPPORTED_FIELD |
| not | error: Field path not found: in | 500 | 6 | 4 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| exclude | error: Invalid operation "exclude" | 500 | 6 | 4 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| excludes (FilterSet spelling) | error: Invalid operation "excludes" | HTTP 400 | HTTP 400 | 0 | **no** | UNSUPPORTED_FIELD | UNSUPPORTED_FIELD |
| case-ID list, 1 | 1 | 1 | 2 | 3 | yes | pass | pass |
| case-ID list, 10 | 10 | 10 | 5 | 3 | yes | pass | pass |
| case-ID list, 100 | 100 | 100 | 6 | 4 | yes | pass | pass |
| case-ID list, 1,000 | 1000 | 1000 | 6 | 14 | yes | pass | pass |
| case-ID list, 5,000 (1,000 match) | 1000 | 1000 | 6 | 42 | yes | pass | pass |
| case-ID list, 10,000 (1,000 match) | 1000 | 1000 | 6 | 67 | yes | pass | pass |
| case-ID list, empty | 0 | 0 | 0 | 3 | yes | pass | pass |
| cases.case_id key, 10 | error: Field path not found: cases.case_id | 10 | 5 | 3 | yes | pass | pass |
| legacy operands `{cases.case_id: id}`, 10 | 10 | 10 | 5 | 3 | yes | pass | pass |

DE pre-analysis eligible counts (the J3 KRAS groups, 520 and 480 samples) with `filter0` all cases,
null, and 100 case IDs: `{control: 520, case: 480}` each time.

A 10,000-id case list is a body of about 190 KB, far under the gateway's 8 MiB, and the bridge
answers it in under 0.1 s. The 8 MiB guard on `sample_lists` therefore cannot trip with a valid
Input (at most 10,000 ids per group); it stays as the gateway's limit.

## Encoded in the guards

| Tool | Guard | What it now checks |
| --- | --- | --- |
| `top_mutated_genes` | pre `filter0_bridge_safe` -> `UNSUPPORTED_FIELD` | The tool (in-process and in the Lambda): null or `{"and": []}`; `and`/`or` nodes; leaves `in`, `=`, `!=`, `>`, `>=`, `<`, `<=` only (`not` and `exclude` refused as one-engine operators, `excludes` and anything else as unreadable); no Elasticsearch-only field; case-ID clauses on `case_id` or `cases.case_id` with valid ids or legacy operands; scalar values. The executor (`bridge_safe_with_fields`): all of that, plus every field known to the vocabulary and not `es_only`, and values typed as stored (numbers for numeric fields, text for text enums), which catches the silent zero |
| `sample_lists` | pre `case_lists_nonempty` -> `EMPTY_COHORT`; pre `filter0_bridge_safe`; pre `body_bytes<=8388608` -> `REQUEST_TOO_LARGE`; post `samples>0_per_group` -> `EMPTY_COHORT` | Each group's filter0 is `case_id_filter` of its ids (always bridge-safe); an empty list stops before any request; a group that maps to no sample is the zero |
| `de_preanalysis`, `de_run` | pre `samples<=4000` -> `REQUEST_TOO_LARGE` | The groups are sample-id lists; neither tool forwards a cohort filter (pre-analysis keeps the golden's fixed all-case `filter0`, which the probe shows has no effect, and `de_run` sends null as the direct DE card does) |

`tests/test_tools_oq11.py` reruns this table against the live stack and asserts the guards agree
with it: every filter the executor's check passes gives the bridge Gen3's count; every filter the
tools' check refuses fails or differs; the executor's check is the stricter; the string-valued
numeric field is the silent case. It skips cleanly when the stack is not reachable.

## Why ORION rarely meets these

Under C-23 a filtered scope is resolved once by its `cases_query` and every ProteinPaint step gets
its case-ID list, so the bridge sees only case-ID clauses; and the compiler validates browser
filters like planner output (C-VOCAB normalises values to their stored spelling and JSON type). The
guards are the last line: they stop a compiler bug, or a browser FilterSet that slipped through, from
reaching the bridge as a bare 400 or a silent zero.

## Left open

- The tools' structural check cannot know an unknown field without the vocabulary, so in the Lambda
  such a filter reaches the bridge and comes back as `HTTP_ERROR` (400), not `UNSUPPORTED_FIELD`.
  The executor's check catches it first in every run; the Lambda carries no vocabulary by design.
- Every row is the local stack. The hosted sandbox runs the same gateway code and data, so the same
  table is expected; not re-run through CloudFront.

## Reproduce

```sh
export PATH=$HOME/.docker/bin:$PATH   # from the sandbox repo root
docker run --rm --network mmrf-synthetic_default -v $PWD/orion/agent:/app -v $PWD/config:/config:ro \
  -e ORION_VLAB_ORIGIN=http://identity:8080 -w /app orion-agent:dev \
  python -m unittest discover -s tests -p 'test_tools_oq11.py' -v
```
