# Sandbox pilot protocol for `zenodo_publish.py`

Nothing in this directory has been run. It prepares the pilot that review round 18 (section 8) requires
before any production deposit. Every step below is an instruction for the operator, with placeholders.

## Files

| File | Role |
|---|---|
| `checksums_before_after.sh` | **Read-only** snapshot / compare of the production state (paper `zenodo.json`, `pdfs/`, bibliographies, `git status` of the paper and of `tools`). Never deletes, refuses to overwrite a snapshot. |
| `inject_failures.py` | Scratch harness: wraps `ZenodoClient._request` (and, for two cases, `build_metadata` / `get_record`) **in memory** to inject one failure case F-a..F-i, then calls `zenodo_publish.main`. Refuses unless `--sandbox` is given, `ZENODO_TOKEN` is absent from the environment and every request URL is `https://sandbox.zenodo.org/...`. The tool file is never modified. |

The tests of both (`tests/test_pilot_harness.py`) run offline against the `Sim` double.

## Prerequisites

* An account on `sandbox.zenodo.org` (a separate registration from zenodo.org) and a personal access token with
  scopes `deposit:write` and `deposit:actions`.
* The tool branch reviewed and pushed; the commit hash recorded in the pilot notes.
* A low-traffic paper on its **version branch**, `zenodo.json` `version` equal to the branch, and a pinned
  `code_url` (not the moving `tree/main/code`). Run `python3 tools/predeposit_check.py --deposit <paper>/` on the
  real paper first (it needs the network for `git ls-remote`; that is the pre-deposit battery, not the tool).
  `history-measure-power-lift-obstruction` passed in review; `trajectory-branching-note` did not (PDF metadata).
* No draft on the sandbox account (check `https://sandbox.zenodo.org/deposit` in the UI).

## Environment (every step)

```sh
cd <WORKSPACE>                                   # the @cosmochrony directory
SCR=<scratch directory OUTSIDE the workspace>    # e.g. a fresh directory under $TMPDIR
mkdir -p "$SCR/journals" "$SCR/snap"
read -rs SBX                                     # paste the sandbox token, nothing is echoed
# One scratch copy of the paper (keeps .git, so the branch rule still applies). Never run the pilot on the
# real paper directory: the sandbox must have nothing production to touch.
cp -R <PAPER_DIR> "$SCR/paper"
PAPER="$SCR/paper"
RUN() { env -i PATH="$PATH" HOME="$HOME" ZENODO_SANDBOX_TOKEN="$SBX" "$@"; }   # the ONLY token in the process
SNAP() { sh tools/pilot/checksums_before_after.sh snapshot "$1" "<PAPER_DIR>" "<WORKSPACE>" "$SCR/snap"; }
```

`env -i` leaves the production token out of the process, `--sandbox` reads only `ZENODO_SANDBOX_TOKEN`, and the
harness refuses if `ZENODO_TOKEN` is present anyway. Run every command in the foreground. After each step run
`SNAP after-Pn` and `sh tools/pilot/checksums_before_after.sh compare "$SCR/snap/before.snapshot" "$SCR/snap/after-Pn.snapshot"`:
**any difference in a production file is a NO-GO.** Take `SNAP before` once at the start.

Every sandbox command carries its own result file and the journal directory, both outside the workspace:
`--sandbox-state "$SCR/<step>.json"` (a **new** path each time: an existing file is refused) and
`--state-dir "$SCR/journals"`. A run that ends in doubt leaves an `UNRECONCILED` journal there, and the next run
refuses to start until `--reconcile` (see P6).

## P0 offline (no token)

```sh
python3 tools/zenodo_publish.py "$PAPER" --dry-run                      # prints the exact metadata incl. custom; the version-label verdict
python3 tools/zenodo_publish.py "$PAPER" --sandbox --sandbox-state "$SCR/p0.json" --state-dir "$SCR/journals"   # refuses: ZENODO_SANDBOX_TOKEN not set
python3 tools/zenodo_publish.py "$PAPER" --sandbox --sandbox-state "<WORKSPACE>/x.json"                          # refuses: inside the workspace
python3 tools/zenodo_publish.py "$PAPER" --dry-run --update --set-title --live-snapshot <LIVE_JSON>             # the diff, offline
```

## P1 first publication

```sh
RUN python3 tools/zenodo_publish.py "$PAPER" --sandbox --sandbox-state "$SCR/p1.json" --state-dir "$SCR/journals" --skip-checks
```

(`--skip-checks` only because the scratch copy is not the pushed paper; the version-label guard still runs.)
Read `$SCR/p1.json` (`record_id`, `conceptrecid`, `doi` 10.5072/...) and the journal in `$SCR/journals/`
(`outcome PUBLISHED`, `pdf.sha256`, `published_files`). Check on the sandbox with a public GET:
`metadata.custom["code:codeRepository"]` equals `code_url`, `metadata.version`, `related_identifiers`,
`files[].key`, `status: published`; the UI Software panel shows the Repository URL.
Expect: `Draft files verified: PASS`, `Software field verified on draft: PASS`, `Software field on published
record: PASS`, `[sandbox] PDF archive and DOI cascade skipped`.

## P2 new version

Bump the label on the scratch copy (branch **and** `zenodo.json` `version` and `CITATION.cff`, which must agree),
rebuild or modify the PDF, then:

```sh
RUN python3 tools/zenodo_publish.py "$PAPER" --sandbox --sandbox-zenodo-id <CONCEPT_RECID> \
    --sandbox-state "$SCR/p2.json" --state-dir "$SCR/journals" --skip-checks
```

Observes: the deposit GET of a published record (`state done`, `conceptrecid`, `files[].filename`,
`metadata.custom`), the absence (or self-pointing) of `links.latest_draft`, and that re-running with the **same**
label is refused by the tool ("equals the live version"). Then repeat with `code_url` removed from the scratch
`zenodo.json`: does the new-version draft inherit the previous `code:codeRepository`? (the tool prints that
nothing is verified in that case).

## P3 `--update`

`--update` now preserves the live metadata. First set "Programming language" by hand in the sandbox UI, then:

```sh
RUN python3 tools/zenodo_publish.py "$PAPER" --update --set-code-url --sandbox --sandbox-zenodo-id <CONCEPT_RECID> \
    --sandbox-state "$SCR/p3a.json" --state-dir "$SCR/journals" --skip-checks      # prints the diff and stops
RUN python3 tools/zenodo_publish.py "$PAPER" --update --set-code-url --yes-apply-diff <DIFF_SHA_PREFIX> --sandbox \
    --sandbox-zenodo-id <CONCEPT_RECID> --sandbox-state "$SCR/p3b.json" --state-dir "$SCR/journals" --skip-checks
```

(the first run creates nothing and leaves a non-blocking `NOT-PUBLISHED` journal.) Check: version, title,
description, keywords and the hand-set programming language are **unchanged**; only the repository changed.
Then `--set-title` with a changed local title to confirm that field-by-field application works.

## P4 existing draft

In the sandbox UI press "New version" on the record, then repeat P2's command: expect "an unpublished
draft/edit session of this concept already exists", exit 1, nothing created, draft intact. Repeat with `--update`.
Record what the deposit GET shows for the `links.latest_draft` of the published record while the draft exists.
Discard the draft by hand afterwards.

## P5 cleanup semantics (by hand, sandbox only)

For each of (i) a first-publication draft, (ii) a new-version draft, (iii) an edit session, (iv) a published
record not in edit: try `POST .../actions/discard` and `DELETE .../depositions/<id>` and record the status code and
the effect (use `curl` with the token read by `read -rs`, never written to a file). Then decide, and only then,
flip `CLEANUP_VERIFIED[kind]` and, if the table is wrong, `CLEANUP_CALLS[kind]` in `zenodo_publish.py` (a reviewed
commit). Until then every cleanup message says its server semantics are UNVERIFIED.

## P6 induced failures (harness)

Each case on a fresh scratch run (new `--sandbox-state`, journals reconciled between cases):

```sh
RUN python3 tools/pilot/inject_failures.py --sandbox --case <F-x> -- "$PAPER" --sandbox --sandbox-zenodo-id <CONCEPT_RECID> \
    --sandbox-state "$SCR/<F-x>.json" --state-dir "$SCR/journals" --skip-checks            # add --update ... for F-f
```

| case | injected | expected |
|---|---|---|
| F-a | real `newversion` request, answer dropped (Timeout) | `UNCERTAINTY`, search, attribution to this run, cleanup with the table call (check the real status code), or `NOTHING WAS DISCARDED` if ambiguous |
| F-b | `newversion` never sent | `No new-version draft attributable`, nothing discarded, non-blocking journal |
| F-c | real `publish`, answer dropped | poll sees `done`, state saved in the sandbox state file and journal, verification runs |
| F-d | `publish` never sent | `ended in doubt ... NOTHING WAS DISCARDED`, `DO NOT RE-RUN`, journal `UNRECONCILED`, draft left; the **next** run refuses to start; discard the draft by hand, then `--reconcile <journal> --outcome not-published` |
| F-e | Ctrl-C (KeyboardInterrupt) at the upload | own draft cleaned up, exit 130 |
| F-f | Ctrl-C at the `--update` PUT | edit session discarded (check the status code), exit 130 |
| F-g / F-g2 | invalid `custom` key / value type | 400 at the PUT or a draft check failure, own draft cleaned up |
| F-h | cleanup answered 500 (draft really left behind) | `CLEANUP FAILED`, journal `DRAFT-LEFT-BEHIND` with the manual URL; next run refuses until reconciled |
| F-i | public record read-back without `custom` | 5 reads (2/4/8/16 s), `FAIL`, state saved, exit non-zero |

Reconcile an unresolved journal locally (the tool sends nothing):

```sh
python3 tools/zenodo_publish.py "$PAPER" --reconcile "$SCR/journals/<JOURNAL>.run.json" --outcome not-published
python3 tools/zenodo_publish.py "$PAPER" --reconcile "$SCR/journals/<JOURNAL>.run.json" --outcome published --record-id <ID>
```

Also observe the listing: `GET /deposit/depositions?status=draft&size=100&all_versions=true` with two or more drafts
(`X-Total`, `Link`, page size) and whether an open edit session is listed. `--finish-after-fail` is production only
and offline: exercise it on a fake workspace (the tests do), never on the sandbox.

## P7 after every step

Compare the snapshots (above). Any production file change is a **NO-GO**. Not inducible safely: a publish answered
4xx (validation happens at the PUT), a real network partition beyond the harness, a concurrent draft created by
another actor, rate limiting (429).

## What to record (the unproven assumptions)

| # | Assumption in the tool | Observe | Where | Consequence if false |
|---|---|---|---|---|
| 1 | `GET /deposit/depositions?status=draft` lists drafts **and** open edit sessions of published records | create an edit session, list | P4, P6 | the guard cannot see an edit session of a published record; `--update` relies on `state`/`links` instead |
| 2 | `X-Total` / `X-Total-Count` / `Link: next` headers on the deposit list | headers of a 2+ draft listing | P6 | the empty-page stop is the only completeness proof (already authoritative); the header checks stay inert |
| 3 | `metadata.custom` is returned by the deposit GET and accepted by the PUT for `workingpaper` | GET after PUT; PUT with a bogus key | P1, P3, F-g | the guard or the draft check refuses every run (fail closed, unusable) |
| 4 | `links.latest_draft` is absent or self-pointing on a clean published record | GET of the published deposition | P2, P4 | a different value refuses every run until the rule is refined |
| 5 | `POST actions/discard` ends an **edit session** (201) | P5 (iii) | P5 | cleanup of `--update` leaves the session open (`DRAFT-LEFT-BEHIND`) |
| 6 | `DELETE /deposit/depositions/:id` removes an **unpublished** draft (201/204) and answers 403 on a published one | P5 (i), (ii), (iv) | P5 | cleanup of first/new-version drafts fails loudly (`DRAFT-LEFT-BEHIND`), never silently |
| 7 | `POST actions/discard` on a plain draft answers 400, `DELETE` on an edit session answers 403 | P5 | P5 | the table could be simplified |
| 8 | a new-version draft inherits `code:codeRepository` of the previous version | P2 without `code_url` | P2 | the no-`code_url` case needs an explicit `code_url` |
| 9 | the deposit GET returns `conceptrecid`, `state`, `files[].filename`, `submitted` | any GET | P1, P2 | the guard / file check refuse |
| 10 | `/records/<id>` returns `custom` right after publication | read-back timing | P1 | the 5 bounded reads may FAIL spuriously |
| 11 | the publish response carries `files[].checksum` (md5) | `$SCR/journals/*.run.json` `published_files` | P1 | `--finish-after-fail` falls back to the sha256 alone |
| 12 | `newversion` repeated while a draft exists is a no-op returning that draft | P4 | P4 | the snapshot-membership refusal is still the protection |
