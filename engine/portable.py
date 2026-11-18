"""A library that travels: one folder on a USB drive that any computer can open, and that keeps what the person does in it.

  <drive>/LastbookData/portable.json    marks the library as one that travels
  <drive>/LastbookData/user/            what the person keeps: saved items, chats, the page's settings (engine/userstore.py)
  <drive>/.lastbook/                    the program's own working state: setup's job and a staged switch. It sits beside the
                                        library, not in it, because a switch renames the library and must outlive that.
  <drive>/Start-Mac.command, ...        the launchers (packaging/portable/)

Opened this way, Lastbook writes its own files only on the drive: nothing in the computer's Application Support, AppData
or ~/.config, and nothing in the web view's storage. What the operating system itself records about a program that ran
(recent items, its own logs) is the system's, and is not something a program can prevent.

This module reads no settings and imports nothing of the engine, because app/main.py asks it where the library is before
the engine's paths are worked out.
"""
import json
import sys
import time
from pathlib import Path

MARK = 'portable.json'
DATA_NAME = 'LastbookData'
CATALOG = Path('library-data') / 'catalog.json'
# How far up from the program a drive's top can be: <drive>/bin/macos-arm64/Lastbook.app/Contents/MacOS/Lastbook is the deepest.
UP = 6


def is_portable(data):
    try:
        return (Path(data) / MARK).is_file()
    except OSError:
        return False


def program():
    """The file that was started: the packaged program, or app/main.py from source."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve()
    main = sys.modules.get('__main__')
    return Path(getattr(main, '__file__', None) or sys.argv[0] or '.').resolve()


def beside(start=None, up=UP):
    """The travelling library that sits with the program: a LastbookData holding portable.json, in the program's folder or
    one of the few above it. Only a marked one counts, so an ordinary library that happens to be nearby is never taken over."""
    here = Path(start) if start else program()
    folder = here if here.is_dir() else here.parent
    for _ in range(up):
        candidate = folder / DATA_NAME
        if is_portable(candidate):
            return candidate
        if folder.parent == folder:
            break
        folder = folder.parent
    return None


def user_dir(data):
    return Path(data) / 'user'


def state_dir(data):
    """Where the program keeps its own working state for a library that travels: beside the library, on the same drive."""
    return Path(data).parent / '.lastbook'


def mark(data, **more):
    """Make a library one that travels. Says what it is and nothing about the computer that made it."""
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    info = {'portable': True, 'made': time.strftime('%Y-%m-%d'), 'layout': 1}
    info.update(more)
    (data / MARK).write_text(json.dumps(info, indent=1) + '\n', encoding='utf-8')
    user_dir(data).mkdir(exist_ok=True)
    return info


def has_library(data):
    try:
        return (Path(data) / CATALOG).is_file()
    except OSError:
        return False
