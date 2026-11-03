"""Keep disposable Kubernetes search storage on this image's fixture version.

Reseeds when storage is recreated, and replaces the sandbox indices when an
upgrade ships different fixtures. Otherwise search would keep the old cohort
while identity and ProteinPaint serve the new one.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from seed_es import base, fixture_fingerprint

if base != 'http://elasticsearch:9200':
    raise SystemExit('Only the sandbox search service is supported')
expected = fixture_fingerprint()
image = os.environ.get('SEED_IMAGE', '')


def call(path, method='GET', data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(base + path, data=body, method=method, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def marker():
    """The completed seed's record, or None when no seed has completed."""
    try:
        return call('/sandbox_seed_complete/_doc/ready')['_source']
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


while True:
    try:
        stored = marker()
        if stored is not None and stored.get('fingerprint') != expected:
            # Synthetic, disposable indices only; the seed rebuilds all of them.
            indices = [row['index'] for row in call('/_cat/indices/sandbox_*?format=json')]
            print(f'Fixtures changed: replacing {len(indices)} sandbox indices', flush=True)
            for index in indices:
                call(f'/{index}', 'DELETE')
            stored = None
        if stored is None:
            subprocess.run(['python', 'seed_es.py'], check=True)
        elif stored.get('seed_image') != image:
            # Same fixtures from a rebuilt image: record it so dependents start.
            call('/sandbox_seed_complete/_doc/ready?refresh=true', 'PUT', {**stored, 'seed_image': image})
    except OSError:
        subprocess.run(['python', 'seed_es.py'], check=True)
    time.sleep(10)
