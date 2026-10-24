#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Reassembling split MOV files..."
for part in *.MOV.part_aa; do
    [ -f "$part" ] || continue
    base="${part%.part_aa}"
    echo "  Reassembling $base..."
    cat "${base}.part_"* > "$base"
done

if [ -f checksums.sha256 ]; then
    echo "Verifying checksums..."
    shasum -a 256 -c checksums.sha256
fi
echo "Done! All files ready."
