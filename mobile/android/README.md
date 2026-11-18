# Lastbook for Android

A small Android app that opens a Lastbook library where it is: on a drive plugged into the phone or tablet, on a
memory card, or on the device itself. Nothing is copied.

It is a WebView around **the desktop's own engine**. The Python that runs on a computer (`server.py`, `engine/`) runs
inside the app ([Chaquopy](https://chaquo.com/chaquopy/)) and serves the same pages to the app's own WebView at
`127.0.0.1`. So what the desktop does with a library, this does: search, reading, videos, maps of any region built
from open data, the place finder.

## How far this has been tried

It builds, and it runs on a **virtual** Android 14 phone, where the engine inside it gave the same answers as the
desktop's. It has **never been run on a real phone or tablet, nor with a drive plugged in**. The last section, "What
was tried", has the details and what is still not known.

## What is not in it

- **Encyclopedias (ZIM).** `libzim` has no build for Android, so they do not open, as on iPhone and iPad.
- **An AI model.** There is no `llama-server` for a phone in this app. Chat shows the pages that match.
- **Setup and the builder.** A library is made on a computer (Build your own library, [docs/WIZARD.md](../../docs/WIZARD.md)).
  The app says so when it finds none.
- **A way round "All files access".** A library is ordinary files in ordinary folders, read in place by ordinary code,
  so the app asks for `MANAGE_EXTERNAL_STORAGE` and the person grants it by hand in Settings. Google Play restricts
  that permission to a few kinds of app; this one is meant to be installed from a file (`Install-Android.apk` on the
  drive), not from a store.

## What it asks of Android

| Permission | Why |
|---|---|
| `MANAGE_EXTERNAL_STORAGE` | To read the library where it is, on a drive or a card. Granted by hand. |
| `INTERNET` | Android asks for it for any socket, the one at `127.0.0.1` between the WebView and the engine included. The app has no page that starts a download (no setup, no builder), plain http is allowed to `127.0.0.1` only (`res/xml/network.xml`), and the WebView follows no link that leaves `127.0.0.1` (`MainActivity.kt`). |

Android 11 or later (the first with "All files access").

## Build

```
cd mobile/android
./gradlew assembleRelease
# app/build/outputs/apk/release/app-release.apk  ->  Install-Android.apk at the top of a drive
```

Needs a JDK (17) and the Android SDK (`ANDROID_HOME`): platform 35 and build-tools 35. The first build downloads the
Android Gradle plugin, Kotlin, Chaquopy and its Python for Android.

The engine's Python and the pages are taken from the repository at build time (`syncEngine`, `syncPages` in
`app/build.gradle`): the same files the desktop runs, with no tests, tools or packaging.

There is no release key. The build is signed with the build machine's debug key so that it installs at all. A copy
built on another machine is signed differently, and Android will not install one over the other.

## Where things are

| File | What |
|---|---|
| `app/src/main/java/org/lastbook/portable/MainActivity.kt` | The one screen: asks for the permission, finds libraries, opens one, keeps the WebView to `127.0.0.1` |
| `app/src/main/python/lastbook_android.py` | Finds libraries, starts the engine, says what this Python's SQLite can do |
| `app/build.gradle` | What goes in: the engine, the pages, and the Python packages |
| `engine/paths.py` (`LASTBOOK_APP`), `server.py` (`LASTBOOK_NO_SETUP`) | The two things the engine was taught for this app |

## What was tried

Built on a Mac (18 Nov 2026) with the Android SDK's platform 35, Gradle 8.10.2, the Android Gradle plugin 8.7.3, Kotlin
2.0.21 and Chaquopy 16.0.0: `Install-Android.apk`, 68 MB, for `arm64-v8a` and `x86_64`, Python 3.12.7 inside.

Run on the SDK's emulator (Android 14, arm64, a Pixel 5's screen), with a library of 81 documents, 29 videos kept by
their words, and the map of the Isle of Man in full detail (302 MB) copied to the device's own storage:

| | |
|---|---|
| Installs, starts, finds the library and says what it holds | Yes |
| Its Python's SQLite has what search and maps need | FTS5 yes, R*Tree yes (SQLite 3.45.3). The first screen says so on every device |
| The engine answers as the desktop's does | 23 of 24 requests the same, byte for byte or value for value, against `server.py` on a Mac with the same library: search, the guides, map tiles at four zooms, elevation, help points, nearest, the place finder (a postcode included), a document, a transcript. The one that differs is the page itself, which carries a value made afresh each run |
| A page of a PDF | Drawn by Android's `PdfRenderer`: the same page as pdfium draws on the desktop, one pixel narrower (565 by 800 against 566). The first attempt failed on every page: the call that paints the page white went to the wrong one of two Java methods of the same name. Found by running it |
| The pages on a phone's screen | The Videos tab and the Map tab were opened and looked at: the phone layout, the tab bar, the map of the island with its help points |
| What the person saves | Goes into the library (`LastbookData/user/`), as on a computer |
| Setup and the builder | Not offered, and the engine answers 404 for them |
| A request under another site's name | Refused (403) |

**Not tried, so not known:**

- A real phone or tablet, of any make.
- A drive plugged in. The app looks in every storage volume Android reports and in `/storage/*`; whether a given phone
  mounts an exFAT drive there, and lets an app with "All files access" write to it, differs by maker.
- Granting "All files access" by hand. In the emulator it was granted from the command line.
- Playing a video. HEVC is decoded by the phone's own chip on most phones of the last several years.
- A large library. The one tried was 302 MB; the catalog of the All library is 12 MB of JSON, read into memory at start.
- Anything on Android 11, 12 or 13.

`pip` could not install `pypdfium2` for Android (it found only the source, which does not build there), which is why
a PDF's page is drawn by Android itself. `libzim` was not tried: it has no Android build listed.
