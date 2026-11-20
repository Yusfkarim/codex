"""Install local Codex skills and query Codex's own skill discovery service."""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

CATALOG = Path(__file__).resolve().parent / 'official_skills'
DEFAULTS = ('gh-fix-ci', 'security-best-practices', 'pdf')
NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def skills_dir():
    return Path(os.environ.get('HOME', str(Path.home()))) / '.agents' / 'skills'


def checked_name(name):
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
        raise ValueError('Use lowercase letters, numbers and hyphens for the skill name')
    return name


def catalog():
    if not CATALOG.is_dir(): return []
    return sorted(p.name for p in CATALOG.iterdir() if p.is_dir() and not p.is_symlink() and (p/'SKILL.md').is_file())


def install(name):
    name = checked_name(name)
    if name not in catalog(): raise ValueError('Skill not in official catalog')
    root = skills_dir(); root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.exists() or target.is_symlink(): raise FileExistsError('Skill already exists; not overwritten')
    shutil.copytree(CATALOG / name, target, symlinks=False)
    return name


def ensure_defaults():
    for name in DEFAULTS:
        if name in catalog():
            try: install(name)
            except FileExistsError: pass


def create(name, description, instructions):
    name = checked_name(name)
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError('Description is required and must be at most 1024 characters')
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 30000:
        raise ValueError('Instructions are required and must be at most 30000 characters')
    root = skills_dir(); root.mkdir(parents=True, exist_ok=True)
    target = root / name
    target.mkdir(mode=0o700)  # exclusive; never overwrite existing skills
    try:
        body = '---\nname: ' + json.dumps(name) + '\ndescription: ' + json.dumps(description.strip(), ensure_ascii=False) + '\n---\n\n' + instructions.strip() + '\n'
        (target / 'SKILL.md').write_text(body, encoding='utf-8')
    except Exception:
        target.rmdir(); raise
    return name


def discovered(cwd, codex_bin='codex', codex_home=None):
    """Use the real CLI's app-server skills/list; fail clearly, not a fake list."""
    env = dict(os.environ)
    if codex_home:
        Path(codex_home).mkdir(parents=True, exist_ok=True)
        env['CODEX_HOME'] = str(codex_home)
    proc = subprocess.Popen([codex_bin, 'app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, text=True, bufsize=1)
    lines = queue.Queue()
    def read():
        try:
            for line in proc.stdout: lines.put(line)
        finally: lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    def send(msg):
        proc.stdin.write(json.dumps(msg) + '\n'); proc.stdin.flush()
    def response(id):
        import time
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError('Codex skills/list timed out')
            try: line = lines.get(timeout=remaining)
            except queue.Empty: raise TimeoutError('Codex skills/list timed out')
            if line is None: raise RuntimeError('Codex app-server exited')
            try: msg = json.loads(line)
            except ValueError: continue
            if msg.get('id') == id:
                if msg.get('error'): raise RuntimeError('Codex app-server rejected skills request')
                return msg.get('result') or {}
    try:
        send({'id':1, 'method':'initialize', 'params':{'clientInfo':{'name':'codex-web','version':'1'}}})
        response(1)
        send({'method':'initialized','params':{}})
        send({'id':2, 'method':'skills/list','params':{'cwds':[str(cwd)],'forceReload':True}})
        result = response(2)
        items=[]; errors=[]
        for entry in result.get('data') or []:
            for s in entry.get('skills') or []:
                items.append({k:s.get(k) for k in ('name','description','scope','enabled')})
            errors.extend(str(x)[:200] for x in (entry.get('errors') or []))
        return {'skills':items, 'errors':errors, 'catalog':catalog()}
    finally:
        proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
