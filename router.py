"""router.py -- put Codex Web on its own subdomain path, with a password.

The upstream front end authenticates with a random token in the first path
segment. That is correct for an internal proxy, but a browser cannot type it
and it is useless as a login. This wrapper adds a real one:

    POST /login   {"p": "<password>"}   -> sets an HttpOnly session cookie
    GET  /        -> the Codex Web page (only with the cookie)
    /codex-web/*  -> proxied to the front end

Set CODEX_WEB_PASSWORD to choose the password; if unset, one is generated and
printed to the log on first start.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# Bind the backend to a private loopback port before importing codex_web, which
# reads it once at import time. The public port belongs to this router.
os.environ.setdefault('CODEX_WEB_BACKEND_PORT', str(int(os.environ.get('CODEX_WEB_PORT', '8098')) + 1))
os.environ['CODEX_WEB_BIND'] = '127.0.0.1'

import codex_web  # noqa: E402

PASSWORD = os.environ.get('CODEX_WEB_PASSWORD', '')
SESSION_SECONDS = int(os.environ.get('CODEX_WEB_SESSION_SECONDS', str(7 * 24 * 3600)))
COOKIE = 'codex_session'

if not PASSWORD:
    PASSWORD = secrets.token_urlsafe(9)
    print('[codex] generated password: %s' % PASSWORD, flush=True)

# One secret per process; a restart invalidates every cookie, which is the
# behaviour you want if the container is rebuilt with a new password.
SECRET = secrets.token_bytes(32)


def _sign(value):
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def _make_cookie():
    issued = str(int(time.time()))
    return '%s.%s' % (issued, _sign(issued))


def _cookie_ok(raw):
    if not raw or '.' not in raw:
        return False
    issued, sig = raw.split('.', 1)
    if not hmac.compare_digest(sig, _sign(issued)):
        return False
    try:
        return time.time() - int(issued) < SESSION_SECONDS
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'codex'

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- helpers
    def _json(self, payload, status=200, extra=None):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _cookie(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == COOKIE:
                return v
        return ''

    def _authed(self):
        return _cookie_ok(self._cookie())

    # ---------------------------------------------------------------- routes
    def _route(self):
        path = urlsplit(self.path).path
        if path == '/login' and self.command == 'POST':
            return self._login()
        if path == '/logout':
            return self._json({'ok': True}, extra={
                'Set-Cookie': '%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax' % COOKIE})
        if path in ('/health', '/healthz'):
            return self._json({'ok': True, 'codex': codex_web.running()})
        if not self._authed():
            if path.startswith('/codex-web'):
                return self.send_error(401)
            return self._login_page()
        if path in ('/', '/index.html'):
            if not codex_web.running() and not codex_web.start():
                return self.send_error(503, 'Codex Web failed to start')
            page = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'login-ok.html')
            with open(page, 'rb') as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return self.wfile.write(body)
        if path.startswith('/codex-web'):
            if not codex_web.running() and not codex_web.start():
                return self.send_error(503, 'Codex Web failed to start')
            return codex_web.proxy(self)
        return self.send_error(404)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route

    # ---------------------------------------------------------------- login
    def _login(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        try:
            data = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            data = {}
        supplied = str(data.get('p', ''))
        if not hmac.compare_digest(supplied.encode(), PASSWORD.encode()):
            time.sleep(0.4)          # a little friction against guessing
            return self._json({'ok': False, 'error': 'wrong password'}, 401)
        return self._json({'ok': True}, extra={
            'Set-Cookie': '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax'
                          % (COOKIE, _make_cookie(), SESSION_SECONDS)})

    def _login_page(self):
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'login.html')
        try:
            with open(page, 'rb') as fh:
                body = fh.read()
        except OSError:
            return self.send_error(500, 'login page missing')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get('CODEX_WEB_PORT', '8098'))
    bind = os.environ.get('CODEX_WEB_PUBLIC_BIND', '0.0.0.0')
    print('[codex] backend on 127.0.0.1:%d' % codex_web.PORT, flush=True)
    codex_web.start()
    print('[codex] http://%s:%d/' % (bind, port), flush=True)
    ThreadingHTTPServer.daemon_threads = True
    with ThreadingHTTPServer((bind, port), Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
