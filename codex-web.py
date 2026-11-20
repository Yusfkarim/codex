#!/usr/bin/env python3
"""codex-web.py — web host for the Codex CLI.

Mobile-friendly browser front end for Codex: streaming chat, model picker,
workspace browser, file viewer, and session list with resume.
Everything runs through the local `codex` binary, so the ChatGPT session
already installed on this machine is what backs it.

    python3 codex-web.py                 # serve on :8098
    PORT=9000 LINK_TOKEN=secret python3 codex-web.py
"""
import json
import os
import pathlib
import re
import secrets
import sys
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import codex_accounts as accounts
import codex_skills as skills
import codex_history as history
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
TOKEN = os.environ.get("LINK_TOKEN") or secrets.token_urlsafe(12)
PORT = int(os.environ.get("PORT", "8098"))
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", str(pathlib.Path.home()))
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEVICE_URL = "https://auth.openai.com/codex/device"
MAX_FILE_BYTES = 400_000
SKIP_DIRS = {"/proc", "/sys", "/dev", "/run", "/snap"}

QUEUE_RETRY_SECONDS = int(os.environ.get("CODEX_QUEUE_RETRY", "300"))
QUEUE_MAX_RETRIES = int(os.environ.get("CODEX_QUEUE_MAX", "12"))
MAX_ROTATIONS = int(os.environ.get("CODEX_MAX_ROTATIONS", "8"))
JOBS = {}
JOB_LOCK = threading.Lock()
LOGINS = {}
LOGIN_LOCK = threading.Lock()


def job_new(cmd, cwd, model, prompt):
    job_id = secrets.token_hex(6)
    with JOB_LOCK:
        JOBS[job_id] = {
            "id": job_id, "cmd": cmd, "cwd": cwd, "model": model, "prompt": prompt,
            "started": time.time(), "proc": None, "state": "running", "thread_id": None,
        }
    return JOBS[job_id]


def job_get(job_id):
    with JOB_LOCK:
        return JOBS.get(job_id)


def job_drop(job_id):
    with JOB_LOCK:
        JOBS.pop(job_id, None)


# ------------------------------------------------------------------- helpers --

def sse(event, data):
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


def list_models():
    models = []
    try:
        out = subprocess.run(
            ["python3", str(pathlib.Path(__file__).with_name("codex_chat.py")), "--list-models"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
        )
        models = [m.strip() for m in out.stdout.splitlines() if m.strip()]
    except Exception:
        pass
    if not models:
        for slug in accounts.load_index():
            try:
                models = accounts.models(slug)
            except Exception:
                continue
            if models: break
    if not models:
        # Original project defaults. Actual account model catalog, when
        # available, takes precedence; defaults do not guarantee access.
        models = ["gpt-6-luna", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-reserve"]
    return models


def safe_workspace(path):
    root = pathlib.Path(DEFAULT_CWD).resolve()
    path = pathlib.Path(path or root).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("outside workspace")
    return str(path)


def list_dir(path):
    path = safe_workspace(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "path": entry.path,
                "dir": entry.is_dir(follow_symlinks=False),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "hidden": entry.name.startswith("."),
            })
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path, "parent": os.path.dirname(path) if path != str(pathlib.Path(DEFAULT_CWD).resolve()) else None,
            "entries": entries[:800]}


def read_file(path):
    path = safe_workspace(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        return {"path": path, "size": size, "truncated": True, "binary": True,
                "content": "", "head": head.decode("utf-8", "replace")}
    data = open(path, "rb").read()
    if b"\x00" in data[:4096]:
        return {"path": path, "size": size, "truncated": False, "binary": True, "content": ""}
    return {"path": path, "size": size, "truncated": False, "binary": False,
            "content": data.decode("utf-8", "replace")}


def fetch_limits():
    try:
        auth = json.loads((CODEX_HOME / "auth.json").read_text())
    except (OSError, ValueError):
        # The web app normally signs in to per-account homes, not the main home.
        index = accounts.load_index()
        current = accounts.current_account()
        slugs = ([current] if current in index else []) + [x for x in index if x != current]
        for slug in slugs:
            if accounts.valid_slug(slug) and (accounts.account_home(slug) / "auth.json").exists():
                data = accounts.usage(slug)
                if data.get("reason"): return {"error": data["reason"]}
                return data
        return {"error": "not logged in"}
    tokens = auth.get("tokens", {})
    tok = tokens.get("access_token")
    if not tok:
        return {"error": "not logged in"}
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage?client_version=0.167.2")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("chatgpt-account-id", tokens.get("account_id", ""))
    req.add_header("User-Agent", "codex_cli_rs/0.167.2")
    try:
        usage = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception as exc:
        return {"error": str(exc)}
    rl = usage.get("rate_limit", {})
    windows = {}
    for name in ("primary_window", "secondary_window"):
        w = rl.get(name)
        if w:
            windows[name] = {
                "used_percent": w.get("used_percent"),
                "window_seconds": w.get("limit_window_seconds"),
                "resets_at": w.get("reset_at"),
            }
    return {
        "email": usage.get("email"),
        "plan": usage.get("plan_type"),
        "allowed": rl.get("allowed"),
        "limit_reached": rl.get("limit_reached"),
        "windows": windows,
        "promo": (usage.get("promo") or {}).get("message"),
        "unlimited": (usage.get("credits") or {}).get("unlimited"),
    }


def list_sessions(limit=60, home=None, slug="main"):
    root = (home or CODEX_HOME) / "sessions"
    sessions = []
    try: names = history.titles(CODEX_HOME)
    except (OSError, ValueError): names = {}
    if not root.exists():
        return sessions
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        meta, first_user = {}, ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "session_meta":
                        meta = rec.get("payload", {})
                    elif rec.get("type") == "response_item" and not first_user:
                        payload = rec.get("payload", {})
                        if payload.get("type") == "message" and payload.get("role") == "user":
                            content = payload.get("content", [])
                            for part in content:
                                if isinstance(part, dict) and part.get("text"):
                                    first_user = history.clean_user_text(part["text"])
                                    if first_user: break
                            if first_user:
                                break
                    if meta and first_user:
                        break
        except OSError:
            continue
        try: modified = path.stat().st_mtime
        except OSError: continue
        sessions.append({
            "account": slug,
            "id": meta.get("session_id") or meta.get("id") or path.stem,
            "cwd": meta.get("cwd", ""),
            "time": meta.get("timestamp", ""),
            "model": meta.get("model", ""),
            "originator": meta.get("originator", ""),
            "preview": (first_user or "").replace("\n", " ")[:160],
            "title": names.get(history.key(slug, meta.get("session_id") or meta.get("id") or path.stem), ""),
            "modified": modified,
        })
    return sessions


def read_session(slug, session_id):
    """Read conversation records from a known account's Codex session.

    Do not accept paths or access arbitrary files; session identity is checked
    against metadata and a filename inside that account's sessions directory.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id or ""):
        raise ValueError("invalid session id")
    if slug == "main":
        home = CODEX_HOME
    elif accounts.valid_slug(slug) and slug in accounts.load_index():
        home = accounts.account_home(slug)
    else:
        raise FileNotFoundError("account not found")
    info = next((x for x in list_sessions(limit=10000, home=home, slug=slug)
                 if x["id"] == session_id), None)
    if not info:
        raise FileNotFoundError("session not found")
    root = (home / "sessions").resolve()
    candidates = [p for p in root.rglob("*.jsonl")
                  if p.name.endswith(session_id + ".jsonl") and p.resolve().is_relative_to(root)]
    if not candidates:
        raise FileNotFoundError("session file not found")
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    messages, fallback = [], []
    size = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            role, text = None, ""
            if record.get("type") == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                content = payload.get("content") or []
                if isinstance(content, str): text = content
                elif isinstance(content, list):
                    text = "\n".join(part.get("text", "") for part in content
                                      if isinstance(part, dict) and isinstance(part.get("text"), str))
            elif record.get("type") == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
                role = "user" if payload.get("type") == "user_message" else "assistant"
                text = payload.get("message") or ""
                if not isinstance(text, str): text = ""
                if text.strip(): fallback.append({"role": role, "text": text[:100000]})
                continue
            if role not in ("user", "assistant") or not text.strip(): continue
            if role == "user":
                text = history.clean_user_text(text)
                if not text: continue
            text = text[:100000]
            size += len(text)
            messages.append({"role": role, "text": text})
            if size > 2000000: break
    info["messages"] = (messages or fallback)[-500:]
    return info


def session_files(slug, session_id):
    """Find exact local rollout files, confined to a registered account."""
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid session ID")
    if slug == "main":
        home = CODEX_HOME
    elif isinstance(slug, str) and accounts.valid_slug(slug) and slug in accounts.load_index():
        home = accounts.account_home(slug)
        if not home.resolve().is_relative_to(accounts.ACCOUNTS_DIR.resolve()):
            raise PermissionError("Invalid account location")
    else:
        raise FileNotFoundError("Account not found")
    root = (home / "sessions").resolve()
    if not root.is_relative_to(home.resolve()): raise PermissionError("Invalid sessions location")
    result = []
    if root.is_dir():
        for file in root.rglob("*.jsonl"):
            if file.is_symlink() or not file.name.endswith(session_id + ".jsonl"):
                continue
            if not file.resolve().is_relative_to(root): continue
            try:
                with file.open(encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh):
                        if i > 30: break
                        try: record = json.loads(line)
                        except ValueError: continue
                        if record.get("type") == "session_meta":
                            meta = record.get("payload") or {}
                            if session_id in (meta.get("id"), meta.get("session_id")):
                                result.append(file)
                            break
            except OSError: continue
    if not result: raise FileNotFoundError("Session not found")
    return result


def delete_session(slug, session_id):
    with JOB_LOCK:
        if any(j.get("state") == "running" and j.get("thread_id") == session_id and j.get("account") == slug for j in JOBS.values()):
            raise RuntimeError("Stop the active session before deleting it")
        files = session_files(slug, session_id)
        for file in files: file.unlink()
    try: history.forget(CODEX_HOME, slug, session_id)
    except (OSError, ValueError):
        # The rollout is already removed; do not report it as a failed deletion.
        print("[codex-web] Could not remove the obsolete title metadata", file=sys.stderr)


# -------------------------------------------------------------------- handler --

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-web"

    def log_message(self, *args):
        pass

    def _guard(self):
        """Check the shared token and return the route segments.

        The token may be the first segment (direct access) or may arrive as the
        X-Codex-Token header, which is what a reverse proxy sends when it mounts
        this app under a prefix of its own.
        """
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] == TOKEN:
            return parts[1:]
        if self.headers.get("X-Codex-Token") == TOKEN:
            return parts
        self._json({"error": "forbidden"}, 403)
        return None

    def _json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, body, status=200):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return {}

    def do_GET(self):
        route = self._guard()
        if route is None:
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = route[0] if route else ""
        # the rest of the path is the action; it can have more than one
        # segment (e.g. "accounts/login"), so it must NOT be split on "/"
        sub = "/".join(route[1:])

        try:
            if name in ("", "index"):
                page = (pathlib.Path(__file__).with_name("codex-web.html"))
                return self._html(page.read_text())
            if name == "api" and len(route) > 1:
                action = sub
                if action == "skills":
                    skill_cwd = safe_workspace(query.get("cwd", [DEFAULT_CWD])[0])
                    slug = accounts.current_account()
                    skill_home = accounts.account_home(slug) if accounts.valid_slug(slug) and slug in accounts.load_index() else CODEX_HOME
                    if skill_home == CODEX_HOME and not (CODEX_HOME / "auth.json").exists():
                        for a in accounts.load_index():
                            if accounts.valid_slug(a) and (accounts.account_home(a) / "auth.json").exists():
                                skill_home = accounts.account_home(a); break
                    return self._json(skills.discovered(skill_cwd, CODEX_BIN, skill_home))
                if action == "models":
                    return self._json({"models": list_models(),
                                       "cwd": DEFAULT_CWD,
                                       "home": str(pathlib.Path.home()),
                                       "limits": fetch_limits()})
                if action == "dirs":
                    return self._json(list_dir(query.get("path", [DEFAULT_CWD])[0]))
                if action == "file":
                    return self._json(read_file(query.get("path", [""])[0]))
                if action == "sessions":
                    sessions = list_sessions()
                    for slug in accounts.load_index():
                        if accounts.valid_slug(slug):
                            sessions.extend(list_sessions(home=accounts.account_home(slug), slug=slug))
                    sessions.sort(key=lambda x: x.get("modified", 0), reverse=True)
                    return self._json({"sessions": sessions[:60]})
                if action == "session":
                    slug = query.get("account", ["main"])[0]
                    session_id = query.get("id", [""])[0]
                    return self._json(read_session(slug, session_id))
                if action == "accounts":
                    main_state = None
                    main_auth = CODEX_HOME / "auth.json"
                    if main_auth.exists():
                        try:
                            data = json.loads(main_auth.read_text())
                            tokens = data.get("tokens", {})
                            main_state = {"access_token": tokens.get("access_token"),
                                          "account_id": tokens.get("account_id", "")}
                        except Exception:
                            main_state = None
                    return self._json({"accounts": accounts.pool(),
                                       "main": accounts._main_usage(main_state or {}),
                                       "current": accounts.current_account()})
                if action == "accounts/login":
                    return self._account_login_state(query)
                if action == "limits":
                    return self._json(fetch_limits())
                if action == "health":
                    return self._json({"ok": True, "jobs": len(JOBS)})
            self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            self._json({"error": f"not found: {exc}"}, 404)
        except PermissionError as exc:
            self._json({"error": f"permission denied: {exc}"}, 403)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        route = self._guard()
        if route is None:
            return
        body = self._body()
        name = route[0] if route else ""
        sub = "/".join(route[1:])

        if name == "api" and len(route) > 1:
            action = sub
            if action in ("sessions/rename", "sessions/delete"):
                slug, session_id = body.get("account", ""), body.get("id", "")
                try:
                    session_files(slug, session_id)
                    if action == "sessions/rename":
                        title = history.rename(CODEX_HOME, slug, session_id, body.get("title"))
                        return self._json({"ok": True, "title": title})
                    delete_session(slug, session_id)
                    return self._json({"ok": True})
                except FileNotFoundError:
                    return self._json({"error": "Session not found"}, 404)
                except PermissionError:
                    return self._json({"error": "Access denied"}, 403)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                except OSError:
                    return self._json({"error": "Could not change the session"}, 500)
            if action in ("skills/install", "skills/create"):
                try:
                    if action == "skills/install":
                        name = skills.install(body.get("name"))
                    else:
                        name = skills.create(body.get("name"), body.get("description"), body.get("instructions"))
                    return self._json({"ok": True, "name": name})
                except FileExistsError:
                    return self._json({"error": "Skill already exists; not overwritten"}, 409)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                except OSError:
                    return self._json({"error": "Could not save skill"}, 500)
            if action == "run":
                return self._stream_run(body)
            if action == "accounts/login":
                return self._account_login(body)
            if action == "accounts/remove":
                slug = accounts.valid_slug(body.get("slug", ""))
                accounts.remove(slug)
                with LOGIN_LOCK:
                    LOGINS.pop(slug, None)
                return self._json({"ok": True, "accounts": accounts.pool()})
            if action == "resume":
                return self._stream_run(body, resume=True)
            if action == "stop":
                job = job_get(body.get("job_id", ""))
                if job:
                    job["cancelled"] = True
                    if job.get("proc"):
                        try:
                            os.killpg(os.getpgid(job["proc"].pid), signal.SIGTERM)
                        except Exception:
                            job["proc"].terminate()
                    job["state"] = "stopped"
                    return self._json({"ok": True})
                return self._json({"ok": False})
        self._json({"error": "not found"}, 404)

    def _account_login(self, body):
        label = (body.get("label") or "").strip()
        # the slug becomes a folder name - it must be one safe path segment
        slug = accounts.valid_slug(body.get("slug")) or accounts.slugify(label or "account")

        if (accounts.account_home(slug) / "auth.json").exists():
            info = accounts.usage(slug)
            return self._json({"ok": True, "slug": slug, "already": True,
                               "status": "authenticated",
                               "email": info.get("email"),
                               "plan": info.get("plan")})

        with LOGIN_LOCK:
            live = LOGINS.get(slug)
        if live and live.get("proc") and live["proc"].poll() is None:
            code = accounts.current_code(slug) or live.get("code")
            return self._json({"ok": True, "slug": slug, "code": code,
                               "url": DEVICE_URL, "status": live.get("status", "waiting")})

        proc = accounts.start_device_login(slug, label)
        code = accounts.read_code(slug, proc)
        entry = {"slug": slug, "proc": proc, "code": code, "status": "waiting"}
        with LOGIN_LOCK:
            LOGINS[slug] = entry
        if not code:
            state = accounts.login_state(slug)
            return self._json({"ok": False, "slug": slug,
                               "error": state.get("detail") or "the login did not start",
                               "log": state.get("log", "")[-600:]}, 502)

        def watch():
            """Wait for the sign-in, renew the code when it expires."""
            while True:
                for _ in range(10):
                    if (accounts.account_home(slug) / "auth.json").exists():
                        entry["status"] = "authenticated"
                        try:
                            entry["proc"].terminate()
                        except Exception:
                            pass
                        return
                    live_proc = entry.get("proc")
                    if live_proc is not None and live_proc.poll() is not None:
                        entry["status"] = "failed"
                        return
                    time.sleep(2)
                # the one-time code expires after 15 minutes -> ask for a new one
                if accounts.current_code(slug) is None:
                    try:
                        entry["proc"].terminate()
                    except Exception:
                        pass
                    new_proc = accounts.start_device_login(slug, label)
                    new_code = accounts.read_code(slug, new_proc)
                    entry["proc"] = new_proc
                    entry["code"] = new_code
                    entry["status"] = "waiting"

        threading.Thread(target=watch, daemon=True).start()
        return self._json({"ok": True, "slug": slug, "code": code,
                           "url": DEVICE_URL, "status": "waiting"})

    def _account_login_state(self, query):
        """Live state of one pending login - the page polls this."""
        slug = accounts.valid_slug(query.get("slug", [""])[0])
        if not slug:
            return self._json({"ok": False, "error": "bad slug"}, 400)
        state = accounts.login_state(slug)
        code = accounts.current_code(slug)
        out = {"ok": True, "slug": slug, "code": code,
               "state": state.get("state"), "detail": state.get("detail")}
        if state.get("log"):
            out["log"] = state["log"][-600:]
        if state.get("state") == "ready":
            info = accounts.usage(slug)
            out.update({"email": info.get("email"), "plan": info.get("plan"),
                        "windows": info.get("windows")})
        return self._json(out)

    def _stream_run(self, body, resume=False):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._json({"error": "prompt required"}, 400)
        try:
            cwd = safe_workspace(body.get("cwd") or DEFAULT_CWD)
        except (PermissionError, ValueError):
            return self._json({"error": "outside workspace"}, 403)
        model = body.get("model") or "gpt-6-luna"
        sandbox = body.get("sandbox") or "workspace-write"
        if sandbox not in ("workspace-write", "read-only"):
            return self._json({"error": "unsupported sandbox"}, 400)
        session_id = body.get("session_id") or ""

        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError:
            pass

        active = accounts.pick(requested_model=model)
        if resume:
            slug = body.get("account") or ""
            if slug == "main":
                active = {"slug":"main", "home":str(CODEX_HOME)}
            elif accounts.valid_slug(slug) and slug in accounts.load_index():
                active = {"slug":slug, "home":str(accounts.account_home(slug))}
            else:
                return self._json({"error":"Session account unavailable"}, 400)
        if not active:
            return self._json({"error": "No usable account. Sign in using Accounts."}, 400)
        used_slugs = []

        def build(model_name, home=None):
            c = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check", "--color", "never",
                 "--cd", cwd, "-m", model_name, "-s", sandbox]
            # In workspace-write mode grant only the skill folders, not the
            # account home containing credentials. Codex's built-in installer
            # writes to CODEX_HOME/skills; shared user skills live in HOME.
            if sandbox == "workspace-write":
                skill_dirs = [skills.skills_dir(), pathlib.Path(home or CODEX_HOME) / "skills"]
                for directory in skill_dirs:
                    directory.mkdir(parents=True, exist_ok=True)
                    c += ["--add-dir", str(directory)]
            if resume and session_id:
                c += ["resume", session_id, prompt]
            else:
                c += ["--", prompt]
            return c

        def build_env(home=None):
            env = dict(os.environ)
            if home:
                env["CODEX_HOME"] = home
            return env

        cmd = build(model)
        job = job_new(cmd, cwd, model, prompt)
        job["account"] = active.get("slug") if active else None
        if resume and session_id: job["thread_id"] = session_id
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        LIMIT_MARKERS = ("usage limit", "rate limit", "429", "usage_limited",
                         "limit_reached", "quota", "exhausted", "luna reserve",
                         "credits are depleted", "included usage",
                         "luna-reserve-recovery", "continue with luna")

        def run(model_name, home=None):
            """Run one attempt, streaming events. Returns (code, stderr_text)."""
            command = build(model_name, home)
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                start_new_session=True, env=build_env(home),
            )
            job["proc"] = proc
            captured = []
            write_lock = threading.Lock()

            def pump(stream, event):
                for line in stream:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    if event == "stderr":
                        captured.append(line)
                    try:
                        payload = json.loads(line)
                    except Exception:
                        payload = {"type": "raw", "text": line}
                    if event == "event" and isinstance(payload, dict) and payload.get("type") == "thread.started":
                        with JOB_LOCK: job["thread_id"] = payload.get("thread_id")
                    try:
                        with write_lock:
                            self.wfile.write(sse(event, payload))
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return

            err_thread = threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)
            err_thread.start()
            pump(proc.stdout, "event")
            proc.wait(timeout=30)
            err_thread.join(timeout=2)
            return proc.returncode, "\n".join(captured)

        try:
            self.wfile.write(sse("job", {"job_id": job["id"], "cmd": cmd}))
            self.wfile.flush()

            def limited(text):
                low = text.lower()
                return any(m in low for m in LIMIT_MARKERS)

            current_home = active["home"] if active else None
            current_slug = active["slug"] if active else "main"
            if active:
                used_slugs.append(current_slug)
                accounts.touch(current_slug) if current_slug != "main" else None
                self.wfile.write(sse("account", {
                    "slug": current_slug, "email": active.get("email"),
                    "plan": active.get("plan"), "home": current_home,
                }))
                self.wfile.flush()

            code, err_text = run(model, current_home)
            hit_limit = limited(err_text)
            if hit_limit and not resume:
                # this account is done for now - the pool moves on to the next one
                accounts.mark_exhausted(current_slug)

            if hit_limit and not resume:
                # 1. reserve model on the same account (separate allocation)
                if model != "gpt-reserve":
                    self.wfile.write(sse("fallback", {
                        "from": model, "to": "gpt-reserve",
                        "reason": "usage limit reached - continuing on the reserve model",
                    }))
                    self.wfile.flush()
                    code, err_text = run("gpt-reserve", current_home)
                    hit_limit = limited(err_text)

                # 2. rotate to the next account in the pool
                rotations = 0
                while hit_limit and rotations < MAX_ROTATIONS:
                    nxt = accounts.pick(requested_model=model, exclude=tuple(used_slugs))
                    if not nxt or nxt["slug"] in used_slugs:
                        break
                    rotations += 1
                    used_slugs.append(nxt["slug"])
                    self.wfile.write(sse("rotate", {
                        "from": current_slug, "to": nxt["slug"],
                        "email": nxt.get("email"), "plan": nxt.get("plan"),
                        "reason": "account quota exhausted - switching account",
                    }))
                    self.wfile.flush()
                    current_slug, current_home = nxt["slug"], nxt["home"]
                    with JOB_LOCK: job["account"], job["thread_id"] = current_slug, None
                    code, err_text = run(model, current_home)
                    hit_limit = limited(err_text)

                # 3. everything is limited: park the request and retry later
                attempt = 0
                while hit_limit and attempt < QUEUE_MAX_RETRIES and not job.get("cancelled"):
                    attempt += 1
                    wait = QUEUE_RETRY_SECONDS
                    self.wfile.write(sse("queued", {
                        "attempt": attempt, "retry_in": wait,
                        "message": f"all accounts exhausted - queued, retrying in {wait}s "
                                   f"(attempt {attempt}/{QUEUE_MAX_RETRIES})",
                        "model": model,
                    }))
                    self.wfile.flush()
                    time.sleep(wait)
                    if job.get("cancelled"):
                        break
                    nxt = accounts.pick(requested_model=model, exclude=tuple(used_slugs))
                    if nxt:
                        used_slugs.append(nxt["slug"])
                    current_slug, current_home = nxt["slug"], nxt["home"]
                    with JOB_LOCK: job["account"], job["thread_id"] = current_slug, None
                    code, err_text = run(model, current_home)
                    hit_limit = limited(err_text)
                    if hit_limit:
                        accounts.mark_exhausted(current_slug)

            job["state"] = "done"
            self.wfile.write(sse("done", {"job_id": job["id"], "code": code,
                                          "limited": hit_limit}))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            job["state"] = "disconnected"
        except Exception as exc:
            try:
                self.wfile.write(sse("error", {"message": str(exc)}))
                self.wfile.flush()
            except Exception:
                pass
            job["state"] = "error"
        finally:
            job_drop(job["id"])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try: skills.ensure_defaults()
    except OSError as exc: print(f"[codex-web] Cannot install default skills: {exc}", file=sys.stderr)
    print(f"[codex-web] http://127.0.0.1:{PORT}/{TOKEN}", flush=True)
    print(f"[codex-web] token: {TOKEN}", flush=True)
    print(f"[codex-web] cwd default: {DEFAULT_CWD}", flush=True)
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
