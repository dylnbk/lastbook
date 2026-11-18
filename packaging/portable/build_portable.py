#!/usr/bin/env python3
"""Builds the app for a drive that travels, for the computer this is run on: bin/<os>-<arch>/, and a zip of it.

  python packaging/portable/build_portable.py --out BUILD [--llama FOLDER] [--webview2 FOLDER] [--smoke LIBRARY]

  --out FOLDER       where everything goes: ship/ (web and content as packaged), work/, dist/, bin/<os>-<arch>/ and
                     Lastbook-portable-<os>-<arch>.zip. On a Mac, a folder on a disk formatted for a Mac (the app is
                     built with links in it, which are then written out as plain files for a drive that has none).
  --llama FOLDER     llama-server and the libraries beside it, to go inside the app as llm/: the program that runs AI models.
                     Without it the app uses one already on the computer, or one in the library (models/runtime/).
  --webview2 FOLDER  Windows: Microsoft.Web.WebView2.Core.dll, Microsoft.Web.WebView2.WinForms.dll and WebView2Loader.dll.
                     With them the app has its own window where the computer has the WebView2 runtime; without, the browser.
  --smoke LIBRARY    after building, start what was built on that library (in the browser's way, with no browser opened)
                     and ask it three questions. The build fails if it does not answer.

Run it with a Python that has packaging/portable/requirements.txt installed: that Python is what goes in the app.
The same on macOS (arm64), Windows (x64) and Linux (x64); .github/workflows/build-portable.yml runs it on all three.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))


def say(*words):
    print(time.strftime('%H:%M:%S'), *words, flush=True)


def platform_folder():
    from engine.setup import hardware
    return f'{hardware.os_name()}-{hardware.arch()}'


def plain_copy(src, dest):
    """A folder copied with every link in it written out as the file it points to: a drive formatted exFAT has no links."""
    for root, dirs, files in os.walk(src, followlinks=True):
        out = Path(dest) / os.path.relpath(root, src)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name == '.DS_Store' or name.startswith('._'):
                continue
            p = Path(root) / name
            shutil.copyfile(p, out / name)
            os.chmod(out / name, 0o755 if os.access(p, os.X_OK) else 0o644)


def zip_folder(folder, top, out):
    """A zip of folder under the name top, with which files are programs kept (a zip made on Windows has no such note,
    so the launchers on a Mac and on Linux also set it themselves)."""
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(Path(folder).rglob('*')):
            if p.is_file() and not p.name.startswith('._') and p.name != '.DS_Store':
                info = zipfile.ZipInfo.from_file(p, f'{top}/{p.relative_to(folder).as_posix()}')
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if os.access(p, os.X_OK) and os.name != 'nt' or p.suffix == '' and p.parent.name in ('MacOS', 'Lastbook', 'llm') else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                with open(p, 'rb') as f:
                    z.writestr(info, f.read())


def program(bundle):
    if sys.platform == 'darwin':
        return bundle / 'Lastbook.app' / 'Contents' / 'MacOS' / 'Lastbook'
    return bundle / 'Lastbook' / ('Lastbook.exe' if os.name == 'nt' else 'Lastbook')


def smoke(bundle, library):
    """The built program, started on a library with no browser opened, must answer as the app does."""
    exe = program(bundle)
    out = subprocess.run([str(exe), '--help'], capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or '--browser' not in out.stdout + out.stderr:
        if os.name != 'nt':         # a windowed program on Windows has nowhere to print its help: the questions below are the test there
            raise SystemExit(f'the built program does not start: exit {out.returncode}\n{out.stdout}\n{out.stderr}')
    port = 47000 + os.getpid() % 2000
    log = Path(library) / 'derived' / 'logs' / 'lastbook.log'
    p = subprocess.Popen([str(exe), '--browser', '--no-open', '--no-box', '--port', str(port), '--data', str(library), '--portable'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base, last = f'http://127.0.0.1:{port}', None
        for _ in range(240):
            if p.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + '/api/health', timeout=5) as r:
                    last = r.status
                    break
            except OSError as e:
                last = e
                time.sleep(0.5)
        if last != 200:
            tail = log.read_text(encoding='utf-8', errors='replace')[-3000:] if log.is_file() else '(no log)'
            raise SystemExit(f'the built program did not answer on {base}: {last}; exit code {p.poll()}\n{tail}')
        with urllib.request.urlopen(base + '/', timeout=20) as r:
            page = r.read()
        with urllib.request.urlopen(base + '/api/wizard/state', timeout=60) as r:
            state = json.loads(r.read())
        with urllib.request.urlopen(base + '/ui/wizard.html', timeout=20) as r:
            wizard = r.read()
        if b'/ui/app.js' not in page or b'/ui/wizard.js' not in wizard or not state.get('current'):
            raise SystemExit('the built program answered, but not as the app does')
        say(f'smoke: it answers on a library; what it can do here: {state["tools"]}')
    finally:
        p.terminate()
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--llama')
    ap.add_argument('--webview2')
    ap.add_argument('--smoke')
    ap.add_argument('--version', default='1.1.11')
    a = ap.parse_args()
    out = Path(a.out).resolve()
    name = platform_folder()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYINSTALLER_CONFIG_DIR=str(out / 'pyinstaller-cache'), LASTBOOK_SHIP=str(out / 'ship'),
               LASTBOOK_VERSION=a.version)
    for key, value in (('LASTBOOK_LLAMA', a.llama), ('LASTBOOK_WEBVIEW2', a.webview2)):
        env.pop(key, None)
        if value:
            env[key] = str(Path(value).resolve())
    for sub in ('ship', 'work', 'dist', 'bin'):
        shutil.rmtree(out / sub, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    say(f'building for {name} with Python {sys.version.split()[0]}')
    subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'packaging' / 'ship_app_files.py'), str(out / 'ship')], check=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(out / 'dist'), '--workpath', str(out / 'work'),
                    str(HERE / 'lastbook_portable.spec')], check=True, env=env, cwd=str(out))
    bundle = out / 'bin' / name
    made = out / 'dist' / 'Lastbook'
    if sys.platform == 'darwin':
        # Lastbook.app, made here: the program in Contents/MacOS and everything it needs in Contents/Frameworks (where
        # PyInstaller's program looks when it finds itself inside an app), as plain files with no links (see the spec for
        # why not PyInstaller's own). Each program inside keeps the signature it was built with (ad hoc: no certificate),
        # which is what an Apple silicon Mac asks of what it runs. The app as a whole is not sealed.
        dest = bundle / 'Lastbook.app'
        (dest / 'Contents' / 'MacOS').mkdir(parents=True)
        shutil.copyfile(made / 'Lastbook', dest / 'Contents' / 'MacOS' / 'Lastbook')
        os.chmod(dest / 'Contents' / 'MacOS' / 'Lastbook', 0o755)
        plain_copy(made / '_internal', dest / 'Contents' / 'Frameworks')
        (dest / 'Contents' / 'Resources').mkdir(parents=True)
        icon = ROOT / 'packaging' / 'Lastbook.icns'
        if icon.is_file():
            shutil.copyfile(icon, dest / 'Contents' / 'Resources' / 'Lastbook.icns')
        import plistlib
        words = 'Lastbook looks here for its library, the LastbookData folder. It reads and saves only inside that folder.'
        with open(dest / 'Contents' / 'Info.plist', 'wb') as f:
            plistlib.dump({
                'CFBundleExecutable': 'Lastbook', 'CFBundleIdentifier': 'local.lastbook.portable', 'CFBundleName': 'Lastbook', 'CFBundleDisplayName': 'Lastbook',
                'CFBundlePackageType': 'APPL', 'CFBundleInfoDictionaryVersion': '6.0', 'CFBundleShortVersionString': a.version, 'CFBundleVersion': a.version,
                'CFBundleIconFile': 'Lastbook.icns', 'LSApplicationCategoryType': 'public.app-category.reference',
                'LSMinimumSystemVersion': '14.0',       # what numpy's binaries ask for
                'LSRequiresNativeExecution': True, 'NSHighResolutionCapable': True, 'NSPrincipalClass': 'NSApplication',
                'NSDocumentsFolderUsageDescription': words, 'NSDownloadsFolderUsageDescription': words, 'NSDesktopFolderUsageDescription': words,
                'NSRemovableVolumesUsageDescription': 'Lastbook opens the library on this drive, the LastbookData folder beside it. It reads and saves only inside that folder.',
            }, f)
        # Every program and library must still be what was signed, or an Apple silicon Mac will not run it. A copy is the
        # same bytes, so each should be; this looks. (Not the app's own program, nor what is inside Python.framework: asked
        # about those, codesign answers for the app or the framework as a whole, neither of which is sealed. Both are
        # run by --smoke.)
        main_program = dest / 'Contents' / 'MacOS' / 'Lastbook'
        unsigned = []
        for p in sorted((dest / 'Contents' / 'Frameworks').rglob('*')):
            if p.is_file() and p != main_program and not any(part.endswith('.framework') for part in p.parts) and p.suffix in ('', '.dylib', '.so') \
                    and open(p, 'rb').read(4) in (b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
                if subprocess.run(['codesign', '--verify', str(p)], capture_output=True).returncode != 0:
                    unsigned.append(p)
        if unsigned:
            raise SystemExit(f'{len(unsigned)} programs or libraries in the app would not run: their signatures do not match them ({unsigned[0]})')
    else:
        dest = bundle / 'Lastbook'
        shutil.copytree(made, dest)
        if os.name == 'nt' and (ROOT / 'packaging' / 'Lastbook.exe.config').is_file() and a.webview2:
            shutil.copyfile(ROOT / 'packaging' / 'Lastbook.exe.config', dest / 'Lastbook.exe.config')     # without it, a downloaded copy cannot open its window
    links = [p for p in bundle.rglob('*') if p.is_symlink()]
    if links:
        raise SystemExit(f'{len(links)} links are left in {bundle}: a drive formatted exFAT cannot hold them ({links[0]})')
    names = [str(p) for p in bundle.rglob('*') if p.is_file()]
    tests = [n for n in names if re.search(r'[/\\]test_[a-z_]+\.py[co]?$', n) or re.search(r'[/\\](buildkit|aikit|regionkit)\.py[co]?$', n)]
    if tests:
        raise SystemExit(f'test code is in the package: {tests[0]}')
    (bundle / 'BUILD.txt').write_text(f'Lastbook {a.version} for {name}\nbuilt {time.strftime("%Y-%m-%d")} with Python {sys.version.split()[0]}\n'
                                      f'AI model runner inside: {"yes" if a.llama else "no"}\n'
                                      + (f'own window on Windows: {"yes" if a.webview2 else "no (opens in the web browser)"}\n' if os.name == 'nt' else ''), encoding='utf-8')
    size = sum(Path(n).stat().st_size for n in names)
    say(f'{bundle}: {len(names)} files, {size / 1e6:.0f} MB')
    if a.smoke:
        smoke(bundle, Path(a.smoke).resolve())
    z = out / f'Lastbook-portable-{name}.zip'
    zip_folder(bundle, f'bin/{name}', z)
    say(f'{z.name}: {z.stat().st_size / 1e6:.0f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
