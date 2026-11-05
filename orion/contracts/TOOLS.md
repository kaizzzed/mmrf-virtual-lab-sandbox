# ORION tool and step catalog (v1, 2026-11-05)

The catalog every builder codes against: the ten server tools, four executor functions and five
client actions, the step tables the compiler emits and the executor runs, the facts each step
yields, and the code templates that cite them. Design source: ORION RFA rev 25, A2 ("Planning",
"Tools", "Data model"), A3 (J1, J3, edge cases, step lifecycle) and A4 (timeout budget). Where this
file and the run-event contract (`README.md`, v1.2) disagree, the contract wins.

The types are in `orion/agent/orion_agent/tools/schemas.py` (Pydantic v2, mypy --strict); the
shared tool contract is `tools/base.py`; wire types are `models.py`. `tests/test_tool_schemas.py`
checks this file's tables against the code, the golden bodies and the fixtures. Every number in an
example is illustrative.

Contents: 1 Conventions · 2 Server tools · 3 Executor functions · 4 Client actions · 5 Step tables ·
6 From the IR to steps · 7 Templates · 8 Viz specs · 9 Choices this catalog makes.

## 1. Conventions

### Names, versions, transport

- A tool's name is its registry key (`TOOL_REGISTRY`), its version `<name>@1` (`ToolSpec.version`),
  recorded on every call. Server tools sit behind one Gateway target, `vlab`, as `vlab___<tool>`;
  `kg_query` is `kg___kg_query` on the stretch target `kg` (`gateway_tool_name`).
- Server tools are read-only HTTP tools through the identity gateway (`ORION_VLAB_ORIGIN`, locally
  `http://identity:8080`). In mode L they run in-process; in modes A and H the Gateway Lambda runs
  the same handler. The local stack needs no credential (the identity gateway issues its own
  synthetic token); in AWS the Lambda adds the Basic header from `orion/vlab-basic`.
- Never called: `/analysis/v0/cohorts/query`, `/cohorts/download`, `/files/*`, `GET /cases/{id}`,
  `/gene_expression/*` and ProteinPaint's chat route.

### Args, request and Observed

- **`StepSpec.args`** is what the compiler writes (and what the plan signature hashes). For a
  server tool it is the tool's Input in which any slot may hold a **StepRef**: a JSON object whose
  keys are exactly `ref` and `pointer`, optionally `intersect`
  (`{"ref": "s2", "pointer": "/observed/caseIds"}`). `pointer` points into that step's last
  successful call, as fact pointers do. `intersect: "s3"` keeps only the case ids also in s3's
  `/observed/caseIds`, sorted. No filter or body ever has the StepRef shape.
- The executor resolves every StepRef (`resolve_step_refs`), validates the result against the
  tool's Input model and sends it. That resolved Input is the call's **`request`** (CallRecord),
  and `requestSha256` is the sha256 of its canonical JSON (`portable=False`). The Gateway Lambda
  never sees a reference.
- `de_run` and `save_cohorts` steps carry their own args models (`DeRunStepArgs`,
  `SaveCohortsStepArgs`), because they also read the confirm read-back (5.3, 5.8).
- An **executor function**'s Input names earlier steps by bare step id (`{"curves": "s4"}`) and is
  recorded that way; the function reads the referenced calls' outputs in-process.
- **Observed** (`ToolResult.observed`, `calls[].observed`) is the tool's normalised output, never
  the raw body, as camelCase JSON. The one exception is the DE gene row (`DeGene`), which keeps
  ProteinPaint's snake_case names because `rank_top_n` keeps source rows unchanged and facts point
  at `/observed/rows/<i>/row/fold_change`.

### `dependsOn`

A step's `dependsOn` lists every step whose output it reads: each StepRef's `ref` and `intersect`,
each bare step id of an executor Input, the count steps a pre-guard reads, the steps that define the
cohorts a `save_cohorts` step saves, and, for a step after a write step, that write step.

### Input hash and Reused

An edit marks a step Reused when its input hash equals a finished step's of the previous version
(contract "The edit sequence"). The input hash is the sha256 of the canonical JSON of
`{"tool", "version", "args"}` in which every StepRef and every bare step id an executor Input names
is replaced by the referenced step's own input hash. A step whose args name a proposed cohort name
(`save_cohorts`, `de_run`) changes when that name is edited; so a Rename at the J3 save pause
reuses s1 to s7, re-pauses s8 with a new nonce and reruns s9 and s10 (`j3-edit-at-pause`).

### Provenance, hashes and size caps

- `provenance` lists every HTTP request of the attempt, in order:
  `{method, path, requestSha256, httpStatus, responseSha256, elapsedMs}`. `requestSha256` hashes the
  canonical JSON of the payload sent (`null` for a payload-free GET), `responseSha256` the canonical
  JSON of the parsed response body (null when there was none), both `portable=False`.
- Every request is sent as a JSON POST of its payload, except a payload-free GET. The gateway turns
  a ProteinPaint GET carrying `filter0`, `filter` or `samplelst` into that same POST anyway.
- A tool reads at most `MAX_UPSTREAM_BYTES` (16 MiB) of an upstream body, and its Observed is at most
  `MAX_TOOL_RESPONSE_BYTES` (6,000,000 bytes of canonical JSON), below Lambda's 6 MB synchronous
  limit. Either cap exceeded is `UPSTREAM_SHAPE`.

### Timeouts and retries (A4)

`timeout_s` (55 s, `TOOL_HTTP_TIMEOUT_S`) is one attempt's budget; a multi-request tool gives each
request what remains. The executor's step timeout is 60 s and the step cap 130 s. Server reads retry
**once**, with an identical body (the same resolved Input, so every request of the attempt is
re-sent), on 429, 502, 503, 504 or a timeout; a 429 backs off 10 to 20 s first. Executor functions
never retry (they are deterministic). Client writes never retry. `kg_query` never retries.

### Error mapping (every server tool)

| Condition | `ToolError.code` | `retryable` | Result status | Step | Limitation |
| --- | --- | --- | --- | --- | --- |
| Resolved input fails the Input model | `INVALID_INPUT` | no | `error` | Failed | `TOOL_FAILED` |
| A pre-guard fails | `GUARD_FAILED` | no | `error` | Skipped | the guard's code; run Limited |
| 429 (ProteinPaint `CACHE_BUSY`) | `CACHE_BUSY` after the retry | once | `error` | Failed | `TOOL_FAILED` |
| 502, 503, 504 | `HTTP_ERROR` after the retry | once | `error` | Failed | `TOOL_FAILED` |
| Any other non-2xx (the gateway's 400 "Invalid sandbox request", 401, 403, 404, 500) | `HTTP_ERROR` | no | `error` | Failed | `TOOL_FAILED` |
| No response within the attempt budget | `TIMEOUT` after the retry | once | `error` | Failed | `TOOL_FAILED` |
| 2xx body not JSON, not the expected shape, a GraphQL `errors` array, or a size cap exceeded | `UPSTREAM_SHAPE` | no | `error` | Failed | `TOOL_FAILED` |
| Stop or an edit aborted the attempt | `CANCELLED` | never | `error` | Skipped "Not run: stopped" | none |
| `kg_query` before its target exists | `NOT_CONNECTED` | no | `unavailable` | Skipped | `KG_NOT_CONNECTED`; run Limited |

The `TOOL_FAILED` line uses template `tool.failed` with `TOOL_PHRASES`, `FAILURE_PHRASES` and
`MISSING_PHRASES` (7.2). An executor function that raises fails its step the same way (failure
phrase `FAILED`, the CallRecord `error` starting `INTERNAL_ERROR`).

### Guards

`ToolSpec.guards` are `GuardSpec`s: a `base.Guard` (`rule`, `phase`, `on_fail`) plus `effect`,
`checked_by` and the step status a failure leaves.

- **pre** guards run on the resolved Input before the call: the executor checks them (and a tool
  re-checks its own, answering `GUARD_FAILED`). A failure skips the step with the guard's code.
- **post** guards run on the tool's result. A `stop` post-guard keeps the step's facts and its
  status (`Empty` for a zero, `Done` otherwise); the run ends **Limited** once no further step can
  run. A `report` post-guard adds its limitation and the run goes on.
- A step that is Failed, Skipped by a guard or Empty with a `stop` guard skips every step that
  depends on it, directly or through `dependsOn` ("Not run: s4 failed", "Not run: <reason>").
  Steps that do not depend on it still run. The run then ends Partial (a Failed step) or Limited
  (a stop guard), and the composer is not called.
- An Empty step whose dependents all still run (a ranking with zero rows) does not stop the run.
- Limitations are written once per code per analysis.

### Empty and zero results (C-40)

`empty` is a zero result, never success with no rows. A tool returns status `empty` with its full
Observed (the zero is in it: `count: 0`) so the count fact exists and Observed can cite the zero.
The step is Empty, Observed reports the zero with the filter used (`empty.count`), and the limitation
`empty.cohort` names both causes: no case has those values, or the API returned an empty set for a
field it does not index. Gen3 and Guppy answer a misspelt value with 0, not an error (C-VOCAB is the
first defence).

### Facts

- **Grammar.** `f.<step id>.<suffix>`, the suffix one or more `[A-Za-z0-9_-]` segments joined by
  dots (`FACT_ID`, the same as `models.FactId`). A fact id is its own placeholder,
  `{{f.s10.top.1.gene}}` (`PLACEHOLDER`, identical to `interfaces.PLACEHOLDER`).
- **Specs.** `FACT_SPECS[tool]` (and `RANK_FACT_SPECS[(source, rankBy)]` for `rank_top_n`) list each
  fact: suffix, JSON pointer into the call (`/observed/...`), value type, unit, label template and
  the `format_fact` rule its unit selects (`p`, `log2FC`, `string` for `gene`, `number` otherwise).
  `${index}` is the 0-based position, `${key}` the group key, `${pos}` the 1-based row position,
  `${rank}` the row's rank (tied rows share one), `${gene}` the row's key.
- **Labels.** `${cases_label}` is `cases_label(<cohort counted>)`: the name without " (ORION)",
  with " cases" added unless it already ends so ("High risk" -> "High risk cases",
  "KRAS not detected (ORION)" -> "KRAS not detected cases"). `${group}` is a survival group's label,
  or a DE group's `cohort_label` (name without " (ORION)"). `${inner}` is check_subset's inner
  step's count label. `FactContext` carries what the plan knows; `draft_facts` turns an Observed
  into `FactDraft`s, which the extractor completes with step, row, tool, callId and hashes.
- **Group keys.** Survival groups: `group_keys(labels)`, the lowercase words minus those every label
  shares (`High risk`, `Standard risk` -> `high`, `standard`; `Stage I`, `Stage II` -> `i`, `ii`),
  falling back to `g1`, `g2`... DE groups are always `control` (group 1) and `case` (group 2).
- **Order.** A step's facts are in spec order, and consecutive per-row facts row by row (rank 1's
  gene, log2fc, padj, then rank 2's), as the fixtures list them.
- **Ranked rows.** A view shows at most `VIEW_RANK_CAP` (20) ranked rows in all, in plan order:
  `FactContext.rank_cap` is what this ranking may still show. `rank_top_n` yields its `count` fact
  only when it has more rows than it shows or fewer than asked (`rank.cap`, `rank.fewer` cite it).
- **Engines** (`FactRecord.engine`): `fixture-fingerprint@1` for s1, `guppy@c687656` for
  `cohort_count`, `gen3-analysis@ffa4069` for `cases_query`, `gene_cases` and `survival_compare`,
  `proteinpaint@2.216.0` for the ProteinPaint tools, null for executor functions and client writes.

## 2. Server tools

Each tool's request starts from a **template** (`RequestTemplate`): the golden request
(`orion/golden/<file>`, `requests[i]`, its `query` or `body`) or an inline body. `Substitutions`
replace values at JSON pointers; `fixed` pointers keep the captured value; everything else in the
golden payload is sent unchanged. Tools read goldens from `ORION_GOLDEN_DIR` (default
`<repo>/orion/golden`).

### 2.1 `fixture_fingerprint` (s1, folded under row 1)

- **Endpoints.** `GET /sandbox/status`, `GET /analysis/v0/_version`, `GET /protein-paint/healthcheck`,
  `POST /protein-paint/termdb/topMutatedGenes`, in that order (4 provenance entries).
- **Input.** `{}` (`FixtureFingerprintInput`).
- **Requests.** The three GETs carry no payload. The ranking sends the golden
  `proteinpaint/termdb-topMutatedGenes.json` query unchanged (`maxGenes` 50, `geneFilter` CGC,
  `filter0 {"and": []}`).
- **Observed** (`FixtureFingerprintObserved`): `{"document", "fingerprintSha256"}`. `document` is
  `models.fingerprint_document(status, version, healthcheck, ranking)` with the ranking normalised
  as for `top_mutated_genes` (`[{gene, cases}]`, returned order); `fingerprintSha256` is
  `models.fingerprint_sha256(document)` and becomes `RunView.environment.fingerprintSha256`.
- **Guards.** None. It runs only after the plan compiles: a compile-time stop makes no data call.
- **Empty.** Never empty. A missing `versionInfo` is `UPSTREAM_SHAPE`.
- **Facts.** `f.s1.cases` (`/observed/document/sandbox/cases`, cases, "Cases in the sandbox"): the
  scope count (`base.caseCountFactId`) when the scope is unfiltered all cases.

### 2.2 `cohort_count`

- **Endpoint.** `POST /guppy/graphql`; no golden. Inline body
  `{"query": COHORT_COUNT_QUERY, "variables": {"filter": <filter>}}`, the query fixed:
  `query ($filter: JSON) { CaseCentric__aggregation { case_centric(filter: $filter, accessibility: accessible) { _totalCount } } }`.
- **Input.** `{"filter": <Gen3 filter>}`, always `and`-wrapped (`compiler.gql_filter`). A filtered
  scope enters as its case-ID clause (`{"in": {"case_id": StepRef}}`, C-23).
- **Observed.** `{"count"}` from `data.CaseCentric__aggregation.case_centric._totalCount`.
- **Guards.** post `count>0` -> `EMPTY_COHORT` (stop, status `empty`).
- **Errors.** A GraphQL `errors` array (for example "Field path not found") at HTTP 200 is
  `UPSTREAM_SHAPE`.
- **Facts.** `f.sN.count` (`/observed/count`, cases, `${cases_label}` of the group or cohort counted).

### 2.3 `cases_query`

- **Endpoint.** `POST /analysis/v0/cases/`; no golden. Inline body
  `{"filter": <filter>, "fields": ["case_id"], "size": <pageSize>, "offset": <page x pageSize>}`;
  `/fields` fixed.
- **Input.** `{"filter", "pageSize": 1000, "maxIds": 10000}`; the compiler writes both numbers.
- **Paging.** Page 0 first. If `pagination.total` > `maxIds`, stop reading: `caseIds` empty,
  `complete` false, post-guard `TOO_MANY_IDS`. Otherwise read pages while `offset + size <= maxIds`
  until `total` ids are read (Guppy rejects `offset + size > 10000` with a 500, so never send one).
- **Observed.** `{"count": pagination.total, "caseIds": data[].case_id deduplicated and sorted,
  "pages", "complete"}`.
- **Guards.** post `count>0` -> `EMPTY_COHORT` (stop, `empty`); post `case_ids<=10000` ->
  `TOO_MANY_IDS` (stop, step Done with its count fact).
- **Provenance.** One entry per page. Response cap: 10 pages of 1,000 ids is about 300 KB.
- **Facts.** `f.sN.count` (`/observed/count`, cases, `${cases_label}`). The scope step's count is
  `base.caseCountFactId`; a planned cohort's is its cohort's.

### 2.4 `gene_cases`

- **Endpoint.** `POST /analysis/v0/genomic/gene_cases`; no golden. Inline body
  `{"cohort_filter": <cohortFilter>, "gene_filter": {"and": [{"in": {"gene_id": [<Ensembl id>]}}]}, "ssm_filter": {}}`.
  The gene filter is **always `and`-wrapped** and names the gene-centric field `gene_id`: an
  unwrapped filter is silently ignored (866 cases instead of 480 locally), and `gene.gene_id`
  matches nothing.
- **Input.** `{"gene": KRAS|NRAS|TP53|BRAF|DIS3|MYC, "cohortFilter": <the scope's Gen3 filter>}`
  (`{"and": []}` for all cases). `PANEL_GENE_IDS` maps the symbol to its Ensembl id.
- **Observed.** `{"gene", "count", "caseIds"}` from the returned id array, deduplicated and sorted.
- **Guards.** pre `gene_in_panel` -> `OUT_OF_PANEL` (C-PANEL stops it at compile time first); post
  `count>0` -> `EMPTY_COHORT` (stop).
- **Facts.** `f.sN.count` (`/observed/count`, cases, "${gene}-mutated cases").

### 2.5 `top_mutated_genes`

- **Endpoint.** `POST /protein-paint/termdb/topMutatedGenes`, golden
  `proteinpaint/termdb-topMutatedGenes.json` query. Substituted: `/maxGenes` <- `geneCount + 1`,
  `/filter0` <- `filter0` (or `{"and": []}` when null). Fixed: `/genome`, `/dslabel`, `/geneFilter`.
- **Input.** `{"filter0", "geneCount"}` (`derive_request_pinned("oncomatrix")` reads `filter0`).
  `filter0` is the cohort's case-ID filter (`case_id_filter`, C-CASEIDS), null for all cases;
  `geneCount` is top N (1 to 50).
- **Observed.** `{"genes": [{gene, cases}], "nextGene"?}`. `cases` is the `count` of the gene's
  `mutationStat` entry with `dt` 1 (SNV/indel; the sandbox has no other class, any other is dropped
  with a warning). `genes` are the first `geneCount` rows in returned order: the card's pinned
  `genes`. `nextGene` is the extra row, read only so `rank_top_n` can see a tie at the cutoff.
- **Guards.** pre `filter0_bridge_safe` -> `UNSUPPORTED_FIELD` (the executor checks `filter0` names
  only `case_id` or fields whose vocabulary `es_only` is false; the gateway's bridge answers 400 for
  Elasticsearch-only fields); post `genes>0` -> `FEWER_THAN_N` (report, status `empty`: the ranking
  writes the one line, `rank.none_genes`).
- **Facts.** None of its own; `rank_top_n` cites its rows.

### 2.6 `survival_compare`

- **Endpoint.** `POST /analysis/v0/survival/`, golden `analysis/survival.json` request 1 (the
  two-group body). Substituted: `/filters` <- `[groups[i].filter]` for each group, then every group's
  filter in order. The body has no `survivalType` (Gen3 Analysis' default `overall`). Calls: one per
  group, then one combined: N + 1 provenance entries.
- **Input** (exactly what `derive_request_pinned("survival")` reads):
  `{"scope", "term": "overall_survival", "split": {"field", "overlay"} | null, "groups": [{"label", "filter"}]}`.
  `scope` is the scope's Gen3 filter (null for all cases; a filtered scope's case-ID clause), which
  the card pins as `filter0`. `split.field` is the vocabulary field, `split.overlay` its ProteinPaint
  twin (`vocab.FieldSpec.pp_twin`). Each group filter is complete (scope included).
- **Observed.** `{"groups": [{label, cases, events, curve}], "pValue", "engine"}`. Per group, from
  its own call's `results[0].donors`: `cases` = donors, `events` = donors with `censored` false.
  `pValue` = the combined call's `overallStats.pValue` (null when fewer than two curves).
  `engine` = `gen3-analysis@ffa4069`. `curve` = `[{time: 0, survival: 1.0}]` then, for each
  distinct donor time t ascending, the engine's estimate just after t: the `survivalEstimate` of a
  censored donor at t; else of an event donor at the next distinct time (the engine gives event
  donors the estimate before their event); else of a censored donor at the next time (no event
  there, so no drop); else 0 (t is the last time and every donor there had the event).
- **Guards.** pre `group_cases>=10` -> `INELIGIBLE_GROUP` (stop; the executor reads each group's count
  fact from `dependsOn`, so no survival call is made for a small group); post
  `every_group_has_a_curve` -> `EMPTY_COHORT` (stop; Gen3 Analysis drops a group with no survival
  data from `results`, so a group's own empty `results` is its zero).
- **Provenance and size.** Each response is about 150 bytes per donor (1.5 MB for 10,000 cases).
- **Facts.** `f.sN.events.<key>` per group (`/observed/groups/<i>/events`, events,
  "${group} events") and `f.sN.p` (`/observed/pValue`, p, "Log-rank p"; only when not null).
  Group sizes cite the count steps' facts, not this tool's `cases`.

### 2.7 `sample_lists`

- **Endpoint.** `POST /protein-paint/termdb` (getsamplelist), golden
  `proteinpaint/termdb-getsamplelist.json` request 0 query. Substituted: `/filter` <- `EMPTY_TVSLST`
  (no native filter), `/filter0` <- `case_id_filter(group.caseIds)`. Fixed: `/genome`, `/dslabel`,
  `/getsamplelist`, `/mapParent2Children`. One call per group, control first. The gateway bridges
  `filter0` into a native sample list (sample n is case `SYNTH-CASE-000n`).
- **Input.** `{"groups": [{"key": "control", "caseIds"}, {"key": "case", "caseIds"}]}`.
- **Observed.** `{"groups": [{key, count, sampleIds}]}`: the response's `[].id`, ascending.
- **Guards.** pre `case_lists_nonempty` -> `EMPTY_COHORT`; pre `filter0_bridge_safe`; pre
  `body_bytes<=8388608` -> `REQUEST_TOO_LARGE` (the gateway's body limit); post
  `samples>0_per_group` -> `EMPTY_COHORT` (stop, `empty`).
- **Facts.** `f.sN.samples.<control|case>` (`/observed/groups/<i>/count`, samples, "${group} samples").

### 2.8 `de_preanalysis`

- **Endpoint.** `POST /protein-paint/termdb/DE`, golden `proteinpaint/termdb-DE-preAnalysis.json`
  body. Substituted: `/samplelst/groups/<i>/name` <- the group key (`control`, `case`: pre-analysis
  never depends on a cohort name, so a Rename reuses it) and `/samplelst/groups/<i>/values` <-
  `[{"sampleId"}]`. Fixed: `/genome`, `/dslabel`, `/samplelst/groups/<i>/in`, `/filter`, `/filter0`,
  `/preAnalysis`.
- **Input.** `{"groups": [{"key": "control", "sampleIds"}, {"key": "case", "sampleIds"}]}`.
- **Observed.** `{"groups": [{key, eligible}]}` from `data[<group name>]`.
- **Guards.** pre `samples<=4000` -> `REQUEST_TOO_LARGE`; post `eligible>=10_per_group` ->
  `INELIGIBLE_GROUP` (stop, step Done with its facts: the run stops **before the save**).
- **Facts.** `f.sN.eligible.<control|case>` (`/observed/groups/<i>/eligible`, samples,
  "${group} RNA-seq-eligible samples"): the confirmation card's eligible counts.

### 2.9 `de_run`

- **Endpoint.** `POST /protein-paint/termdb/DE`, golden `proteinpaint/termdb-DE-wilcoxon.json` body,
  so the tool and the viewer send identical requests. Substituted: `/method`,
  `/samplelst/groups/<i>/name` (the group name), `/samplelst/groups/<i>/values`, `/filter0` <- null
  (as the direct DE card sends it, so ProteinPaint's DE cache key matches),
  `/volcanoRender/significanceThresholds/pValueCutoff` <- `minus_log10(thresholds.pValueCutoff)`,
  `.../pValueType`, `.../foldChangeCutoff`. Without `thresholds` the golden's (1.3, adjusted, 0.3)
  stay. Fixed: `/kind`, `/genome`, `/dslabel`, `/min_count`, `/min_total_count`, `/cpm_cutoff`,
  `/filter`, `/samplelst/groups/<i>/in` and every other `/volcanoRender` field.
- **Input** (what `derive_request_pinned("deDirect"|"deInput")` reads):
  `{"groups": [{"name", "sampleIds", "cohortId"?, "filter0"?}] x2, "method", "thresholds"?}`, control
  first. `name` is the read-back saved name after Create, else the proposed name (C-24: the names
  are part of ProteinPaint's DE cache key). `cohortId` and `filter0` (the saved cohort's
  `case_id_filter`) are present together, once saved or reused.
- **Observed.** `{"method", "sampleSizes": {control, case}, "totals": {tested, significant, up, down},
  "genes": [DeGene], "cacheId"}` from `method`, `sample_size1/2`, `data.totalRows`,
  `data.totalSignificantRows/Up/Down`, `data.dots` (returned order, without `index`, `pixel_x`,
  `pixel_y`) and `data.cacheId`. `volcanoPng` and `plotExtent` are dropped. `DeGene` is
  `{gene_name, fold_change, original_p_value, adjusted_p_value}`; `fold_change` is
  log2(case / control). ProteinPaint returns only the genes past the thresholds.
- **Guards.** pre `samples<=4000` -> `REQUEST_TOO_LARGE`. Retry and 429 back-off as section 1; a
  retry resends the identical body and joins ProteinPaint's in-flight job or reads its cache.
- **Facts.** `f.sN.tested` (`/observed/totals/tested`, genes, "Genes tested"), `f.sN.significant`
  (`/observed/totals/significant`, genes, "Genes past the thresholds").

### 2.10 `kg_query` (stretch)

`kg___kg_query` on the Workstream 1 target. Input `{"question", "genes"}`, Observed
`{"statements": [{text, source}]}`. Until the target is registered (pre-guard
`kg_target_registered`) it answers status `unavailable` with `NOT_CONNECTED` and `KG_NOT_CONNECTED`
(template `kg.not_connected`). No analysis kind compiles
it in the MVP.

## 3. Executor functions

Pure, deterministic, versioned, recorded as steps (`transport` `in_process`, `httpStatus` null, no
provenance). Signatures and result models are in `tools/base.py`.

| Function | Input | Reads | Output (`base.py`) | Guards | Facts |
| --- | --- | --- | --- | --- | --- |
| `set_operation` | `{"op", "a", "b"?}` (`b` absent only for `size`) | `a`'s and `b`'s `/observed/caseIds` | `SetOperationResult` (`caseIds` sorted; empty for `size`) | post `count>0` -> `EMPTY_COHORT` (stop) | `f.sN.count` (`/observed/count`, cases, `${cases_label}` of the resulting cohort) |
| `check_subset` | `{"inner", "outer"}` | both `/observed/caseIds` | `SubsetResult` | post `inner_subset_of_outer` -> `NOT_SUBSET` (report) | `f.sN.outside` (`/observed/outsideCount`, cases, "${inner} outside the tested cases") |
| `rank_top_n` | `{"rows", "key", "rankBy", "n", "ascending", "tieBreak", "thresholds"?}` | `RANK_SOURCES[tool of rows]` | `RankResult` | post `rows>=n` -> `FEWER_THAN_N`, post `no_ties_at_cutoff` -> `TIES_AT_CUTOFF` (both report) | `RANK_FACT_SPECS` (below) |
| `derive_km_median` | `{"curves"}` | `/observed/groups[].{label, curve}` | `KmMedianResult` (unit `days`) | none | `f.sN.median.<key>` (`/observed/groups/<i>/median`, days, "${group} median overall survival"; only when reached) |

- **`set_operation`.** `difference` a minus b, `intersection`, `union`, `size` (count of a). A
  difference always follows a `check_subset(inner=b, outer=a)` (C-GUARD).
- **`check_subset`.** When inner has ids outside outer, `isSubset` is false, `outsideSample` lists up
  to 10 of them, and every later use of inner's ids intersects them with outer's (the StepRef
  `intersect`), so the plan uses only cases inside.
- **`rank_top_n`.** Rows: de_run's `/observed/genes`; top_mutated_genes' `/observed/genes` then its
  `/observed/nextGene` when present. With `thresholds`, a row passes when its p (adjusted or
  original, per `pValueType`) is below `pValueCutoff` and `|fold_change|` above `foldChangeCutoff`.
  `rankBy` is a row field or `abs(<field>)`; rows sort by value (`ascending`), then `tieBreak` in
  code point order. It keeps the top n plus every row whose value equals the n-th's
  (`tiesAtCutoff` lists their keys); `value` is the ranking value; `row` the source row unchanged;
  `count` = `len(rows)`. Zero rows: status `empty`, `FEWER_THAN_N`.
  | IR `rank_by` | `rankBy` | `ascending` |
  | --- | --- | --- |
  | `adjusted_p` (default) | `adjusted_p_value` | true |
  | `p_value` | `original_p_value` | true |
  | `fold_change` | `abs(fold_change)` | false |
  | top mutated genes | `cases` | false |
- **`derive_km_median`.** The median is the first curve time whose survival is at most 0.5; null and
  `reached` false when the curve never gets there.

**`rank_top_n` facts** (per row shown, `${pos}` 1-based; `count` only per section 1):

| Source, `rankBy` | Facts |
| --- | --- |
| `de_run`, `adjusted_p_value` | `top.${pos}.gene` (`/observed/rows/<i>/key`, gene, "Rank ${rank} gene"), `top.${pos}.log2fc` (`/row/fold_change`, log2FC, "${gene} log2 fold change"), `top.${pos}.padj` (`/value`, p, "${gene} adjusted p"), `count` (`/observed/count`, genes, "Ranked genes") |
| `de_run`, `original_p_value` | gene, log2fc, `top.${pos}.p` (`/value`, p, "${gene} p-value"), `top.${pos}.padj` (`/row/adjusted_p_value`), count |
| `de_run`, `abs(fold_change)` | gene, log2fc, `top.${pos}.padj` (`/row/adjusted_p_value`), count |
| `top_mutated_genes`, `cases` | gene, `top.${pos}.cases` (`/value`, cases, "${gene} mutated cases"), count |

## 4. Client actions

| Action | Args (`ToolSpec.input_model`) | The browser reports | Recorded | Writes | Click | When |
| --- | --- | --- | --- | --- | --- | --- |
| `read_cohort_context` | `{}` | `models.CohortContext` | the `ask`'s `cohortContext` | no | no | every `ask` and scope edit |
| `save_cohorts` | `models.SaveCohortsArgs` (the pending confirm with every definition) | `models.ConfirmAction` | `SaveCohortsObserved` `{decision, saved, makeActive}` | yes | yes | Create on the confirmation card; the run waits |
| `set_active_cohort` | `SetActiveCohortArgs` `{cohortId, name, origin}` | `SetActiveCohortResult` | the browser's log (run and step ids) | yes | yes | scope chip, "Make active and open", Create with make-active |
| `mount_viz` | `models.VizSpec` | `models.VizReport` (`vizReports`) | `vizReports` | no | no | from a viz spec; at most two live |
| `open_full_tool` | `OpenFullToolArgs` `{appId, cohortId, makeActive}` | nothing | nothing | no | yes | "Open with this cohort", Open `<app>` |

`save_cohorts` is the only client step a plan contains. Its step args are `SaveCohortsStepArgs`
`{"cohorts": [{ref, name, eligible?}]}`; at the pause the executor builds the pending payload from
them: each cohort's canonical definition and `defSha256` from the step that defined it, `caseCount`
and `caseCountFactId` from that step's count fact, `eligibleCount` and `eligibleCountFactId` from
the `eligible` StepRef's fact (J3; null for J2), and `reuse` from a saved cohort in `cohortContext`
whose `defSha256` matches. Its CallRecord (`transport` `client`) records that payload as `request`
and `SaveCohortsObserved` as `observed` after a verified Create; it never retries, and Don't save
records no call (the step is Skipped `WRITE_SKIPPED`). The read-back check is the contract's
(`confirm create`).

## 5. Step tables

Every table is in step order. **Before/after** says whether the step runs before the save pause
(B), is the write (W), or runs after confirm (A). After `confirm`, finished steps stay Done (the
same plan version resumes); after an edit at the pause, a step is Reused when its input hash is
unchanged. "Templates" names the Observed templates (7.1) and limitation templates (7.2) its facts
feed. `scopeFilter` means: nothing for an unfiltered scope; `{"in": {"case_id": {"ref": "<scope
step>", "pointer": "/observed/caseIds"}}}` for a filtered one (C-23).

### 5.1 J1: survival by a split field, unfiltered scope (`j1-complete`)

IR `survival_compare {split_term, split_values?}`; groups = the split values (all of the field's
values when `split_values` is absent). N groups.

| Step | Row | Tool | Args | B/A | Facts | Templates, viz |
| --- | --- | --- | --- | --- | --- | --- |
| s1 | 1 (folded) | `fixture_fingerprint` | `{}` | B | `f.s1.cases` | scope count |
| s2 ... s(N+1) | 1 "Count both {group} groups" (N = 2) or "Count the {N} {group} groups" | `cohort_count` | `{"filter": {"and": [scopeFilter..., {"in": {<split field>: [<value>]}}]}}` | B | `f.sK.count` ("<value> cases") | `survival.group`, `survival.group_count`, `survival.small_group`, `empty.*` |
| s(N+2) | 2 "Compare survival: Kaplan–Meier, split by {field label}" | `survival_compare` | `{"scope": null, "term": "overall_survival", "split": {"field", "overlay": <twin>}, "groups": [{"label": <value>, "filter": <its count filter>}]}`, dependsOn the counts | B | `events.<key>`, `p` | `survival.group`, `survival.p`, `survival.viewer_p`, `survival.no_hazard_ratio`; survival card (cites this step) |
| s(N+3) | 2 | `derive_km_median` | `{"curves": "s(N+2)"}` | B | `median.<key>` | `survival.median`; frozen table |

Filtered scope: row 1 becomes "Find cases in {scope name}" with s1 and s2 `cases_query`
`{"filter": gql_filter(scope clauses), "pageSize": 1000, "maxIds": 10000}` (`f.s2.count` is the
scope count); the counts and survival follow on rows 2 and 3 with `scopeFilter` set and
`"scope": {"and": [{"in": {"case_id": StepRef(s2)}}]}`. Fixtures: `j1-partial` (s4 fails, s5
Skipped "Not run: s4 failed", Observed `survival.group_count`), `j1-retry`, `j1-stop`.

**Survival between cohorts** (IR `groups`, such as J3's follow-up "Compare survival between the
KRAS mutated and KRAS not detected cohorts"): the groups' cohorts compile first (6.1), each
group's count is the fact of the step that defines it (a `cohort_count` for a filter cohort), and
`survival_compare` has `"split": null` and groups `{"label": <cohort label>, "filter": <its filter or
case_id_filter(StepRef)>}`. No survival card (no ProteinPaint overlay term); Open Cohort Comparison
is offered (fits with hand-off).

### 5.2 J2: top mutated genes in a planned cohort, one save

IR: a filter cohort (J2's race cohort) and `top_mutated_genes {cohort, n?}`. Unfiltered scope:

| Step | Row | Tool | Args | B/W/A | Facts | Templates, viz |
| --- | --- | --- | --- | --- | --- | --- |
| s1 | 1 (folded) | `fixture_fingerprint` | `{}` | B | `f.s1.cases` | scope count |
| s2 | 1 "Find {cohort name} cases" | `cases_query` | `{"filter": gql_filter(cohort clauses), "pageSize": 1000, "maxIds": 10000}` | B | `f.s2.count` | `genes.cohort`, cohort count, card count |
| s3 | 2 "Rank mutated genes: top {n}" | `top_mutated_genes` | `{"filter0": {"and": [{"in": {"case_id": StepRef(s2)}}]}, "geneCount": n}` | B | none | OncoMatrix card (cites s3) |
| s4 | 2 | `rank_top_n` | `{"rows": "s3", "key": "gene", "rankBy": "cases", "n": n, "ascending": false, "tieBreak": "gene"}` | B | `top.<pos>.gene`, `top.<pos>.cases` | `genes.top`, `rank.ties_oncomatrix`, `rank.fewer`, `rank.none_genes`, frozen table |
| s5 | 3 "Save 1 cohort" | `save_cohorts` | `{"cohorts": [{"ref": <cohort>, "name": "<name> (ORION)"}]}` | W | none | `save.skipped_one` on Don't save |

The run pauses at s5 (AwaitingConfirmation, `eligibleCount` null) once s1 to s4 are Done; Observed
and the card render when the run reaches Answering after the confirm turn. The saved cohort's
definition is its filter (scope clauses plus the cohort's, `kind: filter`), so an identical saved
filter cohort is reused. A filtered scope adds the scope's `cases_query` as s2 (row 1 "Find cases
in {scope}"), the cohort's `cases_query` filter becomes `{"and": [scopeFilter, cohort clauses...]}`
and every later step id shifts by one.

**Top mutated genes in the scope** (the cohort is `base`: "top 3 genes in my cohort"): no planned
cohort and no save. Unfiltered: s1 (folded under row 1 "Rank mutated genes: top {n}"), s2
`top_mutated_genes {"filter0": null, ...}`, s3 `rank_top_n {"rows": "s2", ...}`; `genes.cohort` cites
`f.s1.cases`. Filtered: s1, s2 `cases_query` (row 1 "Find cases in {scope}", `f.s2.count`), s3
`top_mutated_genes` (`filter0` from s2), s4 `rank_top_n` (row 2): the `oncomatrix` fixture's step
layout.

### 5.3 J3: differential expression between two gene-defined cohorts, two saves (`j3-save-pause`, `j3-after-confirm`)

IR as in A2: `tested = mutation_tested(base)`, `kras_mut = gene_mutated(KRAS, tested)`,
`kras_wt = difference(tested, kras_mut)`, `differential_expression {group1: kras_wt, group2: kras_mut, top_n: 10}`.
Unfiltered scope:

| Step | Row | Tool | Args | B/W/A | Facts | Templates, viz |
| --- | --- | --- | --- | --- | --- | --- |
| s1 | 1 (folded) | `fixture_fingerprint` | `{}` | B | `f.s1.cases` | scope count; `de.untested` |
| s2 | 1 "Find KRAS-mutated cases" | `gene_cases` | `{"gene": "KRAS", "cohortFilter": {"and": []}}` (the scope, not `tested`: s4 checks) | B | `f.s2.count` | `de.cohorts`; `kras_mut` count |
| s3 | 2 "Find mutation-tested cases" | `cases_query` | `{"filter": {"and": [{"in": {"available_variation_data": ["ssm"]}}]}, "pageSize": 1000, "maxIds": 10000}` | B | `f.s3.count` | `de.cohorts`, `de.untested`; `tested` count |
| s4 | 3 "Subtract to get KRAS not detected" | `check_subset` | `{"inner": "s2", "outer": "s3"}` | B | `f.s4.outside` | `subset.outside` |
| s5 | 3 | `set_operation` | `{"op": "difference", "a": "s3", "b": "s2"}` | B | `f.s5.count` | `de.cohorts`; `kras_wt` count |
| s6 | 4 "Check RNA-seq-eligible group sizes" | `sample_lists` | `{"groups": [{"key": "control", "caseIds": {"ref": "s5", "pointer": "/observed/caseIds"}}, {"key": "case", "caseIds": {"ref": "s2", "pointer": "/observed/caseIds", "intersect": "s3"}}]}` | B | `samples.control`, `samples.case` | `de.too_many_samples`, `empty.samples` |
| s7 | 4 | `de_preanalysis` | `{"groups": [{"key": "control", "sampleIds": StepRef(s6, /observed/groups/0/sampleIds)}, {"key": "case", "sampleIds": StepRef(s6, /observed/groups/1/sampleIds)}]}` | B | `eligible.control`, `eligible.case` | `de.groups`, `de.ineligible`; card eligible counts |
| s8 | 5 "Save 2 cohorts" | `save_cohorts` | `{"cohorts": [{"ref": "kras_wt", "name": "KRAS not detected (ORION)", "eligible": StepRef(s7, /observed/groups/0/eligible)}, {"ref": "kras_mut", "name": "KRAS mutated (ORION)", "eligible": StepRef(s7, /observed/groups/1/eligible)}]}` | W | none | `save.skipped` on Don't save |
| s9 | 6 "Run DE: Wilcoxon, rank by adjusted p" | `de_run` | `DeRunStepArgs` `{"groups": [{"name": "KRAS not detected (ORION)", "cohort": "kras_wt", "sampleIds": StepRef(s6, /observed/groups/0/sampleIds)}, {"name": "KRAS mutated (ORION)", "cohort": "kras_mut", "sampleIds": StepRef(s6, /observed/groups/1/sampleIds)}], "method": "wilcoxon", "thresholds": {"pValueCutoff": 0.05, "pValueType": "adjusted", "foldChangeCutoff": 0.3}}` | A | `tested`, `significant` | `de.totals`; DE card (cites s9, s10) |
| s10 | 6 | `rank_top_n` | `{"rows": "s9", "key": "gene_name", "rankBy": "adjusted_p_value", "n": 10, "ascending": true, "tieBreak": "gene_name", "thresholds": <same>}` | A | `top.<pos>.gene`, `.log2fc`, `.padj` | `de.top`, `rank.cap`, `rank.ties`, `rank.fewer`, `rank.none_de`; frozen table |

- **Pause.** s1 to s7 are Done and s8 AwaitingUser: the confirmation card shows `caseCountFactId`
  `f.s5.count` and `f.s2.count`, `eligibleCountFactId` `f.s7.eligible.control` and
  `f.s7.eligible.case`, control first. A group under 10 eligible stops at s7 (Limited
  `INELIGIBLE_GROUP`) before any write.
- **The read-back enters s9** (C-24): `de_run_input(args, read, saved)` replaces each group's name by
  the read-back `name` and adds `cohortId` and `filter0` (`case_id_filter` of the saved definition)
  for a saved or reused cohort; after Don't save the proposed names stay and no cohort id is sent
  (`j3-dont-save`). Cohorts become `saved`/`reused` with `cohortId` and `savedName`, or `skipped`.
- **Run cohorts** in plan order: `base` (scope), `tested` ("Mutation-tested cases", definition the
  ssm filter, `save: none`), `kras_mut`, `kras_wt` (case-ID lists, `save: proposed`), each sized
  when its defining step finishes (`f.s3.count`, `f.s2.count`, `f.s5.count`).
- **Filtered scope.** s2 becomes the scope's `cases_query` (row 1 "Find cases in {scope}"),
  `gene_cases.cohortFilter` and the tested filter take `scopeFilter`, and every later id shifts by
  one (11 steps, 7 rows).
- **DE between two other cohorts** (filter or thread cohorts, IR `differential_expression {group1,
  group2}`): each group's defining steps (a `cases_query` per filter cohort; `check_subset` plus
  `set_operation` per difference), then sample_lists, de_preanalysis, the save of both, de_run and
  rank_top_n as s6 to s10 here. `de.cohorts` and `panel.gene` apply only to gene-defined groups.

### 5.4 Cohort size

| Cohort | Steps |
| --- | --- |
| `base`, unfiltered | s1 (folded under row 1 "Count {scope name}"), s2 `cohort_count {"filter": {"and": []}}`; `size.count` cites `f.s2.count` |
| `base`, filtered | s1, s2 `cases_query` of the scope (row 1 "Count {scope name}"); `size.count` cites `f.s2.count` |
| a planned filter cohort | (the scope step if filtered), then `cohort_count {"filter": {"and": [scopeFilter, clauses...]}}` |
| a computed cohort | its defining steps, then `set_operation {"op": "size", "a": <its step>}` |

No viz spec; zero is `empty.count` with `empty.cohort`.

### 5.5 The OncoMatrix viz

The OncoMatrix card cites the `top_mutated_genes` step (5.2): `pinned = derive_request_pinned("oncomatrix", request, observed)`
(`filter0` from the request, `genes` = Observed `genes` in returned order), so `requestPinnedHash`
equals `pinnedHash` unless a tie added a gene beyond the chart (then `rank.ties_oncomatrix` says
OncoMatrix shows the first n). Frozen table: `Gene`, `Mutated cases` from `rank_top_n`'s facts.

### 5.6 The DEinput fallback (cut 2)

The same J3 steps. The DE card uses launcher `deInput` instead of `deDirect` only when the deployment
selects the fallback (`ORION_DE_LAUNCHER=deInput`, proposed setting) **and** both groups are saved or
reused, so `de_run`'s request carries `cohortId` and `filter0` and
`derive_request_pinned("deInput")` has what it needs (`j3-deinput`). After Don't save the card stays
`deDirect` and hand-off is off (`WRITE_SKIPPED`). The caption tells the researcher to choose
Wilcoxon and click Run.

### 5.7 Step summaries

Code text on the step row (exempt from fact chips), as the fixtures write them:

| Tool | `inputSummary` | `outputSummary` |
| --- | --- | --- |
| `fixture_fingerprint` | sandbox status, versions, ranking | fingerprint {first 4 hex}… |
| `cohort_count` | {field label} = {value} (`compiler.summarize`) | {n} cases |
| `cases_query` | the clauses summarised, or "Mutation-tested cases (available variation data = ssm)" | {n} case IDs, {pages} page(s) |
| `gene_cases` | {GENE}-mutated cases | {n} case IDs |
| `check_subset` | {GENE}-mutated cases inside the mutation-tested cases | All {n} inside the mutation-tested cases / {k} of {n} outside the mutation-tested cases |
| `set_operation` | Mutation-tested minus {GENE}-mutated | {n} case IDs |
| `sample_lists` | Both groups as ProteinPaint samples | {a} and {b} samples |
| `de_preanalysis` | RNA-seq-eligible samples per group | {a} and {b} RNA-seq eligible |
| `save_cohorts` | {name}, {name} | 2 cohorts saved and read back / 1 saved, 1 reused / Not saved: you chose Don't save |
| `de_run` | {Method}, {control} vs. {case} | {significant} of {tested} genes past the thresholds |
| `rank_top_n` | Top {n} by adjusted p / Top {n} by mutated cases, ties disclosed | Top {n} by adjusted p, no ties / Top {n}, no ties / Top {n}, {k} tied |
| `top_mutated_genes` | {cohort values} cases, top {n} | {n} genes |
| `survival_compare` | {N} groups, Kaplan–Meier | {N} curves, log-rank p reported |
| `derive_km_median` | {N} curves | {N} medians |

Failed and skipped rows: "Timed out on both attempts", "Not run: s4 failed", "Not run: stopped",
"Not run: interrupted".

### 5.8 Where outside values enter

| Value | Validated | Enters | How |
| --- | --- | --- | --- |
| A browser cohort filter (`cohortContext` active or saved) | compile time, like planner output (C-SCOPE, C-VOCAB): an unsupported or unreadable field stops the plan as a limitation, no data call | the scope's `cases_query` (once) | every later step refers to its `/observed/caseIds` by StepRef (C-23); no later step re-sends the browser filter, and ProteinPaint sees only case-ID lists |
| A thread cohort (session ledger) | compile time (C-VOCAB on its clauses) | the step that rebuilds it | as a planned cohort |
| The confirm read-back (`saved`: cohortId, name, defSha256) | the contract's `confirm create` checks | `de_run` (names, cohortId, filter0); the run's cohorts | `de_run_input` |
| A Rename (`edit_plan cohortName`) | the contract | `save_cohorts` args and `de_run` args | the proposed name in both |

## 6. From the IR to steps

### 6.1 IR constructs to steps

| IR | Steps |
| --- | --- |
| Scope (`base_cohort`, a follow-up's thread scope, or the default) | s1; a filtered scope adds one `cases_query` |
| `filter {clauses}` | inline as a Gen3 filter (`scopeFilter` plus its clauses) for Gen3 counts and survival groups; a `cases_query` when its case IDs are needed (ProteinPaint, a set operation, DE, a save) |
| `saved {cohort_name}`, `thread {ledger_ref}` | as `filter`, from the saved cohort's FilterSet or the ledger cohort's clauses (validated at compile time) |
| `mutation_tested {within}` | `cases_query` of `within`'s filter plus `available_variation_data in [ssm]` |
| `gene_mutated {gene, within}` | `gene_cases` with `cohortFilter` = the scope's filter (a computed `within`: its `case_id_filter`); its ids are intersected with `within`'s where they are used |
| `difference {a, b}` | `check_subset {inner: b, outer: a}`, then `set_operation {op: difference, a, b}` |
| `intersection {a, b}` | `set_operation {op: intersection, a, b}` |
| `survival_compare {split_term, split_values?}` / `{groups}` | 5.1 |
| `top_mutated_genes {cohort, n?}` | 5.2 |
| `differential_expression {group1, group2, method?, rank_by?, top_n?}` | 5.3 (group1 is the control) |
| `cohort_size {cohort}` | 5.4 |

### 6.2 What the compiler adds

| Rule | Adds |
| --- | --- |
| Always | s1 `fixture_fingerprint`, folded under row 1, before any data call |
| C-SCOPE | one `cases_query` for a filtered scope, right after s1; it sizes the scope |
| A filter cohort used by ProteinPaint or a set operation | a `cases_query` of scope clauses plus its own |
| C-UNTESTED (`difference(x, gene_mutated)`) | `x` rewritten to mutation-tested within `x` (assumption `EXCLUDE_UNTESTED`); no extra step: `de.untested` states the excluded cases from two facts |
| C-GUARD | `check_subset(inner=b, outer=a)` before every `set_operation difference`; the guards of section 2 |
| C-CASEIDS | case-ID StepRefs wherever ProteinPaint needs a cohort (`filter0`, `sample_lists`) |
| C-SPLIT | `split.overlay` = the field's ProteinPaint twin; no twin: `split` null, no survival card |
| C-SAVE | J2: one `save_cohorts` after the ranking; DE: one `save_cohorts` of both groups after `de_preanalysis` and before `de_run`; `writes: true` |
| C-DEFAULTS | DE: `method` wilcoxon (`DE_METHOD_WILCOXON`), `rankBy` adjusted p (`RANK_ADJ_P`), thresholds adjusted p < 0.05 and abs(log2FC) > 0.3 (`DE_THRESHOLDS_VIEWER`), control = group1 first (`CONTROL_FIRST`); top N 10 for DE and 3 for top mutated genes when the question gives none (`TOP_N_DEFAULT`) |
| J2, J3 | `rank_top_n` after `top_mutated_genes` and `de_run`; `sample_lists` and `de_preanalysis` before the DE save |
| CALL_BUDGET | each step is one counted call; a plan over 20 steps stops at compile time |

Assumption texts follow the fixtures ("Method: Wilcoxon rank-sum", "Genes ranked by adjusted
p-value", "Thresholds: adjusted p < 0.05 and |log2FC| > 0.3, the viewer's defaults",
"{control} is the control group: fold change is log2({case} / {control})").

### 6.3 Rows, reasons and names

| Row tool | Reason (plan card) |
| --- | --- |
| `cohort_count` | Cohort Builder counts filter cohorts |
| `cases_query`, `gene_cases` | Gen3 Analysis API lists case IDs |
| `set_operation` | ORION computes set operations in code |
| `survival_compare` | Correlation Plot runs survival comparisons |
| `top_mutated_genes` | OncoMatrix ranks mutated genes |
| `de_preanalysis` | Differential Expression checks eligible samples before any save |
| `save_cohorts` | Cohort Builder saves cohorts after your click |
| `de_run` | Differential Expression runs the comparison in its own viewer |

The first four are `catalog.TOOL_REASONS`; the last three must be added there. Proposed names
carry " (ORION)": `{GENE} mutated (ORION)`, `{GENE} not detected (ORION)`, a filter cohort's name
plus the suffix; definition texts as the fixtures write them ("Mutation-tested cases with a KRAS
mutation, as a case-ID list").

### 6.4 Editable fields and what each edit changes

| `plan.editable` | Offered for | Changes |
| --- | --- | --- |
| `scope` | every plan | every step's args (recompile); signature |
| `method` | DE | `de_run.method`; row 6 label; `DE_METHOD_*` assumption; s9 input summary |
| `topN` (max 100 DE, 50 genes) | DE, top mutated genes | `rank_top_n.n`; for top mutated genes also `top_mutated_genes.geneCount` |
| `thresholds` | DE | `de_run.thresholds` and `rank_top_n.thresholds`; `de.totals_edited`, `viz.de.note_edited` |
| `cohortName` (`ref`) | each proposed cohort | that cohort's `name` in `save_cohorts` args and its group `name` in `de_run` args |

## 7. Templates

`TEMPLATES` in `schemas.py`. A pattern holds code text, `${var}` (code values, inserted as text and
never parsed again, so a cohort name cannot inject a placeholder), `{{f.<role>.<suffix>}}` (a fact of
the step bound to `<role>`; a `${var}` inside the suffix must bind fact-id segments) and
`{{<role>}}` (a whole fact id bound to `<role>`, such as the cohort count `f.s2.count` or
`f.s1.cases`). `${items}` expands repeated items with `joiner` (and `last_joiner` before the last).
`template_parts` binds a pattern into text and fact parts; each fact part becomes a Segment with
`factId` and text `format_fact(value, unit)`. Adjacent text is merged: text segmentation carries no
meaning. After binding, every fact reference is in `interfaces.PLACEHOLDER`'s grammar, which the
composer and verifier also use. A template applies only when every fact it cites exists.

### 7.1 Observed

| Id | Pattern | When |
| --- | --- | --- |
| `survival.group` | `${group}: {{f.<count>.count}} cases, {{f.<survival>.events.${key}}} events.` | per group, once `survival_compare` is Done |
| `survival.group_count` | `${group}: {{f.<count>.count}} cases.` | per group when `survival_compare` did not finish Done |
| `survival.p` | `Log-rank p = {{f.<survival>.p}} (Gen3 Analysis).` | the p fact exists |
| `survival.median` | `Median overall survival: ${items} (Kaplan–Meier median derived by ORION from the engine's curves).`; items `reached` `${group} {{f.<median>.median.${key}}} days`, `not_reached` `${group} not reached`; joiner `; ` | `derive_km_median` Done |
| `genes.cohort` | `The cohort has {{<cohort_count>}} cases.` | the ranked cohort is sized |
| `genes.top` | `The most frequently mutated genes are ${items}.`; `first` `{{gene}} ({{cases}} of {{<cohort_count>}} cases)`, `rest` `{{gene}} ({{cases}})`; `, ` and ` and ` | two or more rows |
| `genes.top_one` | `The most frequently mutated gene is ${items}.` | one row |
| `genes.none` | `No gene in the six-gene panel is mutated in this cohort.` | `top_mutated_genes` Empty |
| `de.cohorts` | `Mutation-tested cases: {{f.<tested>.count}}; ${gene}-mutated: {{f.<mutated>.count}}; ${gene} not detected: {{f.<difference>.count}}.` | gene-defined groups, the difference Done |
| `de.untested` | `Cases without mutation data are left out: {{f.<tested>.count}} of {{<scope_count>}} cases are mutation-tested.` | gene-defined groups, tested count below the scope count |
| `de.groups` | `Groups: ${control} with {{f.<pre>.eligible.control}} RNA-seq-eligible samples (control), and ${case} with {{f.<pre>.eligible.case}} (case).` | `de_preanalysis` Done; the names `de_run` sent |
| `de.method` | `Method: ${method}; genes ranked by ${rank}; fold change is log2(${case} / ${control}), so a positive value is higher in ${case}.` | every DE answer |
| `de.totals` / `de.totals_edited` | `{{f.<de>.significant}} of {{f.<de>.tested}} tested genes passed the viewer's thresholds.` / `... passed the thresholds set on the plan.` | `de_run` Done (default / edited thresholds) |
| `de.top` | `Top-ranked genes by ${rank}: ${items}.`; `adjusted`/`fold` `{{gene}} (log2FC {{log2fc}}, adjusted p {{padj}})`, `original` adds `p {{p}}, `; joiner `; ` | `rank_top_n` Done with rows |
| `rank.cap` | `Only the first part of the ranking is shown; all {{f.<ranking>.count}} ranked genes are in the run record.` | more rows than shown |
| `size.count` | `${cohort}: {{<count>}} cases.` | cohort size |
| `empty.count` | `${cohort}: {{<count>}} cases match ${filter}.` | a count step Empty (the zero, with the filter) |

Order: J1 groups, p, median; J2 cohort, top, cap; J3 cohorts, untested, groups, method, totals, top,
cap. V-COMP requires J1's `survival.group` and `survival.p`, J2's `genes.cohort` and `genes.top`, and
J3's `de.groups`, `de.method` and `de.top`. Words: `DE_METHOD_LABELS`, `RANK_LABELS`, `RANK_SHORT`,
`DE_TOP_ITEM`. Partial, Limited, Cancelled and Interrupted runs render the templates whose facts
exist.

### 7.2 Limitations

Each carries its code, the plain `text`, `unblock` (code text, `${var}` filled), `nearestApps`, and
`segments` (the same line with chips) exactly when it cites a fact.

| Id | Code | Text | Unblock | When |
| --- | --- | --- | --- | --- |
| `survival.viewer_p` | `VIEWER_P_ROUNDED` | The survival chart shows this p-value as p = 0; the value above comes from Gen3 Analysis. | none | p < `VIEWER_P_FLOOR` (1e-16) |
| `survival.no_hazard_ratio` | `STAT_UNAVAILABLE` | The platform reports no hazard ratio for this comparison, so the answer gives none. | none | every survival answer |
| `survival.small_group` | `INELIGIBLE_GROUP` | Too few cases to compare survival: ${group} has {{<count>}} cases, and each group needs at least 10. | Choose a split with larger groups, or a broader scope. | survival pre-guard |
| `tool.failed` | `TOOL_FAILED` | The ${tool} ${failure}, so ${missing}. | Retry step reruns the ${tool} with the same request. | a read Failed |
| `tool.failed_write` | `TOOL_FAILED` | Saving the cohorts failed, so ${missing}. | Ask again to save the cohorts. | the write failed |
| `empty.cohort` | `EMPTY_COHORT` | No case matched ${filter}: either no case has these values, or the API returned an empty set for a field it does not index. | Check the values on the plan, or ask about a broader cohort. | a count Empty |
| `empty.samples` | `EMPTY_COHORT` | ${group} has no ProteinPaint sample, so differential expression cannot run. | Choose groups with RNA-seq data. | sample_lists Empty |
| `ids.too_many` | `TOO_MANY_IDS` | ${cohort} has {{<count>}} cases, more than the 10,000 case IDs one step can carry, so the steps that need its case list did not run. | Narrow the cohort with another filter. | cases_query over 10,000 |
| `subset.outside` | `NOT_SUBSET` | {{f.<subset>.outside}} ${inner} are outside the ${outer}; only the cases inside were used. | none | check_subset |
| `de.ineligible` | `INELIGIBLE_GROUP` | Too few RNA-seq-eligible samples for differential expression: ${control} has {{eligible.control}} and ${case} has {{eligible.case}}; each group needs at least 10, so nothing was saved. | Choose larger groups, such as a broader scope. | de_preanalysis post-guard |
| `de.too_many_samples` | `REQUEST_TOO_LARGE` | The groups have {{samples.control}} and {{samples.case}} samples, more than the 4,000 one differential expression run takes. | Narrow the groups, such as with a smaller scope. | 4,000-sample guard |
| `samples.too_large` | `REQUEST_TOO_LARGE` | The sample lookup for ${group} would exceed the 8 MiB the gateway accepts. | Narrow the group. | body guard |
| `filter.not_bridge_safe` | `UNSUPPORTED_FIELD` | ${field} cannot filter ProteinPaint directly, so this step did not run. | Remove that filter, or ask about all cases. | bridge guard |
| `rank.fewer` | `FEWER_THAN_N` | Only {{f.<ranking>.count}} genes ranked, fewer than the ${n} asked for. | none | 0 < rows < n |
| `rank.none_de` / `rank.none_genes` | `FEWER_THAN_N` | No gene passed the thresholds, so there is no ranking. / No gene in the panel is mutated in this cohort, so there is no ranking. | none | zero rows |
| `rank.ties` / `rank.ties_oncomatrix` | `TIES_AT_CUTOFF` | ${items} tied with the last gene in the top ${n}, so the list is longer than ${n}. / ...; OncoMatrix shows only the first ${n}. | none | ties at the cutoff (items: the tied genes' gene facts) |
| `panel.gene` | `PANEL_COVERAGE` | ${gene} status comes from the six-gene mutation panel (KRAS, NRAS, TP53, BRAF, DIS3, MYC); other mutations are not profiled. | none | a gene-defined cohort |
| `panel.ranking` | `PANEL_COVERAGE` | Mutation data covers the six-gene panel only. | none | every top mutated genes answer |
| `save.skipped` / `save.skipped_one` | `WRITE_SKIPPED` | You chose Don't save, so no cohort was saved: the chart uses the unsaved groups and Open with this cohort is off. / You chose Don't save, so the cohort was not saved and Open with this cohort is off. | Ask again and choose Create to save the cohorts. / ... the cohort. | Don't save (DE / J2) |
| `kg.not_connected` | `KG_NOT_CONNECTED` | The knowledge-graph agent is not connected yet, so no knowledge-graph result is shown. | none | kg_query |

The controller's own lines (`RUN_INTERRUPTED`, `NAMED_TOOL_MISMATCH`, compile-time stops) are the
contract's and the compiler's.

## 8. Viz specs

| Analysis | `appId`, `launcher` | `citesSteps` (first derives `requestPinnedHash`) | Pinned from | Title | Caption | Frozen columns, rows, note | `cohortRefs` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| survival by a split | `Correlation`, `survival` | the `survival_compare` step | its request: `filter0` = scope, `term`, `overlay` | `Overall survival by ${split}` | `viz.survival.caption` with `VIEWER_P_ROUNDED`, else null | Group, Cases, Events, Median (days); per group the label, the count chip, the events chip, the median chip or "not reached"; note `Log-rank p = {{p}} (Gen3 Analysis)` | `["base"]` |
| top mutated genes | `OncoMatrix`, `oncomatrix` | the `top_mutated_genes` step | its request's `filter0` and its Observed `genes` | `Top mutated genes: ${cohort}` | null | Gene, Mutated cases; up to 10 ranked rows; no note | the ranked cohort's ref, or `["base"]` |
| DE | `DE`, `deDirect` | `[de_run step, rank_top_n step]` | de_run's request: groups (name, sampleIds), method, volcano from thresholds | `Differential expression: ${case} vs. ${control}` (labels without " (ORION)") | `Table sorted by fold change. Sort by adjusted p-value to match the answer.` | Gene, log2FC, Adjusted p; the top 10; note `Ranked by ${rank_short}; {{significant}} genes passed the viewer's thresholds.` (or `viz.de.note_edited`) | `[control ref, case ref]` |
| DE fallback | `DE`, `deInput` | as deDirect | de_run's request: groups (name, cohortId, filter0) | as deDirect | `viz.de_input.caption` | as deDirect | as deDirect |
| cohort size | none | | | | | | |

Template ids: `viz.survival.title`, `viz.survival.caption`, `viz.survival.note`,
`viz.oncomatrix.title`, `viz.de.title`, `viz.de.caption`, `viz.de_input.caption`, `viz.de.note`,
`viz.de.note_edited`; the shapes are `VIZ_TEMPLATES`.

`pinned` is `derive_request_pinned` over the cited call's recorded request (and output for
OncoMatrix), so `pinnedHash` equals `requestPinnedHash` by construction; `vizId`s continue across
plan versions. Survival with `split` null emits no card. A skipped cohort disables hand-off with
the `WRITE_SKIPPED` reason (contract "Hand-off").

## 9. Choices this catalog makes

Each is a decision where the design, the contract or the fixtures left room; builders follow it.

1. **J2's save comes last** (s5, after the ranking), so J2's s1 to s4 keep the `oncomatrix`
   fixture's ids and the analysis never waits on a write; Observed renders after the confirm. The
   `oncomatrix` fixture itself (a planned filter cohort, no save) predates C-09: under this catalog
   its question compiles with a fifth step `save_cohorts`.
2. **Tie detection for top mutated genes** asks ProteinPaint for `geneCount + 1` genes and keeps the
   extra row as `nextGene`, so `genes` stays what the card pins.
3. **C-23 everywhere:** a filtered scope's case IDs (not its clauses) reach every later step, Gen3
   tools included. The request-pinned vector with a clause `scope` remains a valid derivation case.
4. **gene_cases runs within the scope, not within the tested cohort**, so `check_subset` has meaning;
   a non-subset is intersected through StepRef `intersect`, not an extra step.
5. **C-UNTESTED adds no step**; `de.untested` states the excluded cases from the scope and tested
   counts (the J3 fixture has ten steps).
6. **Pre-analysis uses the keys `control` and `case` as group names** so a Rename reuses s7, as
   `j3-edit-at-pause` shows.
7. **de_run sends `filter0: null`**, as the spike's direct DE card does, so ProteinPaint's cache key
   matches the card's request.
8. **Survival curves** are the engine's donor estimates read as section 2.6 describes, not a
   recomputed Kaplan–Meier.
9. **`VIEWER_P_FLOOR` = 1e-16**: ProteinPaint's survival server printed "0" for p = 7.1e-60 locally;
   the floor is an Assumption from its `1 - pchisq` arithmetic.
10. **Engines** are the deployed pins (sources.json commits, ProteinPaint 2.216.0), constants in
    `schemas.py`; the fixtures' engine strings are illustrative.
11. **Defaults for top N** when the question gives none: 10 (DE) and 3 (top mutated genes).
12. **Survival between cohorts** has no survival card (no overlay term) and offers Cohort Comparison.
13. **Facts are cited by count steps, not by survival's own `cases`** (the J1 fixture has no
    `cases.<key>` fact); a survival group with fewer cases with survival data than its count is
    visible only in the call record.
14. **The DE fallback is a deployment setting** (`ORION_DE_LAUNCHER`), not a per-run choice.
