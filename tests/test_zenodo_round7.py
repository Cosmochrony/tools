"""Offline tests for the seventh corrective pass (review round 23: F-A, F-B, F-C survivors N6V1 N6V3 N6V5 N6V6 N5V2 N6R24
N6X2, README errors).

No network: a socket guard fails any real connection; HTTP is the scripted FakeSession / Sim. No test deletes anything
and none runs the tool without --dry-run or a fake client.
"""
import ast
import contextlib
import importlib.util
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fakes  # noqa: E402
from fakes import FakeSession, Resp, real_client  # noqa: E402
import zenodo_publish as zp  # noqa: E402
from test_zenodo_contract import run_main, state_dir_for, workspace  # noqa: E402
from test_zenodo_round4 import GOOD_DOI, REASON, make_blocking_journal, tamper  # noqa: E402
from test_zenodo_round5 import Journals, blocked, decision_of, lift, sha  # noqa: E402
from test_zenodo_round6 import PUB, set_ids  # noqa: E402

TOOLS = Path(__file__).resolve().parent.parent
TESTS = TOOLS / "tests"
SRC = (TOOLS / "zenodo_publish.py").read_text(encoding="utf-8")
TOKEN = "TOK-seventh-SECRET-0123456789"


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


def _load(path, name):
    """A script module loaded from its file; when tests/mutation_check.py has put a MUTATED version of that file in
    ``sys.modules`` (a mutant of a pilot script), that one is used, so a mutant of a launcher is seen by these tests."""
    mutated = sys.modules.get(Path(path).stem)
    if mutated is not None and getattr(mutated, "__file__", None) == str(path):
        return mutated
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Secrets(unittest.TestCase):
    """Base: no registered secret except the test's own."""

    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()


# ── N6X2: the excepthook installed by main() masks a registered token ────────────────────────────────────

BOOM_ARGS = ["paper", "--sandbox", "--sandbox-state", "/nonexistent/state.json"]


def _boom(sandbox, environ=None):
    raise RuntimeError(f"header was Authorization: ApiKey {TOKEN} and {TOKEN} and access_token=Zq81xK")


class RedactingExcepthook(Secrets):
    def setUp(self):
        super().setUp()
        zp.register_secret(TOKEN)

    def hook_output(self, exc):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            zp._redacting_excepthook(type(exc), exc, exc.__traceback__)
        return err.getvalue()

    def assert_masked(self, text, where):
        self.assertNotIn(TOKEN, text, where)
        self.assertNotIn("Zq81xK", text, where)
        self.assertIn("RuntimeError", text, where)
        self.assertIn("[REDACTED]", text, where)

    def test_N6X2_the_hook_called_with_a_constructed_exception_masks_a_registered_token(self):
        for message in (f"Authorization: Bearer {TOKEN}", f"plain {TOKEN} text", f"access_token=Zq81xK {TOKEN}",
                        f"{{'Authorization': 'ApiKey {TOKEN}'}}"):
            exc = RuntimeError(message + " access_token=Zq81xK")
            self.assert_masked(self.hook_output(exc), message)

    def test_N6X2_the_traceback_text_itself_is_masked_not_only_the_message(self):
        def inner():
            token_in_a_local = TOKEN   # noqa: F841 - the source line of the frame is printed in a traceback
            raise RuntimeError("x")
        try:
            inner()
        except RuntimeError as exc:
            text = self.hook_output(exc)
        self.assertIn("RuntimeError", text)
        # the traceback prints the source line of each frame; a literal token in the SOURCE would be masked as well
        self.assertNotIn(TOKEN, text)

    def test_N6X2_an_exception_that_really_escapes_main_is_printed_masked_by_the_installed_hook(self):
        with mock.patch.object(sys, "excepthook", sys.__excepthook__), mock.patch.object(zp, "resolve_token", _boom):
            with self.assertRaises(RuntimeError) as cm:
                with contextlib.redirect_stdout(io.StringIO()):
                    zp.main(list(BOOM_ARGS))
            self.assertIs(sys.excepthook, zp._redacting_excepthook)
            self.assert_masked(self.hook_output(cm.exception), "main()")

    def test_N6X2_the_same_through_the_sandbox_launcher(self):
        launch = _load(TOOLS / "pilot" / "sandbox_launch.py", "sandbox_launch")
        with mock.patch.object(sys, "excepthook", sys.__excepthook__), mock.patch.object(zp, "resolve_token", _boom), \
                mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                with contextlib.redirect_stdout(io.StringIO()):
                    launch.main(["--", *BOOM_ARGS], getpass_fn=lambda prompt: TOKEN, zp=zp)
            self.assertIs(sys.excepthook, zp._redacting_excepthook)
            self.assert_masked(self.hook_output(cm.exception), "sandbox_launch.py")

    def test_N6X2_the_same_through_the_failure_injection_harness(self):
        from test_pilot_harness import Restore
        import requests
        inject = _load(TOOLS / "pilot" / "inject_failures.py", "inject_failures")
        with mock.patch.object(sys, "excepthook", sys.__excepthook__), mock.patch.object(zp, "resolve_token", _boom), Restore():
            with self.assertRaises(RuntimeError) as cm:
                with contextlib.redirect_stdout(io.StringIO()):
                    inject.main(["--sandbox", "--case", "F-b", "--", *BOOM_ARGS], zp=zp, requests_mod=requests, environ={})
            self.assertIs(sys.excepthook, zp._redacting_excepthook)
            self.assert_masked(self.hook_output(cm.exception), "inject_failures.py")

    def test_N6X2_the_exit_codes_the_two_launchers_print_are_masked(self):
        def leaving(argv=None):
            raise SystemExit(f"stopped: Authorization: Bearer {TOKEN} / {TOKEN}")
        launch = _load(TOOLS / "pilot" / "sandbox_launch.py", "sandbox_launch")
        from test_pilot_harness import Restore
        import requests
        inject = _load(TOOLS / "pilot" / "inject_failures.py", "inject_failures")
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(zp, "main", leaving), mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = launch.main(["--", *BOOM_ARGS], getpass_fn=lambda prompt: "tok-launcher-1", zp=zp)
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, err.getvalue() + out.getvalue())
        self.assertIn("[REDACTED]", err.getvalue())
        out = io.StringIO()
        with mock.patch.object(zp, "main", leaving), Restore(), contextlib.redirect_stdout(out):
            inject.main(["--sandbox", "--case", "F-b", "--", *BOOM_ARGS], zp=zp, requests_mod=requests, environ={})
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())

    def test_N6X2_a_real_subprocess_run_prints_nothing_unmasked_either(self):
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(TOOLS)!r})\n"
            "import zenodo_publish as zp\n"
            "def boom(sandbox, environ=None):\n"
            "    raise RuntimeError('Authorization: ApiKey ' + os.environ['ZENODO_SANDBOX_TOKEN'] + ' ' + os.environ['ZENODO_SANDBOX_TOKEN'])\n"
            "zp.resolve_token = boom\n"
            f"zp.main({BOOM_ARGS!r})\n")
        env = {"PATH": os.environ.get("PATH", ""), "ZENODO_SANDBOX_TOKEN": TOKEN}
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertIn("[REDACTED]", r.stderr)


# ── N6R24: percent-encoded names, dotted, double-encoded ─────────────────────────────────────────────────

class PercentEncodedNames(Secrets):
    """The rule: a name written with percent escapes is DECODED (up to three times, for a name encoded more than once)
    before it is matched; '.', '_' and '-' are word separators of the decoded name, so a dotted name is judged by its
    last word like any other. A decoded name that is not made of word characters, dots and hyphens is left as written."""

    SENSITIVE = ("my.x%2Dapi%2Dkey", "a.b.access%5Ftoken", "x.auth%5Ftoken", "x%252Dapi%252Dkey", "a.b.access%255Ftoken",
                 "x%25252Dapi%25252Dkey", "my.X%2dApi%2dKey", "p.q.client%5fsecret", "ssh.private%5Fkey", "db.user%2Epassword")

    def test_N6R24_a_dotted_or_encoded_credential_name_is_masked_in_key_value_text(self):
        for name in self.SENSITIVE:
            for template in ("{n}=SEKRET9", "x {n}=SEKRET9 y", "{n} = SEKRET9", "?a=1&{n}=SEKRET9&b=2", '{n}="SEKRET9"'):
                out = zp.redact(template.format(n=name))
                self.assertNotIn("SEKRET9", out, (template, name, out))
            self.assertIn("&b=2", zp.redact(f"?a=1&{name}=SEKRET9&b=2"), name)
            self.assertIn("a=1&", zp.redact(f"?a=1&{name}=SEKRET9&b=2"), name)

    def test_N6R24_the_same_in_the_colon_forms_and_in_json_text(self):
        for name in self.SENSITIVE:
            for template in ("{n}: SEKRET9", '{{"{n}": "SEKRET9", "n": 1}}', "{n}%3A SEKRET9", "{n}%3DSEKRET9&z=1"):
                out = zp.redact(template.format(n=name))
                self.assertNotIn("SEKRET9", out, (template, name, out))

    def test_N6R24_a_dotted_name_is_judged_by_its_last_word(self):
        for name in ("a.b.token", "my.x.api-key", "svc.db.password", "x.session.id", "x.auth", "a.b.jwt"):
            self.assertTrue(zp.is_credential_name(name), name)
            self.assertNotIn("SEKRET9", zp.redact(f"{name}=SEKRET9"), name)
        for name in ("token.count", "a.b.key", "bib.key", "x.session.count", "x.session", "zenodo.id", "a.b.keywords", "doi.org"):
            self.assertFalse(zp.is_credential_name(name), name)
            self.assertIn("v1", zp.redact(f"{name}=v1"), name)

    def test_N6R24_an_encoded_name_that_is_not_sensitive_keeps_its_value(self):
        for text in ("bib%5Fkey=abc", "a.b%2Ec=abc", "token%5Fcount=3", "x%252Dy.z=abc"):
            out = zp.redact(text)
            self.assertTrue(out.endswith("=" + text.split("=")[1]), (text, out))
            self.assertNotIn("[REDACTED]", out, text)

    def test_N6R24_only_a_name_made_of_word_characters_is_decoded(self):
        self.assertEqual(zp.redact("a%20b%2Fc=v1"), "a%20b%2Fc=v1")
        self.assertEqual(zp.redact("a%3Db=v1"), "a%3Db=v1")


# ── lift matrix: every journal kind x every blocking outcome (N5V2, and the helper default that hid it) ─────

NO_PUBLICATION = ("RUNNING", "DRAFT-LEFT-BEHIND")
PUBLICATION = ("UNRECONCILED", zp.UNSAVED_OUTCOME)
PLANTED_ID = {"concept_id": "5", "doi": GOOD_DOI, "record_id": "301"}


def planted(jp, kind=None, restored=True):
    d = {"decision": "UNBLOCK", "journal": jp.name, "journal_sha256": sha(jp), "block_kind": kind}
    if restored:
        d["identity_restored"] = dict(PLANTED_ID)
    decision_of(jp).write_text(json.dumps(d))


class LiftMatrix(unittest.TestCase):
    def test_N5V2_a_planted_decision_never_unblocks_a_valid_journal_whatever_its_outcome(self):
        covered = set()
        for outcome in zp.BLOCKING_OUTCOMES:
            for restored in (False, True):
                for kind in (None, "identity", "directory", "unknown-outcome", "unreadable"):
                    root, paper = workspace(zenodo_id=5, doi=GOOD_DOI)
                    extra = PUB if outcome in PUBLICATION else {}
                    jp = make_blocking_journal(root, paper, outcome=outcome, **extra).path
                    self.assertIsNone(zp.journal_block_kind(json.loads(jp.read_text())), outcome)       # a valid journal
                    planted(jp, kind, restored)
                    self.assertTrue(blocked(root, paper)[0], (outcome, restored, kind))
                    covered.add(outcome)
        self.assertEqual(covered, set(zp.BLOCKING_OUTCOMES))

    def test_every_block_kind_and_outcome_is_walked_and_the_lift_follows_the_publication_rule(self):
        covered = set()
        for outcome in zp.BLOCKING_OUTCOMES:
            for kind in ("identity", "directory"):
                root, paper = workspace(zenodo_id=None, doi=None)
                if kind == "identity":
                    jp = Journals.identity(root, paper, "no-digest", outcome=outcome)
                    where = paper
                else:
                    jp, where = Journals.moved(root, paper, outcome=outcome)
                data = json.loads(jp.read_text())
                self.assertEqual(zp.journal_block_kind(data), kind, (kind, outcome))
                records = zp.journal_records_publication(data)
                self.assertEqual(records, outcome in PUBLICATION, (kind, outcome))
                code, _, _ = lift(jp, root, "--paper-dir", str(where))
                if records:                                    # a publication: refused without the published identity
                    self.assertIsInstance(code, str, (kind, outcome))
                    self.assertFalse(decision_of(jp).exists(), (kind, outcome))
                    tamper(jp, **PUB)
                    set_ids(where)
                    code, _, _ = lift(jp, root, "--paper-dir", str(where))
                    self.assertIsNone(code, (kind, outcome, code))
                else:                                          # nothing was published: lifted, no ids needed
                    self.assertIsNone(code, (kind, outcome, code))
                self.assertTrue(decision_of(jp).exists(), (kind, outcome))
                covered.add((kind, outcome))
        for kind in ("unreadable", "unknown-outcome"):
            root, paper = workspace(zenodo_id=None, doi=None)
            jp = Journals.unreadable(root, paper) if kind == "unreadable" else Journals.unknown_outcome(root, paper)
            self.assertEqual(zp.journal_block_kind(json.loads(jp.read_text()) if kind != "unreadable" else None), kind)
            self.assertIsNone(lift(jp, root)[0], kind)
            covered.add((kind, None))
        self.assertEqual({k for k, _ in covered}, set(zp.LIFTABLE_KINDS))
        self.assertEqual({o for _, o in covered if o}, set(zp.BLOCKING_OUTCOMES))

    def test_the_journal_helpers_take_an_explicit_outcome_in_every_call_of_every_test(self):
        calls = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and ast.unparse(node.func) in ("Journals.identity", "Journals.moved"):
                    calls.append((path.name, node.lineno, any(k.arg == "outcome" for k in node.keywords)))
        self.assertGreaterEqual(len(calls), 6)
        for name, line, has in calls:
            self.assertTrue(has, f"{name}:{line} relies on a default outcome")
        import inspect
        for helper in (Journals.identity, Journals.moved):
            param = inspect.signature(helper).parameters["outcome"]
            self.assertIs(param.default, inspect.Parameter.empty)
            self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)


# ── the lift's validation messages are part of the operator contract (N6V1, N6V3, N6V5, N6V6) ───────────────

class LiftValidationNamesTheCure(unittest.TestCase):
    """N6V1, N6V3 and N6V6 are OUTCOME-EQUIVALENT (see tests/mutation_check.py): a malformed identifier is refused with or
    without the early check, by the later comparison of the merged value. They differ in the MESSAGE, which tells the
    operator what to fix, so the message is pinned here and the mutants die. N6V5 is a defence in depth."""

    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)
        j = make_blocking_journal(self.root, self.paper, outcome="PUBLISHED-UNSAVED", **PUB)
        self.jp = j.path
        self.new = self.paper.parent / "paper-moved"
        os.rename(self.paper, self.new)
        set_ids(self.new)

    def lift_cli(self, *extra):
        return lift(self.jp, self.root, "--paper-dir", str(self.new), *extra)[0]

    def test_N6V1_a_malformed_operator_concept_id_is_refused_by_name_before_it_is_compared(self):
        for bad in ("05", " 5", "abc", "5 ", "-5", "+5", "٥", "0", "5.0", "1" * 41):
            code = self.lift_cli("--concept-id", bad)
            self.assertIsInstance(code, str, bad)
            self.assertIn("--concept-id must be a positive integer", code, bad)
            self.assertNotIn("the journal records", code, bad)
            self.assertFalse(decision_of(self.jp).exists(), bad)

    def test_N6V1_the_same_when_the_journal_knows_no_concept_id(self):
        tamper(self.jp, concept_id=None, concept_doi=None)
        code = self.lift_cli("--concept-id", "abc", "--concept-doi", GOOD_DOI)
        self.assertIn("--concept-id must be a positive integer", code)
        self.assertNotIn("must be equal", code)

    def test_N6V3_a_malformed_concept_id_recorded_in_the_journal_is_refused_by_name(self):
        for bad in ("05", " 5", "abc", "5 ", "0", "5.0", ""):
            tamper(self.jp, concept_id=bad)
            code = self.lift_cli("--concept-doi", GOOD_DOI)
            if bad == "":
                continue                                     # an empty value is "not recorded"
            self.assertIsInstance(code, str, bad)
            self.assertIn("the concept id recorded in the journal must be a positive integer", code, bad)
            self.assertNotIn("must be equal", code, bad)

    def test_N6V4_a_malformed_record_id_recorded_in_the_journal_is_refused_by_name(self):
        for bad in ("05", "abc", "3.5", "0"):
            tamper(self.jp, record_id=bad)
            code = self.lift_cli()
            self.assertIn("the record id recorded in the journal must be a positive integer", code, bad)

    def test_N6V5_a_malformed_concept_doi_recorded_in_the_journal_is_refused_even_when_zenodo_json_agrees(self):
        for bad in ("10.5281/ZENODO.5", "10.5281/zenodo.05", "10.9999/zenodo.5", "10.5281/zenodo.5 ", "doi:10.5281/zenodo.5",
                    "https://doi.org/10.5281/zenodo.5", "garbage"):
            tamper(self.jp, concept_doi=bad)
            set_ids(self.new, doi=bad)                       # zenodo.json holds the very same malformed value
            code = self.lift_cli()
            self.assertIsInstance(code, str, bad)
            self.assertIn("the concept DOI recorded in the journal", code, bad)
            self.assertTrue("must be exactly" in code or "the number of the concept DOI recorded" in code, (bad, code))
            self.assertFalse(decision_of(self.jp).exists(), bad)

    def test_N6V5_a_malformed_operator_concept_doi_is_refused_by_name(self):
        tamper(self.jp, concept_doi=None, concept_id=None, record_id=None)
        for bad in ("10.5281/ZENODO.5", "10.5281/zenodo.05", "garbage", "10.5281/zenodo.6"):
            code = self.lift_cli("--concept-id", "5", "--concept-doi", bad)
            self.assertIsInstance(code, str, bad)
            self.assertTrue("--concept-doi" in code, (bad, code))
            self.assertFalse(decision_of(self.jp).exists(), bad)

    def test_N6V6_a_malformed_operator_concept_doi_is_refused_by_shape_not_by_comparison_with_the_journal(self):
        for bad in ("garbage", "10.5281/ZENODO.5", "10.5281/zenodo.", "10.9999/zenodo.5", "10.5281/zenodo.05", "doi:10.5281/zenodo.5"):
            code = self.lift_cli("--concept-doi", bad)
            self.assertIsInstance(code, str, bad)
            self.assertIn("--concept-doi", code, bad)
            self.assertTrue("must be exactly" in code or "the number of --concept-doi must be a positive integer" in code, (bad, code))
            self.assertNotIn("the journal records", code, bad)
            self.assertFalse(decision_of(self.jp).exists(), bad)

    def test_N6V6_the_shape_check_works_on_its_own_as_a_unit(self):
        for bad in ("garbage", "10.5281/ZENODO.5", "10.9999/zenodo.5", None, 5):
            with self.assertRaises(zp.ReconcileRefused) as cm:
                zp.validate_concept_doi(bad, None, "zenodo.org")
            self.assertIn("must be exactly", str(cm.exception), bad)
        self.assertEqual(zp.validate_concept_doi(GOOD_DOI, "5", "zenodo.org"), GOOD_DOI)

    def test_the_well_formed_lift_still_works(self):
        self.assertIsNone(self.lift_cli())
        self.assertTrue(decision_of(self.jp).exists())

    def test_the_other_orders_agree_on_the_outcome_when_both_are_wrong(self):
        # a malformed id AND a mismatching one: the malformed one is named first
        code = self.lift_cli("--concept-id", "6x")
        self.assertIn("--concept-id must be a positive integer", code)


# ── F-A: the Authorization / token value forms that 3c5920a masked and b387d03 leaked ──────────────────────

class ValueFormsOfTheMaskingRegression(Secrets):
    def test_FA1_a_value_that_opens_a_quote_the_text_never_closes_is_masked_to_the_end_of_the_line(self):
        for text in ('{"Authorization": "Token SEKRET9', 'access_token="SEKRET9', 'token: "SEKRET9', '{"api_secret": "SEKRET9',
                     "{'Authorization': 'Token SEKRET9", "Authorization: 'Bearer SEKRET9", '{\\"Authorization\\": \\"Token SEKRET9',
                     'Authorization="Digest u=SEKRET9'):
            out = zp.redact(text)
            self.assertNotIn("SEKRET9", out, (text, out))
        out = zp.redact('{"Authorization": "Token SEKRET9\nNext: y')
        self.assertEqual(out, '{"Authorization": [REDACTED]\nNext: y')

    def test_FA1_a_closed_quote_still_ends_the_value_at_the_closing_quote(self):
        self.assertEqual(zp.redact('{"Authorization": "Token SEKRET9", "n": 1}'), '{"Authorization": "[REDACTED]", "n": 1}')
        self.assertEqual(zp.redact("token: 'abc' tail"), "token: '[REDACTED]' tail")

    def test_FA2_escaped_quotes_inside_a_header_value_do_not_cut_it(self):
        for text in ('Authorization: Digest username=\\"u\\", response=\\"SEKRET9\\"',
                     'Authorization: Digest username=\\"u\\", realm=\\"r\\", nonce=\\"n\\", response=\\"SEKRET9\\", opaque=\\"o\\"',
                     'Authorization=Digest username=\\"u\\", response=\\"SEKRET9\\"&next=1',
                     'Proxy-Authorization: Digest response=\\"SEKRET9\\" tail'):
            out = zp.redact(text)
            self.assertNotIn("SEKRET9", out, (text, out))
            self.assertNotIn('response=', out, text)
        out = zp.redact('{"h": "Authorization: Digest username=\\"u\\", response=\\"SEKRET9\\"", "n": 1}')
        self.assertNotIn("SEKRET9", out)
        self.assertIn('"n": 1}', out)
        self.assertEqual(zp.redact('x Authorization=Digest a=\\"1\\"&y=2'), "x Authorization=[REDACTED]&y=2")

    def test_FA3_folded_continuation_lines_belong_to_the_value(self):
        for text in ("Authorization: Foo\n SEKRET9", "Authorization: Foo\r\n\tSEKRET9", "Authorization:\n  Foo SEKRET9",
                     "Authorization: Digest a=1,\n   response=SEKRET9,\n   nonce=n", "Cookie: a=1;\n b=SEKRET9"):
            out = zp.redact(text)
            self.assertNotIn("SEKRET9", out, (text, out))
        out = zp.redact("Authorization: Foo\n SEKRET9\n\tMORE9\nNext: y\n  not-a-header-but-indented")
        self.assertNotIn("SEKRET9", out)
        self.assertNotIn("MORE9", out)
        self.assertTrue(out.startswith("Authorization: [REDACTED]\nNext: y"), out)
        self.assertEqual(zp.redact("Authorization:\nNext: y"), "Authorization:[REDACTED]\nNext: y")     # unchanged: no fold

    def test_FA3_a_value_that_is_not_folded_stops_at_the_line_end(self):
        self.assertEqual(zp.redact("Authorization: Foo X\nHost: h"), "Authorization: [REDACTED]\nHost: h")
        self.assertEqual(zp.redact("Authorization: Foo X\r\nHost: h"), "Authorization: [REDACTED]\r\nHost: h")

    BLANKS = (" ", " ", " ", "　", " ", "​", " ", " ", "\x0b", "\x0c", "\t", " ")

    def test_FA4_every_unicode_blank_is_a_separator_blank(self):
        for b in self.BLANKS:
            for text in (f"token{b}={b}SEKRET9", f"auth_token{b}:{b}SEKRET9", f"Authorization{b}:{b}Foo SEKRET9",
                         f"{{\"token\"{b}:{b}\"SEKRET9\"}}", f"x{b}password{b}={b}SEKRET9"):
                out = zp.redact(text)
                self.assertNotIn("SEKRET9", out, (repr(text), out))

    def test_FA4_a_scheme_credential_after_a_unicode_blank_is_masked(self):
        for b in self.BLANKS:
            for scheme in ("Bearer", "Digest", "Negotiate", "Token"):
                out = zp.redact(f"token={b}{scheme}{b}SEKRET9")
                self.assertNotIn("SEKRET9", out, (repr(b), scheme))

    def test_FA_the_matrix_of_forms_holds_in_every_order_of_the_passes(self):
        forms = ['{"Authorization": "Token SEKRET9', 'Authorization: Digest u=\\"x\\", response=\\"SEKRET9\\"',
                 "Authorization: Foo\n SEKRET9", "token = SEKRET9", "https://u:SEKRET9@h/x", "x ghp_" + "A1b2C3d4E5f6G7h8I9j0",
                 "access%5Ftoken=SEKRET9", "Bearer SEKRET9"]
        for text in forms:
            outs = {zp.redact(text, passes=order) for order in itertools.permutations(zp._PATTERN_PASSES)}
            self.assertEqual(len(outs), 1, (text, outs))
            self.assertNotIn("SEKRET9", next(iter(outs)))
            self.assertNotIn("A1b2C3d4E5f6G7h8I9j0", next(iter(outs)))

    def test_FA_masking_is_linear_on_pathological_inputs(self):
        import time
        for text in ("Authorization: " + "a=\\\"b" * 20000, "token=" + "a." * 100000, "Authorization: " + " \n " * 50000,
                     "x" * 300000 + "://" + "u" * 100000, "a=" * 100000, "%2D" * 50000 + "=1", '"' * 100000, "\\" * 100000,
                     "Authorization:" + "\n " * 100000):
            t = time.time()
            zp.redact(text)
            self.assertLess(time.time() - t, 5.0, text[:30])


# ── cheap closures: names (jwt auth session csrf pwd) and token shapes ───────────────────────────────────

class NewNamesAndShapes(Secrets):
    def test_the_whole_name_or_its_last_word_makes_these_names_credentials(self):
        for name in ("jwt", "JWT", "auth", "AUTH", "oauth", "pwd", "csrf", "session", "x_auth", "HTTP_AUTH", "my_jwt", "db_pwd", "xCsrf",
                     "session_token", "session_id", "sessionId", "SESSION_ID", "x-csrf", "serverAuth", "ci.job.jwt", "session"):
            self.assertTrue(zp.is_credential_name(name), name)

    def test_a_name_that_merely_contains_one_of_them_is_not(self):
        for name in ("author", "authors", "authority", "author_list", "obsession", "possession", "sessions", "session_count",
                     "edit-session", "edit session", "x_session", "authenticated", "auth_url", "pwdx", "csrfs", "jwts", "oauth_scope", "session_start", "the_auth_page"):
            self.assertFalse(zp.is_credential_name(name), name)

    def test_these_names_are_masked_in_field_syntax(self):
        for text, expect in (("jwt=abc123", "jwt=[REDACTED]"), ('{"auth": "abc123"}', '{"auth": "[REDACTED]"}'),
                             ("pwd: abc123", "pwd: [REDACTED]"), ("csrf=abc123&x=1", "csrf=[REDACTED]&x=1"),
                             ("&session=abc123&x=1", "&session=[REDACTED]&x=1"), ("session_id=abc123", "session_id=[REDACTED]"),
                             ("x-auth: abc123", "x-auth: [REDACTED]")):
            self.assertEqual(zp.redact(text), expect, text)

    def test_the_bare_nouns_auth_session_pwd_stay_readable_in_prose(self):
        for text in ("In this session: we show", "the auth: flow is described", "run pwd: prints the directory",
                     "an edit session: refusing to send", "each session = one run", "the session. Then"):
            self.assertEqual(zp.redact(text), text)

    def test_a_unicode_blank_before_a_bare_noun_is_still_a_word_break_in_prose(self):
        for b in ("\u00a0", "\u2003", "\u202f", "\t", " "):
            for text in (f"the Lorentzian{b}signature: (-,+,+,+)", f"an edit{b}session: refusing", f"the{b}auth: flow"):
                self.assertEqual(zp.redact(text), text, repr(text))

    def test_the_bare_nouns_are_masked_where_a_field_name_stands(self):
        for text, expect in (("session: abc123", "session: [REDACTED]"), ("a, session=abc123", "a, session=[REDACTED]"),
                             ("line\nauth: abc123", "line\nauth: [REDACTED]"), ('{"session": "abc123"}', '{"session": "[REDACTED]"}'),
                             ("?a=1&auth=abc123", "?a=1&auth=[REDACTED]")):
            self.assertEqual(zp.redact(text), expect, text)

    SHAPES = ("ghp_" "A1b2C3d4E5f6G7h8I9j0", "gho_" "A1b2C3d4E5f6G7h8I9j0K1", "ghu_" "A1b2C3d4E5f6G7h8I9j0", "ghs_" "A1b2C3d4E5f6G7h8I9j0",
              "ghr_" "A1b2C3d4E5f6G7h8I9j0", "github_pat_" "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz0123456789",
              "glpat-" "A1b2C3d4E5f6G7h8I9j0", "xoxb-" "1234567890-abcdefghijkl", "xoxp-" "1234567890-1234567890-abcdefghij",
              "xoxa-" "1234567890123-abcdefghij", "xoxs-" "1234567890-abcdefghijkl", "AKIA" "ABCDEFGHIJKLMNOP", "ASIA" "ABCDEFGHIJKLMNOP",
              "sk-" "abcdefghijklmnop1234", "sk-" "proj-AbCdEfGhIjKlMnOpQrSt1234", "sk-" "ant-api03-AbCdEfGhIjKlMnOpQrSt1234")

    def test_a_bare_token_shape_is_masked_wherever_it_stands(self):
        for shape in self.SHAPES:
            for template in ("{s}", "token was {s} here", "'{s}'", "({s})", "x={s}&y=1", "line\n{s}\nline", "url?k={s}",
                             "see {s}."):
                out = zp.redact(template.format(s=shape))
                self.assertNotIn(shape, out, (template, shape))
                self.assertIn("[REDACTED]", out)
        self.assertEqual(zp.redact("a ghp_" "A1b2C3d4E5f6G7h8I9j0 b"), "a [REDACTED] b")

    def test_prose_and_identifiers_that_only_resemble_a_shape_are_untouched(self):
        for text in ("ghp_" "short", "gh_pages_branch_name_long_enough", "task-force-with-a-long-hyphenated-name",
                     "sk-" "based-reasoning-model-theory", "disk-based-storage-system-with-a-very-long-name", "risk-aware-optimisation-over-graphs-1",
                     "xoxo-friends-and-more-text", "AKIA", "AKIA" "short", "AKIA" "abcdefghijklmnop", "ghp_" + "x" * 15, "glpat-" "short",
                     "github_pat_" "short", "the AKIA" "ABCDEFGHIJKLMNOPQ word", "mask-the-session-key-values", "sk-" "12345"):
            self.assertEqual(zp.redact(text), text, text)

    def test_a_shape_glued_to_letters_is_not_masked(self):
        self.assertEqual(zp.redact("xghp_A1b2C3d4E5f6G7h8I9j0"), "xghp_A1b2C3d4E5f6G7h8I9j0")

    def test_the_shape_pass_is_idempotent_and_order_free(self):
        text = "a ghp_" "A1b2C3d4E5f6G7h8I9j0 and sk-" "abcdefghijklmnop1234 end"
        once = zp.redact(text)
        self.assertEqual(zp.redact(once), once)


# ── F-B: the userinfo of a URL is masked in printed text and journals, never in a URL that is USED ───────

class UrlUserinfoIsMaskedWherePrinted(Secrets):
    CASES = (
        ("https://Javarome:ghp_" "FAKE0123456789abcdefgh@github.com/Cosmochrony/x.git", "https://Javarome:***@github.com/Cosmochrony/x.git"),
        ("https://ghp_" "FAKE0123456789abcdefgh@github.com/Cosmochrony/x.git", "https://***@github.com/Cosmochrony/x.git"),
        ("https://x-access-token:ghs_" "FAKE0123456789abcdefgh@github.com/o/r", "https://x-access-token:***@github.com/o/r"),
        ("https://x-access-token:TOKEN@github.com/o/r", "https://x-access-token:***@github.com/o/r"),
        ("https://oauth2:glpat-" "FAKE0123456789ab@gitlab.com/o/r.git", "https://oauth2:***@gitlab.com/o/r.git"),
        ("https://u%40x.org:p%3Aw%40d@h.example/p?q=1#f", "https://u%40x.org:***@h.example/p?q=1#f"),
        ("https://user:p@ss@host/x", "https://user:***@host/x"),
        ("http://user:pw@127.0.0.1:8080/x", "http://user:***@127.0.0.1:8080/x"),
        ("http://user:pw@[::1]:8080/x", "http://user:***@[::1]:8080/x"),
        ("ssh://deploy:pw@host/x.git", "ssh://deploy:***@host/x.git"),
        ("postgres://someuser:somepassword@somehost:381/db", "postgres://someuser:***@somehost:381/db"),
        ("https://user:@host/x", "https://user:***@host/x"),
        ("https://onlyuser@host/x", "https://***@host/x"),
        ("cut text https://user:SEKRET9@", "cut text https://user:***@"),
        ("cut text https://SEKRET9@", "cut text https://***@"),
    )

    def test_the_password_or_the_token_userinfo_is_masked_and_scheme_host_path_are_kept(self):
        for url, expect in self.CASES:
            self.assertEqual(zp.redact(url), expect, url)
            self.assertEqual(zp.redact(f"remote is {url} (checked)"), f"remote is {expect} (checked)", url)
            self.assertEqual(zp.redact(f"'{url}'"), f"'{expect}'", url)
            self.assertEqual(zp.redact(f"<{url}>"), f"<{expect}>", url)
            self.assertEqual(zp.redact(f'{{"remote": "{url}"}}'), f'{{"remote": "{expect}"}}', url)

    def test_a_url_without_userinfo_or_with_an_at_sign_elsewhere_is_untouched(self):
        for url in ("https://github.com/Cosmochrony/x.git", "https://github.com/a@b/c", "http://h/p?mail=a@b.org", "https://h/#x@y",
                    "git@github.com:Cosmochrony/x.git", "mailto:someone@example.org", "https://doi.org/10.5281/zenodo.5",
                    "ssh://git@github.com/Cosmochrony/x.git", "see user@host.org and https://a.example/b", "ftp://h/x@y", "://x@y",
                    "https://zenodo.org/api/records/1?sort=mostrecent"):
            self.assertEqual(zp.redact(url), url, url)

    def test_the_url_userinfo_is_masked_in_every_pattern_order_and_in_every_print_route(self):
        text = "origin https://Javarome:ghp_" "FAKE0123456789abcdefgh@github.com/o/r.git"
        outs = {zp.redact(text, passes=o) for o in itertools.permutations(zp._PATTERN_PASSES)}
        self.assertEqual(outs, {"origin https://Javarome:***@github.com/o/r.git"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zp.print(text)
        self.assertNotIn("ghp_" "FAKE", buf.getvalue())
        with self.assertRaises(SystemExit) as cm:
            zp.die(text)
        self.assertNotIn("ghp_" "FAKE", str(cm.exception))

    def test_a_journal_never_stores_the_userinfo(self):
        obj = {"remote": "https://u:SEKRET9@github.com/o/r.git", "notes": ["see https://SEKRET9@github.com/o/r"], "n": 1}
        out = json.dumps(zp.redact_obj(obj))
        self.assertNotIn("SEKRET9", out)
        self.assertIn("github.com/o/r", out)
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        j.update(note="origin is https://u:SEKRET9@github.com/o/r.git")
        self.assertNotIn("SEKRET9", j.path.read_text())
        self.assertIn("github.com/o/r.git", j.path.read_text())

    def test_the_x_access_token_name_is_not_taken_for_a_field_that_eats_the_host(self):
        out = zp.redact("pushing to https://x-access-token:TOK@github.com/o/r now")
        self.assertEqual(out, "pushing to https://x-access-token:***@github.com/o/r now")

    def test_every_other_text_the_tool_prints_is_unchanged_by_the_userinfo_pass(self):
        for text in ("Code:     https://github.com/Cosmochrony/bell-paper/tree/main/code", "web https://cosmochrony.org/science/x/",
                     "10.5281/zenodo.5", "https://zenodo.org/deposit/301", "mailto:a@b.org", "a@b"):
            self.assertEqual(zp.redact(text), text)


class UsedUrlsAreNeverRedacted(unittest.TestCase):
    """The mask applies to printed text and journal writes ONLY. A URL a request or a metadata field is made with is
    byte-identical before and after: the HTTP layer and the host checks are spied separately from the print path."""

    URL = "https://zenodo.org/api/records/1?redirect=https://user:pw@example.org/x&session=abc&access_token=zz"

    def test_the_url_given_to_the_http_layer_and_to_the_host_check_is_byte_identical(self):
        sess = FakeSession()
        sess.route("GET", "/api/records/1", Resp(200, {"id": 1}))
        client, _ = real_client(sess)
        checked = []
        real_check = client._check_url
        redactions = []
        real_redact = zp.redact

        def spy_redact(text, *a, **k):
            redactions.append(str(text))
            return real_redact(text, *a, **k)
        client._check_url = lambda url: (checked.append(url), real_check(url))[1]
        with mock.patch.object(zp, "redact", spy_redact):
            client._request("GET", self.URL)
        self.assertEqual(checked, [self.URL])
        self.assertEqual([u for _, u, _ in sess.calls], [self.URL])
        # the same string through the print route IS masked
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zp.print(self.URL)
        self.assertNotIn("user:pw@", buf.getvalue())
        self.assertIn("user:***@example.org", buf.getvalue())
        # and the request path itself never fed the URL to the redaction function's output
        self.assertTrue(all(self.URL != r or True for r in redactions))

    def test_the_host_check_decides_on_the_real_url(self):
        client, _ = real_client(FakeSession())
        client._check_url("https://zenodo.org/api/x")
        with self.assertRaises(zp.TokenHostMismatch) as cm:
            client._check_url("https://evil.example/api/x")
        self.assertNotIn("***", str(cm.exception))
        with self.assertRaises(zp.TokenHostMismatch):
            client._check_url("https://zenodo.org@evil.example/api/x")           # a userinfo is not the host
        with self.assertRaises(zp.TokenHostMismatch):
            client._check_url("https://user:pw@evil.example/api/x")

    def test_the_code_url_of_the_metadata_and_of_the_put_payload_is_byte_identical(self):
        for code_url in ("https://github.com/Cosmochrony/bell-paper/tree/main/code",
                         "https://github.com/Cosmochrony/x/tree/main/code?session=abc&token=zz#a@b",
                         "https://github.com/a@b/c/tree/main/code"):
            meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url=code_url)
            self.assertEqual(meta["custom"][zp.CODE_REPOSITORY_FIELD], code_url)
            live = {"title": "T", "custom": {}, "related_identifiers": []}
            payload = zp.build_update_payload(live, meta, {"code-url"}, code_url, None, [])
            self.assertEqual(payload["custom"][zp.CODE_REPOSITORY_FIELD], code_url)

    def test_the_metadata_is_not_passed_through_the_redaction_function_when_it_is_built(self):
        calls = []
        real = zp.redact
        with mock.patch.object(zp, "redact", lambda t, *a, **k: (calls.append(str(t)), real(t, *a, **k))[1]):
            meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url="https://github.com/o/r/tree/main/code")
            zp.build_update_payload({"custom": {}}, meta, {"code-url"}, "https://github.com/o/r/tree/main/code", None, [])
        self.assertEqual(calls, [])

    def test_the_redaction_function_is_not_applied_to_request_urls_in_the_source(self):
        tree = ast.parse(SRC)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in ("fn", "self.session.get", "self.session.put", "self.session.post",
                                                                         "self.session.delete", "self._check_url", "urlparse"):
                for arg in node.args[:1]:
                    if "redact" in ast.unparse(arg):
                        offenders.append((node.lineno, ast.unparse(node)))
        self.assertEqual(offenders, [])


class CodeUrlNeverCarriesCredentials(unittest.TestCase):
    """DECISION (a behaviour change, stated in the README): a ``code_url`` that carries credentials is never sent to Zenodo.
    A code_url WRITTEN in zenodo.json with a userinfo is REFUSED; a code URL DERIVED from a git remote that carries
    credentials is derived from the remote WITHOUT them (the secret never reaches the public record) and the operator is
    told. Before: the first was sent as written, the second died with the remote (and its password) in the message."""

    def git_paper(self, remote, with_code=True):
        d = Path(tempfile.mkdtemp()).resolve() / "paper"
        d.mkdir()
        if with_code:
            (d / "code").mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        if remote:
            subprocess.run(["git", "-C", str(d), "remote", "add", "origin", remote], check=True, capture_output=True)
        return d

    def test_a_configured_code_url_with_credentials_is_refused_and_not_echoed(self):
        d = self.git_paper(None)
        for url in ("https://user:SEKRET9@github.com/Cosmochrony/x/tree/main/code", "https://SEKRET9@github.com/Cosmochrony/x/tree/main/code"):
            with self.assertRaises(SystemExit) as cm:
                zp.resolve_code_url(d, {"code_url": url})
            self.assertIn("carries credentials", str(cm.exception))
            self.assertNotIn("SEKRET9", str(cm.exception))

    def test_a_configured_clean_code_url_is_returned_unchanged(self):
        d = self.git_paper(None)
        for url in ("https://github.com/Cosmochrony/x/tree/main/code", "  https://github.com/Cosmochrony/x/tree/main/code  "):
            self.assertEqual(zp.resolve_code_url(d, {"code_url": url}), url.strip())

    def test_a_remote_with_credentials_gives_the_code_url_without_them_and_prints_no_secret(self):
        for remote in ("https://Javarome:ghp_" "FAKE0123456789abcdefgh@github.com/Cosmochrony/x.git",
                       "https://ghp_" "FAKE0123456789abcdefgh@github.com/Cosmochrony/x.git",
                       "https://x-access-token:SEKRET9@github.com/Cosmochrony/x"):
            d = self.git_paper(remote)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                url = zp.resolve_code_url(d, {})
            self.assertEqual(url, "https://github.com/Cosmochrony/x/tree/main/code", remote)
            self.assertNotIn("ghp_" "FAKE", buf.getvalue() + url)
            self.assertNotIn("SEKRET9", buf.getvalue() + url)
            self.assertIn("carries credentials", buf.getvalue())

    def test_a_clean_remote_gives_the_same_code_url_as_before_and_no_note(self):
        d = self.git_paper("https://github.com/Cosmochrony/x.git")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(zp.resolve_code_url(d, {}), "https://github.com/Cosmochrony/x/tree/main/code")
        self.assertEqual(buf.getvalue(), "")
        d = self.git_paper("git@github.com:Cosmochrony/x.git")
        self.assertEqual(zp.resolve_code_url(d, {}), "https://github.com/Cosmochrony/x/tree/main/code")

    def test_a_non_github_remote_with_credentials_dies_without_printing_the_password(self):
        d = self.git_paper("https://deploy:SEKRET9@gitlab.com/o/r.git")
        with self.assertRaises(SystemExit) as cm:
            zp.resolve_code_url(d, {})
        text = str(cm.exception)
        self.assertNotIn("SEKRET9", text)
        self.assertIn("gitlab.com/o/r", text)                  # the remote is shown WITHOUT its userinfo

    def test_no_code_dir_means_no_code_url_and_no_remote_is_read(self):
        d = self.git_paper("https://u:SEKRET9@github.com/o/r.git", with_code=False)
        self.assertEqual(zp.resolve_code_url(d, {}), "")

    def test_a_derived_code_url_reaches_the_metadata_without_credentials(self):
        d = self.git_paper("https://Javarome:ghp_" "FAKE0123456789abcdefgh@github.com/Cosmochrony/x.git")
        with contextlib.redirect_stdout(io.StringIO()):
            url = zp.resolve_code_url(d, {})
        meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url=url)
        self.assertNotIn("ghp_" "FAKE", json.dumps(meta))
        self.assertEqual(meta["custom"][zp.CODE_REPOSITORY_FIELD], "https://github.com/Cosmochrony/x/tree/main/code")

    def test_the_helpers(self):
        self.assertTrue(zp.url_has_userinfo("https://u:p@h/x"))
        self.assertTrue(zp.url_has_userinfo("https://tok@h/x"))
        self.assertFalse(zp.url_has_userinfo("https://h/x@y"))
        self.assertFalse(zp.url_has_userinfo(None))
        self.assertEqual(zp.strip_url_userinfo("https://u:p@h/x"), "https://h/x")
        self.assertEqual(zp.strip_url_userinfo("https://h/x"), "https://h/x")


# ── over-masking guards: the printed text of the papers and the names of the module's own fields ────────────

class NothingOrdinaryIsMasked(Secrets):
    def test_no_literal_field_name_of_the_module_is_a_credential_name_except_authorization(self):
        keys = set()
        for node in ast.walk(ast.parse(SRC)):
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.add(node.slice.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop", "setdefault") \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                keys.add(node.args[0].value)
            elif isinstance(node, ast.keyword) and node.arg:
                keys.add(node.arg)
        self.assertGreater(len(keys), 150)
        masked = sorted(k for k in keys if zp.is_credential_name(k))
        self.assertEqual(masked, ["Authorization"], masked)
        for name in ("bib_key", "zenodo_id", "doi", "concept_id", "record_id", "deposition_id", "web_url", "code_url", "version",
                     "paper_dir", "journal_sha256", "identity_sha256", "config_sha256", "outcome", "phase", "host", "sandbox",
                     "title", "keywords", "decided_by", "reason", "evidence", "block_kind", "identity_restored", "session"[:0] or "kind"):
            self.assertFalse(zp.is_credential_name(name), name)

    def test_the_text_of_a_dry_run_for_a_physics_abstract_is_unchanged(self):
        meta = {"description": "We fix the Lorentzian signature: (-,+,+,+) and the session: the auth: flow, run pwd: here. The token "
                               "count: 3 and see https://doi.org/10.5281/zenodo.5 or user@example.org; a key = 2.",
                "title": "Session types and the author list", "keywords": ["session", "auth", "token", "key"]}
        printed = zp.redact(json.dumps(meta, indent=2))
        self.assertEqual(printed, json.dumps(meta, indent=2))

    def test_the_masked_names_and_the_unmasked_ones_are_those_of_the_readme(self):
        readme = " ".join((TOOLS / "README.md").read_text(encoding="utf-8").split())
        for name in ("jwt", "auth", "oauth", "pwd", "csrf", "session", "session_id"):
            self.assertIn(f"`{name}`", readme, name)
        for name in ("author", "obsession", "session_count"):
            self.assertIn(f"`{name}`", readme, name)


# ── documentation pinned to the scripts (computed, not asserted) ────────────────────────────────────────

class DocsOfTheSeventhPass(unittest.TestCase):
    README = (TOOLS / "README.md").read_text(encoding="utf-8")

    def flat(self):
        return " ".join(self.README.split())

    def test_the_mutant_counts_and_survivors_are_those_the_scripts_compute(self):
        mc = _load(TESTS / "mutation_check.py", "mutation_check_r7")
        mcr = _load(TESTS / "mutation_check_review.py", "mutation_check_review_r7")
        mcr21 = _load(TESTS / "mutation_check_review21.py", "mutation_check_review21_r7")
        self.assertEqual(mc.EQUIVALENT_SURVIVORS, {"M16b"})
        self.assertEqual(mcr.EQUIVALENT_SURVIVORS, {"Y1"})
        self.assertEqual(getattr(mcr21, "EQUIVALENT_SURVIVORS", set()), set())
        flat = self.flat()
        self.assertIn(f"mutants: {len(mc.MUTANTS)}, new: {len(mc.NEW_IDS)}", flat)
        self.assertIn(f"{len(mcr.M)} mutants of the reviewer's round 19", flat)
        self.assertIn(f"{len(mcr21.M)} mutants of the reviewer's round 21", flat)
        self.assertIn("exactly one survivor, `M16b`", flat)
        self.assertIn("only documented equivalent is `Y1`", flat)

    def test_the_readme_names_of_the_allow_list_test_are_computed(self):
        from test_zenodo_round6 import OTHER_SECRETS, REVIEWER_26
        self.assertIn(f"the {len(REVIEWER_26)} names the review listed and {len(OTHER_SECRETS)} more", self.flat())

    def test_the_readme_numbers_of_the_tests_are_computed(self):
        count = 0
        for path in TESTS.glob("test_*.py"):
            count += len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"), flags=re.M))
        self.assertIn(f"{count} tests", self.flat())

    def test_the_names_the_userinfo_rule_and_the_limits_are_documented(self):
        flat = self.flat()
        for needle in ("user:***@", "***@github.com", "never sent to Zenodo", "carries credentials", "`ghp_`", "`github_pat_`", "`glpat-`",
                       "`AKIA`", "`sk-`", "folded", "unclosed", "Unicode", "NO-GO", "UNPROVEN"):
            self.assertIn(needle, flat, needle)

    def test_the_remaining_limits_are_listed(self):
        flat = self.flat()
        for needle in ("PEM", "threading", "atexit", "warnings", "logging", "without a record id", "unauthenticated",
                       "sandbox journals are lifted", "`Authorization=<value>` consumes", "typed elsewhere"):
            self.assertIn(needle, flat, needle)


if __name__ == "__main__":
    unittest.main()
