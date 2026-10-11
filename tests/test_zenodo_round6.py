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


# ── F-9: lifting a block after a CONFIRMED publication never allows a new first publication ───────────────────

PUB = dict(concept_id="5", concept_doi=GOOD_DOI, record_id=301, doi="10.5281/zenodo.301")


def set_ids(paper, zenodo_id=5, doi=GOOD_DOI, **more):
    p = Path(paper) / "zenodo.json"
    cfg = json.loads(p.read_text())
    cfg.update(zenodo_id=zenodo_id, doi=doi, **more)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def drop_ids(paper):
    p = Path(paper) / "zenodo.json"
    cfg = json.loads(p.read_text())
    cfg.update(zenodo_id=None, doi=None)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


class LiftOfAPublishedJournal(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)

    def published_unsaved(self, moved=True, **extra):
        """PUBLISHED-UNSAVED whose paper directory moved (liftable kind 'directory'); returns (journal, new dir)."""
        j = make_blocking_journal(self.root, self.paper, outcome="PUBLISHED-UNSAVED", **{**PUB, **extra})
        if not moved:
            return j.path, self.paper
        new = self.paper.parent / "paper-moved"
        os.rename(self.paper, new)
        return j.path, new

    def lift_cli(self, jp, *extra, evidence=("--i-checked-the-account",)):
        return run_main(["--lift-block", str(jp), *evidence, "--reason", REASON, *extra], {}, self.root)

    def blocked_run(self, paper):
        f = Factory()
        code, out, err = run_main([str(paper), "--skip-checks"], PROD_ONLY, self.root, f)
        return f, str(code), out

    # -- refused without the published identity on disk
    def test_F9_a_published_unsaved_lift_is_refused_when_zenodo_json_holds_no_ids(self):
        jp, new = self.published_unsaved()
        snap = Bytes(jp, new / "zenodo.json")
        code, out, err = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIsInstance(code, str)
        self.assertIn("published identity is not restored", code)
        self.assertIn("zenodo_id", code)
        self.assertIn("set", code)
        self.assertTrue(snap.unchanged())
        self.assertFalse(decision_of(jp).exists())
        self.assertEqual(list(jp.parent.glob("*.quarantine-copy-*")), [], "no backup is made before the check")
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertEqual(f.client.calls, [])

    def test_F9_the_refusal_names_the_two_values_to_set_and_the_reconcile_alternative(self):
        jp, new = self.published_unsaved()
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn('"zenodo_id"', code)
        self.assertIn('"doi"', code)
        self.assertIn("--reconcile", code)
        self.assertIn("5", code)
        self.assertIn(GOOD_DOI, code)

    def test_F9_the_directory_that_moved_must_be_named(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        code, _, _ = self.lift_cli(jp)           # no --paper-dir, the recorded directory is gone
        self.assertIsInstance(code, str)
        self.assertIn("does not exist", code)
        self.assertFalse(decision_of(jp).exists())

    # -- accepted with the ids restored
    def test_F9_accepted_when_the_ids_are_restored_and_the_decision_records_them(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        code, out, err = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIsNone(code, code)
        d = json.loads(decision_of(jp).read_text())
        r = d["identity_restored"]
        self.assertEqual((r["concept_id"], r["doi"], r["record_id"]), ("5", GOOD_DOI, "301"))
        self.assertEqual(r["zenodo_json"], str(new / "zenodo.json"))
        self.assertEqual(r["zenodo_json_sha256"], sha(new / "zenodo.json"))
        self.assertTrue(d["records_publication"])
        self.assertIn("VERIFIED", out)
        self.assertIn("never a first publication", out)

    def test_F9_the_positional_paper_dir_does_the_same_as_the_flag(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        code, _, _ = self.lift_cli(jp, str(new))
        self.assertIsNone(code, code)

    def test_F9_an_intact_directory_needs_no_paper_dir(self):
        jp, paper = self.published_unsaved(moved=False)
        tamper(jp, title="An edited title")      # an edited journal: identity kind, liftable
        set_ids(paper)
        code, _, _ = self.lift_cli(jp)
        self.assertIsNone(code, code)
        self.assertEqual(json.loads(decision_of(jp).read_text())["identity_restored"]["zenodo_json"],
                         str(Path(self.paper) / "zenodo.json"))

    # -- the decision holds only while the ids are there
    def test_F9_ids_removed_later_block_again_and_restored_they_are_honoured_again(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        f, code, _ = self.blocked_run(new)
        self.assertNotIn("REFUSING TO START", code)
        drop_ids(new)
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertIn("NOT honoured", code)
        self.assertIn("zenodo.json", code)
        self.assertEqual(f.client.calls, [])
        set_ids(new)
        f, code, _ = self.blocked_run(new)
        self.assertNotIn("REFUSING TO START", code)

    def test_F9_a_zenodo_json_of_another_concept_blocks(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        set_ids(new, zenodo_id=6, doi="10.5281/zenodo.6")
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertEqual(f.client.calls, [])

    def test_F9_only_the_ids_matter_to_the_decision_not_the_rest_of_zenodo_json(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        set_ids(new, version="2.0", web_url="https://elsewhere.example")
        f, code, _ = self.blocked_run(new)
        self.assertNotIn("REFUSING TO START", code)

    # -- ids belonging to another concept are refused
    def test_F9_ids_of_another_concept_on_disk_are_refused(self):
        jp, new = self.published_unsaved()
        set_ids(new, zenodo_id=6, doi="10.5281/zenodo.6")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("published identity is not restored", code)
        self.assertFalse(decision_of(jp).exists())

    def test_F9_a_prefix_a_numeric_equal_or_a_doi_variant_is_not_the_same_identity(self):
        jp, new = self.published_unsaved()
        for zid, doi in ((50, "10.5281/zenodo.50"), (5, "10.5281/zenodo.50"), (50, GOOD_DOI), (5, GOOD_DOI + " "),
                         (5, GOOD_DOI.upper()), (5, "https://doi.org/" + GOOD_DOI), ("05", GOOD_DOI), (5.0, GOOD_DOI),
                         (True, GOOD_DOI), ([5], GOOD_DOI), (None, GOOD_DOI), (5, None), (5, "")):
            set_ids(new, zenodo_id=zid, doi=doi)
            code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
            self.assertIsInstance(code, str, (zid, doi))
            self.assertFalse(decision_of(jp).exists(), (zid, doi))

    def test_F9_a_string_zenodo_id_equal_to_the_concept_id_is_accepted(self):
        jp, new = self.published_unsaved()
        set_ids(new, zenodo_id="5")
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])

    def test_F9_another_paper_in_the_directory_is_refused(self):
        jp, new = self.published_unsaved()
        set_ids(new, bib_key="Other2026")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("not the same paper", code)
        self.assertFalse(decision_of(jp).exists())

    def test_F9_zenodo_json_whose_content_changed_is_refused_when_the_journal_is_trustworthy(self):
        jp, new = self.published_unsaved()
        set_ids(new, web_url="https://other.example/x")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("no longer has the content", code)
        self.assertFalse(decision_of(jp).exists())

    def test_F9_a_missing_or_unreadable_zenodo_json_is_refused(self):
        jp, new = self.published_unsaved()
        (new / "zenodo.json").write_text("{not json")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("cannot be read", code)
        (new / "zenodo.json").write_text("[1]")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("not a JSON object", code)
        os.remove(new / "zenodo.json")
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("cannot be read", code)
        self.assertFalse(decision_of(jp).exists())

    # -- the journal does not know the ids: the operator supplies them
    def test_F9_a_journal_that_does_not_know_the_ids_needs_them_from_the_operator(self):
        jp, new = self.published_unsaved(concept_id=None, concept_doi=None, record_id=None, doi=None)
        set_ids(new)
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new))
        self.assertIn("does not record the concept id and DOI", code)
        self.assertFalse(decision_of(jp).exists())
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new), "--record-id", "301", "--concept-id", "5",
                                   "--concept-doi", GOOD_DOI)
        self.assertIsNone(code, code)
        r = json.loads(decision_of(jp).read_text())["identity_restored"]
        self.assertEqual((r["concept_id"], r["doi"], r["record_id"]), ("5", GOOD_DOI, "301"))

    def test_F9_operator_ids_are_validated_like_a_reconcile(self):
        jp, new = self.published_unsaved(concept_id=None, concept_doi=None, record_id=None, doi=None)
        set_ids(new)
        for extra in (["--concept-id", "05", "--concept-doi", GOOD_DOI], ["--concept-id", "5\u0665", "--concept-doi", GOOD_DOI],
                      ["--concept-id", "5", "--concept-doi", "10.5281/zenodo.50"], ["--concept-id", "5", "--concept-doi", "10.9999/x"],
                      ["--concept-id", "5", "--concept-doi", "10.5072/zenodo.5"], ["--record-id", "-3", "--concept-id", "5",
                                                                                  "--concept-doi", GOOD_DOI]):
            code, out, err = self.lift_cli(jp, "--paper-dir", str(new), *extra)
            self.assertIsInstance(code, str, extra)
            self.assertNotIn("Traceback", out + err)
            self.assertFalse(decision_of(jp).exists(), extra)

    def test_F9_operator_ids_that_differ_from_the_journals_are_refused(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        for extra in (["--concept-id", "6"], ["--concept-doi", "10.5281/zenodo.6", "--concept-id", "6"], ["--record-id", "302"]):
            code, _, _ = self.lift_cli(jp, "--paper-dir", str(new), *extra)
            self.assertIn("must be equal", str(code), extra)
            self.assertFalse(decision_of(jp).exists(), extra)

    def test_F9_equal_operator_ids_are_accepted(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        code, _, _ = self.lift_cli(jp, "--paper-dir", str(new), "--record-id", "301", "--concept-id", "5", "--concept-doi", GOOD_DOI)
        self.assertIsNone(code, code)

    def test_F9_garbage_ids_recorded_in_the_journal_are_refused_without_a_traceback(self):
        for field, bad in (("concept_id", "abc"), ("concept_id", "007"), ("concept_doi", "10.9999/evil"), ("record_id", "x")):
            root, paper = workspace(zenodo_id=None, doi=None)
            j = make_blocking_journal(root, paper, outcome="PUBLISHED-UNSAVED", **{**PUB, field: bad})
            new = paper.parent / "moved"
            os.rename(paper, new)
            set_ids(new)
            code, out, err = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON,
                                       "--paper-dir", str(new)], {}, root)
            self.assertIsInstance(code, str, field)
            self.assertNotIn("Traceback", out + err)
            self.assertFalse(decision_of(j.path).exists(), field)

    # -- the other kinds of publication-recording journals
    def test_F9_unreconciled_and_a_sent_publish_request_need_the_ids_too(self):
        for label, extra in (("UNRECONCILED", dict(outcome="UNRECONCILED")),
                             ("RUNNING with the publish call as last write",
                              dict(outcome="RUNNING", last_write="POST /api/deposit/depositions/301/actions/publish")),
                             ("RUNNING with a record id", dict(outcome="RUNNING", record_id=301)),
                             ("RUNNING with a doi", dict(outcome="RUNNING", doi="10.5281/zenodo.301"))):
            root, paper = workspace(zenodo_id=None, doi=None)
            j = make_blocking_journal(root, paper, **{**dict(concept_id="5", concept_doi=GOOD_DOI), **extra})
            new = paper.parent / "moved"
            os.rename(paper, new)
            self.assertTrue(zp.journal_records_publication(json.loads(j.path.read_text())), label)
            code, _, _ = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON,
                                   "--paper-dir", str(new)], {}, root)
            self.assertIn("published identity is not restored", str(code), label)
            set_ids(new)
            code, _, _ = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON,
                                   "--paper-dir", str(new)], {}, root)
            self.assertIsNone(code, (label, code))

    def test_F9_a_journal_that_proves_nothing_was_published_keeps_the_earlier_rules(self):
        for label, extra in (("RUNNING no write", dict(outcome="RUNNING")),
                             ("RUNNING after a draft was created", dict(outcome="RUNNING", writes_sent=2,
                                                                         last_write="POST /api/deposit/depositions")),
                             ("DRAFT-LEFT-BEHIND", dict(outcome="DRAFT-LEFT-BEHIND"))):
            root, paper = workspace(zenodo_id=None, doi=None)
            j = make_blocking_journal(root, paper, **extra)
            self.assertFalse(zp.journal_records_publication(json.loads(j.path.read_text())), label)
            new = paper.parent / "moved"
            os.rename(paper, new)
            code, out, err = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON], {}, root)
            self.assertIsNone(code, (label, code))
            d = json.loads(decision_of(j.path).read_text())
            self.assertIsNone(d["identity_restored"], label)
            self.assertFalse(d["records_publication"], label)
            f = Factory()
            code2, _, _ = run_main([str(new), "--skip-checks", "--dry-run"], PROD_ONLY, root, f)
            if label == "DRAFT-LEFT-BEHIND":
                self.assertIn("discard it BY HAND", out)

    def test_F9_the_block_message_of_a_draft_left_behind_says_to_discard_it_first(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        j = make_blocking_journal(root, paper, outcome="DRAFT-LEFT-BEHIND")
        tamper(j.path, bib_key="Changed")
        code, _, _ = run_main([str(paper), "--skip-checks"], PROD_ONLY, root, Factory())
        self.assertIn("discard it BY HAND", str(code))

    def test_F9_the_block_message_of_a_published_journal_says_what_to_restore(self):
        jp, new = self.published_unsaved()
        f, code, _ = self.blocked_run(new)
        self.assertIn("RESTORED first", code)
        self.assertIn('"zenodo_id"', code)
        self.assertIn("--paper-dir DIR", code)

    # -- the scenario: a first publication after a lift is impossible
    def test_F9_after_a_lift_a_run_publishes_a_new_version_never_a_first_deposition(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        f = Factory()
        code, out, err = run_main([str(new), "--skip-checks", "--no-cascade"], PROD_ONLY, self.root, f)
        names = f.client.names()
        self.assertNotIn("create", names, (code, names))
        self.assertIn("newversion", names, (code, names))

    def test_F9_without_the_ids_the_run_stays_blocked_whatever_decision_file_is_planted(self):
        jp, new = self.published_unsaved()
        # an operator (or an older tool) writes the decision by hand, without any verified identity
        raw = jp.read_bytes()
        planted = {"schema": 1, "decision": "UNBLOCK", "journal": jp.name, "journal_sha256": hashlib.sha256(raw).hexdigest(),
                   "block_kind": "directory", "reason": "looked at everything, believe me"}
        decision_of(jp).write_text(json.dumps(planted))
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertIn("no verified restoration", code)
        self.assertEqual(f.client.calls, [])
        planted["identity_restored"] = {"concept_id": "5", "doi": GOOD_DOI}
        decision_of(jp).write_text(json.dumps(planted))
        f, code, _ = self.blocked_run(new)        # the claim is checked against zenodo.json, which has no ids
        self.assertIn("REFUSING TO START", code)
        self.assertEqual(f.client.calls, [])

    def test_F9_a_decision_for_another_concept_than_the_journals_does_not_unblock(self):
        jp, new = self.published_unsaved()
        set_ids(new, zenodo_id=6, doi="10.5281/zenodo.6")
        planted = {"schema": 1, "decision": "UNBLOCK", "journal": jp.name, "journal_sha256": sha(jp),
                   "block_kind": "directory", "identity_restored": {"concept_id": "6", "doi": "10.5281/zenodo.6"}}
        decision_of(jp).write_text(json.dumps(planted))
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertIn("not the one the decision verified", code)

    def test_F9_a_copy_of_the_paper_without_ids_stays_blocked_even_when_the_original_holds_them(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        copy_dir = self.root / "copy-of-paper"
        copy_dir.mkdir()
        cfg = json.loads((new / "zenodo.json").read_text())
        cfg.update(zenodo_id=None, doi=None)
        (copy_dir / "zenodo.json").write_text(json.dumps(cfg))
        import shutil as _sh
        _sh.copytree(new / "tex", copy_dir / "tex")
        _sh.copytree(new / "out", copy_dir / "out")
        f = Factory()
        code, _, _ = run_main([str(copy_dir), "--skip-checks"], PROD_ONLY, self.root, f)
        self.assertIn("REFUSING TO START", str(code))
        self.assertEqual(f.client.calls, [])

    # -- the decision file is checked on its own: planted decisions, no directory, a directory that is none
    def test_F9_a_decision_checked_without_the_directory_of_the_run_is_not_honoured(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        self.assertIsNone(self.lift_cli(jp, "--paper-dir", str(new))[0])
        data = json.loads(jp.read_text())
        self.assertTrue(zp.journal_lift_decision_ok(jp, data, new))
        self.assertFalse(zp.journal_lift_decision_ok(jp, data))
        self.assertFalse(zp.journal_lift_decision_ok(jp, data, None))
        self.assertIn("no paper directory", zp.journal_lift_decision_problem(jp, data, None))

    def planted(self, jp, new, restored, **journal_ids):
        """A decision file written by hand for the journal's exact bytes, with the given identity claim."""
        decision = {"schema": 1, "decision": "UNBLOCK", "journal": jp.name, "journal_sha256": sha(jp),
                    "block_kind": "directory", "identity_restored": restored}
        decision_of(jp).write_text(json.dumps(decision))

    def test_F9_a_planted_decision_whose_concept_id_is_not_the_journals_stays_blocked(self):
        # only the concept id disagrees: the journal records no concept DOI
        jp, new = self.published_unsaved(concept_doi=None)
        set_ids(new, zenodo_id=6, doi="10.5281/zenodo.6")
        self.planted(jp, new, {"concept_id": "6", "doi": "10.5281/zenodo.6"})
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertIn("concept id '5' is not the one the decision verified", code)
        self.assertEqual(f.client.calls, [])

    def test_F9_a_planted_decision_whose_concept_doi_is_not_the_journals_stays_blocked(self):
        # only the concept DOI disagrees: the journal records no concept id
        jp, new = self.published_unsaved(concept_id=None)
        set_ids(new, zenodo_id=6, doi="10.5281/zenodo.6")
        self.planted(jp, new, {"concept_id": "6", "doi": "10.5281/zenodo.6"})
        f, code, _ = self.blocked_run(new)
        self.assertIn("REFUSING TO START", code)
        self.assertIn("concept DOI", code)
        self.assertIn("is not the one the decision verified", code)
        self.assertEqual(f.client.calls, [])

    def test_F9_a_planted_decision_that_matches_a_journal_without_ids_is_honoured_only_with_a_well_formed_identity(self):
        jp, new = self.published_unsaved(concept_id=None, concept_doi=None)
        for restored, why in (({"concept_id": "5", "doi": "10.9999/zenodo.5"}, "exact DOI"),
                              ({"concept_id": "5", "doi": "evil"}, "exact DOI"),
                              ({"concept_id": "5", "doi": "10.5281/zenodo.50"}, "exact DOI"),
                              ({"concept_id": "05", "doi": "10.5281/zenodo.05"}, "exact DOI"),
                              ({"concept_id": "5", "doi": GOOD_DOI.upper()}, "exact DOI"),
                              ({"concept_id": 5, "doi": GOOD_DOI}, "exact DOI")):
            set_ids(new, zenodo_id=restored["concept_id"], doi=restored["doi"])
            self.planted(jp, new, restored)
            f, code, _ = self.blocked_run(new)
            self.assertIn("REFUSING TO START", code, restored)
            self.assertIn(why, code, restored)
            self.assertEqual(f.client.calls, [], restored)
        set_ids(new)                                              # the well-formed claim is honoured (the control)
        self.planted(jp, new, {"concept_id": "5", "doi": GOOD_DOI})
        f, code, _ = self.blocked_run(new)
        self.assertNotIn("REFUSING TO START", code)

    def test_F9_a_paper_dir_that_is_not_a_directory_is_refused_as_such(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        for bad in (self.root / "missing-dir", new / "zenodo.json"):
            code, _, _ = self.lift_cli(jp, "--paper-dir", str(bad))
            self.assertIn("is not a directory", str(code), bad)
        self.assertFalse(decision_of(jp).exists())

    # -- sandbox journals and unreadable journals
    def test_F9_a_sandbox_journal_is_lifted_without_identity_and_refuses_ids(self):
        root, paper = workspace(zenodo_id=None, doi=None)
        j = make_blocking_journal(root, paper, outcome="PUBLISHED-UNSAVED", host="sandbox.zenodo.org", sandbox=True)
        new = paper.parent / "moved"
        os.rename(paper, new)
        code, out, _ = run_main(["--lift-block", str(j.path), "--i-checked-the-account", "--reason", REASON], {}, root)
        self.assertIsNone(code, code)
        self.assertIsNone(json.loads(decision_of(j.path).read_text())["identity_restored"])
        self.assertIn("SANDBOX", out)
        root2, paper2 = workspace(zenodo_id=None, doi=None)
        j2 = make_blocking_journal(root2, paper2, outcome="PUBLISHED-UNSAVED", host="sandbox.zenodo.org", sandbox=True)
        os.rename(paper2, paper2.parent / "moved")
        code, _, _ = run_main(["--lift-block", str(j2.path), "--i-checked-the-account", "--reason", REASON,
                               "--concept-id", "5"], {}, root2)
        self.assertIn("SANDBOX", str(code))
        self.assertFalse(decision_of(j2.path).exists())

    def test_F9_an_unreadable_journal_keeps_the_earlier_rules_and_verifies_ids_when_given(self):
        jp = state_dir_for(self.root) / f"paper-20260101T000000Z-1-{os.urandom(3).hex()}.run.json"
        jp.write_bytes(b"{trunc")
        code, _, _ = self.lift_cli(jp)
        self.assertIsNone(code, code)
        self.assertIsNone(json.loads(decision_of(jp).read_text())["identity_restored"])
        jp2 = state_dir_for(self.root) / f"paper-20260101T000000Z-2-{os.urandom(3).hex()}.run.json"
        jp2.write_bytes(b"{trunc")
        code, _, _ = self.lift_cli(jp2, "--concept-id", "5", "--concept-doi", GOOD_DOI)      # no directory known
        self.assertIn("does not exist", str(code))
        code, _, _ = self.lift_cli(jp2, "--concept-id", "5", "--concept-doi", GOOD_DOI, "--paper-dir", str(self.paper))
        self.assertIn("published identity is not restored", str(code))
        set_ids(self.paper)
        code, _, _ = self.lift_cli(jp2, "--concept-id", "5", "--concept-doi", GOOD_DOI, "--paper-dir", str(self.paper))
        self.assertIsNone(code, code)

    # -- command line
    def test_F9_paper_dir_flag_and_positional_must_agree_and_the_flag_needs_lift_block(self):
        jp, new = self.published_unsaved()
        other = self.root / "other"
        other.mkdir()
        code, _, _ = self.lift_cli(jp, str(other), "--paper-dir", str(new))
        self.assertEqual(code, 2)
        code, out, err = run_main([str(new), "--paper-dir", str(new), "--dry-run"], {}, self.root)
        self.assertEqual(code, 2)
        self.assertIn("--paper-dir only makes sense with --lift-block", err)

    def test_F9_lift_block_still_refuses_reconcile_options(self):
        jp, new = self.published_unsaved()
        code, _, _ = self.lift_cli(jp, "--outcome", "published")
        self.assertEqual(code, 2)
        code, _, _ = run_main(["--backup-journal", str(jp), "--concept-id", "5"], {}, self.root)
        self.assertEqual(code, 2)

    def test_F9_the_decision_file_stays_valid_json_and_never_holds_a_registered_token(self):
        jp, new = self.published_unsaved()
        set_ids(new)
        with mock.patch.object(zp, "_SECRETS", set()):
            code, out, err = run_main(["--lift-block", str(jp), "--i-checked-the-account", "--reason",
                                       f"checked, token {PROD_TOKEN} is not recorded here", "--paper-dir", str(new)],
                                      {"ZENODO_TOKEN": PROD_TOKEN}, self.root)
        self.assertIsNone(code, code)
        text = decision_of(jp).read_text()
        json.loads(text)
        self.assertNotIn(PROD_TOKEN, text + out + err)


# ── F-5: the survivors Z5, Z47, Z49 (and Z9, equivalent by design) ───────────────────────────────────────

class SurvivorsOfTheReviewersOwnMutants(unittest.TestCase):
    def setUp(self):
        self.root, self.paper = workspace(zenodo_id=None, doi=None)

    def test_Z5_identical_ids_already_in_zenodo_json_are_not_rewritten(self):
        j = make_blocking_journal(self.root, self.paper)
        cfg_path = self.paper / "zenodo.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.update(zenodo_id=5, doi=GOOD_DOI)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        st = cfg_path.stat()
        os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))      # an earlier mtime a rewrite would change
        st = cfg_path.stat()
        before = (cfg_path.read_bytes(), st.st_mtime_ns, st.st_ino)
        code, out, err = reconcile(j.path, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        st = cfg_path.stat()
        self.assertEqual((cfg_path.read_bytes(), st.st_mtime_ns, st.st_ino), before)
        self.assertEqual(json.loads(j.path.read_text())["outcome"], "PUBLISHED-RECONCILED")
        self.assertEqual(json.loads(j.path.read_text())["operator_evidence"]["config_written"], str(cfg_path))

    def test_Z5_empty_or_absent_ids_are_filled_and_the_file_is_rewritten(self):
        j = make_blocking_journal(self.root, self.paper)
        cfg_path = self.paper / "zenodo.json"
        st = cfg_path.stat()
        os.utime(cfg_path, ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))
        mtime = cfg_path.stat().st_mtime_ns
        code, _, _ = reconcile(j.path, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        self.assertNotEqual(cfg_path.stat().st_mtime_ns, mtime)
        cfg = json.loads(cfg_path.read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, GOOD_DOI))

    def test_Z5_an_identical_zenodo_id_with_an_empty_doi_is_completed(self):
        j = make_blocking_journal(self.root, self.paper)
        cfg_path = self.paper / "zenodo.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.update(zenodo_id=5, doi=None)
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        code, _, _ = reconcile(j.path, *published_args(), root=self.root)
        self.assertIsNone(code, code)
        self.assertEqual(json.loads(cfg_path.read_text())["doi"], GOOD_DOI)

    def test_Z47_a_garbage_concept_doi_is_refused_before_anything_for_every_outcome(self):
        j = make_blocking_journal(self.root, self.paper)
        snap = Bytes(self.paper / "zenodo.json", j.path)
        for bad in ("garbage", "10.5281/zenodo.", "10.9999/zenodo.5", "10.5281/ZENODO.5", ""):
            if not bad:
                continue
            code, out, err = reconcile(j.path, "--outcome", "not-published", "--i-checked-the-account", "--reason", REASON,
                                       "--concept-doi", bad, root=self.root)
            self.assertIsInstance(code, str, bad)
            self.assertTrue("--concept-doi must be exactly" in code or "positive integer" in code, (bad, code))
            self.assertNotIn("Traceback", out + err)
            self.assertTrue(snap.unchanged(), bad)
        code, _, _ = reconcile(j.path, "--outcome", "not-published", "--i-checked-the-account", "--reason", REASON,
                               "--concept-doi", GOOD_DOI, root=self.root)
        self.assertIsNone(code, code)

    def test_Z47_a_doi_whose_number_differs_from_the_given_concept_id_is_refused_for_not_published_too(self):
        j = make_blocking_journal(self.root, self.paper)
        code, _, _ = reconcile(j.path, "--outcome", "not-published", "--i-checked-the-account", "--reason", REASON,
                               "--concept-id", "5", "--concept-doi", "10.5281/zenodo.6", root=self.root)
        self.assertIn("must be equal", str(code))
        self.assertEqual(json.loads(j.path.read_text())["outcome"], "UNRECONCILED")

    def test_Z49_a_symbolic_link_given_on_the_command_line_is_refused_not_followed(self):
        j = make_blocking_journal(self.root, self.paper, outcome="RUNNING")
        tamper(j.path, title="An edited title")                     # identity kind: liftable
        link = j.path.parent / "link.run.json"
        os.symlink(j.path, link)
        before = sorted(p.name for p in j.path.parent.iterdir())
        for flag in ("--lift-block", "--backup-journal"):
            argv = [flag, str(link)]
            if flag == "--lift-block":
                argv += ["--i-checked-the-account", "--reason", REASON]
            code, out, err = run_main(argv, {}, self.root)
            self.assertIsInstance(code, str, flag)
            self.assertIn("symbolic link", code, flag)
            self.assertIn(str(j.path), code, flag)
            self.assertNotIn("Traceback", out + err)
        self.assertEqual(sorted(p.name for p in j.path.parent.iterdir()), before)

    def test_Z49_the_guard_is_also_tested_directly(self):
        j = make_blocking_journal(self.root, self.paper)
        link = j.path.parent / "l2.run.json"
        os.symlink(j.path, link)
        with self.assertRaises(zp.ReconcileRefused) as cm:
            zp._read_journal_bytes(link)
        self.assertIn("symbolic link", str(cm.exception))
        d = j.path.parent / "dir.run.json"
        d.mkdir()
        with self.assertRaises(zp.ReconcileRefused) as cm:
            zp._read_journal_bytes(d)
        self.assertIn("not a regular file", str(cm.exception))
        self.assertEqual(zp._read_journal_bytes(j.path), j.path.read_bytes())

    def test_Z49_a_relative_path_and_a_tilde_path_still_work(self):
        j = make_blocking_journal(self.root, self.paper, outcome="RUNNING")
        tamper(j.path, title="An edited title")
        cwd = os.getcwd()
        try:
            os.chdir(j.path.parent)
            code, _, _ = run_main(["--backup-journal", j.path.name], {}, self.root)
        finally:
            os.chdir(cwd)
        self.assertIsNone(code, code)
        self.assertTrue(list(j.path.parent.glob("*.quarantine-copy-*")))

    def test_Z9_every_order_of_the_three_pattern_passes_is_complete_on_its_own(self):
        # equivalent by design: the scheme pass applied last instead of first changes nothing, because each pass
        # masks what it is responsible for without relying on another one having run
        for text in ("Authorization: Bearer ABCDEF", "Authorization=ApiKey ABCDEF&x=1", "auth_token=ABCDEF", "Bearer: ABCDEF",
                     '{"api_secret": "ABCDEF"}', "access%5Ftoken=ABCDEF", "Basic ABCDEF", "Cookie: sid=ABCDEF; a=b"):
            outs = {zp.redact(text, passes=order) for order in itertools.permutations(zp._PATTERN_PASSES)}
            self.assertEqual(len(outs), 1, (text, outs))
            self.assertNotIn("ABCDEF", outs.pop(), text)


class BareNounsStayReadableInProse(unittest.TestCase):
    """A bare ``signature`` / ``credential(s)`` is also ordinary physics or admin prose (the Lorentzian signature of a
    metric in an abstract that --dry-run prints): masked only where a field name can stand, never after a word."""

    def setUp(self):
        self._p = mock.patch.object(zp, "_SECRETS", set())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_prose_with_a_bare_noun_and_a_colon_or_equals_is_untouched(self):
        for text in ("the Lorentzian signature: (-,+,+,+) in the abstract", "metric signature = diag(-1,1,1,1)",
                     "see credentials: none needed", "a signature: opposite sign", "with signature=(3,1)"):
            self.assertEqual(zp.redact(text), text)

    def test_the_same_nouns_are_masked_where_a_field_name_stands(self):
        for text, expect in (("Signature=abc&x=1", "Signature=[REDACTED]&x=1"), ("&signature=abc", "&signature=[REDACTED]"),
                             ("?a=1&credentials=u:p", "?a=1&credentials=[REDACTED]"),
                             ('{"signature": "abc"}', '{"signature": "[REDACTED]"}'),
                             ("signature: abc", "signature: [REDACTED]"), ("line\nsignature: abc", "line\nsignature: [REDACTED]"),
                             ("a, signature: abc", "a, signature: [REDACTED]"), ("x-amz-signature: v", "x-amz-signature: [REDACTED]"),
                             ("request_signature=v", "request_signature=[REDACTED]"), ("see aws_credentials: x", "see aws_credentials: [REDACTED]")):
            self.assertEqual(zp.redact(text), expect, text)

    def test_a_dry_run_metadata_print_keeps_a_physics_abstract_readable(self):
        meta = {"description": "We fix the Lorentzian signature: (-,+,+,+) and show that the signature = (3,1) is stable.",
                "title": "T"}
        printed = zp.redact(json.dumps(meta, indent=2))
        self.assertIn("Lorentzian signature: (-,+,+,+)", printed)
        self.assertIn("signature = (3,1)", printed)


# ── documentation pinned to the code (computed, not asserted) ──────────────────────────────────────────────

class DocsOfTheSixthPass(unittest.TestCase):
    README = (TOOLS / "README.md").read_text(encoding="utf-8")
    PILOT = (TOOLS / "pilot" / "README-PILOT.md").read_text(encoding="utf-8")

    def test_every_name_of_the_allow_list_is_documented_and_no_other_variable_is_claimed_passed(self):
        for name in sorted(pc.CHILD_ENV_ALLOW):
            self.assertIn(f"`{name}`", self.README, name)
        for name in zp._GIT_ENV_KEEP:
            self.assertIn(f"`{name}`", self.README, name)

    def test_the_survivor_claims_are_the_ones_of_the_scripts(self):
        import importlib
        mc = importlib.import_module("mutation_check")
        mcr = importlib.import_module("mutation_check_review")
        self.assertEqual(mc.EQUIVALENT_SURVIVORS, {"M16b"})
        self.assertEqual(mcr.EQUIVALENT_SURVIVORS, {"Y1"})
        flat = " ".join(self.README.split())
        self.assertIn("exactly one survivor, `M16b`", flat)
        self.assertIn("only documented equivalent is `Y1`", flat)
        self.assertNotIn("E1", mc.EQUIVALENT_SURVIVORS)
        # E1 is a real, killed mutant of the choke point
        self.assertIn("E1", [m[0] for m in mc.MUTANTS])
        e1 = [m for m in mc.MUTANTS if m[0] == "E1"][0]
        self.assertTrue("_builtins.print(redact(" in e1[2] and "sys.stderr" in e1[3], e1)

    def test_no_surface_mentions_previous_web_url_as_a_feature(self):
        for name, text in (("docstring", zp.__doc__), ("help", zp.build_parser().format_help()),
                           ("example", (TOOLS / "zenodo.example.json").read_text())):
            self.assertNotIn("previous_web_url", text, name)
        flat = " ".join(self.README.split())
        self.assertIn("ignored: it is no longer read", flat)
        for m in re.finditer(r"previous_web_url", self.README):
            self.assertIn("older", self.README[max(0, m.start() - 120): m.end() + 120])

    def test_the_lift_rule_for_a_published_journal_is_in_the_help_the_docstring_and_the_readme(self):
        flat_help = " ".join(zp.build_parser().format_help().split())
        for needle in ("--paper-dir", "records a publication", "REFUSED", "--record-id/--concept-id/--concept-doi"):
            self.assertIn(needle, flat_help, needle)
        flat_doc = " ".join(zp.__doc__.split())
        for needle in ("records a CONFIRMED or LIKELY publication", "--paper-dir DIR"):
            self.assertIn(needle, flat_doc, needle)
        flat = " ".join(self.README.split())
        for needle in ("needs the published identity", "never a first publication", "STILL holds those ids"):
            self.assertIn(needle, flat, needle)
        self.assertIn("allow-list", " ".join(self.PILOT.split()))

    def test_the_replace_related_identifier_help_states_the_role_restriction(self):
        flat = " ".join(zp.build_parser().format_help().split())
        for needle in ("relation isSupplementedBy, scheme url", "isReferencedBy", "never replaceable"):
            self.assertIn(needle, flat, needle)


if __name__ == "__main__":
    unittest.main()
