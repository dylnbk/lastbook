# PyInstaller spec for the app that goes on a drive that travels: one folder for each kind of computer,
# bin/<os>-<arch>/, all opening the same LastbookData beside them. Run through packaging/portable/build_portable.py.
#
#   macOS    Lastbook.app, the app's own window (app/shell_macos.py)
#   Windows  Lastbook\Lastbook.exe: its own window where the WebView2 files were given and the computer has the runtime
#            (app/shell_windows.py), the web browser otherwise (app/shell_browser.py)
#   Linux    Lastbook/Lastbook: the web browser. The Linux window is the system's own Python with GTK and WebKitGTK
#            (packaging/linux), which a program on a drive cannot count on being there.
#
# The product is app/main.py plus what it imports. app/tests is never on the search path.
import importlib
import os
import re
import sys
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parents[1]
APP = ROOT / 'app'
SHIP = Path(os.environ['LASTBOOK_SHIP'])        # web/ and content/ as packaged (packaging/ship_app_files.py), never the source folders
LLAMA = os.environ.get('LASTBOOK_LLAMA')        # a folder holding llama-server and its libraries, when there is one: it becomes llm/
WEBVIEW2 = os.environ.get('LASTBOOK_WEBVIEW2')  # Windows: the three Microsoft WebView2 files, when they were fetched
VERSION = os.environ.get('LASTBOOK_VERSION', '1.1.11')
MAC, WIN = sys.platform == 'darwin', sys.platform == 'win32'


def files(folder, dest, pattern='*'):
    return [(str(p), dest) for p in sorted((SHIP / folder).glob(pattern)) if p.is_file() and not p.name.startswith('.')]


def clean_sysconfig():
    """The standard library's record of how Python was built names folders on the build machine, and nothing in the app
    reads them. Written to a folder searched first, so this copy goes in instead."""
    name = sysconfig._get_sysconfigdata_name()
    built = importlib.import_module(name).build_time_vars
    clean = {k: re.sub(r'(/Users|/home|[A-Za-z]:\\Users)[/\\][^\s:\'"]+', '/python', v) if isinstance(v, str) else v for k, v in built.items()}
    folder = Path(workpath) / 'clean-sysconfig'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.py').write_text(f'build_time_vars = {clean!r}\n', encoding='utf-8')
    return folder


datas = (files('web', 'web') + files('content', 'content', 'gaps.json') + files('content/topics', 'content/topics', '*.json')
         + files('content/regions', 'content/regions', '*.json'))
datas += [(str(ROOT / 'engine' / 'setup' / 'roots.pem'), 'engine/setup')]       # the only roots the downloader trusts
if (SHIP / 'setup' / 'manifest-v1.json').is_file():                              # what setup can install; without it, the list is fetched when setup opens
    datas += [(str(SHIP / 'setup' / 'manifest-v1.json'), 'setup')]
binaries = []
hidden = ['server', 'retrieval', 'assistant', 'llm_local', 'bridge', 'shell_browser'] + collect_submodules('engine')
if LLAMA:
    for p in sorted(Path(LLAMA).iterdir()):
        if p.is_file() and not p.name.startswith('.'):
            # Programs and libraries go in as they are; the licence and notes beside them as data
            (binaries if p.suffix.lower() in ('', '.exe', '.dll', '.dylib', '.so') or '.so.' in p.name else datas).append((str(p), 'llm'))
for package in ('libzim', 'pypdfium2', 'pypdfium2_raw', 'osmium'):      # loaded only when first needed, and they carry native libraries
    try:
        d, b, h = collect_all(package)
    except Exception as e:
        print(f'spec: {package} is not installed here, so the app goes without it ({e})')
        continue
    datas += d
    binaries += b
    hidden += h
excludes = [
    'remote_storage',                                    # stays in the library's own tools: no machine names or ssh code in the package
    'webview', 'bottle', 'proxy_tools', 'lastbook_launcher',
    'onnxruntime', 'tokenizers',                         # see requirements.txt
    'numpy._core._multiarray_tests', 'numpy._core._umath_tests', 'numpy._core._operand_flag_tests', 'numpy._core._rational_tests', 'numpy._core._struct_ufunc_tests',
    'PyObjCTest', 'pytest', 'doctest', 'test', 'numpy.testing', 'numpy.tests', 'numpy.f2py', 'pypdfium2_cli',
    'tkinter', 'idlelib', 'lib2to3', 'ensurepip', 'venv', 'setuptools', 'pkg_resources', 'pip', 'PyInstaller',
]
if MAC:
    hidden += ['shell_macos', 'PyObjCTools.AppHelper']
    for package in ('objc', 'CoreFoundation', 'Foundation', 'AppKit', 'WebKit'):   # each framework's metadata, without which calls into WebKit fail
        hidden += collect_submodules(package)
    excludes += ['shell_windows', 'shell_linux', 'clr', 'clr_loader', 'pythonnet', 'gi']
elif WIN:
    excludes += ['shell_macos', 'shell_linux', 'gi']
    if WEBVIEW2:
        hidden += ['shell_windows', 'clr', '_cffi_backend']
        for package in ('clr_loader', 'pythonnet'):
            d, b, h = collect_all(package)
            datas += d
            binaries += b
            hidden += h
        datas += [(str(Path(WEBVIEW2) / n), 'webview2') for n in ('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll')]
    else:
        excludes += ['shell_windows', 'clr', 'clr_loader', 'pythonnet']
else:
    excludes += ['shell_macos', 'shell_windows', 'shell_linux', 'clr', 'clr_loader', 'pythonnet', 'gi']

# Windows keeps no such record (and asking for its name there fails: found by the first build on Windows)
a = Analysis([str(APP / 'main.py')], pathex=([] if WIN else [str(clean_sysconfig())]) + [str(APP), str(ROOT)], binaries=binaries, datas=datas, hiddenimports=hidden, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
ICO = ROOT / 'packaging' / 'icon' / 'Lastbook.ico'
exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')], exclude_binaries=True, name='Lastbook',
          console=not (MAC or WIN),          # Linux: the terminal it was started from says where it is and stops it
          argv_emulation=False, upx=False, codesign_identity=None, entitlements_file=None,
          icon=str(ICO) if WIN and ICO.exists() else None, **({'target_arch': 'arm64'} if MAC else {}))
coll = COLLECT(exe, a.binaries, a.datas, name='Lastbook', upx=False)
# No BUNDLE on a Mac. PyInstaller's Lastbook.app keeps programs in Contents/Frameworks and everything else in
# Contents/Resources, each linked into the other (89 links in this app): a drive formatted exFAT has no links, and with
# them written out as files the app is two and a half times the size (286 MB against 113) and no longer signs as a bundle.
# build_portable.py puts this one folder inside a Lastbook.app of its own making instead.
