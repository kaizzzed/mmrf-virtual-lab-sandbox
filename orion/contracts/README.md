# ORION run-event contract (v1, amended 2026-11-04)

The browser, the BFF route and the agent share one wire contract: AG-UI events over
server-sent events, with ORION's run view carried in `STATE_SNAPSHOT`. Design source:
ORION RFA rev 25, sections A2 "APIs and events" and "Data model", F2 "Run and step states".

`fixtures/j1-stub.events.jsonl` is the reference stream. The agent tests validate it
against the Pydantic models, and the frontend reducer tests replay a byte-identical copy
at `.sources/frontend/src/features/orion/agui/__fixtures__/j1-stub.events.jsonl` (and its
string copy `j1Stub.ts`). Change all of them together. Its numbers are illustrative stub
values, not oracle results.

`fixtures/viz-pinned-vectors.json` holds the `pinnedHash` test vectors. Both sides must
reproduce every `canonical` string and `sha256` exactly.

The amendment settled six gaps found in the Day 1 AM review: rejections versus failures,
reporting Interrupted, a null scope count, pinned viz inputs, the nearest app on an
Unsupported result, and `NOT_IMPLEMENTED` as a stub code.

## Wire format

- One POST per turn. The response is `text/event-stream`.
- Each event is one frame: `data: <compact JSON>\n\n`, camelCase keys (ag-ui-protocol 1.0.0).
- Readers ignore `event:`, `id:` and `retry:` fields and `:` comment lines, and accept `\r\n`.
- A response that is not `text/event-stream` (for example a 400 JSON body or a 307 redirect)
  is a transport error, not a run event.
- Exactly one `RUN_FINISHED` or `RUN_ERROR` ends every invocation.

## Request: `RunAgentInput`

The browser sends it to the BFF, and the BFF forwards it unchanged. All seven fields are
always present.

```json
{"threadId": "<32 hex>", "runId": "<32 hex>", "state": {}, "messages": [{"id": "<id>", "role": "user", "content": "<question>"}],
 "tools": [], "context": [], "forwardedProps": {"orion": {"action": "ask", "...": "..."}}}
```

Thread and run ids are 32 lowercase hex characters (128 random bits); anything else is rejected.
`runId` names the run the action applies to: a new id for `ask`, the paused run for resume
actions, the target run for `cancel`, and a fresh id for `warmup`.

| `forwardedProps.orion.action` | Extra fields | Day 1 AM |
| --- | --- | --- |
| `warmup` | none | Implemented: `RUN_STARTED`, then `RUN_FINISHED` with `result.status = "Warmed"` |
| `ask` | `question`, `cohortContext` | Implemented (stub planner and executor) |
| `cancel` | none | Implemented |
| `clarify_answer` | `optionId` | Reserved: `RUN_ERROR` `NOT_IMPLEMENTED` |
| `consent` | `accept` | Reserved |
| `confirm` | `nonce`, `decision` (`create` or `dont_save`), `saved` | Reserved |
| `edit_plan` | `field`, `value` | Reserved |
| `retry_step` | `stepId` | Reserved |
| `viz_report` | `vizId`, `mounted`, `error` | Reserved |

`cohortContext` is the browser's cohort snapshot. The compiler validates it like planner output.

```json
{"active": {"id": "<cohort id or null>", "name": "All cases", "filters": {"mode": "and", "root": {}}, "caseCount": 1000},
 "saved": [{"id": "<cohort id>", "name": "<name>", "filters": {"mode": "and", "root": {}}, "caseCount": 334}]}
```

## Events

| Type | Fields | When |
| --- | --- | --- |
| `RUN_STARTED` | `threadId`, `runId` | First event of every invocation whose ids are valid (see Invocation outcomes) |
| `STATE_SNAPSHOT` | `snapshot`: a RunView | After every transition; at most 32 KB |
| `STEP_STARTED`, `STEP_FINISHED` | `stepName`: the step id, such as `s2` | Around each step; the row comes from the snapshot |
| `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END` | `toolCallId`, `toolCallName` = `save_cohorts`, `delta` | At the confirmation pause (Day 1 PM) |
| `TEXT_MESSAGE_START`, `_CONTENT`, `_END` | `messageId`, `role`, `delta` | After the verifier (Day 2) |
| `CUSTOM` | `name` = `orion.heartbeat`, `value` = `{"ts": "<ISO 8601>"}` | Every 15 s while the invocation streams |
| `RUN_FINISHED` | `threadId`, `runId`, `result` = `{"status": <RunStatus or "Warmed">, "code": <string or null>}` | Waiting or end status, including Interrupted when the invocation reports a lost run |
| `RUN_ERROR` | `message`, `code` | A rejection (codes below), or the run ended Failed. It carries no ids: attribute it to the invocation's run |

## Invocation outcomes

**Rejections.** `RUN_ERROR` with code `INVALID_INPUT`, `RUN_BUSY`, `RUN_NOT_FOUND` or
`NOT_IMPLEMENTED` rejects the invocation. No run is created and no run's status changes.

- With invalid ids, the whole stream is that one frame, without `RUN_STARTED`, because a
  `RUN_STARTED` would have to echo ids that break the id format.
- With valid ids, the rejection follows `RUN_STARTED`.
- The client never shows a rejected invocation as a Failed run. A rejected `ask` removes the
  optimistic run, puts the question back in the composer and shows a notice in the thread,
  such as "A run is still active in this thread. Wait for it to finish, then ask again."
- A rejected `cancel` leaves the target run's status as it was and shows a notice. One
  exception: `RUN_NOT_FOUND` for a run the browser never received a snapshot of means the
  agent never started it, so the client handles it like a rejected `ask`.

**Failures.** Every other `RUN_ERROR` code (`INTERNAL_ERROR`, `PLANNER_DECLINED`,
`WRITE_VERIFY_FAILED`, …) ends the invocation's run Failed.

**Interrupted.** A run whose lease expired is reported by the next invocation that touches it:

- A `STATE_SNAPSHOT` of that run (status `Interrupted`, code `RUN_INTERRUPTED`, running steps
  `Failed`, queued steps `Skipped`) comes right after `RUN_STARTED`.
- If the invocation targeted that run (`cancel`; later `retry_step` and `confirm`), it ends
  `RUN_FINISHED` with `{"status": "Interrupted", "code": "RUN_INTERRUPTED"}`.
- If it was an `ask` on the same thread, the new run continues in the same stream.

**Several runs in one stream.** A stream may carry snapshots of more than one run of the
same thread. Readers apply each snapshot to `snapshot.runId`. Step, tool-call and text
events belong to the invocation's run, the `runId` of `RUN_STARTED`.

## RunView (the snapshot)

```json
{"schema": "orion.run-view/v1", "threadId": "…", "runId": "…", "revision": 3,
 "status": "Executing", "code": null, "question": "…", "updatedAt": "<ISO 8601>",
 "plan": {"version": 1, "signature": "<sha256 hex>",
          "scope": {"source": "active_cohort", "name": "All cases", "caseCount": 1000},
          "rows": [{"row": 1, "label": "…", "tool": "…", "reason": "…", "stepIds": ["s1", "s2"]}],
          "assumptions": [{"code": "DEFAULT_SCOPE_ACTIVE", "text": "…"}],
          "writes": false, "paraphrase": "…"},
 "steps": [{"id": "s1", "row": 1, "tool": "fixture_fingerprint", "kind": "server", "status": "Done",
            "folded": true, "inputSummary": "…", "outputSummary": "…", "durationMs": 412, "code": null}],
 "facts": [{"id": "f.s2.count", "label": "…", "value": 339, "unit": "cases", "stepId": "s2", "row": 1,
            "tool": "cohort_count", "jsonPointer": "/observed/count"}],
 "observed": [{"segments": [{"text": "High risk: "}, {"text": "339", "factId": "f.s2.count"}, {"text": " cases."}]}],
 "limitations": [{"code": "VIEWER_P_ROUNDED", "text": "…", "unblock": null, "nearestApps": []}],
 "interpretation": null, "hypotheses": null, "withheldReason": null,
 "vizSpecs": [{"vizId": "v1", "appId": "Correlation", "launcher": "survival", "citesSteps": ["s4"],
               "pinned": {"kind": "survival", "filter0": null, "term": "overall_survival", "overlay": "cgs_risk_category"},
               "pinnedHash": "<sha256 hex>"}]}
```

- `revision` rises with every snapshot of a run; a reader keeps the highest one.
- `plan` is null while Planning. `steps` lists every step of the current plan version.
- Observed sentences are code-rendered. A segment with `factId` is a number that renders as a
  citation chip ("Row n · sN tool"). `interpretation` and `hypotheses` stay null until the
  verifier passes model text.

### Scope count

`plan.scope.caseCount` is a number or null.

- The compiler copies it from `cohortContext` when the browser supplies a count, and the
  browser sends the active cohort's count whenever the cohort bar has one.
- Null means no count is known yet, and the client renders `Scope: <name> · count pending`.
- Once the step that sizes the scope finishes (the fingerprint's case count for all cases, the
  `cases_query` step for a filtered scope), later snapshots carry the observed count. This
  lands with the Day 1 PM executor.
- An active cohort with no filters is named "All cases", on both the composer's scope chip and
  the plan.

### Limitations and the nearest app

Every limitation carries `nearestApps`: a list of `{"appId": "<catalog appId>", "title":
"<catalog title>"}`, empty when no app applies. The Unsupported card names the first entry
and offers "Open on this cohort" for it. When the list is empty, the card says nothing about
apps. `unblock` stays plain text.

### Pinned viz inputs

`vizSpecs[].pinned` holds the exact inputs the card mounts, and `launcher` equals `pinned.kind`:

| `kind` | Fields |
| --- | --- |
| `survival` | `filter0` (Gen3 filter or null), `term`, `overlay` |
| `oncomatrix` | `filter0` (Gen3 filter or null), `genes` (display order) |
| `deDirect` | `groups` (control first: `[{"name", "sampleIds": [int]}, {…}]`), `method`, optional `volcano` (`foldChangeCutoff`, `pValue`, `pValueType`, `rankBy`) |
| `deInput` | `groups`: `[{"name", "cohortId", "filter0"}, {…}]` |

Rules:

- Inputs are never trimmed to fit the 32 KB snapshot. Sample ids are integers, so the 4,000
  sample cap stays near 20 KB.
- `pinnedHash` is the sha256 (lowercase hex) of the UTF-8 canonical JSON of
  `{"schema": "orion.viz-pinned/v1", ...pinned}`, after normalization. Normalization sorts
  each `deDirect` group's `sampleIds` ascending and keeps every other array's order.
- Canonical JSON sorts object keys at every level, keeps array order, has no whitespace,
  leaves non-ASCII characters unescaped, and keeps `null`. An absent optional field is
  omitted, never written as null.
- Numbers must be integers with |n| ≤ 2^53 − 1, or decimals with 1e-4 ≤ |x| < 1e15. Both
  JavaScript and Python print that range identically. A number with no fractional part prints
  as an integer (`2.0` as `2`, `-0.0` as `0`).
- `deDirect` has exactly two groups.

### RunStatus (15)

`Planning`, `Clarifying`, `AwaitingConsent`, `Executing`, `AwaitingConfirmation`, `Stopping`,
`Answering`, `Complete`, `Partial`, `Limited`, `Unsupported`, `ObservedOnly`, `Cancelled`,
`Failed`, `Interrupted`.

Snapshots carry all fifteen. `RUN_FINISHED` carries the waiting statuses (`Clarifying`,
`AwaitingConsent`, `AwaitingConfirmation`) and every end status except `Failed`, which ends
with `RUN_ERROR`. It carries `Interrupted` only when the invocation targeted a run whose
lease expired (see Invocation outcomes); the view route reports Interrupted the same way.

### StepStatus (9)

`Queued`, `AwaitingUser`, `Running`, `Retrying`, `Done`, `Empty`, `Failed`, `Skipped`, `Reused`.

### Codes

Limitation and error codes: `NAMED_TOOL_MISMATCH`, `OUT_OF_PANEL`, `INELIGIBLE_GROUP`,
`TOOL_FAILED`, `MODEL_DECLINED`, `PLANNER_DECLINED`, `UNSUPPORTED_ANALYSIS`, `CLINICAL_ADVICE`,
`OUT_OF_SCOPE`, `EMPTY_COHORT`, `WRITE_SKIPPED`, `RUN_INTERRUPTED`, `WRITE_VERIFY_FAILED`,
`VIEWER_P_ROUNDED`, `UNSUPPORTED_FIELD`, `UNKNOWN_FIELD`, `UNKNOWN_VALUE`. Transport and
controller codes: `INVALID_INPUT`, `NOT_IMPLEMENTED`, `RUN_BUSY`, `RUN_NOT_FOUND` (the four
rejection codes) and `INTERNAL_ERROR`.

`NOT_IMPLEMENTED` may also appear as a limitation code or a step code while a capability is
stubbed (Day 1). A run that hits a stub ends `Limited` with that limitation.
