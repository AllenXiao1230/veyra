"""Build the current complete source inventory; historical manifests remain immutable."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from camera_layout import canonical

RETIRED={'/home/xuan/.config/systemd/user/camera-audio.service.d/readiness.conf':
         'Replaced by the audio supervisor; do not reinstall the old readiness override.'}
COMMON=('camera_sessions.py','camera_diagnostics.py','camera_annotations.py','camera_deployment.py','camera_layout.py')


def build(root, release):
    mapping={};known_hashes={}
    for path in sorted(root.glob('camera-monitor-*-manifest.json')):
        for row in json.loads(path.read_text(encoding='utf-8'))['files']:
            if row['path'] in RETIRED:continue
            target=canonical(row['path'])
            if target in mapping and mapping[target]!=row['source']:
                raise ValueError('Conflicting deployment mapping: '+row['path'])
            mapping[target]=row['source']
            known_hashes[target]=row['sha256']
    for source in COMMON:mapping[canonical('/opt/camera-common/'+source)]=source
    for source in ('preflight.py','release_manifest.py','RECOVERY.md','REPAIR_REPORT_RELIABILITY_20261030.md'):
        mapping[canonical('/opt/camera-monitor/'+source)]=source
    for source in ('camera-ai.service','camera-timestamp.service','camera-recorder.service'):
        mapping[canonical('/etc/systemd/system/'+source)]=source
    for source in ('camera-mediamtx.service','camera-ustreamer.service'):
        mapping[canonical('/home/xuan/.config/systemd/user/'+source)]=source
    for source in ('migrate_layout.py','LAYOUT.md'):
        mapping[canonical('/opt/camera-monitor/'+source)]=source
    files=[];assets=[]
    for target,source in sorted(mapping.items()):
        path=root/source;path.resolve().relative_to(root.resolve())
        if not path.is_file() and path.suffix=='.onnx':
            assets.append({'path':target,'sha256':known_hashes[target],'source':source});continue
        row={'source':source,'path':target,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if source.endswith('.example') or target==canonical('/etc/camera-monitor-maintenance.json') or '/config/mediamtx/' in target:
            row['preserve_existing']=True
        files.append(row)
    return {'schema':1,'release':release,'date':datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
            'kind':'complete','retired':RETIRED,'external_assets':assets,'files':files}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--release',required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parent
    args.output.write_bytes((json.dumps(build(root,args.release),indent=2,ensure_ascii=False)+'\n').encode())
