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
    ("C4", "reconcile accepts a journal that is not unreconciled", "    if data.get(\"outcome\") not in BLOCKING_OUTCOMES:\n        raise ReconcileRefused(f\"the journal {path.name} is not unreconciled", "    if False:\n        raise ReconcileRefused(f\"the journal {path.name} is not unreconciled"),
    ("C4b", "reconcile published without a record id", "        if not record_id:\n            raise ReconcileRefused(", "        if False:\n            raise ReconcileRefused("),
    ("C5", "a draft left behind does not block", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", \"DRAFT-LEFT-BEHIND\", UNSAVED_OUTCOME)", "BLOCKING_OUTCOMES = (\"RUNNING\", \"UNRECONCILED\", UNSAVED_OUTCOME)"),
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
    ("R1", "reconcile writes the paper directory the operator names, not the journal's", "    if paper_dir_arg is not None and _realpath(paper_dir_arg) != _realpath(data.get(\"paper_dir_real\") or jdir):", "    if False:"),
    ("R2", "zenodo.json of another concept overwritten", "            if cfg.get(\"zenodo_id\") not in (None, \"\") and str(cfg[\"zenodo_id\"]) != str(cid):", "            if False:"),
    ("R3", "zenodo.json of another bib_key overwritten", "            if data.get(\"bib_key\") and cfg.get(\"bib_key\") != data[\"bib_key\"]:", "            if False:"),
    ("R4", "not-published needs no evidence", "        elif checked_account and reason:", "        elif True:"),
    ("R4b", "the account check needs no reason", "        elif checked_account and reason:", "        elif checked_account:"),
    ("R5", "record URL of any host accepted", "            if not str(record_url).startswith(f\"https://{host}/\"):", "            if False:"),
    ("R6", "a succeeded publication reconciled as not-published", "        if data.get(\"outcome\") == UNSAVED_OUTCOME:\n            raise ReconcileRefused(\n                \"the journal says", "        if False:\n            raise ReconcileRefused(\n                \"the journal says"),
    ("R7", "published reconciled without the identifiers to save", "            if not cid or not cdoi:", "            if False:"),
    ("R8", "the config written by a reconcile is not read back", "            if str(check.get(\"zenodo_id\")) != str(cid) or check.get(\"doi\") != cdoi:", "            if False:"),
    ("R9", "quarantine deletes instead of renaming", "    os.rename(path, target)", "    os.unlink(path)"),
    ("R10", "quarantine overwrites an earlier quarantined file", "    while target.exists() or target.is_symlink():", "    while False:"),
    ("R11", "quarantine accepts a readable journal", "    if readable:\n        raise ReconcileRefused(f\"{path} is a readable journal", "    if False:\n        raise ReconcileRefused(f\"{path} is a readable journal"),
    # secrets
    ("S1", "journal values not redacted before writing", "    return json.dumps(redact_obj(data), indent=2, ensure_ascii=False) + \"\\n\"", "    return json.dumps(data, indent=2, ensure_ascii=False) + \"\\n\""),
    ("S2", "Bearer strings no longer masked", "    out = _BEARER.sub(\"Bearer [REDACTED]\", out)", "    out = out"),
    ("S3", "access_token query strings no longer masked", "    out = _TOKEN_QUERY.sub(lambda m: m.group(1) + \"=[REDACTED]\", out)", "    out = out"),
    ("S4", "the tokens loaded in the process are not registered", "    register_environment_secrets()        # the exact tokens loaded in this process are masked everywhere", "    pass"),
    ("S5", "locate_own_state returns an unredacted exception text", "return \"unknown\", None, [rid], f\"cannot read record {rid} ({redact(exc)})\"", "return \"unknown\", None, [rid], f\"cannot read record {rid} ({exc})\""),
    ("S6", "Authorization header values no longer masked", "    return _AUTH_HEADER.sub(lambda m: \"Authorization\" + m.group(1) + \"[REDACTED]\", out)", "    return out"),
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
    ("D1", "no directory fsync after a journal replace", "    os.replace(tmp, path)\n    _fsync_dir(path.parent)\n\n\ndef _create_exclusive_text", "    os.replace(tmp, path)\n\n\ndef _create_exclusive_text"),
    ("D2", "directory fsync errors always raised", "            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):", "            if True:"),
    ("D3", "directory fsync errors never raised", "            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):", "            if False:"),
    ("D4", "sandbox state committed with an overwriting replace", "            os.link(self.tmp, self.path)\n", "            os.replace(self.tmp, self.path)\n"),
    ("B8", "GET attempts unbounded", "(\"ZENODO_GET_ATTEMPTS\", DEFAULT_GET_ATTEMPTS, None, False, float(MAX_GET_ATTEMPTS)),", "(\"ZENODO_GET_ATTEMPTS\", DEFAULT_GET_ATTEMPTS, None, False, None),"),
    ("B9", "upper bounds not enforced", "        if maximum is not None and value > maximum:", "        if False:"),
    # --update
    ("W1", "--set-web-url replaces the whole related_identifiers list", "            payload[key] = merge_related_web_url(live.get(key), entries[0])", "            payload[key] = copy.deepcopy(entries)"),
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


def shard_of(items, argv):
    """`--shard K/N` runs only the items whose index modulo N is K (to spread the run over several processes;
    the union of the N shards is the full run)."""
    if "--shard" not in argv:
        return list(items)
    k, n = (int(x) for x in argv[argv.index("--shard") + 1].split("/"))
    return [it for i, it in enumerate(items) if i % n == k]


def main() -> int:
    src = SRC_PATH.read_text(encoding="utf-8")
    n, f, e = run_suite(load_mutated(src))
    print(f"baseline: {n} tests, {f} failures, {e} errors", flush=True)
    if f or e:
        print("baseline is not green: cannot judge mutants")
        return 2
    survivors = []
    mutants = shard_of(MUTANTS, sys.argv[1:])
    for mid, desc, old, new in mutants:
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
