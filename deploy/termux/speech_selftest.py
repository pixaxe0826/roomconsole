"""Run ACTUAL installed whisper-cli through the production media/transcription path.

Run inside the dedicated PRoot environment, with Room Hub stopped (not in parallel).
By default uses upstream whisper.cpp's public English jfk.wav; it is a load/CPU/
transcription smoke test, NOT a Korean accuracy benchmark. A private Korean file
can be tested with --file; transcript is printed only with --show-text.
"""
from __future__ import annotations
import argparse, asyncio, contextlib, dataclasses, json, socket, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app.speech import SpeechConfig,WhisperRunner,TranscriptionError

async def run(args):
    with socket.socket() as s:
        s.settimeout(.5)
        if s.connect_ex(('127.0.0.1',8088))==0:
            raise ValueError('Stop Room Hub before an independent engine test, to avoid parallel CPU jobs.')
    cfg=SpeechConfig.read(ROOT/'data/speech-config.json')
    ready,reason=cfg.readiness()
    if not ready:raise ValueError(reason)
    sample=args.file is None
    source=(ROOT/'runtime/stt/whisper.cpp/samples/jfk.wav') if sample else args.file.expanduser().resolve()
    if not source.is_file():raise ValueError('Audio test file not found.')
    if sample:cfg=dataclasses.replace(cfg,language='en')
    start=time.monotonic()
    text,seconds=await WhisperRunner().transcribe(source,cfg,asyncio.Event(),ROOT/'data/speech-tmp')
    elapsed=time.monotonic()-start
    plausible=(not sample) or any(word in text.lower() for word in ['country','nation','fellow','american'])
    if not plausible:raise ValueError('Engine returned text but known public sample keywords were not found; inspect before use.')
    result={'ok':True,'engine':'actual installed whisper.cpp','model':cfg.model_name,'sample':'upstream public English jfk.wav' if sample else 'user supplied private audio (path/text omitted)', 'audio_seconds':round(seconds,3),'elapsed_seconds':round(elapsed,3),'elapsed_per_audio_second':round(elapsed/seconds,3),'output_characters':len(text),'scope':'Engine load/media/CPU/transcription smoke check. English sample is NOT a Korean accuracy test.'}
    out=ROOT/'data/speech-selftest.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');out.chmod(0o600)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.show_text:print('\nPRIVATE transcript (do not paste without reviewing):\n'+text)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--file',type=Path);p.add_argument('--show-text',action='store_true');a=p.parse_args()
    try:asyncio.run(run(a))
    except (ValueError,OSError,TranscriptionError) as e:print('SELFTEST FAILED:',str(e),file=sys.stderr);return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
