# ORION mode A (AgentCore on AWS): status

As of 2026-11-06, 04:50 UTC, with the Runtime version 4 update of 05:36 UTC added. Mode A of RFA
rev 25 is deployed in the sandbox account, in us-east-1, and is **left running on purpose** for the
demo. To remove it, follow `mode-a-teardown.md`. All data is synthetic.

The code is on branch `orion/mode-a` at `4609db6`: stage 2's head `0c3fded` plus 10 stage 3
commits. They are committed locally and not pushed or tagged. Besides these notes and the README
updates that go with them, the only uncommitted change is `terraform/.terraform.lock.hcl`, which
gained one hash. The frontend is the fork at `3fc0e6a`. The agent version is 0.2.0, with prompts
`orion-prompts@5`.

**The live Runtime is now version 4** (2026-11-06 05:36 UTC, user-approved, findings). Its image
`97926b08c368` is built from `orion/mode-a-rebased` at `97926b0`: the same 10 stage 3 commits
rebased onto `orion/bedrock` `bf0a8b7`, which adds the treatment-run guard (a run defined by a
treatment gets no model text and ends ObservedOnly). That branch is local and not pushed. It is
checked out in a scratch worktree, `wt-mode-a-rb` (`git worktree list`), not here.
**Version 4 got a smoke only** (below).
Every E2E and eval result in this note ran on version 3, image `4609db670295`.

**Verdict: mode A works end to end on AWS.** Every user-facing check passes except one: the plan
p95 gate (NFR-01, still a **Proposal**) fails in mode A, and that is for the team to decide.

- **Browser end to end (Runtime version 3, image `4609db670295`):** 15 of 15 scenarios and 118
  checks pass.
  - The full run passed 14 of 15. Its one failure was a connection the test client lost on the
    port-forward, not an ORION fault (see "Known limits", 7).
  - A rerun of that one scenario passed.
- **Evals (5 repeats, through the hosted BFF, Runtime version 3):** 22 pass, 0 fail, 2 not run
  (E11, E20). Both not-run evals are gaps that need a test Runtime in mode A.
- **Runtime version 4:** a smoke through the BFF only, no E2E and no evals (findings). The
  warm-up answered, J1 was Complete with an Interpretation, and a treatment-defined survival
  question ended ObservedOnly with the treatment reason and a `CLINICAL_ADVICE` limitation
  (`$S3/smoke/v4-treat.json`).
- **Gates:** 7 of 9 pass. Honest limits is not run (because E11 is not run). The plan-visible p95
  gate **fails**: 8.08 s over 91 asks, against a 5 s target. The main cause is Bedrock planner
  time. Cold microVMs add to it but are not the root cause.
- **First-deploy risks:** all four that the design named are settled. One of them, unbuffered
  streaming through CloudFront, was the user's own check in their browser.
- **Reviews:** every review fix is deployed. Undoing an eval or infra fix in a scratch copy makes
  its tests fail.
- **No gate, check or assertion was weakened.**

## Where the numbers come from

Every number below names its source. The sources are all local: none is pushed.

| Short name | Artifact |
| --- | --- |
| report `040956Z` | `orion/evals/reports/orion-evals-20261106T040956Z.json` and `.md`: the final evals. 5 repeats, no thread warm-up, 04:09:56–04:47:03 UTC. Gitignored |
| report `020003Z` | `orion/evals/reports/orion-evals-20261106T020003Z.json`: mode L stubs on the same code, agent direct, 5 repeats. Gitignored |
| report `105200Z` | `orion/evals/reports/orion-evals-20261105T105200Z.json`: E20 with its live lease expiry in mode L (`evals-mode-l.sh e20-lease`), 2026-11-05 10:52 UTC, on the code of that day, before stage 3. Gitignored |
| E2E `041700Z` | `orion/notes/screenshots/e2e/summary-mode-a-bedrock-20261106T041700Z.json`: the final browser E2E, full run, 04:09:19–04:17:00 UTC. Gitignored |
| E2E `041958Z` | `orion/notes/screenshots/e2e/summary-mode-a-bedrock-20261106T041958Z.json`: the rerun of `edges` at 04:19 UTC. `summary.json` currently holds this 1-scenario rerun |
| E2E `232119Z` | `summary-mode-a-bedrock-20261105T232119Z.json`: the prep image pass. It lives in the stage 3 worktree's screenshots folder (`$S3/../wt-mode-a/orion/notes/screenshots/e2e/`), not in this checkout |
| findings | `orion/notes/mode-a-findings.md` (committed copy of `$S3/mode-a-findings.md`): the live smoke checks, the IAM smoke, the user's browser check and the version 4 update |
| integrity check | `$S3/integrity-check/summary.txt`: a read-only re-check of these notes on 2026-11-06 about 05:30–05:55 UTC (Lambda, Gateway and Runtime logs, the `edges` record, the Runtime versions, the force and destroy plans) |
| `$S3/...` | The stage 3 scratchpad, `/private/tmp/claude-501/-Users-kaized-Library-Application-Support-Claude-scratch-workspaces-dade3e05-93a8-418c-acf0-02462b3639f6-f71a3e76-88e2-42d3-ad53-23f582f5b75d-scratch-2026-11-04-c981aa/fbd85976-ea01-42e3-8f2d-64198c4fbfda/scratchpad/stage3`. It holds the runbook (`mode-a-runbook.md`), the prep eval reports (`modeA-evals/reports/`), the final eval derivations (`modeA-final-evals/`: `usage-20261106T040956Z.json`, `record-checks.txt`, `plan-breakdown.txt`, `logs/`), smoke streams and records (`smoke/`), the review evidence, plan and apply logs (`*.log`) and the pre-IAM policy documents (`revert/`). **`/private/tmp` is cleared on reboot**: copy what must be kept next to the reports |
| state | `orion/infra/terraform/orion-infra/terraform.tfstate` (serial 66 after the version 4 apply, Terraform 1.17.1). Backup: `generated/aws/orion-infra.tfstate.bak` (identical) |

Numbers marked "E2E log sweep" come from the final E2E's read-only CloudWatch and cluster queries.
They were reported with that run and not saved as a file. The CloudWatch ones were re-read for the
integrity check (`$S3/integrity-check/summary.txt`); the BFF, pod and port-forward ones were not.

## What is deployed

### orion-infra (Terraform, local state; 35 managed resources)

| Kind | Name | Notes |
| --- | --- | --- |
| AgentCore Runtime | `orion_agent` (id `orion_agent-RE0ngY6qRl`) | **Version 4**. PUBLIC network mode, AG-UI protocol, arm64 image `orion/agent:97926b08c368` (built from `orion/mode-a-rebased` `97926b0`, last updated 2026-11-06 05:36:02 UTC). Idle session timeout 900 s; maximum session lifetime 8 h |
| AgentCore Runtime endpoint | `orion_live` | Live version 4. The BFF calls this qualifier |
| AgentCore Gateway | `orion-vlab` | MCP, `AWS_IAM` inbound. Target `vlab` (the Lambda), with its tool schema in S3 |
| Lambda | `orion-vlab-tools` | python3.12, arm64, 58 s, 512 MB, X-Ray active. Package `orion/agent/lambda/dist/orion-vlab-tools-arm64.zip` (its hash equals the state's `source_code_hash`) |
| S3 | bucket `orion-runs-<account>-us-east-1` | SSE-S3, versioning, public access blocked, bucket owner enforced, TLS only. Records expire after 30 days under `orion/` (non-current versions after 7). Holds `orion/runs/`, `orion/threads/` and the object `gateway/vlab-tools.json` |
| Secrets Manager | `orion/vlab-basic` | **Created and filled by the user.** Terraform imported it before stage 1 (the base plan, `terraform show base.tfplan` in the module: 15 to add, and 1 in-place tag update of the imported secret). Recovery window 0 in the state |
| ECR | `orion/agent` | Immutable tags, scan on push. The lifecycle policy expires only untagged images, after 7 days. Tags: `pre-bb7c6e0bde78` (Runtime v1), `prep-66ee4ff8529c` (v2), `4609db670295` (v3, the rollback target), `97926b08c368` (v4, live) |
| IAM roles, each with one inline policy | `orion-runtime`, `orion-bff`, `orion-gateway`, `orion-vlab-tools` | Grants and trusts as in `../infra/README.md` ("Principals", "Trust policies"), after the least-privilege apply (below) |
| EKS add-on | `eks-pod-identity-agent` v1.4.1-eksbuild.3 | On the sandbox cluster `mmrf-synthetic` |
| EKS Pod Identity association | `mmrf-synthetic/orion-frontend` → `orion-bff` | Session tags on (`disable_session_tags = false`) |
| CloudWatch log groups, 30 days | `/aws/lambda/orion-vlab-tools`, `/aws/vendedlogs/bedrock-agentcore/runtime/orion_agent`, `/aws/vendedlogs/bedrock-agentcore/gateway/orion-vlab` | Plus delivery sources and destinations `orion_agent-app-logs` and `orion-vlab-app-logs`, and their 2 deliveries |
| Not in the state | `/aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-orion_live` and `-DEFAULT` | Created by AgentCore. **No retention yet**: README deploy step 6 has not been applied |

Runtime versions (from the plan and image logs, `$S3/full-plan.log`, `prep-plan.log`,
`final-plan.log`, `rb-plan.log`, `image-*.log`, and `list-agent-runtime-versions`):

| Version | Image tag | Deployed (UTC) | What ran on it |
| --- | --- | --- | --- |
| 1 | `pre-bb7c6e0bde78` | 2026-11-05, 20:57:43 | First smoke. J1 ended Partial on the Gateway null error, and the J3 `de_run` went through the Gateway |
| 2 | `prep-66ee4ff8529c` | 2026-11-05, 21:13:41 | The null fix. The user's browser check through CloudFront, the prep test pass, the IAM smoke |
| 3 | `4609db670295` | 2026-11-06, 04:06:27 | HEAD `4609db6` of this checkout. The final smoke, E2E and evals |
| 4 | `97926b08c368` | 2026-11-06, 05:36:02 | `orion/mode-a-rebased` `97926b0` (adds `bf0a8b7`'s treatment-run guard). A BFF smoke only (findings) |

The final apply (v3) was 0 to add, 3 to change and 0 to destroy (`$S3/final-plan.log`). It changed
the Runtime, the endpoint and the `orion-bff` role policy (re-rendered, because its document reads
the Runtime). The version 4 apply had the same shape (`$S3/rb-plan.log`, `rb.tfplan`).

### The sandbox side

- **Sandbox Terraform:** only `aws_eks_cluster.sandbox` `public_access_cidrs` changed. That was
  four guarded applies, each exactly 1 in-place update, 0 add and 0 destroy, and each a /32 for
  the user's current address (`$S3/deploy.log`, `deploy2.log`, `allowlist.log`,
  `allowlist2.log`).
- **Sandbox images:** all six `mmrf-synthetic/*` repos got the additive tag
  `build-367ebcf26bc746f0`. Its frontend is the fork at `3fc0e6a`, digest `sha256:d78cb60b…`.
  The upstream frontend `build-188f27662ebc4be2` (`sha256:4f1d1da8…`) was not deleted
  (`$S3/deploy.log`). `generated/aws/values.pre-orion.json` keeps the pre-ORION digests.
- **The sandbox patch** (`orion/infra/patches/sandbox.patch`, re-pinned to the fork's `3fc0e6a`)
  is applied and committed in `66fa5c8`. It changes `sources.json`, adds the chart's `orion:`
  block, and adds the template with the ServiceAccount `orion-frontend` and the NetworkPolicy
  `orion-frontend-egress`.
- **Helm release `synthetic`, namespace `mmrf-synthetic`:**

  | Revision | What it is |
  | --- | --- |
  | 1 | The original sandbox, before ORION: the unpatched chart and the upstream frontend |
  | 2 | `aws_sandbox.py deploy --skip-build`, 2026-11-05 21:02 UTC (`$S3/deploy2.log`): the patched chart with `orion.enabled` false and the fork's frontend |
  | 3 | `helm upgrade` with `values.json` and `orion-values.json` (`generated/aws/orion-values.json`): **ORION on**, the current revision |

  The frontend pod was restarted after the IAM apply (a rollout restart, findings). That made no
  new revision.

## The request path

The browser reaches ORION in two ways. Viewers use CloudFront with the team's Basic login.
Automation uses a loopback port-forward of the `identity` service, which injects the synthetic
session and skips CloudFront for the browser hop only. From there:

1. The browser goes to CloudFront, then the internal ALB, the sandbox's `access` gate (Basic) and
   the `identity` gateway.
2. From there to the frontend pod (the fork's Next.js app, ServiceAccount `orion-frontend`). Its
   BFF route is `POST /api/orion/agui`.
3. The BFF signs `InvokeAgentRuntime` with SigV4, using the `orion-bff` credentials that Pod
   Identity supplies (the injected `eks-pod-identity-token`). It calls endpoint `orion_live`,
   session `orion-<threadId>`, so each thread is its own Runtime session and microVM.
4. The AgentCore Runtime `orion_agent` runs the arm64 container as `orion-runtime`:
   - The planner and the composer call Bedrock (InvokeModel on `us.anthropic.claude-opus-5-5`,
     with fallback `us.anthropic.claude-opus-5`).
   - The four pure functions (`set_operation`, `check_subset`, `rank_top_n`, `derive_km_median`)
     run in process.
5. The 9 server tools (`fixture_fingerprint`, `cases_query`, `cohort_count`, `gene_cases`,
   `sample_lists`, `de_preanalysis`, `de_run`, `survival_compare`, `top_mutated_genes`) go as MCP
   `tools/call`, signed with SigV4, to the AgentCore Gateway `orion-vlab`. Its target `vlab`
   invokes the Lambda `orion-vlab-tools` as `orion-gateway`.
6. The Lambda reads `orion/vlab-basic` and calls the sandbox's APIs (Gen3 Analysis, Guppy,
   ProteinPaint) **through CloudFront** with the Basic login.
7. `save_cohorts` is not a server call. It runs in the browser (transport `client`) and only
   inside Create.
8. Records: the Runtime writes `orion/runs/<runId>.json` and `orion/threads/<threadId>.json` to S3.
   The BFF's view and record routes read them with `s3:GetObject`.

## What was verified live

### Browser end to end (final, Runtime version 3)

Command: `SANDBOX_ORIGIN=http://127.0.0.1:3081 ORION_E2E_MODEL=bedrock ORION_E2E_MODE=A node
orion/scripts/e2e-mode-l.mjs`, run in headless Chromium through the port-forward.

Before the run, read-only describes showed the Runtime at version 3 and READY, image
`orion/agent:4609db670295`, and endpoint `orion_live` READY at live version 3. Every scenario but
`e21` (which checks only that nothing is saved) reads its run records through the record route
and checks environment mode A, transport `gateway` and the bedrock planner and composer. Where a
record has calls, every server tool must be on the Gateway; where it has model calls, every one
must be on `us.anthropic.claude-opus-5-5`.

| Scenario | What it covers | Checks | Result (source) |
| --- | --- | --- | --- |
| `j1-open` | J1 open-ended: plan, live steps, survival card, answer, record, Evidence Inspector, a follow-up chip | 15 | Pass (E2E `041700Z`) |
| `j1-named` | J1 tool-named: the same plan, live steps, card and answer | 10 | Pass (`041700Z`) |
| `j2` | J2: plan, confirmation card, Create, cohort list, Complete, OncoMatrix card | 19 | Pass (`041700Z`) |
| `j3` | J3: pause with eligible counts, Create (two cohorts), DE card with the sort caption, Complete | 12 | Pass (`041700Z`) |
| `edit-stop` | J3: an edit at the save pause (plan version 2), then Stop during `de_run` | 4 | Pass (`041700Z`) |
| `dont-save` | J3: Don't save, DE on unsaved groups, hand-off disabled with the reason | 3 | Pass (`041700Z`) |
| `e21` | Enter in the composer as the confirmation card appears saves nothing | 1 | Pass (`041700Z`) |
| `restore` | Reload at the save pause and during `de_run`: the thread restores and the run finishes | 7 | Pass (`041700Z`) |
| `follow` | Plan-card edit (Top N 3 → 5), E14 in the browser, a follow-up chip, switching threads | 12 | Pass (`041700Z`) |
| `edges` | E7 declined substitution, E10 one clarifying question, E8 and E17 limits | 9 | **Pass on rerun** (E2E `041958Z`). The full run failed it after 5 passing checks on a dropped connection ("socket hang up"). See Known limits, 7 |
| `e18` | Unsupported, names Set Operations, Open on this cohort | 3 | Pass (`041700Z`) |
| `writing` | "Writing interpretation": plan, then Observed and Limitations, then the Interpretation | 7 | Pass (`041700Z`) |
| `stop-writing` | Stop during "Writing interpretation": Observed kept, Interpretation withheld | 5 | Pass (`041700Z`) |
| `stop-planning` | Stop during "Planning": no plan and no tool call | 2 | Pass (`041700Z`) |
| `recompose` | A plan-card edit after Complete re-composes (J2 Top N 3 → 5) | 9 | Pass (`041700Z`) |
| **Total** | | **118** | **15 of 15** (109 checks of the 14 passing scenarios in `041700Z`, plus `edges`' 9 in `041958Z`; `041700Z` lists 114 because it also counts the failed attempt's 5); nothing skipped. This equals the prep image's 15 of 15 and 118 checks (E2E `232119Z`) |

Diagnosis of the `edges` failure. Its record-route GET for run `555dfd4a3dbb01437a167ec6fba5e06f`
got no HTTP response at all. The run itself was correct:

- Three later GETs of the same route returned 200 (E2E log sweep).
- The record is Limited, `OUT_OF_PANEL`, mode A, transport gateway, with no tool call and one
  planner call, no fallback (re-read from S3 for the integrity check).
- The BFF log shows the turn ended normally (E2E log sweep).
- The frontend and identity pods had 0 restarts, and the port-forward loop never exited (E2E log
  sweep).

Logs over the E2E window, about 04:09–04:21 UTC (E2E log sweep). That window also holds the
evals' first 11 minutes (they started at 04:09:56), so the Lambda and Gateway counts include eval
calls:

- **Lambda:** 225 invocations, all successful, with every tool status ok. Maximum 2,963 ms, p95
  1,042 ms, 1 cold init. No ERROR or WARNING. Re-read for 04:00–04:50 UTC, the whole E2E and eval
  time: 492 invocations, all successful, every tool status ok, maximum 2,963 ms, 2 cold inits,
  only INFO lines (`$S3/integrity-check/summary.txt`).
- **Gateway vended logs:** about 1,145 events, 0 with `isError` true. For 04:07–04:46 UTC: 2,503
  events, 0 with `isError` true (integrity check).
- **Runtime:** no ERROR. It logged 52 uvicorn "Invalid HTTP request received" warnings (51 on the
  re-read to 04:21:30; Known limits, 9).
- **BFF:** the 3 `upstream_aborted` by cancel are exactly the three Stop scenarios. The 1
  `clientGone` is the restore reload during `de_run`.

### Evals (final, 5 repeats, report `040956Z`)

Command: `python3 -m orion_evals run --target bff --bff-origin http://127.0.0.1:3082
--record-source bff --oracle-origin http://127.0.0.1:3082 --repeats 5 …` with no
`--warmup-threads` ("How to rerun everything").

Setup checks:

- The environment was mode A, bedrock planner and composer, executor engine, agent 0.2.0,
  transport gateway, `threadWarmup` false.
- The agent and oracle fingerprints match: both are `80ac2c93…39257`.
- The oracle reached the stack through the port-forward with no credential.

| Gate | Target | Status | Measured (report `040956Z`) |
| --- | --- | --- | --- |
| Reference prompts completed | 6 of 6 | Pass | 6 of 6 prompts Complete in every run |
| Phrasing parity | E1–E6, E22 | Pass | E1–E6 signature checks and E22 |
| Interpretation present | E16 | Pass | E16 |
| Honest limits | 9 of 9 | **Not run** | 6 of 9: E7, E8, E9, E10, E17, E18 pass. E11's timeout, refusal and empty count are not run |
| Visible tool calls | 100% | Pass | 30 of 30 runs |
| Cited numbers | 100% | Pass | 90 of 90 runs |
| Zero writes without confirmation | 0 | Pass | 100 of 100 runs. Cohort writes were made by the simulated browser only inside Create |
| Plan visible p95 (**Proposal**) | ≤ 5 s over ≥ 20 asks | **Fail** | p95 8.08 s over 91 asks, no thread warm-up |
| Count accuracy | E5, E6 | Pass | E5, E6 group-size checks |

- **Pass:** E1–E10, E12–E19 and E21–E24.
- **Not run: E11.** Every fault case reports "needs an agent started with ORION_FAULTS=…": the
  deployed Runtime has no fault agents.
- **Not run: E20.** Its closed-tab and 300 s save-pause cases pass, but the gating case "lease
  expiry ends Interrupted" is not run because there is no `--restart-cmd`. The agent's unit test
  is listed beside it as a non-gating check.
- On the same code in mode L (stubs, agent direct), E11 passes with its four fault agents and
  Honest limits is 9 of 9. Phrasing parity and E22 are not run there, because they need the
  Bedrock planner, and E20 is not run either, for want of `--restart-cmd` (report `020003Z`).

What the 101 run records of the report say (`$S3/modeA-final-evals/record-checks.txt`):

- **Environment:** all 101 are mode A, bedrock planner and composer, engine, agent 0.2.0,
  transport gateway.
- **Fingerprint:** 92 carry `80ac2c93…`. The other 9 made no tool call (5 Unsupported, 2 Limited
  `OUT_OF_PANEL`, 1 AwaitingConsent, 1 Cancelled before any call).
- **Calls:** 626 tool calls.
  - 410 server calls went through the Gateway (every one of the 9 server tools). 408 have HTTP
    200; the other 2 are the cancelled calls below, which have no HTTP status.
  - 159 pure-function calls ran in process; 57 `save_cohorts` calls were client.
  - The only 2 non-ok calls are intended "CANCELLED: stopped" calls of E24: the `de_run` of Stop
    during `de_run`, and a `cohort_count` of the scope switch while J1's steps run (that run then
    ended Complete).
- **Models:** 195 model calls, all on `us.anthropic.claude-opus-5-5`, all ok, 0 fallback.

### The four first-deploy risks, and how each was settled

1. **Pod Identity with service-account tokens off (Open question 19).**
   - The webhook injected its own `eks-pod-identity-token` volume and the container-credential
     variables. The frontend got `orion-bff` credentials with `automountServiceAccountToken`
     false (findings).
   - After the session-tag trust narrowing (INFRA-3), a rollout restart still got credentials
     with HTTP 200 (findings).
   - The value stays false.
2. **The Gateway timeout for `de_run` (Open questions 15 and 19).**
   - J3's `de_run` through the Gateway took 2,998 ms on Runtime v1 (Lambda 2,873 ms, findings;
     record in `$S3/smoke/j3.record.json`, run created 20:58:50 UTC). The Lambda limit is 58 s.
   - Over the review window, 28 `de_run` calls through the Gateway had p50 1,210 ms and max
     3,023 ms (`$S3/modeA-review-evidence/record-audit.txt`). In the final evals, the 32 ok
     `de_run` calls had p50 1,186 ms and max 1,396 ms (the report's 101 records, copied to
     `$S3/modeA-final-evals/records/runs/`).
   - Cut 1 (`tool_transport = in_process`) was not needed.
   - Not run: Open question 15's own series (`../infra/README.md`): five cold and five warm
     Wilcoxon requests through CloudFront with renamed groups, and the test Runtime's `de_run`.
     The live calls above cover only the tool path, with no control over ProteinPaint's cache.
3. **Gateway null arguments.**
   - The first J1 on Runtime v1 ended Partial `TOOL_FAILED`. The Gateway refused
     `survival_compare` with "Field '/scope' has invalid type: null found, object expected"
     (`$S3/smoke/j1.record.json`).
   - Fixed in the agent (`2d22819`): the Gateway client sends arguments without null-valued keys,
     and the recorded request keeps its nulls. Every nullable Input field defaults to None, so the
     Lambda reads an omitted field as null.
   - J1 was Complete on v2 (findings), and in every E2E and eval run since.
4. **Unbuffered SSE through CloudFront.**
   - The user ran J1, J2 and J3 in their own browser through CloudFront, with the Basic login, at
     about 22:00 UTC on 2026-11-05 (Runtime v2). The answer streamed live: the plan card, the
     steps ticking one by one, and "Writing interpretation" before the Interpretation (findings).
   - This is a human check. The automated runs use the port-forward and do not cover the
     CloudFront hop to the browser. They do cover CloudFront on the tool path (the Lambda calls
     the stack through it).

The runbook's pre-apply review found one more likely failure: S3 403 instead of 404 for a missing
ledger. It was fixed before the first apply (see "Review rounds").

### The IAM tightening the user approved

The user approved it, and it was applied at 2026-11-06 01:42:33 UTC (`$S3/iam-apply-time.txt`).

- **The plan:** 1 to add, 4 to change, 1 to destroy (`$S3/iam-plan.log`).
  - Trust policies of `orion-bff`, `orion-gateway` and `orion-runtime`, updated in place.
  - The `orion-runtime` role policy, updated in place.
  - The ECR lifecycle policy, replaced (the add and the destroy).
  - The repository and both tags of the time were kept.
- **What changed:**
  - **INFRA-1:** `orion-runtime` may invoke the Opus foundation models only through the `us.`
    inference profiles (condition `bedrock:InferenceProfileArn`).
  - **INFRA-2:** the Runtime role trusts only `runtime/orion_agent-*`, and the Gateway role only
    `gateway/orion-vlab-*`.
  - **INFRA-3:** `orion-bff` trusts only the Pod Identity session tags of
    `mmrf-synthetic/orion-frontend`, with session tags explicitly on.
  - **The ECR lifecycle** expires only untagged images.
- **Smoke tests after the apply** (findings; streams in `$S3/smoke/iam-*.sse`):
  - A fresh Runtime session (`orion-iam-*`): the warm-up took 5 s, then J1 was Complete. Every
    server tool went through the Gateway, and the planner and composer were ok on
    `us.anthropic.claude-opus-5-5` (INFRA-1, INFRA-2).
  - A frontend rollout restart: Pod Identity credentials 200 under the session-tag trust
    (INFRA-3).
  - A BFF warm-up through the port-forward: 200, first byte 1.0 s.
- **Rollback:** the policy and trust documents from before the apply are in `$S3/revert/`.
- `terraform test` has 11 of 11 passing, the least-privilege checks included
  (`../infra/README.md`, "Checks run here").

### The final deploy's smoke

On Runtime v3 (2026-11-06 about 04:07 UTC, through a port-forward on 3084), a BFF warm-up and a
J1 ask were made. The warm-up streamed `RUN_STARTED` then `RUN_FINISHED`. J1 ended Complete,
after 5 steps and 9 snapshots (`$S3/smoke/final-warmup.sse`, `final-j1.sse`).

### The version 4 smoke

On Runtime v4 (2026-11-06 about 05:37 UTC, through a port-forward on 3085; findings):

- The warm-up answered 200, and J1 ended Complete with an Interpretation, the planner and the
  composer ok. No stream or record of this J1 was saved.
- "In patients treated with bortezomib, lenalidomide, dexamethasone, compare survival of CGS
  high-risk vs. CGS standard-risk patients" ended ObservedOnly with only a planner call, the
  withheld reason "this run compares groups defined by a treatment, so ORION writes no
  interpretation of it" and a `CLINICAL_ADVICE` limitation (`$S3/smoke/v4-treat.json`, mode A,
  transport gateway).

That is all that ran on version 4. The browser E2E and the evals were not rerun on it.
`bf0a8b7`'s commit message says no eval or E2E scenario uses a treatment-defined run and that every
other run is unchanged.

## Timings

All of these are mode A through the port-forward, on Runtime version 3. A browser through
CloudFront adds its own hop, which was not timed.

| What | Measured | Source |
| --- | --- | --- |
| Warm-up turn the Assistant sends on opening a thread | 5.1 s | E2E `041700Z` (`writing`) |
| **First ask in a fresh tab or thread, warm-up in flight** | 8.1–11.5 s, 6 asks (the J1, J1 tool-named, J2, J2 chip, J3 and race-cohort asks) | E2E `041700Z` |
| The same on the prep image | 8.0–19.0 s, 6 asks | E2E `232119Z` |
| Ask after the warm-up ended (a typed question, two chips) | 4.0, 4.2 and 4.3 s | E2E `041700Z` |
| A plan-card edit to version 2 (compiled, no model call) | 0.3 s | E2E `041700Z` |
| "Writing interpretation" to the Interpretation: J1 | 8.0–10.7 s (4 answers) | E2E `041700Z` |
| The same: follow-up chips / J2 / the edit's version 2 | 7.8–8.5 s (3) / 7.3 s / 7.7 s | E2E `041700Z` |
| The same: J3 | 22.4 s | E2E `041700Z` |
| Plan visible, eval harness, no warm-up | p50 4.53 s, **p95 8.08 s**, max 12.70 s (91 asks) | report `040956Z`, `$S3/modeA-final-evals/usage-20261106T040956Z.json` |
| Planner call (Bedrock, effort low) | p50 3.53 s, p95 5.79 s, max 7.38 s (103 calls). Output 47–320 tokens (p50 132); 12,106 cached prefix tokens | usage `040956Z`, `$S3/modeA-final-evals/records/runs/` |
| Composer call (effort medium) | p50 10.38 s, p95 23.48 s, max 29.77 s (92 calls) | usage `040956Z` |
| Tokens, all 195 calls | Input 259,795; output 106,119; cache read 1,761,247; cache write 17,891 (one cache-write call per role) | usage `040956Z` |
| microVM acquisition (AgentCore receipt to run start) | Median 0.48 s, p95 0.97 s over all 91 asks; 0.38–0.96 s for the 20 slow asks that got a pre-started microVM | `$S3/modeA-final-evals/plan-breakdown.json` and `.txt` |
| **Cold microVM**, none pre-started | 7.42, 7.44 and 9.43 s before the run started (3 of 91 asks, all while the browser E2E shared the Runtime). The first app log came 4.9–6.2 s after receipt, the server was ready 1.2–1.6 s later, then 0.8–1.5 s to the first request | `plan-breakdown.txt` |
| First event after a Runtime version update | 9.5 s through the BFF (BFF first-byte timer 30 s) | findings |
| Fixed overhead per ask, beside the planner | About 0.9 s (medians): acquisition 0.48 s, agent before the planner 0.15 s, plan compile 0.06 s, transport 0.16 s (harness, port-forward, BFF, AgentCore and the event back; max 0.33 s) | `plan-breakdown.json` |
| `de_run` through the Gateway | 2,998 ms (J3 smoke); p50 1,210 ms, max 3,023 ms over 28 calls (review window); p50 1,186 ms, max 1,396 ms over the final evals' 32 | findings, `record-audit.txt`, `$S3/modeA-final-evals/records/runs/` |
| Prep evals, 1 repeat | Plan p95 11.55 s over 31 asks with no warm-up (cold microVMs about 7.6 s); 5.61 s over 31 asks with each thread's warm-up awaited | `$S3/modeA-evals/reports/orion-evals-20261105T231125Z`, `…233911Z`, findings |

## Known limits

1. **NFR-01's plan p95 fails in mode A** (8.08 s over 91 asks, report `040956Z`). Why:
   - **Every eval ask lands on a fresh microVM.** Each thread is its own Runtime session, and
     each eval opens new threads, so 90 of 91 asks were their session's first call. Even with a
     pre-started microVM free, that adds about 0.9 s of fixed overhead to the planner time.
   - **Cold microVMs:** 3 asks waited 7.4–9.4 s for one, all while the browser E2E shared the
     Runtime (04:06:49–04:19:55 UTC: 71 invocations, 19 sessions). Asks during that overlap had
     p95 12.04 s; the 62 asks after it had p95 6.99 s, with 0 cold starts
     (`plan-breakdown.txt`).
   - **The root cause is Bedrock planner time.** Without the cold starts, p95 is still 6.99 s
     (88 asks), because the planner alone has p95 5.83 s. Its time tracks output length only
     weakly (r = 0.38): the J1 prompt took 2.6–7.3 s at the same 115 tokens. That looks like
     Bedrock latency variance.
   - **Stage 2 for comparison:** in mode L the same gate passed at 4.55 s but failed in two of
     five 5-repeat runs (`bedrock-status.md`).
   - **In the browser:** a question asked after the warm-up shows its plan in 4.0–4.3 s. The
     first question in a fresh tab or thread waits for the warm-up and takes 8.1–11.5 s (E2E
     `041700Z`).
   - **Not changed:** the gate's target and measurement are unchanged. The opt-in
     `--warmup-threads` measures a best case and is labelled as one.
   - **Options for the team:** `../infra/README.md`, "Open questions 10 and 12".
2. **E11 and E20's live lease expiry are not run live.** Both need a test Runtime, and neither
   is built.
   - **E11** needs one agent per fault (`de_run:timeout`, `planner:refuse`, `composer:refuse`,
     `cohort_count:empty`), each started with `ORION_FAULTS`. In mode A that means a test
     Runtime per fault: `-var test_runtime=true -var runtime_faults=<fault>`, in a second
     workspace with its own `runtime_name`, `gateway_name`, roles, bucket, secret and Lambda
     names (`../infra/README.md`, "Checks that need a test Runtime").
   - The harness reaches a fault agent at a plain-HTTP invocations URL, with records from a run
     directory (`--fault-agent <fault>=<url>@<run dir>`). So each test Runtime would also need
     two things:
     - a BFF in front of it, for example the fork's frontend run locally in mode H with that
       Runtime's ARN;
     - its records copied from S3 into a run directory.
   - **E20's lease expiry** needs `--restart-cmd` to kill the controller mid-run. The harness
     runs a fixed command and passes it no thread or session id. In mode A that means a test
     Runtime that can be restarted as a whole, or a wrapper that finds the run's session and
     calls `StopRuntimeSession`.
   - Both have passed in mode L: E11 in report `020003Z`, on this code; E20's lease expiry
     through `orion/scripts/evals-mode-l.sh e20-lease` in report `105200Z`, on the code of
     2026-11-05 10:52 UTC, and not rerun since. The test-Runtime open questions 7, 8, 10 and 12,
     and Open question 15's cold series, are not run either.
3. **The frontend pod has open HTTPS egress (accepted residual risk).** `orion-frontend-egress`
   allows TCP 443 to every public IPv4 address. That is more than the AgentCore and S3 endpoints
   the BFF needs, which have no fixed addresses a NetworkPolicy can name.
   - The pod holds `orion-bff` credentials, but those can only invoke and stop the one Runtime
     and read run records.
   - Private and link-local ranges stay closed.
   - Closing it is a sandbox Terraform change (VPC endpoints), out of scope for ORION
     (`../infra/README.md`, "Residual risks").
4. **Next.js cache log noise in the frontend pod.** The pod logs `EACCES: permission denied, mkdir
   '/app/.next/cache'`: the image optimizer's unhandledRejection and "Failed to write image to
   cache".
   - Each error prints a stack of about 12 lines. The final E2E's sweep counted 180 in its 20
     minutes (E2E log sweep, not saved). The prep pass's saved capture
     (`$S3/modeA-evals/frontend.log.raw`) holds 114 of these errors, 57 of each kind, in 1,348
     lines.
   - Tests are unaffected and the pod has 0 restarts.
   - It is a sandbox frontend image or filesystem concern, not ORION's.
5. **Short-term credentials.** The deployed stack runs on its own roles (the Runtime and Lambda
   execution roles, Pod Identity), so it keeps serving when the user's credentials expire.
   - The user's Workshop Studio credentials (profile `sandbox`, role `WSParticipantRole`) expire
     at an unrecorded time. They expired once already, at 18:21 UTC on 2026-11-05 (runbook).
   - Every operator step stops on `ExpiredToken` until the user refreshes them. That includes
     Terraform, the AWS CLI, record and log reads, and `kubectl`, whose kubeconfig gets its token
     from the AWS CLI.
   - The Workshop Studio account may be reclaimed when the event ends (runbook).
6. **The user's changing public IP, and the EKS API allowlist.** The EKS API admits a single /32
   (the sandbox's `operator_cidr` rule).
   - The allowlist was re-pointed four times in stage 3 ("The sandbox side"). The findings
     record two egress changes, once across networks and once within the same /16; a third, to a
     different /16, came before the version 3 deploy (`$S3/allowlist2.log`, about 04:05 UTC).
   - When it changes, `kubectl`, `helm` and the port-forward time out until the allowlist is
     re-pointed. The re-point is a guarded sandbox plan of exactly one in-place update of
     `public_access_cidrs`, with no helm and no image publish (`$S3/update_allowlist.py` with
     `guarded_deploy.py`; the manual steps are in `mode-a-teardown.md`, step 1).
   - Never use `aws_sandbox.py deploy` for this while ORION is on: it re-runs Helm with
     `values.json` alone, which turns ORION off.
   - The CloudFront site is not IP-restricted, so viewers in a browser are unaffected.
7. **The identity gateway's keep-alive (sandbox, not ORION; unverified).** The first `edges`
   attempt lost its connection on the record route. The likely cause: `identity/server.mjs`
   keeps Node's default 5 s `keepAliveTimeout`, and Playwright reused an idle socket about 6.5 s
   after the previous GET.
   - The same 5 s behind the ALB (idle timeout 180 s) is the classic setup for occasional 502s
     through CloudFront. None was observed.
   - Fixing it is a sandbox decision. The rerun passed, and the gate is unchanged.
8. **The service-created Runtime log groups never expire.**
   `/aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-orion_live` and `-DEFAULT` have no
   retention: README deploy step 6 (`put-retention-policy`, 30 days) is an AWS write that has not
   been run. They hold run ids, outcomes, error codes and short answer fragments.
9. **Runtime "Invalid HTTP request received" warnings.** Most new microVMs log one uvicorn warning
   at AgentCore's first contact, before the first POST. Of the 136 microVM log streams of
   04:06–04:47 UTC, 114 have exactly one and 22 have none, among them the three cold-started ones
   (`$S3/modeA-final-evals/logs/`, integrity check).
   - The 114 fall in 04:09–04:50 UTC, while the evals and, until about 04:21, the E2E ran; 52 of
     them in the E2E's window (E2E log sweep). There were 2 in 01:00–04:09 UTC.
   - There were no errors, and every run behaved correctly. It is probably platform probe noise
     for pre-started microVMs; unverified.
10. **Usage needs a local copy.** `python -m orion_evals usage` reads records only from
    `--run-dir`, not from the BFF. In mode A, copy the report's records from S3 first ("How to
    rerun everything").
11. **A later `aws_sandbox.py deploy` turns ORION off.** It runs `helm upgrade` with
    `values.json` alone. Repeat the two-values-file upgrade after it (`../infra/README.md`, deploy
    step 7). Never use `--reuse-values`.

## Review rounds: what each found and fixed

1. **Pre-apply review** (the runbook, read-only, before any ORION resource existed). It found three
   defects likely to bite on first use:
   - (a) The Runtime's `s3:ListBucket` had an `s3:prefix` condition. A GetObject has no
     `s3:prefix`, so a new thread's missing ledger would read as 403 and fail the first ask.
   - (b) The README's `helm upgrade --reuse-values` would drop the patched chart's defaults.
   - (c) The teardown passed `force_delete` and `force_destroy` only on the destroy plan, where
     they have no effect.

   All three were fixed in `66fa5c8`, and the contract text in `8cce3af`:
   - the prefix condition was removed;
   - the README now passes both values files, applies the force flags before the destroy plan,
     and documents importing an operator-created secret.

   The first asks of new threads then read their ledgers without error.
2. **The first live deploy (Runtime v1, about 20:58 UTC on 2026-11-05).**
   - The Gateway refused explicit nulls. That is the first-deploy risk 3 above, fixed in
     `2d22819`.
   - The browser E2E gained its mode switch (`160dba4`, `ORION_E2E_MODE=A` with Gateway transport
     checks).
   - The first `aws_sandbox.py deploy` stopped at `kubectl` with an i/o timeout to the EKS API.
     With the allowlist re-pointed to the then-current address, the second deploy made Helm
     revision 2 (`$S3/deploy.log`, `deploy2.log`).
3. **The first mode A test pass (prep image, Runtime v2, 23:09–00:00 UTC).**
   - The E2E's first attempt failed on its own transport check. It expected `gateway` for
     `save_cohorts`, but the record says `client`, as `TOOLS.md` section 4 specifies: the save
     runs in the browser (`$S3/modeA-e2e/run1.log`).
   - The check now expects `client` for `save_cohorts` and still requires `gateway` for every
     server tool (`5d2b7ac`). The rerun passed 15 of 15 (E2E `232119Z`).
   - The evals' plan p95 of 11.55 s without warm-up led to the opt-in `--warmup-threads`
     (`ff76bde`). It gave 5.61 s, still over the target, and changes no threshold.
4. **The mode A review round (after the prep pass).** The eval and infra fixes were checked by
   undoing each one in a scratch copy and watching its tests fail:
   - eval fixes: 12 undo variants, each failing 1 to 4 of the 132 tests, while the fixed code
     runs all 132 OK, 8 of them skipped (`$S3/verify-evidence/undo-*.log`, `base-unittest.log`,
     `$S3/evals-revert-checks/`);
   - infra: each revert fails `terraform test` (`$S3/infra-lp/test-*.log`, `$S3/verify-infra/`);
   - signer: six mutants were built (`$S3/verify-signer/`), and the old behaviour has a
     reproduction (`$S3/modeA-gw-signer-verify/repro.py`). Their results were not saved as files.

   The fixes:
   - **Evals harness, "reports only what ran" (`538b1df`):**
     - **F1:** E20's lease-expiry case counted as covered when a unit-test name matched a regex.
       It is now not run without `--restart-cmd`, and the unit test is listed as non-gating.
     - **F2:** gates and evals scored "in 5 of 5 runs" passed at 1 repeat. They are now not run
       below 5 repeats, and E13 is not run below 2.
     - **F3:** the warm-up gate label claimed "as the browser does". It now says "each thread's
       warm-up completed before its first ask": a best case.
     - **F4:** a transient failed record read was not retried, and not always logged. A
       transient failure (no answer, timeout, 5xx, 429, cut body) is now tried 3 times in all, a
       4xx is not retried, every failure is logged, and a 200 body must be valid UTF-8.
     - So the prep reports `231125Z` and `233911Z` (1 repeat, old harness) pass Phrasing parity,
       Interpretation present and E20, which today's harness reports not run. Those passes are
       not results.
   - **Gateway signer (`179ed10`):**
     - **Found:** `BotocoreSigner` built a botocore Session and walked the credential chain on
       every MCP POST. A credential or signing failure surfaced as `INVALID_INPUT`, which is not
       retried.
     - **Fix:** it resolves once and signs with the same refreshable credentials, refreshing off
       the event loop when a refresh is due. A signing failure is a retryable `HTTP_ERROR`, with
       one retry.
   - **Infra least privilege (`b49363e`):** INFRA-1, INFRA-2, INFRA-3 and the ECR lifecycle, as
     above.
     - The README records the open egress as a residual risk and gives retention steps for the
       service-created log groups.
     - **Found:** the old lifecycle expired any image beyond the 10 most recent, tagged or not
       (`$S3/revert/ecr-lifecycle.json`). It could therefore expire the live or the previous
       Runtime tag, and a Runtime version names its image by tag.
   - `4609db6`: the E2E summary-shape test expects the new `mode` field.
   - Then, in this order (UTC, 2026-11-06):
     - the IAM change was applied with the user's approval (01:42);
     - the mode L stub E2E passed 11 of 11 with 72 checks (`summary-stub-20261106T015947Z.json`,
       01:59);
     - the stub evals had 22 pass, 0 fail and 2 not run, E20 and E22 (report `020003Z`, 02:00);
     - Runtime v3 was deployed (04:06);
     - the final E2E and evals above ran on v3;
     - Runtime v4, the same commits rebased onto `bf0a8b7`, was deployed with the user's approval
       (05:36) and smoke-tested only.
5. **Final runs (Runtime v3).** No product bug was found and no code was changed. The two issues
   seen (Known limits 7 and 4) belong to the sandbox.

## How to rerun everything

From the sandbox root. These steps only read AWS and the cluster. Never print the account id,
and never read `generated/aws/access.json` in a shared terminal.

```sh
export PATH=$HOME/.docker/bin:$PATH AWS_PROFILE=sandbox AWS_REGION=us-east-1 AWS_PAGER=""
ROOT=/Users/kaized/Desktop/mmrf/mmrf-virtual-lab-sandbox; cd $ROOT
red() { sed -E 's/[0-9]{12}/<ACCT>/g'; }
aws sts get-caller-identity --query Arn --output text | red     # assumed-role/WSParticipantRole/…; ExpiredToken: the user refreshes
```

**1. The stack, read-only.**

```sh
RID=orion_agent-RE0ngY6qRl
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $RID \
  --query '{version: agentRuntimeVersion, status: status, updated: lastUpdatedAt, image: agentRuntimeArtifact.containerConfiguration.containerUri}' | red
aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id $RID --endpoint-name orion_live \
  --query '{status: status, live: liveVersion}'
```

Expect version 4, READY, image tag `97926b08c368`, and endpoint READY at live version 4 (since
05:36 UTC on 2026-11-06; the E2E and evals above ran on version 3).

**2. The loopback port-forward.** Use one port per concurrent client. It injects the synthetic
session, so no Basic login is needed. Never use the Basic credential here.

```sh
while true; do kubectl --kubeconfig generated/aws/kubeconfig --context mmrf-synthetic -n mmrf-synthetic \
  port-forward service/identity 3081:8080 --address 127.0.0.1; sleep 2; done   # background; kill the loop when done
```

An i/o timeout to the EKS API means the public IP changed: re-point the allowlist first (Known
limits 6).

**3. BFF smoke.** Use a warm-up body from `orion/contracts/fixtures/request-examples.json` ("warmup
when the Assistant opens") with fresh 32-hex ids:

```sh
curl -sS -N --max-time 90 -X POST http://127.0.0.1:3081/api/orion/agui -H 'content-type: application/json' \
  -H 'accept: text/event-stream' --data @warmup.json | grep -o '"type":"[A-Z_]*"' | uniq -c   # RUN_STARTED, RUN_FINISHED
```

**4. Browser end to end.** It takes about 8 minutes, and every scenario calls Bedrock.

```sh
SANDBOX_ORIGIN=http://127.0.0.1:3081 ORION_E2E_MODEL=bedrock ORION_E2E_MODE=A node orion/scripts/e2e-mode-l.mjs
SANDBOX_ORIGIN=http://127.0.0.1:3081 ORION_E2E_MODEL=bedrock ORION_E2E_MODE=A node orion/scripts/e2e-mode-l.mjs edges   # one scenario
```

- Summaries go to `orion/notes/screenshots/e2e/summary-mode-a-bedrock-<stamp>.json`.
- `summary.json` is overwritten by every run, a subset included.
- A dropped port-forward connection (Known limits 7) is rerun by scenario. It is never skipped.

**5. Evals through the hosted BFF.** Five repeats take about 37 minutes, about 195 model calls.
Run them on their own port. Running them alongside the E2E shares the Runtime and skews the plan
times.

```sh
cd $ROOT/orion/evals && python3 -m orion_evals run --target bff --bff-origin http://127.0.0.1:3082 \
  --record-source bff --oracle-origin http://127.0.0.1:3082 --repeats 5 \
  --frontend-root $ROOT/.sources/frontend --agent-tests-root $ROOT/orion/agent/tests \
  --e2e-script $ROOT/orion/scripts/e2e-mode-l.mjs
```

- Add `--warmup-threads` only to measure the warmed best case. The report then says so.
- E11 and E20's lease expiry stay not run without a test Runtime (Known limits 2).

**6. Usage and records.** `usage` reads a run directory (`<dir>/runs/<runId>.json`), so copy the
report's records from S3 first:

```sh
B=$(terraform -chdir=$ROOT/orion/infra/terraform/orion-infra output -raw run_bucket); P=orion/
REP=$ROOT/orion/evals/reports/orion-evals-<stamp>.json; D=$(mktemp -d)/records; mkdir -p $D/runs
for id in $(jq -r '[.. | objects | .runId? // empty] | unique | .[]' $REP); do
  aws s3 cp "s3://$B/${P}runs/$id.json" $D/runs/ --only-show-errors; done    # an id with no record prints a 404: ignore it
cd $ROOT/orion/evals && python3 -m orion_evals usage $REP --run-dir $D
aws s3 cp "s3://$B/${P}runs/<runId>.json" - | jq '{status, code, env: .environment, calls: [.calls[] | {tool, transport, status, elapsedMs}], models: [.modelCalls[] | {role, model, outcome, elapsedMs}]}'
```

`runsWithoutRecord` should be 0.

**7. Logs, read-only.**

```sh
for g in /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-orion_live /aws/vendedlogs/bedrock-agentcore/runtime/orion_agent \
         /aws/vendedlogs/bedrock-agentcore/gateway/orion-vlab /aws/lambda/orion-vlab-tools; do
  echo "== $g"; aws logs filter-log-events --log-group-name $g --start-time $(( ($(date +%s) - 3600) * 1000 )) \
    --filter-pattern '?ERROR ?Traceback ?"Task timed out"' --query 'length(events)'; done    # each: 0
```

**8. The human check through CloudFront.** The user does this, and only the user types the login.

- Open the sandbox's `https_url` with `?app=Assistant` (`terraform -chdir=terraform output -raw
  https_url`).
- Ask J1 and J3.
- Confirm the plan, the steps arriving one by one, "Writing interpretation" before the
  Interpretation, and the DE card mounting.

**After any change to the cluster** (the user's call, never in a test shell):

- after `aws_sandbox.py deploy`, repeat the two-values-file `helm upgrade` (Known limits 11);
- after a new agent image, apply orion-infra with the new `agent_image_tag` (`../infra/README.md`,
  "The agent image": keep the live and the previous tag).
