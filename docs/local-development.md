# Local development

Start the stack with `bash scripts/start.sh` and open <http://localhost:3080>.
All backing services run in Compose. The sandbox uses generated data and a
shared synthetic session; regular frontend development can continue using its
existing service configuration.

## Change a service

Edit `.sources/frontend`, `.sources/guppy`, or `.sources/analysis`, then rebuild
that service:

```sh
docker compose build frontend
docker compose up -d --wait frontend
```

The source-fetch script refuses modified checkouts. Use Compose directly while
editing, and commit durable changes in the service's own repository. Update its
revision in `sources.json` for subsequent sandbox builds.

If rebuilding exhausts Docker memory, run `docker compose stop`, build, then
`docker compose up -d --wait`. Existing volumes are retained.

## Regenerate data

```sh
docker compose down --volumes
python3 synthetic/generate.py --cases 1000 --seed 42
docker compose build
docker compose up -d --wait --wait-timeout 300
```

A changed fixture set requires a fresh search volume. Case, sample, variant,
and file identifiers stay consistent across the generated services.

## Check behavior

Run `python3 scripts/smoke.py`. For browser tests and screenshots, see
[validation](verification.md).

To compare saved cohorts in Differential Expression, click **Add group**, open
**Cohort**, and select a saved Virtual Lab cohort. Repeat for the second group,
submit, and check the expression-data counts before running the analysis.

The frontend build flag `NEXT_PUBLIC_SYNTHETIC_SANDBOX=1` enables the banner and
disables analytics. The identity gateway supplies the session and API routing;
the flag alone does not provide a running backend.

Clinical ages use years. Follow-up durations use days. Small cohorts can fall
below the existing ten-case survival threshold. Files are generated examples,
and all plotted values are synthetic.

Categorical fixtures use field-specific terms. CGS labels follow the four groups
in the [IMS/IMWG consensus](https://pubmed.ncbi.nlm.nih.gov/40489728/).
The same seed and generator version reproduce the same data. Marker assignments
are random, weighted to make High risk the smaller group, with matching risk
labels. These weights are arbitrary, not clinical prevalence estimates.
TP53 carrier status matches its CGS marker. High risk and KRAS mutations have
designed survival effects, and KRAS status changes a subset of expression genes.
CGS translocation and copy-number markers drive expression; t(4;14) cases
overexpress NSD2.
See [data generation and sources](synthetic-data.md).
