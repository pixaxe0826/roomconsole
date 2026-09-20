#!/usr/bin/env python3
"""Read-only preflight; does not install or enable anything."""
import argparse, shutil, sys
from pathlib import Path
from runtime import Stop, speech_guard, assert_free
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);a=p.parse_args()
try:
    if (a.root/'VERSION').read_text().strip()not in {'0.1.4','0.1.5','0.1.6'}:raise Stop('Expected Room Hub 0.1.4/0.1.5/0.1.6')
    speech_guard(a.root);assert_free()
    if shutil.disk_usage(a.root).free<3*1024**3:raise Stop('Allow at least 3 GiB free for sources, model, build and temporary files')
    print('PREFLIGHT OK: Room Hub 0.1.4/0.1.5/0.1.6; Whisper Base/6T; 8090 free. Keep personal audio idle during the build.')
except (OSError,Stop,ValueError) as e:print('STOP:',e,file=sys.stderr);raise SystemExit(1)
