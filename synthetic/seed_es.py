"""Seed an isolated Elasticsearch instance with generated fixtures only."""
import json
import os
import hashlib
import sqlite3
from pathlib import Path
import time
import urllib.error
import urllib.request
from collections import defaultdict

base = os.environ.get("GEN3_ES_ENDPOINT", "http://elasticsearch:9200").rstrip("/")
if base not in ("http://elasticsearch:9200", "http://127.0.0.1:19200"):
    raise SystemExit("Seeder only supports the isolated sandbox Elasticsearch address")
root = Path(os.environ.get("DATA_DIR", "/data"))


def fixture_fingerprint():
    """Identify the fixture version this image seeds."""
    return hashlib.sha256(b''.join((root / name).read_bytes() for name in ('cases.json', 'files.json', 'variants.json'))).hexdigest()


def request(path, data=None, method="GET", content_type="application/json"):
    raw = data.encode() if isinstance(data, str) else json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=raw, method=method, headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(exc.read().decode()) from exc
        raise


def mapping(value):
    if isinstance(value, list):
        return mapping(next((v for v in value if v is not None), ""))
    if isinstance(value, dict):
        return {"type": "nested", "properties": {k: mapping(v) for k, v in value.items()}}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "double"}
    return {"type": "keyword"}


def merge(left, right):
    for key, value in right.items():
        if key not in left:
            left[key] = value
        elif isinstance(value, dict) and isinstance(left[key], dict):
            merge(left[key], value)
    return left


def normalize_types(props):
    for key, value in props.items():
        if "properties" in value:
            normalize_types(value["properties"])
        if key in ('project', 'demographic', 'transcript', 'annotation') and 'properties' in value:
            value['type'] = 'object'
        if key == 'transcript' and 'gene' in value.get('properties', {}):
            value['properties']['gene']['type'] = 'object'
        elif key.startswith("days_to_") or key in ("age_at_diagnosis", "age_at_index", "file_size", "start_position", "end_position", "case_count", "file_count"):
            props[key] = {"type": "integer"}


def file_schema(props):
    """Expose optional public file fields even when no fixture has that metadata.

    Empty input/output/index lists stay empty, rather than inventing dangling IDs.
    Field paths come from the public frontend query contract, not source records.
    """
    paths = json.loads((Path(__file__).resolve().parents[1] / 'config/file-fields.json').read_text())
    for path in paths:
        parts = path.split('.')
        current = props
        for part in parts[:-1]:
            if 'properties' not in current.get(part, {}):
                current[part] = {'type': 'nested', 'properties': {}}
            current = current[part]['properties']
        current.setdefault(parts[-1], {'type': 'keyword'})


DETAIL_NUMBERS = {'gene_start', 'gene_end', 'aa_start', 'aa_end', 'relationship_age_at_diagnosis', 'years_smoked'}


def detail_schema(kind, props):
    """Expose fields the case, gene, and mutation detail pages query.

    Guppy rejects a whole GraphQL query that names an unmapped field, so these
    pages showed "Not Found". Unpopulated fields return null. Paths come from the
    frontend detail queries; regenerate them with scripts/detail_fields.py.
    """
    paths = json.loads((Path(__file__).resolve().parents[1] / 'config/detail-fields.json').read_text())
    for path in paths.get(kind, []):
        parts = path.split('.')
        current = props
        for part in parts[:-1]:
            if part in current and 'properties' not in current[part]:
                break  # Never retype an existing value field as an object.
            current = current.setdefault(part, {'type': 'nested', 'properties': {}})['properties']
        else:
            current.setdefault(parts[-1], {'type': 'integer' if parts[-1] in DETAIL_NUMBERS else 'keyword'})


def embedded_file_schemas(props):
    # Guppy shares nested GraphQL type names across indices. Embedded case files
    # must expose the same optional object fields as the file-centric index.
    for key, field in list(props.items()):
        children = field.get('properties')
        if children is None:
            continue
        if key == 'files' and 'file_id' in children:
            file_schema(children)
        embedded_file_schemas(children)


def arrays(doc, prefix=""):
    result = set()
    if isinstance(doc, dict):
        for key, value in doc.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(value, list):
                result.add(name)
                for item in value:
                    result.update(arrays(item, name))
            elif isinstance(value, dict):
                result.update(arrays(value, name))
    return result


def index_documents(index, docs):
    """Bound bulk request memory as the synthetic cohort grows."""
    batch, size = [], 0
    for doc in docs:
        row = json.dumps({"index": {"_index": index, "_id": doc["id"]}}) + "\n" + json.dumps(doc) + "\n"
        batch.append(row)
        size += len(row.encode())
        if size >= 5_000_000 or len(batch) >= 100:
            result = request('/_bulk', ''.join(batch), 'POST', 'application/x-ndjson')
            if result.get('errors'):
                raise RuntimeError(json.dumps(result))
            batch, size = [], 0
    if batch:
        result = request('/_bulk', ''.join(batch), 'POST', 'application/x-ndjson')
        if result.get('errors'):
            raise RuntimeError(json.dumps(result))
    request(f'/{index}/_refresh', method='POST')


if __name__ == "__main__":
    for attempt in range(60):
        try:
            request("/_cluster/health?wait_for_status=yellow&timeout=2s")
            break
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    cases = json.loads((root / "cases.json").read_text())
    files = json.loads((root / "files.json").read_text())
    variants = json.loads((root / "variants.json").read_text())
    if any(not c.get("synthetic") or not c["case_id"].startswith("SYNTH-CASE-") for c in cases):
        raise SystemExit("Refusing non-synthetic fixtures")
    # Refuse a stale index rather than silently mixing different generated cohorts.
    fingerprint = fixture_fingerprint()
    try:
        previous = request('/sandbox_seed_complete/_doc/ready')['_source']
        if previous.get('fingerprint') != fingerprint:
            raise SystemExit('Fixtures changed: discard the disposable search volume before reseeding')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    by_id = {c["case_id"]: c for c in cases}
    for case, file in zip(cases, files):
        case["files"] = [{k: v for k, v in file.items() if k != "cases"}]
        case["summary"] = {"file_count": 1, "data_categories": [{"file_count": 1, "data_category": file['data_category']}],
                           "experimental_strategies": [{"experimental_strategy": file['experimental_strategy'], "file_count": 1}]}
    for case in cases:
        case['available_variation_data'] = ['ssm', 'cnv']
        case['gene'] = []
    with sqlite3.connect(root / "genes.sqlite") as db:
        coordinates = {name: (chrom, start, stop) for name, chrom, start, stop in db.execute("SELECT name, chr, start, stop FROM gene2coord")}
    occurrences = []
    for number, v in enumerate(variants):
        chrom, start, stop = coordinates[v["symbol"]]
        # Synthetic genome coordinates; external references stay empty rather than invented.
        gene = {"gene_id": v["gene_id"], "symbol": v["symbol"], "name": "Synthetic " + v["symbol"], "biotype": "protein_coding", "is_cancer_gene_census": True,
                "gene_chromosome": chrom.removeprefix('chr'), "gene_start": start, "gene_end": stop, "gene_strand": 1,
                "external_db_ids": {"entrez_gene": [], "hgnc": [], "omim_gene": [], "uniprotkb_swissprot": []}}
        mutation = {"ssm_id": v['ssm_id'], "chromosome": v["chr"], "start_position": v["pos"],
                    "end_position": v["pos"] + len(v['ref']) - 1, "reference_allele": v["ref"], "tumor_allele": v["alt"],
                    "mutation_type": "Simple Somatic Mutation", "mutation_subtype": (
                        'SNP' if len(v['ref']) == len(v['alt']) == 1 else
                        'MNV' if len(v['ref']) == len(v['alt']) else
                        'INS' if len(v['alt']) > len(v['ref']) else 'DEL'),
                    "genomic_dna_change": f"{v['chr']}:g.{v['pos']}{v['ref']}>{v['alt']}"}
        mutation['observation'] = [{'observation_id': f'SYNTH-OBS-{number:04d}', 'sample': {'sample_id': v['sample_id']}}]
        score = (v['pos'] % 10) / 10
        annotation = {'vep_impact': 'HIGH' if v['class'] in ('N', 'F') else 'MODERATE',
                      'polyphen_score': score, 'polyphen_impact': 'possibly_damaging' if score >= .5 else 'benign',
                      'sift_score': score, 'sift_impact': 'deleterious' if score < .2 else 'tolerated'}
        mutation['consequence'] = [{'consequence_type': v['consequence'], 'transcript': {'is_canonical': 'true', 'consequence_type': v['consequence'], 'aa_change': v['mname'], 'annotation': annotation, 'gene': gene}}]
        by_id[v['case_id']]['gene'].append({**gene, 'ssm': [mutation]})
        occurrence = {"ssm_occurrence_id": f"SYNTH-OCC-{number:04d}", "case": by_id[v["case_id"]],
                      "sample": {"sample_id": v["sample_id"]}, "ssm": mutation}
        occurrences.append({"id": occurrence["ssm_occurrence_id"], "ssm_occurrence_id": occurrence["ssm_occurrence_id"],
                            **mutation, "gene": gene, "ssm": mutation, "occurrence": [occurrence],
                            "case": by_id[v["case_id"]], "consequence": mutation["consequence"],
                            "auth_resource_path": by_id[v['case_id']]['auth_resource_path']})
    genes = []
    for symbol in sorted({v["symbol"] for v in variants}):
        selected = [o for o in occurrences if o["gene"]["symbol"] == symbol]
        gene = selected[0]["gene"]
        genes.append({**gene, "id": gene["gene_id"], "ssm": [{"ssm_id": o["ssm"]["ssm_id"], "occurrence": [o["occurrence"]]} for o in selected],
                      "case": [{k: value for k, value in o["case"].items() if k != 'gene'} | {'ssm': [o['ssm']], 'cnv': [{'cnv_change_5_category': 'Gain'}]} for o in selected], "auth_resource_path": sorted({o['auth_resource_path'] for o in selected})})
    # A mutation-centric document is keyed by the mutation ID, while its separate
    # occurrence-centric counterpart retains the observation ID.
    grouped = defaultdict(list)
    for occurrence in occurrences:
        grouped[occurrence['ssm_id']].append(occurrence)
    mutations = [{**group[0], 'id': ssm_id, 'occurrence': [o['occurrence'][0] for o in group],
                  'auth_resource_path': sorted({o['auth_resource_path'] for o in group})}
                 for ssm_id, group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))]
    projects = []
    for project_id in sorted({c['project_id'] for c in cases}):
        selected_cases = [c for c in cases if c['project_id'] == project_id]
        selected_files = [f for f in files if f['project_id'] == project_id]
        summary = {'case_count': len(selected_cases), 'file_count': len(selected_files),
                   'file_size': sum(f['file_size'] for f in selected_files)}
        for key, field in [('data_categories', 'data_category'), ('experimental_strategies', 'experimental_strategy')]:
            summary[key] = [{field: value, 'file_count': sum(f[field] == value for f in selected_files),
                             'case_count': len({f['cases'][0]['case_id'] for f in selected_files if f[field] == value})}
                            for value in sorted({f[field] for f in selected_files})]
        projects.append({**selected_cases[0]['project'], 'id': project_id, 'summary': summary,
                         'case_count': len(selected_cases), 'file_count': len(selected_files),
                         'auth_resource_path': selected_cases[0]['auth_resource_path']})
    documents = {
        "case": cases, "case_centric": cases, "file": files,
        "project": projects,
        "gene_centric": genes, "ssm_centric": mutations, "ssm_occurrence_centric": occurrences,
        "cnv_centric": [{"id": "SYNTH-CNV-0001", "cnv_id": "SYNTH-CNV-0001", "gene": {k: genes[0][k] for k in ('gene_id', 'symbol')}, "cnv": {"cnv_change": "Gain"}, "case": cases[0]}],
        "cnv_occurrence_centric": [{"id": "SYNTH-CNV-OCC-0001", "cnv_occurrence_id": "SYNTH-CNV-OCC-0001", "gene": {k: genes[0][k] for k in ('gene_id', 'symbol')}, "cnv": {"cnv_change": "Gain"}, "case": cases[0]}],
    }
    config = {"indices": [], "auth_filter_field": "auth_resource_path", "arrayConfig": []}
    for kind, docs in documents.items():
        props = {}
        array_fields = set()
        for doc in docs:
            merge(props, mapping(doc)["properties"])
            array_fields.update(arrays(doc))
        normalize_types(props)
        if kind == 'file':
            file_schema(props)
        embedded_file_schemas(props)
        detail_schema(kind, props)
        normalize_types(props)
        if kind == 'gene_centric':
            props['case']['properties']['project']['type'] = 'object'
        if kind in ('ssm_centric', 'ssm_occurrence_centric'):
            props['consequence']['properties']['transcript']['type'] = 'object'
            props['consequence']['properties']['transcript']['properties']['annotation']['type'] = 'object'
            props['occurrence']['properties']['case']['type'] = 'object'
            props['occurrence']['properties']['case']['properties']['project']['type'] = 'object'
        index = f"sandbox_{kind}"
        try:
            request(f"/{index}")
            request(f"/{index}/_mapping", {"properties": props}, 'PUT')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            request(f"/{index}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0, "mapping.total_fields.limit": 5000, "mapping.nested_fields.limit": 200, "mapping.nested_objects.limit": 100000}, "mappings": {"properties": props}}, "PUT")
        index_documents(index, docs)
        config["indices"].append({"index": index, "type": kind})
        config["arrayConfig"].append({"index": index, "array": sorted(array_fields)})
        print(f"Seeded {len(docs)} synthetic {kind} records", flush=True)
    (root / "guppy.json").write_text(json.dumps(config, indent=2) + "\n")
    try:
        request('/sandbox_seed_complete')
    except urllib.error.HTTPError:
        request('/sandbox_seed_complete', {}, 'PUT')
    request('/sandbox_seed_complete/_doc/ready?refresh=true', {'synthetic': True, 'fingerprint': fingerprint, 'seed_image': os.environ.get('SEED_IMAGE', '')}, 'PUT')
