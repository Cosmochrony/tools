#!/usr/bin/env python3
"""Mutation self-check for zenodo_publish.py (offline, in memory; no file is modified).

Each mutant is a one-line semantic break of a guard. The suite must FAIL on every one of them;
a mutant that survives names a guard the tests do not really protect.

    python3 tests/mutation_check.py [--shard K/N] [--only-new] [--ids ID1,ID2]

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
# E1 USED to be a second one. As first written ("exception text printed unredacted": the ``redact(exc)`` argument of the
# print in abort_and_cleanup replaced by ``exc``) it was EQUIVALENT: the module ``print`` redacts again and ``redact``
# is idempotent and context-free for the text that follows the fixed prefix (a differential test,
# test_zenodo_round6.ExceptionTextChokePoint.test_E1_the_redaction_of_the_argument_is_redundant_with_the_one_of_print,
# pins that equality over a corpus). Every route by which an exception text can leave the process goes through ONE
# choke point (print, die, the argument parser, the journal serialiser, the excepthook: see the AST tests), so E1 now
# mutates the choke point itself (the module print on stderr) and is killed. It is NOT in the allowed-survivor set.

# (id, description, old, new) -- `old` must occur exactly once.
MUTANTS = [
    ("M1", "cleanup discards dep_id+1", "r = client.discard(dep_id, kind)", "r = client.discard(dep_id + 1, kind)"),
    ('M2', 'existing-draft refusal removed',
     '    if problems:\n        die(\n            "Error: an unpublished',
     '    if False:\n        die(\n            "Error: an unpublished'),
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
    ("A1", "archive overwrites a name that appears during the write", "        os.link(tmp, dest)                                 # exclusive: fails if the name exists", "        os.replace(tmp, dest)"),
    ("A1b", "a different existing archive is treated as identical", "        if existing[\"sha256\"] == src[\"sha256\"]:", "        if True:"),
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
    ("C4", "reconcile accepts a journal that is not unreconciled", '    if data["outcome"] not in BLOCKING_OUTCOMES:\n        raise ReconcileRefused(f"the journal {path.name} is not unreconciled', '    if False:\n        raise ReconcileRefused(f"the journal {path.name} is not unreconciled'),
    ("C4b", "reconcile published without a record id", '        if not rid:\n            raise ReconcileRefused(', '        if False:\n            raise ReconcileRefused('),
    ("C5", "a draft left behind does not block", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", \"DRAFT-LEFT-BEHIND\", UNSAVED_OUTCOME)", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", UNSAVED_OUTCOME)"),
    ("C6", "Ctrl-C right after publish not caught", "        except BaseException as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included", "        except Exception as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included"),
    ('D1', 'sandbox state file may be overwritten',
     '    if p.exists():\n        die(f"Error: --sandbox-state {p} already exists',
     '    if False:\n        die(f"Error: --sandbox-state {p} already exists'),
    ("D2", "no early writability probe", "            sandbox_slot.reserve()       # writability probe BEFORE any request", "            pass"),
    ('E1', 'exception text printed to stderr unredacted: the module print (the choke point of every exception text) does not redact the stderr stream', '    _builtins.print(redact(sep.join(str(a) for a in args)), end="\\n" if end is None else end, file=file, flush=flush)', '    _builtins.print(sep.join(str(a) for a in args) if file is sys.stderr else redact(sep.join(str(a) for a in args)), end="\\n" if end is None else end, file=file, flush=flush)'),
    ('E2', 'token shape not validated',
     '    if not TOKEN_SHAPE.fullmatch(token):\n        die(f"Error: the value of',
     '    if False:\n        die(f"Error: the value of'),
    ("E3", "numeric environment not validated at startup", "        validate_numeric_env()        # every tunable, before any request", "        pass"),
    ("E4", "headers only read from a plain dict", "isinstance(resp.headers, collections.abc.Mapping)", "isinstance(resp.headers, dict)"),
    ("E5", "new-version draft cleaned up like an edit session", "    \"new-version draft\": \"unpublished-new-version-draft\",", "    \"new-version draft\": \"edit-session\","),
    ("N2", "a draft already done may be discarded", "        if dep is not None and dep.get(\"state\") == \"done\":\n            mark(\"UNRECONCILED\", f\"{kind} {dep_id} is unexpectedly", "        if False:\n            mark(\"UNRECONCILED\", f\"{kind} {dep_id} is unexpectedly"),
    ("N8", "deposit_update catches Exception only", "    except BaseException as exc:  # noqa: BLE001\n        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)\n    else:\n        if on_published:\n            on_published(result)\n    verify_published_code_repository(client, dep_id, expected, host)", "    except Exception as exc:  # noqa: BLE001\n        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)\n    else:\n        if on_published:\n            on_published(result)\n    verify_published_code_repository(client, dep_id, expected, host)"),
    ("N13", "snapshot restricted to the concept's drafts", "    client.pre_run_draft_ids = ids\n    client.concept_id = concept", "    client.pre_run_draft_ids = {d.get(\"id\") for d in drafts if str(d.get(\"conceptrecid\")) == str(concept)}\n    client.concept_id = concept"),
    ("N19", "status=draft dropped from the listing query", "\"status\": \"draft\", \"size\": LIST_PAGE_SIZE,", "\"size\": LIST_PAGE_SIZE,"),
    ("O9", "unreadable record after a failed publish does not stop the cleanup", "        if dep is None:\n            mark(\"UNRECONCILED\", f\"state of {dep_id} unreadable", "        if False:\n            mark(\"UNRECONCILED\", f\"state of {dep_id} unreadable"),
    ("O17", "default GET attempts 10", "DEFAULT_GET_ATTEMPTS = 3 ", "DEFAULT_GET_ATTEMPTS = 10 "),
    # ── third corrective pass (review round 19): every new behaviour is mutated ──
    # F1 PUBLISHED-UNSAVED
    ("P1", "a success answer is not recorded as blocking before it is parsed", "        if resp.ok and j is not None:", "        if False:"),
    ("P2", "mark_published records a non-blocking outcome", "self.update(outcome=UNSAVED_OUTCOME, phase=\"published; local state not yet saved\"", "self.update(outcome=\"PUBLISHED\", phase=\"published; local state not yet saved\""),
    ("P3", "PUBLISHED-UNSAVED does not block", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", \"DRAFT-LEFT-BEHIND\", UNSAVED_OUTCOME)", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", \"DRAFT-LEFT-BEHIND\")"),
    ("P4", "the block is lifted before zenodo.json is written", "        config[\"zenodo_id\"] = int(concept_recid)\n        config[\"doi\"] = concept_doi\n", "        journal.mark_saved()\n        config[\"zenodo_id\"] = int(concept_recid)\n        config[\"doi\"] = concept_doi\n"),
    ("P5", "zenodo.json is not read back before the block is lifted", "        if str(saved.get(\"zenodo_id\")) != str(int(concept_recid)) or saved.get(\"doi\") != concept_doi:", "        if False:"),
    ("P6", "a sandbox state that was not committed still lifts the block", "        if record_sandbox_state(sandbox_slot, host, paper_dir.name, version, result):\n            journal.mark_saved()", "        record_sandbox_state(sandbox_slot, host, paper_dir.name, version, result)\n        if True:\n            journal.mark_saved()"),
    ("P7", "an --update journal is never settled", "on_published=save_sandbox_state if args.sandbox else settle_update,", "on_published=save_sandbox_state if args.sandbox else None,"),
    ("P8", "a journal still unsaved is finished", "            if jdata.get(\"outcome\") == UNSAVED_OUTCOME:\n                die(", "            if False:\n                die("),
    # F9 / F10 keying
    ("K1", "paper identity (bib_key, title, content) not matched", "        if data.get(\"host\") == host:\n            for key, label in", "        if False:\n            for key, label in"),
    ("K2", "the paper directory is compared as a string, not a realpath", "_realpath(data.get(\"paper_dir_real\") or data[\"paper_dir\"]) == here", "data[\"paper_dir\"] == str(paper_dir)"),
    ("K3", "the block check runs before the title is known", "    identity = journal_identity(config, title)\n", "    identity = journal_identity(config, None)\n"),
    ("K4", "journal not created through the exclusive primitive", "        _create_exclusive_text(path, _journal_text(data))      # exclusive: never over an existing journal", "        _atomic_write_text(path, _journal_text(data))"),
    ("Y29", "the concept block ignores the host", "                and (host is None or data.get(\"host\") == host):", "                and True:"),
    # reconcile
    ("R1", "reconcile writes the paper directory the operator names, not the journal's", '    if paper_dir_arg is not None and _realpath(paper_dir_arg) != _realpath(recorded_real or jdir):', '    if False:'),
    ("R2", "zenodo.json of another concept overwritten", "            if cfg.get(\"zenodo_id\") not in (None, \"\") and str(cfg[\"zenodo_id\"]) != str(cid):", "            if False:"),
    ("R3", "zenodo.json of another bib_key overwritten", '            if cfg.get("bib_key") != data["bib_key"]:', '            if False:'),
    ("R4", "not-published needs no evidence", '        elif checked_account and reason_text:', '        elif True:'),
    ("R4b", "the account check needs no reason", '        elif checked_account and reason_text:', '        elif checked_account:'),
    ("R5", "record URL of any host accepted", '            evidence["record_url"] = validate_record_url(record_url, host, known_id)', '            evidence["record_url"] = record_url'),
    ("R6", "a succeeded publication reconciled as not-published", "        if data.get(\"outcome\") == UNSAVED_OUTCOME:\n            raise ReconcileRefused(\n                \"the journal says", "        if False:\n            raise ReconcileRefused(\n                \"the journal says"),
    ("R7", "published reconciled without the identifiers to save", "            if not cid or not cdoi:", "            if False:"),
    ("R8", "the config written by a reconcile is not read back", "            if str(check.get(\"zenodo_id\")) != str(cid) or check.get(\"doi\") != cdoi:", "            if False:"),
    ('R9', 'the backup removes the original after copying it',
     '    return target, sha, True\n',
     '    os.unlink(path)\n    return target, sha, True\n'),
    ('R10', 'the backup overwrites an existing file of the same name',
     '        _create_exclusive_bytes(target, raw)\n    except FileExistsError as exc:',
     '        target.write_bytes(raw)\n    except FileExistsError as exc:'),
    ('R11', 'lift-block accepts a journal that has a known outcome and a consistent identity',
     '    if kind not in LIFTABLE_KINDS:\n        outcome = (data or {}).get("outcome")',
     '    if False:\n        outcome = (data or {}).get("outcome")'),
    # secrets
    ("S1", "journal values not redacted before writing", '    out = redact_obj(data)\n    out["identity_sha256"]', '    out = dict(data)\n    out["identity_sha256"]'),
    ('S2', 'bare Bearer / Basic credentials are no longer masked',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon, _pass_field_eq)',
     '_PATTERN_PASSES = (_pass_field_colon, _pass_field_eq)'),
    ('S3', 'access_token query strings (name=value) no longer masked',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon, _pass_field_eq)',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon)'),
    ('S4', 'the tokens loaded in the process are not registered',
     '    register_environment_secrets()        # BEFORE the arguments are parsed: an argparse error echoes the values it refuses',
     '    pass'),
    ("S5", "locate_own_state returns an unredacted exception text", "return \"unknown\", None, [rid], f\"cannot read record {rid} ({redact(exc)})\"", "return \"unknown\", None, [rid], f\"cannot read record {rid} ({exc})\""),
    ('S6', 'Authorization header values (name: value) no longer masked',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon, _pass_field_eq)',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_eq)'),
    # archive
    ("A1c", "an existing archive is judged by size only", "        if existing[\"sha256\"] == src[\"sha256\"]:", "        if existing[\"size\"] == src[\"size\"]:"),
    ("A5", "a symlink at the archive destination is followed", "    if dest.is_symlink():\n        raise ArchiveRefused(f\"{dest} is a symbolic link: refusing", "    if False:\n        raise ArchiveRefused(f\"{dest} is a symbolic link: refusing"),
    ("A6", "a directory at the archive destination is not refused", "    if dest.is_dir():\n        raise ArchiveRefused(f\"{dest} is a directory", "    if False:\n        raise ArchiveRefused(f\"{dest} is a directory"),
    ("A7", "the temp copy is not verified before it gets its final name", "    if file_digests(tmp)[\"sha256\"] != src[\"sha256\"]:", "    if False:"),
    ("A8", "the linked copy is not verified (Y32)", "    if file_digests(dest)[\"sha256\"] != src[\"sha256\"]:\n        raise ArchiveRefused(f\"the archived copy {dest} does not match the source after linking\")", "    if False:\n        raise ArchiveRefused(f\"the archived copy {dest} does not match the source after linking\")"),
    ("A10", "the archive is written directly under its final name", "        with open(tmp, \"xb\") as fh:\n            fh.write(data)", "        with open(dest, \"xb\") as fh:\n            fh.write(data)"),
    ("A11", "a stray partial archive file is not reported", "    for stray, size in stray_archive_files(dest_dir, dest.name):", "    for stray, size in []:"),
    ("A12", "the archive re-reads the PDF instead of using the uploaded bytes", "                                   data=getattr(client, \"uploaded_pdf_bytes\", None))", "                                   data=None)"),
    ("A13", "the PDF is read twice (digest, then upload)", "            pdf_bytes, digest = read_pdf_once(pdf_path)       # the PDF is read ONCE: digest, upload, archive", "            pdf_bytes, digest = None, file_digests(pdf_path)"),
    ("A14", "the cascade runs after a refused archive (Y38)", "            die(f\"!! PUBLISHED (journal: PUBLISHED, zenodo.json saved) but the PDF archive failed or was \"", "            print(f\"!! PUBLISHED (journal: PUBLISHED, zenodo.json saved) but the PDF archive failed or was \""),
    ("A15", "finish-after-fail cascades after a refused archive", "            die(f\"Error: archive refused: {redact(exc)}. The DOI cascade was NOT run.\")", "            print(f\"Error: archive refused: {redact(exc)}. The DOI cascade was NOT run.\")"),
    ("A16", "the archive step skips the directory fsync", "    _fsync_dir(dest_dir)\n    if file_digests(dest)", "    if file_digests(dest)"),
    # upload digest
    ("U1", "an unreadable PDF is not mapped to a refusal before upload", "        except OSError as exc:\n            raise VerificationError(f\"cannot read the PDF", "        except ZeroDivisionError as exc:\n            raise VerificationError(f\"cannot read the PDF"),
    ("U2", "a checksum returned by Zenodo that differs is accepted", "    if value != want:\n        raise VerificationError(f\"Zenodo returned", "    if False:\n        raise VerificationError(f\"Zenodo returned"),
    ("U3", "the upload answer is never compared", "        note = verify_upload_answer(answer, digest)", "        note = None"),
    # durability / bounds
    ('D1', 'no directory fsync after a journal replace',
     '        raise AtomicWriteError(path, tmp if os.path.lexists(tmp) else None, exc) from exc\n    _fsync_dir(path.parent)',
     '        raise AtomicWriteError(path, tmp if os.path.lexists(tmp) else None, exc) from exc'),
    ("D2", "directory fsync errors always raised", "            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):", "            if True:"),
    ("D3", "directory fsync errors never raised", "            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):", "            if False:"),
    ("D4", "sandbox state committed with an overwriting replace", "            os.link(self.tmp, self.path)\n", "            os.replace(self.tmp, self.path)\n"),
    ("B8", "GET attempts unbounded", "(\"ZENODO_GET_ATTEMPTS\", DEFAULT_GET_ATTEMPTS, None, False, float(MAX_GET_ATTEMPTS)),", "(\"ZENODO_GET_ATTEMPTS\", DEFAULT_GET_ATTEMPTS, None, False, None),"),
    ("B9", "upper bounds not enforced", "        if maximum is not None and value > maximum:", "        if False:"),
    # --update
    ('W1', '--set-web-url replaces the whole related_identifiers list',
     '            payload[key] = merge_related_web_url(live.get(key), entries[0], replace_identifier, notes)',
     '            payload[key] = copy.deepcopy(entries)'),
    ("W2", "an ambiguous web entry is guessed", "    if len(same_role) > 1:", "    if False:"),
    ("W3", "an entry with the same identifier is not replaced in place", "    if exact:\n        items[exact[0]]", "    if False:\n        items[exact[0]]"),
    ("V1", "the reviewed-digest pin may be bare or short", "    if not CONFIRM_SHAPE.fullmatch(pin):", "    if False:"),
    ("V2", "the reviewed-digest pin needs one character only", "CONFIRM_MIN_HEX = 12", "CONFIRM_MIN_HEX = 1"),
    ("V3", "the reviewed-digest pin need not match the diff", "    if not digest.startswith(pin):", "    if False:"),
    ("Y19", "implausible version strings accepted", "    if not VERSION_SHAPE.fullmatch(v) or \"..\" in v:", "    if False:"),
    ("Y19b", "'..' accepted in a version label", "    if not VERSION_SHAPE.fullmatch(v) or \"..\" in v:", "    if not VERSION_SHAPE.fullmatch(v):"),
    ("Y19c", "unbounded version length", "VERSION_SHAPE = re.compile(r\"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\")", "VERSION_SHAPE = re.compile(r\"[0-9A-Za-z][0-9A-Za-z._+-]*\")"),
    ("Y26", "finalize clears a journal after a write was sent", "    if int(journal.data.get(\"writes_sent\") or 0) == 0 and not (client is not None and client.created_state):", "    if True:"),
    ("Y27", "unreadable live metadata accepted", "    if not isinstance(live, dict) or not live:", "    if False:"),
    ("Y28", "write intents not counted", "self.data[\"writes_sent\"] = int(self.data.get(\"writes_sent\") or 0) + 1", "self.data[\"writes_sent\"] = int(self.data.get(\"writes_sent\") or 0)"),
    # finish bound to the journal
    ("J1", "the archive label comes from the current config", "            label = jdata.get(\"version\")\n", "            label = version\n"),
    ("J2", "the version-label guard is skipped on finish", "            check_version_label(label, paper_dir, config)\n        except VersionLabelError as exc:\n            die(f\"Error: version label refused for the archive", "            pass\n        except VersionLabelError as exc:\n            die(f\"Error: version label refused for the archive"),
    ("J3", "an --update journal can be selected for finish", "                    and journal_kind(data) == \"deposit\"", "                    and True"),
    ("J4", "a sandbox journal can be selected for finish (Y22)", "                    and not data.get(\"sandbox\")\n                    and journal_kind", "                    and True\n                    and journal_kind"),
    ("J5", "finish picks the OLDEST journal (Z4)", "if best is None or str(data.get(\"started_at\")) >= str(best[1].get(\"started_at\")):", "if best is None or str(data.get(\"started_at\")) <= str(best[1].get(\"started_at\")):"),
    ("J6", "finish ignores a concept mismatch (Y23)", "            if jdata.get(\"concept_id\") not in (None, \"\") and str(jdata[\"concept_id\"]) != str(zid):", "            if False:"),
    ("J7", "a journal without a PDF digest can be selected", "                    and (data.get(\"pdf\") or {}).get(\"sha256\")\n", "                    and True\n"),
    ("J9", "a deposit that published nothing can shadow", "and data.get(\"outcome\") in PUBLISHED_OUTCOMES + (UNSAVED_OUTCOME,))", "and data.get(\"outcome\") in PUBLISHED_OUTCOMES + (UNSAVED_OUTCOME, \"NOT-PUBLISHED\"))"),
    ("J10", "an explicit sandbox journal is accepted for finish", "        if data.get(\"sandbox\"):\n            die(f\"Error: the journal {explicit.name} is a SANDBOX journal", "        if False:\n            die(f\"Error: the journal {explicit.name} is a SANDBOX journal"),
    ("Z5", "publish() does not mark the journal with the answer", "        body = self._ok(resp)\n        if j is not None:\n            j.mark_published(dep_id, body)\n        return body", "        body = self._ok(resp)\n        return body"),
]


# Fourth corrective pass (review round 20; ids T1.., RS*): one mutant per new behaviour (F-1..F-5, N03) and the five survivors
# of the review's own run (RS1, RS11, RS15, RS17, RS28). Run only these with `--only-new` (see below).
NEW_MUTANTS = [
    # F-1 reconcile bound to the real path and the verified identity
    ("T1", "reconcile does not verify the journal identity digest", '    if problems:\n        raise ReconcileRefused(f"the journal {path.name} cannot be trusted', '    if False:\n        raise ReconcileRefused(f"the journal {path.name} cannot be trusted'),
    ("T2", "the identity digest ignores paper_dir_real", 'IDENTITY_FIELDS = ("schema", "paper", "paper_dir", "paper_dir_real", "host",', 'IDENTITY_FIELDS = ("schema", "paper", "paper_dir", "host",'),
    ("T3", "paper_dir vs paper_dir_real (symlink retarget) not compared", '            if _realpath(jdir) != recorded_real or _realpath(recorded_real) != recorded_real:', '            if False:'),
    ("T4", "journal without identity fields may write a zenodo.json", '            if missing:\n                raise ReconcileRefused(', '            if False:\n                raise ReconcileRefused('),
    ("T5", "a vanished paper directory is not noticed", '            if not os.path.isdir(recorded_real):', '            if False:'),
    ("T6", "the config is written to paper_dir, not to the recorded real path", '            cfg_path = Path(recorded_real) / "zenodo.json"', '            cfg_path = Path(jdir) / "zenodo.json"'),
    ("T7", "the zenodo.json content hash is not checked", '            if config_core_fingerprint(cfg) != data["config_core_sha256"]:', '            if False:'),
    ("T8", "the identity digest is not stored", '    out["identity_sha256"] = identity_digest(out)\n', '    pass\n'),
    ("T9", "an absent digest is accepted", '    if not stored:\n        return ["the journal carries no identity digest', '    if False:\n        return ["the journal carries no identity digest'),
    # F-2 fail closed on an unknown outcome
    ("T11", "an unknown outcome does not block (old fail-open rule)", '        if known and outcome in NON_BLOCKING_OUTCOMES:\n            continue', '        if outcome not in BLOCKING_OUTCOMES:\n            continue'),
    ("T12", "NOT-PUBLISHED-RECONCILED is not a known non-blocking outcome", 'NON_BLOCKING_OUTCOMES = PUBLISHED_OUTCOMES + ("NOT-PUBLISHED", "NOT-PUBLISHED-RECONCILED")', 'NON_BLOCKING_OUTCOMES = PUBLISHED_OUTCOMES + ("NOT-PUBLISHED",)'),
    ("T13", "an unknown-outcome journal is not attributed by its file name", '            if _named_after(path, paper_dir):\n                why.append("file named after this paper")', '            if False:\n                why.append("file named after this paper")'),
    ('T14', 'a readable journal with an unknown outcome cannot be lifted',
     '    if not (isinstance(outcome, str) and outcome in KNOWN_OUTCOMES):\n        return "unknown-outcome"',
     '    if not (isinstance(outcome, str) and outcome in KNOWN_OUTCOMES):\n        return None'),
    ('T15', 'the block message omits the lift-block cure',
     '        if kind in LIFTABLE_KINDS:\n            reason_text = {',
     '        if False and kind in LIFTABLE_KINDS:\n            reason_text = {'),
    ("T16", "reconcile of an unknown outcome is not refused as such", '    if not known:\n        raise ReconcileRefused(f"the journal {path.name} has an absent or unknown outcome', '    if False:\n        raise ReconcileRefused(f"the journal {path.name} has an absent or unknown outcome'),
    # F-3 / F-4 validation before any write
    ('T17', 'ids may carry a sign',
     '    if not _is_ascii_digits(text) or text[0] == "0" or len(text) > MAX_ID_DIGITS:',
     '    if not _is_ascii_digits(text.lstrip("+-")) or text[0] == "0" or len(text) > MAX_ID_DIGITS:'),
    ("T18", "ids may be arbitrarily long", "MAX_ID_DIGITS = 12", "MAX_ID_DIGITS = 40"),
    ('T19', 'the DOI shape is not checked',
     '    if registrant not in allowed or not rest.startswith(DOI_SUFFIX_WORD):',
     '    if False:'),
    ('T20', 'a Zenodo DOI need not carry the concept id',
     '    if concept_id not in (None, "") and number != str(concept_id):',
     '    if False:'),
    ("T21", "a reason of one character is evidence", "MIN_REASON_CHARS = 10", "MIN_REASON_CHARS = 1"),
    ("T22", "the reason is not stripped before its length is judged", '    text = reason.strip() if isinstance(reason, str) else ""', '    text = reason if isinstance(reason, str) else ""'),
    ("T23", "the record URL host is matched loosely", 'parts.hostname == host and parts.username is None', 'host in (parts.hostname or "") and parts.username is None'),
    ("T24", "a plain http record URL is accepted", 'parts.scheme == "https" and', 'parts.scheme in ("http", "https") and'),
    ("T25", "the record URL need not contain the record id", '    if record_id and not re.search(', '    if False and not re.search('),
    ("T26", "the record id is matched as a substring", 'rf"(?<!\\d){re.escape(str(record_id))}(?!\\d)"', 'rf"{re.escape(str(record_id))}"'),
    ("T27", "credentials in the record URL are accepted", 'and parts.username is None\n              and parts.password is None', 'and True\n              and parts.password is None'),
    ('T28', 'the lift-block reason is not validated',
     '    reason_text = validate_reason(reason)\n    raw = _read_journal_bytes(path)',
     '    reason_text = reason or ""\n    raw = _read_journal_bytes(path)'),
    ("T29", "the record id is not validated", '    rid = validate_id(record_id, "--record-id") if record_id not in (None, "") else None', '    rid = int(record_id) if record_id not in (None, "") else None'),
    ("T30", "the concept id is not validated", '    cid_arg = validate_id(concept_id, "--concept-id") if concept_id not in (None, "") else None', '    cid_arg = concept_id if concept_id not in (None, "") else None'),
    # F-5 redaction by value and by name
    ("T31", "credential-named dict keys are not masked", '"[REDACTED]" if is_sensitive_name(k) else redact_obj(v))', 'redact_obj(v))'),
    ("T32", "environment tokens shorter than 8 are not registered", '        if isinstance(raw, str):\n            register_secret(raw)', '        if isinstance(raw, str) and len(raw.strip()) >= 8:\n            register_secret(raw)'),
    ("T33", "only the production token variable is registered", '    for name in (PRODUCTION_TOKEN_VAR, SANDBOX_TOKEN_VAR):\n        raw = environ.get(name)', '    for name in (PRODUCTION_TOKEN_VAR,):\n        raw = environ.get(name)'),
    ('T34', 'Cookie is not a sensitive name', '"authorization", "cookie", "signature")', '"authorization", "signature")'),
    ('T35', 'X-Api-Key is not a sensitive name', 'or "apikey" in joined:', 'or False:'),
    ('T36', 'sensitive names are case-sensitive in dict keys', 'return [w.lower() for w in _NAME_WORDS.findall(name)]', 'return [w for w in _NAME_WORDS.findall(name)]'),
    ('T37', 'the empty string is registered as a secret',
     '    if isinstance(value, str) and value.strip():\n        _SECRETS.add(value)',
     '    if isinstance(value, str):\n        _SECRETS.add(value)'),
    ("T38", "the stripped form of a token is not registered", '            register_secret(raw.strip())', '            pass'),
    ('T39', 'refresh_token (any name ending in token) is not a sensitive name', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd",', 'CREDENTIAL_SUFFIXES = ("secret", "password", "passwd",'),
    ('T40', 'client_secret (any name ending in secret) is not a sensitive name', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd",', 'CREDENTIAL_SUFFIXES = ("token", "password", "passwd",'),
    ('T41', 'password is not a sensitive name', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd",', 'CREDENTIAL_SUFFIXES = ("token", "secret", "passwd",'),
    ('T42', 'Proxy-Authorization is not a sensitive name', '"authorization", "cookie", "signature")', '"cookie", "signature")'),
    # N03 related identifiers
    ("T43", "an entry without resource_type is replaced implicitly", '        if target.get("resource_type") is None:', '        if False:'),
    ('T44', 'the cosmochrony.org host is not recognised as the web host among candidates',
     'web_like = [i for i in same_role if _is_web_host(items[i].get("identifier")) or items[i].get("identifier") in known]',
     'web_like = [i for i in same_role if items[i].get("identifier") in known]'),
    ("T45", "--replace-related-identifier is ignored", '    if replace_identifier:\n        named =', '    if False:\n        named ='),
    ("T46", "any host ending in cosmochrony.org is the web host", 'return host == WEB_HOST or host.endswith("." + WEB_HOST)', 'return host.endswith(WEB_HOST)'),
    ("T47", "--replace-related-identifier may match several entries", '        if len(named) != 1:', '        if len(named) < 1:'),
    ('T48', 'the web entry is not appended when nothing matches',
     '    items.append(copy.deepcopy(entry))\n    return items\n\n\ndef build_update_payload',
     '    pass\n    return items\n\n\ndef build_update_payload'),
    # survivors of the review's own run (round 20), now killed by named tests
    ("T49", "get_git_branch hands the whole environment to git", '            check=True,\n            env=git_env(),\n        )', '            check=True,\n        )'),
    ("T50", "git_env keeps every variable (tokens included)", '    return {k: v for k, v in environ.items() if k in _GIT_ENV_KEEP}', '    return dict(environ)'),
    ("RS1", "the block no longer matches on the zenodo.json content hash", '("title", "same title"),\n                               ("config_sha256", "same zenodo.json content")', '("title", "same title")'),
    ('RS11', 'no file fsync in the atomic journal write',
     '            fh.write(text)\n            _fsync_file(fh)\n        os.replace(tmp, path)',
     '            fh.write(text)\n        os.replace(tmp, path)'),
    ("RS15", "the pin is compared on its first 4 characters only", "if not digest.startswith(pin):", "if digest[:4] != pin[:4]:"),
    ("RS17", "the exact web-url match ignores the relation", 'and items[i].get("relation") == entry.get("relation")]\n    if exact:', 'and True]\n    if exact:'),
    ('RS28', 'dictionary keys are not redacted in redact_obj',
     'return {(redact(k, short_secrets=False) if isinstance(k, str) else k):',
     'return {k:'),
]

# ── fifth corrective pass (review round 21): N-1 .. N-11 ───────────────────────────────────────────────────────────────
# A tuple may carry a fifth element, the file it mutates (default zenodo_publish.py).
PC = "predeposit_check.py"
NEW5_MUTANTS = [
    # N-1 / Q6 / Q38: the DOI and the ids
    ("N5U1", "ids accept the digits of other scripts (Q6)", "    return isinstance(text, str) and text != \"\" and all(c in ASCII_DIGITS for c in text)",
     "    return isinstance(text, str) and text != \"\" and text.isdigit()"),
    ("N5U2", "ids may have a leading zero", '    if not _is_ascii_digits(text) or text[0] == "0" or len(text) > MAX_ID_DIGITS:',
     '    if not _is_ascii_digits(text) or len(text) > MAX_ID_DIGITS:'),
    ("N5U3", "the DOI number is compared with the concept id by prefix", "and number != str(concept_id):", "and not number.startswith(str(concept_id)):"),
    ("N5U4", "any registrant is accepted for a Zenodo concept DOI", "    if registrant not in allowed or not rest.startswith(DOI_SUFFIX_WORD):",
     '    if not registrant.startswith("10.") or not rest.startswith(DOI_SUFFIX_WORD):'),
    ("N5U5", "the word zenodo may be in any case", "not rest.startswith(DOI_SUFFIX_WORD):", "not rest.lower().startswith(DOI_SUFFIX_WORD):"),
    ("N5U6", "a different DOI already saved is silently replaced", '            if cfg.get("doi") not in (None, "") and cfg["doi"] != cdoi:', "            if False:"),
    ("N5U7", "the concept id read from the journal is not validated (Q38)", 'cid = cid_arg or (validate_id(data.get("concept_id"), "the concept id recorded in the journal")',
     'cid = cid_arg or (str(data.get("concept_id"))'),
    ("N5U8", "both registrants are accepted on every host", "    allowed = [ZENODO_REGISTRANTS[host]] if host in ZENODO_REGISTRANTS else sorted(ZENODO_REGISTRANTS.values())",
     "    allowed = sorted(ZENODO_REGISTRANTS.values())"),
    ("N5U9", "the concept DOI recorded in the journal is not validated",
     'validate_concept_doi(cdoi, cid, host, "--concept-doi" if concept_doi else "the concept DOI recorded in the journal")', "pass"),
    ("N5U10", "a different zenodo_id already saved is accepted", '            if cfg.get("zenodo_id") not in (None, "") and str(cfg["zenodo_id"]) != str(cid):', "            if False:"),
    ("N5U11", "a journal host that is not a string is not refused", "    if not isinstance(host, str):\n        raise ReconcileRefused(f\"the journal {path.name} records a host",
     "    if False:\n        raise ReconcileRefused(f\"the journal {path.name} records a host"),
    # N-2 / N-5 / N-6: redaction
    ('N5U13', 'name=value masks the scheme word only (the credential after it leaks)', '_TOKEN_EQ = re.compile(r"(?:(?:bearer|basic|digest|negotiate|token)[ \\t]+)?[^&\\s\\"\'<>]+", re.IGNORECASE)', '_TOKEN_EQ = re.compile(r"[^&\\s\\"\'<>]+", re.IGNORECASE)'),
    ('N5U14', 'Basic credentials are not recognised as a scheme', '(?P<scheme>bearer:?|basic)', '(?P<scheme>bearer:?)'),
    ('N5U15', 'percent-encoded names are not decoded', '    return decoded if re.fullmatch(r"[\\w.-]+", decoded) else m.group("enc")', '    return m.group("enc")'),
    ('N5U18', 'ZENODO_TOKEN: in prose is masked too', '_PROSE_NAMES = ("zenodo_token", "zenodo_sandbox_token")', '_PROSE_NAMES = ()'),
    ("N5U19", "a short secret is replaced as a substring", "        if len(secret) >= MIN_BLIND_SECRET:", "        if True:"),
    ("N5U20", "a short secret has no word-boundary rule", '_SHORT_EDGE = (r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])")', '_SHORT_EDGE = ("", "")'),
    ("N5U21", "a dictionary key is masked by a short secret", "redact(k, short_secrets=False)", "redact(k)"),
    ("N5U22", "argument-parsing errors are not redacted", "self._print_message(redact(message), sys.stderr)", "self._print_message(message, sys.stderr)"),
    ("N5U23", "the environment tokens are registered after the arguments are parsed",
     "    register_environment_secrets()        # BEFORE the arguments are parsed: an argparse error echoes the values it refuses\n    parser = build_parser()\n    args = parser.parse_args(argv)\n",
     "    parser = build_parser()\n    args = parser.parse_args(argv)\n    register_environment_secrets()\n"),
    ("N5U24", "print does not redact", "    _builtins.print(redact(sep.join(str(a) for a in args)), end=", "    _builtins.print((sep.join(str(a) for a in args)), end="),
    ("N5U25", "die does not redact", "    sys.exit(redact(message))", "    sys.exit(message)"),
    ("N5U26", "the blind-substitution threshold is one character", "MIN_BLIND_SECRET = 8 ", "MIN_BLIND_SECRET = 1 "),
    ("N5U27", "the scheme pass is the only pattern pass", "_PATTERN_PASSES = (_pass_scheme, _pass_field_colon, _pass_field_eq)", "_PATTERN_PASSES = (_pass_scheme,)"),
    ("N5U28", "a whitespace-only environment token is registered", "            register_secret(raw.strip())\n", "            register_secret(raw.strip()); _SECRETS.add(raw)\n"),
    # N-3: --backup-journal / --lift-block (decision-file mechanism)
    ("N5V1", "the digest of the journal is not compared with the decision's", 'and decision["journal_sha256"] == current)', "and True)"),
    ("N5V2", "a decision is honoured for any journal kind", "    if kind not in LIFTABLE_KINDS:\n        return False\n    dec = decision_path(path)", "    dec = decision_path(path)"),
    ("N5V3", "the original journal is modified by the lift", '    decision["decision_path"] = str(dec)', '    path.write_bytes(raw + b"\\n")\n    decision["decision_path"] = str(dec)'),
    ("N5V4", "the backup is written by a plain write over an existing file", "        _create_exclusive_bytes(target, raw)\n    except FileExistsError as exc:", "        target.write_bytes(raw)\n    except FileExistsError as exc:"),
    ("N5V5", "the decision file is written by a plain write, not an exclusive create", '        _create_exclusive_text(dec, json.dumps(decision, indent=2, ensure_ascii=False) + "\\n")',
     '        dec.write_text(json.dumps(decision, indent=2, ensure_ascii=False) + "\\n")'),
    ("N5V6", "the backup name is unique by timestamp only", '{BACKUP_MARK}{_utc_stamp()}-{sha[:BACKUP_SHA_CHARS]}.json"', '{BACKUP_MARK}{_utc_stamp()}.json"'),
    ("N5V7", "the decision's journal name is not compared", 'and decision.get("journal") == path.name', "and True"),
    ("N5V8", "the decision's block kind is not compared", 'and decision.get("block_kind") == kind', "and True"),
    ("N5V9", "any decision value is taken for UNBLOCK", 'decision.get("decision") == "UNBLOCK"', 'decision.get("decision") is not None'),
    ("N5V10", "a symbolic link is accepted as a decision file", "        if dec.is_symlink() or not dec.is_file():", "        if not dec.is_file():"),
    ("N5V11", "an existing copy is trusted by its name, not by its sha256", "                    and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha):", "                    ):"),
    ("N5V12", "the lift needs no evidence", "    elif checked_account:\n        evidence[\"checked_account\"] = True", "    elif True:\n        evidence[\"checked_account\"] = True"),
    ("N5V13", "an existing decision file is not looked for before the lift", "    if dec.exists() or dec.is_symlink():", "    if False:"),
    ("N5V14", "the lift makes no backup", "        copy_path, _, _ = backup_journal(path)", "        copy_path = path"),
    ("N5V15", "a moved directory is not a liftable kind", '        return "directory"', "        return None"),
    ("N5V16", "an identity problem is not a liftable kind", '    if journal_identity_problems(data):\n        return "identity"', "    if journal_identity_problems(data):\n        return None"),
    ("N5V17", "the decision records another digest than the journal's", '"journal_sha256": sha, "block_kind": kind', '"journal_sha256": hashlib.sha256(b"").hexdigest(), "block_kind": kind'),
    ("N5V18", "--quarantine is accepted silently", '    if args.quarantine:\n        parser.error("--quarantine was removed', '    if False:\n        parser.error("--quarantine was removed'),
    ("N5V19", "an unreadable journal is not a liftable kind", 'LIFTABLE_KINDS = ("unreadable", "unknown-outcome", "identity", "directory")', 'LIFTABLE_KINDS = ("unknown-outcome", "identity", "directory")'),
    ("N5V20", "the copy is not fsynced before it is linked", '    with open(tmp, "xb") as fh:\n        fh.write(data)\n        _fsync_file(fh)', '    with open(tmp, "xb") as fh:\n        fh.write(data)'),
    ("N5V21", "the decision stores no evidence", '"reason": reason_text, "evidence": evidence,', '"reason": reason_text, "evidence": {},'),
    ("N5V22", "the block check does not look at decisions", "        if journal_lift_decision_ok(path, data):\n            continue", "        if False:\n            continue"),
    ("N5V23", "a published-unsaved lift prints no consequence", '    if info.get("outcome_at_decision") == UNSAVED_OUTCOME:', "    if False:"),
    # N-4 / N-5: related identifiers
    ("N5W2", "a same-role entry of any host is replaced implicitly (old behaviour)", 'web_like = [i for i in same_role if _is_web_host(items[i].get("identifier")) or items[i].get("identifier") in known]', "web_like = list(same_role)"),
    ("N5W7", "the kept foreign entry is not reported", "    for i in same_role:\n        notes.append(", "    for i in []:\n        notes.append("),
    ("N5W10", "the replaced entry is not reported", '        notes.append(f"replacing the related identifier', '        (lambda *_: None)(f"replacing the related identifier'),
    ("N5W11", "a lone typeless foreign entry is appended instead of refused", "    if typeless:\n        if len(same_role) > 1:", "    if False:\n        if len(same_role) > 1:"),
    ("N5X1", "the battery's sh() hands the whole environment to its child", "text=True, env=child_env())\n    return r.returncode, r.stdout.strip()", "text=True)\n    return r.returncode, r.stdout.strip()", PC),
    ("N5X2", "the battery's sh_raw() hands the whole environment to its child", 'text=True, env=child_env())\n    return r.returncode, r.stdout.rstrip("\\n")', 'text=True)\n    return r.returncode, r.stdout.rstrip("\\n")', PC),
    ("N5X3", "pdfinfo gets the whole environment", "            check=False,\n            env=child_env(),", "            check=False,", PC),
    # N-8 / N-9: failed writes
    ("N5Z6", "a failed write does not name its temporary file", "raise AtomicWriteError(path, tmp if os.path.lexists(tmp) else None, exc) from exc\n    _fsync_dir(path.parent)",
     "raise AtomicWriteError(path, None, exc) from exc\n    _fsync_dir(path.parent)"),
    ("N5Z7", "a failed write deletes its temporary file", "    except OSError as exc:\n        raise AtomicWriteError(", "    except OSError as exc:\n        os.path.lexists(tmp) and os.unlink(tmp)\n        raise AtomicWriteError("),
    ("N5Z8", "a failed config write in a reconcile is a traceback", '                except OSError as exc:\n                    raise ReconcileRefused(f"{exc}. Nothing else was written; the journal still blocks.") from exc',
     "                except OSError as exc:\n                    raise"),
    ("N5Z9", "a failed journal write after the config is a traceback", '        raise ReconcileRefused(f"{exc}.{saved} The journal was NOT updated and still blocks.") from exc', "        raise"),
]
NEW5_IDS = {m[0] for m in NEW5_MUTANTS}

# ── sixth corrective pass (review round 22): every new behaviour is mutated ──
NEW6_MUTANTS = [
    ('N6E1', 'child_env keeps every variable', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW}', '    return dict(environ)', PC),
    ('N6E2', 'the allow-list admits SSH_AUTH_SOCK', '    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR",\n', '    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR", "SSH_AUTH_SOCK",\n', PC),
    ('N6E3', 'the allow-list admits the proxy variables', '    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR",\n', '    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR", "HTTPS_PROXY", "https_proxy",\n', PC),
    ('N6E4', 'the allow-list admits GIT_ASKPASS and GIT_SSH_COMMAND', '"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS",\n})', '"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_ASKPASS", "GIT_SSH_COMMAND",\n})', PC),
    ('N6E5', 'every GIT_* variable is passed to a child', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW}', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW or k.startswith("GIT_")}', PC),
    ('N6E6', 'every TEXMF* variable is passed to a child', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW}', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW or k.startswith("TEXMF")}', PC),
    ('N6E7', 'every PYTHON* variable is passed to a child', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW}', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW or k.startswith("PYTHON")}', PC),
    ('N6E8', 'every LC_* variable is passed to a child', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW}', '    return {k: v for k, v in environ.items() if k in CHILD_ENV_ALLOW or k.startswith("LC_")}', PC),
    ('N6E9', 'TEXMFHOME is not passed (TeX Live cannot find its tree)', '"TEXINPUTS", "BIBINPUTS", "BSTINPUTS", "TEXFONTS", "TFMFONTS", "TEXMFHOME",', '"TEXINPUTS", "BIBINPUTS", "BSTINPUTS", "TEXFONTS", "TFMFONTS",', PC),
    ('N6E10', 'PATH is not passed', '    "PATH", "HOME", "USER",', '    "HOME", "USER",', PC),
    ('N6E11', 'SOURCE_DATE_EPOCH is not passed (reproducible builds)', '"SOURCE_DATE_EPOCH", "FORCE_SOURCE_DATE",', '"FORCE_SOURCE_DATE",', PC),
    ('N6E12', 'git_env passes every GIT_* variable (askpass, ssh command, config parameters)', '    return {k: v for k, v in environ.items() if k in _GIT_ENV_KEEP}', '    return {k: v for k, v in environ.items() if k in _GIT_ENV_KEEP or k.startswith("GIT_")}'),
    ('N6E13', 'git_env passes the SSH agent socket', '"LOGNAME",\n                 # the GIT_*', '"LOGNAME", "SSH_AUTH_SOCK",\n                 # the GIT_*'),
    ('N6R1', 'Authorization=<scheme> credential: the value stops at the first space', '_HEADER_EQ = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|\\\\(?![rn\\"\'])|[^\\r\\n\\"\'\\\\&<>])*")', '_HEADER_EQ = re.compile(r"[^\\s\\"\'&<>]*")'),
    ('N6R2', 'Authorization: <scheme> credential: the value stops at the first space', '_HEADER_COLON = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|\\\\(?![rn\\"\'])|[^\\r\\n\\"\'\\\\])*")', '_HEADER_COLON = re.compile(r"[^\\s\\"\']*")'),
    ('N6R3', 'the header value does not stop at an escaped line end (\\r \\n written as text)', '_HEADER_COLON = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|\\\\(?![rn\\"\'])|[^\\r\\n\\"\'\\\\])*")', '_HEADER_COLON = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|[^\\r\\n\\"\'])*")'),
    ('N6R4', 'Authorization=value does not stop at an ampersand', '_HEADER_EQ = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|\\\\(?![rn\\"\'])|[^\\r\\n\\"\'\\\\&<>])*")', '_HEADER_EQ = re.compile(r"(?:[\\w.-]+=\\"[^\\"\\r\\n]*\\"|[\\w.-]+=\'[^\'\\r\\n]*\'|\\\\(?![rn\\"\'])|[^\\r\\n\\"\'\\\\<>])*")'),
    ('N6R5', 'a name that is not sensitive swallows its value (a credential field after it is missed)', '        if not sensitive:\n            pos = m.end()\n            continue', '        if not sensitive:\n            pos = _value_end(text, m.end(), colon, False)\n            continue'),
    ('N6R6', 'the suffix rule only matches the exact names', '    if joined.endswith(CREDENTIAL_SUFFIXES) or "apikey" in joined:', '    if joined in CREDENTIAL_SUFFIXES or "apikey" in joined:'),
    ('N6R7', 'access/secret/private + key pairs are not recognised', '    if any(pair in CREDENTIAL_KEY_PAIRS for pair in zip(words, words[1:])):', '    if False:'),
    ('N6R8', '<qualifier>_key (auth_key, signing_key, ...) is not recognised', '    return len(words) >= 2 and words[-1] == "key" and words[-2] in KEY_QUALIFIERS', '    return False'),
    ('N6R9', 'every <word>_key is masked (bib_key included)', '    return len(words) >= 2 and words[-1] == "key" and words[-2] in KEY_QUALIFIERS', '    return len(words) >= 2 and words[-1] == "key"'),
    ('N6R10', 'a bare key is masked', '    return len(words) >= 2 and words[-1] == "key" and words[-2] in KEY_QUALIFIERS', '    return words[-1] == "key"'),
    ('N6R11', 'camelCase boundaries are not split', '_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")', '_NAME_WORDS = re.compile(r"[A-Za-z]+|[0-9]+")'),
    ('N6R12', 'the apikey rule is dropped (x-api-key, API_KEY)', '    if joined.endswith(CREDENTIAL_SUFFIXES) or "apikey" in joined:', '    if joined.endswith(CREDENTIAL_SUFFIXES):'),
    ('N6R13', 'ZENODO_TOKEN= assignments are not masked (the prose exception also applies to the = form)', '(colon and "_".join(credential_name_words(name)) in _PROSE_NAMES)', '("_".join(credential_name_words(name)) in _PROSE_NAMES)'),
    ('N6R14', 'Bearer: X (colon) is not recognised', '(?P<scheme>bearer:?|basic)', '(?P<scheme>bearer|basic)'),
    ('N6R15', 'a bare scheme credential runs on across an escaped line end', '_SCHEME = re.compile(r"(?i)\\b(?P<scheme>bearer:?|basic)\\s+(?P<cred>[^\\s\\\\\\"\'&<>,;]+)")', '_SCHEME = re.compile(r"(?i)\\b(?P<scheme>bearer:?|basic)\\s+(?P<cred>[^\\s\\"\'&<>,;]+)")'),
    ('N6R16', 'the separator may span a line end (an empty value eats the next line)', '_HEAD_COLON = re.compile(r"(?<![\\w.-])(?P<name>\\w[\\w.-]*)(?P<kq>\\\\?[\\"\']?)(?P<sep>[ \\t]*:[ \\t]*|%3[Aa][ \\t]*)")', '_HEAD_COLON = re.compile(r"(?<![\\w.-])(?P<name>\\w[\\w.-]*)(?P<kq>\\\\?[\\"\']?)(?P<sep>\\s*:\\s*|%3[Aa]\\s*)")'),
    ('N6R17', 'a quoted value is not recognised as a unit', '    q = _QUOTED.match(text, pos)\n    if q:\n        return q.end()', '    q = None\n    if q:\n        return q.end()'),
    ('N6R18', 'the Authorization name is not given the whole-value rule', '_value_end(text, m.end(), colon, _is_auth_name(name))', '_value_end(text, m.end(), colon, False)'),
    ('N6R19', 'an escaped-quote JSON value is not recognised', '_QUOTED = re.compile(r"\\"(?:[^\\"\\\\]|\\\\.)*\\"|\'[^\']*\'|\\\\\\"(?:[^\\\\]|\\\\(?!\\"))*\\\\\\"")', '_QUOTED = re.compile(r"\\"(?:[^\\"\\\\]|\\\\.)*\\"|\'[^\']*\'")'),
    ('N6R20', 'passphrase is not a credential name', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd", "passphrase",', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd",'),
    ('N6R21', 'signature is not a credential name', '"authorization", "cookie", "signature")', '"authorization", "cookie")'),
    ('N6R22', 'credentials (plural) is not a credential name', '"credential", "credentials",', '"credential",'),
    ('N6R23', 'passwd is not a credential name', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password", "passwd",', 'CREDENTIAL_SUFFIXES = ("token", "secret", "password",'),
    ('N6R24', 'dots are not allowed in a percent-encoded name', '_ENCODED_NAME = re.compile(r"(?<![\\w.%-])(?P<enc>[\\w.-]*(?:%[0-9A-Fa-f]{2}[\\w.-]*)+)(?=[\\"\']?(?:\\s*[:=]|%3[AaDd]))")', '_ENCODED_NAME = re.compile(r"(?<![\\w.%-])(?P<enc>[\\w-]*(?:%[0-9A-Fa-f]{2}[\\w-]*)+)(?=[\\"\']?(?:\\s*[:=]|%3[AaDd]))")'),
    ('N6R25', 'the scheme word of a generic name=value is not consumed', '_TOKEN_EQ = re.compile(r"(?:(?:bearer|basic|digest|negotiate|token)[ \\t]+)?[^&\\s\\"\'<>]+", re.IGNORECASE)', '_TOKEN_EQ = re.compile(r"[^&\\s\\"\'<>]+", re.IGNORECASE)'),
    ('N6R26', 'a dictionary key is masked only when written exactly (not decoded)', '    return is_credential_name(unquote(key).strip().strip("\\"\'"))', '    return is_credential_name(key)'),
    ('N6X1', 'main() does not install the redacting excepthook', '    sys.excepthook = _redacting_excepthook\n    register_environment_secrets()', '    register_environment_secrets()'),
    ('N6X2', 'the excepthook prints the traceback unredacted', '    print(redact("".join(traceback.format_exception(etype, value, tb))), file=sys.stderr)', '    _builtins.print("".join(traceback.format_exception(etype, value, tb)), file=sys.stderr)'),
    ("N6W1", "the explicit replacement does not check the relation of the named entry", '        if target.get("relation") != entry.get("relation") or target.get("scheme") != entry.get("scheme"):', '        if target.get("scheme") != entry.get("scheme"):'),
    ("N6W2", "the explicit replacement does not check the scheme of the named entry", '        if target.get("relation") != entry.get("relation") or target.get("scheme") != entry.get("scheme"):', '        if target.get("relation") != entry.get("relation"):'),
    ("N6W3", "the explicit replacement is not refused at all", '        if target.get("relation") != entry.get("relation") or target.get("scheme") != entry.get("scheme"):', "        if False:"),
    ("N6W4", "the explicit replacement compares the relation case-insensitively", '        if target.get("relation") != entry.get("relation") or target.get("scheme") != entry.get("scheme"):', '        if str(target.get("relation")).lower() != str(entry.get("relation")).lower() or target.get("scheme") != entry.get("scheme"):'),
]
NEW6_IDS = {m[0] for m in NEW6_MUTANTS}

NEW_IDS = NEW6_IDS                                # --only-new: the sixth-pass mutants
MUTANTS = MUTANTS + NEW_MUTANTS + NEW5_MUTANTS + NEW6_MUTANTS


def load_mutated(src: str, name: str = "zenodo_publish", path: Path = SRC_PATH):
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    exec(compile(src, str(path), "exec"), mod.__dict__)
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


def shard_of(items, argv):
    """`--shard K/N` runs only the items whose index modulo N is K (to spread the run over several processes;
    the union of the N shards is the full run)."""
    if "--shard" not in argv:
        return list(items)
    k, n = (int(x) for x in argv[argv.index("--shard") + 1].split("/"))
    return [it for i, it in enumerate(items) if i % n == k]


def main() -> int:
    if "--count" in sys.argv:                          # the numbers the README states, computed from this script
        print(f"mutants: {len(MUTANTS)}, new: {len(NEW_IDS)}")
        return 0
    src = SRC_PATH.read_text(encoding="utf-8")
    n, f, e = run_suite(load_mutated(src))
    print(f"baseline: {n} tests, {f} failures, {e} errors", flush=True)
    if f or e:
        print("baseline is not green: cannot judge mutants")
        return 2
    survivors = []
    mutants = MUTANTS
    if "--only-new" in sys.argv:                      # the fourth-pass mutants and the five round-20 survivors
        mutants = [m for m in mutants if m[0] in NEW_IDS]
    if "--ids" in sys.argv:                           # --ids P1,P2,RS28
        wanted = set(sys.argv[sys.argv.index("--ids") + 1].split(","))
        mutants = [m for m in mutants if m[0] in wanted]
    mutants = shard_of(mutants, sys.argv[1:])
    real_pc = None
    for mid, desc, old, new, *target in mutants:
        if target and target[0] != SRC_PATH.name:        # a mutant of another module (predeposit_check.py)
            tpath = TOOLS / target[0]
            tsrc = tpath.read_text(encoding="utf-8")
            if tsrc.count(old) != 1:
                print(f"{mid:5} SKIPPED (pattern occurs {tsrc.count(old)} times): {desc}", flush=True)
                survivors.append(mid)
                continue
            stem = tpath.stem
            sys.path.insert(0, str(TOOLS))
            real_pc = sys.modules.get(stem)
            sys.modules[stem] = load_mutated(tsrc.replace(old, new), stem, tpath)
            try:
                n, f, e = run_suite(load_mutated(src))
            finally:
                if real_pc is not None:
                    sys.modules[stem] = real_pc
                else:
                    sys.modules.pop(stem, None)
                sys.path.pop(0)
        else:
            if src.count(old) != 1:
                print(f"{mid:5} SKIPPED (pattern occurs {src.count(old)} times): {desc}", flush=True)
                survivors.append(mid)
                continue
            n, f, e = run_suite(load_mutated(src.replace(old, new)))
        killed = (f + e) > 0
        print(f"{mid:5} {'killed  ' if killed else 'SURVIVED'} ({f} failures, {e} errors of {n}): {desc}", flush=True)
        if not killed:
            survivors.append(mid)
    unexpected = [m for m in survivors if m not in EQUIVALENT_SURVIVORS]
    expected_seen = [m for m in survivors if m in EQUIVALENT_SURVIVORS]
    print(f"mutants: {len(mutants)}, killed: {len(mutants) - len(survivors)}, survivors: {survivors or 'none'}")
    if expected_seen:
        print(f"documented equivalent survivor(s): {expected_seen}")
    if unexpected:
        print(f"UNEXPECTED survivor(s): {unexpected}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
