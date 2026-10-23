"""Restore the exact public model bytes recorded for the local CPU demo."""
import argparse
import hashlib
import json
from pathlib import Path
import os
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]


def verify(path, entry):
    if path.stat().st_size != entry['bytes']: raise ValueError(f"Unexpected model size: {entry['file']}")
    with path.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest() != entry['sha256']:
            raise ValueError(f"Unexpected model SHA-256: {entry['file']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--verify-only',action='store_true'); args=parser.parse_args()
    output=args.output_dir.resolve()
    if not output.is_relative_to((ROOT/'.local').resolve()): raise ValueError('Keep model weights under .local')
    lock=json.loads((Path(__file__).parent/'model-lock-2026-10-22.json').read_text())
    output.mkdir(parents=True,exist_ok=True)
    for entry in lock['downloaded_files']:
        if Path(entry['file']).name != entry['file']: raise ValueError('Only direct model files are allowed')
        destination=output/entry['file']
        if not destination.exists():
            if args.verify_only: raise FileNotFoundError(destination)
            partial=destination.with_suffix(destination.suffix+'.partial')
            if partial.exists(): raise FileExistsError(partial)
            url=f"https://huggingface.co/{lock['repo_id']}/resolve/{lock['revision']}/{entry['file']}"
            try:
                request=Request(url,headers={'User-Agent':'RecallHandoffModelRestore/0.2'})
                with urlopen(request,timeout=120) as response, partial.open('xb') as stream:
                    if response.status!=200: raise ValueError('Model download did not return HTTP 200')
                    remaining=entry['bytes']
                    while remaining:
                        data=response.read(min(1024*1024,remaining))
                        if not data: raise ValueError('Model download ended early')
                        stream.write(data);remaining-=len(data)
                    if response.read(1): raise ValueError('Model download exceeded the recorded size')
                verify(partial,entry)
                os.link(partial,destination)
            finally: partial.unlink(missing_ok=True)
        verify(destination,entry)
    manifest=output/'download-manifest.json'
    if not manifest.exists():
        with manifest.open('x') as stream: stream.write(json.dumps(lock,indent=2)+'\n')
    else:
        existing=json.loads(manifest.read_text())
        if any(existing.get(key)!=lock[key] for key in ['repo_id','revision','downloaded_files']):
            raise ValueError('Existing model manifest conflicts with the lock')
    print(json.dumps({'status':'MODEL_FILES_VERIFIED','files':len(lock['downloaded_files']),
                      'model':lock['repo_id'],'revision':lock['revision'],'model_execution':'NOT_PERFORMED_BY_RESTORE'}))


if __name__=='__main__': main()
