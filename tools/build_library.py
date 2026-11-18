#!/usr/bin/env python3
"""Make a library of chosen parts from a pack, with maps of chosen regions, without the app's window.

  python tools/build_library.py --menu --from-folder /path/to/pack                 what there is to choose from, with sizes
  python tools/build_library.py --target /Volumes/Drive/LastbookData --from-folder /path/to/pack --choice choice.json
  python tools/build_library.py --target ... --from-url https://downloads.lastbook.org/Lastbook-All.zip --choice choice.json

  --choice FILE      what to keep, as engine/wizard/contents.py and engine/wizard/build.py describe. For example:
                       {"documents": "all", "encyclopedias": "all", "videos": {"default": "small", "topic:firearms": "none"},
                        "maps": [{"id": "great-britain", "level": "detail"}, {"id": "france", "level": "overview"}], "portable": true}
  --encoder software|hardware    how videos are made smaller (default software: half the size and a far better picture; hardware: three times as fast)
  --threads N        processor threads the video encoder may use (default 6)
  --jobs N           videos made smaller at once, sharing those threads (default 1; 2 gets through more on a computer with cores to spare)
  --rate N           download no faster than N megabytes a second
  --sources FOLDER   where map downloads are kept between builds
  --again MINUTES    when the pack folder is itself still being filled: run, wait, and run again until nothing is missing

It can be stopped at any time (Ctrl-C) and run again: it carries on from where it was. The library opens in the app as
soon as its catalog is written; videos still to be made smaller show their words until their turn comes.
"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--target')
    ap.add_argument('--from-folder')
    ap.add_argument('--from-url')
    ap.add_argument('--choice')
    ap.add_argument('--menu', action='store_true')
    ap.add_argument('--encoder', choices=('software', 'hardware'))
    ap.add_argument('--threads', type=int)
    ap.add_argument('--jobs', type=int)
    ap.add_argument('--rate', type=float)
    ap.add_argument('--sources')
    ap.add_argument('--again', type=float)
    a = ap.parse_args()
    if a.target:
        os.environ['LASTBOOK_DATA'] = str(Path(a.target).resolve())
    from engine.setup import download
    from engine.wizard import build, contents
    try:
        os.nice(10)
    except OSError:
        pass
    if a.rate:
        download.THROTTLE.set(a.rate * 1e6)
    if not (a.from_folder or a.from_url):
        ap.error('say where the pack is: --from-folder or --from-url')
    source = build.LocalSource(a.from_folder) if a.from_folder else build.RemoteSource(a.from_url)

    def say(*words):
        print(time.strftime('%H:%M:%S'), *words, flush=True)
    if a.menu:
        scratch = Path(a.target or '.') / '.build'
        records, manifest, _ = source.meta(scratch)
        m = contents.Pack(records, manifest).menu()
        gb = lambda n: f'{n / 1e9:8.2f} GB'
        print('DOCUMENTS (ids for "documents")')
        for d in m['documents']:
            print(f'  {gb(d["bytes"])} {d["count"]:6}  {d["id"]:10} {d["title"]}{"  (essential)" if d["essential"] else ""}')
        print('ENCYCLOPEDIAS (ids for "encyclopedias")')
        for d in m['encyclopedias']:
            print(f'  {gb(d["bytes"])}         {d["id"]}')
        print('VIDEOS (ids for "videos"; original, made smaller, words only)')
        for d in m['videos']:
            print(f'  {gb(d["bytes"])} {gb(d["small_bytes"])} {d["words_bytes"] / 1e6:7.0f} MB {d["count"]:5} videos {d["seconds"] / 3600:5.0f} h  {d["id"]}')
        return 0
    if not a.target or not a.choice:
        ap.error('--target and --choice are needed to build')
    choice = json.loads(Path(a.choice).read_text(encoding='utf-8'))
    if a.encoder:
        choice['encoder'] = a.encoder
    if a.threads:
        choice['threads'] = a.threads
    if a.jobs:
        choice['video_jobs'] = a.jobs
    control = download.Control()
    signal.signal(signal.SIGINT, lambda *x: control.stop('cancelled'))
    signal.signal(signal.SIGTERM, lambda *x: control.stop('cancelled'))
    while True:
        b = build.Build(a.target, source, choice, control=control, say=say, sources_folder=a.sources)
        last = [None]

        def watch():
            while not done[0]:
                s = b.status()
                line = f'{s["phase"]}: {s["message"]}' + (f'  [{s["done_bytes"] / 1e9:.1f} of {s["total_bytes"] / 1e9:.1f} GB]' if s['phase'] == 'files' else '')
                if line != last[0]:
                    say(line)
                    last[0] = line
                time.sleep(5)
        import threading
        done = [False]
        threading.Thread(target=watch, daemon=True).start()
        try:
            status = b.run()
        except download.Stopped:
            say('stopped; run it again to carry on')
            return 130
        except (build.Refused, download.Failed) as e:
            say('could not go on:', getattr(e, 'message', None) or e)
            return 1
        finally:
            done[0] = True
        say(status['message'])
        for p in status['problems'][:40]:
            say('  ', p)
        waiting = any('could not be had from the pack' in p or 'more files' in p for p in status['problems'])
        if not (a.again and waiting):
            return 0 if not status['problems'] else 2
        say(f'the pack folder is still being filled: again in {a.again:g} minutes')
        try:
            control.sleep(a.again * 60)
        except download.Stopped:
            return 130


if __name__ == '__main__':
    sys.exit(main())
