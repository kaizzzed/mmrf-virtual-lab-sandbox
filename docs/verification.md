# Validation

## Unit and configuration checks

```sh
npm ci
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
helm lint helm
helm template sandbox helm > /tmp/mmrf-sandbox.yaml
```

The Python suite checks fixture integrity, Helm/Compose schema parity, and
launcher account/state handling. Terraform checks use a mock provider and do not
provision resources:

```sh
cd terraform
terraform init -backend=false
terraform fmt -check
terraform validate
terraform test
```

## Saved cohorts in differential expression

Run `npm run test:cohorts` with the local stack running. The test queries KRAS
case IDs, imports both case lists through the cohort UI, selects the saved
cohorts in Differential Expression, and runs the native analysis. It saves
screenshots and request evidence in `screenshots/`.

## Running services

With the local stack running:

```sh
python3 scripts/smoke.py
npx playwright install chromium
npm run test:ui
npm run test:scenarios
```

The API and browser suites cover cohorts, Projects, repository exports/downloads,
mutation counts and survival, ProteinPaint, bulk expression, and single-cell
views. Browser tests use an isolated headless Chromium profile and save PNGs in
`screenshots/`.

The local scenario suite additionally checks all six gene tracks, reference
sequence rendering and feature bands at multiple window widths, both designed
survival comparisons, native KRAS differential expression with at least ten
significant genes, and t(4;14) differential expression with NSD2 at the
smallest p-value.

For a deployed sandbox, replace the example hostname:

```sh
python3 scripts/smoke.py --origin https://DISTRIBUTION.cloudfront.net \
  --access-file generated/aws/access.json
SANDBOX_ORIGIN=https://DISTRIBUTION.cloudfront.net \
  SANDBOX_ACCESS_FILE=generated/aws/access.json npm run test:ui
```

`python3 scripts/aws_sandbox.py status` checks pods, ALB target health, HTTPS,
anonymous-access rejection, and the authenticated case count. Deployment and
teardown also require testing in the target account; mocks cannot establish
account permissions, quotas, or network-policy enforcement.

## ProteinPaint sample and cohort checks

```sh
python3 scripts/proteinpaint_samples_smoke.py
```

The focused ProteinPaint API check verifies exact KRAS sample membership,
sample summaries, variant-circle sample lists, sample-to-case conversion, and
track reloads for large case-ID cohorts. It sends duplicate JSON content-type
headers to exercise compatibility with the pinned client. To check the UI,
open ProteinPaint, search KRAS, click the sample count and List samples, then
select samples from a variant circle and create a cohort. The saved cohort
should contain those cases and the lollipop track should still show variants.
