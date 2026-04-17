#!/usr/bin/env bash
# Wrapper: runs serato-crates against the default music folder.
# Usage: ./sync.sh                  # dry-run preview
#        ./sync.sh --apply          # write crates
#        ./sync.sh --apply --clean  # wipe existing crates (with backup) and write fresh

set -e
cd "$(dirname "$0")"
exec .venv/bin/serato-crates sync \
    --music-root "$HOME/Music/_DJ MUSIC/DJ - C" \
    "$@"
