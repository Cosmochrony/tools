#!/usr/bin/env python3
"""Pre-deposit check battery for Cosmochrony papers.

Runs the mandatory Step-3 checks of the release procedure against a paper
directory and reports every problem that would make a Zenodo deposit wrong,
misleading, or irreproducible. A Zenodo record cannot be deleted once
published, so these checks exist to fail *before* the upload, not after.

Usage
-----
    python3 tools/predeposit_check.py <paper_dir> [<paper_dir> ...]
    python3 tools/predeposit_check.py --all           # every paper with a zenodo.json

Exit code is 0 when every paper passes, 1 otherwise.

It is also imported by zenodo_publish.py, which gates the upload on it
(bypass with --skip-checks).

What it catches
---------------
Every check below corresponds to a defect actually found in this corpus:

* stale ``version`` field in zenodo.json (it takes precedence over the git
  branch, so five deposits were about to be published under the previous
  version label);
* ``CITATION.cff`` left behind at an older version;
* a PDF cover that self-labels a different version than the branch;
* a build log that is older than its sources (a redirect into a blocked path
  left a previous log in place, which then read as a clean build);
* a log grep that silently returns nothing because the log is ISO-8859 rather
  than UTF-8 -- the sanity probe below fails loudly instead;
* undefined references/citations and LaTeX errors that ``-interaction=nonstopmode``
  lets through while still writing a plausible PDF;
* empty PDF metadata;
* a custom preamble macro that survived abstract expansion (a tokenisation bug
  once turned ``\\log\\rpair`` into the undefined ``\\logr_{...}``);
* tildes and ``\\cite`` leaking into the Zenodo description;
* a version branch that exists only on the local disk.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Patterns from the release procedure. Both need `grep -a`: some logs are
# ISO-8859 and grep without -a prints nothing at all -- not even 0 -- which
# reads exactly like "no errors found" while proving nothing.
UNDEF_RE = r"Warning.*undefined|Citation.*undefined|\?\?"
# "LaTeX Font Warning: Font shape `OMS/cmtt/m/n' undefined" matches UNDEF_RE but is a
# harmless substitution, not a missing \ref or \cite. Subtract it rather than widen
# the pattern, so a genuine undefined reference is never masked.
FONT_UNDEF_RE = r"Font Warning.*undefined|Font shape.*undefined"
ERROR_RE = (
    r"LaTeX Error|Package .* Error|Undefined control sequence|"
    r"Emergency stop|Fatal error|^! "
)
# -file-line-error reformats "! LaTeX Error:" into "./tex/Main.tex:13: LaTeX Error:",
# so searching for ^! alone finds nothing; ERROR_RE covers both shapes.

BUILD_DIRS = ("out", "pdf", "build")
# Status markers that must never survive onto a deposited title page. The cover
# version check below stops at the version number, so "Version 2.0, candidate"
# matched the branch and passed; W1 2.0 was deposited with "candidate" printed on
# its title page. This looks at the whole \date{} block instead.
COVER_STATUS = re.compile(
    r"\b(candidate|draft|preliminary|provisional|tentative|wip|todo"
    r"|not\s+deposited|work\s+in\s+progress)\b",
    re.I,
)

RELEASE_PATHS = ("tex/", "README", "zenodo.json", "CITATION.cff", "compile.sh")


def sh(cmd: str, cwd: Path | None = None) -> tuple[int, str]:
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def sh_raw(cmd: str, cwd: Path | None = None) -> tuple[int, str]:
    """Like sh(), but preserves leading whitespace.

    ``git status --porcelain`` encodes the index/worktree state in the first two
    columns, so a plain ``.strip()`` eats the leading space of the *first* line
    only. That silently truncates the first dirty path by one character, which
    then fails the RELEASE_PATHS prefix test and is reported as an unrelated
    change instead of an uncommitted release file. When the first dirty file is
    the only modified release file, the guard passes a deposit it should refuse.
    """
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.rstrip("\n")


def grep_count(pattern: str, path: Path) -> int | None:
    """Count matching lines, always with -a. None means grep produced no number."""
    rc, out = sh(f"grep -acE {json.dumps(pattern)} {json.dumps(str(path))}")
    try:
        return int(out)
    except ValueError:
        return None


def read_pdf_metadata(path: Path) -> tuple[dict[str, str], str | None]:
    """Read PDF document information through Poppler's structured output."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {}, str(exc)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"pdfinfo exited {result.returncode}"
        return {}, detail

    metadata: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip()
    return metadata, None


def is_dirty(path: Path, repo: Path) -> bool:
    """True when the file has uncommitted content (modified or untracked)."""
    rc, out = sh(f"git status --porcelain -- {json.dumps(str(path))}", repo)
    if rc != 0:
        return False  # not a git repo
    return bool(out.strip())


def find_build(paper_dir: Path, main_tex: str) -> tuple[Path | None, Path | None]:
    base = Path(main_tex).stem
    for d in BUILD_DIRS:
        log = paper_dir / d / f"{base}.log"
        if log.exists():
            return log, paper_dir / d / f"{base}.pdf"
    return None, None


def check(paper_dir: Path, deposit: bool = False) -> dict:
    """Run every check against one paper directory.

    deposit=True is the gate before an actual upload: the paper must be sitting on
    a version branch and every version label must agree with it. deposit=False is
    the resting-state sweep, where living on `main` between releases is normal and
    the labels are cross-checked against each other instead.
    """
    problems: list[str] = []
    notes: list[str] = []
    info: dict = {"paper": str(paper_dir.relative_to(REPO_ROOT))}

    # ── git state ──────────────────────────────────────────────────────────────
    _, branch = sh("git branch --show-current", paper_dir)
    _, head = sh("git rev-parse HEAD", paper_dir)
    info["branch"], info["head"] = branch, head[:7]
    on_version_branch = branch not in ("", "main", "master")

    if not on_version_branch:
        msg = (
            f"on '{branch or 'detached'}': the version is the branch name, and main is "
            "never published — cut a version branch before depositing"
        )
        (problems if deposit else notes).append(msg)
    if branch:
        _, remote = sh(f"git ls-remote --heads origin {branch}", paper_dir)
        remote_sha = remote.split()[0] if remote else None
        if remote_sha != head:
            problems.append(
                f"branch '{branch}' not pushed "
                f"(remote={remote_sha[:7] if remote_sha else 'absent'}, local={head[:7]}) "
                "— never deposit a PDF whose source exists only locally"
            )

    _, status = sh_raw("git status --porcelain", paper_dir)
    dirty = [
        line[3:]
        for line in status.splitlines()
        if line[:2].strip() and not line.startswith("??")
    ]
    stale = [f for f in dirty if f.startswith(RELEASE_PATHS)]
    if stale:
        problems.append(f"uncommitted release files: {stale}")
    other_dirty = [f for f in dirty if f not in stale]
    if other_dirty:
        notes.append(
            f"unrelated tracked changes present ({len(other_dirty)}): isolate them in a "
            "logical commit before the cycle ends"
        )

    # ── zenodo.json ────────────────────────────────────────────────────────────
    cfg_path = paper_dir / "zenodo.json"
    if not cfg_path.exists():
        problems.append("zenodo.json missing")
        return {"info": info, "problems": problems, "notes": notes}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    info["doi"] = cfg.get("doi")
    info["bib_key"] = cfg.get("bib_key")

    cfg_version = cfg.get("version")
    if not cfg.get("doi"):
        notes.append("no concept DOI yet (first publication)")

    cff = paper_dir / "CITATION.cff"
    cff_version = None
    if cff.exists():
        m = re.search(r"^version:\s*\"?([^\"\n]+)\"?", cff.read_text(encoding="utf-8"), re.M)
        cff_version = m.group(1).strip() if m else None
    info["labels"] = {"zenodo.json": cfg_version, "CITATION.cff": cff_version}

    if on_version_branch:
        # The publisher prefers zenodo.json's field over the branch, so a stale value
        # silently mislabels the deposit. Absent is best; present must equal the branch.
        if cfg_version is not None and cfg_version != branch:
            problems.append(
                f"zenodo.json version={cfg_version!r} != branch {branch!r} — the deposit "
                "would be labelled with the wrong version; drop the field"
            )
        elif cfg_version is not None:
            notes.append("zenodo.json carries a version field; dropping it avoids future staleness")
        if cff_version and cff_version != branch:
            problems.append(f"CITATION.cff version={cff_version!r} != branch {branch!r}")
    else:
        # At rest there is no version branch to compare against, but the labels must
        # still agree with each other — this is how a CITATION.cff left a release
        # behind (1.30.1) while the paper had already been deposited as 1.31.0.
        if cfg_version and cff_version and cfg_version != cff_version:
            problems.append(
                f"version labels disagree: zenodo.json={cfg_version!r} vs "
                f"CITATION.cff={cff_version!r} — one was not bumped at the last release"
            )

    # ── main tex ───────────────────────────────────────────────────────────────
    main_tex = cfg.get("main_tex")
    tex_path = paper_dir / main_tex if main_tex else None
    if not tex_path or not tex_path.exists():
        problems.append(f"main_tex not found: {main_tex}")
        return {"info": info, "problems": problems, "notes": notes}
    raw_tex = tex_path.read_text(encoding="utf-8")

    # A cover that self-labels a version must agree with the branch. Papers that
    # do not print a version at all are fine — the check only fires when one exists.
    date_block = re.search(r"\\date\{(.*?)\}", raw_tex, re.S)
    if date_block:
        marker = COVER_STATUS.search(date_block.group(1))
        if marker:
            problems.append(
                f"the title page carries the status marker {marker.group(0)!r} in its "
                f"\\date{{}} block ({' '.join(date_block.group(1).split())!r}) — "
                "a deposited cover must not label itself unfinished"
            )
    cover = re.search(r"Version\s+(\d+(?:\.\d+)*)", date_block.group(1)) if date_block else None
    if cover:
        info["cover_version"] = cover.group(1)
        if on_version_branch and cover.group(1) != branch:
            problems.append(
                f"PDF cover says 'Version {cover.group(1)}' but the branch is {branch!r} — "
                "the deposited PDF would contradict its own Zenodo record"
            )
        elif not on_version_branch and cff_version and cover.group(1) != cff_version:
            notes.append(
                f"cover 'Version {cover.group(1)}' differs from CITATION.cff {cff_version!r}"
            )

    # ── build artefacts ────────────────────────────────────────────────────────
    log, pdf = find_build(paper_dir, main_tex)
    if not log:
        # Build outputs are gitignored, so "never compiled here" is normal at rest
        # and fatal only when we are about to upload one.
        (problems if deposit else notes).append(
            f"no build output for {Path(main_tex).stem} (not compiled locally)"
        )
        return {"info": info, "problems": problems, "notes": notes}

    # Freshness. Only *demonstrable* staleness fails: a source carrying uncommitted
    # content, modified after the build. That is exactly the footgun this guards --
    # a compile that silently never ran (output redirected into a blocked path)
    # leaves the previous log in place while the edits sit in the working tree.
    # A committed source whose commit postdates the build is the normal release
    # order (compile, then commit) and must not fail; git checkout also rewrites
    # mtimes without changing a byte.
    log_mtime = log.stat().st_mtime
    stale = [
        f.name
        for f in tex_path.parent.iterdir()
        if f.suffix in (".tex", ".bib")
        and is_dirty(f, paper_dir)
        and f.stat().st_mtime > log_mtime
    ]
    if stale:
        problems.append(
            f"uncommitted edits to {stale} are newer than the build — the log/PDF "
            "predate them, so this build is not the artefact under review"
        )
    if pdf is None or not pdf.exists():
        (problems if deposit else notes).append("PDF missing (not compiled locally)")
    elif is_dirty(tex_path, paper_dir) and pdf.stat().st_mtime < tex_path.stat().st_mtime:
        problems.append("PDF older than uncommitted edits to the main tex")

    # Sanity probe first: if this fails, every other grep on this log is meaningless.
    if not grep_count("e", log):
        problems.append(
            "grep produced no count on the log (encoding?) — every other log check "
            "on this file would be vacuous; inspect with `file`"
        )
    else:
        undef, errs = grep_count(UNDEF_RE, log), grep_count(ERROR_RE, log)
        font_undef = grep_count(FONT_UNDEF_RE, log) or 0
        if undef is not None:
            undef = max(0, undef - font_undef)
            if font_undef:
                notes.append(f"{font_undef} font-shape substitution warning(s) (harmless)")
        info["undef"], info["errors"] = undef, errs
        if undef:
            problems.append(f"{undef} undefined-reference/citation line(s) in the log")
        if errs:
            problems.append(f"{errs} error line(s) in the log")
        overfull = grep_count(r"Overfull \\hbox", log)
        info["overfull"] = overfull

    _, written = sh(f"grep -a 'Output written' {json.dumps(str(log))}")
    pages = re.search(r"\((\d+) pages", written)
    info["pages"] = int(pages.group(1)) if pages else None

    # ── PDF metadata ───────────────────────────────────────────────────────────
    if pdf and pdf.exists():
        metadata, metadata_error = read_pdf_metadata(pdf)
        if metadata_error:
            problems.append(f"PDF metadata unreadable: {metadata_error}")
        for field in ("Title", "Author"):
            if not metadata.get(field, "").strip():
                problems.append(f"PDF metadata /{field} missing or empty")

    # ── abstract, as Zenodo will receive it ────────────────────────────────────
    try:
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        from zenodo_publish import (
            extract_abstract,
            extract_macros,
            expand_macros,
            resolve_inputs,
        )

        # Mirror the publisher exactly: papers routinely keep the abstract in a
        # separate file pulled in with \input{abstract}, so the abstract (and any
        # macro) must be sought in the fully-resolved source, not the main file alone.
        full = resolve_inputs(raw_tex, tex_path.parent)
        macros = extract_macros(full)
        body = full
        doc = body.find(r"\begin{document}")
        if doc >= 0:
            body = body[:doc] + expand_macros(body[doc:], macros)
        abstract = extract_abstract(body)

        if "~" in abstract:
            problems.append(f"{abstract.count('~')} tilde(s) in the abstract")
        if r"\cite" in abstract:
            problems.append("\\cite in the abstract")

        # Only the paper's OWN preamble macros matter: a standard LaTeX command
        # such as \le or \rightsquigarrow renders fine on Zenodo, whereas a custom
        # macro that survived expansion is a genuine tokenisation failure.
        residual = sorted(
            name
            for name in macros
            if re.search(r"\\" + re.escape(name) + r"(?![A-Za-z])", abstract)
        )
        if residual:
            problems.append(f"custom macro(s) unexpanded in the abstract: {residual}")
        info["abstract_chars"] = len(abstract)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"abstract extraction failed: {exc}")

    if not (paper_dir / "README.md").exists():
        notes.append("README.md missing")

    return {"info": info, "problems": problems, "notes": notes}


def report(results: list[dict]) -> int:
    failed = 0
    for r in results:
        i, problems, notes = r["info"], r["problems"], r["notes"]
        status = "PASS" if not problems else "FAIL"
        if problems:
            failed += 1
        print(f"\n=== {i['paper']}  [{i.get('branch')}] {i.get('head')}  {status}")
        bits = [
            f"doi={i.get('doi')}",
            f"pages={i.get('pages')}",
            f"undef={i.get('undef')}",
            f"errors={i.get('errors')}",
            f"overfull={i.get('overfull')}",
        ]
        print("    " + "  ".join(b for b in bits if not b.endswith("=None")))
        for p in problems:
            print(f"    !! {p}")
        for n in notes:
            print(f"    -- {n}")
    total = len(results)
    print(f"\nPASS {total - failed}/{total}" + (f"   FAIL {failed}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paper_dirs", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true", help="check every paper with a zenodo.json")
    ap.add_argument("--deposit", action="store_true",
                    help="strict pre-upload gate: require a version branch and matching labels")
    args = ap.parse_args()

    if args.all:
        dirs = sorted(p.parent for p in REPO_ROOT.rglob("zenodo.json") if ".git" not in p.parts)
    else:
        dirs = [d.resolve() for d in args.paper_dirs]
    if not dirs:
        ap.error("give at least one paper directory, or --all")

    return report([check(d, deposit=args.deposit) for d in dirs])


if __name__ == "__main__":
    sys.exit(main())
