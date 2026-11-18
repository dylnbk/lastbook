# A drive that opens on any computer

Lastbook was installed: an app on one computer, its library in that computer's folders. This page describes what the
fork adds: **a drive that holds the library and the program for each kind of computer, opened with one double-click,
leaving nothing behind on the computer it was opened on.**

```
<drive>/
  Start-Mac.command        double-click on a Mac (Apple silicon)
  Start-Windows.bat        double-click on Windows (64-bit)
  Start-Linux.sh           sh Start-Linux.sh, on Linux (x86-64)
  READ-ME-FIRST.txt        what the drive is, for whoever finds it
  Install-Android.apk      the Android app, when one was built
  bin/
    macos-arm64/Lastbook.app
    windows-x64/Lastbook/Lastbook.exe
    linux-x64/Lastbook/Lastbook
  LastbookData/            the library, marked as one that travels (portable.json)
    user/                  what the person keeps: notes, saved pages, chats, settings
    models/                AI models, and models/runtime/<os>-<arch>/ the program that runs them
  .lastbook/               the program's own working state, on the drive and not on the computer
```

Each launcher finds the drive from where it sits itself, so the drive's name or letter does not matter. It starts the
program for that computer with `--data <drive>/LastbookData --portable`. A library that travels keeps everything on
its drive ([engine/portable.py](../engine/portable.py)).

## What has been tried, and what has not

| | Built | Run |
|---|---|---|
| Mac (Apple silicon) | On this Mac, from the source | From a 1 TB exFAT drive, by its launcher, on the Mac it was built on: the app's own window, on a library of 12,851 records |
| Windows (64-bit) | **Not yet.** The workflow below builds it | **Never** |
| Linux (x86-64) | **Not yet.** The workflow below builds it | The launcher, with a stand-in for the program, on a Mac. The program itself: **never** |
| Android | On this Mac: `Install-Android.apk`, 68 MB ([mobile/android](../mobile/android/README.md)) | On a virtual Android 14 phone, where its engine gave the desktop's answers. **Never on a real phone, nor with a drive plugged in** |
| iPhone and iPad | See [mobile/ipad](../mobile/ipad/README.md) | |

The Windows and Linux programs are the same Python as the Mac's and the same code, and the part of it they open with
(the browser's way, below) is tested on the Mac. But a program that has never been started on a system is not known to
work there. `.github/workflows/build-portable.yml` builds all three and starts each one on a made-up library before
keeping it: until that workflow has run green on the fork, read "Windows" and "Linux" here as intended.

**Found by running the Mac app from the drive:**

- The first start of a program from the drive takes about 35 seconds before anything shows (37 and 34, for two
  builds): macOS checks every new program once. At the next start the app was running its own code 0.8 seconds
  after the launcher.
- The app left `~/Library/WebKit/local.lastbook.portable/ContentRuleLists` on the Mac, an empty folder with its name
  on it, though the rule list itself was on the drive. Asking WebKit for its usual store, even as a fallback never
  used, makes the folder. It is now asked for only if the drive's store fails. After the fix: nothing under
  `~/Library` has "lastbook" in its name after a run.
- PyInstaller's own `Lastbook.app` cannot live on an exFAT drive: it keeps programs in one folder and everything else
  in another, each linked into the other (89 links), and exFAT has no links. Written out as plain files it is 286 MB
  where the app is 113, and no longer signs. The drive's `Lastbook.app` is put together by
  [build_portable.py](../packaging/portable/build_portable.py) instead: 148 MB, no links, each program inside still
  carrying the signature it was built with.

## Two ways of showing the app

**A window of its own**, as the installed app has: WebKit on a Mac, WebView2 on Windows. No network port is opened.

**A tab in the computer's web browser**, where there is no window Lastbook can make ([app/shell_browser.py](../app/shell_browser.py)):
Linux always (the Linux window needs the system's own Python with GTK and WebKitGTK, which a program on a drive cannot
count on), and Windows when WebView2 is not there. The pages are the same. What differs:

- It listens on `127.0.0.1` only, on a port the system chooses. Nothing else on the network can reach it, but other
  programs on that computer can ask it what the browser asks.
- A request must name `localhost` or an address, never another name, and a change must come from the page itself:
  the two things a page on some other site would have to get past.
- A box ("Lastbook is running") or the terminal stays open while it runs. Closing it stops Lastbook.
- There is no folder chooser: where a folder is asked for, it is typed.

`Lastbook --browser` asks for this way anywhere; `--window-or-browser` uses the window where it can.

## Linux and a drive formatted for every computer

Linux usually mounts an exFAT drive so that only files ending `.exe`, `.com` or `.bat` count as programs. The launcher
is therefore run as `sh Start-Linux.sh`, and when the program itself may not be run from the drive it is copied to the
computer's temporary folder (about 150 MB), run from there, and deleted when Lastbook stops. The library and
everything saved stay on the drive.

## Making a drive

```
# 1. The program for this computer (and a zip of it)
python packaging/portable/build_portable.py --out /path/to/build --llama /folder/with/llama-server --smoke /a/small/LastbookData

# 2. Onto the drive: launchers, READ-ME-FIRST.txt, bin/, and the mark that makes its library one that travels
python packaging/portable/make_drive.py /Volumes/Drive --from /path/to/build \
       --from Lastbook-portable-windows-x64.zip --from Lastbook-portable-linux-x64.zip [--apk Install-Android.apk]

# 3. The library: open Lastbook from the drive and choose "Build your own library", or
python tools/build_library.py --target /Volumes/Drive/LastbookData --from-url https://downloads.lastbook.org/Lastbook-All.zip --choice choice.json
```

`build_portable.py` needs a Python with [packaging/portable/requirements.txt](../packaging/portable/requirements.txt);
that Python goes inside the app. On a Mac its `--out` must be on a disk formatted for a Mac. `--smoke` starts what was
built on a library and asks it three questions; the build fails if it does not answer.

`make_drive.py` refuses a drive formatted FAT32 (no file over 4 GB) and says what any other format means. It replaces
an app on the drive whole, and touches nothing else there.

The fork's GitHub Actions (`Portable drive`) run step 1 on macOS, Windows and Linux and put the three together as
`Lastbook-drive-kit.zip`: unzip it onto a drive beside its `LastbookData`.

## The program that runs AI models

`llama-server` (llama.cpp) is looked for in the library first (`LastbookData/models/runtime/<os>-<arch>/`), then inside
the app (`llm/`), then on the computer. The workflow builds it from source at the build upstream pins (`b12032`) and
puts it inside each app: Metal on a Mac, the processor alone on Windows and Linux.

On the Mac this was written on, the one inside the app and on the drive is a copy of AnythingLLM's (build 11425). The
app's downloader could not fetch llama.cpp's own releases: GitHub's certificates do not verify against the roots the
downloader is limited to.

## Not done

- **Nothing is signed with a certificate.** The Mac app is ad hoc signed, the Windows program unsigned. A Mac may ask
  for the launcher to be opened with Control-click the first time; Windows SmartScreen will warn about the program.
- **No Start-Windows.exe.** The plan named one; a command file needs no compiler and does the same.
- **Intel Macs, Windows on Arm and Linux on Arm** have no program on the drive. Their launchers say so.
- **Vulkan** for AI models on Windows and Linux: the workflow builds for the processor only.

## Where it is

| File | What |
|---|---|
| `packaging/portable/launchers/` | The three launchers |
| `packaging/portable/build_portable.py`, `lastbook_portable.spec`, `requirements.txt` | The program for one kind of computer |
| `packaging/portable/make_drive.py` | Putting a drive together |
| `app/shell_browser.py` | The app in a browser tab |
| `app/main.py` | `--portable`, `--browser`, `--window-or-browser` |
| `.github/workflows/build-portable.yml` | Tests on three systems, the three programs, the Android app, the kit |
| `app/tests/test_browser.py`, `app/tests/test_drive.py`, `app/tests/test_portable.py` | Tests |
