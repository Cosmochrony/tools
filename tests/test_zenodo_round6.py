"""Offline tests for the sixth corrective pass (review round 22: F-1 .. F-13, survivors Z5 Z47 Z49 Z9 E1).

Every class names the finding it closes. No network: a socket guard fails any real connection; HTTP is the scripted
Sim / FakeSession / FakeClient.
"""
import ast
import copy
import hashlib
import itertools
import json
import os
import re
import shutil
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
from test_zenodo_round5 import entry, lift, decision_of, sha  # noqa: E402
from test_zenodo_round2 import new, run  # noqa: E402
from fakes import FakeClient  # noqa: E402

TOOLS = Path(__file__).resolve().parent.parent
SRC = (TOOLS / "zenodo_publish.py").read_text(encoding="utf-8")


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


# ── previous_web_url removed; the explicit replacement is restricted to the web entry's own role ─────────────

class ExplicitReplacementIsRoleRestricted(unittest.TestCase):
    def test_the_relation_is_compared_exactly_not_case_insensitively(self):
        # a named entry whose relation merely differs in case is not the web entry's role: refused, conservatively
        live = [entry("https://a.example/x", "issupplementedby")]
        with self.assertRaises(ValueError):
            zp.merge_related_web_url(live, ENTRY, "https://a.example/x")
        live = [entry("https://a.example/x", "IsSupplementedBy")]
        with self.assertRaises(ValueError):
            zp.merge_related_web_url(live, ENTRY, "https://a.example/x")

    def test_a_host_change_goes_through_the_explicit_replacement_of_the_old_web_entry(self):
        live = [entry("10.1/abc", "cites", "doi", None), entry("https://old.example/page"), entry("https://github.com/x/y")]
        out = zp.merge_related_web_url(live, ENTRY, "https://old.example/page")
        self.assertEqual(out, [live[0], ENTRY, live[2]])

    def test_there_is_no_previous_web_url_machinery_left(self):
        self.assertNotIn("previous_web_url", SRC)
        import inspect
        for fn in (zp.merge_related_web_url, zp.build_update_payload, zp.deposit_update):
            self.assertNotIn("previous_web_urls", inspect.signature(fn).parameters, fn.__name__)

    def test_the_cli_refuses_a_non_web_role_entry_named_by_the_operator(self):
        live = [entry("https://a.example/x", "isVersionOf")]
        c = LiveClient(live={**LIVE, "related_identifiers": live})
        root, paper, (code, _, _) = update_cli(c, "--set-web-url", "--replace-related-identifier", "https://a.example/x",
                                               "--yes-apply-diff", cfg={"web_url": "https://new.example"})
        self.assertIn("never replaceable", str(code))
        self.assertNotIn("put", c.names())


# ── F-2: the ENTIRE value of an Authorization / Proxy-Authorization field, whatever its scheme ─────────────

SCHEMES = ("Bearer", "Basic", "Digest", "Negotiate", "Token", "ApiKey", "OAuth", "GitHub", "AWS4-HMAC-SHA256", "NTLM",
           "Hawk", "MAC", "Foo", "")
CRED = "SECRETX9value"          # 13 characters, never registered in these tests

# (template, text that must survive the masking after the value)
AUTH_FORMS = [
    ("Authorization: {v}", ""),
    ("Authorization: {v}\r\nHost: h", "\r\nHost: h"),
    ("Authorization: {v}\nNext: y", "\nNext: y"),
    ("b'Authorization: {v}\\r\\nHost: h\\r\\n'", "\\r\\nHost: h\\r\\n'"),
    ("Authorization={v}&next=1", "&next=1"),
    ("?Authorization={v}&x=1", "&x=1"),
    ("Authorization = {v}", ""),
    ('Authorization="{v}" next', '" next'),
    ("Authorization='{v}' next", "' next"),
    ('{{"Authorization": "{v}", "next": 1}}', ', "next": 1}'),
    ("{{'Authorization': '{v}', 'next': 1}}", ", 'next': 1}"),
    ('curl -H "Authorization: {v}" -o out', '" -o out'),
    ('curl -H "Authorization={v}" -o out', '" -o out'),
    ("Proxy-Authorization: {v}\r\nHost: h", "\r\nHost: h"),
    ("proxy-authorization={v}&n=2", "&n=2"),
    ("AUTHORIZATION: {v}", ""),
]


class AuthorizationIsMaskedWholeWhateverTheScheme(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_F2_no_scheme_leaks_its_credential_in_any_form(self):
        for scheme in SCHEMES:
            value = f"{scheme} {CRED}" if scheme else CRED
            for template, _ in AUTH_FORMS:
                text = template.format(v=value)
                out = zp.redact(text)
                self.assertNotIn(CRED, out, (text, out))
                self.assertNotIn("SECRETX9", out, (text, out))
                if scheme:
                    self.assertNotIn(scheme + " [", out.replace("Bearer [", "").replace("Basic [", ""), (text, out))

    def test_F2_the_text_after_the_value_is_kept_and_the_field_name_too(self):
        for scheme in SCHEMES:
            value = f"{scheme} {CRED}" if scheme else CRED
            for template, after in AUTH_FORMS:
                text = template.format(v=value)
                out = zp.redact(text)
                self.assertTrue(out.endswith(after), (text, out))
                self.assertIn("authorization", out.lower(), (text, out))

    def test_F2_the_value_is_replaced_as_one_piece(self):
        for text, expect in (
                ("Authorization=ApiKey X1&next=1", "Authorization=[REDACTED]&next=1"),
                ("Authorization=OAuth X1&next=1", "Authorization=[REDACTED]&next=1"),
                ("Authorization=GitHub X1", "Authorization=[REDACTED]"),
                ("Authorization: ApiKey X1", "Authorization: [REDACTED]"),
                ("Authorization: X1", "Authorization: [REDACTED]"),
                ("Authorization=X1", "Authorization=[REDACTED]"),
                ('{"Authorization": "ApiKey X1", "n": 1}', '{"Authorization": "[REDACTED]", "n": 1}'),
                ("Authorization=AWS4-HMAC-SHA256 Credential=AKIA/2020/x, SignedHeaders=host, Signature=abc&z=1",
                 "Authorization=[REDACTED]&z=1"),
                ("Authorization: AWS4-HMAC-SHA256 Credential=AKIA/2020/x, SignedHeaders=host, Signature=abc",
                 "Authorization: [REDACTED]"),
                ('Authorization: Digest username="u", realm="r", response="abc"\nNext: y', "Authorization: [REDACTED]\nNext: y"),
                ('Authorization=Digest username="u", realm="r", response="abc"&n=1', "Authorization=[REDACTED]&n=1")):
            self.assertEqual(zp.redact(text), expect, text)

    def test_F2_every_order_of_the_passes_gives_the_same_result(self):
        for scheme in SCHEMES:
            value = f"{scheme} {CRED}" if scheme else CRED
            for template, _ in AUTH_FORMS[:12]:
                text = template.format(v=value)
                results = {zp.redact(text, passes=order) for order in itertools.permutations(zp._PATTERN_PASSES)}
                for r in results:
                    self.assertNotIn(CRED, r, (text, r))

    def test_F2_a_following_field_is_not_eaten_by_a_name_that_is_not_sensitive(self):
        self.assertEqual(zp.redact("foo: bar Authorization: ApiKey X1"), "foo: bar Authorization: [REDACTED]")
        self.assertEqual(zp.redact("a=1 token=abc b=2"), "a=1 token=[REDACTED] b=2")

    def test_F2_bearer_with_a_colon_is_masked_and_keeps_its_colon(self):
        self.assertEqual(zp.redact("Bearer: X1 and more"), "Bearer: [REDACTED] and more")
        self.assertEqual(zp.redact("Bearer X1"), "Bearer [REDACTED]")
        self.assertEqual(zp.redact("x-header: Bearer: abc"), "x-header: Bearer: [REDACTED]")

    def test_F2_a_generic_credential_name_still_takes_a_scheme_word_in_front_of_its_value(self):
        for sch in ("Bearer", "Basic", "Digest", "Negotiate", "Token"):
            self.assertEqual(zp.redact(f"access_token={sch} X1&n=1"), "access_token=[REDACTED]&n=1", sch)

    def test_F2_a_registered_secret_inside_an_authorization_value_is_masked_too(self):
        zp.register_secret("REGISTEREDvalue1")
        out = zp.redact("Authorization: ApiKey REGISTEREDvalue1")
        self.assertNotIn("REGISTERED", out)


# ── F-3: compound credential names, by a suffix / token-boundary rule ─────────────────────────────────────

MASKED_NAMES = (
    # last word-component is token / secret / password / passwd / passphrase / credential(s) / authorization /
    # cookie / signature
    "token", "access_token", "refresh_token", "auth_token", "id_token", "bearer_token", "jwt_token", "authToken",
    "idToken", "x-amz-security-token", "X-CSRF-Token", "X-Auth-Token", "Private-Token", "x_access_token",
    "secret", "api_secret", "client_secret", "clientSecret", "apiSecret", "app.secret", "DB_PASSWORD", "password", "passwd",
    "db_passwd", "gpg_passphrase", "credential", "credentials", "aws_credentials", "authorization", "Authorization",
    "Proxy-Authorization", "x_authorization", "cookie", "Cookie", "Set-Cookie", "session_cookie", "sessionCookie",
    "signature", "request_signature", "X-Signature",
    # contains apikey, or (access|secret|private) followed by key
    "apikey", "api_key", "API_KEY", "x-api-key", "X-Api-Key", "apiKey", "ApiKey", "api_key_id", "private_key",
    "ssh_private_key", "privateKey", "PRIVATE_KEY", "secret_key", "AWS_SECRET_ACCESS_KEY", "access_key",
    "accessKeyId", "access_key_id", "AWSSecretAccessKey",
    # <qualifier>_key
    "auth_key", "signing_key", "encryption_key", "ssh_key", "session_key", "master_key", "client_key", "license_key",
    # earlier names written without a separator
    "accesstoken", "refreshtoken", "clientsecret",
)
NOT_MASKED_NAMES = (
    "bib_key", "key", "Key", "zenodo_id", "keywords", "keyword", "publication_date", "token_count", "tokens", "max_tokens",
    "monkey", "donkey", "primary_key", "sort_key", "cache_key", "hotkey", "foreign_key", "author", "authors", "authority",
    "authorization_url", "cookie_count", "cookies", "signature_algorithm", "password_policy", "secretary", "secrets_count",
    "doi", "title", "web_url", "version", "paper_dir", "config_sha256", "checked_account", "journal", "reason", "zenodo_token_count",
    "public_key", "bibkey", "keys", "tokenizer", "passwords", "id", "name", "description", "concept_id",
)


class CompoundCredentialNames(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_F3_the_name_rule_itself_in_both_directions(self):
        for name in MASKED_NAMES:
            self.assertTrue(zp.is_credential_name(name), name)
            self.assertTrue(zp.is_sensitive_name(name), name)
        for name in NOT_MASKED_NAMES:
            self.assertFalse(zp.is_credential_name(name), name)
            self.assertFalse(zp.is_sensitive_name(name), name)

    def test_F3_words_are_split_on_separators_and_camel_case(self):
        self.assertEqual(zp.credential_name_words("authToken"), ["auth", "token"])
        self.assertEqual(zp.credential_name_words("AWSSecretAccessKey"), ["aws", "secret", "access", "key"])
        self.assertEqual(zp.credential_name_words("x-amz-security-token"), ["x", "amz", "security", "token"])
        self.assertEqual(zp.credential_name_words("API_KEY"), ["api", "key"])
        self.assertEqual(zp.credential_name_words("app.secret"), ["app", "secret"])
        self.assertEqual(zp.credential_name_words("my token"), ["my", "token"])

    def test_F3_a_masked_name_hides_its_value_in_every_text_form(self):
        for name in MASKED_NAMES:
            for text, expect in (
                    (f"{name}=VALUE1", f"{name}=[REDACTED]"),
                    (f"{name}: VALUE1", f"{name}: [REDACTED]"),
                    (f"{name} = VALUE1", f"{name} = [REDACTED]"),
                    (f"?a=1&{name}=VALUE1&b=2", f"?a=1&{name}=[REDACTED]&b=2"),
                    (f'{{"{name}": "VALUE1", "n": 1}}', f'{{"{name}": "[REDACTED]", "n": 1}}'),
                    (f"{{'{name}': 'VALUE1', 'n': 1}}", f"{{'{name}': '[REDACTED]', 'n': 1}}"),
                    (f'{name}="VALUE1" n', f'{name}="[REDACTED]" n'),
                    ):
                if name.lower() in ("zenodo_token",):
                    continue
                self.assertEqual(zp.redact(text), expect, text)

    def test_F3_a_name_that_is_not_masked_keeps_its_value_in_every_text_form(self):
        for name in NOT_MASKED_NAMES:
            for text in (f"{name}=VALUE1", f"{name}: VALUE1", f"?a=1&{name}=VALUE1&b=2", f'{{"{name}": "VALUE1"}}',
                         f"{{'{name}': 'VALUE1'}}", f"{name} = {{Beau2026}}"):
                self.assertEqual(zp.redact(text), text, text)

    def test_F3_percent_encoded_compound_names_are_masked(self):
        for text, expect in (("auth%5Ftoken=ABC&q=1", "auth_token=[REDACTED]&q=1"),
                             ("id%5Ftoken%3DABC&q=1", "id_token%3D[REDACTED]&q=1"),
                             ("x-amz-security%2Dtoken: ABC", "x-amz-security-token: [REDACTED]"),
                             ("api%5Fsecret=ABC", "api_secret=[REDACTED]"),
                             ("session%5Fcookie: ABC", "session_cookie: [REDACTED]"),
                             ("private%5Fkey=ABC", "private_key=[REDACTED]"),
                             ("X%2DApi%2DKey%3A ABC", "X-Api-Key%3A [REDACTED]"),
                             ("app%2Esecret=ABC", "app.secret=[REDACTED]"),
                             ("bib%5Fkey=Beau", "bib_key=Beau")):
            out = zp.redact(text)
            self.assertNotIn("ABC", out, text)
            self.assertEqual(out, expect, text)

    def test_F3_dictionary_keys_of_a_structure(self):
        data = {name: "VALUE1" for name in MASKED_NAMES}
        data.update({name: "keep" for name in NOT_MASKED_NAMES})
        out = zp.redact_obj({"outer": data, "list": [dict(data)]})
        for name in MASKED_NAMES:
            self.assertEqual(out["outer"][name], "[REDACTED]", name)
            self.assertEqual(out["list"][0][name], "[REDACTED]", name)
        for name in NOT_MASKED_NAMES:
            self.assertEqual(out["outer"][name], "keep", name)

    def test_F3_a_text_with_several_fields_masks_only_the_credential_ones(self):
        self.assertEqual(zp.redact("bib_key=Beau2026 auth_token=abc zenodo_id=5"), "bib_key=Beau2026 auth_token=[REDACTED] zenodo_id=5")
        self.assertEqual(zp.redact("token_count=5 max_tokens: 10 key=Foo"), "token_count=5 max_tokens: 10 key=Foo")
        self.assertEqual(zp.redact("bib_key: Beau2026, api_secret: s1"), "bib_key: Beau2026, api_secret: [REDACTED]")

    def test_F3_the_two_token_variables_stay_readable_in_prose_and_masked_in_assignments(self):
        prose = "never falls back to ZENODO_TOKEN: the production token is never sent to the sandbox"
        self.assertEqual(zp.redact(prose), prose)
        self.assertEqual(zp.redact("ZENODO_SANDBOX_TOKEN: used"), "ZENODO_SANDBOX_TOKEN: used")
        self.assertEqual(zp.redact("ZENODO_TOKEN=abc"), "ZENODO_TOKEN=[REDACTED]")
        self.assertEqual(zp.redact("ZENODO_SANDBOX_TOKEN = abc"), "ZENODO_SANDBOX_TOKEN = [REDACTED]")
        self.assertEqual(zp.redact("MY_ZENODO_TOKEN=abc"), "MY_ZENODO_TOKEN=[REDACTED]")

    def test_F3_the_journal_stays_valid_json_and_keeps_its_non_credential_keys(self):
        d = Path(tempfile.mkdtemp()).resolve()
        j = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {"auth_token": "VALUE1", "bib_key": "Beau"}, "1.0")
        j.update(note="auth_token=VALUE1 bib_key=Beau")
        data = json.loads(j.path.read_text())
        self.assertEqual(data["intent"], {"auth_token": "[REDACTED]", "bib_key": "Beau"})
        self.assertIn("bib_key=Beau", data["notes"][-1])
        self.assertNotIn("VALUE1", j.path.read_text())
        self.assertEqual(zp.journal_identity_problems(data), [])

    def test_F3_every_order_of_the_passes_gives_a_text_without_the_value(self):
        for name in ("auth_token", "api_secret", "x-amz-security-token", "private_key", "session_cookie", "accessKeyId"):
            for text in (f"{name}=VALUE1", f"{name}: VALUE1", f'{{"{name}": "VALUE1"}}'):
                for order in itertools.permutations(zp._PATTERN_PASSES):
                    self.assertNotIn("VALUE1", zp.redact(text, passes=order), (text, [f.__name__ for f in order]))

    def test_F3_a_long_text_is_masked_in_linear_time(self):
        import time
        text = "a.b-c=d: x=\"y\" authorization: foo bar=\"baz " * 5000
        t0 = time.time()
        zp.redact(text)
        self.assertLess(time.time() - t0, 5.0)


class RedactionEdgeForms(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_an_empty_value_does_not_eat_the_next_line(self):
        self.assertEqual(zp.redact("Authorization:\nNext: y"), "Authorization:[REDACTED]\nNext: y")
        self.assertEqual(zp.redact("auth_token=\nNext: y"), "auth_token=[REDACTED]\nNext: y")

    def test_json_text_escaped_inside_a_string_is_masked(self):
        text = '{\\"Authorization\\": \\"ApiKey X1\\", \\"n\\": 1}'
        out = zp.redact(text)
        self.assertNotIn("X1", out)
        self.assertIn('\\"n\\": 1', out)
        text2 = json.dumps({"msg": json.dumps({"auth_token": "X1", "bib_key": "B"})})
        out2 = zp.redact(text2)
        self.assertNotIn("X1", out2)
        self.assertIn("bib_key", out2)

    def test_a_value_with_a_balanced_quoted_piece_is_one_value(self):
        out = zp.redact('Authorization: Digest username="u", response="abc" tail')
        self.assertEqual(out, "Authorization: [REDACTED]")
        out = zp.redact('x Authorization=Digest realm="r", nonce="n"&y=1')
        self.assertEqual(out, "x Authorization=[REDACTED]&y=1")

    def test_the_credential_of_a_bare_scheme_stops_at_an_escaped_line_end(self):
        self.assertEqual(zp.redact("Bearer X1\\r\\nHost: h"), "Bearer [REDACTED]\\r\\nHost: h")

    def test_the_value_of_a_non_authorization_name_stops_at_the_closing_quote(self):
        self.assertEqual(zp.redact("{'cookie': 'a=b; c=d', 'n': 1}"), "{'cookie': '[REDACTED]', 'n': 1}")
        self.assertEqual(zp.redact('say "token: abc" now'), 'say "token: [REDACTED]" now')


# ── F-1 / E1: exception text has ONE choke point; the one route that bypassed it is closed ───────────────────

TOKEN_IN_EXC = "TOK-exc-SECRET-0123456789"


class ExceptionTextChokePoint(unittest.TestCase):
    """E1 ("exception text printed unredacted", the ``redact(exc)`` argument of the print in abort_and_cleanup) is
    EQUIVALENT as a mutation of that argument: the module's ``print`` redacts again, and redact is idempotent and
    context-free for the text that follows a fixed prefix. What matters is that every route by which an exception
    text can leave the process goes through ``print`` / ``die`` / the parser / the journal serialiser / the
    redacting excepthook. These tests pin each route (and the one that was open: an exception escaping ``main()``
    when the module is embedded, as sandbox_launch.py does)."""

    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()
        zp.register_secret(TOKEN_IN_EXC)

    def tearDown(self):
        self._p.stop()

    def test_E1_the_redaction_of_the_argument_is_redundant_with_the_one_of_print(self):
        prefix = "!! Error: RuntimeError: "
        corpus = [f"boom {TOKEN_IN_EXC}", f"{TOKEN_IN_EXC}", f"a{TOKEN_IN_EXC}b", "Authorization: Bearer abc",
                  "token=abc&x=1", "Bearer abc.def", "plain", "", f"x\n{TOKEN_IN_EXC}\ny", "'secret': 'v'",
                  f"HTTP 400 {{'Authorization': 'ApiKey {TOKEN_IN_EXC}'}}", "access%5Ftoken=ZZZ"]
        for s in corpus:
            self.assertEqual(zp.redact(prefix + zp.redact(s)), zp.redact(prefix + s), s)
            self.assertEqual(zp.redact(zp.redact(s)), zp.redact(s), s)

    def test_E1_a_failing_deposit_prints_no_registered_token_on_either_stream_nor_in_the_journal(self):
        d = Path(tempfile.mkdtemp()).resolve()
        for fail_at in ("newversion", "delete_file", "upload", "put", "publish"):
            c = FakeClient(fail_at=fail_at, fail_exc=RuntimeError(f"HTTP 500 Authorization: ApiKey {TOKEN_IN_EXC} body {TOKEN_IN_EXC}"))
            c.journal = zp.Journal.create(d, Path("/x/paper"), "zenodo.org", False, {}, "1.0")
            code, out, err = run(new, c)
            for stream in (str(code), out, err, c.journal.path.read_text()):
                self.assertNotIn(TOKEN_IN_EXC, stream, fail_at)
            self.assertIn("[REDACTED]", err, fail_at)

    def test_E1_a_token_in_an_exception_text_that_the_module_print_would_not_catch_is_still_masked_by_the_explicit_call(self):
        # the explicit redact(exc) of the print site is a second line of defence: it yields the same output
        c = FakeClient(fail_at="upload", fail_exc=RuntimeError(f"x {TOKEN_IN_EXC}"))
        code, out, err = run(new, c)
        self.assertNotIn(TOKEN_IN_EXC, err + out + str(code))

    def test_E1_the_module_print_itself_masks_on_stderr_and_stdout_whatever_the_call_site_did(self):
        # no explicit redact() at the call site: only the choke point can mask here
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            zp.print(f"!! Error: RuntimeError: {TOKEN_IN_EXC} and Authorization: ApiKey Q1w2e3", file=sys.stderr)
            zp.print(f"!! Error: RuntimeError: {TOKEN_IN_EXC} and Authorization: ApiKey Q1w2e3")
            zp.print(f"plain {TOKEN_IN_EXC}", file=sys.stderr)
        for name, buf in (("stderr", err), ("stdout", out)):
            self.assertNotIn(TOKEN_IN_EXC, buf.getvalue(), name)
            self.assertNotIn("Q1w2e3", buf.getvalue(), name)
            self.assertIn("[REDACTED]", buf.getvalue(), name)

    def test_E1_main_installs_the_redacting_excepthook(self):
        with mock.patch.object(sys, "excepthook", sys.__excepthook__):
            run_main(["--dry-run", "--help"], {}, Path(tempfile.mkdtemp()))
            self.assertIs(sys.excepthook, zp._redacting_excepthook)

    def test_E1_an_exception_escaping_main_of_an_embedding_script_is_masked(self):
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(TOOLS)!r})\n"
            "import zenodo_publish as zp\n"
            "def boom(sandbox, environ=None):\n"
            "    raise RuntimeError('header was Authorization: ApiKey ' + os.environ['ZENODO_SANDBOX_TOKEN'] + ' and ' + os.environ['ZENODO_SANDBOX_TOKEN'])\n"
            "zp.resolve_token = boom\n"
            "zp.main(['paper', '--sandbox', '--sandbox-state', '/nonexistent/state.json'])\n")
        env = {"PATH": os.environ.get("PATH", ""), "ZENODO_SANDBOX_TOKEN": TOKEN_IN_EXC}
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RuntimeError", r.stderr)
        self.assertNotIn(TOKEN_IN_EXC, r.stdout + r.stderr)
        self.assertIn("[REDACTED]", r.stderr)

    def test_E1_the_launcher_and_the_harness_print_exit_codes_through_redact(self):
        for name in ("pilot/sandbox_launch.py", "pilot/inject_failures.py"):
            text = (TOOLS / name).read_text(encoding="utf-8")
            self.assertIn("zp.redact(", text, name)

    def test_E1_every_raw_exception_print_site_of_the_module_uses_the_module_print_die_or_the_parser(self):
        tree = ast.parse(SRC)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in ("traceback.print_exc", "traceback.print_exception", "sys.stderr.write", "sys.stdout.write",
                            "os.write", "warnings.warn", "parser.print_help", "logging.error", "logging.exception",
                            "logging.warning"):
                    bad.append((node.lineno, name))
                if name == "SystemExit" and node.args and not (isinstance(node.args[0], ast.Name) and node.args[0].id == "status"):
                    bad.append((node.lineno, "SystemExit(message)"))
        self.assertEqual(bad, [])


# ── F-4: the environment of every child is an ALLOW-LIST ──────────────────────────────────────────────

# the 26 secret-looking names the reviewer found passing through the earlier deny-list (REVIEW-TOOL-5 T01)
REVIEWER_26 = ("SSH_PRIVATE_KEY", "PRIVATE_KEY", "STRIPE_KEY", "SENTRY_KEY", "SESSION_COOKIE", "COOKIE", "AUTH", "HTTP_AUTH",
               "NPM_CONFIG__AUTH", "OAUTH_BEARER", "BEARER", "JWT", "CI_JOB_JWT", "GPG_PASSPHRASE", "PASSPHRASE", "PGPASS",
               "MAIL_PASS", "LOGIN_PASS", "HTTPS_PROXY", "http_proxy", "PIP_INDEX_URL", "DOCKER_CONFIG", "KUBECONFIG",
               "GIT_ASKPASS", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK")
# further names the earlier deny-list caught, and names nobody would think of
OTHER_SECRETS = ("ZENODO_TOKEN", "ZENODO_SANDBOX_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "NETLIFY_PAT", "ORCID_KEY", "AWS_SECRET_ACCESS_KEY",
                 "DB_PASSWORD", "MY_API_KEY", "SOME_SECRET", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "NO_PROXY", "GIT_CONFIG_PARAMETERS",
                 "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_TRACE_CURL", "SSL_CERT_FILE", "DYLD_LIBRARY_PATH",
                 "LD_LIBRARY_PATH", "LD_PRELOAD", "NPM_TOKEN", "PYPI_PASSWORD", "CUSTOM_BANANA", "X_NOBODY_THOUGHT_OF_THIS",
                 "PYTHONSECRET_X", "TEXMF_SECRET", "TEXMFSECRET", "LC_SECRET", "GIT_SECRET_THING", "GIT_PRIVATE_THING", "VIRTUAL_ENV_KEY")
SAFE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/x", "USER": "u", "LOGNAME": "u", "SHELL": "/bin/sh", "TERM": "xterm",
            "TZ": "UTC", "TMPDIR": "/tmp", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C", "TEXINPUTS": "/t:", "BIBINPUTS": "/b:",
            "BSTINPUTS": "/s:", "TEXMFHOME": "/m", "TEXMFVAR": "/v", "TEXMFCONFIG": "/c", "TEXMFCACHE": "/k",
            "SOURCE_DATE_EPOCH": "0", "FORCE_SOURCE_DATE": "1", "PYTHONPATH": "/py", "PYTHONHOME": "/ph", "PYTHONUTF8": "1",
            "VIRTUAL_ENV": "/venv", "GIT_DIR": "/g", "GIT_CONFIG_NOSYSTEM": "1"}


class ChildEnvironmentIsAnAllowList(unittest.TestCase):
    def test_F4_none_of_the_reviewers_26_secret_looking_names_reaches_a_child(self):
        env = {**SAFE_ENV, **{n: "visible-secret-value" for n in REVIEWER_26}}
        out = pc.child_env(env)
        self.assertEqual(set(REVIEWER_26) & set(out), set())
        self.assertEqual(out, SAFE_ENV)

    def test_F4_no_other_secret_looking_or_custom_name_reaches_a_child(self):
        env = {**SAFE_ENV, **{n: "visible-secret-value" for n in OTHER_SECRETS}}
        out = pc.child_env(env)
        self.assertEqual(set(OTHER_SECRETS) & set(out), set())
        self.assertEqual(out, SAFE_ENV)

    def test_F4_a_custom_named_variable_set_in_the_environment_is_not_passed(self):
        with mock.patch.dict(os.environ, {"MY_COSMOCHRONY_CUSTOM_7": "custom-value-123", "KEEPME": "ok"}):
            rc, out = pc.sh("env")
            rc2, out2 = pc.sh_raw("env")
        for text in (out, out2):
            self.assertNotIn("MY_COSMOCHRONY_CUSTOM_7", text)
            self.assertNotIn("custom-value-123", text)
            self.assertNotIn("KEEPME", text)

    def test_F4_real_children_of_the_battery_see_exactly_the_allowed_names(self):
        poisoned = {n: "visible-secret-value" for n in REVIEWER_26 + OTHER_SECRETS}
        with mock.patch.dict(os.environ, {**poisoned, "TEXMFHOME": "/m", "SOURCE_DATE_EPOCH": "0"}):
            rc, out = pc.sh("env")
        seen = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
        # the shell itself may add PWD, SHLVL, _ and OLDPWD; nothing else
        self.assertEqual(seen - pc.CHILD_ENV_ALLOW - {"PWD", "SHLVL", "_", "OLDPWD"}, set(), seen)
        self.assertIn("TEXMFHOME", seen)
        self.assertNotIn("visible-secret-value", out)

    def test_F4_the_allow_list_is_explicit_and_has_no_secret_looking_name(self):
        self.assertIsInstance(pc.CHILD_ENV_ALLOW, frozenset)
        bad = re.compile(r"(?i)token|secret|passw|pass$|key|cred|auth|cookie|jwt|bearer|proxy|ssh|askpass|session|ZENODO|^GH_|^GITHUB|NETLIFY|ORCID|AWS")
        for name in pc.CHILD_ENV_ALLOW:
            self.assertFalse(bad.search(name), name)
        for needed in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "TMPDIR", "LANG", "LC_ALL", "TEXINPUTS",
                       "BIBINPUTS", "BSTINPUTS", "TEXMFHOME", "TEXMFVAR", "TEXMFCONFIG", "TEXMFCACHE", "SOURCE_DATE_EPOCH",
                       "FORCE_SOURCE_DATE", "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "VIRTUAL_ENV"):
            self.assertIn(needed, pc.CHILD_ENV_ALLOW, needed)
        for never in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "GIT_SSH_COMMAND", "HTTPS_PROXY", "https_proxy", "DYLD_LIBRARY_PATH",
                      "LD_PRELOAD", "SSL_CERT_FILE", "PIP_INDEX_URL", "KUBECONFIG", "DOCKER_CONFIG"):
            self.assertNotIn(never, pc.CHILD_ENV_ALLOW, never)

    def test_F4_every_allowed_name_that_is_set_is_passed_with_its_value(self):
        self.assertEqual(pc.child_env(dict(SAFE_ENV)), SAFE_ENV)
        self.assertEqual(pc.child_env({}), {})

    def test_F4_the_git_children_of_zenodo_publish_use_the_same_kind_of_list_and_agree_on_the_git_names(self):
        env = {**SAFE_ENV, **{n: "visible-secret-value" for n in REVIEWER_26 + OTHER_SECRETS}}
        out = zp.git_env(env)
        self.assertEqual(set(REVIEWER_26 + OTHER_SECRETS) & set(out), set())
        self.assertEqual(out["GIT_DIR"], "/g")
        self.assertEqual({k for k in zp._GIT_ENV_KEEP if k.startswith("GIT_")},
                         {k for k in pc.CHILD_ENV_ALLOW if k.startswith("GIT_")})

    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("bibtex"), "TeX Live is not installed")
    def test_F4_pdflatex_and_bibtex_still_build_a_real_paper_under_the_allow_list(self):
        d = Path(tempfile.mkdtemp()).resolve()
        (d / "main.tex").write_text(r"""\documentclass{article}
\title{A small paper}\author{A. Author}\date{Version 1.0}
\begin{document}\maketitle
\begin{abstract}An abstract.\end{abstract}
\section{Intro}See \cite{Beau2026x}.
\bibliographystyle{plain}\bibliography{refs}
\end{document}
""")
        (d / "refs.bib").write_text("@article{Beau2026x, author={J. Beau}, title={{A title}}, journal={J.}, year={2026}}\n")
        poisoned = {n: "visible-secret-value" for n in REVIEWER_26 + OTHER_SECRETS}
        with mock.patch.dict(os.environ, poisoned):
            steps = [pc.sh(f"{cmd} > /dev/null 2>&1; echo $?", d)[1]
                     for cmd in ("pdflatex -interaction=nonstopmode -file-line-error main.tex", "bibtex main",
                                 "pdflatex -interaction=nonstopmode -file-line-error main.tex",
                                 "pdflatex -interaction=nonstopmode -file-line-error main.tex")]
            meta, err = pc.read_pdf_metadata(d / "main.pdf")
        self.assertEqual(steps, ["0", "0", "0", "0"], steps)
        self.assertTrue((d / "main.pdf").is_file())
        self.assertIsNone(err)
        self.assertEqual(meta.get("Pages"), "1")
        log = (d / "main.log").read_text(errors="replace")
        self.assertNotIn("Citation `Beau2026x' on page 1 undefined", log)
        self.assertNotIn("visible-secret-value", log)
        self.assertEqual(pc.grep_count(pc.UNDEF_RE, d / "main.log"), 0)


if __name__ == "__main__":
    unittest.main()
