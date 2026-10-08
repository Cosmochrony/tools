"""Shared offline fixtures for the zenodo_publish tests.

Nothing here touches the network: HTTP is replaced by FakeSession / FakeClient, and install_socket_guard()
makes any real connection attempt fail the test run.
"""
import contextlib
import io
import json
import socket
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import zenodo_publish as zp  # noqa: E402

F = zp.CODE_REPOSITORY_FIELD
URL = "https://github.com/Cosmochrony/x/tree/abc/code"
HOST = "zenodo.org"
PROD_TOKEN = "PRODTOKEN-aaaa1111"
SBX_TOKEN = "SANDBOXTOKEN-bbbb2222"

_patches = []
SLEEPS = []   # every delay requested through zp._sleep during the tests (never actually slept)


def install_socket_guard():
    def no_net(*a, **k):
        raise AssertionError("NETWORK ACCESS ATTEMPTED IN AN OFFLINE TEST")
    for target, name in ((socket.socket, "connect"), (socket, "getaddrinfo"), (socket, "create_connection")):
        p = mock.patch.object(target, name, no_net)
        p.start()
        _patches.append(p)
    p = mock.patch.object(zp, "_sleep", SLEEPS.append)
    p.start()
    _patches.append(p)


def remove_socket_guard():
    while _patches:
        _patches.pop().stop()


class Resp:
    """Minimal requests.Response look-alike."""

    def __init__(self, status=200, body=None, text=None, headers=None, links=None, nonjson=False):
        self.status_code, self.ok = status, status < 400
        self._body, self._nonjson = body, nonjson
        self.text = text if text is not None else ("<html>oops</html>" if nonjson else json.dumps(body))
        self.headers = headers or {}
        self.links = links or {}

    def json(self):
        if self._nonjson:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Scripted requests.Session. route(method, url_substring, response|exception|callable|[list])."""

    def __init__(self):
        self.headers, self.calls, self.routes = {}, [], []

    def route(self, method, needle, *answers):
        self.routes.append([method, needle, list(answers)])

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for r in self.routes:
            needle = r[1]
            if r[0] == method and (url.endswith(needle[1:]) if needle.startswith("=") else needle in url):
                answers = r[2]
                ans = answers.pop(0) if len(answers) > 1 else answers[0]
                if callable(ans) and not isinstance(ans, Resp):
                    ans = ans(method, url, kw)
                if isinstance(ans, BaseException):
                    raise ans
                return ans
        raise AssertionError(f"unrouted request {method} {url}")

    def get(self, url, **kw): return self._do("GET", url, **kw)
    def post(self, url, **kw): return self._do("POST", url, **kw)
    def put(self, url, **kw): return self._do("PUT", url, **kw)
    def delete(self, url, **kw): return self._do("DELETE", url, **kw)

    def methods(self):
        return [(m, u) for m, u, _ in self.calls]

    def posted(self, suffix):
        return [u for m, u, _ in self.calls if m == "POST" and u.endswith(suffix)]


def real_client(session=None, sandbox=False, token=PROD_TOKEN, environ=None, **env):
    """The REAL ZenodoClient over a FakeSession, with zero sleeping."""
    session = session or FakeSession()
    base_env = {"ZENODO_RETRY_BACKOFF": "0"}
    base_env.update({k: str(v) for k, v in env.items()})
    c = zp.ZenodoClient(token, sandbox=sandbox, session=session,
                        environ=environ if environ is not None else base_env)
    c._sleep = lambda s: None
    return c, session


class HTTPFail(Exception):
    def __init__(self, status=400):
        self.response = Resp(status, text="refused")
        super().__init__(f"HTTP {status}")


class FakeClient:
    """Mimics ZenodoClient at the method level; records every call. Behaviour is tuned per test."""

    def __init__(self, drafts=None, put_keeps_custom=True, published_custom=URL, live_custom=None,
                 fail_at=None, fail_exc=None, discard_status=201, latest_draft=None,
                 uncertain_at=None, late_drafts=None, state_after_publish="unsubmitted",
                 record_state="done", draft_files=None, record_concept="5", draft_state=None,
                 unreadable_after_publish=False, record_version=None, record_headers=None):
        self.calls, self.created_state = [], None
        self.journal, self.uploaded_pdf = None, None
        self.draft_state, self.unreadable_after_publish = draft_state, unreadable_after_publish
        self.record_version = record_version
        self.discard_kinds = []
        self.drafts, self.put_keeps_custom = list(drafts or []), put_keeps_custom
        self.published_custom, self.live_custom = published_custom, live_custom or {}
        self.fail_at, self.fail_exc = fail_at, fail_exc
        self.discard_status, self.latest_draft = discard_status, latest_draft
        self.uncertain_at, self.late_drafts = uncertain_at, list(late_drafts or [])
        self.state_after_publish, self.record_state = state_after_publish, record_state
        self.draft_files, self.record_concept = (None if draft_files is None else list(draft_files)), record_concept
        self.files = []
        self.meta_put, self.events = None, []
        self.pre_run_draft_ids, self.pre_run_latest_state, self.concept_id = None, None, None
        self.record_reads_before_ok = 0

    def _c(self, name, *a):
        self.calls.append((name,) + a)
        if self.uncertain_at == name:
            raise TimeoutError(f"timeout at {name}")
        if self.fail_at == name:
            raise (self.fail_exc if self.fail_exc is not None else RuntimeError(f"boom at {name}"))

    def resolve_latest_version_recid(self, r):
        self._c("resolve"); return 10

    def list_drafts(self):
        self._c("list_drafts")
        late = self.late_drafts if any(c[0] in ("newversion", "create", "edit") for c in self.calls) else []
        return self.drafts + late

    def get_deposition(self, i):
        self._c("get_deposition", i)
        if self.unreadable_after_publish and "publish" in self.names() and i != 10:
            raise RuntimeError("read failed after publish")
        if i == 10 and self.created_state is None:
            rec = {"id": 10, "state": self.record_state, "conceptrecid": self.record_concept,
                   "links": {"latest_draft": self.latest_draft} if self.latest_draft else {},
                   "metadata": {"title": "t", "custom": dict(self.live_custom)}}
            if self.record_version is not None:
                rec["metadata"]["version"] = self.record_version
            return rec
        meta = dict(self.meta_put or {"title": "t", "custom": dict(self.live_custom)})
        if self.meta_put is not None and not self.put_keeps_custom:
            meta.pop("custom", None)
        state = self.state_after_publish if "publish" in self.names() else "unsubmitted"
        if self.draft_state is not None and i != 10:
            state = self.draft_state
        d = {"id": i, "state": state, "conceptrecid": "5", "metadata": meta,
             "files": [{"filename": n} for n in (self.files if self.draft_files is None else self.draft_files)]}
        if state == "done":
            d.update(doi="10.5281/zenodo.1", conceptdoi="10.5281/zenodo.0")
        return d

    def edit_deposition(self, i):
        self.created_state = {"dep_id": i, "kind": "edit session", "certain": False}
        self._c("edit", i)
        self.created_state["certain"] = True
        return {}

    def new_version(self, i):
        self.created_state = {"dep_id": None, "kind": "new-version draft", "certain": False}
        self._c("newversion", i)
        self.created_state = {"dep_id": 99, "kind": "new-version draft", "certain": True}
        self.files = ["old.pdf"]
        return {"id": 99, "files": [{"id": "f", "filename": "old.pdf"}], "links": {}}

    def create_deposition(self):
        self.created_state = {"dep_id": None, "kind": "new deposition draft", "certain": False}
        self._c("create")
        self.created_state = {"dep_id": 98, "kind": "new deposition draft", "certain": True}
        return {"id": 98, "files": [], "links": {}}

    def delete_file(self, *a):
        self._c("delete_file"); self.files = [n for n in self.files if n != "old.pdf"]

    def upload_file(self, dep, path, expected_sha256=None):
        self._c("upload"); self.files.append(Path(path).name)

    def set_metadata(self, i, m):
        self._c("put", i); self.meta_put = m

    def publish(self, i):
        self._c("publish", i)
        return {"id": i, "doi": "10.5281/zenodo.1", "conceptdoi": "10.5281/zenodo.0", "conceptrecid": "5",
                "metadata": {}}

    def discard(self, i, kind=None):
        self.discard_kinds.append(kind)
        self._c("discard", i); return Resp(self.discard_status)

    def get_record(self, i):
        self._c("get_record", i)
        self.events.append("verify")
        if self.record_reads_before_ok > 0:
            self.record_reads_before_ok -= 1
            return {"metadata": {"custom": {F: "https://stale"}}}
        return {"metadata": {"custom": {F: self.published_custom} if self.published_custom else {}}}

    def names(self):
        return [c[0] for c in self.calls]


def run(fn, *a, **k):
    """Call fn capturing stdout/stderr; returns (exit_code_or_exception, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*a, **k)
        except SystemExit as e:
            code = e.code
        except zp.PostPublishFailure as e:
            code = ("POSTPUBLISH", str(e))
        except KeyboardInterrupt as e:
            code = ("KEYBOARDINTERRUPT", str(e))
    return code, out.getvalue(), err.getvalue()


def new(client, code_url=URL, zid=5, pdf="/x/p.pdf", **k):
    meta = zp.build_metadata("T", "A", ["k"], "https://w", "1.0", code_url=code_url)
    return zp.deposit_new(client, Path(pdf), meta, code_url, zid, "production", HOST, **k)


def upd(client, code_url=URL, fields=("code-url", "title"), confirm="", **k):
    meta = zp.build_metadata("T", "A", [], "https://w", "1.0", code_url=code_url)
    return zp.deposit_update(client, meta, code_url, 5, "production", HOST, fields=fields, confirm=confirm, **k)


@contextlib.contextmanager
def no_delays():
    """Zero-delay verification / polling (already the default once install_socket_guard() ran)."""
    with mock.patch.object(zp, "_sleep", SLEEPS.append):
        yield


def make_paper(root: Path, name="paper", zenodo=None, with_bib=True, with_pdf=True):
    """A fake workspace: <root>/<name>/{tex,out,zenodo.json}, <root>/pdfs/, <root>/bib/*.bib."""
    paper = root / name
    (paper / "tex").mkdir(parents=True)
    (paper / "out").mkdir()
    (paper / "tex" / "P.tex").write_text(
        "\\documentclass{article}\\title{T}\\begin{document}\\begin{abstract}A\\end{abstract}\\end{document}")
    cfg = {"bib_key": "Beau2026x", "web_url": "https://w", "main_tex": "tex/P.tex",
           "code_url": URL, "version": "1.0", "zenodo_id": None, "doi": None}
    cfg.update(zenodo or {})
    (paper / "zenodo.json").write_text(json.dumps(cfg, indent=2) + "\n")
    if with_pdf:
        (paper / "out" / "P.pdf").write_bytes(b"%PDF-fake")
    (root / "pdfs").mkdir(exist_ok=True)
    if with_bib:
        (root / "bib").mkdir(exist_ok=True)
        (root / "bib" / "cosmochrony-bibliography.bib").write_text(
            "@article{Beau2026x,\n  title={{T}},\n  doi = {\\doi{10.0000/old}}\n}\n".replace("doi = {\\doi{10.0000/old}}", "note = {\\doi{10.0000/old}}"))
    return paper


def tree_checksums(root: Path) -> dict:
    import hashlib
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[str(p.relative_to(root)) + "/"] = "dir"
    return out


class Sim(FakeSession):
    """A tiny stateful Zenodo double behind the REAL client. Modes decide how each call misbehaves."""

    def __init__(self, **modes):
        super().__init__()
        import collections
        self.drafts, self.discarded, self.next_id = {}, [], 300
        self.modes = collections.defaultdict(lambda: "ok")
        self.modes.update(modes)
        self.record_state, self.record_concept = "done", "5"
        self.pub_custom, self.put_keeps_custom, self.discard_status = URL, True, 201
        self.list_page_cap = 100
        self.published_ids = []
        self.cleanup_calls = []

    def add_draft(self, dep_id, concept="5", state="unsubmitted"):
        self.drafts[dep_id] = {"id": dep_id, "conceptrecid": concept, "state": state, "files": [],
                               "metadata": {}}

    def _new_draft(self, with_old_file=True):
        self.next_id += 1
        self.add_draft(self.next_id)
        if with_old_file:
            self.drafts[self.next_id]["files"] = [{"id": "old", "filename": "old.pdf"}]
        return self.next_id

    def _dep_json(self, i):
        d = self.drafts[i]
        out = {"id": i, "state": d["state"], "conceptrecid": d["conceptrecid"],
               "metadata": d["metadata"], "files": d["files"],
               "links": {"bucket": f"https://zenodo.org/api/files/bucket-{i}"}}
        if d["state"] == "done":
            out.update(doi="10.5281/zenodo.1", conceptdoi="10.5281/zenodo.0")
        return out

    def _do(self, method, url, **kw):
        import re
        import requests
        self.calls.append((method, url, kw))
        m = self.modes
        path = url.split("zenodo.org", 1)[-1]
        if method == "GET":
            if path == "/api/records/5":
                return Resp(200, {"id": 10})
            if path == "/api/records/10":
                return Resp(200, {"id": 10, "metadata": {"custom": {F: self.pub_custom} if self.pub_custom else {}}})
            mr = re.fullmatch(r"/api/records/(\d+)", path)
            if mr and int(mr.group(1)) in self.drafts and int(mr.group(1)) != 10:
                d = self.drafts[int(mr.group(1))]
                return Resp(200, {"id": d["id"], "metadata": {"custom": d["metadata"].get("custom", {})}})
            if path == "/api/deposit/depositions":
                page = kw["params"]["page"]
                items = sorted((self._dep_json(i) for i, d in self.drafts.items()
                                if d["state"] in ("unsubmitted", "inprogress") and i != 10), key=lambda x: x["id"])
                if m["list"] == "ignore_page":
                    return Resp(200, items[:1])
                if m["list"] == "500":
                    return Resp(500, text="boom")
                cap = self.list_page_cap
                return Resp(200, items[(page - 1) * cap: page * cap])
            mm = re.fullmatch(r"/api/deposit/depositions/(\d+)", path)
            if mm:
                i = int(mm.group(1))
                if i == 10:
                    if 10 in self.drafts:
                        return Resp(200, self._dep_json(10))
                    return Resp(200, {"id": 10, "state": self.record_state, "conceptrecid": self.record_concept,
                                      "metadata": {"title": "t"}, "links": {}})
                if m[f"get{i}"] == "500":
                    return Resp(500, text="boom")
                return Resp(200, self._dep_json(i))
        if method == "POST":
            if path.endswith("/actions/newversion"):
                mode = m["newversion"]
                if mode == "409":
                    return Resp(409, text="conflict")
                if mode == "timeout_nothing":
                    raise requests.exceptions.Timeout("read timeout")
                i = self._new_draft()
                if mode == "twice":
                    self._new_draft()
                    raise requests.exceptions.ConnectionError("dropped")
                if mode == "timeout_created":
                    raise requests.exceptions.Timeout("read timeout")
                if mode == "interrupt":
                    raise KeyboardInterrupt()
                if mode == "nonjson":
                    return Resp(200, nonjson=True)
                if mode == "nolinks":
                    return Resp(201, {})
                return Resp(201, {"links": {"latest_draft": f"https://zenodo.org/api/deposit/depositions/{i}"}})
            if path.endswith("/actions/edit"):
                mode = m["edit"]
                if mode == "timeout_nothing":
                    raise requests.exceptions.Timeout("read timeout")
                self.drafts[10] = {"id": 10, "conceptrecid": self.record_concept, "state": "unsubmitted",
                                   "files": [], "metadata": {"title": "t", "custom": {}}}
                if mode == "timeout_created":
                    raise requests.exceptions.Timeout("read timeout")
                return Resp(201, {})
            if path.endswith("/actions/discard"):
                i = int(path.split("/")[-3])
                self.cleanup_calls.append(("POST", i))
                if i != 10:                         # documented: discard ends an EDITING SESSION only
                    return Resp(400, text="Deposition is not being edited")
                self.discarded.append(i)
                if self.discard_status < 400:
                    self.drafts.pop(10, None)
                return Resp(self.discard_status, text="discard answer")
            if path.endswith("/actions/publish"):
                i = int(path.split("/")[-3])
                mode = m["publish"]
                if mode == "400":
                    return Resp(400, text="validation")
                if mode in ("500_pending", "timeout_pending"):
                    if mode == "timeout_pending":
                        raise requests.exceptions.Timeout("read timeout")
                    return Resp(500, text="boom")
                if mode == "interrupt":
                    raise KeyboardInterrupt()
                self.drafts[i]["state"] = "done"
                self.published_ids.append(i)
                if mode == "timeout_published":
                    raise requests.exceptions.Timeout("read timeout")
                return Resp(202, {"id": i, "doi": "10.5281/zenodo.1", "conceptdoi": "10.5281/zenodo.0",
                                  "conceptrecid": "5", "metadata": {}})
            if path.endswith("/api/deposit/depositions"):
                if m["create"] == "timeout_created":
                    self._new_draft(with_old_file=False)
                    raise requests.exceptions.Timeout("read timeout")
                if m["create"] == "timeout_nothing":
                    raise requests.exceptions.Timeout("read timeout")
                i = self._new_draft(with_old_file=False)
                self.drafts[i]["conceptrecid"] = None
                return Resp(201, self._dep_json(i))
        if method == "PUT":
            if "/api/files/" in path:
                mode = m["upload"]
                if mode == "interrupt":
                    raise KeyboardInterrupt()
                if mode == "timeout":
                    raise requests.exceptions.Timeout("upload timeout")
                if mode == "500":
                    return Resp(500, text="boom")
                i = int(re.search(r"bucket-(\d+)", path).group(1))
                self.drafts[i]["files"].append({"id": "new", "filename": path.rsplit("/", 1)[-1]})
                return Resp(201, {"key": path.rsplit("/", 1)[-1]})
            mm = re.fullmatch(r"/api/deposit/depositions/(\d+)", path)
            if mm:
                if m["put"] == "interrupt":
                    raise KeyboardInterrupt()
                i = int(mm.group(1))
                meta = dict(kw["json"]["metadata"])
                if not self.put_keeps_custom:
                    meta.pop("custom", None)
                self.drafts[i]["metadata"] = meta
                return Resp(200, self._dep_json(i))
        if method == "DELETE" and re.fullmatch(r"/api/deposit/depositions/(\d+)", path):
            i = int(path.rsplit("/", 1)[-1])
            self.cleanup_calls.append(("DELETE", i))
            if i == 10 or self.drafts.get(i, {}).get("state") == "done":
                return Resp(403, text="published depositions cannot be deleted")
            self.discarded.append(i)
            if self.discard_status < 400:
                self.drafts.pop(i, None)
            return Resp(self.discard_status, text="discard answer")
        if method == "DELETE" and "/files/" in path:
            if m["delete"] == "500":
                return Resp(500, text="boom")
            i = int(path.split("/")[-3])
            self.drafts[i]["files"] = [f for f in self.drafts[i]["files"] if f["id"] != path.rsplit("/", 1)[-1]]
            return Resp(204, {})
        raise AssertionError(f"Sim: unhandled {method} {url}")
