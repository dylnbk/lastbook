# Build your own library

Lastbook is published as three libraries: Small (8 GB), Medium (67 GB) and All (388 GB). All is everything, with
detailed maps of the whole United States, and nothing in between says "everything, but for Britain, on a drive I can
carry". This page describes what the fork adds: **a library of the parts a person chooses, with maps of the parts of
the world they choose and the AI model they choose, built on their own computer**.

Nothing is hosted for it. The parts come out of the published library, read in place (only what is kept is
downloaded); the maps are built from OpenStreetMap, the Copernicus elevation model and GeoNames
([WORLD-MAPS.md](WORLD-MAPS.md)); the model comes from another program on the computer or from Hugging Face
([AI-MODELS.md](AI-MODELS.md)).

## The five steps

The page is `web/wizard.html`, reached from setup ("Or build your own"), from Library and local AI, and from the map
("Add a map of another part of the world"). It works before there is any library, and from inside the app.

| Step | What is asked | What is checked |
|---|---|---|
| 1. Where | A drive that is plugged in, this computer, the library that is open, or another folder | The disk's format, its free room, whether it can be saved to, and what is there already |
| 2. AI | None; a model another program on this computer holds; one of three offered by name; or any Hugging Face repository | Whether each version fits this computer's memory, from the first 64 KB of the file, before anything is downloaded |
| 3. Library | Which published library to start from; documents by topic; encyclopedias one by one; videos by channel and topic | The size of every choice, live, against the disk |
| 4. Maps | Any of 554 regions of the world, each in outline or in full detail, with or without the shape of the land | An estimated size for each, from regions already built |
| 5. Build | How hard the computer may work, and a limit on downloads | That it fits, with the room the build needs while it works |

### Where

A library always gets a folder of its own, called `LastbookData`: choosing a drive or a plain folder puts one inside
it. What the disk's format means is said before anything is built:

| Format | Built on it | Where it then opens |
|---|---|---|
| exFAT | Yes | Mac, Windows, Linux; an iPad, iPhone or Android phone can read it |
| APFS, Mac OS Extended | Yes | A Mac only |
| NTFS | On Windows and Linux. A Mac can read it and not write it, so not from a Mac | Windows and Linux |
| ext4 and other Linux formats | Yes | Linux only |
| FAT32 | No: it cannot hold a file over 4 GB, and an encyclopedia or a country's map is larger | |
| A network folder | No: the search index needs a disk of the computer | |

**A library that travels** (ticked by default on a drive that unplugs) marks the library so that the app keeps
everything on its drive: notes, settings, the window's own state. Nothing is left on a computer it was opened on.

Choosing **the library that is open** adds maps or an AI model to it and changes nothing it holds. The open library
cannot be rebuilt while it is open: its catalog would be rewritten under the app.

### Videos, four ways

| Kept as | What is in the library | Size, for the All library's 2,324 videos |
|---|---|---|
| As they are | The published file | 232 GB |
| Made smaller | Re-encoded here as HEVC, 480p (360p for long talks to camera). A video already under 400 kbps is left alone | about 94 GB |
| Words only | No picture. The transcript, captions and thumbnail: still searched, still cited | 0.3 GB |
| Left out | Nothing | 0 |

Making videos smaller is the long part of a build. The All library has 364 hours of video over 400 kbps (1,547
videos); from the speeds measured on a 90-second piece, that is an estimated two days of an Apple M4's time. The
library opens as soon as its documents are in. A video still waiting its turn is shown by its words, with a line
saying so, and gets its picture when it is done. `engine/wizard/video.py` has the measurements behind the settings.

### What a build needs beyond what it leaves

The page counts these and shows them as "more room while it builds":

- From a published library: the whole of its search index (0.8 GB for All), from which the new library's own is
  made, and one original video at a time.
- For maps: each region's OpenStreetMap extract, its elevation cells, and the world's coastline (0.9 GB to download,
  2.3 GB unpacked), needed once however many regions there are. They are kept in `.lastbook/map-sources` beside the
  library, on its drive, and deleted when every map is built.
- While a map is built, a list of where every point of it is: up to 10 GB for a large country, in the computer's
  temporary folder, not on the drive. An exFAT drive on a Mac loses pages written through a memory map, and nothing
  reports it; [WORLD-MAPS.md](WORLD-MAPS.md) has how that was found. It is deleted as each region finishes.

## The build

The build runs in a process of its own (`engine/wizard/worker.py`): making a map keeps a processor busy for an hour,
and the app has to stay quick meanwhile. In this order, each step picking up where a stopped run left off:

1. **Maps**: each region not already built.
2. **Documents**: everything kept as it is, each file checked against the published SHA-256.
3. **Catalog**: the catalog, the search index and the counts. From here the library opens.
4. **AI model**: copied from the other program, or downloaded and checked.
5. **Videos**: one at a time, smallest first; the catalog is rewritten every five minutes as they finish.
6. **Finishing**: `pack-manifest.tsv`, listing every file with its size and SHA-256, as a published library has.

A file that does not match the published checksum is left out, and so is its catalog record: the app is never told
of something it cannot open. A map that fails, or a model that cannot be had, is a line under "problems" and does
not cost the rest of the library.

**How hard the computer may work**

| | Processor threads for video | Priority | Elevation tiles |
|---|---|---|---|
| Gently | 2 | Background (`taskpolicy -b` on a Mac: the efficiency cores, and the back of the queue for the disk) | 1 process |
| Normally | a little over half | Low (`nice 10`, and the video encoder lower still) | 2 |
| As fast as it can | all | `nice 5` | 4 |

Downloads can be limited to 2, 5, 10 or 20 MB a second.

**Pause, cancel, close.** Pause stops at the next file. Cancel stops and forgets the build; what was built stays in
its folder for the person to delete or to finish later. Closing Lastbook pauses the build: it is never left running
with no window to say so. Opening the library that is being built restarts the app, and the build carries on.

## Without the window

```
python tools/build_library.py --menu --from-folder /path/to/LastbookData
python tools/build_library.py --target /Volumes/Drive/LastbookData --from-url https://downloads.lastbook.org/Lastbook-All.zip --choice choice.json
```

`choice.json` is what the page sends:

```json
{"documents": "all", "encyclopedias": "all",
 "videos": {"default": "small", "channel:Corporals Corner": "words", "topic:firearms": "none"},
 "maps": [{"id": "great-britain", "level": "detail"}, {"id": "france", "level": "overview"}],
 "ai": {"mode": "download", "repo": "Qwen/Qwen3-VL-4B-Instruct-GGUF"},
 "portable": true}
```

With no `version`, the one Lastbook would suggest for the repository is brought.

## The contract: `/api/wizard/*`

Answered only to the computer Lastbook is open on. A phone the library is shared with gets 403.

| | | |
|---|---|---|
| GET | `state` | This computer, the drives plugged in, the libraries to build from, a build under way or left unfinished |
| POST | `pick` | The system's folder chooser, then as `target` |
| POST | `target` `{path}` | `{path, ok, note, filesystem, format, travels, free_bytes, writable, library, portable, is_current, built}` |
| POST | `menu` `{source}` | What a library holds to choose from, with sizes |
| POST | `regions` `{target}` | The regions of the world, and those built there already |
| POST | `ai` `{target}` | Models on this computer and whether each fits; the three offered by name |
| POST | `inspect` `{repo}` | What a Hugging Face repository holds |
| POST | `plan` `{source, choice, target}` | `{bytes, download_bytes, working_bytes, free_bytes, fits, short_bytes, counts, encode_hours, map_minutes, ...}` |
| POST | `start` `{source, choice, target, effort, rate}` | The build's status |
| GET | `status` | `{state, phase, message, done_bytes, total_bytes, videos_done, videos_total, problems, job}` |
| POST | `stop` `{how}` | `pause` or `cancel` |
| POST | `resume` | Carries on the build that was paused |
| POST | `reopen` | The library being built is about to be opened: the build carries on when the app is back |

A `source` is `{"kind": "pack", "id": "all"}` (a published library, by its id in the setup manifest),
`{"kind": "folder", "path": ...}` (a library on a disk of this computer) or `{"kind": "keep"}` (the library in the
target, left as it is). The page never names an address to download from: the worker is given the address the
manifest holds, and the downloader goes only to the hosts it knows.

A choice is read, not trusted: only the keys a build understands reach the worker, each checked.

## What is not done

- **Sizes of maps are estimates**, from 4 regions built in full detail and 30 in outline. In full detail the four
  came out within a tenth of the estimate. In outline a region has come out from under half the estimate (Denmark,
  Belgium) to over twice it (Monaco, Iceland): how much of an extract is buildings, which an outline leaves out,
  differs that much from country to country.
- **Hours to make videos smaller are a guess** from one computer, scaled by the number of processor cores.
- **Maps are built only where `osmium` is installed.** A copy without it builds the rest of the library and says so.
- **HEVC video has been played only on the Mac it was made on.** Apple's and Android's players have read it for
  years. On Windows the app's window plays it only where the computer decodes HEVC in hardware, which has not been
  tried here. Where a video cannot be played, the app says which format it is and where the file is; its transcript
  is on the same page.
- **Windows and Linux are untested** for the page, the drive listing and the worker: the code for them was written
  from their documentation.
- The download server answered `503` to about a third of requests while one computer held ten connections to it.
  The downloader waits and tries again (six times, up to 30 s apart); the page offers "Try again" when it gives up.

## Where it is

| File | What |
|---|---|
| `web/wizard.html`, `web/wizard.js`, `web/wizard.css` | The page |
| `engine/wizard/api.py` | `/api/wizard/*` |
| `engine/wizard/worker.py` | One build, in a process of its own |
| `engine/wizard/build.py` | The build's steps |
| `engine/wizard/contents.py` | What a library holds to choose from, and what a choice comes to |
| `engine/wizard/video.py` | Making a video smaller |
| `engine/wizard/library.py` | Writing a catalog, a search index and a manifest |
| `engine/wizard/packzip.py` | Reading a published library in place |
| `engine/wizard/drives.py` | The disks plugged in, and what each format means |
| `engine/wizard/hf_import.py` | A model from Hugging Face |
| `tools/build_library.py` | The same build without the window |
| `app/tests/test_build.py`, `app/tests/test_builder.py` | Tests: a made-up library, real few-second videos, a build run, paused and carried on |
