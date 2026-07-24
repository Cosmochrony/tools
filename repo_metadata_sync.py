#!/usr/bin/env python3
"""Sync each article repository's GitHub metadata (homepage + topics) from its
paper source.

For every paper directory that has a Cosmochrony git remote:
  - homepage  <- web_url in zenodo.json (the cosmochrony.org page for the paper)
  - topics    <- keywords extracted from the main .tex, mapped to GitHub's
                 dash-case form (lower-case, hyphen-joined, no special chars)

Usage:
  python tools/repo_metadata_sync.py --dry-run      # print intended metadata
  GH_TOKEN=... python tools/repo_metadata_sync.py   # apply via the GitHub API
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zenodo_publish as zp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that are org repos but not single-paper article repos.
SKIP_REPOS = {
    ".github", "cosmochrony.github.io", "simulation", "program",
}


# LaTeX math-mode command names that leak into keywords once braces/backslashes
# are stripped (e.g. \mathrm{SU}(3) -> "mathrm su 3"). Dropped from topics.
TEX_TOKENS = {
    "mathrm", "mathbb", "mathfrak", "mathcal", "mathbf", "mathsf", "mathnormal",
    "operatorname", "text", "textcolor", "textrm", "color", "boldsymbol",
    "tfrac", "frac", "dfrac", "rm", "bf", "it", "sf",
}


def dash_case(keyword: str) -> str:
    """GitHub topic form: ASCII lower-case, hyphen-joined, no special chars."""
    # Strip TeX escapes / accents to ASCII (Born--Infeld -> born-infeld).
    kw = keyword.replace("--", "-").replace("—", "-").replace("–", "-")
    kw = unicodedata.normalize("NFKD", kw).encode("ascii", "ignore").decode()
    kw = kw.lower()
    parts = [p for p in re.split(r"[^a-z0-9]+", kw) if p and p not in TEX_TOKENS]
    topic = "-".join(parts)
    # GitHub: topic <= 50 chars, must start with a letter/number.
    return topic[:50].strip("-")


def git_remote_repo(paper_dir: Path) -> str | None:
    try:
        url = subprocess.check_output(
            ["git", "-C", str(paper_dir), "config", "--get", "remote.origin.url"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    m = re.search(r"github\.com[:/]Cosmochrony/([^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def collect_paper_dirs() -> list[Path]:
    out = subprocess.check_output(
        ["find", str(REPO_ROOT), "-name", ".git", "-maxdepth", "4",
         "-not", "-path", "*/node_modules/*"],
        text=True,
    )
    dirs = []
    for line in out.splitlines():
        d = Path(line).parent
        if git_remote_repo(d):
            dirs.append(d)
    return sorted(dirs)


def paper_metadata(paper_dir: Path) -> tuple[str | None, list[str], str]:
    """Return (homepage, topics, note)."""
    cfg_path = paper_dir / "zenodo.json"
    if not cfg_path.exists():
        return None, [], "no zenodo.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    homepage = cfg.get("web_url") or None

    keywords: list[str] = []
    try:
        tex_path = zp.find_main_tex(paper_dir, cfg)
        raw = zp.resolve_inputs(tex_path.read_text(encoding="utf-8"), tex_path.parent)
        macros = zp.extract_macros(raw)
        if macros:
            doc_start = raw.find(r"\begin{document}")
            if doc_start >= 0:
                raw = raw[:doc_start] + zp.expand_macros(raw[doc_start:], macros)
        keywords = zp.extract_keywords(raw)
    except Exception as exc:  # noqa: BLE001
        return homepage, [], f"keyword extraction failed: {exc}"

    topics, seen = [], set()
    for kw in keywords:
        t = dash_case(kw)
        if t and t not in seen:
            seen.add(t)
            topics.append(t)
    topics = topics[:20]  # GitHub caps at 20 topics
    return homepage, topics, ""


def apply(repo: str, homepage: str | None, topics: list[str], token: str) -> None:
    import requests
    api = f"https://api.github.com/repos/Cosmochrony/{repo}"
    hdr = {"Authorization": f"Bearer {token}",
           "Accept": "application/vnd.github+json"}
    if homepage:
        r = requests.patch(api, headers=hdr, json={"homepage": homepage}, timeout=30)
        r.raise_for_status()
    if topics:  # never clear existing topics with an empty set
        r = requests.put(api + "/topics", headers=hdr, json={"names": topics}, timeout=30)
        r.raise_for_status()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="substring filter on repo name")
    args = ap.parse_args()

    import os
    token = os.environ.get("GH_TOKEN", "")
    if not args.dry_run and not token:
        sys.exit("GH_TOKEN required to apply (or use --dry-run)")

    problems = []
    for paper_dir in collect_paper_dirs():
        repo = git_remote_repo(paper_dir)
        if not repo or repo in SKIP_REPOS:
            continue
        if args.only and args.only not in repo:
            continue
        homepage, topics, note = paper_metadata(paper_dir)
        rel = paper_dir.relative_to(REPO_ROOT)
        flag = ""
        if not homepage:
            flag += " [NO HOMEPAGE]"
        if not topics:
            flag += " [NO TOPICS]"
        if note:
            flag += f" [{note}]"
        print(f"{repo:38} {homepage or '-'}")
        print(f"{'':38} topics: {', '.join(topics) or '-'}{flag}")
        if flag:
            problems.append((repo, str(rel), flag.strip()))
        if not args.dry_run:
            try:
                apply(repo, homepage, topics, token)
                print(f"{'':38} -> applied")
            except Exception as exc:  # noqa: BLE001
                print(f"{'':38} -> FAILED: {exc}")
                problems.append((repo, str(rel), f"apply failed: {exc}"))

    if problems:
        print("\n=== Needs attention ===")
        for repo, rel, why in problems:
            print(f"  {repo} ({rel}): {why}")


if __name__ == "__main__":
    main()
