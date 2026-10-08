"""Offline tests of tools/pilot/: the failure-injection harness (refusals and every case F-a..F-i against the
Sim double, on the SANDBOX host name) and the read-only checksum script."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakes  # noqa: E402
from fakes import SBX_TOKEN, FakeSession, Resp, Sim, make_paper, real_client, run, upd  # noqa: E402
import requests  # noqa: E402
import zenodo_publish as zp  # noqa: E402

PILOT = Path(__file__).resolve().parent.parent / "pilot"
spec = importlib.util.spec_from_file_location("inject_failures", PILOT / "inject_failures.py")
inject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inject)

PDF = None


def setUpModule():
    global PDF
    fakes.install_socket_guard()
    PDF = Path(tempfile.mkdtemp()) / "p.pdf"
    PDF.write_bytes(b"%PDF-fake")


def tearDownModule():
    fakes.remove_socket_guard()


class SandboxSim(Sim):
    """The Sim, answering with sandbox.zenodo.org links."""

    def _do(self, method, url, **kw):
        resp = super()._do(method, url, **kw)
        if isinstance(resp._body, (dict, list)):
            resp._body = json.loads(json.dumps(resp._body).replace("https://zenodo.org/", "https://sandbox.zenodo.org/"))
        return resp


class Restore:
    """Snapshot/restore everything the harness patches, so a case never leaks into another test."""

    def __enter__(self):
        self.ps = [mock.patch.object(zp.ZenodoClient, "_request", zp.ZenodoClient._request),
                   mock.patch.object(zp.ZenodoClient, "get_record", zp.ZenodoClient.get_record),
                   mock.patch.object(zp, "build_metadata", zp.build_metadata)]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()


def sandbox_new(sim, zid=5):
    client, _ = real_client(sim, sandbox=True, token=SBX_TOKEN)
    saved = []
    meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url=fakes.URL)
    res = run(zp.deposit_new, client, PDF, meta, fakes.URL, zid, "sandbox", "sandbox.zenodo.org",
              on_published=saved.append)
    return client, saved, res


class HarnessRefusals(unittest.TestCase):
    def args(self, *own, passthrough=("paper/", "--sandbox")):
        return inject.main([*own, "--", *passthrough], environ={})

    def test_refuses_without_sandbox_flag(self):
        with self.assertRaises(SystemExit) as cm:
            self.args("--case", "F-a")
        self.assertIn("needs --sandbox", str(cm.exception))

    def test_refuses_when_the_passthrough_has_no_sandbox(self):
        with self.assertRaises(SystemExit) as cm:
            self.args("--sandbox", "--case", "F-a", passthrough=("paper/",))
        self.assertIn("must contain --sandbox", str(cm.exception))

    def test_refuses_when_a_production_token_is_in_the_environment(self):
        with self.assertRaises(SystemExit) as cm:
            inject.main(["--sandbox", "--case", "F-a", "--", "p/", "--sandbox"], environ={"ZENODO_TOKEN": "x"})
        self.assertIn("ZENODO_TOKEN", str(cm.exception))

    def test_refuses_unknown_case_and_missing_separator(self):
        with self.assertRaises(SystemExit):
            self.args("--sandbox", "--case", "F-z")
        with self.assertRaises(SystemExit):
            inject.main(["--sandbox", "--case", "F-a"], environ={})

    def test_refuses_a_zenodo_publish_whose_sandbox_is_not_the_sandbox_host(self):
        fake = mock.Mock(SANDBOX_HOST="zenodo.org", SANDBOX_URL="https://zenodo.org/api")
        with self.assertRaises(SystemExit):
            inject.install("F-a", fake, requests)

    def test_a_request_to_any_other_host_is_refused_before_it_is_made(self):
        with Restore():
            inject.install("F-a", zp, requests)
            sess = FakeSession()
            c, _ = real_client(sess, sandbox=True, token=SBX_TOKEN)
            c.base = "https://zenodo.org/api"          # force a production URL past the client's own check
            c.host = "zenodo.org"
            with self.assertRaises(SystemExit) as cm:
                c._request("GET", "https://zenodo.org/api/records/1")
            self.assertIn("not on sandbox.zenodo.org", str(cm.exception))
            self.assertEqual(sess.calls, [])

    def test_main_runs_the_tool_with_the_passthrough_and_reports_injections(self):
        import contextlib
        import io
        with Restore(), mock.patch.object(zp, "main", return_value=None) as m, \
                contextlib.redirect_stdout(io.StringIO()):
            code = inject.main(["--sandbox", "--case", "F-b", "--", "paper/", "--sandbox", "--state-dir", "d"],
                               zp=zp, requests_mod=requests, environ={})
        self.assertEqual(code, 0)
        m.assert_called_once_with(["paper/", "--sandbox", "--state-dir", "d"])


class InjectedCases(unittest.TestCase):
    def case(self, name):
        stack = Restore()
        stack.__enter__()
        self.addCleanup(stack.__exit__)
        return inject.install(name, zp, requests)

    def test_F_a_lost_newversion_answer_is_found_and_cleaned_up(self):
        inj = self.case("F-a")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(len(inj), 1)
        self.assertIn("UNCERTAINTY", err)
        self.assertEqual(sim.discarded, [301])

    def test_F_b_newversion_never_sent(self):
        inj = self.case("F-b")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(len(inj), 1)
        self.assertEqual(sim.drafts, {})
        self.assertIn("No new-version draft attributable", code)

    def test_F_c_lost_publish_answer_is_recovered(self):
        inj = self.case("F-c")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(len(inj), 1)
        self.assertEqual(sim.published_ids, [301])
        self.assertEqual(len(saved), 1)
        self.assertEqual(sim.discarded, [])

    def test_F_d_publish_never_sent_discards_nothing(self):
        inj = self.case("F-d")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(len(inj), 1)
        self.assertEqual(sim.published_ids, [])
        self.assertEqual(sim.discarded, [])
        self.assertIn("NOTHING WAS DISCARDED", code)

    def test_F_e_ctrl_c_during_upload(self):
        inj = self.case("F-e")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(sim.discarded, [301])

    def test_F_f_ctrl_c_during_the_update_put(self):
        inj = self.case("F-f")
        sim = SandboxSim()
        client, _ = real_client(sim, sandbox=True, token=SBX_TOKEN)
        meta = zp.build_metadata("T", "A", [], "https://w", "1.0", code_url=fakes.URL)
        digest = fakes.reviewed_digest(client, meta, fakes.URL, ("title",), host="sandbox.zenodo.org",
                                       env_label="sandbox")
        code, _, err = run(zp.deposit_update, client, meta, fakes.URL, 5, "sandbox",
                           "sandbox.zenodo.org", fields=("title",), confirm=digest)
        self.assertEqual(code[0], "KEYBOARDINTERRUPT")
        self.assertEqual(len(inj), 1)
        self.assertEqual(sim.cleanup_calls, [("POST", 10)])

    def test_F_g_invalid_custom_never_reaches_publication(self):
        for name in ("F-g", "F-g2"):
            with self.subTest(name):
                inj = self.case(name)
                sim = SandboxSim()
                client, saved, (code, _, err) = sandbox_new(sim)
                self.assertTrue(inj)
                self.assertEqual(sim.published_ids, [])
                self.assertEqual(sim.discarded, [301])

    def test_F_h_discard_answered_500_is_loud_and_leaves_a_draft_behind(self):
        inj = self.case("F-h")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertIn("CLEANUP FAILED", code)
        self.assertEqual(sim.discarded, [])
        self.assertTrue(any("answered 500" in i for i in inj))

    def test_F_i_post_publish_mismatch(self):
        inj = self.case("F-i")
        sim = SandboxSim()
        client, saved, (code, _, err) = sandbox_new(sim)
        self.assertEqual(code[0], "POSTPUBLISH")
        self.assertIn("FAIL", err)
        self.assertEqual(sim.published_ids, [301])


class ChecksumScript(unittest.TestCase):
    SCRIPT = str(PILOT / "checksums_before_after.sh")

    def sh(self, *args):
        return subprocess.run(["sh", self.SCRIPT, *args], capture_output=True, text=True)

    def test_snapshot_is_stable_detects_changes_and_never_overwrites(self):
        root = Path(tempfile.mkdtemp())
        paper = make_paper(root)
        out = Path(tempfile.mkdtemp())
        r = self.sh("snapshot", "before", str(paper), str(root), str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("zenodo.json", r.stdout)
        r = self.sh("snapshot", "after", str(paper), str(root), str(out))
        self.assertEqual(self.sh("compare", str(out / "before.snapshot"), str(out / "after.snapshot")).returncode, 0)
        r = self.sh("snapshot", "before", str(paper), str(root), str(out))
        self.assertEqual(r.returncode, 2)                                  # refuses to overwrite
        (root / "pdfs" / "x.pdf").write_bytes(b"new")
        self.sh("snapshot", "changed", str(paper), str(root), str(out))
        self.assertEqual(self.sh("compare", str(out / "before.snapshot"), str(out / "changed.snapshot")).returncode, 1)

    def test_the_script_contains_no_deletion(self):
        text = Path(self.SCRIPT).read_text()
        for word in ("rm ", "rm\t", "unlink", "git clean", "git reset", "git checkout", "rmdir", " -delete"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
