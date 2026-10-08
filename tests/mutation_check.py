#!/usr/bin/env python3
"""Mutation self-check for zenodo_publish.py (offline, in memory; no file is modified).

Each mutant is a one-line semantic break of a guard. The suite must FAIL on every one of them;
a mutant that survives names a guard the tests do not really protect.

    python3 tests/mutation_check.py

Exit status 0 only when every mutant is killed EXCEPT the documented equivalent mutant M16b (removing the
unreachable defensive assertion in save_local_state while the saver choice, tested by M16, is intact): the
expected result is "exactly one survivor, M16b, equivalent". Any other survivor, a skipped pattern, or a
red baseline exits non-zero.
"""
import io
import sys
import types
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
TESTS = TOOLS / "tests"
SRC_PATH = TOOLS / "zenodo_publish.py"

# Mutants that cannot be killed because they are semantically equivalent to the original.
EQUIVALENT_SURVIVORS = {"M16b"}

# (id, description, old, new) -- `old` must occur exactly once.
MUTANTS = [
    ("M1", "cleanup discards dep_id+1", "r = client.discard(dep_id, kind)", "r = client.discard(dep_id + 1, kind)"),
    ("M2", "existing-draft refusal removed", "    if problems:\n        sys.exit(\n            \"Error: an unpublished",
     "    if False:\n        sys.exit(\n            \"Error: an unpublished"),
    ("M3", "post-publish verification skipped", "    attempts, got = verify_attempts(), None\n    for k",
     "    return\n    attempts, got = verify_attempts(), None\n    for k"),
    ("M4", "draft verification skipped", "    if got != expected:\n        raise VerificationError(\n            f\"draft",
     "    if False:\n        raise VerificationError(\n            f\"draft"),
    ("M5", "cleanup hard-wired to the old record id", "r = client.discard(dep_id, kind)", "r = client.discard(10, kind)"),
    ("M6", "no doubt-handling after a failed publish call",
     "if dep.get(\"state\") != \"done\" and not is_definitive_refusal(exc):", "if False:"),
    ("M7", "latest_draft link ignored", "        if link:\n            problems.extend(", "        if False:\n            problems.extend("),
    ("M7b", "a self-pointing latest_draft counts as a draft", "    if lid == latest_id:\n        return []",
     "    if False:\n        return []"),
    ("M8", "guard ignores conceptrecid",
     "if str(d.get(\"conceptrecid\")) == str(concept) or d.get(\"id\") == latest_id:",
     "if d.get(\"id\") == latest_id:"),
    ("M9", "guard fails open on listing error",
     "        die(f\"Error: cannot determine whether a draft already exists ({redact(exc)}). \"\n            \"Nothing was created. Refusing to proceed.\")",
     "        pass"),
    ("M10", "cleanup not executed",
     "msg, recovered, save_error = _cleanup(client, step, exc, host, on_published)",
     "msg, recovered, save_error = ('skipped', None, None)"),
    ("M11", "local state never saved (new)", "    if not recovered and on_published:", "    if False:"),
    ("M12", "list_drafts stops after page 1", "            out.extend(batch)\n", "            out.extend(batch)\n            return out\n"),
    ("M13", "local state saved AFTER verification",
     "    verify_published_code_repository(client, result.get(\"id\", dep_id), code_url, host)\n    if save_error is not None:\n        raise PostPublishFailure(f\"\\n!! local state not saved: {redact(save_error)}\")\n    return result",
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
     "        if args.sandbox:\n            print(\"\\n[sandbox] PDF archive and DOI cascade skipped by contract: no production file was written.\")\n            finalize_journal(journal, client)\n            return\n        concept_doi = published[\"concept_doi\"]",
     "        concept_doi = published.get(\"concept_doi\") or \"10.0/x\""),
    ("M18", "ambiguous candidates accepted", "    if len(cands) == 1:", "    if len(cands) >= 1:"),
    ("M19", "writes are retried", "attempts = self.get_attempts if method == \"GET\" else 1", "attempts = self.get_attempts"),
    ("M20", "no request timeout", "        kw.setdefault(\"timeout\", self.timeout)\n", "        pass\n"),
    ("M21", "KeyboardInterrupt swallowed after cleanup",
     "    if interrupted:\n        print(msg, file=sys.stderr)\n        raise exc\n    die(msg)", "    die(msg)"),
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
    # ── second corrective pass ──
    ("A1", "archive overwrites an existing file", "    if dest.exists():\n        existing = file_digests(dest)", "    if False:\n        existing = file_digests(dest)"),
    ("A1b", "a different existing archive is treated as identical", "if existing[\"sha256\"] == src[\"sha256\"]:", "if True:"),
    ("A2", "archive does not check the uploaded digest", "if expected_sha256 is not None and src[\"sha256\"] != expected_sha256:", "if False:"),
    ("A3", "finish-after-fail does not require a recorded digest", "        expected_sha = recorded or given\n        if not expected_sha:", "        expected_sha = None\n        if False:"),
    ("A4", "uploaded digest not recorded in the journal", "            j.update(pdf=digest, phase=\"upload about to be sent\", deposition_id=dep_id)", "            pass"),
    ("B1", "--update applies the local metadata by default", "    payload = copy.deepcopy(live)\n    for field in sorted(fields):", "    payload = {**copy.deepcopy(live), **copy.deepcopy(built)}\n    for field in sorted(fields):"),
    ("B2", "version-label guard skipped", "    if not args.finish_after_fail and not (args.update and \"version\" not in fields):\n        try:", "    if False:\n        try:"),
    ("B2b", "main/master/HEAD accepted as a label", "    if v.lower() in FORBIDDEN_LABELS:", "    if False:"),
    ("B3", "pre-deposit gate that cannot run only warns", "            die(f\"Error: the pre-deposit checks could not run (", "            print(f\"Warning: the pre-deposit checks could not run ("),
    ("B4", "same-version new version accepted", "if outgoing and live_version and str(outgoing).strip() == str(live_version).strip():", "if False:"),
    ("B5", "diff confirmation bypassed", "    if confirm is None:\n        die(\"Diff printed above.", "    if False:\n        die(\"Diff printed above."),
    ("B6", "live drift between diff and edit not detected", "        if normalised_metadata(live_now) != normalised_metadata(live):", "        if False:"),
    ("B7", "--update without fields accepted", "    if args.update and not fields and not args.dry_run:\n        parser.error(", "    if False:\n        parser.error("),
    ("C1", "journal not written before a write request", "            self.journal.write_intent(method, urlparse(url).path)", "            pass"),
    ("C2", "unreconciled journal does not block", "        if blocking:\n            die(blocking_message(blocking))", "        if False:\n            die(blocking_message(blocking))"),
    ("C3", "publish not journaled as unreconciled before sending", "            j.update(outcome=\"UNRECONCILED\", phase=\"publish request about to be sent\",", "            j.update(outcome=\"RUNNING\", phase=\"publish request about to be sent\","),
    ("C4", "reconcile accepts a journal that is not unreconciled", "    if data.get(\"outcome\") not in BLOCKING_OUTCOMES:\n        sys.exit(f\"Error: the journal", "    if False:\n        sys.exit(f\"Error: the journal"),
    ("C4b", "reconcile published without a record id", "        if not record_id:\n            sys.exit(", "        if False:\n            sys.exit("),
    ("C5", "a draft left behind does not block", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", \"DRAFT-LEFT-BEHIND\")", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\")"),
    ("C6", "Ctrl-C right after publish not caught", "        except BaseException as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included", "        except Exception as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included"),
    ("D1", "sandbox state file may be overwritten", "    if p.exists():\n        sys.exit(f\"Error: --sandbox-state {p} already exists", "    if False:\n        sys.exit(f\"Error: --sandbox-state {p} already exists"),
    ("D2", "no early writability probe", "            sandbox_slot.reserve()       # writability probe BEFORE any request", "            pass"),
    ("E1", "exception text printed unredacted", "print(f\"!! Error: {type(exc).__name__}: {redact(exc)}\", file=sys.stderr)", "print(f\"!! Error: {type(exc).__name__}: {exc}\", file=sys.stderr)"),
    ("E2", "token shape not validated", "    if not TOKEN_SHAPE.fullmatch(token):\n        sys.exit(f\"Error: the value of", "    if False:\n        sys.exit(f\"Error: the value of"),
    ("E3", "numeric environment not validated at startup", "        validate_numeric_env()        # every tunable, before any request", "        pass"),
    ("E4", "headers only read from a plain dict", "isinstance(resp.headers, collections.abc.Mapping)", "isinstance(resp.headers, dict)"),
    ("E5", "new-version draft cleaned up like an edit session", "    \"new-version draft\": \"unpublished-new-version-draft\",", "    \"new-version draft\": \"edit-session\","),
    ("N2", "a draft already done may be discarded", "        if dep is not None and dep.get(\"state\") == \"done\":\n            mark(\"UNRECONCILED\", f\"{kind} {dep_id} is unexpectedly", "        if False:\n            mark(\"UNRECONCILED\", f\"{kind} {dep_id} is unexpectedly"),
    ("N8", "deposit_update catches Exception only", "    except BaseException as exc:  # noqa: BLE001\n        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)\n    else:\n        if on_published:\n            on_published(result)\n    verify_published_code_repository(client, dep_id, expected, host)", "    except Exception as exc:  # noqa: BLE001\n        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)\n    else:\n        if on_published:\n            on_published(result)\n    verify_published_code_repository(client, dep_id, expected, host)"),
    ("N13", "snapshot restricted to the concept's drafts", "    client.pre_run_draft_ids = ids\n    client.concept_id = concept", "    client.pre_run_draft_ids = {d.get(\"id\") for d in drafts if str(d.get(\"conceptrecid\")) == str(concept)}\n    client.concept_id = concept"),
    ("N19", "status=draft dropped from the listing query", "\"status\": \"draft\", \"size\": LIST_PAGE_SIZE,", "\"size\": LIST_PAGE_SIZE,"),
    ("O9", "unreadable record after a failed publish does not stop the cleanup", "        if dep is None:\n            mark(\"UNRECONCILED\", f\"state of {dep_id} unreadable", "        if False:\n            mark(\"UNRECONCILED\", f\"state of {dep_id} unreadable"),
    ("O17", "default GET attempts 10", "DEFAULT_GET_ATTEMPTS = 3 ", "DEFAULT_GET_ATTEMPTS = 10 "),
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
    unexpected = [m for m in survivors if m not in EQUIVALENT_SURVIVORS]
    expected_seen = [m for m in survivors if m in EQUIVALENT_SURVIVORS]
    print(f"mutants: {len(MUTANTS)}, killed: {len(MUTANTS) - len(survivors)}, survivors: {survivors or 'none'}")
    if expected_seen:
        print(f"documented equivalent survivor(s): {expected_seen}")
    if unexpected:
        print(f"UNEXPECTED survivor(s): {unexpected}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
