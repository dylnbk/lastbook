"""A made-up pack for tests: a few documents, saved pages, a course, an encyclopedia and videos, with the catalog, search
index and manifest a real pack has. Never packaged."""
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from engine.wizard import video

LIB = 'library-data/'


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_videos(folder):
    """Three short real videos, or None where there is no ffmpeg: 'big' and 'vlog' at over 400 kbps, 'small' under it."""
    t = video.tools()
    if not t or 'libx264' not in t['encoders']:
        return None
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, size, rate, seconds in (('big', '640x480', '1500k', 4), ('vlog', '640x360', '1200k', 4), ('small', '160x120', '60k', 4)):
        p = folder / f'{name}.mp4'
        if not p.is_file():
            subprocess.run([t['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440',
                            '-t', str(seconds), '-c:v', 'libx264', '-b:v', rate, '-minrate', rate, '-maxrate', rate, '-bufsize', rate, '-pix_fmt', 'yuv420p',
                            '-c:a', 'aac', '-b:a', '64k', str(p)], check=True, capture_output=True)
        out[name] = p.read_bytes()
    return out


def records_and_files(videos=None):
    """(catalog records, {path in the pack: bytes}). videos: make_videos()'s, or None for made-up bytes of the right sizes."""
    v = videos or {'big': b'B' * 900_000, 'vlog': b'V' * 700_000, 'small': b'S' * 40_000}
    files, recs = {}, []

    def doc(rid, kind, path, body, category, collection='Test', publisher='Test Press', text=None, **more):
        files[LIB + path] = body
        r = {'id': rid, 'title': rid.replace('-', ' ').title(), 'kind': kind, 'publisher': publisher, 'collection': collection, 'category': category,
             'path': path, 'bytes': len(body), 'sha256': sha(body), **more}
        if text is not None:
            r['text_path'] = f'text/{rid}.txt'
            files[LIB + r['text_path']] = text.encode()
        recs.append(r)
        return r
    doc('first-aid-manual', 'pdf', 'documents/medical/first-aid.pdf', b'%PDF-1.4 ' + b'm' * 5000, 'medical', pages=3, text='stop the bleeding')
    doc('water-guide', 'pdf', 'documents/water-sanitation/water.pdf', b'%PDF-1.4 ' + b'w' * 4000, 'water-sanitation', pages=2, text='boil water for one minute')
    doc('canning-book', 'html', 'pages/food-preservation/canning/index.html', b'<html>canning</html>', 'food-preservation', text='canning jars')
    files[LIB + 'pages/food-preservation/canning/images/jar.png'] = b'\x89PNG' + b'j' * 3000          # a saved book's picture: no record names it
    files[LIB + 'assets/food-preservation/abc.png'] = b'\x89PNG' + b'a' * 2000                        # a picture of that category's saved pages
    files[LIB + 'assets/medical/def.png'] = b'\x89PNG' + b'd' * 1000
    doc('algebra-notes', 'html', 'courses/18-06-linear-algebra/pages/notes.html', b'<html>algebra</html>', 'mathematics', collection='Linear Algebra', course='Linear Algebra', text='matrix')
    files[LIB + 'courses/18-06-linear-algebra/static/style.css'] = b'body{}' * 300
    doc('glider-handbook', 'pdf', 'documents/aviation/glider.pdf', b'%PDF-1.4 ' + b'g' * 6000, 'aviation', pages=9, text='thermals')
    doc('small-wiki', 'zim', 'collections/small_wiki.zim', b'ZIM\x04' + b'z' * 20000, 'encyclopedias', collection='Kiwix offline collections', publisher='Kiwix')
    doc('big-wiki', 'zim', 'collections/big_wiki.zim', b'ZIM\x04' + b'Z' * 60000, 'encyclopedias', collection='Kiwix offline collections', publisher='Kiwix')

    def vid(vid_id, folder, body, publisher, category, duration=4, transcript=True):
        path = f'videos/{folder}/{vid_id}.mp4'
        caption = f'videos/{folder}/{vid_id}.en.vtt'
        files[LIB + caption] = b'WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nhello from ' + vid_id.encode() + b'\n'
        r = doc('video-' + vid_id, 'video', path, body, category, collection='Practical video demonstrations', publisher=publisher, duration=duration, captions=[caption],
                text=f'words of {vid_id}')
        thumb = f'metadata/videos/video-{vid_id}.thumbnail.jpg'
        files[LIB + thumb] = b'\xff\xd8' + b't' * 1500
        recs.append({'id': 'metadata-video-' + vid_id, 'kind': 'metadata', 'title': 'Metadata: ' + vid_id, 'publisher': publisher, 'collection': 'Video metadata and descriptions',
                     'category': category, 'path': thumb, 'video_id': 'video-' + vid_id, 'video_path': path, 'thumbnail_path': thumb, 'bytes': len(files[LIB + thumb])})
        if transcript:
            tpath = f'transcripts/{vid_id}.en.txt'
            files[LIB + tpath] = f'transcript of {vid_id}'.encode()
            recs.append({'id': 'transcript-' + vid_id, 'kind': 'transcript', 'title': 'Transcript: ' + vid_id, 'publisher': publisher, 'collection': 'Video transcripts', 'category': category,
                         'path': tpath, 'bytes': len(files[LIB + tpath]), 'sha256': sha(files[LIB + tpath]), 'video_path': path, 'text_path': f'text/transcript-{vid_id}.txt',
                         'vtt_path': caption, 'caption_type': 'authored'})
            files[LIB + f'text/transcript-{vid_id}.txt'] = files[LIB + tpath]
        return r
    vid('bandage', 'first-aid', v['big'], 'Health Films', 'first-aid')
    vid('campfire', 'creator-corporals-corner', v['vlog'], 'Corporals Corner', 'field-survival')
    vid('lecture', 'mathematics', v['small'], 'Open Courses', 'mathematics', transcript=False)
    files[LIB + 'reader.css'] = b'p{margin:0}'
    return recs, files


def search_db(path, recs, files):
    """search.sqlite as a pack has it: a page of words for every record that has words."""
    db = sqlite3.connect(path)
    db.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
    for r in recs:
        if r.get('text_path'):
            body = files[LIB + r['text_path']].decode()
            for page in range(1, (r.get('pages') or 1) + 1):
                db.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', (r['id'], r['title'], r['collection'], r['category'], r['path'], page, body + ' ' + 'filler ' * 200))
    db.commit()
    db.close()


def write_pack(folder, videos=None, without=()):
    """A pack as it sits on a disk: <folder>/library-data/..., pack.json, pack-manifest.tsv. without: paths left out, as a pack
    folder still being filled. Returns (records, files)."""
    folder = Path(folder)
    recs, files = records_and_files(videos)
    lib = folder / 'library-data'
    lib.mkdir(parents=True, exist_ok=True)
    search_db(lib / 'search.sqlite', recs, files)
    files[LIB + 'search.sqlite'] = (lib / 'search.sqlite').read_bytes()
    files[LIB + 'catalog.json'] = json.dumps(recs).encode()
    files[LIB + 'summary.json'] = json.dumps({'resources': len(recs)}).encode()
    files['pack.json'] = json.dumps({'name': 'all', 'snapshot': '20261102-025617', 'built': '2026-11-12', 'known_gaps': []}).encode()
    files['derived/geo/map.sqlite'] = b'the US map: never part of a library made of chosen parts' * 100
    manifest = 'path\tbytes\tsha256\thow\tchanges\n' + ''.join(f'{p}\t{len(b)}\t{sha(b)}\tclone\t\n' for p, b in sorted(files.items()))
    files['pack-manifest.tsv'] = manifest.encode()
    for p, b in files.items():
        if p in without:
            continue
        (folder / p).parent.mkdir(parents=True, exist_ok=True)
        (folder / p).write_bytes(b)
    return recs, files
