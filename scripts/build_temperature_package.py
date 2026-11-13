#!/usr/bin/env python3
"""Build a reproducible source-only indoor-sensors ZIP from eight allowlisted files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile

try:
    from scripts import apply_temperature_update as installer
except ModuleNotFoundError:
    import apply_temperature_update as installer


README = """Room Hub indoor-sensors v3 complete HTTP-pull source update

This is the complete first-install patch for BOTH indoor-temperature and
indoor-air-quality. Do NOT install v1 or v2 first. Use this package alone.

Extract this ZIP into a new directory, not into /opt/room-hub.
Use Python 3.11 or newer. No pip packages are needed by this installer.

Read-only check (also the default if no mode is specified):
  python3 apply_update.py --target /opt/room-hub --check

After stopping ONLY room-hub.service and backing up its actual data directory:
  sudo python3 apply_update.py --target /opt/room-hub --apply --server-stopped

Record the printed BACKUP directory. To restore source files after stopping Room Hub:
  sudo python3 apply_update.py --target /opt/room-hub --rollback BACKUP_DIRECTORY --server-stopped

The installer preserves every existing main.py byte and inserts only the reviewed
import and route registration. Unexpected integration structure stops the update.
If an earlier patch inserted those lines, they are left unchanged. Each existing
feature file must match this v3 payload or the exact delivered v1/v2 SHA256.
Unknown edits stop the update before any source file is replaced.
It does not read or change app/store.py, SQLite, settings, keys, models or services.
It does not change the saved layout. Add indoor-temperature and indoor-air-quality
in Manager, with the SAME device_id (default room-temp-01). Configure the existing
board's http://192.168.0.22/api/sensors JSON URL and perform actual sensor testing
as separate steps in the guide. The installer itself does not fetch a sensor URL.
The server queries this shared source once per cycle, regardless of widget count.
Air quality shows dust sensor raw values, not uncalibrated PM2.5/PM10/AQI estimates.

The BACKUP from this v3 installation restores the source immediately before v3:
an upgraded v1/v2 returns to its exact prior files; a first installation removes only
the newly added files and the two main.py lines. Sensor data/configuration remain.
Keep the matching original installer if you also need to undo an earlier patch;
v1/v2 backup receipts are restored with their original installer, not this one.

Hashes detect accidental corruption. They are not a cryptographic signature.
Trust only the package reviewed for this update. This ZIP contains no firmware.
"""


def build(source, output, *, guide=None):
    source = installer.checked_root(source)
    entries = {}
    files = []
    for relative in sorted(installer.PAYLOAD_PATHS):
        blob = installer.read_source(installer.safe_path(source, relative))
        entries['payload/' + relative] = blob
        files.append({'path': relative, 'sha256': installer.digest(blob)})
    manifest = {'schema_version': 1, 'patch_id': installer.PATCH_ID,
                'main_patch': installer.MAIN_PATCH, 'files': files}
    entries['PATCH_MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()
    installer.checked_root(source / 'scripts')
    entries['apply_update.py'] = installer.read_source(source / 'scripts' / 'apply_temperature_update.py')
    entries['README.txt'] = README.encode()
    if guide is not None:
        guide = Path(guide).absolute()
        installer.checked_root(guide.parent)
        entries['ODYSSEY_GUIDE.md'] = installer.read_source(guide)
    output = Path(output).expanduser().absolute()
    installer.checked_root(output.parent, must_exist=False)
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; choose a new ZIP path')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, blob in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, blob, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output, installer.digest(output.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', required=True)
    parser.add_argument('--guide', help='Optional source Markdown guide, bundled outside the installed payload')
    args = parser.parse_args(argv)
    try:
        output, checksum = build(args.source, args.output, guide=args.guide)
        print(str(output))
        print('SHA256: ' + checksum)
    except (OSError, ValueError) as exc:
        parser.exit(1, 'STOP: ' + str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
