"""Offline tests for the third corrective pass (review round 19, F1-F14) and the reviewer's probes
(pa..ph, converted to permanent tests). Every class names the finding, mutant or probe it closes.

No network: a socket guard fails any real connection; HTTP is the scripted Sim / FakeSession / FakeClient.
"""
import copy
import errno
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import (F, PROD_TOKEN, SBX_TOKEN, URL, FakeClient, FakeSession, Resp, Sim,  # noqa: E402
                   make_paper, real_client, run, tree_checksums)
import zenodo_publish as zp  # noqa: E402
from test_zenodo_contract import Factory, run_main, state_dir_for, workspace  # noqa: E402

ENV = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}
PROD_ONLY = {"ZENODO_TOKEN": PROD_TOKEN}


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def journals(root):
    return [json.loads(p.read_text()) for p in sorted(state_dir_for(root).glob("*.run.json"))]


def journal_paths(root):
    return sorted(state_dir_for(root).glob("*.run.json"))


class NoConcept(FakeClient):
    """Probe pg: the publish answer has no conceptdoi / conceptrecid."""

    def publish(self, i):
        self._c("publish", i)
        return {"id": i, "doi": "10.5281/zenodo.1", "metadata": {}}


def write_journal(directory, name, **fields):
    data = {"schema": 2, "paper": "paper", "paper_dir": "/nowhere/paper", "host": "zenodo.org", "sandbox": False,
            "kind": "deposit", "outcome": "UNRECONCILED", "phase": "x", "deposition_id": 99, "concept_id": None,
            "pdf": None, "notes": [], "started_at": "2026-10-08T10:00:00+00:00"}
    data.update(fields)
    path = Path(directory) / name
    path.write_text(json.dumps(data))
    return path


# ── F1: published-but-local-state-unsaved is blocking (Z5, probe pg) ─────────────────────────────

class PublishedUnsavedBlocks(unittest.TestCase):
    def test_Z5_publish_returns_with_a_blocking_journal_that_holds_the_answer(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {})
        c, sess = real_client()
        c.journal = j
        sess.route("POST", "/actions/publish", Resp(202, {"id": 7, "doi": "10.5281/zenodo.7",
                                                          "conceptrecid": "5", "conceptdoi": "10.5281/zenodo.0",
                                                          "files": [{"filename": "p.pdf", "checksum": "md5:aa"}]}))
        c.publish(7)
        on_disk = json.loads(j.path.read_text())
        self.assertEqual(on_disk["outcome"], "PUBLISHED-UNSAVED")
        self.assertIn("PUBLISHED-UNSAVED", zp.BLOCKING_OUTCOMES)
        self.assertEqual((on_disk["record_id"], on_disk["concept_id"], on_disk["concept_doi"]),
                         (7, "5", "10.5281/zenodo.0"))
        self.assertEqual(on_disk["published_files"][0]["checksum"], "md5:aa")

    def test_a_success_answer_that_cannot_be_parsed_is_still_recorded_as_blocking(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {})
        c, sess = real_client()
        c.journal = j
        sess.route("POST", "/actions/publish", Resp(202, nonjson=True))
        with self.assertRaises(zp.MalformedResponse):
            c.publish(7)
        self.assertEqual(json.loads(j.path.read_text())["outcome"], "PUBLISHED-UNSAVED")

    def test_probe_pg_no_concept_in_the_answer_then_a_rerun_is_refused_and_publishes_nothing(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        c1 = NoConcept()
        code, out, err = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(c1))
        self.assertIn("PUBLISHED-UNSAVED", str(code))
        self.assertIn("publish", c1.names())
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "PUBLISHED-UNSAVED")
        c2 = NoConcept()
        f2 = Factory(c2)
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, f2)
        self.assertIn("REFUSING TO START", str(code))
        self.assertIn("--concept-id C --concept-doi D", str(code))          # the real cure is printed
        self.assertEqual(f2.built, [])
        self.assertEqual(c2.calls, [])
        self.assertNotIn("publish", c2.names())

    def test_the_printed_cure_is_accepted_and_lifts_the_block(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(NoConcept()))
        (jp,) = journal_paths(root)
        # without the identifiers to save, the journal stays blocking
        code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "published", "--record-id", "98"], {}, root)
        self.assertIn("--concept-id C --concept-doi D", str(code))
        self.assertEqual(json.loads(jp.read_text())["outcome"], "PUBLISHED-UNSAVED")
        # the cure: writes ONLY the paper directory recorded in the journal (no positional argument needed)
        code, out, _ = run_main(["--reconcile", str(jp), "--outcome", "published", "--record-id", "98",
                                 "--concept-id", "5", "--concept-doi", "10.5281/zenodo.0"], {}, root)
        self.assertIsNone(code, code)
        cfg = json.loads((paper / "zenodo.json").read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, "10.5281/zenodo.0"))
        self.assertEqual(json.loads(jp.read_text())["outcome"], "PUBLISHED-RECONCILED")
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(FakeClient()))
        self.assertNotIn("REFUSING", str(code))

    def test_ctrl_c_between_publish_and_the_zenodo_json_write_leaves_a_blocking_journal(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        real = zp._atomic_write_text

        def interrupt_on_config(path, text):
            if Path(path).name == "zenodo.json":
                raise KeyboardInterrupt()
            return real(path, text)
        code, _, err = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(),
                                extra=[mock.patch.object(zp, "_atomic_write_text", interrupt_on_config)])
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        (j,) = journals(root)
        self.assertEqual(j["outcome"], "PUBLISHED-UNSAVED")
        self.assertIn("DO NOT RE-RUN", err)
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory())
        self.assertIn("REFUSING TO START", str(code))

    def test_a_failed_write_of_zenodo_json_leaves_a_blocking_journal(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        real = zp._atomic_write_text

        def fail_on_config(path, text):
            if Path(path).name == "zenodo.json":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, text)
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(),
                              extra=[mock.patch.object(zp, "_atomic_write_text", fail_on_config)])
        self.assertIn("PUBLISHED-UNSAVED", str(code))
        self.assertEqual(json.loads((paper / "zenodo.json").read_text())["zenodo_id"], None)
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, f)
        self.assertIn("REFUSING TO START", str(code))
        self.assertEqual(f.client.calls, [])

    def test_the_block_is_lifted_only_after_zenodo_json_was_written_and_read_back(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        seen = []
        real = zp._atomic_write_text

        def spy(path, text):
            if Path(path).name == "zenodo.json":
                seen.append(journals(root)[0]["outcome"])          # what the journal says just before the write
            return real(path, text)
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(),
                              extra=[mock.patch.object(zp, "_atomic_write_text", spy)])
        self.assertIsNone(code, code)
        self.assertEqual(seen, ["PUBLISHED-UNSAVED"])
        self.assertEqual(journals(root)[0]["outcome"], "PUBLISHED")
        self.assertNotIn(zp.UNSAVED_OUTCOME, [j["outcome"] for j in journals(root)])

    def test_zenodo_json_not_holding_the_ids_after_writing_keeps_the_block(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        real = zp._atomic_write_text

        def lying_write(path, text):
            if Path(path).name == "zenodo.json":
                return real(path, text.replace('"zenodo_id": 5', '"zenodo_id": 6'))
            return real(path, text)
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(),
                              extra=[mock.patch.object(zp, "_atomic_write_text", lying_write)])
        self.assertIn("PUBLISHED-UNSAVED", str(code))
        self.assertEqual(journals(root)[0]["outcome"], "PUBLISHED-UNSAVED")

    def test_an_update_settles_its_journal_without_any_local_state(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        code, _, _ = run_main([str(paper), "--skip-checks", "--update", "--set-title", "--yes-apply-diff"],
                              PROD_ONLY, root, Factory())
        self.assertIsNone(code, code)
        (j,) = [j for j in journals(root) if j["outcome"] != "NOT-PUBLISHED"]      # the other one is the review preview
        self.assertEqual((j["kind"], j["outcome"]), ("update", "PUBLISHED"))

    def test_sandbox_state_not_committed_keeps_the_journal_blocking(self):
        root, paper = workspace()
        state = Path(tempfile.mkdtemp()) / "s.json"
        code, _, err = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], ENV,
                                root, Factory(),
                                extra=[mock.patch.object(zp.SandboxStateSlot, "commit", side_effect=OSError("x"))])
        self.assertIsNone(code, code)
        self.assertEqual(journals(root)[0]["outcome"], "PUBLISHED-UNSAVED")
        self.assertIn("stays PUBLISHED-UNSAVED", err)

    def test_sandbox_state_committed_lifts_the_block(self):
        root, paper = workspace()
        state = Path(tempfile.mkdtemp()) / "s.json"
        code, _, _ = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], ENV,
                              root, Factory())
        self.assertIsNone(code, code)
        self.assertEqual(journals(root)[0]["outcome"], "PUBLISHED")


# ── F9 / F10: recognising the same paper from another path; host-aware concept block (Y29) ───────

class BlockKeying(unittest.TestCase):
    def blocked(self, root, paper):
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks", "--state-dir", str(state_dir_for(root))], PROD_ONLY,
                              root, f)
        return "REFUSING TO START" in str(code), code

    def unreconciled_first_publication(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        sim = Sim(publish="timeout_pending")
        client, _ = real_client(sim)
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        self.assertEqual(journals(root)[0]["outcome"], "UNRECONCILED")
        return root, paper

    def test_a_copy_of_the_paper_elsewhere_is_blocked_by_bib_key_title_and_content(self):
        root, paper = self.unreconciled_first_publication()
        root2, paper2 = workspace(zenodo_id=None, doi=None)             # same zenodo.json content, other path
        blocked, code = self.blocked_in(root2, paper2, root)
        self.assertTrue(blocked, code)
        self.assertIn("matched by:", str(code))

    def blocked_in(self, root2, paper2, journal_root):
        f = Factory()
        code, _, _ = run_main([str(paper2), "--skip-checks", "--state-dir", str(state_dir_for(journal_root))],
                              PROD_ONLY, root2, f)
        return "REFUSING TO START" in str(code), code

    def test_same_bib_key_alone_blocks(self):
        root, paper = self.unreconciled_first_publication()
        root2, paper2 = workspace(zenodo_id=None, doi=None, web_url="https://other", code_url=URL + "2")
        (paper2 / "tex" / "P.tex").write_text(
            "\\documentclass{article}\\title{Different}\\begin{document}\\begin{abstract}A\\end{abstract}\\end{document}")
        blocked, code = self.blocked_in(root2, paper2, root)
        self.assertTrue(blocked, code)
        self.assertIn("same bib_key", str(code))

    def test_same_title_alone_blocks(self):
        root, paper = self.unreconciled_first_publication()
        root2, paper2 = workspace(zenodo_id=None, doi=None, bib_key="Other2026", web_url="https://other")
        blocked, code = self.blocked_in(root2, paper2, root)
        self.assertTrue(blocked, code)
        self.assertIn("same title", str(code))

    def test_an_unrelated_paper_is_not_blocked(self):
        root, paper = self.unreconciled_first_publication()
        root2, paper2 = workspace(zenodo_id=None, doi=None, bib_key="Other2026", web_url="https://other")
        (paper2 / "tex" / "P.tex").write_text(
            "\\documentclass{article}\\title{Different}\\begin{document}\\begin{abstract}A\\end{abstract}\\end{document}")
        blocked, code = self.blocked_in(root2, paper2, root)
        self.assertFalse(blocked, code)

    def test_probe_pe5_the_same_directory_through_a_symlink_is_blocked(self):
        root, paper = self.unreconciled_first_publication()
        link = root / "alias"
        os.symlink(paper, link)
        blocked, code = self.blocked_in(root, link, root)
        self.assertTrue(blocked, code)
        self.assertIn("same paper directory", str(code))

    def test_Y29_the_concept_block_is_host_aware(self):
        d = Path(tempfile.mkdtemp())
        elsewhere = Path(tempfile.mkdtemp()) / "other-paper"
        j = zp.Journal.create(d, elsewhere, "sandbox.zenodo.org", True, {}, concept_id=7)
        j.update(outcome="UNRECONCILED")
        here = Path(tempfile.mkdtemp()) / "paper"
        none = zp.find_blocking_journals(d, here, [7], "zenodo.org", {})
        self.assertEqual(none, [])
        some = zp.find_blocking_journals(d, here, [7], "sandbox.zenodo.org", {})
        self.assertEqual(len(some), 1)
        self.assertIn("same concept", some[0][1]["matched_by"])

    def test_K2_a_journal_written_under_a_symlinked_path_matches_the_real_directory(self):
        d = Path(tempfile.mkdtemp())
        real_dir = Path(tempfile.mkdtemp()).resolve() / "paper"
        real_dir.mkdir()
        link = Path(tempfile.mkdtemp()).resolve() / "alias"
        os.symlink(real_dir, link)
        j = zp.Journal.create(d, link, "zenodo.org", False, {})          # recorded through the symlink
        j.update(outcome="UNRECONCILED")
        found = zp.find_blocking_journals(d, real_dir, [], "zenodo.org", {})
        self.assertEqual(len(found), 1)
        self.assertIn("same paper directory", found[0][1]["matched_by"])
        j.data.pop("paper_dir_real")                                      # an older journal without the field
        j.update()
        self.assertEqual(len(zp.find_blocking_journals(d, real_dir, [], "zenodo.org", {})), 1)

    def test_identity_matching_is_host_aware_too(self):
        d = Path(tempfile.mkdtemp())
        ident = {"bib_key": "K", "title": "T", "config_sha256": "abc"}
        j = zp.Journal.create(d, Path("/a/p"), "sandbox.zenodo.org", True, {}, identity=ident)
        j.update(outcome="UNRECONCILED")
        self.assertEqual(zp.find_blocking_journals(d, Path("/b/p"), [], "zenodo.org", ident), [])
        self.assertEqual(len(zp.find_blocking_journals(d, Path("/b/p"), [], "sandbox.zenodo.org", ident)), 1)


# ── Reconcile (5): another paper is never modified; evidence; corrupt journals ───────────────────

class ReconcileSafety(unittest.TestCase):
    def unreconciled(self, **cfg):
        root, paper = workspace(zenodo_id=5, doi="10.0/c", **cfg)
        sim = Sim(publish="timeout_pending")
        client, _ = real_client(sim)
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        (jp,) = journal_paths(root)
        return root, paper, jp

    def test_probe_pe2_a_journal_cannot_write_another_papers_zenodo_json(self):
        root, paper, jp = self.unreconciled()
        other_root, other = workspace(zenodo_id=7, doi="10.0/other")
        before = (other / "zenodo.json").read_text()
        code, _, _ = run_main([str(other), "--reconcile", str(jp), "--outcome", "published", "--record-id", "1",
                               "--concept-id", "999", "--concept-doi", "10.9/zzz"], {}, root)
        self.assertIn("belongs to the paper directory", str(code))
        self.assertEqual((other / "zenodo.json").read_text(), before)
        self.assertEqual(json.loads(jp.read_text())["outcome"], "UNRECONCILED")

    def test_the_paper_directory_given_must_be_the_journals_even_through_a_symlink(self):
        root, paper, jp = self.unreconciled()
        link = root / "alias"
        os.symlink(paper, link)
        code, _, _ = run_main([str(link), "--reconcile", str(jp), "--outcome", "published", "--record-id", "301",
                               "--concept-id", "5", "--concept-doi", "10.5281/zenodo.0"], {}, root)
        self.assertIsNone(code, code)

    def test_a_zenodo_json_of_another_concept_or_bib_key_is_never_overwritten(self):
        root, paper, jp = self.unreconciled()
        cfg = json.loads((paper / "zenodo.json").read_text())
        before = (paper / "zenodo.json").read_text()
        code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "published", "--record-id", "1",
                               "--concept-id", "999", "--concept-doi", "10.9/zzz"], {}, root)
        self.assertIn("another concept", str(code))
        cfg["bib_key"] = "SomethingElse"
        (paper / "zenodo.json").write_text(json.dumps(cfg))
        before = (paper / "zenodo.json").read_text()
        code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "published", "--record-id", "1",
                               "--concept-id", "5", "--concept-doi", "10.9/zzz"], {}, root)
        self.assertIn("not the same paper", str(code))
        self.assertEqual((paper / "zenodo.json").read_text(), before)

    def test_probe_pe1_not_published_needs_evidence_and_prints_the_consequences(self):
        root, paper, jp = self.unreconciled()
        for extra in ([], ["--i-checked-the-account"], ["--reason", "looked"],
                      ["--record-url", "https://evil.example/record/1"]):
            code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "not-published", *extra], {}, root)
            self.assertIsNotNone(code, extra)
            self.assertEqual(json.loads(jp.read_text())["outcome"], "UNRECONCILED", extra)
        code, out, _ = run_main(["--reconcile", str(jp), "--outcome", "not-published",
                                 "--record-url", "https://zenodo.org/deposit/301"], {}, root)
        self.assertIsNone(code, code)
        self.assertIn("SECOND time", out)
        self.assertIn("MAY CREATE A NEW DEPOSITION", out)
        data = json.loads(jp.read_text())
        self.assertEqual(data["outcome"], "NOT-PUBLISHED-RECONCILED")
        self.assertEqual(data["operator_evidence"], {"record_url": "https://zenodo.org/deposit/301"})

    def test_not_published_with_the_account_check_stores_the_reason(self):
        root, paper, jp = self.unreconciled()
        code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "not-published", "--i-checked-the-account",
                               "--reason", "no draft, record 5 unchanged"], {}, root)
        self.assertIsNone(code, code)
        ev = json.loads(jp.read_text())["operator_evidence"]
        self.assertEqual((ev["checked_account"], ev["reason"]), (True, "no draft, record 5 unchanged"))

    def test_a_journal_that_says_the_publication_succeeded_cannot_be_reconciled_as_not_published(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(NoConcept()))
        (jp,) = journal_paths(root)
        code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "not-published", "--record-url",
                               "https://zenodo.org/record/1"], {}, root)
        self.assertIn("SUCCEEDED", str(code))
        self.assertEqual(json.loads(jp.read_text())["outcome"], "PUBLISHED-UNSAVED")

    def test_probe_pe3_an_unreadable_journal_has_an_explicit_non_deleting_path(self):
        root, paper = workspace()
        bad = state_dir_for(root) / "paper-20260101T000000Z-1-abc123.run.json"
        bad.write_text("{trunc")
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertIn("--quarantine", str(code))
        code, _, _ = run_main(["--reconcile", str(bad), "--outcome", "not-published", "--record-url",
                               "https://zenodo.org/x"], {}, root)
        self.assertIn("--quarantine", str(code))
        code, _, _ = run_main(["--reconcile", str(bad), "--quarantine"], {}, root)
        self.assertIn("--reason", str(code))
        self.assertTrue(bad.exists())
        code, out, _ = run_main(["--reconcile", str(bad), "--quarantine", "--reason", "checked /deposit"], {}, root)
        self.assertIsNone(code, code)
        self.assertIn("6 bytes", out)
        self.assertIn(str(bad), out)
        moved = Path(str(bad) + ".quarantined")
        self.assertFalse(bad.exists())
        self.assertEqual(moved.read_text(), "{trunc")                      # renamed, content untouched
        self.assertTrue(Path(str(moved) + ".note.txt").exists())
        self.assertEqual(moved.parent, bad.parent)
        code, _, _ = run_main([str(paper), "--skip-checks"], ENV, root, Factory())
        self.assertNotIn("REFUSING", str(code))

    def test_quarantine_never_overwrites_and_refuses_a_readable_journal(self):
        root, paper, jp = self.unreconciled()
        code, _, _ = run_main(["--reconcile", str(jp), "--quarantine", "--reason", "x"], {}, root)
        self.assertIn("readable journal", str(code))
        self.assertTrue(jp.exists())
        bad = state_dir_for(root) / "paper-1.run.json"
        bad.write_text("[1]")
        taken = Path(str(bad) + ".quarantined")
        taken.write_text("earlier")
        run_main(["--reconcile", str(bad), "--quarantine", "--reason", "x"], {}, root)
        self.assertEqual(taken.read_text(), "earlier")
        self.assertEqual(Path(str(bad) + ".quarantined.1").read_text(), "[1]")

    def test_the_config_written_by_a_reconcile_is_read_back(self):
        root, paper, jp = self.unreconciled()
        real = zp._atomic_write_text

        def lying(path, text):
            if Path(path).name == "zenodo.json":
                text = text.replace('"zenodo_id": 5', '"zenodo_id": 6')
            return real(path, text)
        with mock.patch.object(zp, "_atomic_write_text", lying):
            code, _, _ = run_main(["--reconcile", str(jp), "--outcome", "published", "--record-id", "1",
                                   "--concept-id", "5", "--concept-doi", "10.9/zzz"], {}, root)
        self.assertIn("does not hold the identifiers", str(code))
        self.assertEqual(json.loads(jp.read_text())["outcome"], "UNRECONCILED")

    def test_reconcile_flags_without_reconcile_are_refused(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--skip-checks", "--quarantine"], ENV, root, Factory())
        self.assertEqual(code, 2)


# ── Secrets never reach a journal (6, F7, probe pf) ──────────────────────────────────────────────

class SecretsNeverPersisted(unittest.TestCase):
    TOKEN = "SECRETTOKEN123XYZ"

    def setUp(self):
        zp.register_secret(self.TOKEN)

    def test_probe_pf_every_field_written_to_a_journal_is_redacted(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {"note": f"intent {self.TOKEN}"}, "1.0")
        evil = f"cannot read record 5 (HTTPSConnectionPool Authorization: Bearer {self.TOKEN})"
        j.update(note=evil, phase=f"phase {self.TOKEN}", manual_url=f"https://zenodo.org/x?access_token={self.TOKEN}",
                 candidates=[f"c-{self.TOKEN}"], extra={"nested": [f"Bearer {self.TOKEN}"]})
        j.write_intent("POST", f"/api/x?access_token={self.TOKEN}")
        raw = j.path.read_bytes()
        self.assertNotIn(self.TOKEN.encode(), raw)
        self.assertNotIn(b"access_token=" + self.TOKEN.encode(), raw)
        self.assertIn(b"[REDACTED]", raw)

    def test_a_token_with_json_special_characters_is_masked_in_the_file_bytes(self):
        tricky = 'tok"en\\with"quotes12345'
        zp.register_secret(tricky)
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        j.update(note=f"boom {tricky}")
        self.assertNotIn("with", j.path.read_text())

    def test_pattern_based_masking(self):
        for text in ("Authorization: Bearer abc.def", "token=abcdef123456&x=1", "?access_token=zzz999&y",
                     "Authorization: Token abcdef"):
            out = zp.redact(text)
            self.assertNotIn("abc", out.replace("[REDACTED]", ""), text)
            self.assertIn("[REDACTED]", out, text)
        self.assertEqual(zp.redact_obj({"a": [f"x {self.TOKEN}", 3]}), {"a": ["x [REDACTED]", 3]})

    def test_locate_own_state_never_returns_a_token(self):
        class C:
            pre_run_latest_state = "done"
            pre_run_draft_ids = set()
            concept_id = None

            def get_deposition(self, i):
                raise RuntimeError(f"boom {self.TOKEN_}")

            def list_drafts(self):
                raise RuntimeError(f"boom {self.TOKEN_}")
        C.TOKEN_ = self.TOKEN
        for state in ({"kind": "edit session", "dep_id": 5}, {"kind": "new-version draft", "dep_id": None}):
            verdict, _, _, why = zp.locate_own_state(C(), state)
            self.assertEqual(verdict, "unknown")
            self.assertNotIn(self.TOKEN, why)

    def test_both_environment_tokens_are_masked_in_a_whole_run_and_its_journal(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        leak = RuntimeError(f"the other environment's token {SBX_TOKEN} and ours {PROD_TOKEN} in a message")
        client = FakeClient(fail_at="newversion", fail_exc=leak)
        with mock.patch.object(zp, "_SECRETS", set()):          # nothing registered by an earlier test
            code, out, err = run_main([str(paper), "--skip-checks"], ENV, root, Factory(client))
        for stream in (str(code), out, err):
            self.assertNotIn(SBX_TOKEN, stream)
            self.assertNotIn(PROD_TOKEN, stream)
        for p in state_dir_for(root).iterdir():
            raw = p.read_bytes()
            self.assertNotIn(SBX_TOKEN.encode(), raw)
            self.assertNotIn(PROD_TOKEN.encode(), raw)

    def test_the_first_publication_snapshot_failure_message_is_redacted(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        client = FakeClient(fail_at="list_drafts", fail_exc=RuntimeError(f"Bearer {PROD_TOKEN} refused"))
        code, out, err = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        self.assertNotIn(PROD_TOKEN, str(code) + out + err)

    def test_a_reconciled_journal_does_not_store_a_token_given_in_the_reason(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        client, _ = real_client(Sim(publish="timeout_pending"))
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        (jp,) = journal_paths(root)
        run_main(["--reconcile", str(jp), "--outcome", "not-published", "--i-checked-the-account",
                  "--reason", f"checked with Bearer {self.TOKEN}"], {}, root)
        self.assertNotIn(self.TOKEN.encode(), jp.read_bytes())


# ── Archive (7): refusal => no cascade; partial files; single read; F8/F9/F11 ────────────────────

class ArchiveRobustness(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.pdf = self.root / "src.pdf"
        self.pdf.write_bytes(b"%PDF-good")
        (self.root / "pdfs").mkdir()
        self.dest = self.root / "pdfs" / "P_1.0.pdf"
        self.sha = sha(b"%PDF-good")

    def archive(self, **k):
        return zp.archive_pdf(self.root, self.pdf, "P", "1.0", 5, expected_sha256=self.sha, **k)

    def test_Y2_an_existing_archive_of_the_same_size_but_other_content_is_refused(self):
        self.dest.write_bytes(b"%PDF-GOOD")                       # same length, different bytes
        self.assertEqual(len(b"%PDF-GOOD"), len(b"%PDF-good"))
        with self.assertRaises(zp.ArchiveRefused):
            self.archive()
        self.assertEqual(self.dest.read_bytes(), b"%PDF-GOOD")

    def test_results_are_typed(self):
        res = self.archive()
        self.assertEqual((res.path, res.status), (self.dest, "archived"))
        res = self.archive()
        self.assertEqual((res.path, res.status), (self.dest, "identical"))

    def test_probe_pc_partial_write_never_leaves_a_truncated_file_under_the_final_name(self):
        with mock.patch.object(zp, "_fsync_file", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(zp.ArchiveRefused) as cm:
                self.archive()
        self.assertFalse(self.dest.exists())
        strays = zp.stray_archive_files(self.root / "pdfs", self.dest.name)
        self.assertEqual(len(strays), 1)
        self.assertIn(str(strays[0][0]), str(cm.exception))            # the message names the leftover
        self.assertIn("NOT deleted", str(cm.exception))
        # a later run reports the stray (path, size), never deletes it, and still archives
        code, out, err = run(self.archive)
        self.assertIsNone(code, code)
        self.assertIn(str(strays[0][0]), err)
        self.assertTrue(strays[0][0].exists())
        self.assertEqual(self.dest.read_bytes(), b"%PDF-good")

    def test_a_copy_corrupted_while_writing_is_caught_before_it_gets_its_final_name(self):
        real = zp._fsync_file

        def corrupt(fh):
            real(fh)
            with open(fh.name, "r+b") as other:
                other.write(b"X")
        with mock.patch.object(zp, "_fsync_file", corrupt):
            with self.assertRaises(zp.ArchiveRefused):
                self.archive()
        self.assertFalse(self.dest.exists())

    def test_Y32_a_copy_that_differs_after_linking_is_reported(self):
        real_link = os.link

        def corrupting_link(src, dst, *a, **k):
            real_link(src, dst, *a, **k)
            with open(dst, "r+b") as fh:
                fh.write(b"X")
        with mock.patch.object(zp.os, "link", corrupting_link):
            with self.assertRaises(zp.ArchiveRefused) as cm:
                self.archive()
        self.assertIn("after linking", str(cm.exception))

    def test_probe_pc_symlinks_and_directories_at_the_destination_are_refused_with_a_message(self):
        os.symlink(self.root / "elsewhere.pdf", self.dest)                   # dangling
        with self.assertRaises(zp.ArchiveRefused) as cm:
            self.archive()
        self.assertIn("symbolic link", str(cm.exception))
        self.assertFalse((self.root / "elsewhere.pdf").exists())
        os.unlink(self.dest)
        (self.root / "same.pdf").write_bytes(b"%PDF-good")                   # symlink to an IDENTICAL file
        os.symlink(self.root / "same.pdf", self.dest)
        with self.assertRaises(zp.ArchiveRefused):
            self.archive()
        os.unlink(self.dest)
        self.dest.mkdir()
        with self.assertRaises(zp.ArchiveRefused) as cm:
            self.archive()
        self.assertIn("directory", str(cm.exception))

    def test_pdfs_that_is_a_file_or_a_symlink_is_refused(self):
        other = Path(tempfile.mkdtemp()).resolve()
        (other / "pdfs").write_bytes(b"a file")
        with self.assertRaises(zp.ArchiveRefused):
            zp.archive_pdf(other, self.pdf, "P", "1.0", 5)
        third = Path(tempfile.mkdtemp()).resolve()
        os.symlink(other, third / "pdfs")
        with self.assertRaises(zp.ArchiveRefused):
            zp.archive_pdf(third, self.pdf, "P", "1.0", 5)

    def test_a_name_that_appears_while_the_copy_is_written_is_judged_by_digest_and_never_replaced(self):
        real = zp.file_digests
        state = {"first": True}

        def racing(path):
            if state["first"] and Path(path).name.endswith(".tmp"):     # the verification of our temp copy
                state["first"] = False
                self.dest.write_bytes(RACE[0])                          # someone creates the name right now
            return real(path)
        RACE = [b"%PDF-good"]
        with mock.patch.object(zp, "file_digests", racing):
            res = self.archive()
        self.assertEqual(res.status, "identical")
        self.dest.unlink()
        RACE[0], state["first"] = b"%PDF-other", True
        with mock.patch.object(zp, "file_digests", racing):
            with self.assertRaises(zp.ArchiveRefused):
                self.archive()
        self.assertEqual(self.dest.read_bytes(), b"%PDF-other")             # not replaced

    def test_the_directory_is_fsynced_after_the_link(self):
        synced = []
        with mock.patch.object(zp, "_fsync_dir", side_effect=synced.append):
            self.archive()
        self.assertIn(self.root / "pdfs", synced)

    def test_the_bytes_given_are_the_bytes_written_and_the_file_is_not_re_read(self):
        reads = []
        real = Path.read_bytes

        def spy(self_):
            reads.append(self_.name)
            return real(self_)
        with mock.patch.object(Path, "read_bytes", spy):
            self.archive(data=b"%PDF-good")
        self.assertNotIn("src.pdf", reads)
        self.assertEqual(self.dest.read_bytes(), b"%PDF-good")

    def test_fsync_dir_tolerates_unsupported_filesystems_only(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EINVAL, "no")):
            zp._fsync_dir(d)
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                zp._fsync_dir(d)

    def test_exclusive_creation_never_overwrites_and_leaves_no_temp(self):
        d = Path(tempfile.mkdtemp())
        target = d / "j.run.json"
        target.write_text("EXISTING")
        with self.assertRaises(FileExistsError):
            zp._create_exclusive_text(target, "new")
        self.assertEqual(target.read_text(), "EXISTING")
        self.assertEqual([p.name for p in d.iterdir()], ["j.run.json"])

    def test_a_journal_is_created_through_the_exclusive_primitive(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(zp, "_create_exclusive_text", wraps=zp._create_exclusive_text) as spy:
            j = zp.Journal.create(d, Path("/x/p"), "zenodo.org", False, {})
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args[0][0], j.path)

    def test_the_journal_directory_and_file_are_fsynced(self):
        synced = []
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(zp, "_fsync_dir", side_effect=synced.append):
            j = zp.Journal.create(d, Path("/x/p"), "zenodo.org", False, {})
            j.update(phase="x")
        self.assertEqual(synced, [d, d])


class ArchiveInTheFlow(unittest.TestCase):
    """Y38 and the archive/cascade coupling, through main."""

    def run_flow(self, prepare=None, cascade_calls=None):
        root, paper = workspace()
        if prepare:
            prepare(root, paper)
        calls = [] if cascade_calls is None else cascade_calls
        code, out, err = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(),
                                  extra=[mock.patch.object(zp, "doi_cascade", lambda *a, **k: calls.append(a))])
        return root, paper, calls, (code, out, err)

    def test_Y38_a_refused_archive_stops_before_the_cascade(self):
        def different(root, paper):
            (root / "pdfs" / "P_1.0.pdf").write_bytes(b"%PDF-SOMETHING-ELSE")
        root, paper, calls, (code, out, err) = self.run_flow(different)
        self.assertIn("REFUSED", str(code))
        self.assertIn("cascade was NOT run", str(code))
        self.assertEqual(calls, [])
        self.assertIn("old", (root / "bib" / "cosmochrony-bibliography.bib").read_text())   # bibliography untouched
        self.assertEqual((root / "pdfs" / "P_1.0.pdf").read_bytes(), b"%PDF-SOMETHING-ELSE")

    def test_an_archive_that_cannot_be_written_is_a_message_not_a_traceback_and_runs_no_cascade(self):
        def blocked_dir(root, paper):
            (root / "pdfs" / "P_1.0.pdf").mkdir()
        root, paper, calls, (code, out, err) = self.run_flow(blocked_dir)
        self.assertIsInstance(code, str)
        self.assertIn("directory", code)
        self.assertEqual(calls, [])
        self.assertEqual(journals(root)[0]["outcome"], "PUBLISHED")

    def test_an_identical_archive_lets_the_cascade_run(self):
        def identical(root, paper):
            (root / "pdfs" / "P_1.0.pdf").write_bytes(b"%PDF-fake")
        root, paper, calls, (code, out, err) = self.run_flow(identical)
        self.assertIsNone(code, code)
        self.assertEqual(len(calls), 1)
        self.assertIn("Already archived, identical", out)

    def test_a_normal_run_archives_then_cascades(self):
        root, paper, calls, (code, out, err) = self.run_flow()
        self.assertIsNone(code, code)
        self.assertEqual(len(calls), 1)
        self.assertEqual((root / "pdfs" / "P_1.0.pdf").read_bytes(), b"%PDF-fake")

    def test_the_pdf_is_read_once_and_the_archive_holds_the_uploaded_bytes_even_if_the_file_changes(self):
        class Changing(Sim):
            def _do(self, method, url, **kw):
                resp = super()._do(method, url, **kw)
                if method == "PUT" and "/api/files/" in url:
                    PDFFILE[0].write_bytes(b"%PDF-CHANGED-AFTER-UPLOAD")
                return resp
        PDFFILE = []
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        PDFFILE.append(paper / "out" / "P.pdf")
        sim = Changing()
        client, _ = real_client(sim)
        reads = []
        real = Path.read_bytes

        def spy(self_):
            reads.append(self_.name)
            return real(self_)
        with mock.patch.object(Path, "read_bytes", spy):
            code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        self.assertIsNone(code, code)
        self.assertEqual(reads.count("P.pdf"), 1)
        self.assertEqual((root / "pdfs" / "P_1.0.pdf").read_bytes(), b"%PDF-fake")        # the uploaded bytes
        sent = [kw["data"] for m, u, kw in sim.calls if m == "PUT" and "/api/files/" in u][0]
        self.assertEqual(sent, b"%PDF-fake")
        self.assertEqual(journals(root)[0]["pdf"]["sha256"], sha(sent))


class UploadDigestRules(unittest.TestCase):
    """F9: no recorded digest = no upload; the checksum in the upload answer is compared."""

    def test_an_unreadable_pdf_aborts_before_any_upload_and_cleans_the_draft(self):
        sim = Sim()
        client, _ = real_client(sim)
        code, out, err = run(fakes.new, client, pdf="/nonexistent/p.pdf")
        self.assertIn("cannot read the PDF", err)
        self.assertEqual([c for c in sim.calls if c[0] == "PUT" and "/api/files/" in c[1]], [])
        self.assertEqual(sim.published_ids, [])
        self.assertEqual(sim.discarded, [301])

    def test_fake_client_flow_is_also_refused_before_upload(self):
        c = FakeClient()
        run(fakes.new, c, pdf="/nonexistent/p.pdf")
        self.assertNotIn("upload", c.names())

    class Answering(Sim):
        def __init__(self, checksum, **modes):
            super().__init__(**modes)
            self.checksum = checksum

        def _do(self, method, url, **kw):
            resp = super()._do(method, url, **kw)
            if method == "PUT" and "/api/files/" in url:
                data = kw["data"]
                value = self.checksum(data) if callable(self.checksum) else self.checksum
                return Resp(201, {"key": "p.pdf", **({"checksum": value} if value is not None else {})})
            return resp

    def run_with(self, checksum):
        sim = self.Answering(checksum)
        client, _ = real_client(sim)
        code, out, err = run(fakes.new, client)
        return sim, code, err

    def test_a_checksum_that_differs_from_the_bytes_sent_aborts_and_cleans_up(self):
        sim, code, err = self.run_with("md5:" + "0" * 32)
        self.assertIn("not the one sent", err)
        self.assertEqual(sim.published_ids, [])
        self.assertEqual(sim.discarded, [301])

    def test_a_matching_checksum_in_each_accepted_form_passes(self):
        for form in (lambda d: "md5:" + hashlib.md5(d).hexdigest(), lambda d: hashlib.md5(d).hexdigest(),
                     lambda d: "sha256:" + hashlib.sha256(d).hexdigest(), lambda d: hashlib.sha256(d).hexdigest()):
            sim, code, err = self.run_with(form)
            self.assertIsNone(code, (code, err))
            self.assertEqual(len(sim.published_ids), 1)

    def test_no_checksum_in_the_answer_is_tolerated_and_unrecognised_forms_are_not_compared(self):
        sim, code, err = self.run_with(None)
        self.assertIsNone(code, code)
        self.assertIsNone(zp.verify_upload_answer({"checksum": "weird:xyz"}, {"md5": "a", "sha256": "b"}) and None)
        self.assertIsNone(zp.verify_upload_answer(None, {}))

    def test_the_matched_checksum_is_recorded_in_the_journal(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/p"), "zenodo.org", False, {})
        sim = self.Answering(lambda data: "md5:" + hashlib.md5(data).hexdigest())
        client, _ = real_client(sim)
        client.journal = j
        run(fakes.new, client)
        self.assertIn("matches the bytes sent", json.loads(j.path.read_text())["upload_checksum"])


# ── Startup bounds (F13) ─────────────────────────────────────────────────────────────────────────

class NumericBounds(unittest.TestCase):
    def test_upper_bounds_are_refused_at_startup(self):
        for name, ok, bad in (("ZENODO_GET_ATTEMPTS", "10", "11"), ("ZENODO_GET_ATTEMPTS", "3", "1000000"),
                              ("ZENODO_CONNECT_TIMEOUT", "300", "301"), ("ZENODO_READ_TIMEOUT", "3600", "3601"),
                              ("ZENODO_UPLOAD_TIMEOUT", "86400", "86401"), ("ZENODO_RETRY_BACKOFF", "60", "61")):
            zp.validate_numeric_env({name: ok})
            with self.assertRaises(SystemExit) as cm:
                zp.validate_numeric_env({name: bad})
            self.assertIn(name, str(cm.exception))

    def test_probe_pf_a_huge_attempt_count_stops_the_run_before_any_request(self):
        root, paper = workspace(zenodo_id=5)
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks"], {**PROD_ONLY, "ZENODO_GET_ATTEMPTS": "1000000"}, root, f)
        self.assertIn("ZENODO_GET_ATTEMPTS", str(code))
        self.assertEqual(f.built, [])

    def test_the_client_refuses_it_too(self):
        with self.assertRaises(SystemExit):
            real_client(environ={"ZENODO_GET_ATTEMPTS": "1000000", "ZENODO_RETRY_BACKOFF": "0"})


# ── Sandbox state: exclusive create (F12) ────────────────────────────────────────────────────────

class SandboxSlotExclusive(unittest.TestCase):
    def test_a_file_that_appears_between_reserve_and_commit_is_never_overwritten(self):
        path = Path(tempfile.mkdtemp()) / "s.json"
        slot = zp.SandboxStateSlot(path)
        slot.reserve()
        path.write_text("EXISTING")
        with self.assertRaises(FileExistsError):
            slot.commit("new")
        self.assertEqual(path.read_text(), "EXISTING")

    def test_commit_uses_an_exclusive_link(self):
        path = Path(tempfile.mkdtemp()) / "s.json"
        slot = zp.SandboxStateSlot(path)
        slot.reserve()
        with mock.patch.object(zp.os, "replace", side_effect=AssertionError("os.replace is not exclusive")):
            slot.commit("new")
        self.assertEqual(path.read_text(), "new")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["s.json"])


# ── Finish bound to one deposit journal (4): F4, F5, Z4, Y22, Y23 ───────────────────────────────

class FinishBoundToTheJournal(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN}

    def published_workspace(self, **cfg):
        root, paper = workspace(**cfg)
        client = FakeClient(published_custom="https://wrong")          # post-publish FAIL: published, unfinished
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(client))
        self.assertIn("--finish-after-fail", code)
        (jp,) = journal_paths(root)
        return root, paper, jp

    def finish(self, root, paper, *extra):
        calls = []
        res = run_main([str(paper), "--finish-after-fail", *extra], {}, root, Factory(),
                       extra=[mock.patch.object(zp, "doi_cascade", lambda *a, **k: calls.append(a))])
        return res, calls

    def clone_journal(self, root, jp, name, **fields):
        data = json.loads(jp.read_text())
        data.update(fields)
        path = state_dir_for(root) / name
        path.write_text(json.dumps(data))
        return path

    def test_probe_pd1_a_version_bumped_after_the_deposit_cannot_relabel_the_archive(self):
        root, paper, jp = self.published_workspace()
        cfg = json.loads((paper / "zenodo.json").read_text())
        cfg["version"] = "2.0"
        (paper / "zenodo.json").write_text(json.dumps(cfg))
        (code, _, _), calls = self.finish(root, paper)
        self.assertIn("version label refused", str(code))
        self.assertIn("DEPOSITED version", str(code))
        self.assertEqual(os.listdir(root / "pdfs"), [])
        self.assertEqual(calls, [])

    def test_probe_pd3_the_label_is_the_journals_version_even_without_a_version_in_the_config(self):
        root, paper, jp = self.published_workspace()
        cfg = json.loads((paper / "zenodo.json").read_text())
        cfg.pop("version")
        (paper / "zenodo.json").write_text(json.dumps(cfg))
        (code, _, _), calls = self.finish(root, paper)
        self.assertIsNone(code, code)
        self.assertEqual(os.listdir(root / "pdfs"), ["P_1.0.pdf"])
        self.assertEqual(len(calls), 1)

    def test_probe_pd2_an_update_journal_does_not_shadow_the_deposit_journal(self):
        root, paper, jp = self.published_workspace()
        write_journal(state_dir_for(root), "paper-upd.run.json", paper_dir=str(paper), kind="update",
                      outcome="PUBLISHED", concept_id="5", started_at="2026-12-01T00:00:00+00:00",
                      intent={"update": True})
        (code, _, _), calls = self.finish(root, paper)
        self.assertIsNone(code, code)
        self.assertEqual(os.listdir(root / "pdfs"), ["P_1.0.pdf"])

    def test_Z4_the_newest_deposit_journal_is_used_not_the_oldest(self):
        root, paper, jp = self.published_workspace()
        data = json.loads(jp.read_text())
        self.clone_journal(root, jp, "paper-old.run.json", started_at="2020-01-01T00:00:00+00:00",
                           version="0.9", pdf={"name": "P.pdf", "sha256": "d" * 64})
        newest = zp.select_deposit_journal(state_dir_for(root), paper)
        self.assertEqual(newest[0], jp)
        (code, _, _), _ = self.finish(root, paper)
        self.assertIsNone(code, code)
        self.assertEqual(os.listdir(root / "pdfs"), ["P_1.0.pdf"])
        self.assertEqual(data["version"], "1.0")

    def test_Y22_a_sandbox_journal_never_supplies_the_digest(self):
        root, paper, jp = self.published_workspace()
        sbx = self.clone_journal(root, jp, "paper-sbx.run.json", sandbox=True, host="sandbox.zenodo.org",
                                 started_at="2030-01-01T00:00:00+00:00", pdf={"name": "P.pdf", "sha256": "e" * 64})
        (code, _, _), _ = self.finish(root, paper)
        self.assertIsNone(code, code)
        (code, _, _), _ = self.finish(root, paper, "--journal", str(sbx))
        self.assertIn("SANDBOX", str(code))

    def test_Y23_a_journal_of_another_concept_is_refused(self):
        root, paper, jp = self.published_workspace()
        data = json.loads(jp.read_text())
        data["concept_id"] = "999"
        jp.write_text(json.dumps(data))
        (code, _, _), calls = self.finish(root, paper)
        self.assertIn("records concept 999", str(code))
        self.assertEqual(os.listdir(root / "pdfs"), [])
        self.assertEqual(calls, [])

    def test_an_explicit_journal_is_accepted_and_held_to_the_same_rules(self):
        root, paper, jp = self.published_workspace()
        upd = write_journal(state_dir_for(root), "paper-upd.run.json", paper_dir=str(paper), kind="update",
                            outcome="PUBLISHED", pdf={"sha256": sha(b"%PDF-fake")}, intent={"update": True})
        (code, _, _), _ = self.finish(root, paper, "--journal", str(upd))
        self.assertIn("not a published production deposit", str(code))
        (code, _, _), _ = self.finish(root, paper, "--journal", str(jp))
        self.assertIsNone(code, code)

    def test_a_journal_still_unsaved_is_never_finished(self):
        root, paper, jp = self.published_workspace()
        data = json.loads(jp.read_text())
        data["outcome"] = "PUBLISHED-UNSAVED"
        jp.write_text(json.dumps(data))
        (code, _, _), _ = self.finish(root, paper)
        self.assertIn("PUBLISHED-UNSAVED", str(code))
        self.assertEqual(os.listdir(root / "pdfs"), [])

    def test_a_journal_version_that_is_a_branch_name_is_refused(self):
        root, paper, jp = self.published_workspace()
        data = json.loads(jp.read_text())
        data["version"] = "main"
        jp.write_text(json.dumps(data))
        (code, _, _), _ = self.finish(root, paper)
        self.assertIn("version label refused", str(code))

    def test_without_a_journal_the_operator_digest_is_used_with_the_current_labelled_version(self):
        root, paper, jp = self.published_workspace()
        data = json.loads(jp.read_text())
        data["pdf"] = None
        jp.write_text(json.dumps(data))
        (code, _, _), _ = self.finish(root, paper)
        self.assertIn("no digest of the uploaded PDF", str(code))
        (code, _, _), calls = self.finish(root, paper, "--expect-pdf-sha256", sha(b"%PDF-fake"))
        self.assertIsNone(code, code)

    def test_the_cascade_runs_after_an_identical_archive_and_never_after_a_refusal(self):
        root, paper, jp = self.published_workspace()
        (root / "pdfs" / "P_1.0.pdf").write_bytes(b"%PDF-fake")
        (code, out, _), calls = self.finish(root, paper)
        self.assertIsNone(code, code)
        self.assertEqual(len(calls), 1)
        self.assertIn("identical", out)
        (root / "pdfs" / "P_1.0.pdf").write_bytes(b"%PDF-other")
        (code, _, _), calls = self.finish(root, paper)
        self.assertIn("refused", str(code))
        self.assertIn("cascade was NOT run", str(code))
        self.assertEqual(calls, [])

    def test_a_newer_deposit_that_published_nothing_or_has_no_digest_does_not_shadow(self):
        root, paper, jp = self.published_workspace()
        write_journal(state_dir_for(root), "paper-failed.run.json", paper_dir=str(paper), kind="deposit",
                      outcome="NOT-PUBLISHED", pdf={"name": "P.pdf", "sha256": "f" * 64}, version="9.9",
                      started_at="2031-01-01T00:00:00+00:00")
        write_journal(state_dir_for(root), "paper-nodigest.run.json", paper_dir=str(paper), kind="deposit",
                      outcome="PUBLISHED", pdf=None, version="9.8", started_at="2032-01-01T00:00:00+00:00")
        self.assertEqual(zp.select_deposit_journal(state_dir_for(root), paper)[0], jp)
        (code, _, _), _ = self.finish(root, paper)
        self.assertIsNone(code, code)
        self.assertEqual(os.listdir(root / "pdfs"), ["P_1.0.pdf"])

    def test_journal_flag_only_with_finish(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--skip-checks", "--journal", "x"], ENV, root, Factory())
        self.assertEqual(code, 2)


# ── --update: related identifiers kept (F2) and the reviewed-digest pin (F3) ─────────────────────

LIVE = {"title": "Old", "description": "D", "version": "0.9", "keywords": ["a"], "creators": [{"name": "X"}],
        "license": "cc-by-4.0", "upload_type": "publication", "publication_type": "workingpaper", "language": "eng",
        "related_identifiers": [
            {"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"},
            {"identifier": "https://old.example", "relation": "isSupplementedBy", "scheme": "url",
             "resource_type": "other"},
            {"identifier": "https://github.com/x/y", "relation": "isSupplementedBy", "scheme": "url",
             "resource_type": "software"},
            {"identifier": "10.2/zzz", "relation": "isPartOf", "scheme": "doi", "resource_type": "publication"}],
        "custom": {"code:programmingLanguage": ["py"], F: "https://old/code"},
        "contributors": [{"name": "C", "type": "Editor"}], "grants": [{"id": "g"}],
        "communities": [{"identifier": "cosmo"}], "notes": "live note"}


class LiveClient(FakeClient):
    """A FakeClient whose published record carries the rich LIVE metadata above."""

    def __init__(self, live=None, **k):
        self.live = copy.deepcopy(live if live is not None else LIVE)
        k.setdefault("published_custom", (self.live.get("custom") or {}).get(F))
        super().__init__(**k)

    def get_deposition(self, i):
        if i == 10:
            self._c("get_deposition", i)
            meta = self.meta_put if self.meta_put is not None else self.live      # after the PUT: what was sent
            d = {"id": 10, "state": "done", "conceptrecid": "5", "links": {}, "metadata": copy.deepcopy(meta)}
            if self.created_state is not None and self.created_state.get("kind") == "edit session":
                d["state"] = "inprogress"
            return d
        return super().get_deposition(i)


def update_cli(client, *extra, cfg=None, raw_confirm=False):
    root, paper = workspace(zenodo_id=5, doi="10.0/c", **(cfg or {}))
    res = run_main([str(paper), "--skip-checks", "--update", *extra], PROD_ONLY, root, Factory(client),
                   raw_confirm=raw_confirm)
    return root, paper, res


class UpdateKeepsEverythingElse(unittest.TestCase):
    def test_F2_set_web_url_replaces_only_the_web_entry_and_keeps_cites_and_the_rest_field_by_field(self):
        c = LiveClient()
        root, paper, (code, out, err) = update_cli(c, "--set-web-url", "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIsNone(code, code)
        sent = c.meta_put
        for key in set(sent) | set(LIVE):
            if key != "related_identifiers":
                self.assertEqual(sent.get(key), LIVE.get(key), key)
        expect = copy.deepcopy(LIVE["related_identifiers"])
        expect[1] = {"identifier": "https://new.example", "relation": "isSupplementedBy", "scheme": "url",
                     "resource_type": "other"}
        self.assertEqual(sent["related_identifiers"], expect)
        self.assertIn({"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"}, sent["related_identifiers"])

    def test_probe_pa_every_field_changes_only_its_own_key(self):
        built = zp.build_metadata("NewT", "NewA", ["k"], "https://new.example", "1.1", publication_type="preprint",
                                  notes="n2", code_url="https://new/code")
        for field in sorted(zp.UPDATABLE_FIELDS):
            payload = zp.build_update_payload(LIVE, built, {field}, "https://new/code")
            changed = {k for k in set(payload) | set(LIVE) if payload.get(k) != LIVE.get(k)}
            self.assertEqual(changed, {zp.UPDATABLE_FIELDS[field]}, field)
        self.assertEqual(LIVE["related_identifiers"][0]["relation"], "cites")      # the live record is not mutated

    def test_web_url_is_inserted_when_no_entry_exists_and_nothing_else_moves(self):
        live = [{"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"}]
        entry = {"identifier": "https://w", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"}
        self.assertEqual(zp.merge_related_web_url(live, entry), live + [entry])
        self.assertEqual(zp.merge_related_web_url(None, entry), [entry])
        self.assertEqual(zp.merge_related_web_url([], entry), [entry])

    def test_an_entry_with_the_same_identifier_is_replaced_in_place(self):
        entry = {"identifier": "https://w", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"}
        live = [{"identifier": "https://w", "relation": "isSupplementedBy", "scheme": "url"},
                {"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"},
                {"identifier": "https://w2", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"}]
        out = zp.merge_related_web_url(live, entry)
        self.assertEqual(out[0], entry)
        self.assertEqual(out[1:], live[1:])

    def test_an_ambiguous_web_entry_is_refused_not_guessed(self):
        entry = {"identifier": "https://new", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"}
        live = [{"identifier": "https://a", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"},
                {"identifier": "https://b", "relation": "isSupplementedBy", "scheme": "url"}]
        with self.assertRaises(ValueError):
            zp.merge_related_web_url(live, entry)
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, out, err) = update_cli(c, "--set-web-url", "--yes-apply-diff", cfg={"web_url": "https://new"})
        self.assertIn("cannot tell which one is the web url", str(code))
        self.assertNotIn("edit", c.names())

    def test_set_web_url_without_a_local_web_url_is_refused(self):
        c = LiveClient()
        root, paper, (code, _, _) = update_cli(c, "--set-web-url", "--yes-apply-diff", cfg={"web_url": ""})
        self.assertIn("no web_url", str(code))
        self.assertNotIn("edit", c.names())

    def test_the_put_payload_is_exactly_the_object_that_was_diffed(self):
        c = LiveClient(published_custom=URL)
        root, paper, (code, out, err) = update_cli(c, "--set-title", "--set-web-url", "--set-code-url",
                                                   "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIsNone(code, code)
        built = zp.build_metadata("T", "A", [], "https://new.example", "1.0", code_url=URL)
        payload = zp.build_update_payload(LIVE, built, {"title", "web-url", "code-url"}, URL)
        self.assertEqual(c.meta_put, payload)
        diff, digest = zp.metadata_diff(LIVE, payload)
        self.assertIn(digest, out)


class ReviewedDiffPin(unittest.TestCase):
    def review(self, client=None):
        client = client or LiveClient()
        root, paper, (code, out, err) = update_cli(client, "--set-title")
        digest = re.search(r"diff sha256: ([0-9a-f]{64})", out).group(1)
        return out, digest

    def test_the_digest_is_the_sha256_of_exactly_the_printed_diff_text(self):
        out, digest = self.review()
        lines = out.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("--- live"))
        end = next(i for i, ln in enumerate(lines) if ln.strip().startswith("diff sha256:"))
        printed = "\n".join(lines[start:end])
        self.assertEqual(sha(printed.encode("utf-8")), digest)

    def test_B5_without_the_flag_the_run_stops_with_the_instruction_to_pin_this_diff(self):
        c = LiveClient()
        root, paper, (code, out, _) = update_cli(c, "--set-title", raw_confirm=True)
        digest = re.search(r"diff sha256: ([0-9a-f]{64})", out).group(1)
        self.assertIn(f"--yes-apply-diff {digest[:16]}", str(code))
        self.assertIn("sha256 prefix of THIS diff", str(code))
        self.assertNotIn("edit", c.names())

    def test_F3_a_bare_or_short_pin_is_refused_and_nothing_is_created(self):
        out, digest = self.review()
        for pin in ([], [""], [digest[:1]], [digest[:8]], [digest[:11]], ["z" * 12], [digest[:11] + "g"]):
            c = LiveClient()
            root, paper, (code, _, _) = update_cli(c, "--set-title", "--yes-apply-diff", *pin, raw_confirm=True)
            self.assertIn("at least 12 hex characters", str(code), pin)
            for n in ("edit", "put", "publish"):
                self.assertNotIn(n, c.names(), pin)

    def test_a_one_command_review_and_apply_is_not_possible(self):
        c = LiveClient()
        root, paper, (code, _, _) = update_cli(c, "--set-title", "--yes-apply-diff", raw_confirm=True)
        self.assertIsNotNone(code)
        self.assertNotIn("edit", c.names())

    def test_a_wrong_pin_of_enough_length_is_refused_as_a_mismatch(self):
        out, digest = self.review()
        wrong = ("0" if digest[0] != "0" else "1") + digest[1:12]
        c = LiveClient()
        root, paper, (code, _, _) = update_cli(c, "--set-title", "--yes-apply-diff", wrong, raw_confirm=True)
        self.assertIn("does not match", str(code))
        self.assertNotIn("edit", c.names())

    def test_12_to_64_characters_in_any_case_are_accepted(self):
        out, digest = self.review()
        for pin in (digest[:12], digest[:20].upper(), digest):
            c = LiveClient()
            root, paper, (code, _, _) = update_cli(c, "--set-title", "--yes-apply-diff", pin, raw_confirm=True)
            self.assertIsNone(code, (pin, code))
            self.assertIn("publish", c.names())

    def test_the_dry_run_digest_is_the_same_function_of_the_same_diff(self):
        root, paper = workspace(zenodo_id=5)
        snap = Path(tempfile.mkdtemp()) / "live.json"
        snap.write_text(json.dumps({"metadata": copy.deepcopy(LIVE)}))
        code, out, _ = run_main([str(paper), "--dry-run", "--update", "--set-title", "--live-snapshot", str(snap)],
                                {}, root, Factory())
        self.assertIsNone(code, code)
        digest = re.search(r"diff sha256: ([0-9a-f]{64})", out).group(1)
        payload = zp.build_update_payload(LIVE, zp.build_metadata("T", "A", [], "https://w", "1.0"), {"title"}, "")
        self.assertEqual(zp.metadata_diff(LIVE, payload)[1], digest)


class UnreadableLiveMetadata(unittest.TestCase):
    def test_Y27_a_published_record_without_readable_metadata_is_never_updated(self):
        for meta in (None, {}, "garbage"):
            class Unreadable(FakeClient):
                def get_deposition(self, i):
                    d = super().get_deposition(i)
                    if i == 10:
                        d["metadata"] = meta
                    return d
            c = Unreadable()
            code, out, err = run(fakes.upd, c, fields=("title",), confirm=None)
            self.assertIn("cannot be read", str(code), meta)
            for n in ("edit", "put", "publish"):
                self.assertNotIn(n, c.names())


# ── Version label (Y19), journal bookkeeping (Y26, Y28) ──────────────────────────────────────────

class VersionShape(unittest.TestCase):
    def label(self, v):
        d = Path(tempfile.mkdtemp())
        return zp.check_version_label(v, d, {"version": v}, branch_rule=False)

    def test_Y19_implausible_labels_are_refused_even_when_every_other_source_agrees(self):
        for bad in ("1.0 beta", "v 1", "1/0", "../x", "-1", "1.0;rm", "a..b", "1.0\x00", "é1", "x" * 65, "_1"):
            with self.assertRaises(zp.VersionLabelError, msg=repr(bad)):
                self.label(bad)

    def test_ordinary_labels_pass(self):
        for good in ("1.0", "1.0.1", "2.16", "1.0rc1", "3.0-beta+7", "1_0", "x" * 64):
            self.label(good)


class JournalBookkeeping(unittest.TestCase):
    def fresh(self):
        return zp.Journal.create(Path(tempfile.mkdtemp()), Path("/x/paper"), "zenodo.org", False, {})

    def test_Y28_each_write_intent_is_counted_and_recorded(self):
        j = self.fresh()
        j.write_intent("POST", "/api/a")
        j.write_intent("PUT", "/api/b")
        on_disk = json.loads(j.path.read_text())
        self.assertEqual((on_disk["writes_sent"], on_disk["last_write"]), (2, "PUT /api/b"))

    def test_Y26_a_run_that_exits_after_a_write_is_never_cleared(self):
        j = self.fresh()
        zp.finalize_journal(j)
        self.assertEqual(json.loads(j.path.read_text())["outcome"], "NOT-PUBLISHED")
        j = self.fresh()
        j.write_intent("POST", "/api/a")
        zp.finalize_journal(j)
        data = json.loads(j.path.read_text())
        self.assertEqual(data["outcome"], "UNRECONCILED")
        self.assertIn("UNRECONCILED", data["outcome"])

    def test_Y26_a_state_created_without_a_counted_write_is_not_cleared_either(self):
        j = self.fresh()
        zp.finalize_journal(j, FakeClient())
        self.assertEqual(j.outcome(), "NOT-PUBLISHED")
        j = self.fresh()
        c = FakeClient()
        c.created_state = {"dep_id": 1, "kind": "x", "certain": True}
        zp.finalize_journal(j, c)
        self.assertEqual(j.outcome(), "UNRECONCILED")

    def test_Y26_through_main_a_sent_write_followed_by_a_crash_leaves_a_blocking_journal(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        sim = Sim(upload="interrupt")
        sim.discard_status = 500
        client, _ = real_client(sim)
        run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory(client))
        self.assertIn(journals(root)[0]["outcome"], zp.BLOCKING_OUTCOMES)
        self.assertGreater(journals(root)[0]["writes_sent"], 0)


# ── Pilot files (F14) ────────────────────────────────────────────────────────────────────────────

class PilotFiles(unittest.TestCase):
    PILOT = Path(__file__).resolve().parent.parent / "pilot"

    def sh(self, *args):
        import subprocess
        return subprocess.run(["sh", str(self.PILOT / "checksums_before_after.sh"), *args],
                              capture_output=True, text=True)

    def test_labels_with_path_separators_or_dots_are_refused_and_nothing_is_written(self):
        root = Path(tempfile.mkdtemp())
        paper = make_paper(root)
        out = Path(tempfile.mkdtemp())
        for label in ("../esc", "a/b", "..", ".", "", ".hidden", "-x", "a b", "x\ny"):
            r = self.sh("snapshot", label, str(paper), str(root), str(out))
            self.assertEqual(r.returncode, 2, repr(label))
        self.assertEqual(list(out.iterdir()), [])
        self.assertEqual(list(out.parent.glob("esc.snapshot")), [])
        r = self.sh("snapshot", "after-P1_ok.2", str(paper), str(root), str(out))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_dangling_symlink_destination_is_refused(self):
        root = Path(tempfile.mkdtemp())
        paper = make_paper(root)
        out = Path(tempfile.mkdtemp())
        os.symlink(out / "target-that-does-not-exist", out / "before.snapshot")
        r = self.sh("snapshot", "before", str(paper), str(root), str(out))
        self.assertEqual(r.returncode, 2)
        self.assertFalse((out / "target-that-does-not-exist").exists())

    def test_the_pilot_protocol_never_puts_the_token_in_an_argument_vector(self):
        text = (self.PILOT / "README-PILOT.md").read_text()
        for bad in ('ZENODO_SANDBOX_TOKEN="$SBX"', "Bearer $SBX", 'Bearer "$SBX"', "env -i", "-H \"Authorization"):
            self.assertNotIn(bad, text)
        self.assertIn("sandbox_launch.py", text)
        self.assertNotIn("expected refusal: inside the workspace", text)

    def test_the_launcher_reads_the_token_from_the_terminal_and_never_from_argv(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_launch", self.PILOT / "sandbox_launch.py")
        launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launch)
        seen = {}

        class FakeZp:
            @staticmethod
            def main(args):
                seen["args"] = list(args)
                seen["env_token"] = os.environ.get("ZENODO_SANDBOX_TOKEN")
                seen["env_prod"] = os.environ.get("ZENODO_TOKEN")
                seen["env_keys"] = sorted(os.environ)

        environ = {"PATH": "/usr/bin", "HOME": "/h", "FOO": "bar"}
        with mock.patch.dict(os.environ, environ, clear=True):
            code = launch.main(["--", "paper/", "--sandbox", "--state-dir", "d"], getpass_fn=lambda prompt: "TOK-123",
                               zp=FakeZp)
        self.assertEqual(code, 0)
        self.assertEqual(seen["args"], ["paper/", "--sandbox", "--state-dir", "d"])
        self.assertNotIn("TOK-123", " ".join(seen["args"]))
        self.assertEqual(seen["env_token"], "TOK-123")
        self.assertEqual(seen["env_keys"], ["HOME", "PATH", "ZENODO_SANDBOX_TOKEN"])

    def test_the_launcher_refuses_with_a_production_token_or_without_sandbox(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sandbox_launch", self.PILOT / "sandbox_launch.py")
        launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launch)

        def never(prompt):
            raise AssertionError("the token must not be requested")
        with mock.patch.dict(os.environ, {"ZENODO_TOKEN": "x", "PATH": "/usr/bin"}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                launch.main(["--", "p/", "--sandbox"], getpass_fn=never, zp=object())
            self.assertIn("ZENODO_TOKEN", str(cm.exception))
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                launch.main(["--", "p/"], getpass_fn=never, zp=object())
            self.assertIn("--sandbox", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
