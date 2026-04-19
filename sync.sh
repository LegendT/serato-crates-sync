#!/usr/bin/env bash
# Wrapper: runs serato-crates against a music folder.
# Usage: ./sync.sh                                     # dry-run preview (default folder)
#        ./sync.sh --apply                             # write crates
#        ./sync.sh --apply --clean                     # wipe existing crates (with backup) and write fresh
#        MUSIC_ROOT="/path/to/other" ./sync.sh --apply # override music folder for this run

set -e
cd "$(dirname "$0")"
exec .venv/bin/serato-crates sync \
    --music-root "${MUSIC_ROOT:-$HOME/Music/DJ}" \
    "$@"
