"""Making a library's videos smaller: the same video, re-encoded as HEVC at a size chosen for what it shows.

Measured on one 9-minute outdoor video from the library (720p H.264, 1,950 kbps), on a 90 s piece, each result compared
with the original by VMAF (0 to 100, how alike they look) at 480p. Apple M4, with two map builds running beside it:

    how                              kbps   VMAF   speed
    libx265 veryfast  crf 30  480p    244   89.5    3.5x
    libx265 superfast crf 30  480p    250   88.0    5.5x      <- 'howto'
    libx265 ultrafast crf 30  480p    184   82.0   10.1x
    libx265 superfast crf 29  360p    157   78.2   11.8x      <- 'talk' (judged scaled back up to 480p)
    hardware (VideoToolbox) 300k 480p 581   74.3   ~20x
    hardware (VideoToolbox) q 45 360p 364   64.2   ~25x

The Mac's hardware encoder is three or four times as fast and keeps the processor idle, but at these sizes it gives
more than twice the bytes for a plainly rougher picture: it is offered ('hardware'), not chosen. A video already under
400 kbps is left exactly as it is: 777 of the All pack's 2,324 are, and re-encoding them would lose more than it saves.

  tools()                       ffmpeg and ffprobe on this computer, and which encoders that ffmpeg has
  profile_for(record)           'keep', 'howto' or 'talk' for a catalog record
  estimate(record, profile)     bytes it will be, before doing it
  encode(src, dest, profile)    does it; returns what came out

One video at a time, at low priority, on a limited number of threads: the computer stays the person's.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from engine import paths
from engine.setup import hardware

KEEP_UNDER = 400_000        # bits a second: a video already this small is not touched
PROFILES = {
    # height: lines of picture; crf: libx265's quality number (higher is smaller); audio: kbps, one channel; video: the kbps measured, for estimates
    'howto': {'height': 480, 'crf': 30, 'audio': 48, 'video': 250, 'hardware_q': 50},
    'talk': {'height': 360, 'crf': 29, 'audio': 40, 'video': 157, 'hardware_q': 45},
}
# Channels that are a person talking to a camera outdoors for hours: watched for what is said and broadly shown, not for fine detail.
TALK_PUBLISHERS = {'Corporals Corner'}
PRESET = 'superfast'
THREADS = 6                 # of libx265's worker threads: about half an M4, the rest stays free
SMALLER = 0.9               # an encode that is not at least this much smaller than the original is thrown away, and the original kept


class Failed(Exception):
    """A plain sentence saying why a video could not be made smaller."""


def _candidates(name):
    exe = name + ('.exe' if os.name == 'nt' else '')
    out = []
    if os.environ.get('LASTBOOK_FFMPEG'):
        out.append(Path(os.environ['LASTBOOK_FFMPEG']).with_name(exe))
    out.append(paths.DATA / 'models' / 'runtime' / f'{hardware.os_name()}-{hardware.arch()}' / exe)
    out.append(paths.APP / 'tools' / exe)
    home = Path.home()
    if sys.platform == 'darwin':
        out.append(home / 'Library/Application Support/anythingllm-desktop/storage/engines/ffmpeg' / f'mac-{hardware.arch()}' / exe)
        out += [Path('/opt/homebrew/bin') / exe, Path('/usr/local/bin') / exe]
    which = shutil.which(name)
    if which:
        out.append(Path(which))
    return out


_tools = {}


def tools(refresh=False):
    """{'ffmpeg', 'ffprobe', 'encoders'} or None when there is no ffmpeg that can write HEVC here.
    On a Mac, one built for this processor is preferred to one running under translation (measured: 53 against 121 frames a second)."""
    if _tools and not refresh:
        return _tools.get('found')
    found = None
    for p in _candidates('ffmpeg'):
        try:
            if not (p.is_file() and os.access(p, os.X_OK)):
                continue
            out = subprocess.run([str(p), '-hide_banner', '-encoders'], stdin=subprocess.DEVNULL, capture_output=True, timeout=30, **_quiet()).stdout.decode('utf-8', 'replace')
        except (OSError, subprocess.SubprocessError):
            continue
        encoders = {w for line in out.splitlines() for w in line.split()[1:2]}
        if 'libx265' not in encoders and 'hevc_videotoolbox' not in encoders:
            continue
        native = True
        if sys.platform == 'darwin' and hardware.arch() == 'arm64':
            try:
                native = b'arm64' in subprocess.run(['/usr/bin/file', str(p)], capture_output=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                pass
        probe = p.with_name('ffprobe' + p.suffix)
        one = {'ffmpeg': str(p), 'ffprobe': str(probe) if probe.is_file() else None, 'encoders': encoders, 'native': native}
        if native:
            found = one
            break
        found = found or one
    _tools['found'] = found
    return found


def _quiet():
    return {'creationflags': 0x08000000} if os.name == 'nt' else {}


def probe(path, t=None):
    """{'duration', 'width', 'height', 'video', 'audio', 'bitrate'} of a video file, or None if it cannot be read as one."""
    t = t or tools()
    if not t or not t['ffprobe']:
        return None
    try:
        out = subprocess.run([t['ffprobe'], '-v', 'error', '-show_entries', 'format=duration,bit_rate:stream=codec_type,codec_name,width,height', '-of', 'json', str(path)],
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=120, **_quiet())
        d = json.loads(out.stdout or b'{}')
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    v = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'), None)
    a = next((s for s in d.get('streams', []) if s.get('codec_type') == 'audio'), None)
    try:
        duration = float(d['format']['duration'])
    except (KeyError, ValueError, TypeError):
        return None
    if not v or duration <= 0:
        return None
    return {'duration': duration, 'width': v.get('width'), 'height': v.get('height'), 'video': v.get('codec_name'), 'audio': a.get('codec_name') if a else None,
            'bitrate': int(d['format'].get('bit_rate') or 0)}


def bitrate_of(record):
    """Bits a second of a catalog record's video, from its size and length; None if either is missing."""
    try:
        return record['bytes'] * 8 / record['duration'] if record.get('duration') else None
    except (KeyError, TypeError):
        return None


def profile_for(record):
    """What 'make it smaller' means for one video: 'keep' (it is small already, or its length is not known), 'talk' or 'howto'."""
    rate = bitrate_of(record)
    if rate is None or rate < KEEP_UNDER:
        return 'keep'
    return 'talk' if record.get('publisher') in TALK_PUBLISHERS else 'howto'


def estimate(record, profile=None):
    """Bytes a record's video will be once made smaller. An estimate from the measured rates: pictures differ."""
    profile = profile or profile_for(record)
    if profile == 'keep':
        return record.get('bytes') or 0
    p = PROFILES[profile]
    return min(record.get('bytes') or 0, int((p['video'] + p['audio']) * 1000 / 8 * record['duration']))


def command(t, src, dest, profile, encoder='software', threads=THREADS):
    p = PROFILES[profile]
    audio = 'aac_at' if 'aac_at' in t['encoders'] else 'aac'
    a = [t['ffmpeg'], '-hide_banner', '-nostdin', '-loglevel', 'error', '-progress', 'pipe:1', '-y', '-i', str(src),
         '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn', '-vf', f"scale=-2:'min({p['height']},ih)'", '-pix_fmt', 'yuv420p']
    if encoder == 'hardware':
        if 'hevc_videotoolbox' not in t['encoders']:
            raise Failed('This computer’s ffmpeg has no hardware HEVC encoder.')
        a += ['-c:v', 'hevc_videotoolbox', '-q:v', str(p['hardware_q'])]
    else:
        if 'libx265' not in t['encoders']:
            raise Failed('This computer’s ffmpeg has no libx265 encoder.')
        a += ['-c:v', 'libx265', '-preset', PRESET, '-crf', str(p['crf']), '-x265-params', f'log-level=error:pools={max(1, int(threads))}']
    # hvc1: the name Apple's players look for; faststart: the index at the front, so a video starts before it is all read
    a += ['-tag:v', 'hvc1', '-c:a', audio, '-b:a', f"{p['audio']}k", '-ac', '1', '-movflags', '+faststart', '-f', 'mp4', str(dest)]
    return a


def encode(src, dest, profile, encoder='software', threads=THREADS, control=None, progress=None, duration=None):
    """src -> dest, smaller. Written beside dest and renamed only when whole and checked.
    Returns {'bytes', 'seconds', 'kept': False} or, when the result was not worth it, {'kept': True} (dest is then not written).
    Raises Failed; a control that stops raises whatever its check() raises, and nothing is left behind."""
    t = tools()
    if not t:
        raise Failed('No ffmpeg that can write HEVC is on this computer.')
    src, dest = Path(src), Path(dest)
    was = probe(src, t)
    if was is None:
        raise Failed('The video could not be read.')
    duration = was['duration']
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    t0 = time.time()

    def lower():        # below everything the person is doing
        try:
            os.nice(15)
        except OSError:
            pass
    proc = subprocess.Popen(command(t, src, part, profile, encoder, threads), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=lower if os.name != 'nt' else None, **_quiet())
    try:
        for raw in proc.stdout:
            if control is not None:
                control.check()
            line = raw.decode('ascii', 'replace').strip()
            if line.startswith('out_time_ms=') and progress:
                try:
                    progress(min(1.0, int(line[12:]) / 1e6 / duration))
                except ValueError:
                    pass
        code = proc.wait()
        err = proc.stderr.read().decode('utf-8', 'replace')[-400:]
    except BaseException:
        proc.kill()
        proc.wait()
        _forget(part)
        raise
    finally:
        proc.stdout.close()
        proc.stderr.close()
    if code != 0:
        _forget(part)
        raise Failed(f'ffmpeg could not make the video smaller ({err.strip().splitlines()[-1][:200] if err.strip() else "exit " + str(code)}).')
    now = probe(part, t)
    if now is None or abs(now['duration'] - duration) > max(2.0, 0.02 * duration) or (was['audio'] and not now['audio']):
        _forget(part)
        raise Failed('The smaller video did not come out whole, so the original is kept.')
    size = part.stat().st_size
    if size > SMALLER * src.stat().st_size:
        _forget(part)
        return {'kept': True, 'seconds': round(time.time() - t0, 1)}
    os.replace(part, dest)
    _forget(dest.with_name('._' + dest.name))
    return {'kept': False, 'bytes': size, 'seconds': round(time.time() - t0, 1), 'speed': round(duration / max(0.01, time.time() - t0), 1),
            'height': now['height'], 'kbps': round(size * 8 / duration / 1000)}


def _forget(path):
    """Removes a file and the '._name' beside it that an exFAT drive keeps a Mac's file attributes in (128 KB each, for nothing)."""
    for p in (path, path.with_name('._' + path.name)):
        try:
            p.unlink()
        except OSError:
            pass
