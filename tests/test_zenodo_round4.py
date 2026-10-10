"""Offline tests for the fourth corrective pass (review round 20: F-1..F-5, N03 and the five test gaps R1, R11, R15,
R17, R28). Every class names the finding it closes; the reviewer's probes M14..M20 (reconcile), M11 (outcome),
M03/M05 (redaction) and N03 are converted into permanent tests.

No network: a socket guard fails any real connection; HTTP is the scripted Sim / FakeSession / FakeClient.
"""
import copy
import errno
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import PROD_TOKEN, SBX_TOKEN, FakeClient, Sim, real_client  # noqa: E402
import zenodo_publish as zp  # noqa: E402
from test_zenodo_contract import Factory, run_main, state_dir_for, workspace  # noqa: E402
from test_zenodo_round3 import (LIVE, LiveClient, NoConcept, journal_paths, update_cli,  # noqa: E402
                                write_journal)

ENV = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}
PROD_ONLY = {"ZENODO_TOKEN": PROD_TOKEN}
GOOD_DOI = "10.5281/zenodo.5"
REASON = "looked at /deposit and the record page"


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


def reconcile(jp, *extra, root):
    return run_main(["--reconcile", str(jp), *extra], {}, root)


def published_args(rid="301", cid="5", doi=GOOD_DOI):
    return ["--outcome", "published", "--record-id", rid, "--concept-id", cid, "--concept-doi", doi]


def make_blocking_journal(root, paper, outcome="UNRECONCILED", host="zenodo.org", sandbox=False, identity=True,
                          dep_id=301, paper_dir=None, **extra):
    """A journal the tool itself wrote (digest included) for ``paper``, left blocking."""
    cfg = json.loads((paper / "zenodo.json").read_text())
    ident = zp.journal_identity(cfg, "T") if identity else {}
    j = zp.Journal.create(state_dir_for(root), paper_dir or paper, host, sandbox, {}, "1.0", None, ident)
    j.update(outcome=outcome, deposition_id=dep_id, **extra)
    return j


def tamper(jp, **fields):
    data = json.loads(Path(jp).read_text())
    data.update(fields)
    Path(jp).write_text(json.dumps(data, indent=2))


class Bytes:
    """Snapshot of files to prove 'nothing was written'."""

    def __init__(self, *paths):
        self.paths = paths
        self.before = [Path(p).read_bytes() for p in paths]

    def unchanged(self):
        return [Path(p).read_bytes() for p in self.paths] == self.before


# ── F-1: reconcile bound to the REAL path and the verified identity of the paper ─────────────────────

class ReconcileIdentityBinding(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)
        self.j = make_blocking_journal(self.root, self.paper)
        self.jp = self.j.path

    def test_a_journal_written_by_the_tool_carries_an_identity_digest_over_its_identity_fields(self):
        data = json.loads(self.jp.read_text())
        self.assertEqual(data["identity_sha256"], zp.identity_digest(data))
        self.assertEqual(zp.journal_identity_problems(data), [])
        fields = ("schema", "paper", "paper_dir", "paper_dir_real", "host", "sandbox", "kind", "bib_key", "title",
                  "config_sha256", "config_core_sha256")
        self.assertEqual(zp.IDENTITY_FIELDS, fields)
        for field in fields:
            other = dict(data, **{field: "changed"})
            self.assertNotEqual(zp.identity_digest(other), data["identity_sha256"], field)
            self.assertTrue(zp.journal_identity_problems(other), field)

    def test_every_later_write_keeps_the_digest_consistent(self):
        self.j.update(note="later step")
        self.j.write_intent("POST", "/x")
        self.assertEqual(zp.journal_identity_problems(json.loads(self.jp.read_text())), [])

    def test_the_cure_still_works_on_an_untouched_journal_and_writes_the_recorded_real_path(self):
        code, out, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, GOOD_DOI))
        data = json.loads(self.jp.read_text())
        self.assertEqual(data["operator_evidence"]["config_written"], str(self.paper / "zenodo.json"))
        self.assertEqual(zp.journal_identity_problems(data), [])          # the reconcile's own write keeps the digest

    def test_probe_M14_a_journal_edited_to_point_at_another_paper_with_the_same_bib_key_is_refused(self):
        _, other = workspace(zenodo_id=None, doi=None)                      # same bib_key, same content
        snap = Bytes(other / "zenodo.json", self.paper / "zenodo.json", self.jp)
        tamper(self.jp, paper_dir=str(other), paper_dir_real=str(other))
        before = Bytes(self.jp)
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("was edited", str(code))
        self.assertIn("Nothing was written", str(code))
        self.assertTrue(Bytes(other / "zenodo.json", self.paper / "zenodo.json").unchanged() and before.unchanged())
        self.assertEqual(snap.before[0], (other / "zenodo.json").read_bytes())

    def test_an_edited_bib_key_title_hash_host_or_kind_is_refused(self):
        original = self.jp.read_text()
        for field, value in (("bib_key", "Other2026"), ("title", "Other"), ("config_sha256", "0" * 64),
                             ("config_core_sha256", "0" * 64), ("host", "sandbox.zenodo.org"), ("kind", "update"),
                             ("sandbox", True), ("paper", "elsewhere")):
            self.jp.write_text(original)
            tamper(self.jp, **{field: value})
            code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
            self.assertIn("was edited", str(code), field)
            self.assertFalse((self.paper / "zenodo.json").read_text().count('"zenodo_id": 5'), field)
        self.jp.write_text(original)

    def test_a_tampered_journal_cannot_be_reconciled_as_not_published_either(self):
        tamper(self.jp, paper_dir="/somewhere/else")
        code, _, _ = reconcile(self.jp, "--outcome", "not-published", "--i-checked-the-account", "--reason", REASON,
                               root=self.root)
        self.assertIn("cannot be trusted", str(code))
        self.assertEqual(json.loads(self.jp.read_text())["outcome"], "UNRECONCILED")

    def test_a_journal_without_the_digest_is_refused(self):
        data = json.loads(self.jp.read_text())
        data.pop("identity_sha256")
        self.jp.write_text(json.dumps(data))
        cfg = Bytes(self.paper / "zenodo.json")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("no identity digest", str(code))
        self.assertTrue(cfg.unchanged())

    def test_a_hand_written_journal_is_refused(self):
        hand = write_journal(state_dir_for(self.root), "paper-hand.run.json", paper_dir=str(self.paper))
        code, _, _ = reconcile(hand, *published_args(), root=self.root)
        self.assertIn("no identity digest", str(code))

    def test_a_journal_without_a_bib_key_cannot_write_a_zenodo_json(self):
        bare = make_blocking_journal(self.root, self.paper, identity=False)       # digest valid, bib_key absent
        self.assertEqual(zp.journal_identity_problems(json.loads(bare.path.read_text())), [])
        cfg = Bytes(self.paper / "zenodo.json")
        code, _, _ = reconcile(bare.path, *published_args(), root=self.root)
        self.assertIn("lacks identity fields", str(code))
        self.assertIn("bib_key", str(code))
        self.assertTrue(cfg.unchanged())

    def test_a_journal_without_the_recorded_real_path_is_refused(self):
        data = json.loads(self.jp.read_text())
        data.pop("paper_dir_real")
        self.jp.write_text(zp._journal_text(data))                                   # valid digest over the new fields
        cfg = Bytes(self.paper / "zenodo.json")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("paper_dir_real", str(code))
        self.assertTrue(cfg.unchanged())

    def test_a_journal_without_the_content_hash_is_refused(self):
        data = json.loads(self.jp.read_text())
        data.pop("config_core_sha256")
        self.jp.write_text(zp._journal_text(data))
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("config_core_sha256", str(code))

    def test_a_paper_dir_that_no_longer_exists_is_refused(self):
        gone = Path(tempfile.mkdtemp()).resolve() / "vanished"
        j = make_blocking_journal(self.root, self.paper, paper_dir=gone)
        code, _, _ = reconcile(j.path, *published_args(), root=self.root)
        self.assertIn("no longer exists", str(code))
        self.assertFalse(gone.exists())

    def test_a_paper_dir_that_disagrees_with_its_recorded_real_path_is_refused(self):
        _, other = workspace(zenodo_id=None, doi=None)
        data = json.loads(self.jp.read_text())
        data["paper_dir"] = str(other)                                               # paper_dir != paper_dir_real
        self.jp.write_text(zp._journal_text(data))                                   # valid digest: hand-built journal
        cfgs = Bytes(self.paper / "zenodo.json", other / "zenodo.json")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("paper_dir_real", str(code))
        self.assertTrue(cfgs.unchanged())

    def test_a_symlinked_paper_dir_retargeted_elsewhere_is_refused(self):
        link = self.root / "alias"
        os.symlink(self.paper, link)
        j = make_blocking_journal(self.root, self.paper, paper_dir=link)
        data = json.loads(j.path.read_text())
        self.assertEqual((data["paper_dir"], data["paper_dir_real"]), (str(link), str(self.paper)))
        other_root, other = workspace(zenodo_id=None, doi=None)                      # same bib_key and content
        swap = self.root / "alias.new"
        os.symlink(other, swap)
        os.replace(swap, link)                                                       # the link now points elsewhere
        cfgs = Bytes(self.paper / "zenodo.json", other / "zenodo.json", j.path)
        code, _, _ = reconcile(j.path, *published_args(), root=self.root)
        self.assertIn("realpath is now", str(code))
        self.assertTrue(cfgs.unchanged())

    def test_a_symlinked_paper_dir_that_still_points_at_the_same_place_is_accepted_and_the_real_path_is_written(self):
        link = self.root / "alias"
        os.symlink(self.paper, link)
        j = make_blocking_journal(self.root, self.paper, paper_dir=link)
        code, _, _ = reconcile(j.path, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        self.assertEqual(json.loads((self.paper / "zenodo.json").read_text())["zenodo_id"], 5)
        written = json.loads(j.path.read_text())["operator_evidence"]["config_written"]
        self.assertEqual(written, str(self.paper / "zenodo.json"))                   # the real path, not the alias

    def test_the_directory_given_on_the_command_line_must_be_the_recorded_real_path(self):
        other_root, other = workspace(zenodo_id=None, doi=None)
        code, _, _ = run_main([str(other), "--reconcile", str(self.jp), *published_args()], {}, self.root)
        self.assertIn("belongs to the paper directory", str(code))

    def test_a_zenodo_json_changed_since_the_run_is_not_the_file_of_the_journal(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["web_url"] = "https://changed.example"
        (self.paper / "zenodo.json").write_text(json.dumps(cfg))
        before = Bytes(self.paper / "zenodo.json")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("no longer has the content", str(code))
        self.assertTrue(before.unchanged())

    def test_a_zenodo_json_that_holds_only_the_ids_written_by_an_earlier_partial_save_still_matches(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["zenodo_id"], cfg["doi"] = 5, GOOD_DOI
        (self.paper / "zenodo.json").write_text(json.dumps(cfg))
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIsNone(code, code)

    def test_the_zenodo_json_is_written_through_a_temporary_file_and_a_replace(self):
        target = str(self.paper / "zenodo.json")
        replaced = []
        real = os.replace
        with mock.patch.object(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real(a, b))[1]):
            code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        self.assertTrue(any(b == target and a != target and a.startswith(str(self.paper)) for a, b in replaced), replaced)

    def test_probe_M16_a_hand_edited_unsaved_journal_without_bib_key_still_cannot_reach_another_paper(self):
        _, other = workspace(zenodo_id=None, doi=None)
        bare = make_blocking_journal(self.root, self.paper, outcome=zp.UNSAVED_OUTCOME, identity=False)
        tamper(bare.path, paper_dir=str(other), paper_dir_real=str(other))
        cfgs = Bytes(other / "zenodo.json")
        code, _, _ = reconcile(bare.path, *published_args(), root=self.root)
        self.assertIsNotNone(code)
        self.assertTrue(cfgs.unchanged())


# ── F-2: a journal with an absent or unknown outcome BLOCKS (fail closed) ───────────────────────────

class UnknownOutcomeBlocks(unittest.TestCase):
    def blocked(self, root, paper):
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, f)
        return "REFUSING TO START" in str(code), str(code), f

    def journal_with(self, root, paper, **changes):
        j = make_blocking_journal(root, paper)
        data = json.loads(j.path.read_text())
        for k, v in changes.items():
            if v is zp:                      # sentinel: remove
                data.pop(k, None)
            else:
                data[k] = v
        j.path.write_text(json.dumps(data))
        return j.path

    def test_probe_M11_a_journal_without_outcome_blocks_and_names_the_journal_and_the_cure(self):
        root, paper = workspace()
        jp = self.journal_with(root, paper, outcome=zp)
        blocked, msg, f = self.blocked(root, paper)
        self.assertTrue(blocked, msg)
        self.assertIn(str(jp), msg)
        self.assertIn("--lift-block", msg)
        self.assertIn("absent or unknown outcome", msg)
        self.assertEqual(f.built, [])

    def test_an_unknown_or_mistyped_or_future_outcome_blocks(self):
        for outcome in ("PUBLISHD", "published", "", "NEW-FUTURE-OUTCOME", "UNREADABLE", None, 5, ["PUBLISHED"],
                        {"x": 1}, False):
            root, paper = workspace()
            self.journal_with(root, paper, outcome=outcome)
            blocked, msg, _ = self.blocked(root, paper)
            self.assertTrue(blocked, (outcome, msg))

    def test_every_known_non_blocking_outcome_does_not_block(self):
        self.assertEqual(set(zp.NON_BLOCKING_OUTCOMES),
                         {"PUBLISHED", "PUBLISHED-RECONCILED", "NOT-PUBLISHED", "NOT-PUBLISHED-RECONCILED"})
        for outcome in zp.NON_BLOCKING_OUTCOMES:
            root, paper = workspace()
            self.journal_with(root, paper, outcome=outcome)
            blocked, msg, _ = self.blocked(root, paper)
            self.assertFalse(blocked, (outcome, msg))

    def test_every_blocking_outcome_still_blocks(self):
        for outcome in zp.BLOCKING_OUTCOMES:
            root, paper = workspace()
            self.journal_with(root, paper, outcome=outcome)
            self.assertTrue(self.blocked(root, paper)[0], outcome)

    def test_the_known_sets_partition_the_outcomes_the_code_writes(self):
        self.assertFalse(set(zp.BLOCKING_OUTCOMES) & set(zp.NON_BLOCKING_OUTCOMES))
        self.assertEqual(set(zp.KNOWN_OUTCOMES), set(zp.BLOCKING_OUTCOMES) | set(zp.NON_BLOCKING_OUTCOMES))
        src = Path(zp.__file__).read_text()
        written = set(re.findall(r'outcome="([A-Z-]+)"', src)) | set(re.findall(r'mark\("([A-Z-]+)"', src))
        written -= {"UNREADABLE"}
        self.assertTrue(written <= set(zp.KNOWN_OUTCOMES), written - set(zp.KNOWN_OUTCOMES))

    def test_an_unknown_outcome_journal_of_an_unrelated_paper_does_not_block(self):
        root, paper = workspace()
        root2, other = workspace(bib_key="Other2026", web_url="https://other")
        other.rename(root2 / "unrelated-paper")
        other = root2 / "unrelated-paper"
        (other / "tex" / "P.tex").write_text(
            "\\documentclass{article}\\title{Different}\\begin{document}\\begin{abstract}A\\end{abstract}\\end{document}")
        j = make_blocking_journal(root2, other)
        tamper(j.path, outcome="WHATEVER", title="Different")
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks", "--state-dir", str(state_dir_for(root2))], PROD_ONLY, root, f)
        self.assertNotIn("REFUSING", str(code))

    def test_a_hand_written_journal_with_an_unknown_outcome_is_attributed_by_its_file_name(self):
        root, paper = workspace()
        write_journal(state_dir_for(root), "paper-20261001T000000Z-1-abc123.run.json", outcome="WEIRD",
                      paper_dir="/nowhere/else", host="zenodo.org")        # no identity field matches this paper
        blocked, msg, _ = self.blocked(root, paper)
        self.assertTrue(blocked, msg)
        self.assertIn("file named after this paper", msg)
        write_journal(state_dir_for(root), "another-20261001T000000Z-1-abc123.run.json", outcome="WEIRD",
                      paper_dir="/nowhere/else", host="zenodo.org")        # not this paper's name: no match
        root2, paper2 = workspace()
        write_journal(state_dir_for(root2), "another-20261001T000000Z-1-abc123.run.json", outcome="WEIRD",
                      paper_dir="/nowhere/else", host="zenodo.org")
        self.assertFalse(self.blocked(root2, paper2)[0])

    def test_the_lift_block_cure_works_on_a_readable_journal_with_an_unknown_outcome_and_unblocks(self):
        root, paper = workspace()
        jp = self.journal_with(root, paper, outcome="PUBLISHD")
        content = jp.read_bytes()
        code, out, _ = run_main(["--lift-block", str(jp), "--i-checked-the-account", "--reason", REASON], {}, root)
        self.assertIsNone(code, code)
        self.assertEqual(jp.read_bytes(), content)                      # NOT renamed, NOT touched (was: renamed)
        self.assertTrue(Path(str(jp) + ".decision.json").exists())
        self.assertFalse(self.blocked(root, paper)[0])

    def test_lift_block_still_refuses_a_readable_journal_with_a_known_outcome(self):
        root, paper = workspace()
        j = make_blocking_journal(root, paper)
        code, _, _ = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON], {}, root)
        self.assertIn("reconcile", str(code))
        self.assertTrue(j.path.exists())
        self.assertFalse(Path(str(j.path) + ".decision.json").exists())

    def test_reconcile_of_an_unknown_outcome_points_at_lift_block(self):
        root, paper = workspace()
        jp = self.journal_with(root, paper, outcome="PUBLISHD")
        code, _, _ = reconcile(jp, *published_args(), root=root)
        self.assertIn("--lift-block", str(code))
        self.assertIn("absent or unknown outcome", str(code))
        self.assertEqual(json.loads(jp.read_text())["outcome"], "PUBLISHD")


# ── F-3 / F-4: identifiers, DOI, URL and reason are validated BEFORE any write ───────────────────────

class ReconcileInputValidation(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)
        self.jp = make_blocking_journal(self.root, self.paper).path
        self.snap = Bytes(self.paper / "zenodo.json", self.jp)

    def refused(self, *args, expect=None):
        code, out, err = reconcile(self.jp, *args, root=self.root)
        self.assertIsNotNone(code, args)
        self.assertIsInstance(code, str, args)                    # a message, never a traceback
        self.assertNotIn("Traceback", out + err)
        self.assertTrue(self.snap.unchanged(), args)
        if expect:
            self.assertIn(expect, code, args)
        return code

    def test_probe_M17_concept_id_garbage_is_a_message_not_a_traceback(self):
        for bad in ("abc", "-5", "+5", " 5", "5 ", "0", "05", "5.0", "1" * 13, "0x5", "５", "5\n", "1e3", "٣"):
            self.refused("--outcome", "published", "--record-id", "301", "--concept-id", bad, "--concept-doi", GOOD_DOI,
                         expect="--concept-id")

    def test_record_id_garbage_is_refused(self):
        for bad in ("abc", "-1", "+301", " 301", "0", "007", "3.5", "9" * 13, "", "٣٠١"):
            self.refused("--outcome", "published", "--record-id", bad, "--concept-id", "5", "--concept-doi", GOOD_DOI)

    def test_probe_M18_concept_doi_garbage_is_refused(self):
        for bad in ("hello world", "10.12/x", "10.1234567890/x", "10.1234/", "10.1234/a b", "doi:10.1234/x",
                    "https://doi.org/10.1234/x", "11.1234/x", " 10.1234/x", "10.1234/x ", "10.1234/x\ny"):
            self.refused("--outcome", "published", "--record-id", "301", "--concept-id", "5", "--concept-doi", bad,
                         expect="--concept-doi")

    def test_a_zenodo_doi_must_carry_the_concept_id(self):
        self.refused("--outcome", "published", "--record-id", "301", "--concept-id", "5",
                     "--concept-doi", "10.5281/zenodo.6", expect="must be equal")
        self.refused("--outcome", "published", "--record-id", "301", "--concept-id", "5",
                     "--concept-doi", "10.5281/zenodo.abc", expect="positive integer")
        self.refused("--outcome", "published", "--record-id", "301", "--concept-id", "5",
                     "--concept-doi", "10.5281/ZENODO.50", expect="must be exactly")

    def test_a_consistent_zenodo_doi_and_a_foreign_doi_are_accepted(self):
        code, _, _ = reconcile(self.jp, *published_args(doi="10.5281/zenodo.5"), root=self.root)
        self.assertIsNone(code, code)

    def test_a_non_zenodo_doi_is_refused_for_a_zenodo_concept(self):       # was: accepted (N-1 closes it)
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper).path
        snap = Bytes(paper / "zenodo.json", jp)
        code, _, _ = reconcile(jp, *published_args(doi="10.9999/anything.123"), root=root)
        self.assertIn("must be exactly", str(code))
        self.assertTrue(snap.unchanged())

    def test_the_doi_check_also_applies_when_the_concept_id_comes_from_the_journal(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper, concept_id="7").path
        snap = Bytes(paper / "zenodo.json", jp)
        code, _, _ = reconcile(jp, "--outcome", "published", "--record-id", "301", "--concept-doi", GOOD_DOI, root=root)
        self.assertIn("must be equal", str(code))
        self.assertTrue(snap.unchanged())

    def test_probe_M20_reason_blank_or_too_short_is_not_evidence(self):
        for bad in ("", "   ", "short", "123456789", " 123456789 ", "\t\n  \t", "x" * 3):
            self.refused("--outcome", "not-published", "--i-checked-the-account", "--reason", bad)
        code, _, _ = reconcile(self.jp, "--outcome", "not-published", "--i-checked-the-account",
                               "--reason", "  0123456789  ", root=self.root)               # exactly 10 after strip
        self.assertIsNone(code, code)
        self.assertEqual(json.loads(self.jp.read_text())["operator_evidence"]["reason"], "0123456789")

    def test_a_short_reason_is_refused_even_next_to_a_record_url(self):
        self.refused("--outcome", "not-published", "--record-url", "https://zenodo.org/deposit/301",
                     "--reason", "ok", expect="--reason")

    def test_the_lift_block_reason_follows_the_same_rule(self):
        bad = state_dir_for(self.root) / "paper-1.run.json"
        bad.write_text("{trunc")
        for reason in ("", "  ", "x", "123456789"):
            code, _, _ = run_main(["--lift-block", str(bad), "--i-checked-the-account", "--reason", reason], {}, self.root)
            self.assertIn("--reason", str(code), reason)
            self.assertTrue(bad.exists())

    def test_the_record_url_must_be_https_on_the_journals_host_and_hold_the_record_id(self):
        for bad in ("http://zenodo.org/deposit/301", "https://sandbox.zenodo.org/deposit/301",
                    "https://zenodo.org.evil.example/deposit/301", "https://user@zenodo.org/deposit/301",
                    "https://zenodo.org:8443/deposit/301", "https://zenodo.org/", "https://zenodo.org",
                    "https://zenodo.org/deposit/3010", "https://zenodo.org/deposit/30", "ftp://zenodo.org/deposit/301",
                    "zenodo.org/deposit/301", "https://evil.example/https://zenodo.org/deposit/301"):
            self.refused("--outcome", "not-published", "--record-url", bad)
        code, _, _ = reconcile(self.jp, "--outcome", "not-published", "--record-url",
                               "https://zenodo.org/deposit/301", root=self.root)
        self.assertIsNone(code, code)

    def test_the_record_url_follows_an_explicit_record_id_over_the_journals(self):
        self.refused("--outcome", "not-published", "--record-id", "999", "--record-url",
                     "https://zenodo.org/deposit/301", expect="record id 999")

    def test_a_sandbox_journal_accepts_only_a_sandbox_url(self):
        root, paper = workspace()
        j = make_blocking_journal(root, paper, host="sandbox.zenodo.org", sandbox=True)
        code, _, _ = reconcile(j.path, "--outcome", "not-published", "--record-url",
                               "https://zenodo.org/deposit/301", root=root)
        self.assertIsNotNone(code)
        code, _, _ = reconcile(j.path, "--outcome", "not-published", "--record-url",
                               "https://sandbox.zenodo.org/deposit/301", root=root)
        self.assertIsNone(code, code)

    def test_nothing_is_written_when_only_the_last_validated_input_is_bad(self):
        self.refused("--outcome", "published", "--record-id", "301", "--concept-id", "5", "--concept-doi", GOOD_DOI,
                     "--reason", "short")

    def test_validators_unit(self):
        self.assertEqual(zp.validate_id("123456789012", "x"), "123456789012")
        self.assertEqual(zp.validate_id(7, "x"), "7")
        for bad in (True, None, 1.5, "", "a"):
            with self.assertRaises(zp.ReconcileRefused):
                zp.validate_id(bad, "x")
        self.assertEqual(zp.validate_reason("  ten chars!  "), "ten chars!")
        with self.assertRaises(zp.ReconcileRefused):
            zp.validate_reason(None)


# ── F-5: redaction by value (both tokens, any length) and by field NAME ─────────────────────────────

class RedactionByName(unittest.TestCase):
    NAMES = ("Authorization", "X-Api-Key", "X-API-KEY", "Api-Key", "Cookie", "Set-Cookie", "Proxy-Authorization",
             "access_token", "token", "refresh_token", "client_secret", "password")

    def test_every_sensitive_name_is_masked_in_a_header_dict_whatever_the_value(self):
        for name in self.NAMES:
            for variant in (name, name.lower(), name.upper()):
                out = zp.redact_obj({variant: "plain-visible-value", "Accept": "application/json"})
                self.assertEqual(out[variant], "[REDACTED]", variant)
                self.assertEqual(out["Accept"], "application/json")
        for value in (None, 12, ["a", "b"], {"nested": "v"}):
            self.assertEqual(zp.redact_obj({"Authorization": value})["Authorization"], "[REDACTED]")

    def test_nested_header_dicts_and_lists_are_walked(self):
        out = zp.redact_obj({"request": {"headers": {"cookie": "sid=abc"}}, "all": [{"password": "hunter2"}]})
        self.assertEqual(out["request"]["headers"]["cookie"], "[REDACTED]")
        self.assertEqual(out["all"][0]["password"], "[REDACTED]")

    def test_every_sensitive_name_is_masked_in_key_value_text(self):
        for name in self.NAMES:
            for variant in (name, name.lower(), name.upper()):
                self.assertNotIn("S3CR3T", zp.redact(f"failed with {variant}=S3CR3T and more"), variant)
                self.assertNotIn("S3CR3T", zp.redact(f"https://zenodo.org/api/x?page=2&{variant}=S3CR3T&size=5"), variant)
                self.assertNotIn("S3CR3T", zp.redact(f"{variant}: S3CR3T"), variant)

    def test_the_text_around_a_masked_query_value_survives(self):
        out = zp.redact("https://zenodo.org/api/x?page=2&access_token=S3CR3T&size=5")
        self.assertEqual(out, "https://zenodo.org/api/x?page=2&access_token=[REDACTED]&size=5")

    def test_json_looking_and_dict_repr_text(self):
        for text in ('{"password": "S3 CR3T", "ok": 1}', "{'X-API-KEY': 'S3CR3T', 'a': 1}",
                     '{"headers": {"Authorization": "Bearer S3CR3T"}}', '{"refresh_token":"S3CR3T"}',
                     "headers={'Cookie': 'sid=S3CR3T; other=1'}", 'client_secret = "S3CR3T"'):
            out = zp.redact(text)
            self.assertNotIn("S3CR3T", out, text)
            self.assertNotIn("S3 CR3T", out, text)
        self.assertIn('"ok": 1', zp.redact('{"password": "S3 CR3T", "ok": 1}'))

    def test_a_cookie_header_is_masked_to_the_end_of_the_line_and_the_next_line_survives(self):
        out = zp.redact("Cookie: a=S3CR3T; b=2\nnext line")
        self.assertEqual(out, "Cookie: [REDACTED]\nnext line")

    def test_names_inside_other_identifiers_are_not_touched(self):
        self.assertEqual(zp.redact("ZENODO_TOKEN is not set"), "ZENODO_TOKEN is not set")
        self.assertEqual(zp.redact("the cookies are nice"), "the cookies are nice")

    def test_redaction_is_idempotent(self):
        text = "Authorization: Bearer S3CR3T ?token=S3CR3T&password=S3CR3T"
        self.assertEqual(zp.redact(zp.redact(text)), zp.redact(text))

    def test_the_session_headers_repr_never_shows_the_credential(self):
        sess = fakes.FakeSession()
        sess.headers = {"Authorization": "Bearer abc.def", "X-Api-Key": "k-123", "Accept": "x"}
        out = zp.redact(f"request failed with headers {dict(sess.headers)}")
        self.assertNotIn("abc.def", out)
        self.assertNotIn("k-123", out)

    def test_names_are_masked_before_a_journal_write(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False,
                              {"headers": {"X-Api-Key": "S3CR3T-intent", "Cookie": "sid=S3CR3T-cookie"}}, "1.0")
        j.update(note="boom password=S3CR3T-note", extra={"client_secret": "S3CR3T-secret", "list": ["token=S3CR3T-l"]},
                 refresh_token="S3CR3T-top")
        raw = j.path.read_text()
        self.assertNotIn("S3CR3T", raw)
        self.assertIn("[REDACTED]", raw)
        self.assertEqual(zp.journal_identity_problems(json.loads(raw)), [])


class RedactionByValue(unittest.TestCase):
    def test_probe_M05_a_short_token_present_only_in_the_other_variable_is_masked(self):
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_environment_secrets({"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": "Z~"})
            self.assertEqual(zp.redact("the sandbox token Z~ leaked"), "the sandbox token [REDACTED] leaked")

    def test_both_tokens_are_masked_whenever_either_is_present_whatever_the_length(self):
        for prod, sbx in (("Q~", "Z~"), ("Q", "Z"), ("Q~", SBX_TOKEN), (PROD_TOKEN, "z"), ("  Q~  ", " Z~ ")):
            with mock.patch.object(zp, "_SECRETS", set()):
                zp.register_environment_secrets({"ZENODO_TOKEN": prod, "ZENODO_SANDBOX_TOKEN": sbx})
                text = f"a {prod.strip()} b {sbx.strip()} c"
                out = zp.redact(text)
                self.assertNotIn(prod.strip(), out.replace("[REDACTED]", ""), (prod, sbx))
                self.assertNotIn(sbx.strip(), out.replace("[REDACTED]", ""), (prod, sbx))

    def test_an_empty_or_blank_value_is_never_registered_and_masks_nothing(self):
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_secret("")
            zp.register_secret(None)
            zp.register_environment_secrets({"ZENODO_TOKEN": "", "ZENODO_SANDBOX_TOKEN": None})
            self.assertEqual(zp._SECRETS, set())
            self.assertEqual(zp.redact("nothing to hide"), "nothing to hide")
            zp.register_environment_secrets({"ZENODO_TOKEN": "   "})
            self.assertEqual(zp.redact("nothing to hide"), "nothing to hide")

    def test_a_one_character_secret_is_registered_and_masked_as_a_whole_token(self):
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_secret("Q")
            self.assertEqual(zp.redact("x Q y"), "x [REDACTED] y")
            self.assertEqual(zp.redact("xQy"), "xQy")          # no blind substring replacement of a short secret

    def test_a_whole_run_masks_a_short_sandbox_token_in_streams_and_journal(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        leak = RuntimeError("the other environment's token Z~ and ours in a message")
        client = FakeClient(fail_at="newversion", fail_exc=leak)
        with mock.patch.object(zp, "_SECRETS", set()):
            code, out, err = run_main([str(paper), "--skip-checks"], {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": "Z~"},
                                      root, Factory(client))
        for stream in (str(code), out, err):
            self.assertNotIn("Z~", stream)
        for p in state_dir_for(root).iterdir():
            self.assertNotIn(b"Z~", p.read_bytes())

    def test_a_whole_run_masks_a_short_production_token_too(self):
        root, paper = workspace(zenodo_id=5, doi="10.0/c")
        client = FakeClient(fail_at="newversion", fail_exc=RuntimeError("boom with Q~ and Authorization: Q~ inside"))
        with mock.patch.object(zp, "_SECRETS", set()):
            code, out, err = run_main([str(paper), "--skip-checks"], {"ZENODO_TOKEN": "Q~", "ZENODO_SANDBOX_TOKEN": SBX_TOKEN},
                                      root, Factory(client))
        for stream in (str(code), out, err):
            self.assertNotIn("Q~", stream)
        for p in state_dir_for(root).iterdir():
            self.assertNotIn(b"Q~", p.read_bytes())

    def test_probe_R28_dictionary_keys_are_redacted_too(self):
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_secret("SECRETKEY-123")
            out = zp.redact_obj({"SECRETKEY-123": 1, "Bearer abc123": 2, "plain": {"id-SECRETKEY-123": 3}})
            self.assertEqual(out, {"[REDACTED]": 1, "Bearer [REDACTED]": 2, "plain": {"id-[REDACTED]": 3}})
            self.assertNotIn("SECRETKEY-123", zp._journal_text({"SECRETKEY-123": "v", "n": {"SECRETKEY-123": 1}}))

    def test_non_string_keys_survive(self):
        self.assertEqual(zp.redact_obj({1: "a", None: "b"}), {1: "a", None: "b"})


# ── N03: an ambiguous related identifier is preserved or refused, never replaced implicitly ─────────

ENTRY = {"identifier": "https://new.example", "relation": "isSupplementedBy", "scheme": "url", "resource_type": "other"}


class WebUrlNeverReplacedImplicitly(unittest.TestCase):
    def test_N03_a_single_entry_without_resource_type_and_a_foreign_url_is_refused_and_named(self):
        live = [{"identifier": "https://github.com/x/y", "relation": "isSupplementedBy", "scheme": "url"}]
        with self.assertRaises(ValueError) as cm:
            zp.merge_related_web_url(live, ENTRY)
        self.assertIn("https://github.com/x/y", str(cm.exception))
        self.assertIn("--replace-related-identifier", str(cm.exception))
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, out, err) = update_cli(c, "--set-web-url", "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIn("https://github.com/x/y", str(code))
        self.assertIn("--replace-related-identifier", str(code))
        self.assertNotIn("edit", c.names())
        self.assertNotIn("put", c.names())

    def test_the_operator_can_name_the_entry_explicitly(self):
        live = [{"identifier": "https://github.com/x/y", "relation": "isSupplementedBy", "scheme": "url"},
                {"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"}]
        out = zp.merge_related_web_url(live, ENTRY, "https://github.com/x/y")
        self.assertEqual(out, [ENTRY, live[1]])
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, _, _) = update_cli(c, "--set-web-url", "--replace-related-identifier",
                                               "https://github.com/x/y", "--yes-apply-diff",
                                               cfg={"web_url": "https://new.example"})
        self.assertIsNone(code, code)
        self.assertEqual(c.meta_put["related_identifiers"], [ENTRY, live[1]])

    def test_the_explicit_identifier_must_match_exactly_one_live_entry(self):
        live = [{"identifier": "https://a", "relation": "cites", "scheme": "url"}]
        for name in ("https://missing", "https://A"):
            with self.assertRaises(ValueError):
                zp.merge_related_web_url(live, ENTRY, name)
        dup = live + [dict(live[0])]
        with self.assertRaises(ValueError):
            zp.merge_related_web_url(dup, ENTRY, "https://a")

    def test_the_explicit_identifier_replaces_whatever_the_relation_or_type(self):
        live = [{"identifier": "https://a", "relation": "isDocumentedBy", "scheme": "url", "resource_type": "software"}]
        self.assertEqual(zp.merge_related_web_url(live, ENTRY, "https://a"), [ENTRY])

    def test_replace_related_identifier_needs_set_web_url(self):
        c = LiveClient()
        root, paper, (code, _, _) = update_cli(c, "--set-title", "--replace-related-identifier", "https://x")
        self.assertEqual(code, 2)
        self.assertNotIn("edit", c.names())

    def test_N03_two_candidates_are_refused_as_ambiguous(self):
        live = [{"identifier": "https://cosmochrony.org/a", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"},
                {"identifier": "https://cosmochrony.org/b", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"}]
        with self.assertRaises(ValueError) as cm:
            zp.merge_related_web_url(live, ENTRY)
        self.assertIn("cannot tell which one is the web url", str(cm.exception))
        live2 = [{"identifier": "https://a", "relation": "isSupplementedBy", "scheme": "url"},
                 {"identifier": "https://b", "relation": "isSupplementedBy", "scheme": "url"}]
        with self.assertRaises(ValueError):
            zp.merge_related_web_url(live2, ENTRY)

    def test_N03_none_matching_appends_and_keeps_everything(self):
        live = [{"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"},
                {"identifier": "https://github.com/x/y", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "software"},
                {"identifier": "https://a", "relation": "isDocumentedBy", "scheme": "url", "resource_type": "other"}]
        self.assertEqual(zp.merge_related_web_url(live, ENTRY), live + [ENTRY])

    def test_a_single_typed_entry_is_replaced_in_place(self):
        live = [{"identifier": "10.1/abc", "relation": "cites", "scheme": "doi"},
                {"identifier": "https://cosmochrony.org/science/old/", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"}]
        self.assertEqual(zp.merge_related_web_url(live, ENTRY), [live[0], ENTRY])

    def test_the_cosmochrony_org_entry_is_the_web_entry_among_several_typed_candidates(self):
        live = [{"identifier": "https://elsewhere.example/x", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"},
                {"identifier": "https://cosmochrony.org/science/old/", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"}]
        self.assertEqual(zp.merge_related_web_url(live, ENTRY), [live[0], ENTRY])

    def test_a_cosmochrony_org_entry_without_resource_type_is_still_not_replaced_implicitly(self):
        live = [{"identifier": "https://elsewhere.example/x", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"},
                {"identifier": "https://www.cosmochrony.org/science/old/", "relation": "isSupplementedBy",
                 "scheme": "url"}]
        with self.assertRaises(ValueError) as cm:
            zp.merge_related_web_url(live, ENTRY)
        self.assertIn("www.cosmochrony.org", str(cm.exception))

    def test_a_host_that_only_ends_like_cosmochrony_org_is_not_the_web_host(self):
        live = [{"identifier": "https://evilcosmochrony.org/x", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"},
                {"identifier": "https://other.example/y", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"}]
        # neither host is the web host: both foreign typed entries are KEPT and the web entry is appended; had
        # evilcosmochrony.org been taken for the web host, its entry would have been replaced
        self.assertEqual(zp.merge_related_web_url(live, ENTRY), live + [ENTRY])

    def test_R17_an_entry_with_the_same_identifier_but_another_relation_is_not_the_web_entry(self):
        live = [{"identifier": "https://new.example", "relation": "cites", "scheme": "url"}]
        out = zp.merge_related_web_url(live, ENTRY)
        self.assertEqual(out, live + [ENTRY])
        both = live + [{"identifier": "https://new.example", "relation": "isSupplementedBy", "scheme": "url"}]
        self.assertEqual(zp.merge_related_web_url(both, ENTRY), [live[0], ENTRY])

    def test_the_live_list_is_not_mutated(self):
        live = [{"identifier": "https://cosmochrony.org/science/old/", "relation": "isSupplementedBy", "scheme": "url",
                 "resource_type": "other"}]
        frozen = copy.deepcopy(live)
        zp.merge_related_web_url(live, ENTRY)
        zp.merge_related_web_url(live, ENTRY, "https://cosmochrony.org/science/old/")
        self.assertEqual(live, frozen)

    def test_the_dry_run_diff_reports_the_refusal(self):
        root, paper = workspace(zenodo_id=5)
        live = {**LIVE, "related_identifiers": [{"identifier": "https://github.com/x/y", "relation": "isSupplementedBy",
                                                  "scheme": "url"}]}
        snap = Path(tempfile.mkdtemp()) / "live.json"
        snap.write_text(json.dumps({"metadata": live}))
        code, out, _ = run_main([str(paper), "--dry-run", "--update", "--set-web-url", "--live-snapshot", str(snap)],
                                {}, root, Factory())
        self.assertIn("Cannot build the update payload", out)
        self.assertIn("https://github.com/x/y", out)


# ── R11: the journal file is fsynced, then the directory ──────────────────────────────────────────────

class JournalDurability(unittest.TestCase):
    def test_R11_a_journal_write_fsyncs_the_file_and_then_the_directory(self):
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        events, real = [], os.fsync

        def spy(fd):
            mode = os.fstat(fd).st_mode
            events.append("dir" if stat.S_ISDIR(mode) else "file")
            return real(fd)
        with mock.patch.object(os, "fsync", spy):
            j.update(note="step")
        self.assertEqual(events, ["file", "dir"])

    def test_R11_the_file_is_fsynced_before_it_is_renamed_over_the_journal(self):
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        order = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                order.append("file-fsync")
            return real_fsync(fd)

        def spy_replace(a, b):
            order.append("replace")
            return real_replace(a, b)
        with mock.patch.object(os, "fsync", spy_fsync), mock.patch.object(os, "replace", spy_replace):
            j.update(note="step")
        self.assertEqual(order, ["file-fsync", "replace"])

    def test_R11_the_file_fsync_goes_through_the_full_sync_where_the_platform_has_one(self):
        import fcntl
        if not hasattr(fcntl, "F_FULLFSYNC"):
            self.skipTest("no F_FULLFSYNC on this platform")
        d = Path(tempfile.mkdtemp()).resolve()
        seen = []
        real = fcntl.fcntl

        def spy(fd, cmd, *a):
            seen.append(cmd)
            return real(fd, cmd, *a)
        with mock.patch.object(fcntl, "fcntl", spy):
            zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        self.assertIn(fcntl.F_FULLFSYNC, seen)

    def test_a_failing_file_fsync_leaves_the_previous_journal_untouched(self):
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        before = j.path.read_bytes()
        with mock.patch.object(zp, "_fsync_file", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(zp.JournalError):
                j.write_intent("POST", "/x")
        self.assertEqual(j.path.read_bytes(), before)


# ── R1: the block also matches on the zenodo.json content hash alone ───────────────────────────────

class BlockOnContentHashAlone(unittest.TestCase):
    def test_R1_a_copy_with_another_bib_key_and_title_but_the_same_content_hash_is_blocked(self):
        d = Path(tempfile.mkdtemp())
        j = zp.Journal.create(d, Path("/a/p"), "zenodo.org", False, {}, "1.0", None,
                              {"bib_key": "K1", "title": "T1", "config_sha256": "h" * 64})
        j.update(outcome="UNRECONCILED")
        ident = {"bib_key": "K2", "title": "T2", "config_sha256": "h" * 64}
        found = zp.find_blocking_journals(d, Path("/b/p"), [], "zenodo.org", ident)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1]["matched_by"], ["same zenodo.json content"])
        other = {"bib_key": "K2", "title": "T2", "config_sha256": "g" * 64}
        self.assertEqual(zp.find_blocking_journals(d, Path("/b/p"), [], "zenodo.org", other), [])

    def test_R1_the_hash_rule_in_a_whole_run(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        cfg = json.loads((paper / "zenodo.json").read_text())
        j = zp.Journal.create(state_dir_for(root), Path("/elsewhere/other"), "zenodo.org", False, {}, "1.0", None,
                              {"bib_key": "Different", "title": "Different title",
                               "config_sha256": zp.config_fingerprint(cfg)})
        j.update(outcome="UNRECONCILED")
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory())
        self.assertIn("same zenodo.json content", str(code))


# ── R15: the pin is compared as a whole prefix ─────────────────────────────────────────────────────

class PinIsAFullPrefix(unittest.TestCase):
    def review(self):
        root, paper, (code, out, err) = update_cli(LiveClient(), "--set-title")
        return re.search(r"diff sha256: ([0-9a-f]{64})", out).group(1)

    def attempt(self, pin):
        c = LiveClient()
        root, paper, (code, _, _) = update_cli(c, "--set-title", "--yes-apply-diff", pin, raw_confirm=True)
        return code, c

    def test_R15_a_pin_that_agrees_with_the_digest_on_its_first_four_characters_only_is_refused(self):
        digest = self.review()
        for tail in ("0" * 8, "f" * 8, "a1b2c3d4"):
            pin = digest[:4] + (tail if tail != digest[4:12] else "ffffffff")
            self.assertNotEqual(pin, digest[:12])
            code, c = self.attempt(pin)
            self.assertIn("does not match", str(code), pin)
            self.assertNotIn("edit", c.names())

    def test_R15_a_pin_that_agrees_on_all_but_the_last_character_is_refused(self):
        digest = self.review()
        pin = digest[:11] + ("0" if digest[11] != "0" else "1")
        code, c = self.attempt(pin)
        self.assertIn("does not match", str(code))
        self.assertNotIn("edit", c.names())

    def test_R15_a_pin_of_exactly_eleven_characters_is_refused_even_when_it_is_a_true_prefix(self):
        digest = self.review()
        code, c = self.attempt(digest[:11])
        self.assertIn("at least 12 hex characters", str(code))
        self.assertNotIn("edit", c.names())

    def test_R15_twelve_or_more_characters_in_any_case_are_accepted(self):
        digest = self.review()
        for pin in (digest[:12], digest[:12].upper(), digest[:13].upper(), digest[:40], digest.upper(), "  " + digest[:12] + " "):
            code, c = self.attempt(pin)
            self.assertIsNone(code, (pin, code))
            self.assertIn("publish", c.names())


# ── Nits: git children get a scrubbed environment; docs state the corrected behaviour ────────────────

class GitChildrenEnvironment(unittest.TestCase):
    SECRET_ENV = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN, "PATH": os.environ.get("PATH", ""),
                  "HOME": "/home/x", "GIT_DIR_HINT": "1", "OTHER": "o", "LANG": "C"}

    def test_git_env_keeps_only_what_git_needs_and_never_a_zenodo_variable(self):
        env = zp.git_env(self.SECRET_ENV)
        self.assertEqual(set(env), {"PATH", "HOME", "GIT_DIR_HINT", "LANG"})
        self.assertNotIn(PROD_TOKEN, env.values())
        self.assertNotIn(SBX_TOKEN, env.values())

    def test_every_git_call_of_the_module_passes_the_scrubbed_environment(self):
        seen = []

        def fake_run(cmd, *a, **k):
            seen.append((cmd, k.get("env")))
            return mock.Mock(returncode=0, stdout="https://github.com/Cosmochrony/paper.git\n")
        root, paper = workspace()
        (paper / "code").mkdir()
        with mock.patch.dict(os.environ, self.SECRET_ENV, clear=True), mock.patch.object(zp.subprocess, "run", fake_run):
            zp.get_git_branch(paper)
            zp.git_branch_raw(paper)
            zp.resolve_code_url(paper, {})
        self.assertGreaterEqual(len(seen), 4)
        for cmd, env in seen:
            self.assertEqual(cmd[0], "git")
            self.assertIsNotNone(env, cmd)
            self.assertFalse([k for k in env if k.startswith("ZENODO")], cmd)
            self.assertNotIn("OTHER", env, cmd)


class DocsStateTheCorrectedBehaviour(unittest.TestCase):
    TOOLS = Path(zp.__file__).resolve().parent

    def texts(self):
        return {"README": (self.TOOLS / "README.md").read_text(),
                "docstring": zp.__doc__,
                "help": zp.build_parser().format_help()}

    def test_no_go_and_the_unproven_list_are_kept_everywhere(self):
        for name, text in self.texts().items():
            self.assertIn("PRODUCTION USE REMAINS NO-GO UNTIL A SUCCESSFUL SANDBOX PILOT", text, name)
            self.assertIn("UNPROVEN", text, name)
        readme = self.texts()["README"]
        for item in ("Cleanup semantics", "links.latest_draft", "Header presence", "Redirects", "Journal durability",
                     "The whole-live-metadata PUT"):
            self.assertIn(item, readme)

    def test_no_document_claims_a_token_is_never_printed_or_written(self):
        for name, text in self.texts().items():
            flat = " ".join(text.split())
            self.assertNotIn("No token ever appears", flat, name)
            self.assertNotIn("no token is ever printed", flat.lower(), name)
            self.assertNotIn("A token is never printed", flat, name)
        self.assertIn("masked by value and by field name", " ".join(self.texts()["README"].split()))
        self.assertIn("MASKED, by value and by field name", " ".join(zp.__doc__.split()))

    def test_the_launcher_does_not_claim_the_token_never_reaches_a_child(self):
        launcher = (self.TOOLS / "pilot" / "sandbox_launch.py").read_text()
        pilot = (self.TOOLS / "pilot" / "README-PILOT.md").read_text()
        self.assertNotIn("never a child", launcher)
        self.assertNotIn("not exported to a child process", pilot)
        self.assertNotIn("never exported to a child", pilot)
        self.assertIn("inherit", launcher)

    def test_the_pilot_p0_line_asks_for_a_dummy_token_without_spaces(self):
        pilot = (self.TOOLS / "pilot" / "README-PILOT.md").read_text()
        self.assertNotIn("enter any dummy text", pilot)
        self.assertIn("dummy token WITHOUT spaces", pilot)

    def test_the_new_flags_and_rules_are_in_the_help_and_the_readme(self):
        helptext = " ".join(zp.build_parser().format_help().split())
        readme = " ".join((self.TOOLS / "README.md").read_text().split())
        for needle in ("--replace-related-identifier", "at least 10 characters", "positive integer"):
            self.assertIn(needle, helptext, needle)
        for needle in ("identity_sha256", "paper_dir_real", "fail closed", "--replace-related-identifier",
                       "masked by value and by field name"):
            self.assertIn(needle, readme, needle)
        self.assertIn("absent or UNKNOWN outcome", " ".join(zp.__doc__.split()))


if __name__ == "__main__":
    unittest.main()
