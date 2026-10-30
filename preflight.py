"""Read-only host and release checks for Camera Monitor recovery/deployment."""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import sys


def inspect(stage=None,manifest=None):
    result={'python':sys.version.split()[0], 'platform':sys.platform,
            'modules':{name:importlib.util.find_spec(name) is not None for name in ('cv2','numpy','yaml')},
            'commands':{name:bool(shutil.which(name)) for name in ('ffmpeg','ffprobe','rsync','nginx','systemctl')},
            'hardware_encoder':Path('/dev/dri/renderD128').exists(),
            'runtime_lock':Path('/run/camera-monitor/media.lock').is_file()}
    if stage and manifest:
        inventory=json.loads(manifest.read_text());rows=inventory['files'];failures=[]
        for row in rows:
            path=stage/row['source'];path.resolve().relative_to(stage.resolve())
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
                failures.append(row['source'])
        result['release_files']=len(rows);result['checksum_failures']=failures
        result['external_asset_failures']=[r['source'] for r in inventory.get('external_assets',[])
            if not Path(r['path']).is_file() or hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()!=r['sha256']]
    result['ready']=result['platform']=='linux' and all(result['modules'].values()) and all(result['commands'].values()) and result['hardware_encoder'] and result['runtime_lock'] and not result.get('checksum_failures') and not result.get('external_asset_failures')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--stage',type=Path);parser.add_argument('--manifest',type=Path)
    args=parser.parse_args();result=inspect(args.stage,args.manifest)
    print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['ready'] else 1)
