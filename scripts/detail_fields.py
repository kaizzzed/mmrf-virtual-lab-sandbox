#!/usr/bin/env python3
"""Regenerate config/detail-fields.json from the frontend's record detail queries.

Guppy rejects a whole query that names an unmapped field, so the case, gene,
and mutation pages need every queried field mapped even without fixture values.
Run after scripts/fetch_sources.py updates .sources/frontend.
"""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.sources/frontend/src'
QUERIES = (
    ('core/features/cases/caseSlice.ts', 'graphQLQuery'),
    ('core/features/biospecimen/biospecimenSlice.ts', 'biospecimenDownload'),
    ('core/genomic/geneSummary/geneSummarySlice.ts', 'geneSummary_query'),
    ('core/genomic/ssmsSummary/ssmsSummarySlice.ts', 'SSMSummaryQuery'),
    ('core/genomic/ssmsConsequenceTableSlice.ts', 'SSMSConsequenceTableGraphQLQuery'),
)
# Guppy root fields and the seeded index kind each one reads.
ROOTS = {'Case_case': 'case', 'CaseCentric_case_centric': 'case_centric',
         'GeneCentric_gene_centric': 'gene_centric', 'SsmCentric_ssm_centric': 'ssm_centric',
         'SsmCentric__aggregation.ssm_centric': 'ssm_centric'}
AGGREGATION_LEAVES = ('histogram', 'key', 'count', '_totalCount')


def leaves(query):
    """Leaf field paths of a GraphQL selection; aliases resolve to field names."""
    query = re.sub(r'\([^)]*\)', '', query)
    stack, paths, last = [], [], None
    for token in re.findall(r'[{}]|[A-Za-z_]\w*(?:\s*:\s*[A-Za-z_]\w*)?', query):
        if token == '{':
            stack.append(last)
            last = None
        elif token == '}':
            if last:
                paths.append(stack[1:] + [last])
            last = None
            stack.pop()
        else:
            if last:
                paths.append(stack[1:] + [last])
            last = token.split(':')[-1].strip()
    return paths


def main():
    fields = {}
    for path, name in QUERIES:
        query = re.search(name + r'\s*=\s*`(.*?)`', (SOURCE / path).read_text(), re.S).group(1)
        for parts in leaves(query):
            parts = [p for p in parts if p not in AGGREGATION_LEAVES]
            for root, kind in ROOTS.items():
                depth = root.count('.') + 1
                if '.'.join(parts[:depth]) == root and len(parts) > depth:
                    fields.setdefault(kind, set()).add('.'.join(parts[depth:]))
    output = ROOT / 'config/detail-fields.json'
    output.write_text(json.dumps({kind: sorted(paths) for kind, paths in sorted(fields.items())}, indent=2) + '\n')
    print(f'Wrote {sum(map(len, fields.values()))} fields to {output.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
