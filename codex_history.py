"""Local session titles, safely associated with Codex account/session IDs."""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

_lock = threading.RLock()


def clean_user_text(text):
    if not isinstance(text, str): return ''
    # Codex injects these as user-role messages, but they are not user prompts.
    for tag in ('environment_context', 'user_instructions', 'permissions_instructions'):
        text = re.sub(r'<'+tag+r'>.*?</'+tag+r'>\s*', '', text, flags=re.S | re.I)
    text = text.strip()
    if text.startswith(('# AGENTS.md instructions', '<environment_context>', '<user_instructions>', '<permissions_instructions>')):
        return ''
    return text


def path_for(home): return Path(home) / 'web-session-titles.json'


def _load(home):
    path = path_for(home)
    if not path.exists(): return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict): raise ValueError('Invalid session title store')
    return data


def titles(home):
    with _lock: return _load(home)


def _save(home, data):
    root = Path(home); root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.session-titles-', dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path_for(home))
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def key(slug, session_id): return str(slug) + ':' + str(session_id)


def rename(home, slug, session_id, title):
    if not isinstance(title, str): raise ValueError('Session name is required')
    title = ' '.join(title.split())
    if not title or len(title) > 100 or any(ord(c) < 32 for c in title):
        raise ValueError('Session name must be 1–100 characters')
    with _lock:
        data = _load(home); data[key(slug, session_id)] = title; _save(home, data)
    return title


def forget(home, slug, session_id):
    with _lock:
        data = _load(home)
        if key(slug, session_id) in data:
            del data[key(slug, session_id)]; _save(home, data)
