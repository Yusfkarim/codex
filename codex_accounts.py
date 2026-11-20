#!/usr/bin/env python3
"""codex_accounts.py — multi-account pool for Codex.

Each account gets its own CODEX_HOME under ~/.codex/accounts/<slug>/home, so
sessions, config, and credentials are fully isolated — nothing from one account
can leak into another. The pool can report live quota state per account and pick
the best one to serve a request.
"""
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import hmac

BASE = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
ACCOUNTS_DIR = BASE / "accounts"
INDEX_FILE = ACCOUNTS_DIR / "index.json"
LOGIN_STATE_FILE = ACCOUNTS_DIR / "logins.json"
CURRENT_FILE = ACCOUNTS_DIR / "current.json"
EXHAUSTED_FILE = ACCOUNTS_DIR / "exhausted.json"
KEYS_FILE = ACCOUNTS_DIR / "keys.json"
ADMIN_SLUG = "__admin__"
UA = "codex_cli_rs/0.167.2"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CODE_RE = re.compile(r"([A-Z0-9]{4}-[A-Z0-9]{4,5})")
CODE_TTL = int(os.environ.get("CODEX_CODE_TTL", "840"))   # renew the one-time code before it expires (15 min)
LOGIN_TAIL = 1200       # how much of the CLI log to keep for the UI

CONFIG_TEMPLATE = """# generated per account - isolated config
model = "{model}"
model_provider = "openai"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
model_reasoning_effort = "high"
project_doc_max_bytes = 32768
"""


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or f"acc-{int(time.time())}"


def load_index():
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_index(index):
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2))
    INDEX_FILE.chmod(0o600)


def account_home(slug):
    return ACCOUNTS_DIR / slug / "home"


def register(slug, label):
    """Make sure the account has its own CODEX_HOME with a config."""
    home = account_home(slug)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(ACCOUNTS_DIR / slug, 0o700)
    index = load_index()
    entry = index.get(slug, {})
    entry.update({"slug": slug, "label": label or slug,
                  "added": entry.get("added", time.time())})
    index[slug] = entry
    save_index(index)
    return entry


def write_account_config(slug, model="gpt-6-luna"):
    """Give the account its own config, cloned from the main one when possible."""
    home = account_home(slug)
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.toml"
    main_cfg = BASE / "config.toml"
    if main_cfg.exists() and main_cfg.read_text().strip():
        text = main_cfg.read_text()
    else:
        text = CONFIG_TEMPLATE.format(model=model)
    text = re.sub(r'^model\s*=.*$', f'model = "{model}"', text, count=1, flags=re.M)
    text = re.sub(r'^model_provider\s*=.*$', 'model_provider = "openai"', text, count=1, flags=re.M)
    cfg.write_text(text)
    return cfg



CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"


def refresh_tokens(slug="main"):
    """Mint fresh tokens for an account. Returns (state, error)."""
    state = token_state(slug) if slug != "main" else _main_state()
    if not state or not state.get("refresh_token"):
        return None, "no refresh token"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": state["refresh_token"],
        "scope": "openid profile email offline_access",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = json.loads(resp.read().decode())
    except Exception as exc:
        return None, str(exc)[:160]
    import base64
    claims = {}
    try:
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        pass
    auth = (claims.get("https://api.openai.com/auth") or {})
    new_state = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        "account_id": auth.get("chatgpt_account_id") or state.get("account_id", ""),
        "login_owner_id": claims.get("sub") or state.get("login_owner_id", ""),
    }
    home = BASE if slug == "main" else account_home(slug)
    auth_file = home / "auth.json"
    try:
        existing = json.loads(auth_file.read_text()) if auth_file.exists() else {}
    except Exception:
        existing = {}
    existing.update({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": new_state["id_token"],
            "access_token": new_state["access_token"],
            "refresh_token": new_state["refresh_token"],
            "oauth_provenance": {"issuer": "https://auth.openai.com", "client_id": CLIENT_ID},
            "login_owner_id": new_state["login_owner_id"],
            "account_id": new_state["account_id"],
        },
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000000Z",
    })
    auth_file.write_text(json.dumps(existing, indent=2))
    os.chmod(auth_file, 0o600)
    stash = home / "auth.stash.json"
    stash.write_text(json.dumps(existing, indent=2))
    os.chmod(stash, 0o600)
    return new_state, None


def _main_state():
    auth = BASE / "auth.json"
    if not auth.exists():
        return None
    try:
        data = json.loads(auth.read_text())
    except Exception:
        return None
    tokens = data.get("tokens", {})
    return {"access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens.get("id_token"),
            "account_id": tokens.get("account_id", ""),
            "login_owner_id": tokens.get("login_owner_id", "")}

def token_state(slug):
    """Read the stored tokens for an account."""
    home = account_home(slug)
    auth = home / "auth.json"
    if not auth.exists():
        return None
    try:
        data = json.loads(auth.read_text())
    except Exception:
        return None
    tokens = data.get("tokens", {})
    return {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        "account_id": tokens.get("account_id", ""),
        "login_owner_id": tokens.get("login_owner_id", ""),
    }


def _get(url, token, account_id):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("chatgpt-account-id", account_id)
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


def usage(slug):
    """Live quota state for one account."""
    if slug == "main":
        return _main_usage(_main_state())
    state = token_state(slug)
    if not state or not state["access_token"]:
        return {"available": False, "reason": "no credentials"}
    try:
        data = _get("https://chatgpt.com/backend-api/codex/usage?client_version=0.167.2",
                    state["access_token"], state["account_id"])
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:120]}
    rl = data.get("rate_limit", {})
    windows = {}
    for name in ("primary_window", "secondary_window"):
        w = rl.get(name)
        if w:
            windows[name] = {"used_percent": w.get("used_percent"),
                             "window_seconds": w.get("limit_window_seconds"),
                             "resets_at": w.get("reset_at")}
    return {
        "available": bool(rl.get("allowed")) and not rl.get("limit_reached"),
        "limit_reached": bool(rl.get("limit_reached")),
        "email": data.get("email"),
        "plan": data.get("plan_type"),
        "account_id": state["account_id"],
        "windows": windows,
        "unlimited": (data.get("credits") or {}).get("unlimited"),
        "promo": (data.get("promo") or {}).get("message"),
    }


def models(slug):
    state = token_state(slug)
    if not state or not state["access_token"]:
        return []
    try:
        data = _get("https://chatgpt.com/backend-api/codex/models?client_version=0.167.2",
                    state["access_token"], state["account_id"])
    except Exception:
        return []
    return [m.get("slug") for m in data.get("models", []) if m.get("slug")]


def pool(refresh=True):
    """All registered accounts with their live state, primary account first."""
    index = load_index()
    out = []

    main_auth = BASE / "auth.json"
    if main_auth.exists():
        try:
            data = json.loads(main_auth.read_text())
            tokens = data.get("tokens", {})
            main = {"slug": "main", "label": "main", "added": 0,
                    "last_used": 0, "has_auth": True, "state": "ready",
                    "current": current_account() == "main"}
            mu = _main_usage({"access_token": tokens.get("access_token"),
                              "account_id": tokens.get("account_id", "")})
            main.update(mu)
            main["models"] = _main_models({"access_token": tokens.get("access_token"),
                                           "account_id": tokens.get("account_id", "")})
            out.append(main)
        except Exception:
            pass

    current = current_account()
    for slug, entry in index.items():
        item = {
            "slug": slug,
            "label": entry.get("label", slug),
            "added": entry.get("added"),
            "last_used": entry.get("last_used"),
            "has_auth": (account_home(slug) / "auth.json").exists(),
            "current": slug == current,
        }
        if item["has_auth"]:
            item.update(usage(slug))
            item["state"] = "ready"
            if not item.get("available"):
                item["state"] = "exhausted"
        else:
            st = login_state(slug)
            item.update({"available": False,
                         "reason": st.get("detail") or "not logged in",
                         "state": st.get("state", "idle"),
                         "code": current_code(slug)})
        out.append(item)
    out.sort(key=lambda a: (not a.get("available"), a.get("last_used") or 0))
    return out


def pick(requested_model=None, exclude=()):
    """Choose the account that should serve the next request.

    The pool is used in order and one account at a time: the account that is
    "current" keeps serving requests until its quota is exhausted, then the
    next one in line takes over. The main account (~/.codex/auth.json) is the
    first in line.
    """
    candidates = []

    main_state = None
    main_auth = BASE / "auth.json"
    if main_auth.exists():
        try:
            data = json.loads(main_auth.read_text())
            tokens = data.get("tokens", {})
            main_state = {"access_token": tokens.get("access_token"),
                          "account_id": tokens.get("account_id", "")}
        except Exception:
            main_state = None

    if main_state and main_state.get("access_token") and "main" not in exclude:
        candidates.append({"slug": "main", "home": str(BASE),
                           "account_id": main_state["account_id"], "added": 0})

    index = load_index()
    for slug in index:
        if slug in exclude:
            continue
        state = token_state(slug)
        if state and state.get("access_token"):
            candidates.append({"slug": slug, "home": str(account_home(slug)),
                               "account_id": state["account_id"],
                               "added": index.get(slug, {}).get("added") or 0})

    if not candidates:
        return None

    exhausted = load_exhausted()
    now = time.time()
    usable = []
    for cand in candidates:
        try:
            u = usage(cand["slug"]) if cand["slug"] != "main" else _main_usage(main_state)
        except Exception:
            u = {"available": False}
        if not u.get("available"):
            continue
        until = exhausted.get(cand["slug"])
        if until and now < float(until):
            continue                      # quota ran out, wait for the window to reset
        allowed = models(cand["slug"]) if cand["slug"] != "main" else _main_models(main_state)
        if requested_model and allowed and requested_model not in allowed:
            continue
        win = (u.get("windows") or {}).get("primary_window") or {}
        cand.update({"score": win.get("used_percent") or 0,
                     "plan": u.get("plan"), "email": u.get("email"),
                     "models": allowed})
        usable.append(cand)

    if not usable:
        return None

    # 1. the account that is serving right now keeps serving until it is done
    current = current_account()
    if current and current not in exclude:
        for cand in usable:
            if cand["slug"] == current:
                return cand

    # 2. otherwise the next account in line (main first, then by the order they
    #    were added) takes over
    usable.sort(key=lambda c: (c.get("added") or 0, c["slug"]))
    chosen = usable[0]
    set_current(chosen["slug"])
    return chosen


def load_exhausted():
    try:
        if EXHAUSTED_FILE.exists():
            return json.loads(EXHAUSTED_FILE.read_text())
    except Exception:
        pass
    return {}


def mark_exhausted(slug, seconds=600):
    """Remember that this account ran out of quota (until its window resets)."""
    if not slug:
        return
    data = load_exhausted()
    data[slug] = time.time() + seconds
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        EXHAUSTED_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
    if current_account() == slug:
        set_current(None)


def current_account():
    """The account that is serving right now ("" when there is none)."""
    try:
        if CURRENT_FILE.exists():
            return json.loads(CURRENT_FILE.read_text()).get("slug") or ""
    except Exception:
        pass
    return ""


def set_current(slug):
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        CURRENT_FILE.write_text(json.dumps({"slug": slug, "at": time.time()}))
    except Exception:
        pass


def _main_usage(state):
    if not state or not state.get("access_token"):
        return {"available": False}
    try:
        data = _get("https://chatgpt.com/backend-api/codex/usage?client_version=0.167.2",
                    state["access_token"], state["account_id"])
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:100]}
    rl = data.get("rate_limit", {})
    windows = {}
    for name in ("primary_window", "secondary_window"):
        w = rl.get(name)
        if w:
            windows[name] = {"used_percent": w.get("used_percent"),
                             "window_seconds": w.get("limit_window_seconds"),
                             "resets_at": w.get("reset_at")}
    return {"available": bool(rl.get("allowed")) and not rl.get("limit_reached"),
            "limit_reached": bool(rl.get("limit_reached")),
            "email": data.get("email"), "plan": data.get("plan_type"),
            "account_id": state["account_id"], "windows": windows,
            "unlimited": (data.get("credits") or {}).get("unlimited")}


def _main_models(state):
    if not state or not state.get("access_token"):
        return []
    try:
        data = _get("https://chatgpt.com/backend-api/codex/models?client_version=0.167.2",
                    state["access_token"], state["account_id"])
    except Exception:
        return []
    return [m.get("slug") for m in data.get("models", []) if m.get("slug")]


def touch(slug):
    index = load_index()
    if slug in index:
        index[slug]["last_used"] = time.time()
        save_index(index)


def valid_slug(slug):
    """A slug must be one safe path segment - never "", "..", "main" or a path."""
    slug = (slug or "").strip()
    if not slug or slug in (".", "..", "main"):
        return ""
    if "/" in slug or "\\" in slug or slug.startswith("."):
        return ""
    return slug


# --------------------------------------------------------------- API keys ----

def load_keys():
    """Per-account API keys: {slug: "sk-..."}."""
    try:
        if KEYS_FILE.exists():
            return json.loads(KEYS_FILE.read_text())
    except Exception:
        pass
    return {}


def save_keys(keys):
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        KEYS_FILE.write_text(json.dumps(keys, indent=2))
        KEYS_FILE.chmod(0o600)
    except Exception:
        pass


def key_for_slug(slug):
    """The API key for one account (the admin key is the owner)."""
    if slug == ADMIN_SLUG:
        return os.environ.get("CODEX_ADMIN_KEY") or load_keys().get(ADMIN_SLUG, "")
    return load_keys().get(slug, "")


def issue_key(slug):
    """Create a fresh API key for one account and remember it."""
    slug = valid_slug(slug)
    if not slug:
        return ""
    keys = load_keys()
    keys[slug] = "sk-" + secrets.token_urlsafe(24)
    save_keys(keys)
    return keys[slug]


def revoke_key(slug):
    slug = valid_slug(slug)
    if not slug:
        return False
    keys = load_keys()
    if slug not in keys:
        return False
    keys.pop(slug, None)
    save_keys(keys)
    return True


def account_from_key(key):
    """Which account an API key belongs to (or None)."""
    if not key:
        return None
    if os.environ.get("CODEX_ADMIN_KEY") and hmac.compare_digest(key, os.environ["CODEX_ADMIN_KEY"]):
        return ADMIN_SLUG
    keys = load_keys()
    for slug, k in keys.items():
        if hmac.compare_digest(key, k):
            return slug
    return None


def remove(slug):
    """Remove one account. Never touches the pool itself."""
    slug = valid_slug(slug)
    if not slug:
        return False
    index = load_index()
    index.pop(slug, None)
    save_index(index)
    target = ACCOUNTS_DIR / slug
    if target.is_dir() and target != ACCOUNTS_DIR:
        shutil.rmtree(target, ignore_errors=True)
    return True


def start_device_login(slug, label=None):
    """Begin a device-code login for a new account in its own CODEX_HOME.

    The CLI writes into a log file instead of a pipe: a pipe would fill up and
    block the CLI, and whatever it printed (errors!) would be lost.
    """
    home = account_home(slug)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(ACCOUNTS_DIR / slug, 0o700)
    register(slug, label or slug)
    write_account_config(slug)
    logp = login_log(slug)
    logp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(logp, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        ["codex", "login", "--device-auth"],
        stdout=logf, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "CODEX_HOME": str(home)},
    )
    proc.logfile = logf
    save_login_state(slug, {"pid": proc.pid, "started": time.time()})
    return proc


def login_log(slug):
    """Where the device-login CLI writes its output for one account."""
    return account_home(slug) / "log" / "device-login.log"


def login_log_tail(slug):
    try:
        p = login_log(slug)
        if not p.exists():
            return ""
        return ANSI.sub("", p.read_text(errors="replace"))[-LOGIN_TAIL:]
    except Exception:
        return ""


def save_login_state(slug, state):
    data = load_login_states()
    data[slug] = state
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        LOGIN_STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def load_login_states():
    try:
        if LOGIN_STATE_FILE.exists():
            return json.loads(LOGIN_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def login_state(slug):
    """Where an account's device login stands: ready / waiting / failed."""
    if (account_home(slug) / "auth.json").exists():
        return {"state": "ready", "detail": "signed in"}
    tail = login_log_tail(slug)
    low = tail.lower()
    if "timed out" in low or "expired" in low:
        return {"state": "failed", "detail": "the code expired - press Retry for a new one",
                "log": tail}
    if "invalid" in low or "rejected" in low or "denied" in low:
        return {"state": "failed", "detail": "the code was rejected - sign in again", "log": tail}
    if "already logged in" in low or "already signed in" in low:
        return {"state": "failed", "detail": "this account is already signed in", "log": tail}
    if "error" in low or "failed" in low:
        return {"state": "failed", "detail": "the login failed - see the log", "log": tail}
    if "one-time code" in low:
        return {"state": "waiting", "detail": "waiting for you to sign in on the phone", "log": tail}
    if tail.strip():
        return {"state": "waiting", "detail": "starting the login", "log": tail}
    return {"state": "idle", "detail": "not started"}


def read_code(slug, proc=None, timeout=90):
    """Wait for the CLI to print the one-time code, and remember it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = ANSI.sub("", login_log(slug).read_text(errors="replace"))
        except Exception:
            text = ""
        m = CODE_RE.search(text)
        if m:
            code = m.group(1)
            state = load_login_states().get(slug, {})
            state.update({"code": code, "issued": time.time(), "pid": getattr(proc, "pid", None)})
            save_login_state(slug, state)
            return code
        if proc is not None and proc.poll() is not None:
            break
        time.sleep(1)
    return None


def current_code(slug):
    """The code that is live right now (None when it expired)."""
    state = load_login_states().get(slug) or {}
    code = state.get("code")
    issued = state.get("issued") or 0
    if not code or (time.time() - issued) > CODE_TTL:
        return None
    return code


