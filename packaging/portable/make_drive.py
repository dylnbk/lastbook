#!/usr/bin/env python3
"""Makes a drive one that opens Lastbook on whatever computer it is plugged into.

  python packaging/portable/make_drive.py /Volumes/Drive [--from FOLDER_OR_ZIP ...] [--apk Install-Android.apk]

Puts at the top of the drive:
  Start-Mac.command, Start-Windows.bat, Start-Linux.sh    one to double-click on each kind of computer
  READ-ME-FIRST.txt                                       what the drive is and how to open it
  bin/<os>-<arch>/                                        the app for each kind of computer that was given with --from
  Install-Android.apk                                     when one was given
  LastbookData/portable.json                              marks the library as one that travels; the library itself is
                                                          built there by the app (Build your own library) or tools/build_library.py

  --from   a folder holding bin/<os>-<arch>/ (the --out of build_portable.py), or a Lastbook-portable-<os>-<arch>.zip
           (what the fork's GitHub Actions make). Any number. An app already on the drive for the same kind of computer
           is replaced; the others, and the library, are left alone.

Nothing else on the drive is touched. A drive for every computer is formatted exFAT; this says so when it is not.
"""
import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
LAUNCHERS = {'macos-arm64': 'Start-Mac.command', 'windows-x64': 'Start-Windows.bat', 'linux-x64': 'Start-Linux.sh'}
# The iPhone and iPad app is not published anywhere: it is built from mobile/ipad with Xcode (mobile/ipad/README.md).
IPHONE = ('  iPhone or iPad                     with the Lastbook app built from the source code (mobile/ipad): Library, then\n'
          '                                     "Choose a LastbookData folder in Files", and choose LastbookData on this drive\n')
READ_ME = """LASTBOOK ON THIS DRIVE

This drive holds an offline library (the LastbookData folder) and the program that opens it, for each kind of
computer. Nothing is installed and nothing is left behind on a computer it is opened on: notes, saved pages and
settings stay on the drive. It needs no internet.

TO OPEN IT

  Mac (Apple silicon, M1 or later)   double-click  Start-Mac.command
  Windows (64-bit)                   double-click  Start-Windows.bat
  Linux (x86-64)                     in a terminal:  sh Start-Linux.sh
{phones}
{have}

ON A MAC, THE FIRST TIME
  macOS may say Start-Mac.command "cannot be opened because it is from an unidentified developer". Control-click it,
  choose Open, then Open. Nothing here is signed with an Apple certificate: it was built from the source code at
  {source}

ON WINDOWS AND LINUX
  Where the computer has no window Lastbook can use, it opens in your web browser, at an address that begins
  http://127.0.0.1 and that only that computer can reach. A small box, or the terminal, stays open while it runs:
  close it to stop Lastbook.

BEFORE YOU UNPLUG
  Close Lastbook, then eject the drive as the system asks you to. A drive pulled out while it is being written to
  can lose what was being saved.

WHAT IS ON IT
{what}
Made {date}.
"""


def say(*words):
    print(*words, flush=True)


def copy_tree(src, dest):
    """A folder copied as plain files. dest is replaced whole."""
    tmp = dest.with_name(dest.name + '.new')
    shutil.rmtree(tmp, ignore_errors=True)
    n = 0
    for root, dirs, files in os.walk(src):
        out = tmp / os.path.relpath(root, src)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name == '.DS_Store' or name.startswith('._'):
                continue
            shutil.copyfile(Path(root) / name, out / name)
            if os.name != 'nt' and os.access(Path(root) / name, os.X_OK):
                try:
                    os.chmod(out / name, 0o755)
                except OSError:
                    pass        # a drive that keeps no such note
            n += 1
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return n


def from_zip(z, drive):
    """bin/<os>-<arch>/... out of a Lastbook-portable zip. Returns the kinds of computer it held."""
    kinds = set()
    with zipfile.ZipFile(z) as f:
        names = [i for i in f.infolist() if not i.is_dir()]
        for i in names:
            parts = i.filename.split('/')
            if len(parts) < 3 or parts[0] != 'bin' or parts[1] not in LAUNCHERS or '..' in parts or i.filename.startswith('/'):
                raise SystemExit(f'{z} is not a Lastbook portable zip: it holds {i.filename}')
            kinds.add(parts[1])
        for kind in kinds:
            shutil.rmtree(drive / 'bin' / kind, ignore_errors=True)
        for i in names:
            out = drive / i.filename
            out.parent.mkdir(parents=True, exist_ok=True)
            with f.open(i) as src, open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            if os.name != 'nt' and (i.external_attr >> 16) & 0o111:
                try:
                    os.chmod(out, 0o755)
                except OSError:
                    pass
    return kinds


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('drive')
    ap.add_argument('--from', dest='sources', action='append', default=[])
    ap.add_argument('--apk')
    a = ap.parse_args()
    drive = Path(a.drive).resolve()
    if not drive.is_dir():
        raise SystemExit(f'{drive} is not a folder')
    from engine.wizard import drives
    fs = drives.filesystem(drive)
    verdict = drives.judge(fs)
    say(f'{drive}: {verdict["note"]}')
    if not verdict['ok']:
        raise SystemExit('Not made: see above.')
    for src in a.sources:
        src = Path(src).resolve()
        if src.is_file() and src.suffix == '.zip':
            for kind in sorted(from_zip(src, drive)):
                say(f'  bin/{kind}: from {src.name}')
        elif (src / 'bin').is_dir():
            for kind_dir in sorted(p for p in (src / 'bin').iterdir() if p.is_dir() and p.name in LAUNCHERS):
                n = copy_tree(kind_dir, drive / 'bin' / kind_dir.name)
                say(f'  bin/{kind_dir.name}: {n} files from {src}')
        else:
            raise SystemExit(f'{src} holds no bin/<os>-<arch>/ and is not a Lastbook portable zip')
    for kind, name in LAUNCHERS.items():
        text = (HERE / 'launchers' / name).read_text(encoding='utf-8')
        if name.endswith('.bat'):
            text = text.replace('\r\n', '\n').replace('\n', '\r\n')     # the line endings Windows' command files are read with
        (drive / name).write_bytes(text.encode('utf-8'))
        if os.name != 'nt':
            try:
                os.chmod(drive / name, 0o755)
            except OSError:
                pass
    if a.apk:
        shutil.copyfile(a.apk, drive / 'Install-Android.apk')
    data = drive / 'LastbookData'
    data.mkdir(exist_ok=True)
    if not (data / 'portable.json').is_file():
        from engine import portable
        portable.mark(data)
    have = sorted(p.name for p in (drive / 'bin').iterdir() if p.is_dir() and p.name in LAUNCHERS) if (drive / 'bin').is_dir() else []
    missing = [k for k in LAUNCHERS if k not in have]
    names = {'macos-arm64': 'Mac', 'windows-x64': 'Windows', 'linux-x64': 'Linux'}
    lines = ['THE PROGRAMS ON THIS DRIVE', '  ' + (', '.join(names[k] for k in have) if have else 'None yet.')]
    if missing:
        lines.append('  Not on it yet: ' + ', '.join(names[k] for k in missing) + '. Their launchers say so when opened.')
    if not (drive / 'Install-Android.apk').is_file():
        lines.append('  The Android app (Install-Android.apk) is not on it yet.')
    else:       # as mobile/android/README.md has it: to be taken out when someone has run it on a phone
        lines.append('  The Android app has been run on a virtual Android 14 phone only: not yet on a real one, nor with a drive plugged in.')
    what = ''
    try:
        pack = json.loads((data / 'pack.json').read_text(encoding='utf-8'))
        counts = pack.get('counts') or {}
        what += f'  A library of {counts.get("documents", "?")} documents, {counts.get("encyclopedias", "?")} encyclopedias and {counts.get("videos", "?")} videos'
        what += f', made from Lastbook {str((pack.get("made_from") or {}).get("name") or pack.get("name") or "").capitalize()}.\n'
        # Counted now, from what is on the drive: pack.json is written when a build begins, and both of these move on after it
        pending, maps = counts.get('videos_pending') or 0, pack.get('regions') or 0
        try:
            catalog = json.loads((data / 'library-data' / 'catalog.json').read_text(encoding='utf-8'))
            pending = sum(1 for r in catalog if r.get('kind') == 'video' and r.get('pending'))
        except (OSError, ValueError):
            pass
        try:
            from engine.wizard.library import regions_here
            maps = len(regions_here(data))
        except Exception:
            maps = len(maps) if isinstance(maps, list) else maps
        if pending:
            what += (f'  When this note was written, {pending} of the videos were still being made smaller by the computer building\n'
                     '  the library. Until a video is done it is here by its words, and says so.\n')
        if maps:
            what += f'  Maps of {maps} regions of the world, on the outline of every country.\n'
    except (OSError, ValueError):
        what += '  No library yet. Open Lastbook from this drive and it will offer to build one here.\n'
    phones = ''
    if (drive / 'Install-Android.apk').is_file():
        phones += '  Android phone or tablet            plug the drive in, install Install-Android.apk from it, then choose LastbookData\n'
    phones += IPHONE
    phones += ('  Any phone on the same Wi-Fi        on the computer, in Lastbook: Library, About this library, then\n'
               '                                     "Share on this network", and scan the code with the phone\n')
    (drive / 'READ-ME-FIRST.txt').write_text(READ_ME.format(have='\n'.join(lines), what=what, phones=phones, date=time.strftime('%Y-%m-%d'),
                                                              source='https://github.com/Immortal-Protocols/lastbook (this fork: see docs/PORTABLE-DRIVE.md)'), encoding='utf-8')
    # Asks a Mac not to index the drive for Spotlight, which would write its index onto it at every Mac it visits.
    (drive / '.metadata_never_index').touch()
    # A Mac writing to a drive that keeps no file attributes puts them beside each file as ._<name>: 128 KB each on a large
    # exFAT drive, for nothing, and Windows shows every one. Those beside what was just written are removed.
    for folder in [drive / 'bin'] if (drive / 'bin').is_dir() else []:
        for p in folder.rglob('._*'):
            try:
                p.unlink()
            except OSError:
                pass
    for name in list(LAUNCHERS.values()) + ['READ-ME-FIRST.txt', 'Install-Android.apk']:
        try:
            (drive / ('._' + name)).unlink()
        except OSError:
            pass
    say(f'Launchers and READ-ME-FIRST.txt written. Programs on the drive: {", ".join(have) or "none"}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
