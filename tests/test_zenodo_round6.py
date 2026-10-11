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


if __name__ == "__main__":
    unittest.main()
