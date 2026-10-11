#!/usr/bin/env python3
"""Scratch harness for the sandbox pilot: inject ONE failure case into zenodo_publish.py, then run it.

    python3 tools/pilot/sandbox_launch.py --case F-a -- \\
        "$SCR/paper" --sandbox --sandbox-state "$SCR/f-a.json" --sandbox-zenodo-id N \\
        --state-dir "$SCR/journals" --skip-checks

(sandbox_launch.py reads the token from the terminal and calls this harness in-process: the token is never in
an argument vector. Calling this file directly needs ZENODO_SANDBOX_TOKEN in the environment and is only for the
offline tests.)

Everything after `--` is handed to zenodo_publish.main unchanged. The tool file is NOT modified: the harness
wraps ZenodoClient._request (and, for two cases, build_metadata / get_record) in memory only.

SAFETY (the harness refuses otherwise, before importing anything networked):
  * the harness itself needs --sandbox, and the passed-through arguments must contain --sandbox;
  * ZENODO_TOKEN (production) must not be present in the environment;
  * every request URL must be https://sandbox.zenodo.org/...: any other host is refused BEFORE the real
    request is made, and the refusal is an exception, never a silent pass.

Cases (the letter is the review's F-a..F-i):
  F-a  lost response on newversion: the real request is made, the answer is dropped, Timeout is raised
  F-b  newversion request never sent: Timeout raised before the request
  F-c  lost response on publish: the real publish is made, the answer is dropped, Timeout is raised
  F-d  publish request never sent: Timeout raised before the request
  F-e  Ctrl-C during the file upload (KeyboardInterrupt instead of the PUT to the bucket)
  F-f  Ctrl-C during the --update metadata PUT (KeyboardInterrupt instead of PUT /deposit/depositions/ID)
  F-g  invalid custom value: build_metadata is made to emit custom={"code:bogus": "x"} (F-g2: a non-string)
  F-h  discard returning 500: the upload is made to fail, then the cleanup call is answered 500 (not sent)
  F-i  post-publish mismatch: get_record is made to return metadata without custom
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
SANDBOX_PREFIX = "https://sandbox.zenodo.org/"
CASES = {
    "F-a": "lost response on newversion", "F-b": "newversion request never sent",
    "F-c": "lost response on publish", "F-d": "publish request never sent",
    "F-e": "Ctrl-C during upload", "F-f": "Ctrl-C during the --update PUT",
    "F-g": "invalid custom key", "F-g2": "invalid custom value type",
    "F-h": "discard returning 500", "F-i": "post-publish mismatch",
}


class HarnessRefusal(SystemExit):
    pass


def refuse(message: str):
    raise HarnessRefusal(f"inject_failures: REFUSED: {message}")


def check_preconditions(own_args, passthrough, environ=None) -> None:
    environ = os.environ if environ is None else environ
    if not own_args.sandbox:
        refuse("the harness needs --sandbox (it never runs against production)")
    if "--sandbox" not in passthrough:
        refuse("the arguments after `--` must contain --sandbox")
    if environ.get("ZENODO_TOKEN"):
        refuse("ZENODO_TOKEN (production) is present in the environment: unset it and run through "
               "tools/pilot/sandbox_launch.py")
    if own_args.case not in CASES:
        refuse(f"unknown case {own_args.case!r} (known: {', '.join(CASES)})")


def install(case: str, zp, requests_mod):
    """Patch zp in memory for ``case``. Returns a list that records every injection made."""
    if zp.SANDBOX_HOST != "sandbox.zenodo.org" or not zp.SANDBOX_URL.startswith(SANDBOX_PREFIX):
        refuse("zenodo_publish does not point its sandbox at sandbox.zenodo.org")
    injected: list = []
    orig = zp.ZenodoClient._request
    Timeout = requests_mod.exceptions.Timeout

    class FakeResp:
        def __init__(self, status):
            self.status_code, self.ok, self.text = status, status < 400, "injected"
            self.headers, self.links = {}, {}

        def json(self):
            return {}

    def wrapped(self, method, url, **kw):
        if not str(url).startswith(SANDBOX_PREFIX):
            refuse(f"request to {url} is not on sandbox.zenodo.org: not sent")
        path = url.split("?")[0]
        if case == "F-a" and method == "POST" and path.endswith("/actions/newversion"):
            orig(self, method, url, **kw)
            injected.append("dropped the answer of newversion")
            raise Timeout("injected: answer lost")
        if case == "F-b" and method == "POST" and path.endswith("/actions/newversion"):
            injected.append("newversion never sent")
            raise Timeout("injected: request never sent")
        if case == "F-c" and method == "POST" and path.endswith("/actions/publish"):
            orig(self, method, url, **kw)
            injected.append("dropped the answer of publish")
            raise Timeout("injected: answer lost")
        if case == "F-d" and method == "POST" and path.endswith("/actions/publish"):
            injected.append("publish never sent")
            raise Timeout("injected: request never sent")
        if case == "F-e" and method == "PUT" and "/api/files/" in path:
            injected.append("Ctrl-C during upload")
            raise KeyboardInterrupt()
        if case == "F-f" and method == "PUT" and path.rsplit("/", 2)[-2] == "depositions":
            injected.append("Ctrl-C during the update PUT")
            raise KeyboardInterrupt()
        if case == "F-h":
            if method == "PUT" and "/api/files/" in path:
                injected.append("upload made to fail")
                raise requests_mod.exceptions.ConnectionError("injected: upload failed")
            is_cleanup = (method == "DELETE" and path.rsplit("/", 2)[-2] == "depositions") or \
                         (method == "POST" and path.endswith("/actions/discard"))
            if is_cleanup:
                injected.append(f"cleanup call {method} {path} answered 500 without being sent")
                return FakeResp(500)
        return orig(self, method, url, **kw)

    zp.ZenodoClient._request = wrapped

    if case in ("F-g", "F-g2"):
        build = zp.build_metadata

        def bogus(*a, **k):
            meta = build(*a, **k)
            meta["custom"] = {"code:bogus": "x"} if case == "F-g" else {zp.CODE_REPOSITORY_FIELD: 12345}
            injected.append(f"custom replaced by {meta['custom']!r}")
            return meta
        zp.build_metadata = bogus

    if case == "F-i":
        get_record = zp.ZenodoClient.get_record

        def stripped(self, rec_id):
            rec = get_record(self, rec_id)
            meta = dict(rec.get("metadata") or {})
            meta.pop("custom", None)
            injected.append("custom removed from the public record read-back")
            return {**rec, "metadata": meta}
        zp.ZenodoClient.get_record = stripped
    return injected


def main(argv=None, zp=None, requests_mod=None, environ=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        refuse("usage: inject_failures.py --sandbox --case F-x -- <zenodo_publish arguments>")
    cut = argv.index("--")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--case", required=True)
    own, passthrough = parser.parse_args(argv[:cut]), argv[cut + 1:]
    check_preconditions(own, passthrough, environ)
    if zp is None:
        sys.path.insert(0, str(TOOLS))
        import zenodo_publish as zp  # noqa: PLC0415
    if requests_mod is None:
        import requests as requests_mod  # noqa: PLC0415
    injected = install(own.case, zp, requests_mod)
    print(f"[inject_failures] case {own.case}: {CASES[own.case]}")
    try:
        zp.main(passthrough)
        status = 0
    except SystemExit as exc:
        print(f"[inject_failures] zenodo_publish exited with: {zp.redact(repr(exc.code))}")
        status = exc.code if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        print("[inject_failures] zenodo_publish ended with KeyboardInterrupt (exit 130 expected)")
        status = 130
    print(f"[inject_failures] injections made: {injected or 'NONE (the case never triggered)'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
