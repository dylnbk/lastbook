#!/bin/bash
# Lastbook, from this drive. Double-click it on a Mac with Apple silicon (M1 or later).
# It opens the library in the LastbookData folder beside this file, and keeps everything it writes on this drive.
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/bin/macos-arm64/Lastbook.app"
PROGRAM="$APP/Contents/MacOS/Lastbook"
DATA="$HERE/LastbookData"

if [ "$(uname -m)" != arm64 ]; then
  echo "This drive's Mac app is for Apple silicon (M1 or later). This Mac has an Intel processor."
  echo "Press Return to close."; read -r _; exit 1
fi
if [ ! -f "$PROGRAM" ]; then
  echo "The Mac app is not on this drive: $APP"
  echo "It is made with packaging/portable/build_portable.py and put here with packaging/portable/make_drive.py."
  echo "Press Return to close."; read -r _; exit 1
fi
# A drive formatted for every computer keeps no note of which files are programs. On a Mac they all count as programs
# already; this is for a copy of the drive made onto a disk that does keep that note.
chmod +x "$PROGRAM" "$APP/Contents/Frameworks/llm/llama-server" 2>/dev/null
mkdir -p "$DATA"
[ -f "$DATA/portable.json" ] || printf '{\n "portable": true,\n "layout": 1\n}\n' > "$DATA/portable.json"

# As an app of its own, so this window can close. If the system will not open it that way, straight from here.
# To see what Lastbook says as it starts, keep it in this window:  LASTBOOK_IN_TERMINAL=1 ./Start-Mac.command
if [ -z "$LASTBOOK_IN_TERMINAL" ] && /usr/bin/open -n -a "$APP" --args --data "$DATA" --portable 2>/dev/null; then
  exit 0
fi
exec "$PROGRAM" --data "$DATA" --portable "$@"
