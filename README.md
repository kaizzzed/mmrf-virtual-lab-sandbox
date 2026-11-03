# MMRF Virtual Lab Sandbox

Run the Virtual Lab with synthetic data using Docker Compose or AWS EKS.
The stack includes the frontend, Guppy, Gen3 Analysis, ProteinPaint,
Elasticsearch, and a shared synthetic identity.

## Run locally

Requirements: Git, Python 3.9+, Bash, and Docker with Compose 2.17+.
Allow 12 GiB of Docker memory and 30 GiB of free disk. The ProteinPaint image
requires amd64 or Docker emulation.

```sh
git clone https://github.com/theMMRF/mmrf-virtual-lab-sandbox.git
cd mmrf-virtual-lab-sandbox
bash scripts/start.sh
```

Open <http://localhost:3080>. Local sessions are created automatically.

```sh
docker compose down            # Stop; retain the search index.
docker compose down --volumes  # Stop and remove the search index.
```

## Deploy to AWS

The [AWS setup instructions](docs/aws-deployment.md) cover prerequisites,
account configuration, deployment, access, and teardown.

```sh
python3 scripts/aws_sandbox.py doctor
python3 scripts/aws_sandbox.py init --profile SANDBOX_PROFILE --account-id ACCOUNT_ID
python3 scripts/aws_sandbox.py deploy
```

Replace the profile and account placeholders before running `init`.
The deployment provides a password-protected CloudFront HTTPS URL.

## Data and development

The default dataset has 1,000 cases, two projects, a six-gene mutation panel,
10,000 expression genes, and 80,000 cells. Patient records and measurements are
fabricated. Public reference proteins and hotspot labels support the gene
displays; genomic coordinates remain synthetic.

Designed scenarios give High risk and KRAS-mutated groups poorer survival,
and KRAS groups different expression profiles. Expression also follows
published myeloma patterns; for example, t(4;14) cases overexpress NSD2. These are demonstration effects,
not clinical estimates. See [data generation and sources](docs/synthetic-data.md).

- [Local development and fixture generation](docs/local-development.md)
- [API examples](docs/developer-api.md)
- [Validation](docs/verification.md)
- [Local Kubernetes](docs/local-kubernetes.md)
- [Components and licenses](docs/component-inventory.md)

Source revisions are pinned in `sources.json` and fetched into `.sources/`.
Application changes belong in their respective repositories.

## Limits and licenses

The environment uses a shared identity and disposable data. Uploads, notebooks,
and production authorization are not provided. Repository downloads are small
generated examples; mutation fixtures cover only KRAS, NRAS, TP53, BRAF, DIS3,
and MYC.

Sandbox code is Apache-2.0. Dependencies retain their own licenses, including
ProteinPaint's [academic-use license](https://github.com/stjude/proteinpaint/blob/master/LICENSE).
