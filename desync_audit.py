"""Measure the Zenodo/website desynchronisation debt across the Cosmochrony corpus.

Read-only. For every paper carrying a zenodo.json, resolve the latest published Zenodo version
through the API, locate the corresponding website page(s) under cosmochrony.github.io, and compare
the page's last git commit date with the Zenodo publication date.

Output: a JSON ledger plus a human-readable table on stdout.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = "/Users/javarome/project/@cosmochrony"
SITE = os.path.join(ROOT, "cosmochrony.github.io")
API = "https://zenodo.org/api/records/{}"


CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zenodo_cache.json")
try:
    with open(CACHE_PATH, encoding="utf-8") as _fh:
        CACHE = json.load(_fh)
except Exception:
    CACHE = {}


def _token():
    env = os.path.join(ROOT, "tools", ".env")
    try:
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("ZENODO_TOKEN"):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


TOKEN = _token()


def zenodo_latest(rec_id):
    """Return (version, publication_date, title, latest_id) for the latest version of a record.

    Zenodo rate-limits anonymous callers hard, so requests are token-authenticated, paced, retried
    with exponential back-off, and cached on disk so a rerun costs nothing.
    """
    key = str(rec_id)
    if key in CACHE:
        c = CACHE[key]
        return c["version"], c["date"], c["title"], c["latest_id"]

    url = API.format(rec_id)
    headers = {"Accept": "application/json", "User-Agent": "cosmochrony-desync-audit/1.0"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    last = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as fh:
                data = json.load(fh)
            md = data.get("metadata", {})
            res = (md.get("version"), md.get("publication_date"), md.get("title"), data.get("id"))
            CACHE[key] = {"version": res[0], "date": res[1], "title": res[2], "latest_id": res[3]}
            with open(CACHE_PATH, "w", encoding="utf-8") as fh:
                json.dump(CACHE, fh)
            return res
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise last


def page_paths(web_url):
    """Map a cosmochrony.org URL to the local page files, including language twins."""
    if not web_url:
        return []
    rel = re.sub(r"^https?://(www\.)?cosmochrony\.org/?", "", web_url).strip("/")
    base = os.path.join(SITE, rel) if rel else SITE
    if base.endswith(".html"):
        candidates = [base]
        stem = base[: -len(".html")]
        candidates += [f"{stem}_{lang}.html" for lang in ("fr", "es", "de", "it")]
    else:
        candidates = [os.path.join(base, "index.html")]
        candidates += [os.path.join(base, f"index_{lang}.html") for lang in ("fr", "es", "de", "it")]
    return [p for p in candidates if os.path.isfile(p)]


def last_commit_date(path):
    out = subprocess.run(
        ["git", "-C", SITE, "log", "-1", "--format=%ad", "--date=short", "--", os.path.relpath(path, SITE)],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or None


def declared_version(path):
    """The version the page declares for its OWN paper, or None.

    A page mentions other papers' versions all the time, so a bare scan for "version N.N" cannot
    decide synchronisation. Only three places speak for the page itself, in decreasing authority:
    the release banner, the Quick-facts version field, and the citation line.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    for pattern in (
        r'data-release-version="([0-9][0-9.]*)"',
        r'<div class="k">\s*(?:Version|Version courante|Current version|Version actuelle)\s*</div>'
        r'\s*<div class="v">\s*(?:[^<]*?\bversion\s+)?([0-9][0-9.]*)',
        r'\bversion\s+([0-9][0-9.]*)\s*,\s*20\d\d',
    ):
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def main():
    paper_dirs = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # .codex/.claude worktrees hold clones of paper repositories; auditing them double-counts.
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", ".venv", ".codex", ".claude")]
        if "zenodo.json" in filenames:
            paper_dirs.append(dirpath)
    paper_dirs.sort()

    ledger = []
    for d in paper_dirs:
        rel_paper = os.path.relpath(d, ROOT)
        try:
            with open(os.path.join(d, "zenodo.json"), encoding="utf-8") as fh:
                zj = json.load(fh)
        except Exception as exc:  # malformed zenodo.json is itself a finding
            ledger.append({"paper": rel_paper, "error": f"zenodo.json unreadable: {exc}"})
            continue

        rec_id = zj.get("zenodo_id")
        web_url = zj.get("web_url")
        entry = {"paper": rel_paper, "web_url": web_url, "zenodo_id": rec_id}

        # A retired paper deliberately has no live record: its content was absorbed elsewhere and
        # its own deposit was withdrawn. That is not the same as work never deposited, and its
        # signpost page still has to point at the superseding record, so check that instead.
        if zj.get("retired"):
            entry["status"] = "RETIRED"
            entry["superseded_by"] = zj.get("superseded_by")
            entry["retired_reason"] = zj.get("retired_reason")
            problems = []
            if not zj.get("superseded_by"):
                problems.append("zenodo.json declares retired without superseded_by")
            if zj.get("zenodo_id") or zj.get("doi"):
                problems.append("zenodo.json still names a record while marked retired")
            pages = page_paths(web_url)
            if not pages:
                problems.append("no signpost page for the retired note")
            for p in pages:
                text = open(p, encoding="utf-8", errors="replace").read()
                rel = os.path.relpath(p, SITE)
                if zj.get("superseded_by") and zj["superseded_by"] not in text:
                    problems.append(f"{rel} does not name the superseding DOI")
                if "canonical" not in text:
                    problems.append(f"{rel} has no canonical link to the superseding page")
                if re.search(r'name="robots"[^>]*content="[^"]*\bindex\b', text):
                    problems.append(f"{rel} is still indexable")
            entry["problems"] = problems
            if problems:
                entry["status"] = "RETIRED_DEFECT"
            ledger.append(entry)
            print(f"  .. {rel_paper}: {entry['status']}", file=sys.stderr)
            continue

        if not rec_id:
            entry["status"] = "NOT_DEPOSITED"
            ledger.append(entry)
            continue

        try:
            version, pub_date, title, latest_id = zenodo_latest(rec_id)
        except Exception as exc:
            entry["status"] = "API_ERROR"
            entry["error"] = str(exc)
            ledger.append(entry)
            continue
        time.sleep(1.1)

        entry.update({"zenodo_version": version, "zenodo_date": pub_date,
                      "zenodo_title": title, "latest_record": latest_id})

        pages = page_paths(web_url)
        if not pages:
            entry["status"] = "NO_PAGE"
            ledger.append(entry)
            continue

        page_info = []
        stale = False
        mismatch = unstated = False
        for p in pages:
            cd = last_commit_date(p)
            dv = declared_version(p)
            page_stale = bool(cd and pub_date and cd < pub_date)
            page_mismatch = bool(version and dv and dv != version)
            page_unstated = bool(version and dv is None)
            stale = stale or page_stale
            mismatch = mismatch or page_mismatch
            unstated = unstated or page_unstated
            page_info.append({"page": os.path.relpath(p, SITE), "last_commit": cd,
                              "declared_version": dv, "older_than_deposit": page_stale,
                              "version_mismatch": page_mismatch,
                              "version_unstated": page_unstated})
        entry["pages"] = page_info
        # A page can carry today's commit date and still name the wrong version, so the date
        # predicate alone does not enforce the synchronisation rule this audit exists to check.
        if stale:
            entry["status"] = "STALE"
        elif mismatch:
            entry["status"] = "VERSION_MISMATCH"
        elif unstated:
            entry["status"] = "VERSION_UNSTATED"
        else:
            entry["status"] = "OK"
        ledger.append(entry)
        print(f"  .. {rel_paper}: {entry['status']}", file=sys.stderr)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desync_ledger.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, ensure_ascii=False)

    buckets = {}
    for e in ledger:
        buckets.setdefault(e.get("status", "?"), []).append(e)

    print("\n== Zenodo / website synchronisation ledger ==")
    print(f"papers with zenodo.json: {len(ledger)}")
    for k in sorted(buckets):
        print(f"  {k}: {len(buckets[k])}")

    print("\n-- STALE (page committed before the current Zenodo version was published) --")
    for e in sorted(buckets.get("STALE", []), key=lambda x: x.get("zenodo_date") or ""):
        print(f"{e['zenodo_date']}  v{e['zenodo_version']:<8} {e['paper']}")
        for p in e["pages"]:
            flag = "STALE" if p["older_than_deposit"] else "ok   "
            vm = " version-not-stated" if p["version_not_stated"] else ""
            print(f"      {flag} {p['last_commit']}  {p['page']}  stated={p['stated_versions']}{vm}")

    print("\n-- NO_PAGE --")
    for e in buckets.get("NO_PAGE", []):
        print(f"  {e['paper']}  -> {e['web_url']}")

    for label in ("VERSION_MISMATCH", "VERSION_UNSTATED"):
        rows = buckets.get(label, [])
        print(f"\n-- {label} ({len(rows)}) --")
        for e in rows:
            declared = sorted({p.get("declared_version") for p in e["pages"]}, key=lambda v: (v is None, v))
            print(f"  {e['paper']}: zenodo v{e['zenodo_version']}, pages declare {declared}")

    print("\n-- RETIRED --")
    for e in buckets.get("RETIRED", []):
        print(f"  {e['paper']}  -> superseded by {e.get('superseded_by')}")

    print("\n-- RETIRED_DEFECT --")
    for e in buckets.get("RETIRED_DEFECT", []):
        print(f"  {e['paper']}")
        for problem in e["problems"]:
            print(f"      {problem}")

    print("\n-- NOT_DEPOSITED --")
    for e in buckets.get("NOT_DEPOSITED", []):
        print(f"  {e['paper']}")

    print("\n-- API_ERROR --")
    for e in buckets.get("API_ERROR", []):
        print(f"  {e['paper']}  {e.get('error')}")

    print(f"\nledger: {out_path}")


if __name__ == "__main__":
    main()
