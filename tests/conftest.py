"""Keep test identities small without changing the values under test."""
from __future__ import annotations

import hashlib
import pytest


def pytest_make_parametrize_id(config, val, argname):
    # pytest includes node IDs in PYTEST_CURRENT_TEST. A raw 10 MiB bytes
    # parameter exceeds Windows' environment limit and floods CI/XML logs.
    # Only the display identity changes; fixtures receive the original value.
    if isinstance(val, (str, bytes)) and len(val) > 128:
        data = val.encode('utf-8', errors='surrogatepass') if isinstance(val, str) else val
        digest = hashlib.sha256(data).hexdigest()[:12]
        return f'{argname[:40]}-{type(val).__name__}-{len(val)}-{digest}'
    return None


def pytest_collection_modifyitems(items):
    # Explicit ids bypass the hook above. Reject unbounded future IDs at
    # collection, not after serializing them to the Windows environment.
    if any(len(item.nodeid) > 4096 for item in items):
        raise pytest.UsageError('Test node ID exceeds 4096 characters; provide a short explicit id.')
