"""Offline tests for the Software-field hardening of zenodo_publish.py.

No network: the HTTP layer is replaced by FakeClient; a socket guard fails any real connection.
"""
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import (F, HOST, URL, FakeClient, HTTPFail, Resp, new, no_delays, run, upd,  # noqa: E402
                   real_client, FakeSession)
import zenodo_publish as zp  # noqa: E402


def setUpModule():
    fakes.install_socket_guard()


def tearDownModule():
    fakes.remove_socket_guard()


class BuildMetadata(unittest.TestCase):
    def test_with_code_url(self):
        m = zp.build_metadata("T", "A", [], "https://w", code_url=URL)
        self.assertEqual(m["custom"], {F: URL})
        self.assertEqual([r["resource_type"] for r in m["related_identifiers"]], ["other"])

    def test_without_code_url(self):
        m = zp.build_metadata("T", "A", [], "https://w")
        self.assertNotIn("custom", m)


class DryRun(unittest.TestCase):
    def test_dry_run_network_free(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / "tex").mkdir()
        (tmp / "tex" / "P.tex").write_text(
            "\\documentclass{article}\\title{T}\\begin{document}\\begin{abstract}A\\end{abstract}\\end{document}")
        (tmp / "zenodo.json").write_text(json.dumps(
            {"web_url": "https://w", "main_tex": "tex/P.tex", "code_url": URL}))
        def no_net(*a, **k): raise AssertionError("network used")
        with mock.patch.object(socket.socket, "connect", no_net), \
             mock.patch.object(zp, "REPO_ROOT", tmp.resolve()), \
             mock.patch.object(zp, "ZenodoClient", side_effect=AssertionError("client built")), \
             mock.patch.object(sys, "argv", ["zp", str(tmp), "--dry-run"]), \
             mock.patch.dict("os.environ", {}, clear=False):
            code, out, _ = run(zp.main)
        self.assertIsNone(code)
        self.assertIn(F, out)
        self.assertIn("Dry run", out)


class NewVersionFlow(unittest.TestCase):
    def test_happy_path_verifies_and_publishes(self):
        c = FakeClient()
        code, out, _ = run(new, c)
        self.assertIsNone(code)
        self.assertIn("PASS", out)
        self.assertNotIn("discard", c.names())

    def test_failure_after_newversion_discards_created_draft_once(self):
        for step in ("upload", "put", "publish"):
            c = FakeClient(fail_at=step, fail_exc=HTTPFail(400) if step == "publish" else None)
            code, _, err = run(new, c)
            self.assertNotEqual(code, None, step)
            discards = [x for x in c.calls if x[0] == "discard"]
            self.assertEqual(discards, [("discard", 99)], step)
            self.assertNotIn(("discard", 10), c.calls)

    def test_preexisting_draft_refused_zero_creates_zero_discards(self):
        for kw in ({"drafts": [{"id": 77, "conceptrecid": "5", "state": "unsubmitted"}]},
                   {"latest_draft": "https://zenodo.org/api/deposit/depositions/77"}):
            c = FakeClient(**kw)
            code, _, _ = run(new, c)
            self.assertIsInstance(code, str)
            self.assertIn("already exists", code)
            for n in ("newversion", "create", "edit", "discard", "put", "publish"):
                self.assertNotIn(n, c.names())

    def test_draft_listing_failure_fails_closed(self):
        c = FakeClient(fail_at="list_drafts")
        code, _, _ = run(new, c)
        self.assertIsInstance(code, str)
        self.assertNotIn("newversion", c.names())

    def test_discard_failure_is_loud(self):
        c = FakeClient(fail_at="upload", discard_status=500)
        code, _, err = run(new, c)
        self.assertIn("CLEANUP FAILED", code)
        self.assertIn("MANUAL CLEANUP", code)
        self.assertIn("/deposit/99", code)

    def test_custom_missing_after_put_aborts_before_publish_and_discards(self):
        c = FakeClient(put_keeps_custom=False)
        code, _, err = run(new, c)
        self.assertNotEqual(code, None)
        self.assertNotIn("publish", c.names())
        self.assertIn(("discard", 99), c.calls)
        self.assertIn("refusing to publish", err)

    def test_post_publish_mismatch_reports_fail_without_further_calls(self):
        c = FakeClient(published_custom="https://wrong")
        before = []
        code, _, err = run(new, c)
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertIn("FAIL", err)
        self.assertEqual(c.names()[-1], "get_record")
        self.assertNotIn("discard", c.names())

    def test_publish_failure_on_already_published_is_not_discarded(self):
        c = FakeClient(fail_at="publish", state_after_publish="done")
        with no_delays():
            code, _, _ = run(new, c)
        self.assertIn("PUBLISHED", str(code) + _)
        self.assertNotIn("discard", c.names())

    def test_first_publication_cleans_only_its_own_deposition(self):
        c = FakeClient(fail_at="upload")
        run(new, c, zid=None)
        self.assertEqual([x for x in c.calls if x[0] == "discard"], [("discard", 98)])
        self.assertNotIn("resolve", c.names())


class UpdateFlow(unittest.TestCase):
    def upd(self, c, code_url=URL, **k):
        return upd(c, code_url, **k)

    def test_update_keeps_other_custom_fields(self):
        c = FakeClient(live_custom={"code:programmingLanguage": ["py"], F: "https://old"})
        code, _, _ = run(self.upd, c)
        self.assertIsNone(code)
        self.assertEqual(c.meta_put["custom"]["code:programmingLanguage"], ["py"])
        self.assertEqual(c.meta_put["custom"][F], URL)

    def test_update_without_code_url_cannot_clear_existing_repository(self):
        c = FakeClient(live_custom={F: "https://keep"}, published_custom="https://keep")
        code, _, _ = run(self.upd, c, code_url="")
        self.assertIsNone(code)
        self.assertEqual(c.meta_put["custom"][F], "https://keep")

    def test_update_detects_dropped_custom_and_cleans_edit_session(self):
        c = FakeClient(live_custom={F: "https://keep"}, put_keeps_custom=False)
        code, _, _ = run(self.upd, c, code_url="")
        self.assertNotEqual(code, None)
        self.assertNotIn("publish", c.names())
        self.assertEqual([x for x in c.calls if x[0] == "discard"], [("discard", 10)])

    def test_update_refused_when_draft_exists(self):
        c = FakeClient(drafts=[{"id": 77, "conceptrecid": "5"}])
        code, _, _ = run(self.upd, c)
        self.assertIn("already exists", code)
        self.assertNotIn("edit", c.names())
        self.assertNotIn("discard", c.names())

    def test_update_post_publish_mismatch(self):
        c = FakeClient(published_custom=None)
        code, _, _ = run(self.upd, c)
        self.assertEqual(code[0], "POSTPUBLISH")


class RealClientState(unittest.TestCase):
    """The real client records what THIS run created, before any follow-up call can fail."""

    def test_new_version_records_draft_id_even_if_followup_get_fails(self):
        c, sess = real_client()
        sess.route("POST", "/actions/newversion", Resp(201, {"links": {
            "latest_draft": "https://zenodo.org/api/deposit/depositions/321"}}))
        sess.route("GET", "/depositions/321", RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            c.new_version(10)
        self.assertEqual(c.created_state, {"dep_id": 321, "kind": "new-version draft", "certain": True})

    def test_failed_newversion_records_nothing(self):
        c, sess = real_client()
        sess.route("POST", "/actions/newversion", Resp(409, text="conflict"))
        with self.assertRaises(zp.ZenodoAPIError):
            run_quiet(c.new_version, 10)
        self.assertIsNone(c.created_state)


def run_quiet(fn, *a):
    import contextlib, io
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


class SurvivingMutantKillers(unittest.TestCase):
    """Mutants that survived the first review: local state never saved, saved too late, listing cut short."""

    def test_on_published_called_once_with_result(self):
        c, seen = FakeClient(), []
        code, _, _ = run(new, c, on_published=seen.append)
        self.assertIsNone(code)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["conceptrecid"], "5")

    def test_local_state_saved_before_verification(self):
        c, order = FakeClient(), []
        c.events = order
        run(new, c, on_published=lambda r: order.append("saved"))
        self.assertEqual(order, ["saved", "verify"])

    def test_local_state_saved_even_when_verification_fails(self):
        c, order = FakeClient(published_custom="https://wrong"), []
        c.events = order
        with no_delays():
            code, _, _ = run(new, c, on_published=lambda r: order.append("saved"))
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertEqual(order[0], "saved")

    def test_list_drafts_reads_every_page(self):
        c, sess = real_client()
        pages = [[{"id": i} for i in (1, 2, 3)], [{"id": i} for i in (4, 5, 6)], [{"id": 7}], []]
        sess.route("GET", "/deposit/depositions", *[Resp(200, p) for p in pages])
        self.assertEqual([d["id"] for d in c.list_drafts()], [1, 2, 3, 4, 5, 6, 7])

    def test_draft_on_a_later_page_is_seen_by_the_guard(self):
        c, sess = real_client()
        sess.route("GET", "/records/5", Resp(200, {"id": 10}))
        sess.route("GET", "/depositions/10", Resp(200, {"id": 10, "state": "done", "conceptrecid": "5"}))
        sess.route("GET", "/deposit/depositions", Resp(200, [{"id": 1, "conceptrecid": "9"}]),
                   Resp(200, [{"id": 77, "conceptrecid": "5", "state": "unsubmitted"}]), Resp(200, []))
        code, _, _ = run(zp.refuse_if_unpublished_state, c, 5, 10, HOST)
        self.assertIn("already exists", code)
        self.assertIn("77", code)


if __name__ == "__main__":
    unittest.main()
