"""Offline tests for the Zenodo tool contract C1-C9 (sandbox isolation, uncertainty, pagination,
timeouts, interruption, early local save, bounded verification, blocking upload/delete failures).

No network: a socket guard fails any real connection; HTTP is the scripted Sim / FakeSession / FakeClient.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import (F, HOST, PROD_TOKEN, SBX_TOKEN, URL, FakeClient, FakeSession, HTTPFail, Resp, Sim,  # noqa: E402
                   make_paper, new, real_client, run, tree_checksums, upd)
import requests  # noqa: E402
import zenodo_publish as zp  # noqa: E402

PDF = None


def setUpModule():
    global PDF
    fakes.install_socket_guard()
    PDF = Path(tempfile.mkdtemp()) / "p.pdf"
    PDF.write_bytes(b"%PDF-fake")


def tearDownModule():
    fakes.remove_socket_guard()


def new_real(sim, zid=5, **k):
    client, _ = real_client(sim)
    return client, new(client, zid=zid, pdf=str(PDF), **k)


def run_new_real(sim, zid=5, **k):
    client, _ = real_client(sim)
    return client, run(new, client, zid=zid, pdf=str(PDF), **k)


class Factory:
    """Stands in for zp.ZenodoClient inside main(): records (token, sandbox), hands out a FakeClient."""

    def __init__(self, client=None):
        self.client, self.built = client or FakeClient(), []

    def __call__(self, token, sandbox=False, **k):
        self.built.append((token, sandbox))
        return self.client


_STATE_DIRS = {}


def state_dir_for(root):
    """One journal directory per fake workspace, OUTSIDE it (as the tool requires)."""
    key = str(root)
    if key not in _STATE_DIRS:
        _STATE_DIRS[key] = Path(tempfile.mkdtemp()).resolve()
    return _STATE_DIRS[key]


def _reviewed(argv, env, root, factory, extra):
    """--yes-apply-diff must carry the digest of the diff the operator reviewed (a bare flag is refused). Tests
    written with the bare flag get the two-step workflow here: a PREVIEW run on a copy of the client (no
    --yes-apply-diff) prints the diff, its `diff sha256:` is read and passed to the real run."""
    import copy
    import re
    if "--yes-apply-diff" not in argv or "--update" not in argv or factory is None:
        return argv
    i = argv.index("--yes-apply-diff")
    if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        return argv                                    # an explicit value: untouched
    preview = argv[:i] + argv[i + 1:]
    _, out, _ = run_main(preview, env, root, Factory(copy.deepcopy(factory.client)), extra=extra)
    m = re.search(r"diff sha256: ([0-9a-f]{64})", out)
    return argv[:i] + ["--yes-apply-diff", m.group(1)] + argv[i + 1:] if m else argv


def run_main(argv, env, root, factory=None, extra=(), raw_confirm=False):
    argv = list(argv)
    if not raw_confirm:
        argv = _reviewed(argv, env, root, factory, extra)
    if "--dry-run" not in argv and "--state-dir" not in argv and "--reconcile" not in argv:
        argv += ["--state-dir", str(state_dir_for(root))]
    patches = [mock.patch.object(zp, "REPO_ROOT", root.resolve()),
               mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", ""), **env}, clear=True)]
    if factory is not None:
        patches.append(mock.patch.object(zp, "ZenodoClient", factory))
    patches.extend(extra)
    for p in patches:
        p.start()
    try:
        return run(zp.main, argv)
    finally:
        for p in reversed(patches):
            p.stop()


def workspace(**cfg):
    root = Path(tempfile.mkdtemp()).resolve()
    paper = make_paper(root, zenodo=cfg)
    return root, paper


# ── C1 ────────────────────────────────────────────────────────────────────────────────────────

class C1Tokens(unittest.TestCase):
    def test_sandbox_has_no_fallback_to_production_token(self):
        with self.assertRaises(SystemExit) as cm:
            zp.resolve_token(True, {"ZENODO_TOKEN": PROD_TOKEN})
        self.assertIn("ZENODO_SANDBOX_TOKEN", str(cm.exception))
        self.assertNotIn(PROD_TOKEN, str(cm.exception))

    def test_each_mode_reads_only_its_own_variable(self):
        env = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}
        self.assertEqual(zp.resolve_token(True, env), SBX_TOKEN)
        self.assertEqual(zp.resolve_token(False, env), PROD_TOKEN)
        with self.assertRaises(SystemExit):
            zp.resolve_token(False, {"ZENODO_SANDBOX_TOKEN": SBX_TOKEN})

    def test_empty_or_blank_token_refuses(self):
        for v in ("", "   ", None):
            env = {} if v is None else {"ZENODO_SANDBOX_TOKEN": v}
            with self.assertRaises(SystemExit):
                zp.resolve_token(True, env)

    def test_pairing_check_in_constructor(self):
        both = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}
        with self.assertRaises(zp.TokenHostMismatch):
            zp.ZenodoClient(PROD_TOKEN, sandbox=True, session=FakeSession(), environ=both)
        with self.assertRaises(zp.TokenHostMismatch):
            zp.ZenodoClient(SBX_TOKEN, sandbox=False, session=FakeSession(), environ=both)
        s1, s2 = FakeSession(), FakeSession()
        zp.ZenodoClient(SBX_TOKEN, sandbox=True, session=s1, environ=both)
        zp.ZenodoClient(PROD_TOKEN, sandbox=False, session=s2, environ=both)
        self.assertEqual(s1.headers["Authorization"], f"Bearer {SBX_TOKEN}")
        self.assertEqual(s2.headers["Authorization"], f"Bearer {PROD_TOKEN}")

    def test_mismatch_message_never_prints_a_token(self):
        both = {"ZENODO_TOKEN": PROD_TOKEN}
        with self.assertRaises(zp.TokenHostMismatch) as cm:
            zp.ZenodoClient(PROD_TOKEN, sandbox=True, session=FakeSession(), environ=both)
        self.assertNotIn(PROD_TOKEN, str(cm.exception))

    def test_client_refuses_to_send_to_a_foreign_host(self):
        c, sess = real_client(token=PROD_TOKEN)
        for bad in ("https://evil.example/api/x", "https://sandbox.zenodo.org/api/x",
                    "http://zenodo.org/api/x", "https://zenodo.org.evil.example/api/x"):
            with self.assertRaises(zp.TokenHostMismatch):
                c._request("GET", bad)
        self.assertEqual(sess.calls, [])

    def test_sandbox_client_talks_only_to_the_sandbox_host(self):
        c, sess = real_client(sandbox=True, token=SBX_TOKEN)
        with self.assertRaises(zp.TokenHostMismatch):
            c._request("GET", "https://zenodo.org/api/records/1")
        sess.route("GET", "/records/1", Resp(200, {"id": 1}))
        c.get_record(1)
        self.assertEqual(sess.calls[0][1], "https://sandbox.zenodo.org/api/records/1")

    def test_foreign_bucket_url_blocks_the_upload_and_cleans_up(self):
        sim = Sim()
        orig = sim._dep_json
        sim._dep_json = lambda i: {**orig(i), "links": {"bucket": f"https://evil.example/api/files/b{i}"}}
        client, (code, _, err) = run_new_real(sim)
        self.assertIsInstance(code, str)
        self.assertFalse([u for _, u, _ in sim.calls if "evil.example" in u])
        self.assertEqual(sim.discarded, [301])

    def test_main_sandbox_without_sandbox_token_refuses_before_anything(self):
        root, paper = workspace()
        factory = Factory()
        state = Path(tempfile.mkdtemp()) / "s.json"
        with mock.patch.object(zp, "find_main_tex", side_effect=AssertionError("started")):
            code, out, err = run_main([str(paper), "--sandbox", "--sandbox-state", str(state)],
                                      {"ZENODO_TOKEN": PROD_TOKEN}, root, factory)
        self.assertIsInstance(code, str)
        self.assertIn("ZENODO_SANDBOX_TOKEN", code)
        self.assertEqual(factory.built, [])
        self.assertNotIn(PROD_TOKEN, code + out + err)

    def test_main_production_without_token_refuses(self):
        root, paper = workspace()
        factory = Factory()
        code, _, _ = run_main([str(paper)], {"ZENODO_SANDBOX_TOKEN": SBX_TOKEN}, root, factory)
        self.assertIn("ZENODO_TOKEN", code)
        self.assertEqual(factory.built, [])

    def test_main_hands_the_right_token_to_each_host(self):
        env = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}
        root, paper = workspace()
        f1 = Factory()
        run_main([str(paper), "--skip-checks"], env, root, f1)
        self.assertEqual(f1.built, [(PROD_TOKEN, False)])
        root, paper = workspace()
        f2 = Factory()
        state = Path(tempfile.mkdtemp()) / "s.json"
        run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], env, root, f2)
        self.assertEqual(f2.built, [(SBX_TOKEN, True)])

    def test_sandbox_requires_sandbox_state(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--sandbox"], {"ZENODO_SANDBOX_TOKEN": SBX_TOKEN}, root, Factory())
        self.assertEqual(code, 2)

    def test_dry_run_needs_no_token_and_no_state(self):
        root, paper = workspace()
        f = Factory()
        code, out, _ = run_main([str(paper), "--dry-run", "--sandbox"], {}, root, f)
        self.assertIsNone(code)
        self.assertEqual(f.built, [])
        self.assertIn("Dry run", out)


# ── C2 ────────────────────────────────────────────────────────────────────────────────────────

class C2SandboxIsolation(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN, "ZENODO_SANDBOX_TOKEN": SBX_TOKEN}

    def sandbox_run(self, extra=(), cfg=None, client=None):
        root, paper = workspace(**(cfg or {}))
        state = Path(tempfile.mkdtemp()) / "state.json"
        before = tree_checksums(root)
        f = Factory(client)
        res = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state), *extra],
                       self.ENV, root, f)
        return root, paper, state, before, tree_checksums(root), f, res

    def test_new_version_sandbox_run_changes_no_production_file(self):
        root, paper, state, before, after, f, (code, out, _) = self.sandbox_run(
            cfg={"zenodo_id": 5, "doi": "10.5281/zenodo.0"})
        self.assertIsNone(code)
        self.assertEqual(before, after)          # zenodo.json, pdfs/, bib files: byte-identical
        self.assertNotIn("resolve", f.client.names())   # the production id 5 was never used on the sandbox
        self.assertIn("create", f.client.names())
        rec = json.loads(state.read_text())
        self.assertTrue(rec["sandbox"])
        self.assertEqual(rec["host"], "sandbox.zenodo.org")
        self.assertIn("sandbox.zenodo.org", out)
        self.assertEqual(os.listdir(root / "pdfs"), [])

    def test_sandbox_zenodo_id_is_explicit(self):
        root, paper, state, before, after, f, (code, _, _) = self.sandbox_run(
            extra=("--sandbox-zenodo-id", "77"), cfg={"zenodo_id": 5})
        self.assertIsNone(code)
        self.assertEqual(before, after)
        self.assertIn("newversion", f.client.names())

    def test_update_sandbox_run_changes_no_production_file(self):
        root, paper, state, before, after, f, (code, _, _) = self.sandbox_run(
            extra=("--update", "--set-title", "--yes-apply-diff", "--sandbox-zenodo-id", "77"), cfg={"zenodo_id": 5})
        self.assertIsNone(code, code)
        self.assertEqual(before, after)
        self.assertTrue(state.exists())

    def test_update_sandbox_without_sandbox_id_refuses_even_if_config_has_production_id(self):
        root, paper, state, before, after, f, (code, _, _) = self.sandbox_run(
            extra=("--update", "--set-title"), cfg={"zenodo_id": 5})
        self.assertIn("--update needs", code)
        self.assertEqual(before, after)
        self.assertEqual(f.client.calls, [])

    def test_sandbox_state_inside_workspace_is_refused(self):
        root, paper = workspace()
        for inside in (root / "state.json", paper / "state.json", paper / "zenodo.json"):
            f = Factory()
            code, _, _ = run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(inside)],
                                  self.ENV, root, f)
            self.assertIn("lies inside", code)
            self.assertEqual(f.built, [])

    def test_a_production_run_does_write_them_so_the_checksum_test_is_sensitive(self):
        root, paper = workspace()
        before = tree_checksums(root)
        f = Factory()
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, f)
        self.assertIsNone(code)
        after = tree_checksums(root)
        self.assertNotEqual(before, after)
        cfg = json.loads((paper / "zenodo.json").read_text())
        self.assertEqual((cfg["zenodo_id"], cfg["doi"]), (5, "10.5281/zenodo.0"))
        self.assertTrue((root / "pdfs" / "P_1.0.pdf").exists())
        self.assertIn("10.5281/zenodo.0", (root / "bib" / "cosmochrony-bibliography.bib").read_text())

    def test_save_local_state_is_unreachable_from_a_sandbox_run(self):
        # defence in depth: even if it were called, a sandbox run raises instead of writing zenodo.json
        root, paper = workspace()
        state = Path(tempfile.mkdtemp()) / "s.json"
        captured = {}
        orig = zp.deposit_new

        def spy(client, pdf, meta, code_url, zid, env_label, host, on_published=None):
            captured["cb"] = on_published
            return orig(client, pdf, meta, code_url, zid, env_label, host, on_published=on_published)
        before = tree_checksums(root)
        run_main([str(paper), "--skip-checks", "--sandbox", "--sandbox-state", str(state)], self.ENV, root,
                 Factory(), extra=[mock.patch.object(zp, "deposit_new", spy)])
        self.assertEqual(captured["cb"].__name__, "save_sandbox_state")
        self.assertEqual(before, tree_checksums(root))


# ── C3 ────────────────────────────────────────────────────────────────────────────────────────

class C3Uncertainty(unittest.TestCase):
    def test_timeout_but_draft_created_is_found_and_discarded(self):
        sim = Sim(newversion="timeout_created")
        client, (code, out, err) = run_new_real(sim)
        self.assertIn("UNCERTAINTY", err)
        self.assertNotIn("nothing to clean up", err + str(code))
        self.assertEqual(sim.discarded, [301])
        self.assertIn("Cleanup performed", code)

    def test_malformed_answers_are_uncertain_and_resolved_by_search(self):
        for mode in ("nonjson", "nolinks"):
            sim = Sim(newversion=mode)
            client, (code, _, err) = run_new_real(sim)
            self.assertIn("UNCERTAINTY", err, mode)
            self.assertEqual(sim.discarded, [301], mode)

    def test_timeout_and_nothing_created_discards_nothing_and_says_so(self):
        sim = Sim(newversion="timeout_nothing")
        client, (code, _, err) = run_new_real(sim)
        self.assertEqual(sim.discarded, [])
        self.assertIn("UNCERTAINTY", err)
        self.assertIn("No new-version draft attributable", code)
        self.assertNotIn("nothing to clean up", code)

    def test_ambiguous_candidates_delete_nothing_and_print_manual_instructions(self):
        sim = Sim(newversion="twice")
        client, (code, _, err) = run_new_real(sim)
        self.assertEqual(sim.discarded, [])
        self.assertIn("NOTHING WAS DISCARDED", code)
        self.assertIn("MANUAL CHECK REQUIRED", code)
        self.assertIn("/deposit/301", code)
        self.assertIn("/deposit/302", code)

    def test_definitive_refusal_creates_nothing_and_the_message_is_true(self):
        sim = Sim(newversion="409")
        client, (code, _, _) = run_new_real(sim)
        self.assertIsNone(client.created_state)
        self.assertIn("nothing to clean up", code)
        self.assertEqual(sim.discarded, [])

    def test_intent_is_recorded_before_the_request_is_sent(self):
        c, sess = real_client()
        seen = {}

        def post(method, url, kw):
            seen["state"] = dict(c.created_state)
            return Resp(500, text="boom")
        sess.route("POST", "/actions/newversion", post)
        with self.assertRaises(zp.ZenodoAPIError):
            fakes_quiet(c.new_version, 10)
        self.assertEqual(seen["state"], {"dep_id": None, "kind": "new-version draft", "certain": False})
        self.assertFalse(c.created_state["certain"])   # a 500 may still have created it

    def test_locate_own_state_rules(self):
        def client(drafts, snapshot, concept="5", fail=False):
            c = FakeClient(drafts=drafts, fail_at="list_drafts" if fail else None)
            c.pre_run_draft_ids, c.concept_id = snapshot, concept
            return c
        st = {"dep_id": None, "kind": "new-version draft", "certain": False}
        D = lambda i, concept="5", state="unsubmitted": {"id": i, "conceptrecid": concept, "state": state}
        v = lambda c: zp.locate_own_state(c, st)[0:2]
        self.assertEqual(v(client([D(1), D(2)], {1})), ("own", 2))
        self.assertEqual(v(client([D(1), D(2, "9")], {1})), ("none", None))          # other concept ignored
        self.assertEqual(v(client([D(1), D(2, state="done")], {1})), ("none", None))  # not unpublished
        self.assertEqual(v(client([D(1)], {1})), ("none", None))                      # in the snapshot
        self.assertEqual(v(client([D(2), D(3)], set()))[0], "unknown")                # ambiguous
        self.assertEqual(v(client([D(2)], None))[0], "unknown")                       # no snapshot
        self.assertEqual(v(client([D(2)], set(), fail=True))[0], "unknown")           # listing failed
        self.assertEqual(v(client([{"id": 2, "conceptrecid": "5"}], set()))[0], "none")  # state missing

    def test_edit_session_uncertain_is_attributed_by_state_change(self):
        sim = Sim(edit="timeout_created")
        client, _ = real_client(sim)
        code, _, err = run(upd, client)
        self.assertIn("UNCERTAINTY", err)
        self.assertEqual(sim.discarded, [10])

    def test_edit_session_uncertain_but_record_still_done_discards_nothing(self):
        sim = Sim(edit="timeout_nothing")
        client, _ = real_client(sim)
        code, _, err = run(upd, client)
        self.assertEqual(sim.discarded, [])
        self.assertIn("no edit session exists", code)

    def test_discard_refused_for_an_id_present_before_the_run(self):
        c = FakeClient(fail_at="upload")
        c.pre_run_draft_ids = {99}
        c.concept_id = "5"
        # the guard would normally set these; force a misattribution and check the refusal
        with mock.patch.object(zp, "refuse_if_unpublished_state"):
            code, _, _ = run(new, c)
        self.assertIn("REFUSING to discard", code)
        self.assertNotIn("discard", c.names())

    def test_discard_refused_for_a_foreign_concept(self):
        c = FakeClient(fail_at="upload")
        c.concept_id, c.pre_run_draft_ids = "7", set()
        with mock.patch.object(zp, "refuse_if_unpublished_state"):
            code, _, _ = run(new, c)
        self.assertIn("REFUSING to discard", code)
        self.assertNotIn("discard", c.names())

    def test_first_publication_uncertain_create_is_attributed_by_snapshot_diff(self):
        sim = Sim(create="timeout_created")
        sim.add_draft(50, concept="9")      # somebody else's draft, present before the run
        client, (code, _, err) = run_new_real(sim, zid=None)
        self.assertIn("UNCERTAINTY", err)
        self.assertEqual(sim.discarded, [301])

    def test_first_publication_other_concurrent_draft_makes_it_ambiguous(self):
        sim = Sim(create="timeout_created")
        client, _ = real_client(sim)
        orig = sim._new_draft

        def two(with_old_file=True):
            orig(with_old_file)
            return orig(with_old_file)
        sim._new_draft = two
        code, _, _ = run(new, client, zid=None, pdf=str(PDF))
        self.assertEqual(sim.discarded, [])
        self.assertIn("NOTHING WAS DISCARDED", code)

    def test_discard_failure_is_loud_and_names_the_draft(self):
        sim = Sim(upload="500")
        sim.discard_status = 500
        client, (code, _, _) = run_new_real(sim)
        self.assertIn("CLEANUP FAILED", code)
        self.assertIn("MANUAL CLEANUP REQUIRED", code)
        self.assertIn("/deposit/301", code)

    def test_publish_outcomes(self):
        # timeout, but the server published: recovered, local state saved, nothing discarded
        sim, saved = Sim(publish="timeout_published"), []
        client, (code, out, err) = run_new_real(sim, on_published=saved.append)
        self.assertIsNone(code, code)
        self.assertEqual(sim.discarded, [])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["conceptrecid"], "5")
        self.assertIn("PUBLISHED", err)
        # timeout and still unpublished: the publication may complete later -> discard NOTHING
        sim, saved = Sim(publish="timeout_pending"), []
        client, (code, _, _) = run_new_real(sim, on_published=saved.append)
        self.assertEqual(sim.discarded, [])
        self.assertEqual(saved, [])
        self.assertIn("NOTHING WAS DISCARDED", code)
        # 5xx: same doubt
        sim = Sim(publish="500_pending")
        client, (code, _, _) = run_new_real(sim)
        self.assertEqual(sim.discarded, [])
        # definitive 4xx: the draft is certainly unpublished -> discarded
        sim = Sim(publish="400")
        client, (code, _, _) = run_new_real(sim)
        self.assertEqual(sim.discarded, [301])


def fakes_quiet(fn, *a):
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


# ── C4 ────────────────────────────────────────────────────────────────────────────────────────

class C4Pagination(unittest.TestCase):
    def sim(self, n, cap):
        s = Sim()
        for i in range(1, n + 1):
            s.add_draft(100 + i, concept="9")
        s.list_page_cap = cap
        return s

    def test_reads_all_pages_even_if_the_server_caps_the_page_size(self):
        s = self.sim(10, 3)
        c, _ = real_client(s)
        self.assertEqual(len(c.list_drafts()), 10)

    def test_cap_hit_fails_closed(self):
        s = self.sim(10, 1)
        c, _ = real_client(s)
        with mock.patch.object(zp, "MAX_LIST_PAGES", 3):
            with self.assertRaises(zp.CompletenessError):
                c.list_drafts()

    def test_server_ignoring_page_fails_closed(self):
        s = Sim(list="ignore_page")
        s.add_draft(1), s.add_draft(2)
        c, _ = real_client(s)
        with self.assertRaises(zp.CompletenessError):
            c.list_drafts()

    def test_next_link_on_an_empty_page_fails_closed(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", Resp(200, [{"id": 1}]),
                   Resp(200, [], links={"next": {"url": "x"}}))
        with self.assertRaises(zp.CompletenessError):
            c.list_drafts()

    def test_total_count_mismatch_fails_closed(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", Resp(200, [{"id": 1}], headers={"X-Total": "3"}),
                   Resp(200, [], headers={"X-Total": "3"}))
        with self.assertRaises(zp.CompletenessError):
            c.list_drafts()

    def test_total_count_match_is_accepted(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", Resp(200, [{"id": 1}], headers={"X-Total": "1"}),
                   Resp(200, [], headers={"X-Total": "1"}))
        self.assertEqual(len(c.list_drafts()), 1)

    def test_non_list_answer_fails(self):
        c, sess = real_client()
        sess.route("GET", "=/deposit/depositions", Resp(200, {"oops": 1}))
        with self.assertRaises(zp.MalformedResponse):
            c.list_drafts()

    def test_guard_refuses_when_completeness_is_unproven(self):
        c = FakeClient(fail_at="list_drafts", fail_exc=zp.CompletenessError("cap"))
        code, _, _ = run(new, c)
        self.assertIn("cannot determine", code)
        self.assertNotIn("newversion", c.names())

    def test_guard_with_real_client_sees_a_late_page_draft(self):
        s = self.sim(7, 3)
        s.add_draft(900, concept="5")
        c, _ = real_client(s)
        code, _, _ = run(zp.refuse_if_unpublished_state, c, 5, 10, HOST)
        self.assertIn("900", code)


# ── C5 ────────────────────────────────────────────────────────────────────────────────────────

class C5Timeouts(unittest.TestCase):
    def test_every_request_has_a_bounded_connect_read_timeout(self):
        sim = Sim()
        client, code = run_new_real(sim)
        self.assertIsNone(code[0], code)
        for method, url, kw in sim.calls:
            t = kw.get("timeout")
            self.assertIsInstance(t, tuple, (method, url))
            self.assertTrue(all(isinstance(x, (int, float)) and 0 < x < 10_000 for x in t), (method, url))
        uploads = [kw["timeout"] for m, u, kw in sim.calls if m == "PUT" and "/api/files/" in u]
        self.assertEqual(uploads, [(zp.DEFAULT_CONNECT_TIMEOUT, zp.DEFAULT_UPLOAD_READ_TIMEOUT)])

    def test_timeouts_overridable_by_environment(self):
        c, sess = real_client(ZENODO_CONNECT_TIMEOUT=2, ZENODO_READ_TIMEOUT=7, ZENODO_UPLOAD_TIMEOUT=33)
        sess.route("GET", "/records/1", Resp(200, {"id": 1}))
        c.get_record(1)
        self.assertEqual(sess.calls[0][2]["timeout"], (2.0, 7.0))
        self.assertEqual(c.upload_timeout, (2.0, 33.0))

    def test_bad_timeout_value_refuses(self):
        with self.assertRaises(SystemExit):
            real_client(ZENODO_READ_TIMEOUT="soon")

    def test_get_is_retried_with_exponential_backoff_then_gives_up(self):
        c, sess = real_client(ZENODO_RETRY_BACKOFF=1, ZENODO_GET_ATTEMPTS=3)
        sleeps = []
        c._sleep = sleeps.append
        sess.route("GET", "/records/1", requests.exceptions.Timeout("t"))
        with self.assertRaises(requests.exceptions.Timeout):
            c.get_record(1)
        self.assertEqual(len(sess.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_get_recovers_after_transient_failures(self):
        c, sess = real_client()
        sess.route("GET", "/records/1", requests.exceptions.ConnectionError("c"), Resp(503, text="x"),
                   Resp(200, {"id": 1}))
        self.assertEqual(c.get_record(1), {"id": 1})
        self.assertEqual(len(sess.calls), 3)

    def test_get_client_errors_are_not_retried(self):
        c, sess = real_client()
        sess.route("GET", "/records/1", Resp(404, text="nope"))
        with self.assertRaises(zp.ZenodoAPIError):
            fakes_quiet(c.get_record, 1)
        self.assertEqual(len(sess.calls), 1)

    def test_writes_are_never_retried(self):
        for method, call in (("POST", lambda c: c.publish(1)), ("POST", lambda c: c.discard(1, "edit session")), ("DELETE", lambda c: c.discard(1, "new-version draft")),
                             ("PUT", lambda c: c.set_metadata(1, {})), ("DELETE", lambda c: c.delete_file(1, "f")),
                             ("POST", lambda c: c.edit_deposition(1)), ("POST", lambda c: c.create_deposition())):
            c, sess = real_client(ZENODO_GET_ATTEMPTS=5)
            sess.route(method, "", requests.exceptions.Timeout("t"))
            with self.assertRaises(requests.exceptions.Timeout):
                call(c)
            self.assertEqual(len(sess.calls), 1, method)

    def test_write_answered_503_is_not_retried(self):
        c, sess = real_client(ZENODO_GET_ATTEMPTS=5)
        sess.route("POST", "/actions/publish", Resp(503, text="busy"))
        with self.assertRaises(zp.ZenodoAPIError):
            fakes_quiet(c.publish, 1)
        self.assertEqual(len(sess.calls), 1)


# ── C6 ────────────────────────────────────────────────────────────────────────────────────────

class C6Interruption(unittest.TestCase):
    def test_ctrl_c_during_upload_cleans_own_draft_and_reraises(self):
        sim = Sim(upload="interrupt")
        client, (code, _, err) = run_new_real(sim)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(sim.discarded, [301])
        self.assertIn("Cleanup performed", err)

    def test_ctrl_c_during_newversion_request_searches_for_the_draft(self):
        sim = Sim(newversion="interrupt")
        client, (code, _, err) = run_new_real(sim)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertIn("UNCERTAINTY", err)
        self.assertEqual(sim.discarded, [301])

    def test_ctrl_c_during_publish_discards_nothing(self):
        sim = Sim(publish="interrupt")
        client, (code, _, err) = run_new_real(sim)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(sim.discarded, [])
        self.assertIn("NOTHING WAS DISCARDED", err)

    def test_ctrl_c_during_update_publish(self):
        sim = Sim(publish="interrupt")
        client, _ = real_client(sim)
        code, _, err = run(upd, client)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(sim.discarded, [])

    def test_systemexit_inside_the_flow_is_cleaned_up_and_reraised(self):
        c = FakeClient(fail_at="upload", fail_exc=SystemExit(3))
        code, _, _ = run(new, c)
        self.assertEqual(code, 3)
        self.assertIn(("discard", 99), c.calls)

    def test_interruption_before_any_creation_reraises_without_discard(self):
        c = FakeClient(fail_at="resolve", fail_exc=KeyboardInterrupt())
        code, _, _ = run(new, c)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertNotIn("discard", c.names())

    def test_second_interrupt_during_cleanup_is_reported_not_swallowed(self):
        c = FakeClient(fail_at="upload", fail_exc=KeyboardInterrupt())
        orig = c.discard

        def boom(i):
            raise KeyboardInterrupt()
        c.discard = boom
        code, _, err = run(new, c)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")


# ── C7 ────────────────────────────────────────────────────────────────────────────────────────

class C7EarlySave(unittest.TestCase):
    ENV = {"ZENODO_TOKEN": PROD_TOKEN}

    def test_zenodo_json_saved_before_archive_and_cascade(self):
        root, paper = workspace()
        order = []
        cfg_path = paper / "zenodo.json"

        def arch(*a, **k):
            order.append(("archive", json.loads(cfg_path.read_text())["zenodo_id"]))
            return zp.ArchiveResult(Path("x"), "archived")

        def casc(*a, **k):
            order.append(("cascade", json.loads(cfg_path.read_text())["zenodo_id"]))
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(),
                              extra=[mock.patch.object(zp, "archive_pdf", arch),
                                     mock.patch.object(zp, "doi_cascade", casc)])
        self.assertIsNone(code, code)
        self.assertEqual(order, [("archive", 5), ("cascade", 5)])

    def test_publish_error_followed_by_done_still_saves_zenodo_json(self):
        root, paper = workspace()
        client = FakeClient(fail_at="publish", state_after_publish="done")
        code, out, err = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(client))
        cfg = json.loads((paper / "zenodo.json").read_text())
        self.assertEqual(cfg["zenodo_id"], 5)
        self.assertIsNone(code, code)
        self.assertNotIn("discard", client.names())

    def test_failed_save_is_loud_and_forbids_rerun(self):
        sim, saved = Sim(publish="timeout_published"), []

        def bad(result):
            raise RuntimeError("disk full")
        client, (code, _, err) = run_new_real(sim, on_published=bad)
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertIn("DO NOT re-run", err + code[1])

    def test_post_publish_fail_keeps_saved_state_and_finish_after_fail_completes_it(self):
        root, paper = workspace()
        bib = root / "bib" / "cosmochrony-bibliography.bib"
        bib_before = bib.read_text()
        client = FakeClient(published_custom="https://wrong")
        code, _, _ = run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(client))
        self.assertIn("--finish-after-fail", code)
        self.assertEqual(json.loads((paper / "zenodo.json").read_text())["zenodo_id"], 5)
        self.assertEqual(os.listdir(root / "pdfs"), [])
        self.assertEqual(bib.read_text(), bib_before)
        # offline completion: no token, no client, no network
        built = Factory()
        code2, out, _ = run_main([str(paper), "--finish-after-fail"], {}, root, built)
        self.assertIsNone(code2, code2)
        self.assertEqual(built.built, [])
        self.assertTrue((root / "pdfs" / "P_1.0.pdf").exists())
        self.assertIn("10.5281/zenodo.0", bib.read_text())

    def test_finish_after_fail_refuses_when_nothing_was_saved(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--finish-after-fail"], {}, root, Factory())
        self.assertIn("nothing to finish", code)
        self.assertEqual(os.listdir(root / "pdfs"), [])

    def test_finish_after_fail_is_not_combinable_with_sandbox(self):
        root, paper = workspace()
        code, _, _ = run_main([str(paper), "--finish-after-fail", "--sandbox"], {}, root, Factory())
        self.assertEqual(code, 2)

    def test_rerun_after_first_publication_is_a_new_version_not_a_duplicate(self):
        root, paper = workspace()
        run_main([str(paper), "--skip-checks"], self.ENV, root, Factory())
        c2 = FakeClient()
        run_main([str(paper), "--skip-checks"], self.ENV, root, Factory(c2))
        self.assertIn("newversion", c2.names())
        self.assertNotIn("create", c2.names())


# ── C8 ────────────────────────────────────────────────────────────────────────────────────────

class C8BoundedVerification(unittest.TestCase):
    def setUp(self):
        fakes.SLEEPS.clear()

    def test_persistent_mismatch_is_bounded_and_fails(self):
        c = FakeClient(published_custom="https://wrong")
        code, _, err = run(new, c)
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertEqual(c.names().count("get_record"), 5)
        self.assertEqual(len(fakes.SLEEPS), 4)
        self.assertLessEqual(sum(fakes.SLEEPS), 60)
        self.assertIn("FAIL after 5 attempt", err)

    def test_stale_reads_then_correct_passes(self):
        c = FakeClient()
        c.record_reads_before_ok = 2
        code, out, _ = run(new, c)
        self.assertIsNone(code)
        self.assertEqual(c.names().count("get_record"), 3)
        self.assertIn("PASS", out)

    def test_trailing_slash_is_not_a_mismatch(self):
        c = FakeClient(published_custom=URL + "/")
        code, _, _ = run(new, c)
        self.assertIsNone(code)

    def test_attempts_configurable_and_clamped(self):
        for env, expected in (("2", 2), ("99", 6), ("0", 1)):
            c = FakeClient(published_custom="https://wrong")
            with mock.patch.dict(os.environ, {"ZENODO_VERIFY_ATTEMPTS": env}):
                run(new, c)
            self.assertEqual(c.names().count("get_record"), expected, env)
        self.assertLessEqual(sum(zp.VERIFY_DELAYS[min(k, 3)] for k in range(5)), 60)

    def test_unreadable_record_is_retried(self):
        c = FakeClient(published_custom="https://wrong", fail_at="get_record")
        code, _, err = run(new, c)
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertEqual(c.names().count("get_record"), 5)
        self.assertIn("unreadable", err)


# ── C9 / F6 / F11 ─────────────────────────────────────────────────────────────────────────────

class C9BlockingFailures(unittest.TestCase):
    def test_failed_old_file_deletion_blocks_and_cleans_up(self):
        sim = Sim(delete="500")
        client, (code, _, err) = run_new_real(sim)
        self.assertIsInstance(code, str)
        self.assertFalse(sim.posted("/actions/publish"))
        self.assertEqual(sim.discarded, [301])

    def test_failed_upload_blocks_and_cleans_up(self):
        for mode in ("500", "timeout"):
            sim = Sim(upload=mode)
            client, (code, _, _) = run_new_real(sim)
            self.assertFalse(sim.posted("/actions/publish"), mode)
            self.assertEqual(sim.discarded, [301], mode)

    def test_draft_with_an_extra_file_is_refused(self):
        c = FakeClient(draft_files=("p.pdf", "old.pdf"))
        code, _, err = run(new, c)
        self.assertNotIn("publish", c.names())
        self.assertIn(("discard", 99), c.calls)
        self.assertIn("refusing to publish", err)

    def test_draft_without_the_new_pdf_is_refused(self):
        c = FakeClient(draft_files=())
        run(new, c)
        self.assertNotIn("publish", c.names())

    def test_real_client_delete_file_raises_on_failure(self):
        c, sess = real_client()
        sess.route("DELETE", "/files/f", Resp(500, text="boom"))
        with self.assertRaises(zp.ZenodoAPIError):
            fakes_quiet(c.delete_file, 1, "f")

    def test_new_version_without_code_url_documents_that_nothing_is_verified(self):
        c = FakeClient()
        code, out, _ = run(new, c, code_url="")
        self.assertIsNone(code)
        self.assertIn("neither set nor verified", out)
        self.assertNotIn("get_record", c.names())

    def test_guard_uses_the_concept_of_the_latest_record_not_the_config_id(self):
        # config holds a VERSION id (10); the pending draft belongs to concept 5
        c = FakeClient(drafts=[{"id": 77, "conceptrecid": "5", "state": "unsubmitted"}])
        code, _, _ = run(new, c, zid=10)
        self.assertIn("already exists", code)

    def test_guard_requires_state_done(self):
        for state in (None, "inprogress", "unsubmitted"):
            c = FakeClient(record_state=state)
            code, _, _ = run(new, c)
            self.assertIn("not 'done'", code, state)
            self.assertNotIn("newversion", c.names())

    def test_guard_requires_a_concept_id(self):
        c = FakeClient(record_concept=None)
        code, _, _ = run(new, c)
        self.assertIn("cannot determine", code)
        self.assertNotIn("newversion", c.names())


# ── main() ordering, real client over a fake session, secrecy ────────────────────────────────

class MainOrdering(unittest.TestCase):
    def test_token_is_checked_before_any_parsing_or_network(self):
        root, paper = workspace()
        with mock.patch.object(zp, "find_main_tex", side_effect=AssertionError("parsed first")):
            code, _, _ = run_main([str(paper)], {}, root, Factory())
        self.assertIn("ZENODO_TOKEN", code)

    def test_pre_deposit_gate_runs_before_the_client_is_built(self):
        root, paper = workspace()
        order = []

        class Spy(Factory):
            def __call__(self, *a, **k):
                order.append("client")
                return super().__call__(*a, **k)
        fake_check = mock.Mock(side_effect=lambda *a, **k: order.append("gate") or {"problems": [], "notes": []})
        with mock.patch.dict(sys.modules, {"predeposit_check": mock.Mock(check=fake_check)}):
            run_main([str(paper)], {"ZENODO_TOKEN": PROD_TOKEN}, root, Spy())
        self.assertEqual(order, ["gate", "client"])

    def test_dry_run_builds_no_client_and_reads_no_token(self):
        root, paper = workspace()
        f = Factory()
        code, out, _ = run_main([str(paper), "--dry-run"], {}, root, f)
        self.assertIsNone(code)
        self.assertEqual(f.built, [])


class RealClientOverFakeSession(unittest.TestCase):
    def test_new_version_flow_uses_only_the_production_host_and_the_production_token(self):
        sim = Sim()
        client, (code, out, err) = run_new_real(sim)
        self.assertIsNone(code, code)
        self.assertEqual(sim.headers["Authorization"], f"Bearer {PROD_TOKEN}")
        for _, url, _ in sim.calls:
            self.assertTrue(url.startswith("https://zenodo.org/api/"), url)
        seq = [(m, u.split("/api/", 1)[1]) for m, u, _ in sim.calls]
        self.assertIn(("POST", "deposit/depositions/10/actions/newversion"), seq)
        self.assertIn(("POST", "deposit/depositions/301/actions/publish"), seq)
        self.assertFalse(sim.discarded)
        self.assertNotIn(PROD_TOKEN, out + err)

    def test_failure_flows_never_print_the_token(self):
        for mode in ({"upload": "500"}, {"publish": "timeout_pending"}, {"newversion": "twice"}):
            sim = Sim(**mode)
            client, (code, out, err) = run_new_real(sim)
            self.assertNotIn(PROD_TOKEN, f"{code}{out}{err}", mode)
            self.assertNotIn("Bearer", f"{code}{out}{err}", mode)

    def test_discard_is_a_single_post_without_retry(self):
        sim = Sim(upload="500")
        sim.discard_status = 500
        client, _ = run_new_real(sim)
        self.assertEqual(sim.cleanup_calls, [("DELETE", 301)])

    def test_sandbox_flow_uses_the_sandbox_host_and_token(self):
        sim = Sim()
        client, _ = real_client(sim, sandbox=True, token=SBX_TOKEN)
        code, _, _ = run(new, client, zid=None, pdf=str(PDF))
        # bucket links of the Sim point at zenodo.org, which a sandbox client must refuse
        self.assertIsInstance(code, str)
        self.assertEqual(sim.headers["Authorization"], f"Bearer {SBX_TOKEN}")
        self.assertTrue(all("sandbox.zenodo.org" in u or "zenodo.org/api/files" not in u
                            for _, u, _ in sim.calls))
        self.assertFalse([u for _, u, _ in sim.calls if u.startswith("https://zenodo.org/")])


if __name__ == "__main__":
    unittest.main()
