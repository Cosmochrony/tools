#!/usr/bin/env python3
"""Mutation self-check for zenodo_publish.py (offline, in memory; no file is modified).

Each mutant is a one-line semantic break of a guard. The suite must FAIL on every one of them;
a mutant that survives names a guard the tests do not really protect.

    python3 tests/mutation_check.py
"""
import io
import sys
import types
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
TESTS = TOOLS / "tests"
SRC_PATH = TOOLS / "zenodo_publish.py"

# (id, description, old, new) -- `old` must occur exactly once.
MUTANTS = [
    ("M1", "cleanup discards dep_id+1", "r = client.discard(dep_id)", "r = client.discard(dep_id + 1)"),
    ("M2", "existing-draft refusal removed", "    if problems:\n        sys.exit(\n            \"Error: an unpublished",
     "    if False:\n        sys.exit(\n            \"Error: an unpublished"),
    ("M3", "post-publish verification skipped", "    attempts, got = verify_attempts(), None\n    for k",
     "    return\n    attempts, got = verify_attempts(), None\n    for k"),
    ("M4", "draft verification skipped", "    if got != expected:\n        raise VerificationError(\n            f\"draft",
     "    if False:\n        raise VerificationError(\n            f\"draft"),
    ("M5", "cleanup hard-wired to the old record id", "r = client.discard(dep_id)", "r = client.discard(10)"),
    ("M6", "no doubt-handling after a failed publish call",
     "if dep.get(\"state\") != \"done\" and not is_definitive_refusal(exc):", "if False:"),
    ("M7", "latest_draft link ignored", "        if link:\n            problems.append", "        if False:\n            problems.append"),
    ("M8", "guard ignores conceptrecid",
     "if str(d.get(\"conceptrecid\")) == str(concept) or d.get(\"id\") == latest_id:",
     "if d.get(\"id\") == latest_id:"),
    ("M9", "guard fails open on listing error",
     "        sys.exit(f\"Error: cannot determine whether a draft already exists ({exc}). \"\n                 \"Nothing was created. Refusing to proceed.\")",
     "        pass"),
    ("M10", "cleanup not executed",
     "msg, recovered, save_error = _cleanup(client, step, exc, host, on_published)",
     "msg, recovered, save_error = ('skipped', None, None)"),
    ("M11", "local state never saved (new)", "    if not recovered and on_published:", "    if False:"),
    ("M12", "list_drafts stops after page 1", "            out.extend(batch)\n", "            out.extend(batch)\n            return out\n"),
    ("M13", "local state saved AFTER verification",
     "    verify_published_code_repository(client, result.get(\"id\", dep_id), code_url, host)\n    if save_error is not None:\n        raise PostPublishFailure(f\"\\n!! local state not saved: {save_error}\")\n    return result",
     "    verify_published_code_repository(client, result.get(\"id\", dep_id), code_url, host)\n    if on_published and not recovered:\n        on_published(result)\n    return result"),
    ("M13b", "...and the early-save block disabled", "    if not recovered and on_published:", "    if False and on_published:"),
    ("M14", "sandbox falls back to ZENODO_TOKEN",
     "    token = (environ.get(name) or \"\").strip()\n    if not token:\n        extra",
     "    token = (environ.get(name) or environ.get(PRODUCTION_TOKEN_VAR) or \"\").strip()\n    if not token:\n        extra"),
    ("M15", "host/token pairing check removed", "if not token or (other and token == other):", "if not token:"),
    ("M16", "sandbox run saves production state",
     "    saver = save_sandbox_state if args.sandbox else save_local_state", "    saver = save_local_state"),
    ("M16b", "...and the defensive assertion removed",
     "        if args.sandbox:\n            raise AssertionError", "        if False:\n            raise AssertionError"),
    ("M17", "sandbox run archives / cascades",
     "    if args.sandbox:\n        print(\"\\n[sandbox] PDF archive and DOI cascade skipped by contract: no production file was written.\")\n        return\n    concept_doi = published[\"concept_doi\"]",
     "    concept_doi = published.get(\"concept_doi\") or \"10.0/x\""),
    ("M18", "ambiguous candidates accepted", "    if len(cands) == 1:", "    if len(cands) >= 1:"),
    ("M19", "writes are retried", "attempts = self.get_attempts if method == \"GET\" else 1", "attempts = self.get_attempts"),
    ("M20", "no request timeout", "        kw.setdefault(\"timeout\", self.timeout)\n", "        pass\n"),
    ("M21", "KeyboardInterrupt swallowed after cleanup",
     "    if interrupted:\n        print(msg, file=sys.stderr)\n        raise exc\n    sys.exit(msg)", "    sys.exit(msg)"),
    ("M22", "delete_file failure only warns", "        self._check(resp)\n\n    def upload_file", "        pass\n\n    def upload_file"),
    ("M23", "post-publish verification single attempt", "attempts, got = verify_attempts(), None\n    for k", "attempts, got = 1, None\n    for k"),
    ("M24", "no snapshot membership check before discard",
     "if kind != \"edit session\" and snapshot is not None and dep_id in snapshot:", "if False:"),
    ("M25", "first-publication snapshot skipped", "        snapshot_before_first_publication(client, host)", "        pass"),
    ("M26", "list_drafts stops on a short page",
     "            out.extend(batch)\n", "            out.extend(batch)\n            if len(batch) < LIST_PAGE_SIZE:\n                return out\n"),
    ("M27", "host check of outgoing requests removed", "        parsed = urlparse(url)\n        if parsed.scheme", "        return\n        parsed = urlparse(url)\n        if parsed.scheme"),
    ("M28", "guard accepts a missing record state", "        if latest.get(\"state\") != \"done\":", "        if latest.get(\"state\") not in (None, \"done\"):"),
    ("M29", "extra draft file tolerated", "    if names != [pdf_name]:", "    if pdf_name not in names:"),
    ("M30", "token placed in the Authorization header of a foreign host (bucket) allowed",
     "        if parsed.scheme != \"https\" or parsed.hostname != self.host:", "        if parsed.scheme != \"https\":"),
]


def load_mutated(src: str):
    mod = types.ModuleType("zenodo_publish")
    mod.__file__ = str(SRC_PATH)
    exec(compile(src, str(SRC_PATH), "exec"), mod.__dict__)
    return mod


def run_suite(mod) -> tuple[int, int, int]:
    for name in [n for n in sys.modules if n in ("fakes", "zenodo_publish") or n.startswith("test_")
                 or n.startswith("tests.")]:
        del sys.modules[name]
    sys.modules["zenodo_publish"] = mod
    sys.path.insert(0, str(TESTS))
    sys.path.insert(0, str(TOOLS))
    try:
        suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test_*.py")
        res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
    return res.testsRun, len(res.failures), len(res.errors)


def main() -> int:
    src = SRC_PATH.read_text(encoding="utf-8")
    n, f, e = run_suite(load_mutated(src))
    print(f"baseline: {n} tests, {f} failures, {e} errors")
    if f or e:
        print("baseline is not green: cannot judge mutants")
        return 2
    survivors = []
    for mid, desc, old, new in MUTANTS:
        if src.count(old) != 1:
            print(f"{mid:5} SKIPPED (pattern occurs {src.count(old)} times): {desc}")
            survivors.append(mid)
            continue
        n, f, e = run_suite(load_mutated(src.replace(old, new)))
        killed = (f + e) > 0
        print(f"{mid:5} {'killed  ' if killed else 'SURVIVED'} ({f} failures, {e} errors of {n}): {desc}")
        if not killed:
            survivors.append(mid)
    print("survivors:", survivors or "none")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
