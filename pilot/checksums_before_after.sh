#!/bin/sh
# READ-ONLY state snapshot for the sandbox pilot. Never deletes, never modifies, never contacts the network.
#
#   sh tools/pilot/checksums_before_after.sh snapshot LABEL PAPER_DIR WORKSPACE_ROOT OUT_DIR
#       writes OUT_DIR/LABEL.snapshot (refuses to overwrite an existing file) and prints it
#   sh tools/pilot/checksums_before_after.sh compare BEFORE.snapshot AFTER.snapshot
#       prints the differences; exit 0 only when the two snapshots are identical
#
# A snapshot records: shasum -a 256 of the paper's zenodo.json, of every file of the central pdfs/ archive
# and of every cosmochrony-bibliography.bib under the workspace; the listing of pdfs/; `git status
# --porcelain` of the paper repository and of the tools repository.
set -u
set -C        # noclobber: a redirection never overwrites (nor follows a dangling symlink onto) an existing name

die() { echo "checksums_before_after: $*" >&2; exit 2; }

snapshot() {
    label=$1; paper=$2; root=$3; out=$4
    # LABEL names a file inside OUT_DIR: letters, digits, dot, underscore, dash; no separator, no leading dot or dash
    case "$label" in
        ""|.*|-*|*[!A-Za-z0-9._-]*) die "invalid label (letters, digits, '.', '_', '-' only; not empty, no leading '.' or '-'): $label" ;;
    esac
    [ -d "$paper" ] || die "paper directory not found: $paper"
    [ -d "$root" ] || die "workspace root not found: $root"
    [ -d "$out" ] || die "output directory not found: $out"
    dest="$out/$label.snapshot"
    if [ -e "$dest" ] || [ -L "$dest" ]; then die "refusing to overwrite $dest"; fi
    {
        echo "## zenodo.json"
        shasum -a 256 "$paper/zenodo.json" 2>&1
        echo "## pdfs/ listing"
        ls -l "$root/pdfs" 2>&1 | awk '{ $6=""; $7=""; $8=""; print }'
        echo "## pdfs/ checksums"
        find "$root/pdfs" -type f -print 2>/dev/null | sort | while read -r f; do shasum -a 256 "$f"; done
        echo "## bibliographies"
        find "$root" -name cosmochrony-bibliography.bib -not -path '*/.git/*' -print 2>/dev/null | sort \
            | while read -r f; do shasum -a 256 "$f"; done
        echo "## git status paper"
        git -C "$paper" status --porcelain 2>&1
        echo "## git status tools"
        git -C "$root/tools" status --porcelain 2>&1
    } > "$dest"
    cat "$dest"
}

compare() {
    [ -f "$1" ] && [ -f "$2" ] || die "both snapshots must exist"
    if diff -u "$1" "$2"; then
        echo "IDENTICAL: no production file changed"
        exit 0
    fi
    echo "DIFFERENT: any production file change is a NO-GO" >&2
    exit 1
}

case "${1:-}" in
    snapshot) [ $# -eq 5 ] || die "usage: snapshot LABEL PAPER_DIR WORKSPACE_ROOT OUT_DIR"; shift; snapshot "$@" ;;
    compare)  [ $# -eq 3 ] || die "usage: compare BEFORE AFTER"; shift; compare "$@" ;;
    *) die "usage: snapshot|compare ..." ;;
esac
