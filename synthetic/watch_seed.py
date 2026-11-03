"""Keep disposable Kubernetes search storage seeded by this image.

Reseeds when storage is recreated, and replaces the sandbox indices when an
upgrade ships a different seed image or fixtures. Otherwise search would keep
the old cohort or mappings while identity and ProteinPaint serve the new ones.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

from seed_es import base, fixture_fingerprint

if base != 'http://elasticsearch:9200':
    raise SystemExit('Only the sandbox search service is supported')
expected = fixture_fingerprint()
image = os.environ.get('SEED_IMAGE', '')
SEED = [sys.executable, str(Path(__file__).with_name('seed_es.py'))]


def call(path, method='GET'):
    request = urllib.request.Request(base + path, method=method)
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
        # A new image can change mappings without changing fixtures, so reseed.
        if stored is None or (stored.get('fingerprint'), stored.get('seed_image')) != (expected, image):
            # Clear outdated or partly deleted indices so seeds never mix versions.
            # The marker goes first, so an interrupted run restarts here.
            # Synthetic, disposable indices only; the seed rebuilds all of them.
            indices = sorted((row['index'] for row in call('/_cat/indices/sandbox_*?format=json')),
                             key=lambda index: index != 'sandbox_seed_complete')
            if indices:
                print(f'Replacing {len(indices)} sandbox indices', flush=True)
            for index in indices:
                call(f'/{index}', 'DELETE')
            subprocess.run(SEED, check=True)
    except OSError:
        subprocess.run(SEED, check=True)
    time.sleep(10)
