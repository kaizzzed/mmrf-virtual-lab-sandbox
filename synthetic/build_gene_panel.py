"""Select the public expression gene panel from an HGNC complete-set download.

Usage: python3 synthetic/build_gene_panel.py hgnc_complete_set.txt

Input: https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt
HGNC data are CC0. Only symbols, Ensembl gene IDs and cytogenetic locations are
kept; no expression values or patient data are read.
"""
import argparse
import csv
import hashlib
import importlib.util
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('generator', Path(__file__).with_name('generate.py'))
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
STANDARD_LOCATION = re.compile(r'^(\d+|X|Y)[pq]')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hgnc')
    args = parser.parse_args()
    with open(args.hgnc, newline='') as handle:
        rows = {row['symbol']: row for row in csv.DictReader(handle, delimiter='\t') if row['status'] == 'Approved'}
    named = list(dict.fromkeys(generator.EXPRESSION_GENES + list(generator.NAMED_EXPRESSION_GENES)))
    missing = [symbol for symbol in named if not rows.get(symbol, {}).get('ensembl_gene_id')]
    if missing:
        raise ValueError(f'Named genes missing from HGNC: {missing}')
    # The remaining genes are an arbitrary, deterministic protein-coding sample.
    pool = sorted((symbol for symbol, row in rows.items()
                   if row['locus_type'] == 'gene with protein product' and row['ensembl_gene_id']
                   and STANDARD_LOCATION.match(row['location']) and symbol not in named),
                  key=lambda symbol: hashlib.sha256(symbol.encode()).hexdigest())
    selected = named + sorted(pool[:generator.EXPRESSION_GENE_COUNT - len(named)])
    output = Path(__file__).with_name('public-genes.tsv')
    with output.open('w') as handle:
        handle.write('symbol\tensembl_gene_id\tlocation\n')
        for symbol in selected:
            handle.write(f"{symbol}\t{rows[symbol]['ensembl_gene_id']}\t{rows[symbol]['location']}\n")
    print(f'Wrote {len(selected)} genes to {output}')


if __name__ == '__main__':
    main()
