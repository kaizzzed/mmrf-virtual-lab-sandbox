"""Exercise real sandbox APIs and cross-service synthetic ID consistency."""
import argparse
import gzip
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import sqlite3
from sandbox_connection import connection

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--origin', default='http://127.0.0.1:3080')
parser.add_argument('--access-file', help='Private access JSON created by the AWS launcher')
args = parser.parse_args()
access_headers, opener = connection(args.origin, args.access_file)
root = Path(__file__).resolve().parents[1]
cases = json.loads((root / 'generated/cases.json').read_text())
variants = json.loads((root / 'generated/variants.json').read_text())


def call(path, payload=None, raw=False):
    req = urllib.request.Request(args.origin + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Content-Type': 'application/json', **access_headers})
    try:
        with opener.open(req, timeout=90) as response:
            assert 'Set-Cookie' not in response.headers, 'Sandbox must not replace localhost login cookies'
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise AssertionError(f'{path}: HTTP {exc.code}: {exc.read().decode()[:1500]}') from exc
    if raw:
        return data
    value = json.loads(data)
    if isinstance(value, dict):
        assert not value.get('error') and not value.get('errors'), (path, value)
    return value


for attempt in range(90):
    try:
        status = call('/sandbox/status')
        break
    except (OSError, AssertionError):
        if attempt == 89:
            raise
        time.sleep(2)
assert status['synthetic'] and status['cases'] == len(cases)
assert call('/api/auth/sessionToken')['status'] == 'issued'
print('PASS: credential-free frontend session')

female = [c for c in cases if c['gender'] == 'female']
cohort_filter = {'in': {'demographic.gender': ['female']}}
query = '''query($filter:JSON) {
  CaseCentric_case_centric(first:1000,filter:$filter) { case_id samples { sample_id } }
  CaseCentric__aggregation {case_centric(filter:$filter) {_totalCount}}
}'''
result = call('/guppy/graphql', {'query': query, 'variables': {'filter': cohort_filter}})['data']
assert result['CaseCentric__aggregation']['case_centric']['_totalCount'] == len(female)
assert {c['case_id'] for c in result['CaseCentric_case_centric']} == {c['case_id'] for c in female}
print(f'PASS: real Guppy cohort filtering ({len(cases)} -> {len(female)})')

query = '{ File_file(first:1000) { file_id cases { case_id } } }'
files = call('/guppy/graphql', {'query': query})['data']['File_file']
assert len(files) == len(cases)
assert all(f['file_id'].startswith('SYNTH-FILE-') and f['cases'][0]['case_id'].startswith('SYNTH-CASE-') for f in files)
sequence = call('/sandbox/files/SYNTH-FILE-0001', raw=True).decode().splitlines()
assert sequence[0].startswith('>SYNTH-SAMPLE-0001') and set(sequence[1]) <= set('ACGT')
print('PASS: synthetic repository metadata and FASTA download')
fixture_files = json.loads((root / 'generated/files.json').read_text())
projects = call('/guppy/graphql', {'query': '''{Project_project{project_id name program{name program_id dbgap_accession_number} dbgap_accession_number disease_type primary_site summary{case_count file_count file_size data_categories{data_category case_count file_count} experimental_strategies{experimental_strategy case_count file_count}}}}'''})['data']['Project_project']
assert len(projects) == 2
for project in projects:
    expected_files = [f for f in fixture_files if f['project_id'] == project['project_id']]
    assert project['summary']['file_count'] == len(expected_files)
    assert project['summary']['case_count'] == sum(c['project_id'] == project['project_id'] for c in cases)
    assert project['summary']['file_size'] == sum(f['file_size'] for f in expected_files)
print('PASS: two navigable project query contracts with exact synthetic summaries')
for file_format in {f['data_format'] for f in fixture_files}:
    selected_files = [f for f in fixture_files if f['data_format'] == file_format]
    sample = selected_files[0]
    downloaded = call('/user/data/download/' + sample['file_id'] + '?redirect=true', raw=True)
    assert downloaded == (root / 'generated' / sample['file_name']).read_bytes()
    exported = call('/analysis/v0/cohorts/download', {'index': 'file', 'case_ids_filter_path': 'cases.case_id',
        'cohort_filter': {}, 'filter': {'in': {'data_format': [file_format]}}, 'accessibility': 'accessible',
        'fields': ['file_id', 'file_name', 'file_size', 'md5sum', 'cases.case_id']})
    assert {f['file_id'] for f in exported} == {f['file_id'] for f in selected_files}
print('PASS: varied file facets, exact filtered manifest source and local download bytes for every format')

health = call('/protein-paint/healthcheck')
assert health['dsSummary']['failed'] == 0 and health['dsSummary']['done'] == 1
pp = {'genome': 'hg38', 'dslabel': 'MMRF', 'filter0': cohort_filter}
samples = call('/protein-paint/termdb/singlecellSamples', pp)['samples']
assert {s['sample'] for s in samples if s['sample'] != 'combined'} == {c['sample_id'] for c in female}
assert any(s['sample'] == 'combined' for s in samples)
all_samples = call('/protein-paint/termdb/singlecellSamples?' + urllib.parse.urlencode({'genome': 'hg38', 'dslabel': 'MMRF', 'filter0': '{}'}))['samples']
assert {s['sample'] for s in all_samples if s['sample'] != 'combined'} == {c['sample_id'] for c in cases}
converted = call('/protein-paint/termdb', {**pp, 'for': 'convertSampleId', 'inputs': [c['sample_id'] for c in female]})['mapping']
assert set(converted.values()) == {c['case_id'] for c in female}
genes = call('/protein-paint/termdb/topMutatedGenes', pp)['genes']
expected = {}
for variant in variants:
    if variant['case_id'] in {c['case_id'] for c in female}:
        expected.setdefault(variant['symbol'], set()).add(variant['sample_id'])
assert {g['gene']: g['mutationStat'][0]['count'] for g in genes} == {g: len(s) for g, s in expected.items()}
print('PASS: ProteinPaint cohort, sample-to-case conversion, and mutation counts match Gen3')
chart = call('/analysis/v0/genomic/gene_frequency_chart', {'cohort_filter': cohort_filter})
table = call('/analysis/v0/genomic/gene_table', {'cohort_filter': cohort_filter})
assert {g['symbol']: g['numCases'] for g in chart['data']} == {g: len(v) for g, v in expected.items()}
assert {g['symbol']: g['case_count'] for g in table['genes']} == {g: len(v) for g, v in expected.items()}
for path, items, count_field in [('gene_frequency_chart', 'data', 'numCases'), ('gene_table', 'genes', 'case_count')]:
    filtered = call('/analysis/v0/genomic/' + path, {'cohort_filter': cohort_filter, 'gene_filter': {'in': {'is_cancer_gene_census': ['true']}}})
    assert {g['symbol']: g[count_field] for g in filtered[items]} == {g: len(v) for g, v in expected.items()}
assert all(g['ssm_count'] > 0 for g in table['genes'])

mutations = call('/analysis/v0/genomic/ssm_table', {'cohort_filter': cohort_filter})
female_ids = {c['case_id'] for c in female}
assert mutations['ssmsTotal'] == len({v['ssm_id'] for v in variants if v['case_id'] in female_ids})
for mutation in mutations['ssms']:
    members = {v['case_id'] for v in variants if v['ssm_id'] == mutation['ssm_id'] and v['case_id'] in female_ids}
    assert mutation['score'] == len(members), (mutation['ssm_id'], mutation['score'], len(members))
for selected_filter in [{}, cohort_filter]:
    selected_ids = {c['case_id'] for c in cases if not selected_filter or c['gender'] == 'female'}
    for mutation_filter in [{}, {'in': {'biotype': ['protein_coding']}}]:
        result = call('/analysis/v0/genomic/ssm_table', {'cohort_filter': selected_filter, 'ssm_filter': mutation_filter})
        for mutation in result['ssms']:
            expected_count = len({v['case_id'] for v in variants if v['ssm_id'] == mutation['ssm_id'] and v['case_id'] in selected_ids})
            assert mutation['score'] == expected_count, (selected_filter, mutation_filter, mutation, expected_count)
print('PASS: real analysis gene/mutation totals match independently generated cohort variants')


seq = call('/protein-paint/ntseq', {'genome': 'hg38', 'coord': 'chr17:30000-30020'})['seq']
assert len(seq) == 21 and set(seq) <= set('ACGT')
meta_query = {**pp,
    'singleCellPlot': {'name': 'Meta analysis', 'sample': {'sID': 'combined', 'isMetaResult': True}},
    'canvasSettings': {'cutoff': 1000, 'width': 400, 'height': 400, 'radius': 2, 'devicePixelRatio': 1, 'startColor': '#d3d3d3', 'stopColor': '#ff0000'}}
single = call('/protein-paint/termdb/singleCellPlots', meta_query)
assert single['result']['Default']['totalSampleCount'] == len(female) * 80
assert single['result']['Default']['src']
colored = call('/protein-paint/termdb/singleCellPlots', {**meta_query, 'colorTW': {'$id': 'synthetic_myc',
    'term': {'type': 'singleCellGeneExpression', 'name': 'MYC', 'gene': 'MYC', 'sample': {'sID': 'combined', 'isMetaResult': True}, 'plot': 'Meta analysis'},
    'q': {'mode': 'continuous'}}})
assert colored['result']['Default']['totalSampleCount'] == len(female) * 80
assert colored['result']['Default']['src']
scatter = call('/protein-paint/termdb/sampleScatter', {**pp, 'plotName': 'Bulk RNA-seq UMAP'})
assert {s['sample'] for group in scatter['result'].values() for s in group['samples']} == {c['sample_id'] for c in female}
empty = call('/protein-paint/termdb/sampleScatter', {**pp, 'filter0': {'in': {'case_id': ['SYNTH-CASE-NO-MATCH']}}, 'plotName': 'Bulk RNA-seq UMAP'})
assert not any(group['samples'] for group in empty['result'].values())
config = call('/protein-paint/termdb/config?genome=hg38&dslabel=MMRF')['termdbConfig']
assert set(config['supportedChartTypes']['']) == {'summaryInput', 'survival', 'DA'}
for alias in ('MYC', 'c-Myc', 'ENSG00000136997', 'ENST00000621592', 'NM_002467'):
    lookup = call('/protein-paint/genelookup', {'genome': 'hg38', 'input': alias, 'deep': True})
    assert lookup['gmlst'][0]['name'] == 'MYC', alias
print('PASS: synthetic reference, filtered meta-analysis UMAP, bulk UMAP, curated chart menu and MYC aliases')

survival = call('/analysis/v0/survival/', {'filters': [cohort_filter]})
assert survival['results']
assert {d['id'] for d in survival['results'][0]['donors']} == {c['case_id'] for c in female}
print('PASS: real analysis service returns survival for the selected synthetic cohort')
female_case_ids = {c['case_id'] for c in female}
for symbol, mutated_samples in expected.items():
    mutated_case_ids = {v['case_id'] for v in variants if v['symbol'] == symbol and v['case_id'] in female_case_ids}
    comparison = call('/analysis/v0/survival/compare_genomic', {'case_filter': cohort_filter, 'filter': {}, 'symbol': symbol, 'type': 'gene'})
    assert len(comparison['results']) == 2, (symbol, comparison)
    assert {d['id'] for d in comparison['results'][0]['donors']} == female_case_ids - mutated_case_ids
    assert {d['id'] for d in comparison['results'][1]['donors']} == mutated_case_ids
print('PASS: gene-survival curves include the exact mutated and unmutated cases for all six fixture genes')
hotspot = max(mutations['ssms'], key=lambda m: m['score'])
carriers = {v['case_id'] for v in variants if v['ssm_id'] == hotspot['ssm_id'] and v['case_id'] in female_case_ids}
comparison = call('/analysis/v0/survival/compare_genomic', {'case_filter': cohort_filter, 'filter': {}, 'symbol': hotspot['ssm_id'], 'type': 'ssm'})
assert {d['id'] for d in comparison['results'][0]['donors']} == female_case_ids - carriers
assert {d['id'] for d in comparison['results'][1]['donors']} == carriers
assert len(carriers) >= 10
print('PASS: recurring mutation-level survival includes exact carriers and noncarriers')
for expression, selected in [
    ({'>=': {'demographic.days_to_death': 1000}}, [c for c in cases if c['days_to_death'] is not None and c['days_to_death'] >= 1000]),
    ({'in': {'files.data_format': ['FASTA']}}, [c for c in cases if any(f['data_format'] == 'FASTA' for f in c['files'])]),
]:
    result = call('/guppy/graphql', {'query': query if 'CaseCentric' in query else 'query($filter:JSON){CaseCentric_case_centric(first:1000,filter:$filter){case_id}}', 'variables': {'filter': expression}})
    assert {c['case_id'] for c in result['data']['CaseCentric_case_centric']} == {c['case_id'] for c in selected}
    result = call('/protein-paint/termdb/singlecellSamples', {'genome': 'hg38', 'dslabel': 'MMRF', 'filter0': expression})
    assert {s['sample'] for s in result['samples'] if s['sample'] != 'combined'} == {c['sample_id'] for c in selected}
print('PASS: nullable clinical and file-facet cohorts match across real Guppy and ProteinPaint')

if len(cases) >= 1000:
    demographic_groups = []
    for gender, race in [('male', 'white'), ('female', 'black or african american')]:
        selection = {'and': [{'in': {'demographic.gender': [gender]}}, {'in': {'demographic.race': [race]}}]}
        expected_cases = {c['case_id'] for c in cases if c['gender'] == gender and c['race'] == race}
        curve = call('/analysis/v0/survival/', {'filters': [selection]})['results'][0]
        assert len(expected_cases) >= 100
        assert {d['id'] for d in curve['donors']} == expected_cases
        demographic_groups.append(selection)
    compared = call('/analysis/v0/survival/', {'filters': demographic_groups})
    assert len(compared['results']) == 2 and all(len(curve['donors']) >= 100 for curve in compared['results'])
    print('PASS: two-filter demographic cohorts retain >=100 survival cases per curve')

# Diagnosis age is an API value in years; use exactly matching half-open bins.
age_field = 'diagnoses.age_at_diagnosis'
comparison = call('/analysis/v0/compare/facets', {
    'doc_type': 'case_centric', 'cohort1': {}, 'cohort2': {},
    'facets': [age_field], 'interval': {age_field: 10},
})
for bucket in comparison['cohort1']['facets'][age_field]['buckets']:
    lower, upper = bucket['key']
    expected = {c['case_id'] for c in cases if lower <= c['diagnoses'][0]['age_at_diagnosis'] < upper}
    assert bucket['count'] == len(expected)
    data = call('/guppy/graphql', {
        'query': 'query($filter:JSON){CaseCentric_case_centric(first:1000,filter:$filter){case_id}}',
        'variables': {'filter': {'and': [{'>=': {age_field: lower}}, {'<': {age_field: upper}}]}},
    })['data']['CaseCentric_case_centric']
    assert {c['case_id'] for c in data} == expected
print('PASS: ten-year age comparison buckets and cohort filters return identical cases')

groups = [{'name': gender, 'values': [{'sampleId': i + 1} for i, c in enumerate(cases) if c['gender'] == gender]} for gender in ('female', 'male')]
de = {'genome': 'hg38', 'dslabel': 'MMRF', 'samplelst': {'groups': groups}, 'method': 'wilcoxon',
      'min_count': 10, 'min_total_count': 15, 'cpm_cutoff': 0,
      'volcanoRender': {'pixelWidth': 600, 'pixelHeight': 400, 'significanceThresholds': {'pValueCutoff': 1.3, 'pValueType': 'adjusted', 'foldChangeCutoff': 1}}}
assert call('/protein-paint/termdb/DE', {**de, 'preAnalysis': True})['data'] == {'female': len(female), 'male': len(cases) - len(female)}
result = call('/protein-paint/termdb/DE', de)
with gzip.open(root / 'generated/counts.tsv.gz', 'rt') as handle:
    panel = sum(1 for _ in handle) - 1
with sqlite3.connect(root / 'generated/genes.sqlite') as db:
    modelled = db.execute('select count(*) from genes').fetchone()[0]
# The count filter removes barely expressed genes from the 10,000-gene panel.
assert panel / 2 < result['data']['totalRows'] <= panel and result['data']['volcanoPng']
pseudobulk = call('/protein-paint/termdb/DE', {**de, 'pseudobulk': {'assay': 'geneExpression', 'memberId': 'Cell Type', 'category': 'Plasma'}})
assert pseudobulk['data']['totalRows'] == modelled and pseudobulk['data']['volcanoPng']
print('PASS: actual bulk and single-cell pseudobulk differential expression and volcano rendering')

# Fold change is log2(group 2 / group 1); Absent first makes NSD2 positive.
translocated = ['t(4;14): Present' in c['cgs_risk_key_criteria']['other_criteria'] for c in cases]
groups = [{'name': name, 'values': [{'sampleId': i + 1} for i, present in enumerate(translocated) if present == wanted]}
          for name, wanted in (('Absent', False), ('Present', True))]
dots = call('/protein-paint/termdb/DE', {**de, 'samplelst': {'groups': groups}})['data']['dots']
genes = {dot['gene_name']: dot for dot in dots}
# Wilcoxon p-values saturate, so several genes can share the smallest value.
smallest = min(dot['adjusted_p_value'] for dot in dots)
assert genes['NSD2']['fold_change'] > 2 and genes['NSD2']['adjusted_p_value'] <= smallest * 1.01, genes.get('NSD2')
assert genes['FGFR3']['fold_change'] > 3 and genes['FGFR3']['adjusted_p_value'] < 1e-10, genes.get('FGFR3')
print(f"PASS: t(4;14) differential expression gives NSD2 the smallest p-value "
      f"(log2FC {genes['NSD2']['fold_change']:.2f}) and raises FGFR3")
print('Sandbox integration smoke checks passed')
