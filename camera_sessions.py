"""Per-file recording state and conservative recovery of interrupted sessions."""
import json
from pathlib import Path
import re
import time
from camera_state import atomic_json, media_guard

SESSION = re.compile(r'[0-9a-f]{12}')
VIDEO = re.compile(r'recording-\d{8}-\d{6}-[0-9a-f]{12}\.mp4')


def file_states(root, session, active=lambda _: False):
    root = Path(root)
    result = {}
    for name in session.get('files', []):
        if not isinstance(name, str) or not VIDEO.fullmatch(name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            result[name] = 'missing'
        elif active(name):
            result[name] = 'recording'
        else:
            result[name] = 'complete'
    return result


def reconcile_sessions(root, inspect, skip=lambda _:False, limit=None):
    """Never rewrite video or hold the media lock while ffprobe is running."""
    root = Path(root)
    issues = []
    checked=0
    for path in sorted((root / 'sessions').glob('*.json'),key=lambda p:p.stat().st_mtime):
        try:
            if path.is_symlink() or not SESSION.fullmatch(path.stem):
                continue
            signature=path.stat().st_mtime_ns
            session = json.loads(path.read_text())
            if not isinstance(session,dict) or skip(path.stem):continue
            old_states=session.get('file_states',{})
            if session.get('ended_at') and not any(v in ('unknown','missing') for v in old_states.values()):continue
            discovered=[p.name for p in root.glob(f'recording-*-{path.stem}.mp4')
                        if not p.is_symlink() and VIDEO.fullmatch(p.name)]
            recorded=session.get('files',[])
            if not isinstance(recorded,list): recorded=[]
            session['files']=sorted(set(discovered)|{n for n in recorded if isinstance(n,str) and VIDEO.fullmatch(n)})
            states = file_states(root, session)
            for name, state in list(states.items()):
                if state == 'complete':
                    check = inspect(str(root / name), active=False)
                    states[name] = 'complete' if check.get('health') == 'ok' else 'damaged' if check.get('health') == 'damaged' else 'unknown'
            session.update(state='interrupted', ended_at=session.get('ended_at') or time.time(), recovered_at=time.time(),
                           file_states=states, interruption='服務意外中止；原檔已保留並重新檢查')
            with media_guard(root):
                if skip(path.stem) or path.stat().st_mtime_ns!=signature:continue
                atomic_json(path, session)
            checked+=1
            if limit and checked>=limit:break
        except (OSError, ValueError, TypeError, AttributeError) as error:
            issues.append({'session': path.stem, 'error': type(error).__name__})
    return issues
