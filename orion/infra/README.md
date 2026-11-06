# ORION infrastructure (IN track)

Mode L on the local Compose stack, the `orion-infra` Terraform module for modes A and H, the
arm64 agent image, and the sandbox patch for mode A. Design: ORION RFA rev 25, A1 and A4
("Deployment", "Timeout budget", "Security at sandbox level", "Model configuration on Bedrock",
"Observability and evaluation"), and "Delivery cut" (C-13, C-18, C-26, C-27, C-28). Wire contract:
`../contracts/README.md` v1.2 ("BFF routes", "Where records live"), which wins where they differ.

**Mode A is deployed** (2026-11-05, in the sandbox account, us-east-1): orion-infra stages 1 and 2
are applied, the sandbox patch is applied and the cluster runs the fork's frontend with ORION on.
`../notes/mode-a-status.md` has what was run and verified. Sections still marked "Not run" (for
example the test-Runtime checks) need AWS and have not been run; their commands are for the
operator, with the sandbox account's profile.

| Path | What it is |
| --- | --- |
| `../compose.orion.yaml` | Mode L: the Compose override layered on the root `compose.yaml` |
| `../scripts/orion-local.sh` | Wraps `up`, `down`, `logs`, `status` and `config` for mode L |
| `../agent/Dockerfile`, `../agent/.dockerignore` | The multi-stage, non-root arm64 agent image |
| `terraform/orion-infra/` | Runtime, Gateway, tool Lambda, S3, Secrets Manager, IAM, Pod Identity, CloudWatch |
| `terraform/orion-infra/vlab-tools.json` | The `vlab` target's MCP tool schema, generated from the catalog |
| `gateway_tools.py`, `tests/` | The schema generator and its tests |
| `patches/sandbox.patch` | The sandbox patch (mode A only, values-gated, off by default). Never applied here |

## Modes

| Concern | Mode L, local | Mode A, AWS demo | Mode H, fallback |
| --- | --- | --- | --- |
| Agent host | Compose container `orion-agent` | AgentCore Runtime | AgentCore Runtime |
| Tool transport | In-process | Gateway → Lambda | Gateway → Lambda |
| Stack the tools read | Local, via `http://identity:8080` | AWS, via CloudFront | Unmodified AWS stack, via CloudFront |
| Stack the viewers read | Local | AWS | Local; the browser compares a local fingerprint |
| BFF credentials | Developer profile (none needed with stubs) | Pod Identity role `orion-bff` | Developer profile, assuming `orion-bff` |
| Sandbox changes | Compose override | `patches/sandbox.patch` | None |
| Run records | Volume `orion-runs` | S3 | S3 |

## Mode L

```sh
docker compose -f compose.yaml -f orion/compose.orion.yaml config --quiet   # check the merge
orion/scripts/orion-local.sh up            # build orion-agent and the fork's frontend, start, wait
orion/scripts/orion-local.sh logs -f       # agent logs
orion/scripts/orion-local.sh down          # remove orion-agent; frontend back to compose.yaml alone
```

- `orion-agent` builds from `orion/agent` with two named build contexts (`golden` = `orion/golden`,
  `sandbox-config` = `config`), so the image carries `/config/cohort-fields.json` and `/golden`, as
  AgentCore runs it. It gets `ORION_MODE=L`, `ORION_PLANNER=stub`, `ORION_COMPOSER=stub`,
  `ORION_TOOLS=inprocess` and `ORION_TOOL_TRANSPORT=in_process` (settings.py and tools/config.py
  read one each), `ORION_VLAB_ORIGIN=http://identity:8080` and the volume
  `orion-runs` read-write at `/data/runs`. Its port is published on `127.0.0.1:8081` only
  (`ORION_AGENT_PORT`): the local gateway is unauthenticated and never leaves the machine.
- The frontend gets the same volume read-only at `/data/runs`, `ORION_MODE=L`,
  `ORION_AGENT_URL=http://orion-agent:8080` and `ORION_RUN_DIR=/data/runs`. It starts after the
  agent, so Docker copies the agent's `/data/runs` ownership (uid 10001) into the new volume.
- Healthchecks call `/ping` (200 for `Healthy` and `HealthyBusy`).
- Bedrock stays off. The AWS and model variables (`AWS_REGION`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `ORION_BEDROCK_CLIENT`, `ORION_BEDROCK_REGION`,
  `ORION_PLANNER_MODEL`, `ORION_COMPOSER_MODEL`, `ORION_FALLBACK_MODEL`, and `ORION_EXECUTOR`,
  `ORION_DE_LAUNCHER`, `ORION_FAULTS`) have no value in the override, so Compose passes each only
  when it is set in your shell. Nothing reads `~/.aws`. To try the real planner and composer, export
  short-term credentials and run `ORION_PLANNER=bedrock ORION_COMPOSER=bedrock AWS_REGION=us-east-1
  orion/scripts/orion-local.sh up`.

## The agent image

```sh
export PATH=$HOME/.docker/bin:$PATH
docker buildx build --platform linux/arm64 --load -t orion-agent:<tag> \
  --build-context golden=orion/golden --build-context sandbox-config=config orion/agent
```

Two stages: `deps` builds a virtualenv from `requirements.txt`; `runtime` copies it and the
package, runs as uid 10001 (`orion`), exposes 8080 and has a `HEALTHCHECK` on `/ping`. Without the
named contexts (`docker build orion/agent`) it still builds and `/config` and `/golden` are mounted
at run time, so the agent test command works unchanged against any tag.

For ECR, push a single-platform image without attestations (a provenance index is a second manifest):

```sh
# Not run (needs AWS).
docker buildx build --platform linux/arm64 --provenance=false --sbom=false --push \
  -t "$(terraform -chdir=orion/infra/terraform/orion-infra output -raw ecr_repository_url):$(git rev-parse --short=12 HEAD)" \
  --build-context golden=orion/golden --build-context sandbox-config=config orion/agent
```

**Keep the live and the previous Runtime tags.** The repository's lifecycle expires untagged images
only (7 days after the push), never a tag: a lifecycle rule cannot see which image a Runtime version
runs, and a Runtime version names its image by tag, so an expired tag breaks a rollback to that
version and any microVM that pulls it. Prune tags by hand, and never delete the image of the live
version or of the previous one (the rollback target). List the tags in use first:

```sh
RID=$(terraform -chdir=orion/infra/terraform/orion-infra output -raw runtime_id)
for v in $(aws bedrock-agentcore-control list-agent-runtime-versions --agent-runtime-id "$RID" \
    --query 'agentRuntimes[].agentRuntimeVersion' --output text); do
  aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RID" --agent-runtime-version "$v" \
    --query '[agentRuntimeVersion, agentRuntimeArtifact.containerConfiguration.containerUri]' --output text
done
aws ecr describe-images --repository-name orion/agent --query 'imageDetails[].[imageTags[0], imagePushedAt]' --output text
# Only for a tag no kept version uses:
# aws ecr batch-delete-image --repository-name orion/agent --image-ids imageTag=<old tag>
```

## The orion-infra module

A root module with local state in `terraform/orion-infra/` (gitignored), separate from the
sandbox's `terraform/`. Provider `hashicorp/aws ~> 6.69`; `terraform init` resolved 6.72.0, which
has `aws_bedrockagentcore_agent_runtime` (with `server_protocol = "AGUI"`),
`aws_bedrockagentcore_agent_runtime_endpoint`, `aws_bedrockagentcore_gateway` and
`aws_bedrockagentcore_gateway_target`. The lock file is committed on purpose.

| Piece | Resources |
| --- | --- |
| Agent | ECR repository `orion/agent` (immutable tags, scan on push; the lifecycle expires untagged images after 7 days and never a tag); Runtime `orion_agent` (PUBLIC, AGUI, arm64 image) and endpoint `orion_live` |
| Tools | Gateway `orion-vlab` (MCP, `AWS_IAM` inbound); target `vlab` (Lambda, tool schema from S3); Lambda `orion-vlab-tools` (arm64, 58 s, X-Ray active); stretch target `kg` off |
| Data | Bucket `orion-runs-<account>-<region>`: SSE-S3, versioning, public access blocked, bucket owner enforced, TLS only, 30-day expiry under `orion/`; secret `orion/vlab-basic`, created empty |
| Identity | Roles `orion-runtime`, `orion-bff`, `orion-gateway`, `orion-vlab-tools`; the `eks-pod-identity-agent` add-on and one association (`mmrf-synthetic/orion-frontend` → `orion-bff`) |
| Logs | `/aws/lambda/orion-vlab-tools`; vended application logs for the Runtime and the Gateway; optional X-Ray trace delivery; all 30 days (`log_retention_days`). The Runtime's stdout goes to service-created `/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>` groups this module does not own: they never expire until deploy step 6 sets their retention |

Every name, ARN and model id is a variable (`variables.tf`). The ones you must set:
`sandbox_account_id`, `vlab_origin` (the sandbox's `https_url` output), and in stage 2
`agent_image_tag` and `lambda_zip_path` (or `lambda_package_type = "Image"` and `lambda_image_uri`).

### Principals (A4)

| Principal | Granted | Never |
| --- | --- | --- |
| `orion-bff` (Pod Identity; mode H developers through `bff_extra_trusted_principal_arns`) | `bedrock-agentcore:InvokeAgentRuntime` and `StopRuntimeSession` on the Runtime and its endpoint; `s3:GetObject` on `orion/runs/*` | Bedrock, S3 writes, Gateway, secrets |
| `orion-runtime` | `bedrock_invoke_actions` on the planner, composer and fallback inference profiles, and on their foundation models in every Region only through those profiles (condition `bedrock:InferenceProfileArn`; the `us.` profiles route to us-east-1, us-east-2 and us-west-2, and the `*` Region also covers a `global.` profile's Region-less model ARN); `InvokeGateway` on the one Gateway; `s3:GetObject`/`PutObject` on `orion/runs/*` and `orion/threads/*`, `ListBucket` on the bucket (no prefix condition: a GetObject has no `s3:prefix`, so a conditioned grant turns a missing record or ledger into 403, and `run_store.py` treats only 404 as missing); logs, X-Ray, metrics in `bedrock-agentcore`, ECR pull, workload token | A direct `InvokeModel` on a foundation model in any Region (a base id, with the mantle client, is granted in `region` only); the Basic secret (granted only under cut 1, `tool_transport = "in_process"`) |
| `orion-gateway` | `lambda:InvokeFunction` on the tool Lambda (and the kg Lambda when enabled); `s3:GetObject` on its tool schema | Everything else |
| `orion-vlab-tools` | `secretsmanager:GetSecretValue` on `orion/vlab-basic`; its own log group; X-Ray | Bedrock, S3, Gateway |

`terraform output iam_actions` lists every granted action per principal for review. The BFF
cannot list the bucket, so a missing record reads as 403, not 404: the view and record routes
must answer `RUN_NOT_FOUND` for both `NoSuchKey` and `AccessDenied` on `orion/runs/<runId>.json`.

### Trust policies

Each role an AgentCore resource or a pod assumes can be assumed only by the one resource that uses
it, in the sandbox account (`aws:SourceAccount`) and Region:

| Role | Assumed by | Conditions |
| --- | --- | --- |
| `orion-runtime` | `bedrock-agentcore.amazonaws.com` | `aws:SourceArn` ArnLike `arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime_name>-*`: the Runtime and its endpoints (`runtime/<id>/runtime-endpoint/<name>`). A Runtime name has no hyphen, so no other Runtime, and no Gateway, code interpreter or browser, matches. (AgentCore's guide shows `<account>:*`; this is narrower.) |
| `orion-gateway` | `bedrock-agentcore.amazonaws.com` | `aws:SourceArn` ArnLike `...:gateway/<gateway_name in lower case>-*`, the guide's pattern (a Gateway id is its name in lower case, a hyphen and 10 characters). It also admits a Gateway whose name starts with `orion-vlab-`; do not create one with this role |
| `orion-bff` | `pods.eks.amazonaws.com` (`sts:AssumeRole`, `sts:TagSession`) | `aws:SourceArn` = the sandbox cluster; `aws:RequestTag/kubernetes-namespace` = `kubernetes_namespace` and `aws:RequestTag/kubernetes-service-account` = `frontend_service_account` (the EKS guide's conditions), so only `mmrf-synthetic/orion-frontend` gets credentials, whatever other association names the role. The association keeps session tags on (`disable_session_tags = false`): with them off, the frontend gets no credentials. Mode H developers are a separate statement (`bff_extra_trusted_principal_arns`) |
| `orion-vlab-tools` | `lambda.amazonaws.com` | None (Lambda's standard execution-role trust) |

A test Runtime or a second workspace with its own `runtime_name` and `gateway_name` gets roles that
trust only its own names.

### Residual risks

Accepted for the synthetic-data sandbox, and not fixed here:

- **The frontend pod can reach any public HTTPS host.** The patch's NetworkPolicy
  `orion-frontend-egress` allows TCP 443 to `0.0.0.0/0` minus `orion.egress.exceptCidrs` (10/8,
  172.16/12, 192.168/16, 169.254/16), and TCP 80 only to the Pod Identity Agent at
  169.254.170.23. That admits every public IPv4 address, not only the AgentCore Runtime and S3
  endpoints the BFF needs: those have no fixed addresses a NetworkPolicy can name, and nothing else
  narrows it (the node security group, the subnet's network ACL and the route to the internet
  gateway all allow egress; the VPC has no endpoints, Network Firewall or DNS Firewall). The pod is
  the one that holds `orion-bff` credentials, but those can only invoke and stop the one Runtime and
  read run records. The 443 rule adds no route to private or link-local addresses (other pods, nodes,
  IMDS); the chart's existing `sandbox-only` policy still decides which pods of the release it reaches. Closing it is a sandbox Terraform change (an S3 gateway endpoint and a
  `bedrock-agentcore` interface endpoint, then 443 only to the endpoint ENIs and the S3 prefix list),
  out of scope for ORION.
- **The Gateway role's name prefix.** `orion-gateway` also trusts a Gateway named `orion-vlab-<more>`
  (see "Trust policies"); creating one with that role needs `iam:PassRole`.

### Environment it sets

| Where | Variables |
| --- | --- |
| Runtime | `ORION_MODE=A`, `ORION_RUN_BUCKET`, `ORION_RUN_PREFIX`, `ORION_PLANNER=bedrock`, `ORION_COMPOSER=bedrock` (variables `planner`, `composer`), `ORION_EXECUTOR=engine`, `ORION_TOOLS` (`gateway`/`inprocess`) and `ORION_TOOL_TRANSPORT` (`gateway`/`in_process`), `ORION_GATEWAY_URL`, `ORION_GATEWAY_TARGET`, `ORION_BEDROCK_CLIENT` (`runtime`), `ORION_BEDROCK_REGION`, `ORION_PLANNER_MODEL` and `ORION_COMPOSER_MODEL` (`us.anthropic.claude-opus-5-5`), `ORION_FALLBACK_MODEL` (`us.anthropic.claude-opus-5`); under cut 1 `ORION_VLAB_ORIGIN` and `ORION_VLAB_BASIC_SECRET_ARN`; a test Runtime adds `ORION_TEST_RUNTIME=1` and `ORION_FAULTS`; then `runtime_environment` |
| Tool Lambda | `ORION_MODE=A`, `ORION_VLAB_ORIGIN` (CloudFront), `ORION_VLAB_BASIC_SECRET_ARN` (the secret's ARN, never its value), `ORION_GOLDEN_DIR` only when `lambda_golden_dir` is set (the zip carries the golden snapshot), a test Runtime's `ORION_TEST_RUNTIME` and `ORION_FAULTS`, then `lambda_environment` |
| BFF (`terraform output bff_environment`, or `helm_values` for the patch) | `ORION_MODE=A`, `ORION_RUNTIME_ARN`, `ORION_RUNTIME_QUALIFIER` (= `orion_live`), `ORION_RUN_BUCKET`, `ORION_RUN_PREFIX`, `AWS_REGION` |

The A4 timeout budget is enforced where it is configurable: `lambda_timeout_s` must be 56 to 59 s
(default 58), between the 55 s tool HTTP timeout and the 60 s step timeout in `tools/base.py`.
CloudFront's 60 s origin read timeout is the sandbox's (`terraform/https.tf`). The Gateway's own
timeout cannot be set (Open question 19).

### The vlab tool schema

`vlab-tools.json` is generated from each server tool's Input model in `TOOL_REGISTRY` and uploaded
to `s3://<bucket>/gateway/vlab-tools.json`; a changed file replaces the target. Every node keeps a
`type` (the target schema requires one) and only `type`, `description`, `properties`, `required`
and `items`. Gateway cannot say "or null", so required-but-nullable inputs
(`top_mutated_genes.filter0`, `survival_compare.scope` and `.split`) appear optional there; the
Lambda validates against the Input model itself. `orion/agent/lambda/build.py` writes a second
listing, `gateway-tools.json`, which leaves nullable properties without a `type`; point
`vlab_tool_schema_path` at it only if the Gateway accepts that.

```sh
docker run --rm -v $PWD/orion/agent:/app:ro -v $PWD/orion/infra:/infra -w /app -e PYTHONPATH=/app \
  orion-agent:dev python /infra/gateway_tools.py --write /infra/terraform/orion-infra/vlab-tools.json
```

## Deploy order (modes A and H)

Day-2 plan: deploy Runtime, Gateway and Lambda; Pod Identity, egress and the fork; then the
**12:00 gate: mode A or mode H**. All of it: **Not run (needs AWS).**

0. **Before.** The sandbox runs on AWS with HTTPS (`scripts/aws_sandbox.py deploy`, its own
   track), and Bedrock model access for Opus 5.5 and Opus 5 is granted in us-east-1 (A-4, Open
   question 18). Note the sandbox outputs `https_url` and `cluster_name`.
1. **Stage 1: base.** In `orion/infra/terraform/orion-infra`, with a `stage.tfvars` holding
   `sandbox_account_id` and `vlab_origin` (gitignored):

   ```sh
   terraform init
   terraform plan -var-file=stage.tfvars -var create_runtime=false -var create_tools=false -out=base.tfplan
   terraform apply base.tfplan
   ```

   Creates ECR, the bucket, the empty secret, the four roles, the Pod Identity add-on and association.
   If the operator already created `orion/vlab-basic`, adopt it before the plan instead of creating it
   (the plan then shows only an in-place tag update for it, never a replace), and skip step 2:

   ```sh
   terraform import -var-file=stage.tfvars -var create_runtime=false -var create_tools=false \
     aws_secretsmanager_secret.vlab_basic "$(aws secretsmanager describe-secret --secret-id orion/vlab-basic --query ARN --output text)"
   ```
2. **Fill the secret once**, from the team credential `aws_sandbox.py` keeps in
   `generated/aws/access.json` (mode 0600). The value goes through a pipe, never the command line:

   ```sh
   jq -c '{username, password}' generated/aws/access.json | aws secretsmanager put-secret-value \
     --secret-id orion/vlab-basic --secret-string file:///dev/stdin --query VersionId --output text
   ```
3. **Push the agent image** (above), tagged with the commit.
4. **Package the tool Lambda** with the TL track's builder (zip for arm64, handler `handler.handler`,
   the golden snapshot inside), and confirm the tool schema is current:

   ```sh
   docker run --rm -v $PWD/orion/agent:/app -w /app orion-agent:dev python lambda/build.py --deps installed --verify
   docker run --rm -v $PWD/orion/agent:/app:ro -v $PWD/orion/infra:/infra:ro -w /app -e PYTHONPATH=/app \
     orion-agent:dev python /infra/gateway_tools.py --check /infra/terraform/orion-infra/vlab-tools.json
   ```
5. **Stage 2: everything.**

   ```sh
   terraform plan -var-file=stage.tfvars -var agent_image_tag=<commit> \
     -var lambda_zip_path=../../../agent/lambda/dist/orion-vlab-tools-arm64.zip -out=full.tfplan
   terraform apply full.tfplan
   ```
6. **Smoke the Runtime** with a `warmup` turn (expect `RUN_STARTED`, then `RUN_FINISHED` `Warmed`):

   ```sh
   printf '{"threadId":"%s","runId":"%s","state":{},"messages":[],"tools":[],"context":[],"forwardedProps":{"orion":{"action":"warmup"}}}' \
     "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > /tmp/orion-infra/warmup.json
   aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn "$(terraform output -raw runtime_arn)" \
     --qualifier "$(terraform output -raw runtime_qualifier)" --runtime-session-id "orion-$(openssl rand -hex 16)" \
     --content-type application/json --accept text/event-stream \
     --payload fileb:///tmp/orion-infra/warmup.json /tmp/orion-infra/warmup.sse && cat /tmp/orion-infra/warmup.sse
   ```

   Then **set retention on the Runtime's stdout log groups.** AgentCore creates
   `/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>` itself, one per endpoint, on first use,
   with no expiry, and this module does not manage them (Terraform would have to import each group
   after its first call). They hold run ids, outcomes, error codes and short answer fragments, so
   give them the 30 days of the module's own groups and of the run records:

   ```sh
   RID=$(terraform output -raw runtime_id)
   for ep in "$(terraform output -raw runtime_qualifier)" DEFAULT; do
     aws logs put-retention-policy --log-group-name "/aws/bedrock-agentcore/runtimes/$RID-$ep" --retention-in-days 30
   done
   aws logs describe-log-groups --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/$RID-" \
     --query 'logGroups[].[logGroupName, retentionInDays]' --output text   # every group: 30
   ```

   For the Runtime deployed on 2026-11-05 (`orion_agent-RE0ngY6qRl`, endpoints `orion_live` and
   `DEFAULT`) that is exactly:

   ```sh
   aws logs put-retention-policy --region us-east-1 \
     --log-group-name /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-orion_live --retention-in-days 30
   aws logs put-retention-policy --region us-east-1 \
     --log-group-name /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-DEFAULT --retention-in-days 30
   ```

   A group that does not exist yet answers `ResourceNotFoundException`: run the command again after
   that endpoint's first call. Repeat for every new Runtime id (a test Runtime, or a redeploy after
   teardown) and for any new endpoint. The policy stays on a group across Runtime versions.
7. **Sandbox patch (mode A only; needs MMRF's approval, Open question 14).** The patch's
   `sources.json` hunk pins the frontend to the fork `kaizzzed/mmrf-data-commons` at `3fc0e6a`
   (`3fc0e6aec6737b30e012dab1dae51828336c376d`), the head of the fork's `orion/mode-l` branch (also
   tag `orion-mode-l`). To ship a newer frontend, push it to the fork first and set the new commit in
   that hunk. Then, in the sandbox checkout:

   ```sh
   git apply orion/infra/patches/sandbox.patch
   python3 scripts/aws_sandbox.py deploy          # rebuilds and publishes the fork's frontend
   terraform -chdir=orion/infra/terraform/orion-infra output -json helm_values > generated/aws/orion-values.json
   helm upgrade synthetic helm -n mmrf-synthetic --kubeconfig generated/aws/kubeconfig \
     --values generated/aws/values.json --values generated/aws/orion-values.json --wait --timeout 20m
   ```

   Pass both values files; never `--reuse-values`. With `--reuse-values` Helm takes the previous
   release's merged values instead of the new chart's defaults, so when the release was installed
   from the unpatched chart `.Values.orion.egress` is missing and the template fails (or
   `automountServiceAccountToken` renders empty). Any later `aws_sandbox.py deploy` runs
   `helm upgrade` with `values.json` alone, which turns ORION off again: repeat this command after it.

   `.sources/frontend` must already be at the pinned commit (or have the fork as `origin`):
   `fetch_sources.py` fetches from `origin`, which in this checkout is theMMRF's repository.
8. **Check the BFF path**: the frontend pod has `serviceAccountName: orion-frontend`, the six
   `ORION_*`/`AWS_REGION` variables and `AWS_CONTAINER_CREDENTIALS_FULL_URI` (Pod Identity
   injected); `kubectl get networkpolicy orion-frontend-egress`; then J1 end to end through
   CloudFront, with SSE arriving event by event (no buffering).

### The 12:00 gate

At 12:00 on day 2, take **mode A** only if all four hold; otherwise take **mode H** (cut 3):

1. The frontend pod gets `orion-bff` credentials through Pod Identity with service-account tokens
   off (Open question 19), and its egress reaches AgentCore and S3.
2. J1 runs end to end in the deployed fork through CloudFront, and the stream is not buffered.
3. Wilcoxon DE for J3 fits the A4 budget through CloudFront (Open question 15, below).
4. A Gateway call to `vlab___de_run` returns before the Lambda's 58 s (Open question 19); if not,
   take cut 1 too: `-var tool_transport=in_process` (the Runtime then reads the secret).

**Mode H**: do not apply the patch (or undo it: `helm upgrade synthetic helm ... --values
generated/aws/values.json --wait`, which leaves `orion.enabled` at its default, false). Run the fork's frontend locally on the local stack with `ORION_MODE=H`,
`ORION_RUNTIME_ARN`, `ORION_RUNTIME_QUALIFIER`, `ORION_RUN_BUCKET`, `ORION_RUN_PREFIX` and
`AWS_REGION` from `terraform output bff_environment`, on a developer profile that assumes
`orion-bff` (add its role to `bff_extra_trusted_principal_arns`). The tools read the AWS stack, the
viewers the local one; the viz card compares the local fixture fingerprint (C-28).

## Checks that need a test Runtime

A **test Runtime** is this module applied with `-var test_runtime=true` (and, for E11,
`-var runtime_faults=...`), on day 1 PM. Before the gate, re-apply with `test_runtime=false`, which
drops `ORION_TEST_RUNTIME` and `ORION_FAULTS` in a new Runtime version. To keep both at once, use a
second workspace with its own names (`runtime_name`, `gateway_name`, role, bucket, secret and
Lambda names are all variables). Write results to `/tmp/orion-infra/checks/` and copy the summary
into the decision log. **All four: Not run (needs AWS).**

Shared setup for the shell (never echo the credential):

```sh
cd orion/infra/terraform/orion-infra
RT=$(terraform output -raw runtime_arn); Q=$(terraform output -raw runtime_qualifier)
B=$(terraform output -raw run_bucket); P=$(terraform output -raw run_prefix)
record() { aws s3 cp "s3://$B/${P}runs/$1.json" - | jq '{status, code, revision, updatedAt, lease, steps: [.steps[] | {id, status, code}], calls: [.calls[] | {callId, startedAt, elapsedMs, status}], modelCalls}'; }
```

### Open question 7: a tab closed mid-stream

Does AgentCore Runtime cancel the handler, or recycle the session, when the caller disconnects?

1. Through the BFF in mode H (or the patched mode A frontend), run J3 to the save pause and click
   Create. When the step list shows `de_run` Running, close the tab. Note the time (UTC, seconds).
2. Without a browser, the same with the CLI: start a J1 `ask` (a `RunAgentInput` like the warmup
   above, action `ask`, question "Compare overall survival by CGS risk category", `cohortContext` for
   all cases) with `timeout 3 aws bedrock-agentcore invoke-agent-runtime ...`, so the client drops
   three seconds into the stream.
3. Every 10 s for 4 minutes, run `record <runId>` and read the BFF's view route
   (`GET /api/orion/runs/<runId>/view`), which applies the expired-lease rule.
4. Record: disconnect time; each record revision with `status` and `updatedAt`; whether any call's
   `startedAt` is after the disconnect; the final status (Complete, or Interrupted with
   `RUN_INTERRUPTED` after the 180 s lease); Runtime log lines for `CancelledError` or client
   disconnect in `/aws/bedrock-agentcore/runtimes/<runtime_id>-orion_live`.
5. Reading: an end status with calls started after the disconnect means the detached controller
   survives and nothing else is needed. Interrupted after the lease means AgentCore stopped the
   process: say so in the decision log, and E20 then expects Interrupted.

### Open question 8: a second call on a busy session

Will a second `InvokeAgentRuntime` or a `StopRuntimeSession` on a busy session get 409?

1. Start a J1 `ask` on session `orion-<threadId>` and keep its stream open (no timeout).
2. Within 2 s, on the same `--runtime-session-id`, send a `warmup` turn; record the CLI exit code,
   the error code and message (for example `ConflictException`), the time to first byte, and whether
   the first stream keeps going.
3. Repeat step 2 with a `cancel` turn for the running run (`runId` of step 1).
4. On a fresh busy session, call `aws bedrock-agentcore stop-runtime-session --agent-runtime-arn "$RT"
   --qualifier "$Q" --runtime-session-id <id>`; record the response and what the first stream and
   the run record do next (the run should end Cancelled after its Stopping lease, or Interrupted).
5. Repeat each three times; record the botocore and AWS CLI versions
   (`aws --version`), because the botocore changelog says enforcement is "not yet active".
6. Reading: 409 on a second invoke confirms the BFF rule (abort the open call before `cancel`,
   `edit_plan` or `confirm`; map 409 to `SESSION_BUSY`). A 409 on `StopRuntimeSession` breaks
   C-18's last resort: report it.

### Open questions 10 and 12: planner and composer timings

At least 20 cold and 20 warm runs per role, J1 and J3 prompts alternating.

- **Cold**: a new session id after at least 6 minutes with no call (the 5-minute cache has
  expired). **Warm**: the same session, within 5 minutes of a `warmup` turn or of the previous run.
- Per run, from the record's `modelCalls`: `role`, `model`, `fallback`, `effort`, `elapsedMs`,
  `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `outcome`, `stopReason`;
  and from the client stream (timestamps per frame, `ts` of `CUSTOM` heartbeats): POST to
  `RUN_STARTED`, to the first snapshot with a non-null `plan` (first plan render, Open question 12),
  and, for the composer, from the first `Answering` snapshot to the snapshot with `interpretation`
  filled ("Writing interpretation"), counting re-composes.

  ```sh
  for id in $(cat /tmp/orion-infra/checks/run-ids.txt); do
    aws s3 cp "s3://$B/${P}runs/$id.json" - | jq -c --arg id "$id" '.modelCalls[] | {run: $id, role, model, effort, elapsedMs, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, outcome}'
  done > /tmp/orion-infra/checks/model-calls.jsonl
  ```
- Report p50 and p95 per role, cold and warm, and the cache-read share. Against the 5 s plan p95
  **Proposal** (NFR-01): if warm misses, try re-warming per session; if cold misses, price the
  1-hour cache TTL; only then a Sonnet 5.5 planner, and only if every eval still passes.

### Open question 15: CloudFront and Wilcoxon timing

Does Wilcoxon DE on the J3 groups finish inside the A4 budget through CloudFront?

1. Body: `orion/golden/proteinpaint/termdb-DE-wilcoxon.json`, `requests[0].body`, the viewer's own
   J3 request (520 vs 480 samples). ProteinPaint caches by request, and the group names are part of
   the key, so a cold run renames both groups with a fresh suffix:

   ```sh
   jq --arg s "$(date +%s)" '.requests[0].body | .samplelst.groups[0].name += " " + $s | .samplelst.groups[1].name += " " + $s' \
     orion/golden/proteinpaint/termdb-DE-wilcoxon.json > /tmp/orion-infra/checks/de-cold.json
   jq -r '"user = \"" + .username + ":" + .password + "\""' generated/aws/access.json | \
     curl -sS -K - -o /dev/null -w '%{http_code} %{time_starttransfer} %{time_total}\n' \
       -H 'content-type: application/json' --data @/tmp/orion-infra/checks/de-cold.json \
       "$(jq -r .origin generated/aws/access.json)/protein-paint/termdb/DE"
   ```
2. Five cold runs (new suffix each), then five warm runs (the last body again, a cache hit). Record
   HTTP status, `time_starttransfer` and `time_total`, and any 429 `CACHE_BUSY` or 504.
3. Then through the tool path: `vlab___de_run` from the test Runtime on the same groups; record the
   Lambda's `REPORT Duration`, the Gateway's application log for the call, and the step's call
   `elapsedMs` in the record.
4. Pass: every cold `time_total` below 55 s (the tool HTTP timeout), with p95 at most 45 s for
   margin. Fail: take the VPC path (a VPC Lambda to the internal ALB, 180 s idle timeout) as the
   hardening item, or keep DE in-process (cut 1) if only the Gateway cuts it off.

## Checks run here (2026-11-05)

| Check | Result |
| --- | --- |
| `terraform fmt -recursive -check`, `init -backend=false`, `validate` in `terraform/orion-infra` | Pass (hashicorp/aws 6.72.0) |
| `terraform/orion-infra/tests/orion_infra.tftest.hcl` (mocked provider, plan only) | Pass, 11 of 11 (`terraform test`, Terraform 1.17.1), including the least-privilege checks: models only through the inference profiles, the three trust policies, session tags on, and an untagged-only ECR lifecycle. The mock gives `aws_iam_policy_document` a valid JSON `json`, since the role and bucket policy arguments reject a random string at plan time |
| `docker compose -f compose.yaml -f orion/compose.orion.yaml config --quiet` | Pass; read only, nothing started (print with `--no-interpolate`: an interpolated print shows exported AWS credentials) |
| `bash -n orion/scripts/orion-local.sh` | Pass; the script itself was not run |
| `docker buildx build --platform linux/arm64 --load` as `orion-agent:arm64-check` | Pass: arm64, uid 10001, healthy on `/ping`, warmup turn answered, no mounts needed |
| Agent suite in `orion-agent:arm64-check` | Pass, golden tests included (the image bakes `/golden`) |
| The image with the Compose environment and the current code | Starts and turns healthy on the Compose network |
| The Runtime's Terraform environment through `Settings.from_env`, `tools.config` and `model.config` | Parses: mode A, bedrock planner and composer, engine executor, Gateway tools, `us.` model profiles |
| `python -m unittest discover -s tests` in `orion/infra`, `mypy --strict` | Pass |
| `git apply --check orion/infra/patches/sandbox.patch` in the sandbox | Pass; never applied |
| `helm template` / `helm lint` of the patched chart (scratch copy) | Off: renders byte-identical to the current chart; on: the ServiceAccount, `orion-frontend-egress`, `serviceAccountName` and the six variables only |

## Teardown

Removes everything the module creates and leaves the sandbox's own stack as it was. **Not run.**

1. **Sandbox patch first.** `helm upgrade synthetic helm -n mmrf-synthetic --kubeconfig
   generated/aws/kubeconfig --values generated/aws/values.json --wait` (no `orion-values.json`, so
   `orion.enabled` is false: the ServiceAccount, the NetworkPolicy and the frontend's ORION env go),
   or `helm rollback synthetic <revision before the patch>`, then `git apply -R orion/infra/patches/sandbox.patch` and redeploy if the fork pin should go too.
2. **Empty what Terraform will not.** `force_delete` (ECR) and `force_destroy` (S3) are read from
   the state when the resource is deleted, so passing them only on the destroy plan has no effect:
   **apply** them first, with the same variables as the last apply plus the two flags (this deletes
   every image and every record version when the destroy runs: the run records are gone for good):

   ```sh
   terraform plan -var-file=stage.tfvars <same -var flags> -var ecr_force_delete=true \
     -var bucket_force_destroy=true -out=force.tfplan     # expect 2 in-place updates, nothing else
   terraform apply force.tfplan
   ```

   Or empty the ECR repository and every object version in the bucket by hand first.
3. **Destroy** in `orion/infra/terraform/orion-infra`, with the same variables as the last apply:

   ```sh
   terraform plan -destroy -var-file=stage.tfvars <same -var flags, force flags included> -out=destroy.tfplan
   terraform apply destroy.tfplan
   terraform state list        # must print nothing
   ```

   This removes the Runtime and endpoint, the Gateway and its targets, the Lambda, the log groups
   and log deliveries, the bucket, the secret (immediately, `secret_recovery_window_days = 0`), the
   four roles, the Pod Identity association and the add-on. To keep an operator-created secret,
   `terraform state rm aws_secretsmanager_secret.vlab_basic` before the destroy plan.
4. **Service-created leftovers.** AgentCore creates `/aws/bedrock-agentcore/runtimes/<runtime_id>-*`
   log groups itself; delete them by name. If `enable_trace_delivery` was on, the X-Ray traces age out.
5. **Order with the sandbox.** Destroy orion-infra before `aws_sandbox.py destroy`: the association
   and the add-on live on the sandbox's cluster. If the cluster is already gone, `terraform state rm
   aws_eks_pod_identity_association.bff 'aws_eks_addon.pod_identity[0]'` and destroy the rest. Nothing
   here touches the sandbox's `terraform/` directory or its state.
