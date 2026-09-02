"""Normalise French high-punctuation spacing in the Cosmochrony website pages.

French typography requires a non-breaking space before ``:``, ``;``, ``!`` and ``?``.
The programme's convention (PREFERENCES.md) is a literal U+00A0, not the ``&nbsp;`` entity and not a
plain space.

The character is written here as an explicit ``\\u00a0`` escape on purpose: a literal non-breaking
space pasted into a shell heredoc does not survive reliably, which silently turns a normalisation
pass into a corruption pass.

Scope, deliberately narrow:

* only ``*_fr.html`` files;
* text nodes, plus the human-readable attributes ``content``/``title``/``alt``/``aria-label``;
* never inside ``<script>``/``<style>``, never inside inline maths, never inside a URL-valued
  attribute.

Usage::

    python3 tools/fr_typography.py --check <root>
    python3 tools/fr_typography.py --write <root>
"""

import argparse
import os
import re
import sys

NBSP = "\u00a0"
PUNCT = ";:!?"

SKIP_BLOCK = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]*>", re.S)
MATH = re.compile(
    r"\\\((?:(?!\\\)).)*?\\\)"      # \( ... \)
    r"|\\\[(?:(?!\\\]).)*?\\\]"     # \[ ... \]
    r"|\$[^$\n]*\$",                # $ ... $ on one line
    re.S,
)
ATTR = re.compile(r'\b(content|title|alt|aria-label)="([^"]*)"', re.S)
URLISH = re.compile(r"\s*(https?:|/|#|width=|[\d.]+\s*$)")

# A plain space, or the entity, sitting before high punctuation.
LOOSE = re.compile(r"(?:&nbsp;|[ \t])+([" + PUNCT + r"])")
# Already correct: exactly one NBSP.
TIGHT = re.compile(NBSP + r"([" + PUNCT + r"])")
# NBSP with stray whitespace around it, from a botched earlier pass.
SLOPPY = re.compile(r"(?:[ \t]|" + NBSP + r")*" + NBSP + r"(?:[ \t]|" + NBSP + r")*([" + PUNCT + r"])")


def normalise(fragment):
    """Return (fixed fragment, number of repairs) for one non-markup, non-maths fragment."""
    fragment, a = SLOPPY.subn(NBSP + r"\1", fragment)
    fragment, b = LOOSE.subn(NBSP + r"\1", fragment)
    return fragment, a + b


def split_maths(text):
    """Yield (fragment, is_maths) pairs so maths is never touched."""
    last = 0
    for m in MATH.finditer(text):
        yield text[last:m.start()], False
        yield m.group(0), True
        last = m.end()
    yield text[last:], False


def process(src):
    blocks = []

    def stash(m):
        blocks.append(m.group(0))
        return "\x01%d\x01" % (len(blocks) - 1)

    s = SKIP_BLOCK.sub(stash, src)

    out, repairs, last = [], 0, 0
    for m in TAG.finditer(s):
        for frag, is_maths in split_maths(s[last:m.start()]):
            if is_maths:
                out.append(frag)
            else:
                frag, k = normalise(frag)
                out.append(frag)
                repairs += k
        tag, k = fix_attributes(m.group(0))
        out.append(tag)
        repairs += k
        last = m.end()
    for frag, is_maths in split_maths(s[last:]):
        if is_maths:
            out.append(frag)
        else:
            frag, k = normalise(frag)
            out.append(frag)
            repairs += k

    result = "".join(out)
    result = re.sub(r"\x01(\d+)\x01", lambda m: blocks[int(m.group(1))], result)
    return result, repairs


def fix_attributes(tag):
    repairs = 0

    def rep(m):
        nonlocal repairs
        name, value = m.group(1), m.group(2)
        if URLISH.match(value):
            return m.group(0)
        parts = []
        for frag, is_maths in split_maths(value):
            if is_maths:
                parts.append(frag)
            else:
                frag, k = normalise(frag)
                parts.append(frag)
                repairs += k
        return '%s="%s"' % (name, "".join(parts))

    return ATTR.sub(rep, tag), repairs


def residue(src):
    """Count places still carrying a plain space or entity before high punctuation."""
    s = SKIP_BLOCK.sub(" ", src)
    total, last = 0, 0
    chunks = []
    for m in TAG.finditer(s):
        chunks.append(s[last:m.start()])
        for am in ATTR.finditer(m.group(0)):
            if not URLISH.match(am.group(2)):
                chunks.append(am.group(2))
        last = m.end()
    chunks.append(s[last:])
    for chunk in chunks:
        for frag, is_maths in split_maths(chunk):
            if not is_maths:
                total += len(LOOSE.findall(frag))
    return total


def french_pages(root):
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude", ".codex")]
        found += [os.path.join(base, f) for f in files if f.endswith("_fr.html")]
    return sorted(found)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--write", action="store_true", help="apply the repairs")
    ap.add_argument("--check", action="store_true", help="report the residue only")
    args = ap.parse_args()

    pages = french_pages(args.root)
    if args.check or not args.write:
        left = {p: residue(open(p, encoding="utf-8", errors="replace").read()) for p in pages}
        bad = {p: n for p, n in left.items() if n}
        print("french pages: %d   non-compliant occurrences: %d in %d files"
              % (len(pages), sum(bad.values()), len(bad)))
        for p, n in sorted(bad.items(), key=lambda kv: -kv[1])[:15]:
            print("   %4d  %s" % (n, p))
        return 1 if bad else 0

    total, touched = 0, 0
    for p in pages:
        src = open(p, encoding="utf-8").read()
        out, k = process(src)
        if k and out != src:
            open(p, "w", encoding="utf-8").write(out)
            total += k
            touched += 1
    print("french pages: %d   files repaired: %d   repairs: %d" % (len(pages), touched, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
