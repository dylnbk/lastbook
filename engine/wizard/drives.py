"""The disks a library could be built on, and what each one's format means for a library that travels.

  filesystem(path)   'exfat', 'apfs', 'ntfs', 'fat32', 'ext4', ... or None when the system will not say
  listed()           the drives plugged into this computer: [{'path', 'name', 'filesystem', 'free_bytes', 'total_bytes', 'removable'}]
  judge(fs)          {'ok', 'travels', 'note'}: whether a library can be built there, and where it will open afterwards

Nothing here writes anything.
"""
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

NAMES = {'msdos': 'fat32', 'vfat': 'fat32', 'fat': 'fat32', 'fat32': 'fat32', 'exfat': 'exfat', 'fuseblk': 'ntfs', 'ntfs': 'ntfs', 'ntfs3': 'ntfs',
         'ufsd_ntfs': 'ntfs', 'apfs': 'apfs', 'hfs': 'hfs+', 'hfsplus': 'hfs+', 'ext2': 'ext', 'ext3': 'ext', 'ext4': 'ext', 'btrfs': 'btrfs',
         'xfs': 'xfs', 'f2fs': 'f2fs', 'zfs': 'zfs', 'refs': 'refs', 'udf': 'udf', 'smbfs': 'network', 'cifs': 'network', 'nfs': 'network',
         'nfs4': 'network', 'afpfs': 'network', 'webdav': 'network', 'tmpfs': 'memory'}
SHOWN = {'exfat': 'exFAT', 'fat32': 'FAT32', 'ntfs': 'NTFS', 'apfs': 'APFS', 'hfs+': 'Mac OS Extended', 'ext': 'ext4', 'btrfs': 'Btrfs', 'xfs': 'XFS',
         'f2fs': 'F2FS', 'zfs': 'ZFS', 'refs': 'ReFS', 'udf': 'UDF', 'network': 'a network folder', 'memory': 'memory'}
LINUX_ROOTS = ('/media/', '/run/media/', '/mnt/')


def _mac_mounts():
    """[(mount point, type, flags)] from `mount`, whose lines read: /dev/disk4s1 on /Volumes/T9 (exfat, local, nodev, ...)."""
    try:
        text = subprocess.run(['/sbin/mount'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in text.splitlines():
        if ' on ' not in line or not line.rstrip().endswith(')'):
            continue
        rest = line.split(' on ', 1)[1]
        point, _, flags = rest.rpartition(' (')
        flags = [f.strip() for f in flags.rstrip(')').split(',')]
        if point and flags:
            out.append((point, flags[0].lower(), flags[1:]))
    return out


def _linux_mounts():
    out = []
    try:
        with open('/proc/mounts', encoding='utf-8', errors='replace') as f:
            for line in f:
                c = line.split()
                if len(c) >= 4:
                    point = c[1].replace('\\040', ' ').replace('\\011', '\t')
                    out.append((point, c[2].lower(), c[3].split(',')))
    except OSError:
        pass
    return out


def _windows_volume(root):
    """(label, filesystem, drive type) of 'D:\\'. Drive type 2 is removable, 3 fixed, 4 network."""
    k = ctypes.windll.kernel32
    label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
    if not k.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, None, None, None, fs, 261):
        return None
    return label.value, fs.value.lower(), k.GetDriveTypeW(ctypes.c_wchar_p(root))


def _existing(path):
    p = Path(path).expanduser()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _mount_of(path, mounts):
    """The mount a path sits on: the longest mount point that is the path or a folder above it."""
    try:
        p = str(_existing(path).resolve())
    except OSError:
        return None
    best = None
    for point, kind, flags in mounts:
        if p == point or p.startswith(point.rstrip('/') + '/') or point == '/':
            if best is None or len(point) > len(best[0]):
                best = (point, kind, flags)
    return best


def filesystem(path):
    try:
        if sys.platform == 'win32':
            root = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
            v = _windows_volume(root)
            return NAMES.get(v[1], v[1]) if v else None
        m = _mount_of(path, _mac_mounts() if sys.platform == 'darwin' else _linux_mounts())
        return NAMES.get(m[1], m[1]) if m else None
    except Exception:
        return None


def listed():
    """Drives other than the one the system is on. A drive that cannot be read is left out, never guessed at."""
    out = []
    try:
        if sys.platform == 'win32':
            k = ctypes.windll.kernel32
            bits = k.GetLogicalDrives()
            system = (os.environ.get('SystemDrive') or 'C:').upper()
            for i in range(26):
                if not bits >> i & 1:
                    continue
                letter = chr(65 + i) + ':'
                v = _windows_volume(letter + '\\')
                if not v or letter == system or v[2] not in (2, 3):
                    continue
                out.append(_entry(letter + '\\', f'{v[0] or "Drive"} ({letter})', NAMES.get(v[1], v[1]), v[2] == 2))
        elif sys.platform == 'darwin':
            for point, kind, flags in _mac_mounts():
                if not point.startswith('/Volumes/') or kind in ('devfs', 'autofs', 'nullfs'):
                    continue
                if 'read-only' in flags and NAMES.get(kind) != 'ntfs':      # an installer's disk image; an NTFS drive is listed, to say why it cannot be used
                    continue
                out.append(_entry(point, Path(point).name, NAMES.get(kind, kind), _mac_external(point)))
        else:
            for point, kind, flags in _linux_mounts():
                if not point.startswith(LINUX_ROOTS) or kind in ('tmpfs', 'overlay', 'squashfs', 'proc', 'sysfs'):
                    continue
                out.append(_entry(point, Path(point).name, NAMES.get(kind, kind), point.startswith(('/media/', '/run/media/'))))
    except Exception as e:      # a strange disk must not stop the wizard: it then offers only "choose a folder"
        print('wizard: drives:', type(e).__name__, e, flush=True)
    return [d for d in out if d]


def _mac_external(point):
    try:
        import Foundation
        ok, value, _ = Foundation.NSURL.fileURLWithPath_(point).getResourceValue_forKey_error_(None, Foundation.NSURLVolumeIsInternalKey, None)
        return bool(ok and value is not None and not value)
    except Exception:
        return False


def _entry(path, name, fs, removable):
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return dict({'path': path, 'name': name, 'filesystem': fs, 'free_bytes': u.free, 'total_bytes': u.total, 'removable': bool(removable),
                 'writable': os.access(path, os.W_OK)}, **judge(fs))


def judge(fs):
    """What a disk's format means. ok: a library can be built on it. travels: the computers a library on it will open on."""
    shown = SHOWN.get(fs, fs or 'unknown')
    if fs == 'exfat':
        return {'ok': True, 'format': shown, 'travels': 'all', 'note': 'exFAT: opens on a Mac, on Windows and on Linux, and an iPad, iPhone or Android phone can read it.'}
    if fs == 'fat32':
        return {'ok': False, 'format': shown, 'travels': 'all',
                'note': 'This drive is formatted FAT32, which cannot hold a file over 4 GB. An encyclopedia or the map of a country is larger than that. '
                        'Reformat it as exFAT (this erases the drive) and it will work everywhere.'}
    if fs == 'ntfs':
        if sys.platform == 'darwin':
            return {'ok': False, 'format': shown, 'travels': 'windows', 'note': 'This drive is formatted NTFS, which a Mac can read but not write. Reformat it as exFAT (this erases the drive) to build on it here.'}
        return {'ok': True, 'format': shown, 'travels': 'windows', 'note': 'NTFS: opens on Windows and Linux. A Mac can read it but not save notes or settings to it, and an iPad or phone cannot read it. exFAT works everywhere.'}
    if fs in ('apfs', 'hfs+'):
        return {'ok': True, 'format': shown, 'travels': 'mac', 'note': f'{shown}: only a Mac can read this drive. For a library that also opens on Windows, Linux or a phone, use a drive formatted exFAT.'}
    if fs in ('ext', 'btrfs', 'xfs', 'f2fs', 'zfs'):
        return {'ok': True, 'format': shown, 'travels': 'linux', 'note': f'{shown}: only Linux can read this drive. For a library that also opens on a Mac, Windows or a phone, use a drive formatted exFAT.'}
    if fs == 'network':
        return {'ok': False, 'format': shown, 'travels': 'none', 'note': 'This is a folder on another computer. A library’s search index needs a disk of this computer.'}
    return {'ok': True, 'format': shown, 'travels': 'unknown', 'note': 'Lastbook cannot tell how this disk is formatted, so it cannot say which computers will read it.'}
