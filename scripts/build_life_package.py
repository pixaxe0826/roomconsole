"""Build the offline runtime patch from a reviewed 0.1.6 source baseline."""
from __future__ import annotations
import argparse,hashlib,json,shutil,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.apply_life_update import PATCH_ID,ALLOWED,source_digest

BASE_COMMIT='3b35f32c4aeeb51330c742893536745913d5c7f2'
# Only files required by the V35 runtime plus new documentation. Do not overwrite
# older local README/CI/deployment notes that need not match the full Git checkout.
RUNTIME=[p for p in ALLOWED if p=='VERSION' or p.startswith(('app/','web/','widgets/'))]
RUNTIME+=['docs/NOTES_ALARMS.md','docs/UPDATE_017.md','docs/TEST_REPORT_017.md']


def build(baseline:Path,dest:Path):
    if (baseline/'VERSION').read_text('utf-8').strip()!='0.1.6':raise ValueError('Require a source-only 0.1.6 baseline')
    if dest.exists():raise FileExistsError('Choose a new empty output directory')
    dest.mkdir(parents=True)
    manifest={'patch_id':PATCH_ID,'from_version':'0.1.6','to_version':'0.1.7','source_base_commit':BASE_COMMIT,'files':[]}
    for rel in sorted(RUNTIME):
        data=(ROOT/rel).read_bytes();old=baseline/rel
        target=dest/'files'/rel;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        manifest['files'].append({'path':rel,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
                                  'before':source_digest(old.read_bytes()) if old.exists() else None})
    (dest/'PATCH_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    shutil.copyfile(ROOT/'scripts/apply_life_update.py',dest/'apply_update.py')
    (dest/'APPLY_V35.md').write_text((ROOT/'docs/UPDATE_017.md').read_text('utf-8').replace('(NOTES_ALARMS.md)', '(FEATURES.md)').replace('(TEST_REPORT_017.md)', '(VALIDATION.md)'), encoding='utf-8')
    shutil.copyfile(ROOT/'docs/NOTES_ALARMS.md',dest/'FEATURES.md')
    shutil.copyfile(ROOT/'docs/TEST_REPORT_017.md',dest/'VALIDATION.md')
    # A serverless design preview is supplemental, never installed into V35.
    shutil.copyfile(ROOT/'previews/notes_alarms_preview.html',dest/'PREVIEW.html')
    archive=dest.parent/'room_hub_notes_alarms_v0.1.7_patch.zip'
    if archive.exists():raise FileExistsError(str(archive))
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for f in sorted(dest.rglob('*')):
            if f.is_file():
                info=zipfile.ZipInfo(f.relative_to(dest).as_posix(),date_time=(2026,9,20,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=0o100644<<16;z.writestr(info,f.read_bytes())
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n')
    return archive

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(build(a.baseline,a.output))
