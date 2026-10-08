# Cosmochrony release tooling

Scripts supporting the publication cycle of the Cosmochrony research programme:
depositing papers to Zenodo, checking them before deposit, and keeping repository
metadata in sync.

Each paper of the programme lives in its own Git repository and is released from a
version branch (the branch name *is* the version; `main` is never published). These
tools operate on a paper directory containing a `zenodo.json`.

## Contents

| Script | Role |
|---|---|
| `predeposit_check.py` | Pre-deposit check battery — fails before an upload that would be wrong, misleading, or irreproducible |
| `zenodo_publish.py` | Deposits a paper to Zenodo (new version under its concept DOI) behind explicit guards, archives the versioned PDF, runs the DOI cascade (see [Zenodo deposits](#zenodo-deposits)) |
| `pilot/` | Sandbox pilot preparation: `README-PILOT.md` (protocol P0-P7), `checksums_before_after.sh` (read-only), `inject_failures.py` (failure-injection harness, sandbox only) |
| `repo_metadata_sync.py` | Syncs GitHub repository description/topics with the paper metadata |
| `zenodo.example.json` | Template for a paper's `zenodo.json` |

## Pre-deposit checks

A published Zenodo record cannot be deleted, so the battery is meant to fail *before*
the upload rather than to explain afterwards.

```bash
python3 tools/predeposit_check.py <paper_dir>            # one paper, resting state
python3 tools/predeposit_check.py --deposit <paper_dir>  # strict gate before an upload
python3 tools/predeposit_check.py --all                  # sweep every paper
```

Two modes, because the same fact means different things at different times:

* **resting sweep** (default) — a paper living on `main` between releases is normal;
  version labels are cross-checked against each other, and a paper never compiled
  locally is reported as a note.
* **`--deposit`** — the paper must sit on a version branch, every version label must
  agree with that branch, and a build must exist.

Exit code is 0 when every paper passes, 1 otherwise.

### What it catches

Every check corresponds to a defect actually found in this corpus:

* a stale `version` field in `zenodo.json` — it takes precedence over the git branch,
  so a deposit gets labelled with the *previous* version;
* a `CITATION.cff` left behind at an older version, or disagreeing with `zenodo.json`;
* a PDF cover that self-labels a different version than the branch it is released from;
* uncommitted edits newer than the build — the log and PDF predate them, so the
  reviewed artefact is not the one on disk;
* a log grep that silently returns nothing because the log is ISO-8859 rather than
  UTF-8 (a sanity probe fails loudly instead of reading as "no errors");
* undefined references/citations and LaTeX errors that `-interaction=nonstopmode` lets
  through while still writing a plausible PDF (font-shape substitutions are subtracted,
  since they are harmless and would otherwise mask the real signal);
* empty PDF metadata;
* a custom preamble macro that survived abstract expansion — only the paper's *own*
  macros are checked, so standard LaTeX in the abstract is not flagged;
* tildes and `\cite` leaking into the Zenodo description;
* a version branch that exists only on the local disk.

## Zenodo deposits

```bash
# Production. The script reads the PROCESS ENVIRONMENT only: it does NOT load tools/.env itself.
cd /path/to/@cosmochrony
export $(grep -v '^#' tools/.env | xargs)          # standard invocation (CLAUDE.md); exports every variable
python3 tools/zenodo_publish.py <paper_dir>/ --dry-run   # no token, no network: metadata, version-label verdict
python3 tools/zenodo_publish.py <paper_dir>/             # new version (or first publication)
python3 tools/zenodo_publish.py <paper_dir>/ --update --set-code-url            # prints the diff, creates nothing
python3 tools/zenodo_publish.py <paper_dir>/ --update --set-code-url --yes-apply-diff <DIFF_SHA256_PREFIX>
python3 tools/zenodo_publish.py <paper_dir>/ --finish-after-fail   # offline, see "After a post-publish FAIL"
python3 tools/zenodo_publish.py <paper_dir>/ --reconcile <JOURNAL> --outcome published|not-published   # offline
```

Run in the foreground, from the version branch (the branch name is the version label), after the
pre-deposit checks and the independent review. `python3 -m unittest discover -s tests` runs the offline
suite (about 210 tests, no network: a socket guard fails any real connection). `python3 tests/mutation_check.py`
re-applies the in-memory mutants; **its expected result is: every mutant killed except exactly one survivor,
`M16b`, a documented equivalent mutant** (it removes an unreachable defensive assertion; its reachable
counterpart M16 is killed). The script exits 0 only in that case and 1 for any other survivor, skipped pattern or
red baseline.

### Environment variables

| Variable | Used for |
|---|---|
| `ZENODO_TOKEN` | production host `zenodo.org` only |
| `ZENODO_SANDBOX_TOKEN` | `--sandbox` (`sandbox.zenodo.org`) only; there is **no fallback** to `ZENODO_TOKEN` |
| `ZENODO_CONNECT_TIMEOUT` / `ZENODO_READ_TIMEOUT` / `ZENODO_UPLOAD_TIMEOUT` | seconds, finite and > 0; defaults 10 / 60 / 900 |
| `ZENODO_GET_ATTEMPTS` / `ZENODO_RETRY_BACKOFF` | GET attempts (default 3) and first back-off in seconds (>= 0; 1, 2, 4... capped at 30) |
| `ZENODO_VERIFY_ATTEMPTS` | post-publication read-backs (default 5, clamped to 1..6; delays 2, 4, 8, 16 s) |
| `XDG_CACHE_HOME` | only to place the default `--state-dir` (`$XDG_CACHE_HOME` or `~/.cache`, then `cosmochrony-zenodo/`) |

Every numeric setting is validated at startup, **before any request** (NaN, infinity, non-numbers and
non-positive timeouts stop the run). `tools/.env` currently defines `ZENODO_TOKEN` (not `ZENODO_SANDBOX_TOKEN`).
The script never reads that file: whoever invokes it exports the variables first (the line above exports all of
them, including the production token, into the shell that runs the command; for a sandbox run export only
`ZENODO_SANDBOX_TOKEN` in a clean environment, e.g. `env -i PATH="$PATH" ZENODO_SANDBOX_TOKEN=... python3 ...`).
A missing or empty token stops the run before anything is parsed or sent. A token must be printable ASCII
without whitespace; one containing a newline, a space or a control character is refused **without being shown**.
A token is never printed: every exception text, HTTP body and URL that reaches the console is redacted of the
token and of any `Bearer ...` string. The client refuses to be built with a token equal to the other host's
variable, and refuses to send a request (bucket upload links included) to any host but its own, so the
production token cannot reach the sandbox, nor the sandbox token production.

### The run journal (`--state-dir`)

Before any publication-related write request leaves the process, and after each step, the tool writes the known
state atomically (temp file + `os.replace`) to `<--state-dir>/<paper>-<timestamp>-<pid>-<id>.run.json`
(default `$XDG_CACHE_HOME` or `~/.cache`, then `cosmochrony-zenodo/`; a directory inside the workspace or the
paper directory is refused, so a repository can never commit it). It records the phase, the deposition id, the
concept id, the intent flags, the host, the sha256/md5/size of the PDF actually uploaded and the outcome:

| outcome | meaning | blocks the next run |
|---|---|---|
| `RUNNING` | the run started and did not finish (killed, crashed) | yes |
| `UNRECONCILED` | a publication cannot be excluded (publish of unknown outcome, unreadable record, cleanup aborted) | yes |
| `DRAFT-LEFT-BEHIND` | a draft or edit session may remain: cleanup failed, was refused, or the draft could not be attributed | yes |
| `PUBLISHED` | published; `zenodo.json` saved (production) | no |
| `NOT-PUBLISHED` | nothing is published and nothing is left behind (refusal before any write, or a clean discard) | no |
| `*-RECONCILED` | set by `--reconcile` | no |

While a blocking journal exists for the same paper directory, or for the same concept on the same host, **every
later run refuses to start**, prints what is known and "DO NOT RE-RUN". After inspecting the record and
`https://<host>/deposit` yourself, state what you found (no network is used, nothing is verified by the tool):

```bash
python3 tools/zenodo_publish.py <paper_dir>/ --reconcile <JOURNAL> --outcome published --record-id N \
        [--concept-id C --concept-doi D]       # production: with both, also writes zenodo_id and doi to zenodo.json
python3 tools/zenodo_publish.py <paper_dir>/ --reconcile <JOURNAL> --outcome not-published   # after discarding any leftover draft
```

A sandbox journal never writes any `zenodo.json`. If the journal cannot be written before a write request, the
request is not sent. `Ctrl-C` right after `publish` returns, or any case where publication cannot be excluded,
prints the same "DO NOT RE-RUN" instruction and re-raises.

### Guards of a production deposit

* **Existing draft: refusal.** A new version or `--update` first reads the latest record (its own
  `conceptrecid`, state must be exactly `done`) and lists **all** pages of the account's drafts. If a draft or
  edit session of the concept exists, or any of this cannot be established, it exits with nothing created and
  nothing discarded: inspect `https://zenodo.org/deposit` and publish or discard it by hand. The ids of **all**
  drafts seen (every concept) are kept as the *pre-run snapshot*. A `links.latest_draft` that points to the record
  itself is not a draft; one that points elsewhere counts only if a GET of that id shows an unpublished state, and
  a link that cannot be read or judged refuses.
* **Pagination.** The draft listing reads pages until an empty one, with `status=draft`, `size=100`,
  `all_versions=true`. It fails closed if a page brings nothing new, an empty page advertises a next link, the
  server's total (`X-Total`/`X-Total-Count`, looked up case-insensitively, also on a real `requests` response)
  differs from what was read, or 50 pages are exceeded.
* **Cleanup of own state only, through an explicit table.** On any failure, interruption or `SystemExit` after a
  state-creating request, the tool removes only the draft or edit session it certainly created, with the call
  `CLEANUP_CALLS` gives for its kind: edit session -> `POST .../actions/discard`; unpublished new-version draft and
  unpublished first-publication draft -> `DELETE /deposit/depositions/:id`. A kind not in the table is refused.
  **Every entry is UNVERIFIED** (`CLEANUP_VERIFIED`) until the sandbox pilot confirms status codes and effects, and
  each cleanup message says so. A cleanup that fails, is refused or cannot be attributed prints
  `CLEANUP FAILED ... MANUAL CLEANUP REQUIRED` with the URL, and the journal says `DRAFT-LEFT-BEHIND`. A draft that
  existed in the snapshot, belongs to another concept, or is already `done`, is never removed.
* **Uncertain effects.** The intent to create is recorded *before* the request. If the request times out, the
  connection drops, or the answer is not JSON or lacks the draft id, the tool prints `UNCERTAINTY`, lists the
  drafts again and compares with the snapshot. Exactly one new unpublished draft of the concept is attributed to
  this run and cleaned up; zero candidates means nothing is removed; several candidates mean nothing is removed and
  the candidate URLs are printed (`DRAFT-LEFT-BEHIND`). A 4xx (except 408) is a demonstrable refusal.
* **Publish of unknown outcome.** A `publish` call that times out, is interrupted or returns 5xx removes
  nothing. The tool re-reads the record (bounded, errors logged): if it is `done`, the publication is treated as
  confirmed (local state saved, verification continues); otherwise the journal is `UNRECONCILED` and the message
  says **DO NOT RE-RUN**. Only a 4xx answer to `publish` leads to a cleanup of the draft. An unreadable record
  after a failed publish removes nothing.
* **Blocking failures.** A failed upload, a failed removal of the previous version's file, or a draft whose
  file list is not exactly the new PDF aborts the run and cleans up its own draft; none is a warning. The PDF is
  read once and the bytes sent are the bytes digested.
* **Software field.** The record's Software section ("Repository URL") is the field
  `custom["code:codeRepository"]`, sent from `code_url` of `zenodo.json` (or derived from the git remote:
  a *moving* `tree/main/code` reference, so pin `code_url` to the commit for reproducibility). It is verified
  on the draft after the metadata PUT (mismatch: abort, clean up own draft, nothing published) and on the public
  record after publishing, with up to 5 bounded read-backs (a trailing `/` is tolerated). A persistent mismatch
  is a loud `FAIL`; the record is **not** un-published. **When a paper has no `code_url` (and no `code/`
  directory), nothing is set or verified for a new version**, and whether the new-version draft inherits the
  previous version's repository is *not* known.
* **Early local save.** `zenodo.json` (concept id and concept DOI) is written the moment a publication is
  confirmed, before any verification or archiving, also when a failed publish call is followed by a read
  showing `done`. A re-run is then a new version, never a second first publication. If that save fails the
  tool says so and tells you to set `zenodo_id` and `doi` by hand and **not** to re-run (in `--sandbox`: that the
  result is in the journal and printed, and that no production `zenodo.json` is to be touched).
* **Timeouts and retries.** Every request has a bounded `(connect, read)` timeout. Only GET requests are
  retried (connection error, timeout, 429/502/503/504); POST, PUT and DELETE are never retried
  automatically, so a timeout on them goes through the uncertainty handling above.

### The version label

Independently of `--skip-checks` and of the pre-deposit gate, a non-dry run refuses an outgoing version that is
empty, is `main`/`master`/`HEAD` (any case), is not a plausible version string, differs from `zenodo.json`'s
`version`, differs from the git branch (or the paper is on `main`/`master`/a detached head), or differs from
`CITATION.cff`'s version. A new version whose label equals the **live** version of the record is refused before
anything is created. The pre-deposit gate (`predeposit_check`) fails closed: if it cannot be imported or raises,
the run refuses; `--skip-checks` bypasses that battery only, never the label guard, the journal block or the diff
confirmation. (A `--dry-run` prints the verdict as `REFUSED, a real run would stop` instead of exiting.)

### `--update`: the live metadata is preserved

`--update` builds the PUT from the **live** deposition's metadata and replaces only the fields you name:
`--set-version`, `--set-title`, `--set-abstract` (description), `--set-keywords`, `--set-notes`, `--set-web-url`
(related identifiers), `--set-publication-type`, `--set-code-url` (the repository field, other `custom` keys kept),
or `--apply-local-metadata=version,title,...`. With none of them the run is refused. **Before anything is created**
the tool prints the whole-metadata unified diff `live -> to-be-sent` and its sha256, and stops unless
`--yes-apply-diff` is given (optionally `--yes-apply-diff <sha256-prefix>` to pin the diff just reviewed). After
the edit session is opened the live metadata is read again; a difference from what the diff was built on aborts
and cleans up the edit session. `--dry-run --update --live-snapshot <file.json>` shows the same diff fully
offline from a saved copy of the deposition (or of its metadata). The labels, title, abstract and keywords of the
local state are therefore **never** applied by default (the earlier behaviour overwrote them).

### After a post-publish FAIL

The record is public, `zenodo.json` is saved, the PDF archive and the DOI cascade were skipped. Fix the
Software field with a reviewed `--update` or in the Zenodo UI, then run the skipped local steps offline:

```bash
python3 tools/zenodo_publish.py <paper_dir>/ --finish-after-fail [--no-cascade] [--expect-pdf-sha256 HEX]
```

It reads `zenodo_id`/`doi` from `zenodo.json`, and **refuses unless the newest PDF of the paper has the sha256
recorded in the run journal at upload time** (or the digest you pass with `--expect-pdf-sha256` when no journal
survives), and, when the publish response recorded one, the md5 Zenodo returned. It never overwrites a file of
`pdfs/`: an identical archive is reported as "already archived, identical", a different one is refused with both
digests. It needs no token and sends nothing. The archive step of a normal run follows the same rules.

### Sandbox (`--sandbox`)

```bash
env -i PATH="$PATH" ZENODO_SANDBOX_TOKEN=... python3 tools/zenodo_publish.py <paper_dir>/ \
    --sandbox --sandbox-state "$SCRATCH/result-1.json" --state-dir "$SCRATCH/journals" \
    [--sandbox-zenodo-id N] [--update --set-code-url --yes-apply-diff]
```

* reads only `ZENODO_SANDBOX_TOKEN` (missing or empty: refuses to start, nothing sent);
* `--sandbox-state PATH` is mandatory, must lie outside the workspace and **must not exist** (an existing file is
  refused, never overwritten); a unique temp file next to it is created **before any request** as a writability
  probe and is the file later moved into place, so no residue is left; the result is always printed in full, and if
  the file cannot be written after publication the message says so and never suggests editing a production file;
* a sandbox run uses the run journal like a production run, and never writes `zenodo.json`, never archives into
  `pdfs/`, never runs the DOI cascade, so no production identifier, DOI, archived PDF or bibliography changes
  (enforced by checksum tests);
* the production `zenodo_id` is **never** used on the sandbox: without `--sandbox-zenodo-id` the run is a
  first publication, with it a new version (or `--update`) of that sandbox record.

### The pilot rule

The deposit API's handling of `custom` was inferred from the zenodo-rdm sources and public records, not from
documentation. Before any batch, run the sandbox protocol of [`pilot/README-PILOT.md`](pilot/README-PILOT.md)
(first publication with `code_url`, new version, `--update`, existing-draft refusal, discard/DELETE semantics,
the induced failures F-a..F-i of `pilot/inject_failures.py`, with `pilot/checksums_before_after.sh` proving that no
production file changed), then **one** low-traffic production record, preferably with `--update` limited to the
field that needs changing (no new DOI is minted). The first production use follows an independent review of the
exact commit of this tool and an explicit go.

### What is still UNPROVEN (only a sandbox pilot or a real deposit can settle it)

* **Cleanup semantics.** That `POST actions/discard` ends an edit session, that `DELETE /deposit/depositions/:id`
  removes an unpublished draft and is refused (403) on a published record, and the status codes of both. The docs
  say so; nothing here has observed it. Until then a failed cleanup is a loud `DRAFT-LEFT-BEHIND`, never silent.
* **`custom`.** That the deposit-API GET returns `metadata.custom` and `conceptrecid` (otherwise the guard refuses
  every run: fail closed, unusable); that PUT accepts `custom` for `publication_type: workingpaper`; that an
  unknown custom key gives a 400 rather than a silent drop; that `/records/<id>` returns `custom` right after
  publishing; that a PUT without `custom` neither clears nor inherits it.
* **`links.latest_draft` on published records.** The documented sample of an *unpublished* deposition points to
  itself; whether a clean published deposition carries no link or a self-link is unobserved (both are handled; any
  other value costs one GET and refuses if it cannot be judged).
* **Header presence.** That the deposit list sends `X-Total`/`X-Total-Count` or `Link: next` is unknown; the empty
  page is the only stop that is relied on.
* **Listing scope.** Whether `status=draft` includes the edit sessions of published records and honours
  `size`/`page`; whether a new-version draft inherits `code:codeRepository` (matters only without `code_url`);
  whether the publish response carries `files[].checksum`.
* **Attribution.** It assumes no other actor creates drafts on the account during the few seconds of a run.
* **Redirects.** `requests` drops `Authorization` on a cross-host redirect; `_check_url` guards the request URL, not
  redirect targets.

## Credentials

`zenodo_publish.py` reads `ZENODO_TOKEN` (production) or `ZENODO_SANDBOX_TOKEN` (`--sandbox`) from its
environment; pushes and repository creation use `GH_TOKEN`; website deploys use `NETLIFY_PAT` as
`NETLIFY_AUTH_TOKEN`. These live in a local `tools/.env`, which is **git-ignored and must never be
committed** (this repository is public) and which no script of this directory loads by itself: the caller
exports what a command needs.

## Licence

CC BY 4.0, as for the rest of the programme.
