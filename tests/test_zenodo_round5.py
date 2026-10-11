"""Offline tests for the fifth corrective pass (review round 21: N-1 .. N-11 and the survivors Q6, Q38).

Every class names the finding it closes. The reviewer's probes of round 21 (DOI B03/B05/B07/B08/F01, redaction
C02-C05/C08-C11/C14/C20, related identifiers D06/D12, environment E05, write failures A27/A28, quarantine
A15-A20/A26) are converted into permanent tests; the quarantine ones now test the replacement
(--backup-journal / --lift-block, a decision file that never touches the journal).

No network: a socket guard fails any real connection; HTTP is the scripted Sim / FakeSession / FakeClient.
"""
import ast
import copy
import hashlib
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
from fakes import PROD_TOKEN, SBX_TOKEN, Sim  # noqa: E402
import predeposit_check as pc  # noqa: E402
import zenodo_publish as zp  # noqa: E402
from test_zenodo_contract import Factory, run_main, state_dir_for, workspace  # noqa: E402
from test_zenodo_round3 import LIVE, LiveClient, update_cli  # noqa: E402
from test_zenodo_round4 import (Bytes, GOOD_DOI, PROD_ONLY, REASON, ENTRY, make_blocking_journal,  # noqa: E402
                                published_args, reconcile, tamper)

TOOLS = Path(__file__).resolve().parent.parent
SRC = (TOOLS / "zenodo_publish.py").read_text(encoding="utf-8")
SECRET = "SECRETX9value"          # 13 characters


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


def sha(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    return hashlib.sha256(data).hexdigest()


def blocked(root, paper):
    f = Factory()
    code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, f)
    return "REFUSING TO START" in str(code), str(code)


def lift(jp, root, *extra, reason=REASON, evidence=("--i-checked-the-account",)):
    return run_main(["--lift-block", str(jp), *evidence, "--reason", reason, *extra], {}, root)


def decision_of(jp):
    return Path(str(jp) + ".decision.json")


# ── N-1 / Q6 / Q38: the DOI is exactly 10.5281/zenodo.<concept id>, in ASCII ─────────────────────────────

class ConceptDoiIsExact(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)
        self.jp = make_blocking_journal(self.root, self.paper).path
        self.snap = Bytes(self.paper / "zenodo.json", self.jp)

    def refused(self, doi, cid="5", expect=None):
        code, out, err = reconcile(self.jp, "--outcome", "published", "--record-id", "301", "--concept-id", cid,
                                   "--concept-doi", doi, root=self.root)
        self.assertIsInstance(code, str, doi)
        self.assertNotIn("Traceback", out + err)
        self.assertTrue(self.snap.unchanged(), doi)
        if expect:
            self.assertIn(expect, code, doi)

    def test_B03_unicode_digits_are_refused_in_the_doi_number_and_in_the_registrant(self):
        for bad in ("10.5281/zenodo.\u0665", "10.5281/zenodo.5\u0665", "10.\u06655281/zenodo.5", "10.5281/zenodo.\uff15",
                    "10.5281/zenodo.\u0665".replace("\u0665", "\u0035\u200b")):
            self.refused(bad)

    def test_B05_case_and_leading_zero_variants_are_refused(self):
        for bad in ("10.5281/ZENODO.5", "10.5281/Zenodo.5", "10.5281/zenodo.05", "10.5281/zenodo.005", "10.5281/zenodo.+5",
                    "10.5281/zenodo.5.0", "10.5281/zenodo.0x5", "10.5281/zenodo."):
            self.refused(bad)

    def test_B07_lookalike_letters_and_zero_width_characters_are_refused(self):
        for bad in ("10.5281/z\u0435nodo.5", "10.5281/zenodo.5\u200b", "10.5281/zenodo.\u200b5", "10.5281/\u200bzenodo.5",
                    "10.5281/zenodo.5\u00a0", "\u00a010.5281/zenodo.5"):
            self.refused(bad)

    def test_whitespace_variants_are_refused(self):
        for bad in (" 10.5281/zenodo.5", "10.5281/zenodo.5 ", "10.5281/zenodo.5\n", "10.5281/zenodo.5\t", "10.5281/ zenodo.5",
                    "10.5281/zenodo. 5", "10.5281 /zenodo.5"):
            self.refused(bad)

    def test_B08_another_registrant_is_refused_for_a_zenodo_concept(self):
        for bad in ("10.9999/zzz", "10.1234/zenodo.5", "10.5072/zenodo.5", "10.5281/other.5", "10.5281/5",
                    "https://doi.org/10.5281/zenodo.5", "doi:10.5281/zenodo.5", "10.5282/zenodo.5", "10.528/zenodo.5"):
            self.refused(bad, expect="must be exactly")

    def test_the_number_must_equal_the_concept_id_not_start_with_it(self):
        self.refused("10.5281/zenodo.50", cid="5", expect="must be equal")
        self.refused("10.5281/zenodo.5", cid="50", expect="must be equal")
        self.refused("10.5281/zenodo.6", cid="5", expect="must be equal")
        self.refused("10.5281/zenodo.15", cid="5", expect="must be equal")

    def test_the_exact_form_is_accepted_and_written(self):
        code, _, _ = reconcile(self.jp, *published_args(doi="10.5281/zenodo.5"), root=self.root)
        self.assertIsNone(code, code)
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, "10.5281/zenodo.5"))

    def test_the_sandbox_registrant_is_accepted_only_for_a_sandbox_journal(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        sb = make_blocking_journal(root, paper, host="sandbox.zenodo.org", sandbox=True).path
        code, _, _ = reconcile(sb, *published_args(doi="10.5072/zenodo.5"), root=root)
        self.assertIsNone(code, code)
        root2, paper2 = workspace(zenodo_id=None, doi=None)
        sb2 = make_blocking_journal(root2, paper2, host="sandbox.zenodo.org", sandbox=True).path
        code, _, _ = reconcile(sb2, *published_args(doi="10.5281/zenodo.5"), root=root2)
        self.assertIn("must be exactly 10.5072", str(code))
        code, _, _ = reconcile(self.jp, *published_args(doi="10.5072/zenodo.5"), root=self.root)
        self.assertIn("must be exactly 10.5281", str(code))
        self.assertTrue(self.snap.unchanged())

    def test_F01_a_different_doi_already_saved_is_refused_with_both_values(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["doi"] = "10.5281/zenodo.7"
        (self.paper / "zenodo.json").write_text(json.dumps(cfg, indent=2) + "\n")
        snap = Bytes(self.paper / "zenodo.json", self.jp)
        code, _, _ = reconcile(self.jp, *published_args(doi="10.5281/zenodo.5"), root=self.root)
        self.assertIn("10.5281/zenodo.7", str(code))
        self.assertIn("10.5281/zenodo.5", str(code))
        self.assertIn("never replaced", str(code))
        self.assertTrue(snap.unchanged())

    def test_F01_an_identical_doi_and_id_already_saved_is_accepted_and_not_rewritten(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["zenodo_id"], cfg["doi"] = 5, "10.5281/zenodo.5"
        (self.paper / "zenodo.json").write_text(json.dumps(cfg, indent=2) + "\n")
        before = (self.paper / "zenodo.json").read_bytes()
        code, _, _ = reconcile(self.jp, *published_args(doi="10.5281/zenodo.5"), root=self.root)
        self.assertIsNone(code, code)
        self.assertEqual((self.paper / "zenodo.json").read_bytes(), before)

    def test_an_empty_doi_or_an_absent_one_is_filled(self):
        for value in ("", None):
            root, paper = workspace(zenodo_id=None, doi=value)
            jp = make_blocking_journal(root, paper).path
            code, _, _ = reconcile(jp, *published_args(doi="10.5281/zenodo.5"), root=root)
            self.assertIsNone(code, code)
            self.assertEqual(json.loads((paper / "zenodo.json").read_text())["doi"], "10.5281/zenodo.5")

    def test_a_different_zenodo_id_already_saved_is_refused_with_both_values(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["zenodo_id"] = 9
        (self.paper / "zenodo.json").write_text(json.dumps(cfg, indent=2) + "\n")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("zenodo_id 9", str(code))
        self.assertIn("concept 5", str(code))

    def test_a_non_string_doi_already_saved_is_not_silently_replaced(self):
        cfg = json.loads((self.paper / "zenodo.json").read_text())
        cfg["doi"] = ["10.5281/zenodo.5"]
        (self.paper / "zenodo.json").write_text(json.dumps(cfg, indent=2) + "\n")
        code, _, _ = reconcile(self.jp, *published_args(), root=self.root)
        self.assertIn("never replaced", str(code))

    def test_Q6_validate_id_refuses_digits_of_other_scripts(self):
        for bad in ("\u0665", "5\u0665", "\u0661\u0662", "\uff15", "5\u0966", "\u00b2", "1\u00b2", "\u0665" * 3):
            with self.assertRaises(zp.ReconcileRefused, msg=bad):
                zp.validate_id(bad, "x")
            self.assertFalse(zp._is_ascii_digits(bad), bad)
        for good in ("5", "10", "123456789012"):
            self.assertEqual(zp.validate_id(good, "x"), good)
        for bad in ("0", "05", "", " 5", "5 ", "+5", "-5", "1" * 13, "5\n"):
            with self.assertRaises(zp.ReconcileRefused, msg=bad):
                zp.validate_id(bad, "x")
        for bad in (True, 5.0, None, b"5", [5]):
            with self.assertRaises(zp.ReconcileRefused):
                zp.validate_id(bad, "x")
        self.assertEqual(zp.validate_id(5, "x"), "5")

    def test_Q6_an_arabic_indic_record_or_concept_id_is_a_clean_refusal_on_the_command_line(self):
        for flag, extra in (("--record-id", ["--concept-id", "5"]), ("--concept-id", ["--record-id", "301"])):
            code, out, err = reconcile(self.jp, "--outcome", "published", flag, "5\u0665", *extra,
                                       "--concept-doi", GOOD_DOI, root=self.root)
            self.assertIn(flag, str(code))
            self.assertNotIn("Traceback", out + err)
        self.assertTrue(self.snap.unchanged())

    def test_Q38_a_concept_id_read_from_the_journal_is_validated_before_use(self):
        for bad in ("abc", "\u0665", "007", "-5", " 5", "5.0", "1" * 13):
            root, paper = workspace(zenodo_id=None, doi=None)
            jp = make_blocking_journal(root, paper, concept_id="5").path
            tamper(jp, concept_id=bad)
            snap = Bytes(paper / "zenodo.json", jp)
            code, out, err = reconcile(jp, "--outcome", "published", "--record-id", "301", "--concept-doi", GOOD_DOI,
                                       root=root)
            self.assertIsInstance(code, str, bad)
            self.assertIn("the concept id recorded in the journal", code, bad)
            self.assertNotIn("Traceback", out + err)
            self.assertTrue(snap.unchanged(), bad)

    def test_a_concept_doi_recorded_in_the_journal_is_validated_too(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper, concept_id="5", concept_doi="10.9999/evil").path
        snap = Bytes(paper / "zenodo.json", jp)
        code, _, _ = reconcile(jp, "--outcome", "published", "--record-id", "301", root=root)
        self.assertIn("the concept DOI recorded in the journal", str(code))
        self.assertTrue(snap.unchanged())

    def test_a_journal_host_that_is_not_a_string_is_refused_without_a_traceback(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper).path
        data = json.loads(jp.read_text())
        data["host"] = ["zenodo.org"]
        data.pop("identity_sha256")
        data["identity_sha256"] = zp.identity_digest(data)
        jp.write_text(json.dumps(data))
        code, out, err = reconcile(jp, *published_args(), root=root)
        self.assertIsInstance(code, str)
        self.assertNotIn("Traceback", out + err)


# ── N-2 / N-5 / N-6: redaction ────────────────────────────────────────────────────────────────────

SECRET_FORMS = [
    "Authorization=Bearer {s}", "Authorization=Basic {s}", "Authorization: Bearer {s}", "authorization = bearer {s}",
    "Authorization: Basic {s}", 'Authorization="Bearer {s}"', "Authorization='Basic {s}'", 'Authorization: "Bearer {s}"',
    '{{"Authorization": "Bearer {s}"}}', "{{'Authorization': 'Basic {s}'}}", "AUTHORIZATION=BEARER {s}",
    "Proxy-Authorization=Basic {s}", "Proxy-Authorization: Basic {s}", "authorization:bearer {s}", "Authorization = Basic {s}",
    "Authorization=Token {s}", "Authorization: Digest {s}",
    "access%5Ftoken={s}", "X%2DApi%2DKey: {s}", "%61ccess_token={s}", "tok%65n={s}", "x%2dapi%2dkey={s}",
    "Authorization%3A Bearer {s}", "access_token%3D{s}", "Access%5FToken%3D{s}",
    "X-Auth-Token: {s}", "X-Access-Token: {s}", "Private-Token: {s}", "x_auth_token={s}", "private_token={s}",
    "ZENODO_TOKEN={s}", "ZENODO_SANDBOX_TOKEN={s}", "zenodo_token = {s}", "export ZENODO_TOKEN='{s}'",
    "?a=1&token={s}&b=2", "https://h/x?access_token={s}", "Cookie: sid={s}", "Set-Cookie: sid={s}; Path=/",
    "Bearer {s}", "see Bearer {s}.", "bearer {s}", "Basic {s}",
]


class RedactionMatrix(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_N2_every_form_masks_the_credential_in_every_order_of_pattern_application(self):
        for form in SECRET_FORMS:
            text = form.format(s=SECRET)
            for order in itertools.permutations(zp._PATTERN_PASSES):
                out = zp.redact(text, passes=order)
                self.assertNotIn(SECRET, out, (form, [f.__name__ for f in order], out))
                self.assertNotIn("SECRETX9", out, (form, out))

    def test_N2_the_scheme_word_is_consumed_with_the_credential_in_a_field(self):
        for text, expect in (("Authorization=Bearer X1", "Authorization=[REDACTED]"),
                             ("Authorization=Basic X1", "Authorization=[REDACTED]"),
                             ("Authorization: Bearer X1", "Authorization: [REDACTED]"),
                             ("authorization = bearer X1", "authorization = [REDACTED]"),
                             ('Authorization="Bearer X1"', 'Authorization="[REDACTED]"'),
                             ('"Authorization": "Bearer X1"', '"Authorization": "[REDACTED]"'),
                             ("Proxy-Authorization=Basic X1", "Proxy-Authorization=[REDACTED]")):
            self.assertEqual(zp.redact(text), expect, text)

    def test_N2_a_bare_scheme_keeps_its_word_and_masks_the_credential_only(self):
        self.assertEqual(zp.redact("see Bearer abc.def-1_2."), "see Bearer [REDACTED]")
        self.assertEqual(zp.redact("x Basic YWxhZGRpbjpvcGVu y"), "x Basic [REDACTED] y")
        self.assertEqual(zp.redact('"v": "Bearer q1w2"'), '"v": "Bearer [REDACTED]"')      # the closing quote survives

    def test_N6_encoded_names_are_decoded_before_matching(self):
        self.assertEqual(zp.redact("access%5Ftoken=ABC"), "access_token=[REDACTED]")
        self.assertEqual(zp.redact("X%2DApi%2DKey: ABC"), "X-Api-Key: [REDACTED]")
        self.assertTrue(zp.is_sensitive_name("access%5Ftoken"))
        self.assertTrue(zp.is_sensitive_name("X%2DApi%2DKey"))
        self.assertEqual(zp.redact("nothing%20to%20hide=1"), "nothing%20to%20hide=1")      # not a sensitive name: unchanged

    def test_N6_the_two_token_variables_are_masked_in_assignments_only(self):
        self.assertEqual(zp.redact("ZENODO_TOKEN=abc"), "ZENODO_TOKEN=[REDACTED]")
        self.assertEqual(zp.redact("ZENODO_SANDBOX_TOKEN=abc"), "ZENODO_SANDBOX_TOKEN=[REDACTED]")
        prose = "never falls back to ZENODO_TOKEN: the production token is never sent to the sandbox"
        self.assertEqual(zp.redact(prose), prose)

    def test_N6_new_header_names(self):
        for name in ("X-Auth-Token", "X-Access-Token", "Private-Token", "x_auth_token", "PRIVATE-TOKEN"):
            self.assertEqual(zp.redact(f"{name}: v1"), f"{name}: [REDACTED]")
            self.assertTrue(zp.is_sensitive_name(name), name)
        self.assertEqual(zp.redact_obj({"X-Auth-Token": "v1", "ok": "fine"}), {"X-Auth-Token": "[REDACTED]", "ok": "fine"})

    def test_N6_whitespace_only_secrets_are_never_registered(self):
        zp.register_secret("  ")
        zp.register_secret("\t\n ")
        zp.register_secret("")
        zp.register_environment_secrets({"ZENODO_TOKEN": "  ", "ZENODO_SANDBOX_TOKEN": "\n\t"})
        self.assertEqual(zp._SECRETS, set())
        self.assertEqual(zp.redact("a  b\t\nc"), "a  b\t\nc")

    def test_N6_a_short_secret_is_masked_only_as_a_whole_token(self):
        for secret in ("a", "ab", "Z~", "abc1234"):
            zp._SECRETS.clear()
            zp.register_secret(secret)
            self.assertEqual(zp.redact(f"x {secret} y"), "x [REDACTED] y", secret)
            self.assertEqual(zp.redact(f"({secret}) {secret}."), "([REDACTED]) [REDACTED].", secret)
            self.assertEqual(zp.redact(f"x{secret}y"), f"x{secret}y", secret)
            self.assertEqual(zp.redact(f"_{secret}_"), f"_{secret}_", secret)
            self.assertEqual(zp.redact(f"1{secret}"), f"1{secret}", secret)

    def test_N6_a_secret_of_eight_characters_or_more_is_masked_wherever_it_occurs(self):
        zp.register_secret("12345678")
        self.assertEqual(zp.redact("x12345678y"), "x[REDACTED]y")
        zp._SECRETS.clear()
        zp.register_secret("1234567")
        self.assertEqual(zp.redact("x1234567y"), "x1234567y")           # 7: documented limit
        self.assertEqual(MIN := zp.MIN_BLIND_SECRET, 8)

    def test_N6_one_and_two_character_secrets_leave_a_journal_valid_with_its_keys_intact(self):
        for secret in ("a", "p", "ap", "e_", "1", "e"):
            d = Path(tempfile.mkdtemp()).resolve()
            ref = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {"note": "a paper e"}, "1.0")
            ref_keys = set(json.loads(ref.path.read_text()))
            with mock.patch.object(zp, "_SECRETS", set()):
                zp.register_secret(secret)
                j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {"note": f"a paper {secret} e"}, "1.0")
                j.update(note=f"boom {secret} and {secret}.")
                data = json.loads(j.path.read_text())              # still valid JSON
            self.assertEqual(set(data), ref_keys | {"identity_sha256"} if "identity_sha256" not in ref_keys else ref_keys, secret)
            self.assertEqual(data["paper"], "paper", secret)
            self.assertIn("[REDACTED]", " ".join(data["notes"]), secret)
            self.assertEqual(zp.journal_identity_problems(data), [], secret)

    def test_N6_a_dictionary_key_equal_to_a_short_secret_is_not_mangled(self):
        zp.register_secret("paper")
        self.assertEqual(zp.redact_obj({"paper": "paper x", "n": 1}), {"paper": "[REDACTED] x", "n": 1})

    def test_N6_both_environment_tokens_and_their_stripped_forms_are_registered(self):
        zp.register_environment_secrets({"ZENODO_TOKEN": " TOKEN-ONE-12345 ", "ZENODO_SANDBOX_TOKEN": "tok-two"})
        self.assertEqual(zp._SECRETS, {" TOKEN-ONE-12345 ", "TOKEN-ONE-12345", "tok-two"})


class EveryOutputSiteIsRedacted(unittest.TestCase):
    """N-6: the arguments echo, every print and every message pass through redact (checked by AST and by behaviour)."""

    def calls(self):
        tree = ast.parse(SRC)
        return [(n.lineno, ast.unparse(n.func)) for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def test_the_only_raw_output_calls_are_the_redacting_ones(self):
        raw = [(l, f) for l, f in self.calls() if f in ("sys.exit", "_builtins.print", "builtins.print", "os._exit")
               or f.endswith("stderr.write") or f.endswith("stdout.write") or f.startswith("logging.")
               or f.startswith("traceback.print") or f.endswith("_print_message")]
        names = sorted(f for _, f in raw)
        self.assertEqual(names, ["_builtins.print", "self._print_message", "sys.exit"], raw)       # print(), parser, die()
        by_name = {f: l for l, f in raw}
        text = SRC.splitlines()
        self.assertIn("redact(", text[by_name["_builtins.print"] - 1])
        self.assertIn("redact(", text[by_name["sys.exit"] - 1])
        self.assertIn("redact(", text[by_name["self._print_message"] - 1])

    def test_print_is_the_module_print_and_masks(self):
        out = Path(tempfile.mkdtemp()) / "o.txt"
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_secret("TOPSECRET12345")
            with open(out, "w") as fh:
                zp.print("a TOPSECRET12345 b", "token=zzz", file=fh)
        self.assertEqual(out.read_text(), "a [REDACTED] b token=[REDACTED]\n")

    def test_no_function_of_the_module_calls_the_builtin_print_by_another_name(self):
        tree = ast.parse(SRC)
        shadowed = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "print"]
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(zp.print.__module__, "zenodo_publish")

    def test_N6_the_argparse_echo_of_a_token_is_masked_even_before_parsing(self):
        for argv in (["--outcome", PROD_TOKEN, "--reconcile", "/x/y.run.json"], ["--reconcile", "x", "--outcome", SBX_TOKEN],
                     ["--bogus-flag-" + PROD_TOKEN], [PROD_TOKEN, SBX_TOKEN, "--unknown"], ["--record-id=" + PROD_TOKEN, "--bad"]):
            root, paper = workspace()
            with mock.patch.object(zp, "_SECRETS", set()):
                code, out, err = run_main(argv, {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}, root)
            for stream in (str(code), out, err):
                self.assertNotIn(PROD_TOKEN, stream, argv)
                self.assertNotIn(SBX_TOKEN, stream, argv)
            self.assertEqual(code, 2, argv)

    def test_N6_a_plain_message_carrying_a_token_is_masked_everywhere_it_is_printed(self):
        root, paper = workspace(zenodo_id=5, doi=None)
        paper_named = paper.parent / f"x{PROD_TOKEN}y"
        os.rename(paper, paper_named)
        with mock.patch.object(zp, "_SECRETS", set()):
            code, out, err = run_main([str(paper_named), "--dry-run"], {"ZENODO_TOKEN": PROD_TOKEN}, root, Factory())
        self.assertNotIn(PROD_TOKEN, out + err + str(code))

    def test_die_masks_the_registered_secrets_and_the_named_fields(self):
        with mock.patch.object(zp, "_SECRETS", set()):
            zp.register_secret("TOPSECRET12345")
            with self.assertRaises(SystemExit) as cm:
                zp.die("Error: boom TOPSECRET12345 and token=zzz")
        self.assertEqual(cm.exception.code, "Error: boom [REDACTED] and token=[REDACTED]")

    def test_every_sys_exit_site_goes_through_die(self):
        self.assertEqual(SRC.count("sys.exit("), 1)
        self.assertIn("def die(", SRC)


# ── N-3: --backup-journal / --lift-block (a decision file; the journal is never touched) ───────────────────

class Journals:
    """Builders for the four liftable kinds (and one that is not)."""

    @staticmethod
    def unreadable(root, paper, content=b"{trunc"):
        p = state_dir_for(root) / f"paper-20260101T000000Z-1-{os.urandom(3).hex()}.run.json"
        p.write_bytes(content)
        return p

    @staticmethod
    def unknown_outcome(root, paper, outcome="PUBLISHD"):
        j = make_blocking_journal(root, paper)
        tamper(j.path, outcome=outcome)
        return j.path

    @staticmethod
    def identity(root, paper, how="edit"):
        j = make_blocking_journal(root, paper)
        if how == "edit":
            tamper(j.path, bib_key="SomethingElse")
        elif how == "no-digest":
            d = json.loads(j.path.read_text())
            d.pop("identity_sha256")
            j.path.write_text(json.dumps(d))
        return j.path

    @staticmethod
    def moved(root, paper, outcome="UNRECONCILED"):
        j = make_blocking_journal(root, paper, outcome=outcome)
        moved = paper.parent / "paper-moved"
        os.rename(paper, moved)
        return j.path, moved

    @staticmethod
    def normal(root, paper):
        return make_blocking_journal(root, paper).path


class BackupJournal(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace()

    def test_the_copy_has_a_unique_name_the_same_bytes_and_the_original_is_untouched(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        before = jp.read_bytes()
        mtime = jp.stat().st_mtime_ns
        code, out, _ = run_main(["--backup-journal", str(jp)], {}, self.root)
        self.assertIsNone(code, code)
        (copy_path,) = list(jp.parent.glob("*.quarantine-copy-*.json"))
        self.assertRegex(copy_path.name, rf"^{re.escape(jp.name[:-5])}\.quarantine-copy-\d{{8}}T\d{{6}}Z-{sha(before)[:16]}\.json$")
        self.assertEqual(copy_path.parent, jp.parent)
        self.assertEqual(copy_path.read_bytes(), before)
        self.assertEqual((jp.read_bytes(), jp.stat().st_mtime_ns), (before, mtime))
        self.assertFalse(decision_of(jp).exists())
        self.assertTrue(blocked(self.root, self.paper)[0])                         # nothing is lifted
        self.assertIn(sha(before), out)

    def test_the_copy_is_not_itself_a_journal(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        zp.backup_journal(jp)
        self.assertEqual([p.name for p, _ in zp.load_journals(jp.parent)], [jp.name])

    def test_a_planted_file_of_the_same_name_is_never_overwritten(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        name = f"{jp.name[:-5]}.quarantine-copy-20261010T000000Z-{sha(jp)[:16]}.json"
        planted = jp.parent / name
        planted.write_bytes(b"planted")
        with mock.patch.object(zp, "_utc_stamp", lambda: "20261010T000000Z"):
            code, _, _ = run_main(["--backup-journal", str(jp)], {}, self.root)
        self.assertIn("refusing to overwrite", str(code))
        self.assertEqual(planted.read_bytes(), b"planted")

    def test_the_same_second_and_the_same_content_reuses_the_copy_the_same_second_other_content_makes_another(self):
        jp = Journals.unreadable(self.root, self.paper, b"{a")
        with mock.patch.object(zp, "_utc_stamp", lambda: "20261010T000000Z"):
            first, sha1, created1 = zp.backup_journal(jp)
            again, _, created2 = zp.backup_journal(jp)
            self.assertEqual((again, created1, created2), (first, True, False))
            jp.write_bytes(b"{b")
            second, sha2, created3 = zp.backup_journal(jp)
        self.assertNotEqual(first, second)
        self.assertNotEqual(sha1, sha2)
        self.assertEqual((first.read_bytes(), second.read_bytes()), (b"{a", b"{b"))

    def test_an_unreadable_journal_with_invalid_utf8_is_copied_byte_for_byte(self):
        jp = Journals.unreadable(self.root, self.paper, b"\xff\xfe\x00{\x80")
        copy_path, digest, _ = zp.backup_journal(jp)
        self.assertEqual(copy_path.read_bytes(), b"\xff\xfe\x00{\x80")
        self.assertEqual(digest, sha(b"\xff\xfe\x00{\x80"))

    def test_the_copy_is_written_by_exclusive_create_and_fsynced(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        events = []
        real_link, real_fsync = os.link, os.fsync
        with mock.patch.object(os, "link", lambda a, b: (events.append(("link", str(b))), real_link(a, b))[1]), \
                mock.patch.object(os, "fsync", lambda fd: (events.append(("fsync",)), real_fsync(fd))[1]):
            copy_path, _, _ = zp.backup_journal(jp)
        self.assertIn(("link", str(copy_path)), events)
        self.assertLess(events.index(("fsync",)), events.index(("link", str(copy_path))))

    def test_a_non_journal_name_a_directory_and_a_missing_file_are_refused_cleanly(self):
        for target in (self.paper / "zenodo.json", self.paper, state_dir_for(self.root) / "nothing.run.json"):
            code, out, err = run_main(["--backup-journal", str(target)], {}, self.root)
            self.assertIsInstance(code, str, target)
            self.assertNotIn("Traceback", out + err)

    def test_it_takes_no_other_reconcile_option(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        code, _, _ = run_main(["--backup-journal", str(jp), "--reason", REASON], {}, self.root)
        self.assertEqual(code, 2)


class QuarantineFlagIsRemoved(unittest.TestCase):
    def test_it_is_refused_with_a_pointer_to_the_new_flags_and_nothing_is_touched(self):
        root, paper = workspace()
        jp = Journals.unknown_outcome(root, paper)
        snap = Bytes(jp)
        for argv in (["--reconcile", str(jp), "--quarantine", "--reason", REASON], ["--quarantine"],
                     [str(paper), "--quarantine"], ["--reconcile", str(jp), "--quarantine"]):
            code, out, err = run_main(argv, {}, root)
            self.assertEqual(code, 2, argv)
            self.assertIn("--lift-block", err, argv)
            self.assertIn("--backup-journal", err, argv)
        self.assertTrue(snap.unchanged())
        self.assertEqual(sorted(p.name for p in jp.parent.iterdir()), [jp.name])

    def test_no_message_of_the_tool_tells_the_operator_to_quarantine_or_rename_a_journal(self):
        for line in SRC.splitlines():
            if "--quarantine" in line:
                self.assertTrue("REMOVED" in line or "was removed" in line or "parser.error" in line or '"--quarantine"' in line
                                or "args.quarantine" in line, line)
        self.assertNotIn("*.quarantined", SRC)
        self.assertNotIn("def quarantine_journal", SRC)


class LiftBlock(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace()

    def assert_lifts(self, jp, *extra, **kw):
        before = jp.read_bytes()
        self.assertTrue(blocked(self.root, self.paper)[0])
        code, out, err = lift(jp, self.root, *extra, **kw)
        self.assertIsNone(code, code)
        self.assertEqual(jp.read_bytes(), before)                               # the original is byte-identical
        self.assertTrue(decision_of(jp).exists())
        self.assertFalse(blocked(self.root, self.paper)[0])
        return out

    def test_each_liftable_kind_is_lifted_by_a_decision_and_the_journal_is_never_touched(self):
        cases = {
            "unreadable": Journals.unreadable(self.root, self.paper),
            "unknown-outcome": None, "identity-edit": None, "identity-no-digest": None,
        }
        for kind in cases:
            root, paper = workspace()
            self.root, self.paper = root, paper
            jp = {"unreadable": lambda: Journals.unreadable(root, paper),
                  "unknown-outcome": lambda: Journals.unknown_outcome(root, paper),
                  "identity-edit": lambda: Journals.identity(root, paper, "edit"),
                  "identity-no-digest": lambda: Journals.identity(root, paper, "no-digest")}[kind]()
            self.assert_lifts(jp)

    def test_a_journal_whose_directory_moved_is_lifted(self):
        jp, moved = Journals.moved(self.root, self.paper)
        self.paper = moved
        self.assert_lifts(jp)

    def test_the_decision_file_records_who_when_why_the_evidence_and_the_digest(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        code, _, _ = lift(jp, self.root, reason="  looked at the account and the draft list  ")
        self.assertIsNone(code, code)
        d = json.loads(decision_of(jp).read_text())
        self.assertEqual(d["decision"], "UNBLOCK")
        self.assertEqual(d["journal"], jp.name)
        self.assertEqual(d["journal_sha256"], sha(jp))
        self.assertEqual(d["block_kind"], "unknown-outcome")
        self.assertEqual(d["reason"], "looked at the account and the draft list")
        self.assertEqual(d["evidence"], {"checked_account": True})
        self.assertRegex(d["decided_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertRegex(d["decided_by"], r".+@.+")
        self.assertEqual(d["backup_sha256"], d["journal_sha256"])
        self.assertEqual((jp.parent / d["backup"]).read_bytes(), jp.read_bytes())
        self.assertEqual(d["outcome_at_decision"], "PUBLISHD")

    def test_the_record_url_is_validated_and_stored_as_the_evidence(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        for bad in ("http://zenodo.org/deposit/301", "https://evil.example/deposit/301", "https://zenodo.org/",
                    "https://zenodo.org/deposit/30", "zenodo.org/deposit/301", "https://user@zenodo.org/deposit/301"):
            code, _, _ = run_main(["--lift-block", str(jp), "--record-url", bad, "--reason", REASON], {}, self.root)
            self.assertIsInstance(code, str, bad)
            self.assertFalse(decision_of(jp).exists(), bad)
        code, _, _ = run_main(["--lift-block", str(jp), "--record-url", "https://zenodo.org/deposit/301",
                               "--reason", REASON], {}, self.root)
        self.assertIsNone(code, code)
        self.assertEqual(json.loads(decision_of(jp).read_text())["evidence"], {"record_url": "https://zenodo.org/deposit/301"})

    def test_evidence_and_a_reason_of_ten_characters_are_required(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        for argv in (["--lift-block", str(jp)], ["--lift-block", str(jp), "--reason", REASON],
                     ["--lift-block", str(jp), "--i-checked-the-account"],
                     ["--lift-block", str(jp), "--i-checked-the-account", "--reason", "123456789"],
                     ["--lift-block", str(jp), "--i-checked-the-account", "--reason", "          "]):
            code, _, _ = run_main(argv, {}, self.root)
            self.assertIsInstance(code, str, argv)
            self.assertFalse(decision_of(jp).exists(), argv)
            self.assertEqual(list(jp.parent.glob("*.quarantine-copy-*")), [], argv)        # nothing was made at all
        code, _, _ = lift(jp, self.root, reason="0123456789")
        self.assertIsNone(code, code)

    def test_a_second_lift_is_refused_and_the_first_decision_is_not_touched(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        self.assertIsNone(lift(jp, self.root)[0])
        first = decision_of(jp).read_bytes()
        code, _, _ = lift(jp, self.root, reason="a second and different reason")
        self.assertIn("already exists", str(code))
        self.assertEqual(decision_of(jp).read_bytes(), first)
        self.assertEqual(len(list(jp.parent.glob("*.quarantine-copy-*"))), 1)

    def test_a_decision_file_that_appears_between_the_check_and_the_write_is_never_overwritten(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        real = zp.find_journal_backup

        def plant(path, digest):
            decision_of(jp).write_text("planted")
            return real(path, digest)
        with mock.patch.object(zp, "find_journal_backup", plant):
            code, _, _ = lift(jp, self.root)
        self.assertIn("appeared meanwhile", str(code))
        self.assertEqual(decision_of(jp).read_text(), "planted")

    def test_editing_the_journal_after_the_decision_blocks_again(self):
        for edit in (lambda b: b + b"\n", lambda b: b.replace(b"PUBLISHD", b"PUBLISHE"), lambda b: b[:-1]):
            root, paper = workspace()
            jp = Journals.unknown_outcome(root, paper)
            self.assertIsNone(lift(jp, root)[0])
            self.assertFalse(blocked(root, paper)[0])
            jp.write_bytes(edit(jp.read_bytes()))
            self.assertTrue(blocked(root, paper)[0])

    def test_a_decision_for_another_journal_digest_is_ignored(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        self.assertIsNone(lift(jp, self.root)[0])
        d = json.loads(decision_of(jp).read_text())
        d["journal_sha256"] = sha(b"something else")
        decision_of(jp).write_text(json.dumps(d))
        self.assertTrue(blocked(self.root, self.paper)[0])

    def test_a_decision_copied_from_another_journal_of_identical_bytes_is_ignored(self):
        a = Journals.unreadable(self.root, self.paper, b"{same")
        b = Journals.unreadable(self.root, self.paper, b"{same")
        self.assertIsNone(lift(a, self.root)[0])
        self.assertTrue(blocked(self.root, self.paper)[0])                          # b still blocks
        decision_of(b).write_bytes(decision_of(a).read_bytes())
        self.assertTrue(blocked(self.root, self.paper)[0])                          # the copy names journal a

    def test_a_decision_of_another_block_kind_or_not_UNBLOCK_or_a_symlink_is_ignored(self):
        for change in ({"block_kind": "identity"}, {"decision": "REFUSE"}, {"decision": "unblock"}, {"journal": "x.run.json"},
                       {"journal_sha256": None}, {"journal_sha256": 5}):
            root, paper = workspace()
            jp = Journals.unknown_outcome(root, paper)
            self.assertIsNone(lift(jp, root)[0])
            d = json.loads(decision_of(jp).read_text())
            d.update(change)
            decision_of(jp).write_text(json.dumps(d))
            self.assertTrue(blocked(root, paper)[0], change)
        root, paper = workspace()
        jp = Journals.unknown_outcome(root, paper)
        self.assertIsNone(lift(jp, root)[0])
        real = decision_of(jp)
        elsewhere = real.parent / "elsewhere.json"
        elsewhere.write_bytes(real.read_bytes())
        os.rename(real, real.parent / "kept.json")
        os.symlink(elsewhere, real)
        self.assertTrue(blocked(root, paper)[0])

    def test_a_malformed_decision_file_never_lifts_and_never_crashes(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        for content in ("", "{", "[]", "null", '"UNBLOCK"', "\xff"):
            decision_of(jp).write_bytes(content.encode("latin-1"))
            self.assertTrue(blocked(self.root, self.paper)[0], content)

    def test_a_decision_never_lifts_a_journal_that_has_a_known_outcome_and_a_consistent_identity(self):
        jp = Journals.normal(self.root, self.paper)
        code, _, _ = lift(jp, self.root)
        self.assertIn("reconcile", str(code))
        self.assertFalse(decision_of(jp).exists())
        self.assertEqual(list(jp.parent.glob("*.quarantine-copy-*")), [])
        # a hand-written decision for it is ignored as well
        d = {"decision": "UNBLOCK", "journal": jp.name, "journal_sha256": sha(jp), "block_kind": None}
        decision_of(jp).write_text(json.dumps(d))
        self.assertTrue(blocked(self.root, self.paper)[0])
        d["block_kind"] = "identity"
        decision_of(jp).write_text(json.dumps(d))
        self.assertTrue(blocked(self.root, self.paper)[0])

    def test_a_non_blocking_journal_is_not_liftable(self):
        j = make_blocking_journal(self.root, self.paper, outcome="PUBLISHED")
        code, _, _ = lift(j.path, self.root)
        self.assertIsInstance(code, str)
        self.assertFalse(decision_of(j.path).exists())

    def test_the_original_is_byte_identical_after_backup_and_lift_and_nothing_else_changes(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        cfg = Bytes(self.paper / "zenodo.json", jp)
        before_names = {p.name for p in jp.parent.iterdir()}
        self.assertIsNone(run_main(["--backup-journal", str(jp)], {}, self.root)[0])
        self.assertIsNone(lift(jp, self.root)[0])
        self.assertTrue(cfg.unchanged())
        added = {p.name for p in jp.parent.iterdir()} - before_names
        self.assertEqual(len(added), 2, added)                                       # the copy and the decision only
        self.assertEqual(len(list(jp.parent.glob("*.quarantine-copy-*.json"))), 1)      # the lift reused the copy

    def test_an_existing_copy_with_the_same_sha256_is_reused_one_with_other_bytes_is_not_trusted(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        digest = sha(jp)
        fake = jp.parent / f"{jp.name[:-5]}.quarantine-copy-20200101T000000Z-{digest[:16]}.json"
        fake.write_bytes(b"not the journal")
        self.assertIsNone(lift(jp, self.root)[0])
        d = json.loads(decision_of(jp).read_text())
        self.assertNotEqual(d["backup"], fake.name)
        self.assertEqual((jp.parent / d["backup"]).read_bytes(), jp.read_bytes())
        self.assertEqual(fake.read_bytes(), b"not the journal")
        jp2 = Journals.unknown_outcome(self.root, self.paper, outcome="OTHER")
        good, _, _ = zp.backup_journal(jp2)
        self.assertIsNone(lift(jp2, self.root)[0])
        self.assertEqual(json.loads(decision_of(jp2).read_text())["backup"], good.name)

    def test_a_journal_that_is_not_a_regular_run_journal_is_refused(self):
        for target in (self.paper / "zenodo.json", self.paper):
            code, out, err = lift(target, self.root)
            self.assertIsInstance(code, str, target)
            self.assertNotIn("Traceback", out + err)

    def test_lift_block_takes_no_reconcile_option(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        for extra in (["--outcome", "published"], ["--record-id", "5"], ["--concept-id", "5"], ["--concept-doi", GOOD_DOI],
                      ["--reconcile", str(jp)]):
            code, _, _ = run_main(["--lift-block", str(jp), "--i-checked-the-account", "--reason", REASON, *extra], {}, self.root)
            self.assertEqual(code, 2, extra)
        self.assertFalse(decision_of(jp).exists())

    def test_a_write_failure_of_the_decision_leaves_the_journal_blocking_and_is_a_message(self):
        jp = Journals.unknown_outcome(self.root, self.paper)
        with mock.patch.object(zp, "_create_exclusive_text", side_effect=OSError(28, "No space left")):
            code, out, err = lift(jp, self.root)
        self.assertIn("cannot write the decision file", str(code))
        self.assertNotIn("Traceback", out + err)
        self.assertTrue(blocked(self.root, self.paper)[0])


class LiftBlockKindsAndConsequences(unittest.TestCase):
    def test_a_published_unsaved_journal_whose_directory_moved_is_refused_by_reconcile_and_lifted_with_its_consequences(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp, moved = Journals.moved(root, paper, outcome=zp.UNSAVED_OUTCOME)
        snap = Bytes(moved / "zenodo.json", jp)
        code, _, _ = reconcile(jp, *published_args(), root=root)
        self.assertIn("no longer exists", str(code))
        self.assertIn("--lift-block", str(code))
        self.assertTrue(snap.unchanged())
        code, out, _ = lift(jp, root)
        self.assertIsNone(code, code)
        self.assertIn("SUCCEEDED", out)
        self.assertIn("do NOT re-run the deposit", out)
        self.assertIn("by hand", out)
        self.assertTrue(snap.unchanged())                                           # nothing is saved, nothing is edited
        self.assertEqual(json.loads(decision_of(jp).read_text())["block_kind"], "directory")
        self.assertEqual(json.loads(decision_of(jp).read_text())["outcome_at_decision"], zp.UNSAVED_OUTCOME)

    def test_the_consequences_of_an_ordinary_lift_say_a_new_deposition_may_be_created(self):
        root, paper = workspace()
        jp = Journals.unknown_outcome(root, paper)
        out = lift(jp, root)[1]
        self.assertIn("MAY CREATE A NEW DEPOSITION", out)

    def test_the_blocking_message_names_the_right_cure_for_each_kind(self):
        for kind, build in (("unreadable", Journals.unreadable), ("unknown", Journals.unknown_outcome),
                            ("identity", Journals.identity)):
            root, paper = workspace()
            jp = build(root, paper)
            _, msg = blocked(root, paper)
            self.assertIn(f"--lift-block {jp}", msg, kind)
            self.assertIn("--backup-journal", msg, kind)
            self.assertNotIn("quarantine --reason", msg, kind)
            self.assertNotIn("renames it", msg, kind)
        root, paper = workspace()
        jp = Journals.normal(root, paper)
        _, msg = blocked(root, paper)
        self.assertIn("--reconcile", msg)
        self.assertNotIn("--lift-block", msg)

    def test_the_block_kind_of_each_journal(self):
        root, paper = workspace()
        self.assertEqual(zp.journal_block_kind(None), "unreadable")
        self.assertEqual(zp.journal_block_kind({"outcome": "X"}), "unknown-outcome")
        self.assertEqual(zp.journal_block_kind({}), "unknown-outcome")
        self.assertIsNone(zp.journal_block_kind({"outcome": "PUBLISHED"}))
        normal = json.loads(Journals.normal(root, paper).read_text())
        self.assertIsNone(zp.journal_block_kind(normal))
        self.assertEqual(zp.journal_block_kind(dict(normal, bib_key="Z")), "identity")
        self.assertEqual(zp.journal_block_kind({k: v for k, v in normal.items() if k != "identity_sha256"}), "identity")
        gone = dict(normal, paper_dir_real=str(root / "nowhere"))
        gone["identity_sha256"] = zp.identity_digest(gone)
        self.assertEqual(zp.journal_block_kind(gone), "directory")
        none_dir = dict(normal, paper_dir=None)
        none_dir["identity_sha256"] = zp.identity_digest(none_dir)
        self.assertEqual(zp.journal_block_kind(none_dir), "directory")

    def test_a_retargeted_symlink_is_a_directory_kind_and_reconcile_still_refuses(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        _, other = workspace(zenodo_id=None, doi=None)
        link = root / "alias"
        os.symlink(paper, link)
        j = make_blocking_journal(root, paper, outcome=zp.UNSAVED_OUTCOME, paper_dir=link)
        os.unlink(link)
        os.symlink(other, link)
        snap = Bytes(other / "zenodo.json", paper / "zenodo.json")
        code, _, _ = reconcile(j.path, *published_args(), root=root)
        self.assertIsNotNone(code)
        self.assertTrue(snap.unchanged())
        self.assertEqual(zp.journal_block_kind(json.loads(j.path.read_text())), "directory")


# ── N-4: related identifiers ──────────────────────────────────────────────────────────────────────

def entry(identifier, relation="isSupplementedBy", scheme="url", resource_type="other"):
    e = {"identifier": identifier, "relation": relation, "scheme": scheme}
    if resource_type is not None:
        e["resource_type"] = resource_type
    return e


class RelatedIdentifiersAreKept(unittest.TestCase):
    def test_N4_D06_a_single_typed_other_entry_on_a_foreign_host_is_kept_and_the_web_entry_appended(self):
        live = [entry("https://github.com/x/y")]
        notes = []
        out = zp.merge_related_web_url(live, ENTRY, notes=notes)
        self.assertEqual(out, live + [ENTRY])
        self.assertEqual(len(notes), 1)
        self.assertIn("https://github.com/x/y", notes[0])
        self.assertIn("kept", notes[0])

    def test_N4_several_foreign_typed_entries_are_all_kept(self):
        live = [entry("https://a.example/1"), entry("https://b.example/2"), entry("10.1/x", "cites", "doi", None)]
        self.assertEqual(zp.merge_related_web_url(live, ENTRY), live + [ENTRY])

    def test_N4_a_cosmochrony_org_entry_is_replaced_in_place(self):
        for host in ("https://cosmochrony.org/science/old/", "https://www.cosmochrony.org/x", "https://a.b.cosmochrony.org/"):
            live = [entry("10.1/x", "cites", "doi", None), entry(host), entry("https://github.com/x/y")]
            self.assertEqual(zp.merge_related_web_url(live, ENTRY), [live[0], ENTRY, live[2]], host)

    def test_previous_web_url_is_gone_and_a_leftover_key_is_harmless(self):
        self.assertFalse(hasattr(zp, "previous_web_urls_of"))
        live = [entry("https://old.example/page"), entry("https://github.com/x/y")]
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, out, _) = update_cli(c, "--set-web-url", "--yes-apply-diff",
                                                 cfg={"web_url": "https://new.example", "previous_web_url": "https://old.example/page"})
        self.assertIsNone(code, code)
        # the key is ignored: the entry of another host is KEPT and the web entry appended
        self.assertEqual(c.meta_put["related_identifiers"], live + [ENTRY])
        for junk in (5, ["https://old.example/page"], {"a": 1}, None):
            c2 = LiveClient(live={**LIVE, "related_identifiers": live})
            root, paper, (code, out, _) = update_cli(c2, "--set-web-url", "--yes-apply-diff",
                                                     cfg={"web_url": "https://new.example", "previous_web_url": junk})
            self.assertIsNone(code, (junk, code))
            self.assertEqual(c2.meta_put["related_identifiers"], live + [ENTRY], junk)

    def test_N4_a_typed_entry_must_also_match_relation_scheme_and_type(self):
        for other in (entry("https://cosmochrony.org/x", "isDocumentedBy"), entry("https://cosmochrony.org/x", scheme="doi"),
                      entry("https://cosmochrony.org/x", resource_type="software")):
            live = [other]
            self.assertEqual(zp.merge_related_web_url(live, ENTRY), live + [ENTRY], other)

    def test_N4_a_whole_update_run_keeps_cites_foreign_entries_and_prints_the_note(self):
        live = [entry("10.1/abc", "cites", "doi", None), entry("https://github.com/x/y", resource_type="software"),
                entry("https://other.example/page")]
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, out, _) = update_cli(c, "--set-web-url", "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIsNone(code, code)
        self.assertEqual(c.meta_put["related_identifiers"], live + [ENTRY])
        self.assertIn("kept the related identifier 'https://other.example/page'", out)

    def test_N4_the_dry_run_diff_prints_the_note_too(self):
        root, paper = workspace(zenodo_id=5)
        live = {**LIVE, "related_identifiers": [entry("https://other.example/page")]}
        snap = Path(tempfile.mkdtemp()) / "live.json"
        snap.write_text(json.dumps({"metadata": live}))
        code, out, _ = run_main([str(paper), "--dry-run", "--update", "--set-web-url", "--live-snapshot", str(snap)],
                                {}, root, Factory())
        self.assertIn("kept the related identifier", out)
        self.assertIn("+", out)

    def test_N4_replace_related_identifier_matches_exactly_one_live_entry_by_exact_identifier(self):
        live = [entry("https://a.example/x"), entry("https://a.example/x/"), entry("https://A.example/x")]
        for name in ("https://missing", "https://a.example/X", "https://a.example", ""):
            if name:
                with self.assertRaises(ValueError, msg=name):
                    zp.merge_related_web_url(live, ENTRY, name)
        notes = []
        out = zp.merge_related_web_url(live, ENTRY, "https://a.example/x/", notes=notes)
        self.assertEqual(out, [live[0], ENTRY, live[2]])
        self.assertIn("replacing the related identifier", notes[0])
        self.assertIn("https://a.example/x/", notes[0])
        dup = [live[0], dict(live[0])]
        with self.assertRaises(ValueError) as cm:
            zp.merge_related_web_url(dup, ENTRY, "https://a.example/x")
        self.assertIn("matches 2", str(cm.exception))

    def test_N5_a_citation_a_reference_or_a_doi_is_never_replaceable(self):
        for e in (entry("https://a.example/x", "cites"), entry("https://a.example/x", "isCitedBy"),
                  entry("https://a.example/x", "references"), entry("https://a.example/x", "ISCITEDBY"),
                  entry("https://a.example/x", "isReferencedBy"), entry("10.1/abc", "isSupplementedBy", "doi"),
                  entry("10.1/abc", "isPartOf", "DOI")):
            with self.assertRaises(ValueError, msg=e) as cm:
                zp.merge_related_web_url([e], ENTRY, e["identifier"])
            self.assertIn("never replaceable", str(cm.exception))
        ok = entry("https://a.example/x", "isSupplementedBy", resource_type="software")
        self.assertEqual(zp.merge_related_web_url([ok], ENTRY, ok["identifier"]), [ENTRY])
        for rel in ("isDocumentedBy", "isVersionOf", "isPartOf", "hasPart", "isDerivedFrom", "compiles"):
            other = entry("https://a.example/x", rel)
            with self.assertRaises(ValueError, msg=rel) as cm:
                zp.merge_related_web_url([other], ENTRY, other["identifier"])
            self.assertIn("never replaceable", str(cm.exception))
        for scheme in ("handle", "urn", "arxiv", "isbn"):
            other = entry("x/y", scheme=scheme)
            with self.assertRaises(ValueError, msg=scheme):
                zp.merge_related_web_url([other], ENTRY, other["identifier"])

    def test_N5_the_refusal_reaches_the_operator_and_nothing_is_edited(self):
        live = [entry("10.1/abc", "cites", "doi", None)]
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, _, _) = update_cli(c, "--set-web-url", "--replace-related-identifier", "10.1/abc",
                                               "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIn("never replaceable", str(code))
        self.assertNotIn("edit", c.names())
        self.assertNotIn("put", c.names())

    def test_N5_the_explicit_replacement_shows_the_replaced_entry_in_the_diff(self):
        live = [entry("https://a.example/x", "isSupplementedBy")]
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, out, _) = update_cli(c, "--set-web-url", "--replace-related-identifier", "https://a.example/x",
                                                 cfg={"web_url": "https://new.example"}, raw_confirm=True)
        self.assertIn('-      "identifier": "https://a.example/x"', out)
        self.assertIn('+      "identifier": "https://new.example"', out)
        self.assertIn("replacing the related identifier", out)

    def test_N4_the_help_text_no_longer_says_none_matching_is_appended(self):
        help_text = zp.build_parser().format_help()
        self.assertNotIn("none matching", help_text)
        flat = " ".join(help_text.split())
        self.assertIn("must match exactly one live entry", flat)
        self.assertIn("cites / isCitedBy / references", flat)


# ── N-7: no secret in the environment of any child ───────────────────────────────────────────────────

SECRET_ENV = {"GH_HOST": "h", "ZENODO_TOKEN": "zt-1", "ZENODO_SANDBOX_TOKEN": "zs-1", "ZENODO_ANYTHING": "z", "GH_TOKEN": "g", "GITHUB_PAT": "g",
              "NETLIFY_PAT": "n", "NETLIFY_AUTH_TOKEN": "n", "MY_API_KEY": "k", "SOME_SECRET": "s", "DB_PASSWORD": "p",
              "AWS_ACCESS_KEY_ID": "a", "ORCID_KEY": "o", "FOO_TOKEN": "t", "gh_token": "g"}
KEPT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/x", "LANG": "C", "TMPDIR": "/tmp", "TEXMFHOME": "/tex", "PYTHONPATH": "/py",
            "LC_ALL": "C"}


class ChildrenNeverSeeASecret(unittest.TestCase):
    def test_N7_child_env_drops_every_secret_looking_name_and_keeps_the_rest(self):
        env = {**SECRET_ENV, **KEPT_ENV}
        out = pc.child_env(env)
        self.assertEqual(out, KEPT_ENV)

    def test_N7_every_subprocess_call_of_the_battery_passes_a_scrubbed_env(self):
        seen = []

        def spy(*a, **k):
            seen.append(k.get("env"))
            return subprocess.CompletedProcess(a, 0, "", "")
        with mock.patch.dict(os.environ, {**SECRET_ENV, **KEPT_ENV}), mock.patch.object(pc.subprocess, "run", spy):
            pc.sh("true")
            pc.sh_raw("true")
            pc.read_pdf_metadata(Path("/x.pdf"))
        self.assertEqual(len(seen), 3)
        for env in seen:
            self.assertIsInstance(env, dict)
            self.assertFalse(set(env) & set(SECRET_ENV), env)
            self.assertEqual({k: v for k, v in env.items() if k in KEPT_ENV}, KEPT_ENV)

    def test_N7_a_real_child_of_the_battery_does_not_see_the_token(self):
        with mock.patch.dict(os.environ, {"ZENODO_TOKEN": "visible-token-123", "GH_TOKEN": "gh-123", "TEXMFHOME": "ok"}):
            rc, out = pc.sh("env")
        self.assertIn("TEXMFHOME=ok", out)
        self.assertNotIn("visible-token-123", out)
        self.assertNotIn("ZENODO_TOKEN", out)
        self.assertNotIn("GH_TOKEN", out)

    def test_N7_every_subprocess_call_of_both_modules_names_env(self):
        for name in ("predeposit_check.py", "zenodo_publish.py"):
            tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and ast.unparse(n.func) in ("subprocess.run", "subprocess.Popen", "subprocess.check_output",
                                                 "subprocess.call", "subprocess.check_call", "os.system", "os.popen")]
            self.assertGreaterEqual(len(calls), 3, name)
            for call in calls:
                self.assertIn("env", [k.arg for k in call.keywords], (name, call.lineno))

    def test_N7_the_git_calls_of_zenodo_publish_run_without_any_zenodo_variable(self):
        seen = []

        def spy(*a, **k):
            seen.append(k.get("env"))
            return subprocess.CompletedProcess(a, 0, "main\n", "")
        root, paper = workspace()
        with mock.patch.dict(os.environ, {**SECRET_ENV, **KEPT_ENV}), mock.patch.object(zp.subprocess, "run", spy):
            zp.get_git_branch(paper)
            zp.git_branch_raw(paper)
            zp.resolve_code_url(paper, {})
        self.assertGreaterEqual(len(seen), 3)
        for env in seen:
            self.assertFalse(set(env) & set(SECRET_ENV), env)
            self.assertIn("PATH", env)


# ── N-8 / N-9: a failed write is reported, never a traceback, and the temporary file is kept ───────────

class FailedWritesAreReported(unittest.TestCase):
    def test_N9_a_failed_replace_names_the_kept_temporary_file_and_leaves_the_target_alone(self):
        d = Path(tempfile.mkdtemp()).resolve()
        target = d / "zenodo.json"
        target.write_text("old")
        with mock.patch.object(os, "replace", side_effect=OSError(18, "Invalid cross-device link")):
            with self.assertRaises(zp.AtomicWriteError) as cm:
                zp._atomic_write_text(target, "new")
        err = cm.exception
        self.assertEqual(target.read_text(), "old")
        self.assertIsNotNone(err.tmp)
        self.assertTrue(Path(err.tmp).exists())                                # kept, not deleted
        self.assertEqual(Path(err.tmp).read_text(), "new")
        self.assertEqual(Path(err.tmp).parent, d)                              # next to the target: a rename is atomic only there
        self.assertIn(str(err.tmp), str(err))
        self.assertIn("deletes nothing", str(err))
        self.assertIsInstance(err, OSError)

    def test_N9_a_failed_write_before_any_file_exists_says_so(self):
        d = Path(tempfile.mkdtemp()).resolve()
        with mock.patch("builtins.open", side_effect=OSError(13, "Permission denied")):
            with self.assertRaises(zp.AtomicWriteError) as cm:
                zp._atomic_write_text(d / "zenodo.json", "x")
        self.assertIsNone(cm.exception.tmp)
        self.assertIn("No temporary file remains", str(cm.exception))

    def test_N9_a_reconcile_whose_config_write_fails_is_a_message_with_the_temp_path_and_no_traceback(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper).path
        snap = Bytes(paper / "zenodo.json", jp)
        real = os.replace

        def failing(a, b):
            if Path(b).name == "zenodo.json":
                raise OSError(28, "No space left on device")
            return real(a, b)
        with mock.patch.object(os, "replace", failing):
            code, out, err = reconcile(jp, *published_args(), root=root)
        self.assertIsInstance(code, str)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("LEFT in place", code)
        self.assertIn(str(paper), code)
        self.assertIn("still blocks", code)
        self.assertTrue(snap.unchanged())
        stray = list(paper.glob(".zenodo.json.*.tmp"))
        self.assertEqual(len(stray), 1)                                         # kept: this tool deletes nothing
        self.assertIn(str(stray[0]), code)

    def test_N9_a_failed_journal_write_after_the_config_is_saved_says_so_and_the_same_command_can_be_repeated(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        jp = make_blocking_journal(root, paper).path
        real = os.replace

        def failing(a, b):
            if Path(b) == jp:
                raise OSError(5, "I/O error")
            return real(a, b)
        with mock.patch.object(os, "replace", failing):
            code, out, err = reconcile(jp, *published_args(), root=root)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("ALREADY holds the identifiers", code)
        self.assertIn("still blocks", code)
        self.assertEqual(json.loads(jp.read_text())["outcome"], "UNRECONCILED")
        self.assertEqual(json.loads((paper / "zenodo.json").read_text())["zenodo_id"], 5)
        code, _, _ = reconcile(jp, *published_args(), root=root)               # idempotent: an identical value is accepted
        self.assertIsNone(code, code)
        self.assertEqual(json.loads(jp.read_text())["outcome"], "PUBLISHED-RECONCILED")

    def test_N9_a_journal_write_failure_stays_loud_but_is_not_a_traceback_in_update(self):
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
        with mock.patch.object(os, "replace", side_effect=OSError(5, "I/O")):
            j.update(note="n")
        self.assertTrue(j.degraded)
        self.assertEqual(len(list(d.glob(".*.tmp"))), 1)                        # kept and named by the error message


# ── N-11: documentation says what the code does ─────────────────────────────────────────────────────

class DocsMatchTheCode(unittest.TestCase):
    README = (TOOLS / "README.md").read_text(encoding="utf-8")
    PILOT = (TOOLS / "pilot" / "README-PILOT.md").read_text(encoding="utf-8")

    def test_no_document_tells_the_operator_to_quarantine_a_journal(self):
        help_text = zp.build_parser().format_help()
        for name, text in (("README", self.README), ("README-PILOT", self.PILOT), ("docstring", zp.__doc__), ("help", help_text)):
            self.assertNotIn("--reconcile JOURNAL --quarantine", text, name)
            self.assertNotIn("--quarantine --reason", text, name)
            self.assertNotIn("*.quarantined", text, name)
            self.assertNotIn("quarantine them", text, name)
            self.assertNotIn("renames it", text, name)
            self.assertIn("--lift-block", text, name)
            self.assertIn("--backup-journal", text, name)

    def test_the_wordings_that_review_21_called_wrong_are_gone(self):
        help_text = " ".join(zp.build_parser().format_help().split())
        self.assertNotIn("unreadable one", help_text)
        self.assertNotIn("for an unreadable journal", help_text)
        self.assertNotIn("whatever the value", self.README)
        self.assertNotIn("none matching: the web entry is appended", help_text)

    def test_redaction_is_described_by_value_and_by_field_name_with_its_limits(self):
        flat = " ".join(self.README.split())
        for needle in ("by value and by field name", "shorter than 8 characters", "differently named", "percent escapes"):
            self.assertIn(needle, flat, needle)

    def test_the_no_go_statement_and_the_unproven_list_are_kept(self):
        for name, text in (("README", self.README), ("docstring", zp.__doc__), ("help", zp.build_parser().format_help())):
            self.assertIn("PRODUCTION USE REMAINS NO-GO UNTIL A SUCCESSFUL SANDBOX PILOT", text, name)
            self.assertIn("UNPROVEN", text, name)
        for item in ("Cleanup semantics", "links.latest_draft", "Header presence", "Redirects", "Journal durability",
                     "The whole-live-metadata PUT"):
            self.assertIn(item, self.README, item)
        self.assertIn("NO-GO", self.PILOT)

    def test_the_doi_rule_and_the_related_identifier_rules_are_documented(self):
        flat = " ".join(self.README.split())
        for needle in ("10.5281/zenodo.<concept id>", "never replaced", "--replace-related-identifier"):
            self.assertIn(needle, flat, needle)

    def test_the_mutant_counts_in_the_readme_are_the_ones_computed_from_the_script(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import importlib
        mc = importlib.import_module("mutation_check")
        total, new = len(mc.MUTANTS), len(mc.NEW_IDS)
        flat = " ".join(self.README.split())
        self.assertIn(f"{total} mutants", flat)
        self.assertIn(f"{new} new", flat)

    def test_the_lift_block_help_and_the_blocking_message_are_consistent(self):
        flat = " ".join(zp.build_parser().format_help().split())
        for needle in ("--lift-block JOURNAL", "--backup-journal JOURNAL", "never renamed, edited or deleted",
                       "exactly these bytes"):
            self.assertIn(needle, flat, needle)


if __name__ == "__main__":
    unittest.main()
