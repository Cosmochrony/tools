#!/usr/bin/env python3
"""The 40 mutants Q1-Q40 of independent review round 21 (the reviewer's own, `r21/my_mut4.py`), ported onto the current code.

    python3 tests/mutation_check_review21.py [--shard K/N] [--ids Q6,Q38]

Only the PATH was adapted (it now reads the repository this script lives in); the 11 patterns whose anchor moved with
the fifth corrective pass were re-anchored on the new code, keeping the reviewer's mutation (Q16 and Q21 targeted the
removed `--quarantine`: they now target the equivalent guards of `--backup-journal` / `--lift-block`). The two mutants
that survived the fourth pass, Q6 (`validate_id` with `\\d`) and Q38 (the concept id read from the journal not validated),
are killed by tests/test_zenodo_round5.py. Exit 0 only when every mutant is killed (there is no documented equivalent).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402

M = [
    ('Q1', 'reconcile skips the identity digest',
     '    problems = journal_identity_problems(data)\n    if problems:',
     '    problems = journal_identity_problems(data)\n    if False:'),
    ('Q2', 'digest present but never compared',
     '    if stored != identity_digest(data):',
     '    if False:'),
    ('Q3', 'write path from paper_dir, not paper_dir_real',
     'cfg_path = Path(recorded_real) / "zenodo.json"',
     'cfg_path = Path(jdir) / "zenodo.json"'),
    ('Q4', 'realpath(paper_dir) vs recorded real not compared',
     'if _realpath(jdir) != recorded_real or _realpath(recorded_real) != recorded_real:',
     'if False:'),
    ('Q5', 'unknown outcome treated as non-blocking',
     '        if known and outcome in NON_BLOCKING_OUTCOMES:\n            continue',
     '        if outcome not in BLOCKING_OUTCOMES:\n            continue'),
    ('Q6', "validate_id accepts the digits of other scripts (the reviewer's \\d)",
     '    return isinstance(text, str) and text != "" and all(c in ASCII_DIGITS for c in text)',
     '    return isinstance(text, str) and text != "" and text.isdigit()'),
    ('Q7', 'DOI number compared by startswith',
     'and number != str(concept_id):',
     'and not number.startswith(str(concept_id)):'),
    ('Q8', 'URL host compared by endswith',
     'parts.hostname == host',
     "(parts.hostname or '').endswith(host)"),
    ('Q9', 'URL host compared by substring in netloc',
     'parts.hostname == host',
     "host in (parts.netloc or '')"),
    ('Q10', 'X-Api-Key no longer a sensitive name',
     'r"x[-_]api[-_]key", ',
     ''),
    ('Q11', 'credential-named dict key value not masked',
     '("[REDACTED]" if is_sensitive_name(k) else redact_obj(v))',
     'redact_obj(v)'),
    ('Q12', 'short token not registered (len>=8 again)',
     '        if isinstance(raw, str):\n            register_secret(raw)',
     '        if isinstance(raw, str) and len(raw.strip()) >= 8:\n            register_secret(raw)'),
    ('Q13', 'typeless entry replaced implicitly',
     'if target.get("resource_type") is None:',
     'if False:'),
    ('Q14', 'override with several matches takes the first',
     'if len(named) != 1:',
     'if len(named) < 1:'),
    ('Q15', 'reason not validated first',
     '    reason_text = validate_reason(reason) if reason not in (None, "") else None',
     '    reason_text = reason'),
    ('Q16', "the backup overwrites an existing file (the reviewer's quarantine overwrite)",
     '        _create_exclusive_bytes(target, raw)\n    except FileExistsError as exc:',
     '        target.write_bytes(raw)\n    except FileExistsError as exc:'),
    ('Q17', 'git env drops PATH',
     '_GIT_ENV_KEEP = ("PATH", "HOME",',
     '_GIT_ENV_KEEP = ("HOME",'),
    ('Q18', 'leading zero ids allowed',
     '    if not _is_ascii_digits(text) or text[0] == "0" or len(text) > MAX_ID_DIGITS:',
     '    if not _is_ascii_digits(text) or len(text) > MAX_ID_DIGITS:'),
    ('Q19', 'reason not stripped before length test',
     'text = reason.strip() if isinstance(reason, str) else ""',
     'text = reason if isinstance(reason, str) else ""'),
    ('Q20', 'file named after the paper no longer blocks unknown outcomes',
     '            if _named_after(path, paper_dir):\n                why.append("file named after this paper")',
     "            if False:\n                why.append('x')"),
    ('Q21', "lift-block accepts a journal with a known outcome and a consistent identity (the reviewer's quarantine of a known outcome)",
     '    if kind not in LIFTABLE_KINDS:\n        outcome = (data or {}).get("outcome")',
     '    if False:\n        outcome = (data or {}).get("outcome")'),
    ('Q22', 'content-hash check of zenodo.json removed',
     'if config_core_fingerprint(cfg) != data["config_core_sha256"]:',
     'if False:'),
    ('Q23', 'bib_key check of zenodo.json removed',
     'if cfg.get("bib_key") != data["bib_key"]:',
     'if False:'),
    ('Q24', 'missing identity fields accepted',
     '            if missing:\n',
     '            if False:\n'),
    ('Q25', 'vanished directory not detected',
     '            if not os.path.isdir(recorded_real):',
     '            if False:'),
    ('Q26', 'record URL userinfo accepted',
     'and parts.username is None\n              and parts.password is None and parts.port is None',
     'and parts.port is None'),
    ('Q27', 'web host suffix without the dot',
     'host.endswith("." + WEB_HOST)',
     'host.endswith(WEB_HOST)'),
    ('Q28', 'record id not looked for in the URL',
     '            known_id = rid or data.get("record_id") or data.get("deposition_id")',
     '            known_id = None'),
    ('Q29', 'key=value fields not masked',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon, _pass_field_eq)',
     '_PATTERN_PASSES = (_pass_scheme, _pass_field_colon)'),
    ('Q30', 'paper_dir_real not part of the identity digest',
     '"schema", "paper", "paper_dir", "paper_dir_real", "host"',
     '"schema", "paper", "paper_dir", "host"'),
    ('Q31', 'directory fsync of the atomic write removed',
     'raise AtomicWriteError(path, tmp if os.path.lexists(tmp) else None, exc) from exc\n    _fsync_dir(path.parent)',
     'raise AtomicWriteError(path, tmp if os.path.lexists(tmp) else None, exc) from exc'),
    ('Q32', 'zenodo DOI vs concept equality removed',
     '    if concept_id not in (None, "") and number != str(concept_id):',
     '    if False:'),
    ('Q33', 'only the production token registered',
     '    for name in (PRODUCTION_TOKEN_VAR, SANDBOX_TOKEN_VAR):\n        raw = environ.get(name)',
     '    for name in (PRODUCTION_TOKEN_VAR,):\n        raw = environ.get(name)'),
    ('Q34', 'cosmochrony.org narrowing removed',
     'web_like = [i for i in same_role if _is_web_host(items[i].get("identifier")) or items[i].get("identifier") in known]',
     'web_like = [i for i in same_role if items[i].get("identifier") in known]'),
    ('Q35', 'replace identifier not forwarded to merge',
     'payload[key] = merge_related_web_url(live.get(key), entries[0], replace_identifier, notes)',
     'payload[key] = merge_related_web_url(live.get(key), entries[0], None, notes)'),
    ('Q36', 'DOI shape check removed',
     '    if registrant not in allowed or not rest.startswith(DOI_SUFFIX_WORD):',
     '    if False:'),
    ('Q37', 'unknown outcome reconcile not refused',
     '    if not known:\n        raise ReconcileRefused(f"the journal {path.name} has an absent',
     '    if False:\n        raise ReconcileRefused(f"the journal {path.name} has an absent'),
    ('Q38', 'reconcile concept id from journal not validated',
     'cid = cid_arg or (validate_id(data.get("concept_id"), "the concept id recorded in the journal")',
     'cid = cid_arg or (str(data.get("concept_id"))'),
    ('Q39', 'zenodo_id already set to another concept accepted',
     'if cfg.get("zenodo_id") not in (None, "") and str(cfg["zenodo_id"]) != str(cid):',
     'if False:'),
    ('Q40', 'digest excludes host',
     '"paper_dir_real", "host", "sandbox"',
     '"paper_dir_real", "sandbox"'),
]


def main() -> int:
    src = mc.SRC_PATH.read_text(encoding="utf-8")
    n, f, e = mc.run_suite(mc.load_mutated(src))
    print(f"baseline: {n} tests, {f} failures, {e} errors")
    if f or e:
        print("baseline is not green: cannot judge mutants")
        return 2
    chosen = M
    if "--ids" in sys.argv:
        wanted = set(sys.argv[sys.argv.index("--ids") + 1].split(","))
        chosen = [m for m in M if m[0] in wanted]
    chosen = mc.shard_of(chosen, sys.argv[1:])
    survivors = []
    for mid, desc, old, new in chosen:
        if src.count(old) != 1:
            print(f"{mid:5} SKIPPED (pattern occurs {src.count(old)} times): {desc}", flush=True)
            survivors.append(mid)
            continue
        n, f, e = mc.run_suite(mc.load_mutated(src.replace(old, new)))
        killed = (f + e) > 0
        print(f"{mid:5} {'killed  ' if killed else 'SURVIVED'} ({f} failures, {e} errors of {n}): {desc}", flush=True)
        if not killed:
            survivors.append(mid)
    print(f"review-21 mutants: {len(chosen)}, killed: {len(chosen) - len(survivors)}, survivors: {survivors or 'none'}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
