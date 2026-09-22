"""Reject evaluation suite files in Git without opening runtime/private data."""
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def violations(paths):
    result = []
    for name in paths:
        p = PurePosixPath(name)
        if (p.parts and p.parts[0] == 'benchmarks' and
                (p.suffix not in {'.py', '.md'} or any(x in {'suites', 'datasets', 'results'} for x in p.parts))):
            result.append(name)
        elif any(x in {'benchmark-data', 'room-hub-benchmark-data', 'benchmark-results', 'room-hub-benchmark-results'} for x in p.parts):
            result.append(name)
        elif p.match('all_*.jsonl') or p.match('unique_tasks_registry*.json'):
            result.append(name)
    return sorted(set(result))

if __name__ == '__main__':
    proc = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    found = violations(proc.stdout.decode('utf-8').strip('\0').split('\0'))
    if found:
        print('Forbidden evaluation dataset paths: ' + ', '.join(found), file=sys.stderr)
        raise SystemExit(1)
    print('No tracked evaluation suite data; tiny generated test fixtures only.')
