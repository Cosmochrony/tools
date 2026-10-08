#!/usr/bin/env python3
"""The reviewer's 43 mutants of review round 19 (Y1-Y38, Z1-Z5), ported onto the current code.

    python3 tests/mutation_check_review.py

Each pattern of the review was re-anchored on the code as it is after the third corrective pass (the code the
review attacked has changed shape: an archive written through a temp file and an exclusive link, journals bound
to a kind, and so on); the mutation itself is the review's. Exit 0 only when every mutant is killed except the
documented equivalents below; a skipped pattern, a red baseline or any other survivor exits non-zero.

Documented equivalents:
  Y1  `open(tmp, "xb")` -> `"wb"`: the archive copy is written under a UNIQUE temp name (uuid) and only then hard-linked to
      the final name; exclusivity of the FINAL name is os.link, which mutant A1 of tests/mutation_check.py replaces
      (and is killed). Changing the open mode of a name nobody else can have is unobservable.
Y7 and Z1 are the same mutation (the review's Y7 pattern was a typo, redone as Z1); both are kept.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402

EQUIVALENT_SURVIVORS = {"Y1"}

M = [
    ("Y1", "archive temp opened 'wb' instead of 'xb'", 'with open(tmp, "xb") as fh:\n            fh.write(data)', 'with open(tmp, "wb") as fh:\n            fh.write(data)'),
    ("Y2", "identical archive judged by size only", '        if existing["sha256"] == src["sha256"]:', '        if existing["size"] == src["size"]:'),
    ("Y3", "unwritable journal does not stop the request",
     '            raise JournalError(f"cannot write the run journal {self.path} ({redact(exc)}): "\n                               "refusing to send a publication-related request") from exc', '            return'),
    ("Y4", "sandbox journal reconcile writes production zenodo.json", '        writes_config = not data.get("sandbox") and data.get("kind") != "update"', '        writes_config = data.get("kind") != "update"'),
    ("Y5", "--set-title also applies the local version", '        elif key in built:\n            payload[key] = copy.deepcopy(built[key])', '        elif key in built:\n            payload[key] = copy.deepcopy(built[key]); payload["version"] = built.get("version", payload.get("version"))'),
    ("Y6", "version guard skipped by --skip-checks", '    if not args.finish_after_fail and not (args.update and "version" not in fields):', '    if not args.skip_checks and not args.finish_after_fail and not (args.update and "version" not in fields):'),
    ("Y7", "redact() does not replace registered secrets", '        out = out.replace(secret, "[REDACTED]")', '        pass'),
    ("Y8", "latest_draft GET failure treated as no draft", '        return [f"record {latest_id} advertises a latest_draft {link} that cannot be read ({redact(exc)})"]', '        return []'),
    ("Y9", "another unpublished latest_draft treated as published", '    if state == "done" and submitted is not False:\n        return []', '    if True:\n        return []'),
    ("Y10", "empty page with next link accepted", 'if links.get("next"):\n                    raise CompletenessError("the server returned an empty page', 'if False:\n                    raise CompletenessError("the server returned an empty page'),
    ("Y11", "finish-after-fail md5 check skipped", 'if rec.get("filename") == pdf_path.name and checksum and checksum != local["md5"]:', 'if False:'),
    ("Y12", "state-dir inside the workspace accepted", '        if p == root or root in p.parents:\n            die(f"Error: --state-dir', '        if False:\n            die(f"Error: --state-dir'),
    ("Y13", "no blocking by concept", '        if concepts and str(data.get("concept_id")) in concepts \\', '        if False and concepts and str(data.get("concept_id")) in concepts \\'),
    ("Y14", "unreadable journal never blocks", 'if path.name.startswith(re.sub(r"[^A-Za-z0-9_.-]", "_", paper_dir.name) + "-"):', 'if False:'),
    ("Y15", "unknown publish outcome marked NOT-PUBLISHED", 'mark("UNRECONCILED", f"publish of {dep_id} ended in doubt', 'mark("NOT-PUBLISHED", f"publish of {dep_id} ended in doubt'),
    ("Y16", "DO NOT RE-RUN wording removed", '"!! DO NOT RE-RUN this deposit', '"!! it is fine to run this deposit'),
    ("Y17", "upload does not compare with the journal digest", 'if expected_sha256 is not None and digest != expected_sha256:', 'if False:'),
    ("Y18", "pinned diff digest ignored", '    if not digest.startswith(pin):', '    if False:'),
    ("Y19", "implausible version string accepted", '    if not VERSION_SHAPE.fullmatch(v) or ".." in v:', '    if False:'),
    ("Y20", "branch rule skipped", '    if branch_rule:\n        branch = git_branch_raw(paper_dir)', '    if False:\n        branch = git_branch_raw(paper_dir)'),
    ("Y21", "CITATION.cff mismatch accepted", '    if cff is not None and cff != v:', '    if False:'),
    ("Y22", "finish-after-fail uses sandbox journals", '                    and not data.get("sandbox")\n                    and journal_kind', '                    and True\n                    and journal_kind'),
    ("Y23", "finish-after-fail concept mismatch ignored", '            if jdata.get("concept_id") not in (None, "") and str(jdata["concept_id"]) != str(zid):', '            if False:'),
    ("Y24", "unknown cleanup kind defaults to a DELETE", '    if key is None or key not in CLEANUP_CALLS:\n        raise UnknownCleanupKind(f"no cleanup call is defined for {state_kind!r}: nothing will be applied")', '    if key is None:\n        key = "unpublished-first-draft"'),
    ("Y25", "client constructor skips token-shape check", '        if isinstance(token, str) and token and not TOKEN_SHAPE.fullmatch(token):', '        if False:'),
    ("Y26", "finalize marks NOT-PUBLISHED even after a write", '    if int(journal.data.get("writes_sent") or 0) == 0 and not (client is not None and client.created_state):', '    if True:'),
    ("Y27", "unreadable live metadata accepted", '    if not isinstance(live, dict) or not live:', '    if False:'),
    ("Y28", "journal intent counter not incremented", 'self.data["writes_sent"] = int(self.data.get("writes_sent") or 0) + 1', 'self.data["writes_sent"] = int(self.data.get("writes_sent") or 0)'),
    ("Y29", "blocking-journal check ignores host", '                and (host is None or data.get("host") == host):', '                and True:'),
    ("Y30", "code-url replaces the whole custom dict", 'payload["custom"] = {**(live.get("custom") or {}), CODE_REPOSITORY_FIELD: code_url}', 'payload["custom"] = {CODE_REPOSITORY_FIELD: code_url}'),
    ("Y31", "edit-session cleanup discards without reading state", '        if dep is not None and dep.get("state") == "done":\n            mark("UNRECONCILED", f"{kind} {dep_id} is unexpectedly in state done"', '        if False:\n            mark("UNRECONCILED", f"{kind} {dep_id} is unexpectedly in state done"'),
    ("Y32", "archive copy not re-verified after write", '    if file_digests(dest)["sha256"] != src["sha256"]:\n        raise ArchiveRefused(f"the archived copy {dest} does not match the source after linking")', '    if False:\n        raise ArchiveRefused(f"the archived copy {dest} does not match the source after linking")'),
    ("Y33", "same-concept draft ignored by guard", 'if str(d.get("conceptrecid")) == str(concept) or d.get("id") == latest_id:', 'if d.get("id") == latest_id:'),
    ("Y34", "reconcile accepts non-blocking journals", '    if data.get("outcome") not in BLOCKING_OUTCOMES:\n        raise ReconcileRefused(f"the journal {path.name} is not unreconciled', '    if False:\n        raise ReconcileRefused(f"the journal {path.name} is not unreconciled'),
    ("Y35", "numeric env negative backoff accepted", '("ZENODO_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF, 0.0, False, 60.0)', '("ZENODO_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF, -1e9, False, 60.0)'),
    ("Y36", "Authorization sent to a hostname suffix match", 'parsed.hostname != self.host:', 'not parsed.hostname.endswith(self.host):'),
    ("Y37", "post-publish on_published catches Exception only", '        except BaseException as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included', '        except Exception as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included'),
    ("Y38", "DOI cascade runs even if archive refused", '            die(f"!! PUBLISHED (journal: PUBLISHED, zenodo.json saved) but the PDF archive failed or was "', '            print(f"!! PUBLISHED (journal: PUBLISHED, zenodo.json saved) but the PDF archive failed or was "'),
    ("Z1", "redact() does not replace registered secrets", '        out = out.replace(secret, "[REDACTED]")', '        pass'),
    ("Z2", "DELETE requests not journaled", 'if method != "GET" and self.journal is not None:', 'if method not in ("GET", "DELETE") and self.journal is not None:'),
    ("Z3", "journal records sandbox=False", '"sandbox": bool(sandbox), "kind"', '"sandbox": False, "kind"'),
    ("Z4", "finish-after-fail picks the OLDEST published journal", 'if best is None or str(data.get("started_at")) >= str(best[1].get("started_at")):', 'if best is None or str(data.get("started_at")) <= str(best[1].get("started_at")):'),
    ("Z5", "publish() does not mark the journal with the answer", '        body = self._ok(resp)\n        if j is not None:\n            j.mark_published(dep_id, body)\n        return body', '        body = self._ok(resp)\n        return body'),
]


def main() -> int:
    src = mc.SRC_PATH.read_text(encoding="utf-8")
    n, f, e = mc.run_suite(mc.load_mutated(src))
    print(f"baseline: {n} tests, {f} failures, {e} errors")
    if f or e:
        print("baseline is not green: cannot judge mutants")
        return 2
    survivors = []
    for mid, desc, old, new in M:
        if src.count(old) != 1:
            print(f"{mid:5} SKIPPED (pattern occurs {src.count(old)} times): {desc}", flush=True)
            survivors.append(mid)
            continue
        n, f, e = mc.run_suite(mc.load_mutated(src.replace(old, new)))
        killed = (f + e) > 0
        print(f"{mid:5} {'killed  ' if killed else 'SURVIVED'} ({f} failures, {e} errors of {n}): {desc}", flush=True)
        if not killed:
            survivors.append(mid)
    unexpected = [m for m in survivors if m not in EQUIVALENT_SURVIVORS]
    print(f"review mutants: {len(M)}, killed: {len(M) - len(survivors)}, survivors: {survivors or 'none'}")
    if unexpected:
        print(f"UNEXPECTED survivor(s): {unexpected}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
