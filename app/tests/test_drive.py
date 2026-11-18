#!/usr/bin/env python3
"""A drive that opens Lastbook on whatever computer it is plugged into: what make_drive.py puts on it, and what each
launcher does when it is run.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_drive.py [-v]

The "app" on the drive is a script that writes down how it was started. The Mac launcher is run on a Mac and the Linux
one wherever there is a POSIX shell; the Windows one is read, not run, except on Windows. Never packaged."""
import json, os, shutil, stat, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(ROOT / 'packaging' / 'portable'), str(HERE)]
import make_drive  # noqa: E402
import build_portable  # noqa: E402
from engine import portable  # noqa: E402
from engine.wizard import drives  # noqa: E402

LAUNCHERS = ROOT / 'packaging' / 'portable' / 'launchers'
RECORDER = '#!/bin/sh\nprintf "%s\\n" "$@" > "$(dirname "$0")/started.txt"\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-drive-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.drive = self.tmp / 'drive'
        self.drive.mkdir()

    def bundle(self, kind, program):
        """bin/<kind>/... as build_portable.py leaves it, with a program that records its arguments."""
        out = self.tmp / 'built' / kind
        p = out / 'bin' / kind / program
        p.parent.mkdir(parents=True)
        p.write_text(RECORDER)
        os.chmod(p, 0o755)
        (p.parent / 'data.bin').write_bytes(b'x' * 100)
        (out / 'bin' / kind / 'BUILD.txt').write_text('made up\n')
        return out

    def make(self, *args):
        with mock.patch.object(sys, 'argv', ['make_drive.py', str(self.drive), *args]), mock.patch.object(make_drive, 'say'):
            return make_drive.main()


class Made(Base):
    def test_what_is_put_on_the_drive(self):
        mac = self.bundle('macos-arm64', 'Lastbook.app/Contents/MacOS/Lastbook')
        self.assertEqual(self.make('--from', str(mac)), 0)
        names = sorted(p.name for p in self.drive.iterdir())
        self.assertEqual(names, ['.metadata_never_index', 'LastbookData', 'READ-ME-FIRST.txt', 'Start-Linux.sh', 'Start-Mac.command', 'Start-Windows.bat', 'bin'])
        self.assertTrue(portable.is_portable(self.drive / 'LastbookData'))             # a library there is one that travels
        self.assertEqual((self.drive / 'bin/macos-arm64/Lastbook.app/Contents/MacOS/Lastbook').read_text(), RECORDER)
        bat = (self.drive / 'Start-Windows.bat').read_bytes()
        self.assertIn(b'\r\n', bat)
        self.assertNotIn(b'\n', bat.replace(b'\r\n', b''))                              # the line endings Windows reads command files with, throughout
        self.assertNotIn(b'\r', (self.drive / 'Start-Mac.command').read_bytes())
        self.assertNotIn(b'\r', (self.drive / 'Start-Linux.sh').read_bytes())
        note = (self.drive / 'READ-ME-FIRST.txt').read_text()
        self.assertIn('Start-Mac.command', note)
        self.assertIn('Not on it yet: Windows, Linux', note)                            # it says what is not there
        self.assertIn('Install-Android.apk) is not on it yet', note)
        self.assertNotIn('install Install-Android.apk', note)                           # and does not tell anyone to install what is not
        self.assertIn('No library yet', note)

    def test_from_the_zips_the_builds_make(self):
        zips = []
        for kind, program in (('windows-x64', 'Lastbook/Lastbook.exe'), ('linux-x64', 'Lastbook/Lastbook')):
            built = self.bundle(kind, program)
            z = self.tmp / f'Lastbook-portable-{kind}.zip'
            build_portable.zip_folder(built / 'bin' / kind, f'bin/{kind}', z)
            zips += ['--from', str(z)]
        apk = self.tmp / 'app.apk'
        apk.write_bytes(b'PK made up')
        self.make(*zips, '--apk', str(apk))
        self.assertEqual((self.drive / 'bin/linux-x64/Lastbook/Lastbook').read_text(), RECORDER)
        self.assertTrue((self.drive / 'bin/windows-x64/Lastbook/Lastbook.exe').is_file())
        if os.name != 'nt':
            self.assertTrue(os.access(self.drive / 'bin/linux-x64/Lastbook/Lastbook', os.X_OK))     # a program is still one after the zip
            self.assertFalse(os.access(self.drive / 'bin/linux-x64/Lastbook/data.bin', os.X_OK))
        self.assertEqual((self.drive / 'Install-Android.apk').read_bytes(), b'PK made up')
        note = (self.drive / 'READ-ME-FIRST.txt').read_text()
        self.assertIn('Not on it yet: Mac', note)
        self.assertIn('install Install-Android.apk', note)

    def test_an_app_is_replaced_whole_and_the_library_left_alone(self):
        lib = self.drive / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        catalog = json.dumps([{'id': 'video-a', 'kind': 'video', 'pending': True}, {'id': 'video-b', 'kind': 'video'}, {'id': 'doc-c', 'kind': 'document'}])
        (lib / 'library-data' / 'catalog.json').write_text(catalog)
        # As a build leaves it part of the way through: written when the build began, when nine videos waited and one map was there
        (lib / 'pack.json').write_text(json.dumps({'name': 'custom', 'made_from': {'name': 'all'}, 'regions': ['great-britain'],
                                                   'counts': {'documents': 8283, 'encyclopedias': 4, 'videos': 2324, 'videos_pending': 9}}))
        for region in ('great-britain', 'france'):
            (lib / 'derived' / 'geo' / 'regions' / region).mkdir(parents=True)
            (lib / 'derived' / 'geo' / 'regions' / region / 'region.json').write_text(json.dumps({'id': region}))
            (lib / 'derived' / 'geo' / 'regions' / region / 'map.sqlite').write_bytes(b'')
        (lib / 'user').mkdir()
        (lib / 'user' / 'notes.txt').write_text('mine')
        old = self.drive / 'bin' / 'macos-arm64' / 'Lastbook.app' / 'old-file'
        old.parent.mkdir(parents=True)
        old.write_text('from the build before')
        (self.drive / 'holiday.jpg').write_bytes(b'not ours')
        self.make('--from', str(self.bundle('macos-arm64', 'Lastbook.app/Contents/MacOS/Lastbook')))
        self.assertFalse(old.exists())                                                  # nothing of the old app is mixed into the new
        self.assertEqual((lib / 'user' / 'notes.txt').read_text(), 'mine')
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), catalog)
        self.assertEqual((self.drive / 'holiday.jpg').read_bytes(), b'not ours')        # nor is anything else on the drive touched
        note = (self.drive / 'READ-ME-FIRST.txt').read_text()
        self.assertIn('8283 documents, 4 encyclopedias and 2324 videos, made from Lastbook All', note)
        self.assertIn('Maps of 2 regions', note)                                        # what is on the drive now, not what the build wrote down as it began
        self.assertIn('1 of the videos were still being made smaller', note)

    def test_what_is_refused(self):
        with self.assertRaises(SystemExit):                                             # not a drive at all
            with mock.patch.object(sys, 'argv', ['make_drive.py', str(self.tmp / 'nowhere')]):
                make_drive.main()
        with mock.patch.object(drives, 'filesystem', return_value='fat32'), self.assertRaises(SystemExit):
            self.make()                                                                 # a format a library cannot live on
        self.assertEqual(list(self.drive.iterdir()), [])
        with self.assertRaises(SystemExit):
            self.make('--from', str(self.tmp))                                          # a folder with no app in it
        bad = self.tmp / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('bin/macos-arm64/../../outside.txt', 'x')
        with self.assertRaises(SystemExit):
            self.make('--from', str(bad))
        self.assertFalse((self.tmp / 'outside.txt').exists())
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('somewhere/else.txt', 'x')
        with self.assertRaises(SystemExit):
            self.make('--from', str(bad))


@unittest.skipIf(os.name == 'nt', 'the Mac and Linux launchers are shell scripts')
class Started(Base):
    def run_launcher(self, name, env=None, args=()):
        e = dict(os.environ, **(env or {}))
        return subprocess.run(['/bin/sh' if name.endswith('.sh') else '/bin/bash', str(self.drive / name), *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, timeout=60)

    def started(self, kind, program):
        p = self.drive / 'bin' / kind / Path(program).parent / 'started.txt'
        return p.read_text().splitlines() if p.is_file() else None

    @unittest.skipUnless(sys.platform == 'darwin' and os.uname().machine == 'arm64', 'needs an Apple silicon Mac')
    def test_the_mac_launcher_opens_the_library_beside_it(self):
        self.make('--from', str(self.bundle('macos-arm64', 'Lastbook.app/Contents/MacOS/Lastbook')))
        shutil.rmtree(self.drive / 'LastbookData')                                      # a drive someone emptied: the launcher makes it ready again
        out = self.run_launcher('Start-Mac.command', {'LASTBOOK_IN_TERMINAL': '1'})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        data = str(self.drive / 'LastbookData')
        self.assertEqual(self.started('macos-arm64', 'Lastbook.app/Contents/MacOS/Lastbook'), ['--data', data, '--portable'])
        self.assertTrue(portable.is_portable(self.drive / 'LastbookData'))

    @unittest.skipUnless(sys.platform == 'darwin', 'needs a Mac')
    def test_the_mac_launcher_says_when_the_app_is_not_there(self):
        self.make()
        out = self.run_launcher('Start-Mac.command', {'LASTBOOK_IN_TERMINAL': '1'})
        self.assertEqual(out.returncode, 1)
        self.assertIn('not on this drive', out.stdout)

    def linux(self, machine='x86_64'):
        """The Linux launcher, on a computer that says it is `machine`: a uname of our own, first on the path."""
        fake = self.tmp / 'fakebin'
        fake.mkdir(exist_ok=True)
        (fake / 'uname').write_text(f'#!/bin/sh\nif [ "$1" = "-m" ]; then echo {machine}; else /usr/bin/uname "$@"; fi\n')
        os.chmod(fake / 'uname', 0o755)
        return {'PATH': f'{fake}{os.pathsep}{os.environ["PATH"]}', 'TMPDIR': str(self.tmp / 'tmp')}

    def test_the_linux_launcher_opens_it_in_the_browser(self):
        self.make('--from', str(self.bundle('linux-x64', 'Lastbook/Lastbook')))
        out = self.run_launcher('Start-Linux.sh', self.linux(), args=('--no-open',))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.started('linux-x64', 'Lastbook/Lastbook'), ['--browser', '--data', str(self.drive / 'LastbookData'), '--portable', '--no-open'])

    def test_on_a_drive_that_lets_nothing_run_the_program_is_copied_off_it(self):
        self.make('--from', str(self.bundle('linux-x64', 'Lastbook/Lastbook')))
        program = self.drive / 'bin/linux-x64/Lastbook/Lastbook'
        os.chmod(program, 0o644)                                                        # as exFAT is usually mounted on Linux: nothing on it is a program
        (self.tmp / 'tmp').mkdir()
        out = self.run_launcher('Start-Linux.sh', self.linux())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('does not let programs run from it', out.stdout)
        self.assertIsNone(self.started('linux-x64', 'Lastbook/Lastbook'))               # not run from the drive
        self.assertEqual(list((self.tmp / 'tmp').iterdir()), [])                        # the copy is gone when Lastbook stops
        self.assertTrue(portable.is_portable(self.drive / 'LastbookData'))              # and the library is still the drive's

    def test_the_linux_launcher_says_what_it_cannot_do(self):
        self.make('--from', str(self.bundle('linux-x64', 'Lastbook/Lastbook')))
        out = self.run_launcher('Start-Linux.sh', self.linux('aarch64'))
        self.assertEqual(out.returncode, 1)
        self.assertIn('x86-64', out.stdout)
        shutil.rmtree(self.drive / 'bin')
        out = self.run_launcher('Start-Linux.sh', self.linux())
        self.assertEqual(out.returncode, 1)
        self.assertIn('not on this drive', out.stdout)


class Windows(Base):
    def test_the_windows_launcher_as_written(self):
        text = (LAUNCHERS / 'Start-Windows.bat').read_text(encoding='utf-8')
        self.assertIn('set "HERE=%~dp0"', text)                                         # where the file itself is: whatever letter the drive has
        self.assertIn('"%PROGRAM%" --window-or-browser --data "%DATA%" --portable', text)
        self.assertIn('set "DATA=%HERE%LastbookData"', text)                            # no trailing backslash before a closing quote
        self.assertNotIn('C:\\', text)
        text.encode('ascii')                                                            # cmd reads it in the computer's own code page: plain ASCII is the same in all

    @unittest.skipUnless(os.name == 'nt', 'needs Windows')
    def test_the_windows_launcher_starts_the_program(self):
        self.make()
        exe = self.drive / 'bin' / 'windows-x64' / 'Lastbook' / 'Lastbook.exe'
        exe.parent.mkdir(parents=True)
        shutil.copyfile(sys.executable, exe)                                            # any program will do: this one says what it was given
        out = subprocess.run(['cmd', '/c', str(self.drive / 'Start-Windows.bat')], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(portable.is_portable(self.drive / 'LastbookData'))


class Built(unittest.TestCase):
    def test_a_copy_with_no_links_in_it(self):
        tmp = Path(tempfile.mkdtemp(prefix='lb-plain-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        src = tmp / 'src'
        (src / 'a' / 'Versions' / '3').mkdir(parents=True)
        (src / 'a' / 'Versions' / '3' / 'lib').write_bytes(b'the library')
        (src / 'prog').write_text('#!/bin/sh\n')
        os.chmod(src / 'prog', 0o755)
        (src / '.DS_Store').write_text('x')
        if os.name != 'nt':
            os.symlink('Versions/3/lib', src / 'a' / 'lib')
            os.symlink('3', src / 'a' / 'Versions' / 'Current')
        build_portable.plain_copy(src, tmp / 'out')
        out = tmp / 'out'
        self.assertEqual([p for p in out.rglob('*') if p.is_symlink()], [])
        self.assertFalse((out / '.DS_Store').exists())
        if os.name != 'nt':
            self.assertEqual((out / 'a' / 'lib').read_bytes(), b'the library')          # what a link pointed to, as a file of its own
            self.assertEqual((out / 'a' / 'Versions' / 'Current' / 'lib').read_bytes(), b'the library')
            self.assertTrue(os.access(out / 'prog', os.X_OK))
            self.assertEqual(stat.S_IMODE((out / 'a' / 'Versions' / '3' / 'lib').stat().st_mode), 0o644)

    def test_the_folder_names_are_the_ones_the_app_looks_for(self):
        from engine.ai import runtime
        self.assertEqual(build_portable.platform_folder(), runtime.platform_folder())   # bin/<this> and models/runtime/<this>
        self.assertIn(build_portable.platform_folder().split('-')[0], ('macos', 'windows', 'linux'))
        for kind in make_drive.LAUNCHERS:
            self.assertTrue((LAUNCHERS / make_drive.LAUNCHERS[kind]).is_file())
        # The deepest program on a drive is still within reach of the library beside the launchers
        deepest = Path('bin/macos-arm64/Lastbook.app/Contents/MacOS/Lastbook')
        self.assertLessEqual(len(deepest.parts), portable.UP)


if __name__ == '__main__':
    unittest.main(verbosity=1)
