"""codex_web.py -- run Codex Web as a standalone service.

This is the whole server side of Codex Web: it launches `codex-web.py` (the
browser front end for the Codex CLI) and reverse-proxies to it under one path
prefix. Everything it touches lives under CODEX_WEB_HOME.

    PORT=8098 CODEX_WEB_HOME=/data/codex-web python3 codex_web.py

The launcher that ships in server-agent does the user authentication; this
module deliberately has none, because it is meant to sit in a container or
behind any reverse proxy. Set CODEX_WEB_ACCESS_TOKEN to require a bearer token
of its own.
"""
import http.client
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# The backend listens on its own loopback port; a front door (router.py) or
# any reverse proxy binds the public one. Sharing a single variable would make
# the two processes fight over the same socket.
PORT = int(os.environ.get('CODEX_WEB_BACKEND_PORT') or os.environ.get('CODEX_WEB_PORT', '8098'))
HOME = os.environ.get('CODEX_WEB_HOME', '/data/codex-web')
BIND = os.environ.get('CODEX_WEB_BIND', '127.0.0.1')
ACCESS_TOKEN = os.environ.get('CODEX_WEB_ACCESS_TOKEN', '')
# A random per-process token keeps the Codex front end unreachable except
# through this proxy: the front end checks the first path segment, and only we
# know what it is.
_token = secrets.token_urlsafe(32)

process = None
lock = threading.Lock()


def _backend_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codex-web.py')


def start():
    """Launch the front end. Returns True once it is accepting connections."""
    global process
    with lock:
        if process and process.poll() is None:
            return True
        work = os.path.join(HOME, 'workspace')
        os.makedirs(work, exist_ok=True)
        os.makedirs(os.path.join(HOME, '.codex'), exist_ok=True)
        env = dict(os.environ)
        env.update({
            'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            'HOME': HOME,
            'CODEX_HOME': os.path.join(HOME, '.codex'),
            'DEFAULT_CWD': work,
            'PORT': str(PORT),
            'LINK_TOKEN': _token,
            'LANG': 'C.UTF-8',
            'PYTHONUNBUFFERED': '1',
        })
        script = _backend_script()
        if not os.path.exists(script):
            print('[codex-web] missing %s' % script, file=sys.stderr, flush=True)
            return False
        log_path = os.environ.get('CODEX_WEB_LOG', os.path.join(HOME, 'server.log'))
        try:
            log = open(log_path, 'ab')
        except OSError:
            log = subprocess.DEVNULL
        process = subprocess.Popen([sys.executable, script], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(150):
            if process.poll() is not None:
                return False
            try:
                with socket.create_connection(('127.0.0.1', PORT), .1):
                    return True
            except OSError:
                time.sleep(.1)
        return False


def stop():
    global process
    with lock:
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        process = None
        # a run job spawns `codex` children in its own session; take them too
        subprocess.run(['pkill', '-TERM', '-f', _backend_script()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def running():
    with lock:
        return bool(process and process.poll() is None)


def proxy(handler, prefix='/codex-web'):
    """Forward one request to the front end, streaming the response back."""
    if not running():
        return handler.send_error(503, 'Codex Web is stopped')
    u = urlsplit(handler.path)
    body = None
    if handler.command in ('POST', 'PUT', 'PATCH'):
        origin = handler.headers.get('Origin')
        if origin:
            o = urlsplit(origin)
            if o.scheme not in ('http', 'https') or o.netloc.lower() != handler.headers.get('Host', '').lower():
                return handler.send_error(403)
        try:
            n = int(handler.headers.get('Content-Length', '0'))
        except ValueError:
            return handler.send_error(400)
        if n < 0 or n > 8 * 1024 * 1024:
            return handler.send_error(413)
        body = handler.rfile.read(n)
    rest = u.path[len(prefix):] or '/'
    # the front end expects its token as the first path segment
    target = '/' + _token + rest + (('?' + u.query) if u.query else '')
    conn = http.client.HTTPConnection('127.0.0.1', PORT, timeout=3600)
    try:
        conn.request(handler.command, target, body=body, headers={
            'Content-Type': handler.headers.get('Content-Type', 'application/json'),
            'Accept': handler.headers.get('Accept', 'application/json'),
            'Accept-Encoding': 'identity',
        })
        resp = conn.getresponse()
        handler.send_response(resp.status)
        handler.send_header('Content-Type', resp.getheader('Content-Type', 'application/octet-stream'))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        length = resp.getheader('Content-Length')
        if length:
            handler.send_header('Content-Length', length)
        else:
            handler.send_header('Connection', 'close')
            handler.close_connection = True
        handler.end_headers()
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (OSError, http.client.HTTPException):
        handler.close_connection = True
    finally:
        conn.close()


class Standalone(BaseHTTPRequestHandler):
    """A minimal entry point so the module can run without server-agent."""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _authorised(self):
        if not ACCESS_TOKEN:
            return True
        auth = self.headers.get('Authorization', '')
        return auth == 'Bearer ' + ACCESS_TOKEN

    def _route(self):
        if not self._authorised():
            return self.send_error(401)
        if self.command == 'GET' and urlsplit(self.path).path in ('/', '/health'):
            if not running() and not start():
                return self.send_error(503, 'Codex Web failed to start')
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if urlsplit(self.path).path.startswith('/codex-web'):
            if not running() and not start():
                return self.send_error(503, 'Codex Web failed to start')
            return proxy(self)
        return self.send_error(404)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route


def main():
    if not start():
        print('[codex-web] could not start the front end', file=sys.stderr)
        return 1
    print('[codex-web] listening on http://%s:%d/codex-web/' % (BIND, PORT), flush=True)
    ThreadingHTTPServer.daemon_threads = True
    with ThreadingHTTPServer((BIND, PORT), Standalone) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
