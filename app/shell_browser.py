"""Lastbook in a tab of this computer's own web browser, for where it cannot make a window of its own.

The app's window is WebKit on a Mac, WebView2 on Windows and WebKitGTK on Linux. A drive that travels meets computers
that have none of them to hand: a Linux desktop without WebKitGTK's Python bindings, a Windows computer without the
WebView2 runtime. There the same pages are served to the browser instead:

  - on 127.0.0.1 only, on a port the system chooses (or the one asked for), so nothing on the network can reach it;
  - a request must name localhost or an address, never another name (a web page using DNS rebinding arrives under its own
    name), and a POST must come from the page itself (server.py does the same for the library; setup mode gets it here);
  - it runs until the person closes the "Lastbook is running" box, or the terminal it was started from.

What differs from the window: the address has a port, other programs on this computer could ask it questions as the
browser does, and there is no folder chooser, so a folder is typed. This is the server version (python server.py) with
the app's way of finding its library and of starting again after setup.
"""
import http.server
import ipaddress
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

PORT_ENV = 'LASTBOOK_BROWSER_PORT'      # kept across a start-again (setup finished), so the tab that is open still finds it
OPENED_ENV = 'LASTBOOK_BROWSER_OPENED'
TRIES = 12                              # quarter-seconds to wait for a port that is asked for and still held
MESSAGE = 'Lastbook is open in your web browser at {url}\n\nIt runs for as long as this box is open. Choose OK to stop it.'


def host_ok(host):
    """Only localhost or a bare IP address."""
    try:
        name = urllib.parse.urlsplit('//' + (host or 'localhost')).hostname or ''
    except ValueError:
        return False
    if name == 'localhost':
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def origin_ok(origin, host):
    """A POST from another page carries that page's Origin; one from a file or a sandbox carries 'null'."""
    if not origin:
        return True
    try:
        return urllib.parse.urlsplit(origin).netloc == host
    except ValueError:
        return False


def guarded(handler):
    """A request handler made for the app's own window, given the two checks a real port needs."""
    class Guarded(handler):
        def _refuse(self):
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def _allowed(self, post):
            host = self.headers.get('Host') or ''
            return host_ok(host) and (not post or origin_ok(self.headers.get('Origin'), host))

        def do_GET(self):
            return super().do_GET() if self._allowed(False) else self._refuse()

        def do_HEAD(self):
            return super().do_HEAD() if self._allowed(False) else self._refuse()

        def do_POST(self):
            return super().do_POST() if self._allowed(True) else self._refuse()
    return Guarded


def _server(handler, port):
    cls = http.server.ThreadingHTTPServer
    if os.name == 'nt':     # SO_REUSEADDR on Windows lets a second copy silently share the port
        class cls(cls):
            allow_reuse_address = False
    srv = None
    for attempt in range(TRIES if port else 1):     # started again after setup, the run before may not have let go of the port yet
        try:
            srv = cls(('127.0.0.1', port), handler)
            break
        except OSError:
            if not port:
                raise
            time.sleep(0.25)
    if srv is None:
        srv = cls(('127.0.0.1', 0), handler)       # the port asked for is taken by something else: any other
    srv.daemon_threads = True
    return srv


def _box(url):
    """Something the person can close to stop Lastbook, where there is a way to show one. Returns when they have, or None at
    once when nothing could be shown (the terminal is then the way to stop it)."""
    text = MESSAGE.format(url=url)
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, 'Lastbook', 0x40)     # MB_ICONINFORMATION
        return True
    if sys.platform == 'darwin':
        script = 'display dialog %s with title "Lastbook" buttons {"Stop Lastbook"} default button 1 with icon note' % _applescript(text.replace('Choose OK', 'Choose Stop Lastbook'))
        try:
            return subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True).returncode in (0, 1)
        except OSError:
            return None
    for tool, args in (('zenity', ['--info', '--title=Lastbook', '--no-wrap', '--text=' + text]), ('kdialog', ['--title', 'Lastbook', '--msgbox', text])):
        path = shutil.which(tool)
        if path and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            try:
                subprocess.run([path] + args, capture_output=True)
                return True
            except OSError:
                continue
    return None


def _applescript(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def run(handler, port=0, open_browser=True, box=True, path='/', ready=None):
    """Serves handler on a loopback port until the person stops it. Returns 0.
    ready(server, url): called once it is listening (tests)."""
    port = int(os.environ.get(PORT_ENV) or port or 0)
    srv = _server(handler, port)
    url = f'http://127.0.0.1:{srv.server_address[1]}{path}'
    os.environ[PORT_ENV] = str(srv.server_address[1])
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} in the browser: {url}', flush=True)
    for out in (sys.__stdout__, ):
        if out is not None and out is not sys.stdout:       # a packaged app's own output goes to its log: the terminal is told too
            try:
                print(f'Lastbook: {url}\nClose this window, or press Ctrl-C, to stop it.', file=out, flush=True)
            except (OSError, ValueError):
                pass
    serving = threading.Thread(target=srv.serve_forever, daemon=True, name='browser-server')
    serving.start()
    if ready is not None:
        ready(srv, url)
    if open_browser and not os.environ.get(OPENED_ENV):     # started again after setup: the tab is already open, on this address
        os.environ[OPENED_ENV] = '1'
        try:
            webbrowser.open(url)
        except Exception as e:
            print('could not open a browser:', type(e).__name__, e, flush=True)
    try:
        if not (box and _box(url)):
            while serving.is_alive():
                serving.join(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0
