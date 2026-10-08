"""Offline tests for the second corrective pass (review round 18): archive safety, live-preserving --update,
the independent version-label guard, the durable run journal and reconcile, sandbox-state safety, header
handling with real `requests` objects, secret redaction, startup validation, the cleanup-call table, and
the tests that kill the mutants N2, N8, N13, N19, O9, O17.

No network: a socket guard fails any real connection; HTTP is the scripted Sim / FakeSession / FakeClient.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import (F, HOST, PROD_TOKEN, SBX_TOKEN, URL, FakeClient, FakeSession, Resp, Sim,  # noqa: E402
                   make_paper, new, real_client, run, tree_checksums, upd)
import requests  # noqa: E402
import zenodo_publish as zp  # noqa: E402
from test_zenodo_contract import Factory, run_main, run_new_real, state_dir_for, workspace  # noqa: E402

PDF = None
ENV = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}


def setUpModule():
    global PDF
    fakes.install_socket_guard()
    PDF = Path(tempfile.mkdtemp()) / "p.pdf"
    PDF.write_bytes(b"%PDF-fake")


def tearDownModule():
    fakes.remove_socket_guard()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def journals(root):
    d = state_dir_for(root)
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.run.json"))]


def main_with_sim(sim, extra=(), cfg=None, env=ENV, skip_checks=True):
    root, paper = workspace(**(cfg or {}))
    client, _ = real_client(sim)
    argv = [str(paper), *(["--skip-checks"] if skip_checks else []), *extra]
    res = run_main(argv, env, root, Factory(client))
    return root, paper, client, res


# ── (A) archive ───────────────────────────────────────────────────────────────────────────────

class ArchiveNeverOverwrites(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.pdf = self.root / "src.pdf"
        self.pdf.write_bytes(b"%PDF-good")

    def test_identical_existing_archive_is_reported_and_accepted(self):
        (self.root / "pdfs").mkdir()
        (self.root / "pdfs" / "P_1.0.pdf").write_bytes(b"%PDF-good")
        code, out, _ = run(zp.archive_pdf, self.root, self.pdf, "P", "1.0", 5, expected_sha256=sha(b"%PDF-good"))
        self.assertIsNone(code)
        self.assertIn("already archived, identical", out.lower())

    def test_different_existing_archive_is_refused_with_both_digests_and_untouched(self):
        (self.root / "pdfs").mkdir()
        dest = self.root / "pdfs" / "P_1.0.pdf"
        dest.write_bytes(b"%PDF-OTHER")
        with self.assertRaises(zp.ArchiveRefused) as cm:
            zp.archive_pdf(self.root, self.pdf, "P", "1.0", 5)
        self.assertIn(sha(b"%PDF-OTHER"), str(cm.exception))
        self.assertIn(sha(b"%PDF-good"), str(cm.exception))
        self.assertEqual(dest.read_bytes(), b"%PDF-OTHER")

    def test_a_pdf_that_is_not_the_uploaded_one_is_refused_and_nothing_written(self):
        with self.assertRaises(zp.ArchiveRefused):
            zp.archive_pdf(self.root, self.pdf, "P", "1.0", 5, expected_sha256=sha(b"%PDF-uploaded"))
        self.assertFalse((self.root / "pdfs" / "P_1.0.pdf").exists())

    def test_first_archive_is_written_and_verified(self):
        code, out, _ = run(zp.archive_pdf, self.root, self.pdf, "P", "1.0", 5, expected_sha256=sha(b"%PDF-good"))
        self.assertIsNone(code)
        self.assertEqual((self.root / "pdfs" / "P_1.0.pdf").read_bytes(), b"%PDF-good")


class UploadDigestAndFinish(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN}

    def test_journal_records_the_sha256_of_the_uploaded_bytes_at_upload_time(self):
        sim = Sim()
        root, paper, client, (code, out, err) = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        self.assertIsNone(code, code)
        sent = [kw["data"] for m, u, kw in sim.calls if m == "PUT" and "/api/files/" in u]
        self.assertEqual(len(sent), 1)
        (j,) = journals(root)
        self.assertEqual(j["pdf"]["sha256"], sha(sent[0]))
        self.assertEqual(j["pdf"]["size"], len(sent[0]))
        self.assertEqual(j["outcome"], "PUBLISHED")

    def test_upload_refuses_a_file_that_changed_after_the_digest(self):
        c, sess = real_client()
        p = Path(tempfile.mkdtemp()) / "f.pdf"
        p.write_bytes(b"one")
        with self.assertRaises(zp.VerificationError):
            c.upload_file({"links": {"bucket": "https://zenodo.org/api/files/b"}}, p, expected_sha256=sha(b"two"))
        self.assertEqual(sess.calls, [])

    def published_workspace(self, **cfg):
        root, paper = workspace(**cfg)
        client = FakeClient(published_custom="https://wrong")       # post-publish FAIL, record published
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(client))
        self.assertIn("--finish-after-fail", code)
        return root, paper

    def test_finish_after_fail_completes_when_the_pdf_is_the_uploaded_one(self):
        root, paper = self.published_workspace()
        code, out, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIsNone(code, code)
        self.assertEqual((root / "pdfs" / "P_1.0.pdf").read_bytes(), b"%PDF-fake")

    def test_finish_after_fail_cannot_replace_a_good_archive(self):
        root, paper = self.published_workspace()
        dest = root / "pdfs" / "P_1.0.pdf"
        dest.write_bytes(b"%PDF-GOOD-ARCHIVE")
        before = tree_checksums(root)
        code, _, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIn("refus", str(code).lower())
        self.assertEqual(dest.read_bytes(), b"%PDF-GOOD-ARCHIVE")
        self.assertEqual(before, tree_checksums(root))

    def test_finish_after_fail_refuses_a_rebuilt_pdf(self):
        root, paper = self.published_workspace()
        (paper / "out" / "P.pdf").write_bytes(b"%PDF-REBUILT")
        code, _, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIn("not the PDF that was uploaded", str(code))
        self.assertFalse((root / "pdfs" / "P_1.0.pdf").exists())

    def test_finish_after_fail_without_any_recorded_digest_refuses_unless_given_one(self):
        root, paper = self.published_workspace()
        for p in state_dir_for(root).glob("*.run.json"):
            data = json.loads(p.read_text())
            data["pdf"] = None
            p.write_text(json.dumps(data))
        code, _, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIn("no digest of the uploaded PDF", str(code))
        code, _, _ = run_main([str(paper), "--finish-after-fail", "--expect-pdf-sha256", sha(b"%PDF-fake")],
                              {}, root, Factory())
        self.assertIsNone(code, code)

    def test_finish_after_fail_checks_the_checksum_returned_by_zenodo(self):
        root, paper = self.published_workspace()
        for p in state_dir_for(root).glob("*.run.json"):
            data = json.loads(p.read_text())
            data["published_files"] = [{"filename": "P.pdf", "checksum": "0" * 32}]
            p.write_text(json.dumps(data))
        code, _, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIn("differs from the local file", str(code))
        self.assertFalse((root / "pdfs" / "P_1.0.pdf").exists())


# ── (B) --update ──────────────────────────────────────────────────────────────────────────────

class UpdatePreservesLiveMetadata(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN}

    def update_run(self, extra, client=None, cfg=None):
        root, paper = workspace(zenodo_id=5, doi="10.0/c", **(cfg or {}))
        client = client or FakeClient()
        res = run_main([str(paper), "--skip-checks", "--update", *extra], self.ENV, root, Factory(client))
        return root, paper, client, res

    def test_update_without_any_field_is_refused(self):
        _, _, client, (code, _, _) = self.update_run([])
        self.assertEqual(code, 2)
        self.assertEqual(client.calls, [])

    def test_only_the_requested_field_changes(self):
        c = FakeClient(live_custom={"code:programmingLanguage": ["py"]})
        _, _, c, (code, out, _) = self.update_run(["--set-code-url", "--yes-apply-diff"], client=c)
        self.assertIsNone(code, code)
        self.assertEqual(c.meta_put["title"], "t")                       # the local title "T" was NOT applied
        for key in ("version", "description", "keywords", "related_identifiers", "creators"):
            self.assertNotIn(key, c.meta_put)
        self.assertEqual(c.meta_put["custom"], {"code:programmingLanguage": ["py"], F: URL})

    def test_local_title_version_abstract_keywords_need_their_own_flag(self):
        for flag, key, value in (("--set-title", "title", "T"), ("--set-version", "version", "1.0"),
                                 ("--set-abstract", "description", "A")):
            _, _, c, (code, _, _) = self.update_run([flag, "--yes-apply-diff"])
            self.assertIsNone(code, (flag, code))
            self.assertEqual(c.meta_put.get(key), value, flag)
            others = {"title", "version", "description"} - {key}
            for o in others:
                self.assertNotEqual(c.meta_put.get(o), {"title": "T", "version": "1.0", "description": "A"}[o], flag)

    def test_apply_local_metadata_list_is_equivalent(self):
        _, _, c, (code, _, _) = self.update_run(["--apply-local-metadata=title,version", "--yes-apply-diff"])
        self.assertIsNone(code, code)
        self.assertEqual((c.meta_put["title"], c.meta_put["version"]), ("T", "1.0"))

    def test_diff_is_printed_and_without_the_flag_nothing_is_created(self):
        _, _, c, (code, out, _) = self.update_run(["--set-title"])
        self.assertIn("--- live", out)
        self.assertIn("+++ to-be-sent", out)
        self.assertIn('-  "title": "t"', out)
        self.assertIn('+  "title": "T"', out)
        self.assertIn("diff sha256:", out)
        self.assertIn("--yes-apply-diff", str(code))
        for n in ("edit", "put", "publish", "discard"):
            self.assertNotIn(n, c.names())

    def test_pinned_digest_must_match_the_diff(self):
        _, _, c, (code, out, _) = self.update_run(["--set-title"])
        digest = [ln for ln in out.splitlines() if ln.strip().startswith("diff sha256:")][0].split()[-1]
        _, _, c, (code, _, _) = self.update_run(["--set-title", "--yes-apply-diff", "0" * 16])
        self.assertIn("does not match", str(code))
        self.assertNotIn("edit", c.names())
        _, _, c, (code, _, _) = self.update_run(["--set-title", "--yes-apply-diff", digest[:16]])
        self.assertIsNone(code, code)
        self.assertIn("publish", c.names())

    def test_nothing_to_update_when_the_live_record_already_matches(self):
        class Same(FakeClient):
            def get_deposition(self, i):
                d = super().get_deposition(i)
                d["metadata"]["title"] = "T"
                return d
        _, _, c, (code, out, _) = self.update_run(["--set-title", "--yes-apply-diff"], client=Same())
        self.assertIsNone(code)
        self.assertIn("Nothing to update", out)
        self.assertNotIn("edit", c.names())

    def test_live_metadata_changed_after_the_diff_aborts_and_discards_the_edit_session(self):
        class Drift(FakeClient):
            def get_deposition(self, i):
                d = super().get_deposition(i)
                if self.created_state is not None and i == 10:
                    d["metadata"] = {"title": "changed by someone else", "custom": {}}
                return d
        _, _, c, (code, _, err) = self.update_run(["--set-title", "--yes-apply-diff"], client=Drift())
        self.assertIn("changed between", err)
        self.assertNotIn("put", c.names())
        self.assertIn(("discard", 10), c.calls)

    def test_dry_run_shows_the_diff_offline_from_a_live_snapshot(self):
        root, paper = workspace(zenodo_id=5)
        snap = Path(tempfile.mkdtemp()) / "live.json"
        snap.write_text(json.dumps({"metadata": {"title": "old", "version": "0.9", "custom": {}}}))
        code, out, _ = run_main([str(paper), "--dry-run", "--update", "--set-title",
                                 "--live-snapshot", str(snap)], {}, root, Factory())
        self.assertIsNone(code, code)
        self.assertIn('-  "title": "old"', out)
        self.assertIn('+  "title": "T"', out)
        self.assertNotIn('"version": "1.0"', out.split("Whole-metadata diff")[1])


class ScratchCopyOutsideTheWorkspace(unittest.TestCase):
    def test_a_paper_directory_outside_the_workspace_runs(self):
        root = Path(tempfile.mkdtemp()).resolve()
        scratch = Path(tempfile.mkdtemp()).resolve()
        paper = make_paper(scratch)
        code, out, _ = run_main([str(paper), "--dry-run"], {}, root, Factory())
        self.assertIsNone(code, code)
        self.assertIn("Dry run", out)


class VersionLabelGuard(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN}

    def git(self, paper, *args):
        subprocess.run(["git", "-C", str(paper), *args], check=True, capture_output=True)

    def test_version_main_is_refused_even_with_skip_checks(self):
        for v in ("main", "master", "HEAD", "Main"):
            root, paper = workspace(version=v)
            f = Factory()
            code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, f)
            self.assertIn("version label refused", str(code), v)
            self.assertEqual(f.built, [], v)

    def test_update_with_set_version_main_is_refused(self):
        root, paper = workspace(version="main", zenodo_id=5)
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks", "--update", "--set-version", "--yes-apply-diff"],
                              self.ENV, root, f)
        self.assertIn("version label refused", str(code))
        self.assertEqual(f.built, [])

    def test_update_without_set_version_does_not_send_a_label_so_it_is_not_judged(self):
        root, paper = workspace(version="main", zenodo_id=5)
        code, _, _ = run_main([str(paper), "--skip-checks", "--update", "--set-title", "--yes-apply-diff"],
                              self.ENV, root, Factory())
        self.assertIsNone(code, code)

    def test_branch_rule_main_branch_and_mismatch(self):
        root, paper = workspace(version="1.0")
        self.git(paper, "init", "-q", "-b", "main")
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, f)
        self.assertIn("never published", str(code))
        self.assertEqual(f.built, [])
        self.git(paper, "checkout", "-q", "-b", "2.0")
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, f)
        self.assertIn("!= git branch", str(code))
        self.assertEqual(f.built, [])
        self.git(paper, "checkout", "-q", "-b", "1.0")
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, f)
        self.assertIsNone(code, code)
        self.assertEqual(len(f.built), 1)

    def test_citation_cff_version_must_agree(self):
        root, paper = workspace(version="1.0")
        (paper / "CITATION.cff").write_text('cff-version: 1.2.0\nversion: "0.9"\n')
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory())
        self.assertIn("CITATION.cff", str(code))

    def test_new_version_equal_to_the_live_version_is_refused_before_creating_anything(self):
        c = FakeClient(record_version="1.0")
        code, _, _ = run(new, c)
        self.assertIn("equals the live version", code)
        for n in ("newversion", "create", "upload", "put", "publish"):
            self.assertNotIn(n, c.names())

    def test_a_different_live_version_is_accepted(self):
        c = FakeClient(record_version="0.9")
        code, _, _ = run(new, c)
        self.assertIsNone(code, code)

    def test_gate_that_cannot_run_fails_closed_and_skip_checks_bypasses_only_the_gate(self):
        for broken in (None, mock.Mock(check=mock.Mock(side_effect=RuntimeError("boom")))):
            root, paper = workspace()
            f = Factory()
            with mock.patch.dict(sys.modules, {"predeposit_check": broken}):
                code, _, _ = run_main([str(paper)], self.ENV, root, f)
            self.assertIn("could not run", str(code))
            self.assertEqual(f.built, [])
        root, paper = workspace(version="main")
        with mock.patch.dict(sys.modules, {"predeposit_check": None}):
            code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory())
        self.assertIn("version label refused", str(code))       # --skip-checks does not skip the guard


# ── (C) journal, reconcile, do-not-rerun ─────────────────────────────────────────────────────

class JournalSpy(Sim):
    """Records what the journal file said at the instant each write request left."""

    def __init__(self, journal_dir, **modes):
        super().__init__(**modes)
        self.journal_dir, self.seen = journal_dir, []

    def _do(self, method, url, **kw):
        if method != "GET":
            files = sorted(self.journal_dir.glob("*.run.json"))
            self.seen.append((method, url.split("/api/", 1)[-1],
                              json.loads(files[-1].read_text()) if files else None))
        return super()._do(method, url, **kw)


class RunJournal(unittest.TestCase):
    def test_journal_is_on_disk_before_each_write_request_leaves(self):
        root, paper = workspace(zenodo_id=5)
        spy = JournalSpy(state_dir_for(root))
        client, _ = real_client(spy)
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory(client))
        self.assertIsNone(code, code)
        writes = [(m, p, j) for m, p, j in spy.seen if "/files/" not in p or m == "PUT"]
        self.assertTrue(writes)
        for method, path, j in spy.seen:
            self.assertIsNotNone(j, (method, path))
            self.assertEqual(j["last_write"], f"{method} /api/{path.split('?')[0]}", (method, path))
        publish = [j for m, p, j in spy.seen if p.endswith("/actions/publish")][0]
        self.assertEqual(publish["outcome"], "UNRECONCILED")          # from before the request is sent

    def test_journal_is_atomic_and_leaves_no_temp_file(self):
        root, paper, client, (code, _, _) = main_with_sim(Sim(), cfg={"zenodo_id": 5, "doi": "10.0/c"})
        names = [p.name for p in state_dir_for(root).iterdir()]
        self.assertTrue(all(n.endswith(".run.json") for n in names), names)
        (j,) = journals(root)
        for key in ("phase", "deposition_id", "concept_id", "intent", "pdf", "outcome"):
            self.assertIn(key, j)
        self.assertEqual(j["concept_id"], "5")
        self.assertEqual(j["host"], "zenodo.org")

    def test_journal_that_cannot_be_written_stops_the_request(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        c, sess = real_client()
        c.journal = j
        with mock.patch.object(zp, "_atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(zp.JournalError):
                code, _, _ = run(c.create_deposition)
                raise zp.JournalError("x") if code is None else AssertionError(code)
        self.assertEqual(sess.calls, [])

    def test_journal_directory_inside_the_workspace_is_refused(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--skip-checks", "--state-dir", str(root / "journals")], ENV, root,
                              Factory())
        self.assertIn("lies inside", str(code))

    def test_unknown_publish_outcome_blocks_every_later_run_and_says_do_not_rerun(self):
        sim = Sim(publish="timeout_pending")
        root, paper, client, (code, out, err) = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        self.assertIn("DO NOT RE-RUN", str(code) + err)
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "UNRECONCILED")
        self.assertEqual(sim.discarded, [])
        f = Factory()
        code, out, err = run_main([str(paper), "--skip-checks"], ENV, root, f)
        self.assertIn("UNRECONCILED", str(code))
        self.assertIn("DO NOT RE-RUN", str(code))
        self.assertIn("--reconcile", str(code))
        self.assertEqual(f.built, [])
        self.assertEqual(f.client.calls, [])
        # a run for the same concept from another path is blocked too
        root2, paper2 = workspace(zenodo_id=5)
        for p in state_dir_for(root).glob("*.run.json"):
            (state_dir_for(root2) / p.name).write_text(p.read_text())
        code, _, _ = run_main([str(paper2), "--skip-checks"], ENV, root2, Factory())
        self.assertIn("REFUSING TO START", str(code))

    def test_unreadable_journal_of_the_paper_blocks(self):
        root, paper = workspace()
        (state_dir_for(root) / "paper-20260101T000000Z-1-abc123.run.json").write_text("{not json")
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertIn("REFUSING TO START", str(code))

    def test_reconcile_not_published_unblocks_and_never_touches_the_network(self):
        sim = Sim(publish="timeout_pending")
        root, paper, client, _ = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        (jpath,) = state_dir_for(root).glob("*.run.json")
        before_cfg = (paper / "zenodo.json").read_text()
        f = Factory()
        code, out, _ = run_main([str(paper), "--reconcile", str(jpath), "--outcome", "not-published",
                                 "--i-checked-the-account", "--reason", "looked at /deposit and the record"],
                                {}, root, f)
        self.assertIsNone(code, code)
        self.assertEqual(f.built, [])
        self.assertEqual(json.loads(jpath.read_text())["outcome"], "NOT-PUBLISHED-RECONCILED")
        self.assertEqual((paper / "zenodo.json").read_text(), before_cfg)
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertIsNone(code, code)

    def test_reconcile_published_needs_the_record_id_and_can_save_the_ids(self):
        sim = Sim(publish="timeout_pending")
        root, paper, client, _ = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        (jpath,) = state_dir_for(root).glob("*.run.json")
        code, _, _ = run_main([str(paper), "--reconcile", str(jpath), "--outcome", "published"], {}, root)
        self.assertIn("--record-id", str(code))
        self.assertEqual(json.loads(jpath.read_text())["outcome"], "UNRECONCILED")
        code, out, _ = run_main([str(paper), "--reconcile", str(jpath), "--outcome", "published",
                                 "--record-id", "301", "--concept-id", "5", "--concept-doi", "10.5281/zenodo.0"],
                                {}, root)
        self.assertIsNone(code, code)
        data = json.loads(jpath.read_text())
        self.assertEqual((data["outcome"], data["record_id"], data["operator_asserted"]),
                         ("PUBLISHED-RECONCILED", 301, True))
        cfg = json.loads((paper / "zenodo.json").read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, "10.5281/zenodo.0"))

    def test_reconcile_of_a_sandbox_journal_never_writes_zenodo_json(self):
        root, paper = workspace()
        j = zp.Journal.create(state_dir_for(root), paper, "sandbox.zenodo.org", True, {})
        j.update(outcome="UNRECONCILED")
        before = (paper / "zenodo.json").read_text()
        code, out, _ = run_main([str(paper), "--reconcile", str(j.path), "--outcome", "published",
                                 "--record-id", "9", "--concept-id", "8", "--concept-doi", "10.5072/x"], {}, root)
        self.assertIsNone(code, code)
        self.assertEqual((paper / "zenodo.json").read_text(), before)

    def test_reconcile_refuses_a_journal_that_is_not_unreconciled(self):
        root, paper, client, _ = main_with_sim(Sim(), cfg={"zenodo_id": 5, "doi": "10.0/c"})
        (jpath,) = state_dir_for(root).glob("*.run.json")
        code, _, _ = run_main([str(paper), "--reconcile", str(jpath), "--outcome", "not-published"], {}, root)
        self.assertIn("not unreconciled", str(code))

    def test_ctrl_c_right_after_publish_returns_prints_do_not_rerun_and_reraises(self):
        sim = Sim()

        def interrupt(result):
            raise KeyboardInterrupt()
        client, (code, out, err) = run_new_real(sim, on_published=interrupt)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertIn("DO NOT re-run", err)
        self.assertIn("DO NOT RE-RUN", err)

    def test_ctrl_c_during_publish_prints_do_not_rerun(self):
        client, (code, _, err) = run_new_real(Sim(publish="interrupt"))
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertIn("DO NOT RE-RUN", err)

    def test_sandbox_runs_use_the_journal(self):
        root, paper = workspace()
        state = Path(tempfile.mkdtemp()) / "s.json"
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], ENV, root, f)
        self.assertIsNone(code, code)
        (j,) = journals(root)
        self.assertTrue(j["sandbox"])
        self.assertEqual(j["host"], "sandbox.zenodo.org")
        self.assertEqual(j["outcome"], "PUBLISHED")

    def test_failed_cleanup_leaves_a_blocking_journal_with_the_manual_url(self):
        sim = Sim(upload="500")
        sim.discard_status = 500
        root, paper, client, (code, _, _) = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "DRAFT-LEFT-BEHIND")
        self.assertIn("/deposit/301", j["manual_url"])
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertIn("REFUSING TO START", str(code))

    def test_clean_failure_does_not_block(self):
        root, paper, client, (code, _, _) = main_with_sim(Sim(upload="500"), cfg={"zenodo_id": 5, "doi": "10.0/c"})
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "NOT-PUBLISHED")
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertIsNone(code, code)

    def test_guard_refusal_leaves_a_non_blocking_journal(self):
        sim = Sim()
        sim.add_draft(77, concept="5")
        root, paper, client, (code, _, _) = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        self.assertIn("already exists", code)
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "NOT-PUBLISHED")


# ── (D) sandbox state file ───────────────────────────────────────────────────────────────────

class SandboxStateSafety(unittest.TestCase):
    def sandbox(self, state, root_paper=None, patches=()):
        root, paper = root_paper or workspace()
        f = Factory()
        res = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], ENV, root, f,
                       extra=list(patches))
        return root, paper, f, res

    def test_existing_state_file_is_refused_and_left_untouched(self):
        state = Path(tempfile.mkdtemp()) / "precious.json"
        state.write_text("precious")
        _, _, f, (code, _, _) = self.sandbox(state)
        self.assertIn("already exists", str(code))
        self.assertEqual(state.read_text(), "precious")
        self.assertEqual(f.built, [])

    def test_unwritable_location_is_found_before_any_request(self):
        state = Path(tempfile.mkdtemp()) / "s.json"
        with mock.patch.object(zp.SandboxStateSlot, "reserve", side_effect=OSError("read-only")):
            _, _, f, (code, _, _) = self.sandbox(state)
        self.assertIn("cannot be created", str(code))
        self.assertEqual(f.built, [])

    def test_probe_file_is_the_one_committed_and_leaves_no_residue(self):
        d = Path(tempfile.mkdtemp())
        state = d / "s.json"
        _, _, f, (code, out, _) = self.sandbox(state)
        self.assertIsNone(code, code)
        self.assertEqual([p.name for p in d.iterdir()], ["s.json"])
        self.assertTrue(json.loads(state.read_text())["sandbox"])

    def test_write_failure_after_publication_prints_everything_and_never_suggests_production_edits(self):
        state = Path(tempfile.mkdtemp()) / "s.json"
        _, paper, f, (code, out, err) = self.sandbox(
            state, patches=[mock.patch.object(zp.SandboxStateSlot, "commit", side_effect=OSError("disk full"))])
        self.assertIsNone(code, code)
        self.assertIn('"record_id"', out)
        self.assertIn("do NOT edit any production zenodo.json", err)
        self.assertNotIn("BY HAND", err)

    def test_failed_local_save_message_is_mode_aware(self):
        self.assertIn("SANDBOX", zp._manual_state_hint(True))
        self.assertNotIn("zenodo_id (concept recid) and doi in zenodo.json BY HAND", zp._manual_state_hint(True))
        prod = zp._manual_state_hint(False)
        self.assertIn("PUBLISHED-UNSAVED", prod)
        self.assertIn("--reconcile", prod)
        self.assertIn("zenodo.json", prod)


# ── R5: real requests objects ────────────────────────────────────────────────────────────────

def real_response(body, headers=None, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.encoding = "utf-8"
    r.headers = requests.structures.CaseInsensitiveDict(headers or {})
    return r


class RealRequestsObjects(unittest.TestCase):
    def test_x_total_is_honoured_with_a_case_insensitive_dict(self):
        for header in ("X-Total", "x-total", "X-TOTAL-COUNT"):
            c, sess = real_client()
            sess.route("GET", "=/deposit/depositions", real_response([{"id": 1}], {header: "3"}),
                       real_response([], {header: "3"}))
            with self.assertRaises(zp.CompletenessError, msg=header):
                c.list_drafts()

    def test_x_total_match_is_accepted(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", real_response([{"id": 1}], {"x-total": "1"}),
                   real_response([], {"x-total": "1"}))
        self.assertEqual(len(c.list_drafts()), 1)

    def test_next_link_header_on_an_empty_page_fails_closed(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", real_response([{"id": 1}]),
                   real_response([], {"Link": '<https://zenodo.org/api/deposit/depositions?page=9>; rel="next"'}))
        with self.assertRaises(zp.CompletenessError):
            c.list_drafts()

    def test_plain_dict_headers_still_work(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", Resp(200, [{"id": 1}], headers={"X-Total": "5"}),
                   Resp(200, [], headers={"X-Total": "5"}))
        with self.assertRaises(zp.CompletenessError):
            c.list_drafts()


# ── R10 / R11 ─────────────────────────────────────────────────────────────────────────────────

class SecretsAndStartup(unittest.TestCase):
    BAD = "abc\ndef-SECRETPART"

    def test_token_with_a_control_character_is_refused_without_echo(self):
        for bad in (self.BAD, "abc def", "tok\x00en", "tökén"):
            with self.assertRaises(SystemExit) as cm:
                zp.resolve_token(False, {"ZENODO_TOKEN": bad})
            self.assertNotIn("SECRETPART", str(cm.exception))
            self.assertNotIn(bad, str(cm.exception))

    def test_client_refuses_such_a_token_without_echo(self):
        with self.assertRaises(zp.InvalidToken) as cm:
            zp.ZenodoClient(self.BAD, session=FakeSession(), environ={})
        self.assertNotIn("SECRETPART", str(cm.exception))

    def test_main_with_a_newline_token_stops_before_anything_and_prints_no_token(self):
        root, paper = workspace()
        f = Factory()
        code, out, err = run_main([str(paper), "--skip-checks"], {"ZENODO_TOKEN": self.BAD}, root, f)
        self.assertIn("printable ASCII", str(code))
        self.assertEqual(f.built, [])
        self.assertNotIn("SECRETPART", f"{code}{out}{err}")

    def test_exception_text_carrying_the_header_is_redacted_everywhere(self):
        zp.register_secret(PROD_TOKEN)
        leak = RuntimeError(f"Invalid leading whitespace, reserved character(s), or return character(s) "
                            f"in header value: 'Bearer {PROD_TOKEN}'")
        c = FakeClient(fail_at="upload", fail_exc=leak)
        code, out, err = run(new, c)
        self.assertNotIn(PROD_TOKEN, f"{code}{out}{err}")
        self.assertIn("[REDACTED]", err)

    def test_http_error_body_echoing_the_token_is_redacted(self):
        zp.register_secret(PROD_TOKEN)
        sim = Sim()
        orig = sim._do

        def leaky(method, url, **kw):
            if method == "PUT" and "/api/files/" in url:
                return Resp(500, text=f"echo Authorization: Bearer {PROD_TOKEN}")
            return orig(method, url, **kw)
        sim._do = leaky
        client, (code, out, err) = run_new_real(sim)
        self.assertNotIn(PROD_TOKEN, f"{code}{out}{err}")

    def test_redact_helper(self):
        zp.register_secret("s3cr3t-value")
        self.assertEqual(zp.redact("x s3cr3t-value y"), "x [REDACTED] y")
        self.assertNotIn("abc", zp.redact("Authorization: Bearer abc"))

    def test_bad_numeric_environment_stops_the_run_before_any_client(self):
        for name, value in (("ZENODO_VERIFY_ATTEMPTS", "nan"), ("ZENODO_VERIFY_ATTEMPTS", "inf"),
                            ("ZENODO_VERIFY_ATTEMPTS", "many"), ("ZENODO_READ_TIMEOUT", "0"),
                            ("ZENODO_CONNECT_TIMEOUT", "-3"), ("ZENODO_RETRY_BACKOFF", "-1"),
                            ("ZENODO_GET_ATTEMPTS", "nan"), ("ZENODO_UPLOAD_TIMEOUT", "inf")):
            root, paper = workspace()
            f = Factory()
            code, _, _ = run_main([str(paper), "--skip-checks"], {"ZENODO_TOKEN": PROD_TOKEN, name: value}, root, f)
            self.assertIn(name, str(code), (name, value))
            self.assertEqual(f.built, [], (name, value))

    def test_good_numeric_environment_is_accepted(self):
        zp.validate_numeric_env({"ZENODO_VERIFY_ATTEMPTS": "3", "ZENODO_READ_TIMEOUT": "5.5"})


# ── R1 / R7: cleanup-call table and latest_draft ─────────────────────────────────────────────

class CleanupCallTable(unittest.TestCase):
    def test_table_contents_and_unverified_marking(self):
        self.assertEqual(zp.CLEANUP_CALLS["edit-session"], ("POST", "/deposit/depositions/{id}/actions/discard"))
        self.assertEqual(zp.CLEANUP_CALLS["unpublished-new-version-draft"], ("DELETE", "/deposit/depositions/{id}"))
        self.assertEqual(zp.CLEANUP_CALLS["unpublished-first-draft"], ("DELETE", "/deposit/depositions/{id}"))
        self.assertEqual(set(zp.CLEANUP_VERIFIED), set(zp.CLEANUP_CALLS))
        self.assertFalse(any(zp.CLEANUP_VERIFIED.values()))          # nothing is VERIFIED before the pilot

    def test_every_created_state_kind_maps_to_a_call(self):
        for kind in ("edit session", "new-version draft", "new deposition draft"):
            self.assertIn(zp.cleanup_call(kind), zp.CLEANUP_CALLS.values())

    def test_unknown_kind_is_refused_and_nothing_is_sent(self):
        with self.assertRaises(zp.UnknownCleanupKind):
            zp.cleanup_call("published record")
        c, sess = real_client()
        with self.assertRaises(zp.UnknownCleanupKind):
            c.discard(5, "published record")
        self.assertEqual(sess.calls, [])

    def test_client_sends_the_table_call_for_each_kind(self):
        for kind, (method, path) in (("edit session", ("POST", "/deposit/depositions/9/actions/discard")),
                                     ("new-version draft", ("DELETE", "/deposit/depositions/9")),
                                     ("new deposition draft", ("DELETE", "/deposit/depositions/9"))):
            c, sess = real_client()
            sess.route(method, "", Resp(201))
            c.discard(9, kind)
            self.assertEqual(sess.calls[0][0], method, kind)
            self.assertTrue(sess.calls[0][1].endswith(path), kind)

    def test_flows_use_delete_for_drafts_and_discard_for_edit_sessions(self):
        sim = Sim(upload="500")
        run_new_real(sim)
        self.assertEqual(sim.cleanup_calls, [("DELETE", 301)])
        sim = Sim(upload="500")
        run_new_real(sim, zid=None)
        self.assertEqual(sim.cleanup_calls, [("DELETE", 301)])
        sim = Sim(put="500x")
        client, _ = real_client(sim)
        sim.modes["publish"] = "400"
        code, _, _ = run(upd, client)
        self.assertEqual(sim.cleanup_calls, [("POST", 10)])

    def test_message_flags_the_unverified_semantics(self):
        client, (code, _, _) = run_new_real(Sim(upload="500"))
        self.assertIn("UNVERIFIED", code)

    def test_refused_cleanup_marks_the_draft_as_left_behind(self):
        sim = Sim(upload="500")
        sim.discard_status = 403
        root, paper, client, (code, _, _) = main_with_sim(sim, cfg={"zenodo_id": 5, "doi": "10.0/c"})
        self.assertIn("CLEANUP FAILED", str(code))
        self.assertEqual(journals(root)[0]["outcome"], "DRAFT-LEFT-BEHIND")


class LatestDraftLink(unittest.TestCase):
    def guard(self, **kw):
        c = FakeClient(**kw)
        return c, run(zp.refuse_if_unpublished_state, c, 5, 10, HOST)

    def test_self_pointing_link_is_not_a_draft(self):
        c, (code, _, _) = self.guard(latest_draft="https://zenodo.org/api/deposit/depositions/10")
        self.assertIsNone(code, code)

    def test_self_pointing_link_is_judged_without_any_extra_read(self):
        c, (code, _, _) = self.guard(latest_draft="https://zenodo.org/api/deposit/depositions/10")
        self.assertIsNone(code, code)
        self.assertEqual(c.calls.count(("get_deposition", 10)), 1)

    def test_link_to_another_unpublished_deposition_refuses(self):
        c, (code, _, _) = self.guard(latest_draft="https://zenodo.org/api/deposit/depositions/77")
        self.assertIn("unpublished", str(code))

    def test_link_to_another_published_deposition_is_not_a_draft(self):
        c, (code, _, _) = self.guard(latest_draft="https://zenodo.org/api/deposit/depositions/77",
                                     draft_state="done")
        self.assertIsNone(code, code)

    def test_link_that_cannot_be_read_or_judged_refuses(self):
        class Unreadable(FakeClient):
            def get_deposition(self, i):
                if i == 77:
                    raise RuntimeError("404")
                return super().get_deposition(i)
        c = Unreadable(latest_draft="https://zenodo.org/api/deposit/depositions/77")
        code, _, _ = run(zp.refuse_if_unpublished_state, c, 5, 10, HOST)
        self.assertIn("cannot be read", str(code))
        c, (code, _, _) = self.guard(latest_draft="https://zenodo.org/api/deposit/depositions/bogus")
        self.assertIn("cannot be parsed", str(code))

        class NoState(FakeClient):
            def get_deposition(self, i):
                d = super().get_deposition(i)
                if i == 77:
                    d.pop("state")
                return d
        c = NoState(latest_draft="https://zenodo.org/api/deposit/depositions/77")
        code, _, _ = run(zp.refuse_if_unpublished_state, c, 5, 10, HOST)
        self.assertIn("ambiguous", str(code))


# ── R8: tests that kill N2, N8, N13, N19, O9, O17 ────────────────────────────────────────────

class KillSurvivors(unittest.TestCase):
    def test_N2_a_draft_that_is_already_done_is_never_discarded(self):
        c = FakeClient(fail_at="upload", draft_state="done")
        code, _, err = run(new, c)
        self.assertNotIn("discard", c.names())
        self.assertIn("nothing to discard", str(code))

    def test_N8_ctrl_c_at_the_update_put_discards_the_edit_session_and_reraises(self):
        sim = Sim(put="interrupt")
        client, _ = real_client(sim)
        code, _, err = run(upd, client)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(sim.cleanup_calls, [("POST", 10)])
        self.assertEqual(sim.discarded, [10])
        self.assertIn("Cleanup performed", err)

    def test_N13_the_snapshot_holds_the_drafts_of_every_concept(self):
        sim = Sim()
        sim.add_draft(50, concept="9")
        sim.add_draft(51, concept="5") if False else None
        client, _ = real_client(sim)
        zp.refuse_if_unpublished_state(client, 5, 10, HOST)
        self.assertIn(50, client.pre_run_draft_ids)

    def test_N13_a_foreign_draft_in_the_snapshot_is_never_discarded_when_misattributed(self):
        sim = Sim()
        sim.add_draft(50, concept="9")
        client, _ = real_client(sim)
        zp.refuse_if_unpublished_state(client, 5, 10, HOST)
        client.created_state = {"dep_id": 50, "kind": "new-version draft", "certain": True}
        code, _, _ = run(zp.abort_and_cleanup, client, "upload", RuntimeError("x"), HOST)
        self.assertEqual(sim.cleanup_calls, [])
        self.assertIn("REFUSING to discard", code)

    def test_N19_the_listing_query_is_exact(self):
        sim = Sim()
        for i in (1, 2, 3):
            sim.add_draft(100 + i, concept="9")
        sim.list_page_cap = 2
        c, _ = real_client(sim)
        c.list_drafts()
        listing = [kw["params"] for m, u, kw in sim.calls if u.endswith("/deposit/depositions")]
        self.assertEqual([p["page"] for p in listing], [1, 2, 3])
        for p in listing:
            self.assertEqual(p["status"], "draft")
            self.assertEqual(p["size"], zp.LIST_PAGE_SIZE)
            self.assertEqual(p["all_versions"], "true")

    def test_O9_an_unreadable_record_after_a_failed_publish_discards_nothing(self):
        c = FakeClient(fail_at="publish", fail_exc=RuntimeError("boom"), unreadable_after_publish=True)
        code, _, err = run(new, c)
        self.assertNotIn("discard", c.names())
        self.assertIn("CLEANUP SKIPPED", str(code))
        self.assertIn("DO NOT RE-RUN", str(code))

    def test_O17_a_get_is_attempted_three_times_by_default(self):
        self.assertEqual(zp.DEFAULT_GET_ATTEMPTS, 3)
        c, sess = real_client()                    # environ has only the zero back-off
        sess.route("GET", "/records/1", requests.exceptions.Timeout("t"))
        with self.assertRaises(requests.exceptions.Timeout):
            c.get_record(1)
        self.assertEqual(len(sess.calls), 3)


if __name__ == "__main__":
    unittest.main()
