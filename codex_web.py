"""codex_web.py -- run Codex Web as a standalone service.

Codex Web has no login of its own. It is a single-user tool: whoever can reach
the port has the machine's Codex session, which is the same trust model as
running `codex` in a terminal. Access is controlled by Docker, Fly, or whatever
reverse proxy you put in front of it -- not by this module.

    CODEX_WEB_PORT=8098 CODEX_WEB_HOME=/data/codex-web python3 codex_web.py

The front end is launched on loopback. Requests arriving on this server are
forwarded to it with a per-process token that the front end requires, so the
front end cannot be reached around the launcher even though it is on the same
host.
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

PORT = int(os.environ.get('CODEX_WEB_PORT', '8098'))
# The front end listens privately on the next port up, so the two processes
# never compete for the same socket. Override if you have a reason to.
BACKEND_PORT = int(os.environ.get('CODEX_WEB_BACKEND_PORT', str(PORT + 1)))
BIND = os.environ.get('CODEX_WEB_BIND', '127.0.0.1')
HOME = os.environ.get('CODEX_WEB_HOME', '/data/codex-web')
LOG = os.environ.get('CODEX_WEB_LOG', '')

_token = secrets.token_urlsafe(32)

process = None
lock = threading.Lock()


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
            'PORT': str(BACKEND_PORT),
            'LINK_TOKEN': _token,
            'LANG': 'C.UTF-8',
            'PYTHONUNBUFFERED': '1',
        })
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codex-web.py')
        if not os.path.exists(script):
            print('[codex-web] missing %s' % script, file=sys.stderr, flush=True)
            return False
        try:
            log = open(LOG or os.path.join(HOME, 'server.log'), 'ab')
        except OSError:
            log = subprocess.DEVNULL
        process = subprocess.Popen([sys.executable, script], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(150):
            if process.poll() is not None:
                return False
            try:
                with socket.create_connection(('127.0.0.1', BACKEND_PORT), .1):
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
        subprocess.run(['pkill', '-TERM', '-f', 'codex-web.py'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def running():
    with lock:
        return bool(process and process.poll() is None)


def proxy(handler, prefix=''):
    """Forward one request to the front end, streaming the response back.

    Works at any mount point: `prefix` is the leading path this proxy owns and
    is stripped before forwarding, while the front end's token travels in a
    header."""
    if not running() and not start():
        return handler.send_error(503, 'Codex Web failed to start')
    u = urlsplit(handler.path)
    body = None
    if handler.command in ('POST', 'PUT', 'PATCH', 'DELETE'):
        try:
            n = int(handler.headers.get('Content-Length', '0'))
        except ValueError:
            return handler.send_error(400)
        if n < 0 or n > 8 * 1024 * 1024:
            return handler.send_error(413)
        body = handler.rfile.read(n)
    rest = u.path[len(prefix):] if prefix and u.path.startswith(prefix) else u.path
    rest = rest or '/'
    if not rest.startswith('/'):
        rest = '/' + rest
    target = rest + (('?' + u.query) if u.query else '')
    conn = http.client.HTTPConnection('127.0.0.1', BACKEND_PORT, timeout=3600)
    try:
        conn.request(handler.command, target, body=body, headers={
            'X-Codex-Token': _token,
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


class Server(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'codex'

    def log_message(self, *a):
        pass

    PREFIXES = ('/codex-web',)

    def _route(self):
        path = urlsplit(self.path).path
        if path in ('/health', '/healthz'):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        # Everything else is the app. It is served at "/" and also under
        # /codex-web/ so that both an embedded dashboard card and a direct link
        # work with the same code.
        for prefix in self.PREFIXES:
            if path == prefix:
                self.send_response(301)
                self.send_header('Location', prefix + '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            if path.startswith(prefix + '/'):
                return proxy(self, prefix)
        return proxy(self, '')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _route


def main():
    if not start():
        print('[codex-web] could not start the front end', file=sys.stderr)
        return 1
    print('[codex-web] http://%s:%d/  (front end on 127.0.0.1:%d)' % (BIND, PORT, BACKEND_PORT), flush=True)
    print('[codex-web] home: %s' % HOME, flush=True)
    ThreadingHTTPServer.daemon_threads = True
    allow_reuse = ThreadingHTTPServer.allow_reuse_address
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        with ThreadingHTTPServer((BIND, PORT), Server) as httpd:
            httpd.serve_forever()
    finally:
        ThreadingHTTPServer.allow_reuse_address = allow_reuse
        stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
