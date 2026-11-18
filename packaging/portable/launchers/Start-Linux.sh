#!/bin/sh
# Lastbook, from this drive, on Linux (x86-64). Run it from a terminal:  sh Start-Linux.sh
# (a drive formatted for every computer cannot mark a file as a program, so a double-click may not start it).
# It opens the library in the LastbookData folder beside this file, in this computer's web browser, at an address only
# this computer can reach, and keeps everything it writes on this drive.
HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="$HERE/bin/linux-x64/Lastbook"
PROGRAM="$BUNDLE/Lastbook"
DATA="$HERE/LastbookData"

case "$(uname -m)" in
  x86_64|amd64) ;;
  *) echo "This drive's Linux program is for x86-64 computers. This one is $(uname -m)."; exit 1 ;;
esac
if [ ! -f "$PROGRAM" ]; then
  echo "The Linux program is not on this drive: $BUNDLE"
  echo "It is built by the fork's GitHub Actions (Lastbook-portable-linux-x64.zip) and put here with packaging/portable/make_drive.py."
  exit 1
fi
mkdir -p "$DATA"
[ -f "$DATA/portable.json" ] || printf '{\n "portable": true,\n "layout": 1\n}\n' > "$DATA/portable.json"

if [ ! -x "$PROGRAM" ]; then
  # The drive is mounted so that nothing on it may run (the usual way for exFAT on Linux), or keeps no note of what is a
  # program. The program is copied to this computer's temporary folder and run from there; the library stays on the drive.
  RUN="${TMPDIR:-/tmp}/lastbook-$(id -u)"
  echo "This drive does not let programs run from it, so Lastbook's program is copied to $RUN (about 150 MB) and run from there."
  echo "It is deleted when Lastbook stops. The library and everything you save stay on the drive."
  rm -rf "$RUN"
  mkdir -p "$RUN" && chmod 700 "$RUN" && cp -R "$BUNDLE" "$RUN/" || { echo "It could not be copied."; exit 1; }
  chmod -R u+rwX "$RUN"
  chmod +x "$RUN/Lastbook/Lastbook" "$RUN/Lastbook/_internal/llm/llama-server" 2>/dev/null
  trap 'rm -rf "$RUN"' EXIT INT TERM
  "$RUN/Lastbook/Lastbook" --browser --data "$DATA" --portable "$@"
  exit $?
fi
exec "$PROGRAM" --browser --data "$DATA" --portable "$@"
