# ORION mode A: teardown

**The mode A resources are left running on purpose,** for the demo. Nothing here has been run,
apart from read-only `-refresh=false` plans of steps 4 and 6 for the integrity check (no plan file
kept, no apply: `$S3/integrity-check/`, where `$S3` is the stage 3 scratchpad named in
`mode-a-status.md`, "Where the numbers come from").
This is the ordered procedure for when the user decides to remove them. Every step that changes
something is the user's call: a Terraform or Helm write, a deletion, a git commit. Each step says
what to expect before going on.

This note covers ORION only:

- the `orion-infra` Terraform module (35 managed resources, listed in `mode-a-status.md`, "What is
  deployed");
- the two log groups AgentCore created itself;
- the cluster side: Helm and the sandbox patch.

It leaves the sandbox's own stack in place: EKS, CloudFront, the ALB, the VPC and the six
`mmrf-synthetic/*` repositories. It follows `../infra/README.md`, "Teardown", with this
deployment's names and revisions filled in.

## 0. Before you start

```sh
export PATH=$HOME/.docker/bin:$PATH AWS_PROFILE=sandbox AWS_REGION=us-east-1 AWS_PAGER=""
ROOT=$(git rev-parse --show-toplevel); cd $ROOT      # run from inside the sandbox checkout
INFRA=$ROOT/orion/infra/terraform/orion-infra
red() { sed -E 's/[0-9]{12}/<ACCT>/g'; }
K() { kubectl --kubeconfig $ROOT/generated/aws/kubeconfig --context mmrf-synthetic -n mmrf-synthetic "$@"; }
H() { helm --kubeconfig $ROOT/generated/aws/kubeconfig --kube-context mmrf-synthetic -n mmrf-synthetic "$@"; }
aws sts get-caller-identity --query Arn --output text | red      # assumed-role/WSParticipantRole/…
```

`K` and `H` are shell functions, not variables: zsh does not word-split `$K`-style strings.

1. **Credentials.** `ExpiredToken` means the user refreshes the Workshop Studio credentials. Check
   again before each apply: an apply that loses its credentials midway leaves a partial state.
2. **Nothing else is using the stack.** Stop any port-forward loop, browser E2E or eval run
   against it, and end any demo.
3. **The files the destroy needs are present.** Keep all of them until step 6 says the state is
   empty:
   - `$INFRA/terraform.tfstate` (serial 66 after the Runtime version 4 apply of 2026-11-06 05:36
     UTC) and its backup `generated/aws/orion-infra.tfstate.bak`;
   - `$INFRA/stage.tfvars`;
   - **the Lambda zip `orion/agent/lambda/dist/orion-vlab-tools-arm64.zip`.** The destroy plan
     reads it. Its SHA-256 equals the state's `source_code_hash`
     (`ZzMIJcN4GLcekzG7GJ0XwNq/95zCqlhiNITwk7Nmy/s=`). Do not rebuild it before the destroy.

   Check its hash:

   ```sh
   shasum -a 256 orion/agent/lambda/dist/orion-vlab-tools-arm64.zip | awk '{print $1}' | xxd -r -p | base64
   ```
4. **Keep anything you want from S3 and the logs.** The destroy deletes every run record version
   and the tool schema. Step 7 deletes the Runtime's stdout logs. To keep the records, copy them
   first (this only reads AWS):

   ```sh
   B=$(terraform -chdir=$INFRA output -raw run_bucket)
   aws s3 sync "s3://$B/orion/" $ROOT/generated/aws/orion-runs-backup/ --only-show-errors
   ```

   Write down the outputs step 6 checks against, before the state is empty:

   ```sh
   terraform -chdir=$INFRA output -json | jq '{runtime_id, gateway_id, tool_lambda_name, run_bucket, log_groups} | map_values(.value)' | red
   ```

## 1. EKS API access (only if `kubectl` times out)

```sh
K get deploy frontend -o name      # deployment.apps/frontend; an i/o timeout means the public IP changed
```

The EKS API admits one /32, the operator's current address (the sandbox's `operator_cidr` rule).
Re-point it with a guarded sandbox plan:

- the plan must show exactly one in-place update of `aws_eks_cluster.sandbox`
  `public_access_cidrs`, with 0 to add and 0 to destroy;
- no Helm and no image publish;
- **not** `aws_sandbox.py deploy`, which also republishes six images and re-runs Helm.

The stage 3 helper does exactly this and prints no address:

```sh
python3 -I orion/infra/scripts/update_allowlist.py    # uses guarded_deploy.py beside it; exit 0 = updated or already current, 3 = plan refused
```

(`<stage 3 scratchpad>` is the folder named `$S3` in `mode-a-status.md`, "Where the numbers come
from".) If the scratchpad is gone, the same steps by hand:

```sh
# 1. This machine's /32 into the saved config (the file stays 0600; nothing is printed)
python3 -c "import json,urllib.request,pathlib;p=pathlib.Path('generated/aws/config.json');c=json.loads(p.read_text());c['operator_cidr']=urllib.request.urlopen('https://checkip.amazonaws.com',timeout=10).read().decode().strip()+'/32';p.write_text(json.dumps(c,indent=2)+'\n');print('operator_cidr updated')"
# 2. Plan with the sandbox's own operator (rewrites terraform/sandbox.auto.tfvars.json from config.json); show only the actions
OP='import json,sys,pathlib; sys.path.insert(0,"scripts"); import aws_sandbox as a; o=a.Operator(json.loads(pathlib.Path("generated/aws/config.json").read_text())); o.identity()'
python3 -c "$OP; o.init_terraform(); o.tf('plan','-input=false','-out=sandbox.tfplan')" > /dev/null
terraform -chdir=terraform show -no-color sandbox.tfplan | grep -E '^  # |^Plan:'
#    expect: "# aws_eks_cluster.sandbox will be updated in-place" and "Plan: 0 to add, 1 to change, 0 to destroy"; anything else: stop
# 3. Apply that plan file, then refresh the kubeconfig
python3 -c "$OP; o.tf('apply','-input=false','sandbox.tfplan'); o.connect_cluster()" > /dev/null
K get deploy frontend -o name
```

## 2. Cluster side first: take ORION out of the release

Helm goes first. After it, the frontend no longer uses the Pod Identity association, and it no
longer calls the Runtime that the destroy removes.

```sh
H history synthetic      # expect revision 5 "deployed" (ORION on), 4 "failed", 3, 2 and 1 "superseded"
```

| Revision | What it is |
| --- | --- |
| 1 | The sandbox before ORION: the unpatched chart and the upstream frontend `build-188f27662ebc4be2` (`sha256:4f1d1da8…`) |
| 2 | The patched chart with `orion.enabled` false, and the fork's frontend `build-367ebcf26bc746f0` (`sha256:d78cb60b…`) |
| 3 | ORION on (`values.json` plus `orion-values.json`) |
| 4 | Failed: the stage 4 sandbox redeploy (`build-01113ac244d94caf`) removed the `orion-frontend` ServiceAccount, so the new frontend pod never started |
| 5 | ORION on again (`values.json` plus `orion-values.json`), with the fork's frontend at `e91490e` (`build-01113ac244d94caf`, `sha256:b98ba976…`). This is current |

If the history shows a revision after 5, re-read it before choosing: someone deployed again.

**Rollback target: revision 1.** The full teardown also reverts the patch (step 3), and revision 1
is the release that matches the reverted checkout. Revision 1 pins all six services' images by
digest: the digests in `generated/aws/values.pre-orion.json`, which ECR tags
`build-188f27662ebc4be2`. First check that each tag still resolves to the pinned digest:

```sh
for s in frontend guppy analysis proteinpaint identity seed; do
  d=$(aws ecr describe-images --repository-name mmrf-synthetic/$s --image-ids imageTag=build-188f27662ebc4be2 \
    --query 'imageDetails[0].imageDigest' --output text)
  p=$(jq -r --arg s $s '.images[$s] | split("@")[1]' generated/aws/values.pre-orion.json)
  [ "$d" = "$p" ] && echo "$s ok" || echo "$s MISMATCH: stop"; done      # six "ok"; frontend is sha256:4f1d1da85ee2…
H rollback synthetic 1 --wait --timeout 20m               # check `helm rollback --help` for the installed Helm's flags
cp generated/aws/values.pre-orion.json generated/aws/values.json   # the values file matches the release again (pre-ORION digests)
```

There is an alternative, if the fork's frontend should stay, with ORION off:
`H rollback synthetic 2 --wait --timeout 20m` (revision 2 runs the fork's stage 3 frontend at
`3fc0e6a`, not `e91490e`). Then skip the `cp`, and skip the `sources.json`
part of step 3. Never use `--reuse-values`.

Watch for the Helm 4 behaviour that failed revision 4 (`orion/infra/README.md`, "Deploy order",
step 7): the frontend Deployment can keep `serviceAccountName: orion-frontend` after the
ServiceAccount is removed, and the new frontend pod then never starts. After the rollback, run
`K rollout status deployment/frontend --timeout 5m` and
`K get deployment frontend -o jsonpath='{.spec.template.spec.serviceAccountName}'` (expect empty).
A rollback was never run, so this check is untested.

Check:

```sh
K get serviceaccount orion-frontend            # NotFound
K get networkpolicy orion-frontend-egress      # NotFound
K get deploy frontend -o json | jq '{sa: .spec.template.spec.serviceAccountName, orionEnv: [.spec.template.spec.containers[0].env[]?.name | select(startswith("ORION_"))] | length}'
                                                # sa null (the default), orionEnv 0
K get pods                                     # every pod Running, frontend restarted
python3 scripts/aws_sandbox.py status           # read-only: ALB healthy, the authenticated site check passes; prints no password
```

## 3. Revert the sandbox patch in the checkout

This step is local and git only. It changes nothing in AWS or the cluster: step 2 already did
that.

```sh
git switch -c orion/mode-a-teardown              # or work on whichever branch the user chooses
git apply -R --check orion/infra/patches/sandbox.patch && git apply -R orion/infra/patches/sandbox.patch
jq -r .frontend.commit sources.json              # 27cf09aca01a496b9d52f1b6a14e6859bf530fbb (theMMRF upstream)
git status --short                               # sources.json, helm/values.yaml, helm/templates/workloads.yaml; helm/templates/orion.yaml deleted
```

The reverse check passed on 2026-11-06 against `97926b0`, and again on `main` after stage 4 (`6a1da53`). Commit only with the user's approval,
and never push to `upstream`.

A later `aws_sandbox.py build` moves `.sources/frontend` back to the upstream pin by itself.
`fetch_sources.py` fetches `27cf09a` from `origin`, which is theMMRF and has it, then checks it
out detached. It needs a clean tree, so check that first:

```sh
git -C .sources/frontend status --porcelain --untracked-files=all    # must print nothing
```

Two side effects to know before that build:

- **The local Compose stack loses the fork too.** `orion/scripts/orion-local.sh up` builds the
  frontend from `.sources/frontend`, so once that checkout is back at `27cf09a`, the next mode L
  frontend build has no Assistant. Keep mode L on the fork by not running `aws_sandbox.py build`
  or `fetch_sources.py` until mode L is done, or by re-checking out `e91490e` (remote `fork`)
  before building mode L.
- **`generated/aws/build.json` still records the fork's build** `build-01113ac244d94caf`. An
  `aws_sandbox.py deploy --skip-build` republishes that record, which puts the fork's frontend
  back with ORION off. Use a full `aws_sandbox.py deploy` (it rebuilds) when the sandbox should
  run the upstream images again.

## 4. Apply the force flags, before the destroy plan

`force_delete` (ECR) and `force_destroy` (S3) are read from the **state** when the resource is
deleted. Passed only on the destroy plan, they do nothing, and the destroy then fails on the
non-empty repository and bucket, half done. Apply them first, with the variables of the last
apply. (These are zsh arrays: `$V`-style word splitting does not work in zsh.)

`agent_image_tag` is the live Runtime's tag: `97926b08c368` since the version 4 apply of
2026-11-06 05:36 UTC. Check it first (`aws bedrock-agentcore-control get-agent-runtime
--agent-runtime-id orion_agent-RE0ngY6qRl --query agentRuntimeArtifact.containerConfiguration.containerUri
--output text | sed 's/.*://'`); if it differs, use what it prints.

```sh
cd $INFRA
V=(-var-file=stage.tfvars -var agent_image_tag=97926b08c368 -var lambda_zip_path=../../../agent/lambda/dist/orion-vlab-tools-arm64.zip)
terraform plan -input=false "${V[@]}" | grep -E '^Plan:|No changes'     # "No changes": V matches the last apply
F=(-var ecr_force_delete=true -var bucket_force_destroy=true)
terraform plan -input=false "${V[@]}" "${F[@]}" -out=force.tfplan
terraform show -no-color force.tfplan | grep -E '^  # |must be replaced|^Plan:' | red
```

Expect `Plan: 0 to add, 6 to change, 0 to destroy.`, with exactly these six updates (a read-only
plan of this on 2026-11-06 showed them: `$S3/integrity-check/force-plan-v4tag.log`):

- `# aws_ecr_repository.agent will be updated in-place` (`force_delete` false → true)
- `# aws_s3_bucket.runs will be updated in-place` (`force_destroy` false → true)
- `# aws_iam_role_policy.bff`, `.gateway[0]` and `.runtime will be updated in-place`, and
  `# aws_s3_bucket_policy.runs will be updated in-place`, each only `policy = ... -> (known after
  apply)` (read those in the full `terraform show -no-color force.tfplan | red`)

plus four `# data.aws_iam_policy_document.<bff, gateway[0], runs_bucket, runtime> will be read
during apply` lines with "(depends on a resource or a module with changes pending)". Those four
documents read the bucket's or the repository's ARN, so a change to either defers them to the
apply, which writes the same documents again.

Anything else means the variables, the zip or the code differ from the last apply. For example,
the Runtime or the Lambda showing a change, or a policy showing a changed statement instead of
"known after apply". Stop and find out why.

```sh
terraform apply force.tfplan
cp terraform.tfstate $ROOT/generated/aws/orion-infra.tfstate.bak && chmod 600 $ROOT/generated/aws/orion-infra.tfstate.bak
```

From here on, the destroy deletes for good:

- **every image** in `orion/agent`: `pre-bb7c6e0bde78`, `prep-66ee4ff8529c`, `4609db670295` and
  `97926b08c368`;
- **every object version** in `orion-runs-<account>-us-east-1`: run records, thread ledgers and
  `gateway/vlab-tools.json`.

## 5. The secret `orion/vlab-basic`: decided, the destroy deletes it

The user created this secret and filled it by hand. Terraform adopted it with `terraform import`
before stage 1, so it is in the state now, with `recovery_window_in_days = 0`. **The destroy
therefore deletes it at once, with no recovery window.**

**Decision: let the destroy delete it.** The reasons:

- It holds only a copy of the team Basic login, for the tool Lambda, which the same destroy
  removes.
- The original stays where the sandbox keeps it: `generated/aws/access.json` (0600) and the
  cluster Secret `sandbox-access`. Nothing is lost.
- A credential copy left with no consumer is worse.

Because the user created it, the user confirms this at the plan review in step 6. The destroy plan
lists `aws_secretsmanager_secret.vlab_basic will be destroyed`.

To keep it instead, remove it from the state **before** the destroy plan:

```sh
terraform state rm aws_secretsmanager_secret.vlab_basic      # the destroy plan then shows 34, not 35
```

If kept, delete it later only by hand, with
`aws secretsmanager delete-secret --secret-id orion/vlab-basic --force-delete-without-recovery`.

## 6. Destroy orion-infra

```sh
terraform plan -destroy -input=false "${V[@]}" "${F[@]}" -out=destroy.tfplan
terraform show -no-color destroy.tfplan | grep -E '^Plan:'                       # Plan: 0 to add, 0 to change, 35 to destroy.
terraform show -no-color destroy.tfplan | grep -E '^  # .* will be destroyed' | red | sort
```

The 35 resources:

- the Runtime and its endpoint;
- the Gateway and its target;
- the Lambda;
- three log groups;
- two log deliveries, two delivery sources and two delivery destinations;
- the ECR repository and its lifecycle policy;
- the bucket, with its six configurations and the schema object;
- the secret;
- four roles and four role policies;
- the Pod Identity association and the `eks-pod-identity-agent` add-on.

Anything outside this module (any `mmrf-synthetic` resource, the cluster, CloudFront) must not
appear: stop if it does.

```sh
terraform apply destroy.tfplan
terraform state list          # must print nothing
```

If the apply fails part way, it is usually one of two causes. Fix it and re-run the same two
commands; the state shrinks each time:

- an IAM or AgentCore dependency still settling: wait a minute;
- `ExpiredToken`: refresh the credentials.

Check from AWS, read-only. Each check should find nothing:

```sh
aws bedrock-agentcore-control list-agent-runtimes --output json | grep -c orion_agent          # 0
aws bedrock-agentcore-control list-gateways --output json | grep -c orion-vlab                 # 0
aws lambda get-function --function-name orion-vlab-tools 2>&1 | grep -c ResourceNotFound      # 1
aws ecr describe-repositories --repository-names orion/agent 2>&1 | grep -c RepositoryNotFound # 1
aws s3api head-bucket --bucket "$B" 2>&1 | grep -c -E 'Not Found|404'                          # 1 (B from step 0)
aws secretsmanager describe-secret --secret-id orion/vlab-basic 2>&1 | grep -c ResourceNotFound  # 1 (unless kept in step 5)
for r in orion-runtime orion-bff orion-gateway orion-vlab-tools; do aws iam get-role --role-name $r 2>&1 | grep -c NoSuchEntity; done   # 1 each
aws eks list-pod-identity-associations --cluster-name mmrf-synthetic --output json | grep -c orion-frontend   # 0
aws eks describe-addon --cluster-name mmrf-synthetic --addon-name eks-pod-identity-agent 2>&1 | grep -c ResourceNotFound   # 1
aws logs describe-delivery-sources --output json | grep -c -E 'orion_agent-app-logs|orion-vlab-app-logs'   # 0
```

## 7. The log groups AgentCore created itself

AgentCore created these two groups on first use. Terraform never owned them, so the destroy leaves
them. Retention was never set on them (README deploy step 6 was not applied), so they hold every
Runtime stdout line since 2026-11-05: run ids, outcomes, error codes and short answer fragments.
Export them first if they should be kept.

```sh
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl \
  --query 'logGroups[].logGroupName' --output text
aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-orion_live
aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/orion_agent-RE0ngY6qRl-DEFAULT
```

Then confirm nothing ORION's is left in CloudWatch Logs:

```sh
for p in /aws/bedrock-agentcore/runtimes/orion_agent /aws/vendedlogs/bedrock-agentcore /aws/lambda/orion; do
  aws logs describe-log-groups --log-group-name-prefix $p --query 'logGroups[].logGroupName' --output text; done   # empty
```

Two leftovers to know about:

- **X-Ray traces** from the Lambda's active tracing age out by themselves. Trace delivery for the
  Runtime was off.
- **A CloudWatch Logs resource policy (unverified).** Vended log delivery may have added one to
  the account. List them with `aws logs describe-resource-policies --query
  'resourcePolicies[].policyName'`. Remove one only if it names nothing but ORION's deleted
  groups. Other deliveries may share it.

## 8. Local files

Do this only after step 6 printed an empty state and the checks passed.

These hold ORION's account-specific values, and can be removed:

- `$INFRA/stage.tfvars` (the account id);
- the plan files `$INFRA/*.tfplan` (base, full, prep, iam, final, rb, force, destroy: they hold
  ARNs);
- `generated/aws/orion-values.json`;
- the empty `terraform.tfstate` and `.backup` in `$INFRA`;
- `generated/aws/orion-infra.tfstate.bak`;
- `generated/aws/values.pre-orion.json`;
- the Lambda zip (it is now free to rebuild or delete).

**Keep:**

- `generated/aws/access.json`, `config.json` and `kubeconfig`;
- the sandbox's `terraform/` directory and its state.

They are the sandbox's, not ORION's.

## 9. The allowlist, at the end

There is no ORION entry to remove from the EKS API allowlist. Every change during mode A replaced
one /32 with another: the operator's address, as the sandbox's rule requires. Four guarded
applies were made (`mode-a-status.md`, "The sandbox side"). Leave it at the operator's current
/32, matching `operator_cidr` in `generated/aws/config.json`. Check it without printing an
address:

```sh
aws eks describe-cluster --name mmrf-synthetic --query 'length(cluster.resourcesVpcConfig.publicAccessCidrs)'   # 1
```

If the sandbox's owner wants a different address there, for example the one from before ORION,
set it as `operator_cidr` in `config.json`. Then run the same guarded plan as step 1, skipping its
checkip line. Never widen it beyond a single /32 without the owner's say.

## What NOT to run

- **`python3 scripts/aws_sandbox.py destroy`**, unless the user wants the **whole sandbox** gone.
  It empties the six `mmrf-synthetic/*` repositories and deletes EKS, CloudFront, the ALB and the
  VPC. If the user does want that, run it only after step 6:
  - the Pod Identity association and the add-on live on the cluster;
  - if the cluster is already gone, run `terraform state rm aws_eks_pod_identity_association.bff
    'aws_eks_addon.pod_identity[0]'` in `$INFRA` and destroy the rest;
  - keep `generated/aws` and the sandbox state until it succeeds.
- **`aws_sandbox.py deploy`** as a teardown or allowlist step. It republishes six images and runs
  Helm with `values.json` alone.
- **`helm uninstall synthetic`.** It removes the whole sandbox application, not just ORION.
  Also never use `helm upgrade --reuse-values`.
- **A destroy without step 4 applied first.** Also wrong: the force flags only on the destroy
  plan, or deleting or rebuilding the Lambda zip before the destroy.
- **`terraform destroy` with flags that differ from the last apply.** That includes a new
  `agent_image_tag` or a changed zip: a mismatch shows up as updates in the force plan. Never
  delete the orion-infra state before `state list` is empty.
- **Any `terraform apply` in the sandbox's `terraform/`** other than the guarded allowlist plan of
  step 1.
- **`aws_sandbox.py credentials`**, or `cat generated/aws/access.json`, in a shared terminal.
- **The account's second CloudFront distribution.** It is not in the sandbox's state; leave it
  alone.
