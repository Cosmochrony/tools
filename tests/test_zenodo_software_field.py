"""Offline tests for the Software-field hardening of zenodo_publish.py.

No network: the HTTP layer is replaced by FakeClient; a socket guard fails any real connection.
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zenodo_publish as zp  # noqa: E402

F = zp.CODE_REPOSITORY_FIELD
URL = "https://github.com/Cosmochrony/x/tree/abc/code"
HOST = "zenodo.org"


class Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code, self.text, self.ok = status, text, status < 400


class FakeClient:
    """Mimics ZenodoClient; records every call. Behaviour is tuned per test."""

    def __init__(self, drafts=None, put_keeps_custom=True, published_custom=URL,
                 live_custom=None, fail_at=None, discard_status=201, latest_draft=None):
        self.calls, self.created_state = [], None
        self.drafts, self.put_keeps_custom = drafts or [], put_keeps_custom
        self.published_custom, self.live_custom = published_custom, live_custom or {}
        self.fail_at, self.discard_status, self.latest_draft = fail_at, discard_status, latest_draft
        self.meta_put = None
        self.state = "done"

    def _c(self, name, *a):
        self.calls.append((name,) + a)
        if self.fail_at == name:
            raise RuntimeError(f"boom at {name}")

    def resolve_latest_version_recid(self, r):
        self._c("resolve"); return 10
    def list_drafts(self):
        self._c("list_drafts"); return self.drafts
    def get_deposition(self, i):
        self._c("get_deposition", i)
        if i == 10 and self.created_state is None:
            return {"state": self.state, "links": {"latest_draft": self.latest_draft} if self.latest_draft else {},
                    "metadata": {"title": "t", "custom": dict(self.live_custom)}}
        meta = dict(self.meta_put or {"title": "t", "custom": dict(self.live_custom)})
        if self.meta_put is not None and not self.put_keeps_custom:
            meta.pop("custom", None)
        return {"state": "unsubmitted", "metadata": meta}
    def edit_deposition(self, i):
        self._c("edit", i); self.created_state = {"dep_id": i, "kind": "edit session"}; return {}
    def new_version(self, i):
        self._c("newversion", i); self.created_state = {"dep_id": 99, "kind": "new-version draft"}
        return {"id": 99, "files": [{"id": "f", "filename": "old.pdf"}], "links": {}}
    def create_deposition(self):
        self._c("create"); self.created_state = {"dep_id": 98, "kind": "new deposition draft"}
        return {"id": 98, "files": [], "links": {}}
    def delete_file(self, *a): self._c("delete_file")
    def upload_file(self, *a): self._c("upload")
    def set_metadata(self, i, m):
        self._c("put", i); self.meta_put = m
    def publish(self, i):
        self._c("publish", i)
        return {"id": i, "doi": "10.5281/zenodo.1", "conceptdoi": "10.5281/zenodo.0", "conceptrecid": "5",
                "metadata": {}}
    def discard(self, i):
        self._c("discard", i); return Resp(self.discard_status)
    def get_record(self, i):
        self._c("get_record", i)
        return {"metadata": {"custom": {F: self.published_custom} if self.published_custom else {}}}

    def names(self):
        return [c[0] for c in self.calls]


def run(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*a, **k)
        except SystemExit as e:
            code = e.code
        except zp.PostPublishFailure as e:
            code = ("POSTPUBLISH", str(e))
    return code, out.getvalue(), err.getvalue()


def new(client, code_url=URL, zid=5, **k):
    meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url=code_url)
    return zp.deposit_new(client, Path("/x/p.pdf"), meta, code_url, zid, "production", HOST, **k)


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
            c = FakeClient(fail_at=step)
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
        c = FakeClient(fail_at="publish")
        orig = c.get_deposition
        def gd(i):
            if i == 99 and "publish" in c.names():
                return {"state": "done", "metadata": {}}
            return orig(i)
        c.get_deposition = gd
        code, _, _ = run(new, c)
        self.assertIn("PUBLISHED", code)
        self.assertNotIn("discard", c.names())

    def test_first_publication_cleans_only_its_own_deposition(self):
        c = FakeClient(fail_at="upload")
        run(new, c, zid=None)
        self.assertEqual([x for x in c.calls if x[0] == "discard"], [("discard", 98)])
        self.assertNotIn("list_drafts", c.names())


class UpdateFlow(unittest.TestCase):
    def upd(self, c, code_url=URL, built_extra=None):
        meta = zp.build_metadata("T", "A", [], "https://w", "1.0", code_url=code_url)
        return zp.deposit_update(c, meta, code_url, 5, "production", HOST)

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

    def make(self):
        c = zp.ZenodoClient.__new__(zp.ZenodoClient)
        c.base, c.created_state = "https://z/api", None
        c.session = mock.Mock()
        return c

    def test_new_version_records_draft_id_even_if_followup_get_fails(self):
        c = self.make()
        post = Resp(); post.json = lambda: {"links": {"latest_draft": "https://z/api/deposit/depositions/321"}}
        c.session.post.return_value = post
        c.session.get.side_effect = RuntimeError("down")
        with self.assertRaises(RuntimeError):
            c.new_version(10)
        self.assertEqual(c.created_state, {"dep_id": 321, "kind": "new-version draft"})

    def test_failed_newversion_records_nothing(self):
        c = self.make()
        c.session.post.return_value = Resp(409, "conflict")
        c.session.post.return_value.raise_for_status = mock.Mock(side_effect=RuntimeError("409"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            c.new_version(10)
        self.assertIsNone(c.created_state)


if __name__ == "__main__":
    unittest.main()
