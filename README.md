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
python3 tools/zenodo_publish.py <paper_dir>/ --dry-run   # no token needed, no network: prints the exact metadata
python3 tools/zenodo_publish.py <paper_dir>/             # new version (or first publication)
python3 tools/zenodo_publish.py <paper_dir>/ --update    # metadata only, same DOI, PDF unchanged
python3 tools/zenodo_publish.py <paper_dir>/ --finish-after-fail   # offline, see "After a post-publish FAIL"
```

Run in the foreground, from the version branch (the branch name is the version label), after the
pre-deposit checks and the independent review. `python3 -m unittest discover -s tests` runs the offline
suite (about 110 tests, no network: a socket guard fails any real connection);
`python3 tests/mutation_check.py` re-applies the in-memory mutants and must report no survivor.

### Environment variables

| Variable | Used for |
|---|---|
| `ZENODO_TOKEN` | production host `zenodo.org` only |
| `ZENODO_SANDBOX_TOKEN` | `--sandbox` (`sandbox.zenodo.org`) only; there is **no fallback** to `ZENODO_TOKEN` |
| `ZENODO_CONNECT_TIMEOUT` / `ZENODO_READ_TIMEOUT` / `ZENODO_UPLOAD_TIMEOUT` | seconds, defaults 10 / 60 / 900 |
| `ZENODO_GET_ATTEMPTS` / `ZENODO_RETRY_BACKOFF` | GET attempts (default 3) and first back-off in seconds (1, 2, 4... capped at 30) |
| `ZENODO_VERIFY_ATTEMPTS` | post-publication read-backs (default 5, clamped to 1..6; delays 2, 4, 8, 16 s) |

`tools/.env` currently defines `ZENODO_TOKEN` (not `ZENODO_SANDBOX_TOKEN`). The script never reads that
file: whoever invokes it exports the variables first (the line above exports all of them, including the
production token, into the shell that runs the command; for a sandbox run export only
`ZENODO_SANDBOX_TOKEN` in a clean environment, e.g. `env -i PATH="$PATH" ZENODO_SANDBOX_TOKEN=... python3 ...`).
A missing or empty token stops the run before anything is parsed or sent. A token is never printed.
The client refuses to be built with a token equal to the other host's variable, and refuses to send a
request (bucket upload links included) to any host but its own, so the production token cannot reach the
sandbox, nor the sandbox token production.

### Guards of a production deposit

* **Existing draft: refusal.** A new version or `--update` first reads the latest record (its own
  `conceptrecid`, state must be exactly `done`, no `latest_draft` link) and lists **all** pages of the account's
  drafts. If a draft or edit session of the concept exists, or any of this cannot be established, it exits
  with nothing created and nothing discarded: inspect `https://zenodo.org/deposit` and publish or discard it
  by hand. The ids of all drafts seen are kept as the *pre-run snapshot*.
* **Pagination.** The draft listing reads pages until an empty one. It fails closed (the run refuses) if a
  page brings nothing new, an empty page advertises a next link, the server's total differs from what was
  read, or 50 pages are exceeded.
* **Cleanup of own state only.** On any failure, interruption (`Ctrl-C`) or `SystemExit` after a
  state-creating request, the tool discards (`actions/discard`) only the draft or edit session it certainly
  created, and re-raises. A discard that fails prints `CLEANUP FAILED ... MANUAL CLEANUP REQUIRED` with the
  URL. A draft that existed in the snapshot, or belongs to another concept, is never discarded.
* **Uncertain effects.** The intent to create is recorded *before* the request. If the request times out,
  the connection drops, or the answer is not JSON or lacks the draft id, the tool prints
  `UNCERTAINTY`, lists the drafts again and compares with the snapshot. Exactly one new unpublished draft of
  the concept (for a first publication: exactly one new unpublished draft) is attributed to this run and
  discarded; zero candidates means nothing is discarded and the message says the server may still show it
  later; several candidates mean nothing is discarded and the candidate URLs are printed. An answer of
  4xx (except 408) is a demonstrable refusal: nothing was created and the message says so.
* **Publish of unknown outcome.** A `publish` call that times out, is interrupted or returns 5xx discards
  nothing. The tool re-reads the record (bounded): if it is `done`, the publication is treated as confirmed
  (local state saved, verification continues); otherwise it prints that the publication may still complete.
  Only a 4xx answer to `publish` leads to a discard of the draft.
* **Blocking failures.** A failed upload, a failed removal of the previous version's file, or a draft whose
  file list is not exactly the new PDF aborts the run and cleans up its own draft; none is a warning.
* **Software field.** The record's Software section ("Repository URL") is the field
  `custom["code:codeRepository"]`, sent from `code_url` of `zenodo.json` (or derived from the git remote:
  a *moving* `tree/main/code` reference, so pin `code_url` to the commit for reproducibility). It is verified
  on the draft after the metadata PUT (mismatch: abort, discard own draft, nothing published) and on the public
  record after publishing, with up to 5 bounded read-backs (a trailing `/` is tolerated). A persistent mismatch
  is a loud `FAIL`; the record is **not** un-published. **When a paper has no `code_url` (and no `code/`
  directory), nothing is set or verified for a new version**, and whether the new-version draft inherits the
  previous version's repository is *not* known; `--update` preserves an existing repository field and verifies it.
* **Early local save.** `zenodo.json` (concept id and concept DOI) is written the moment a publication is
  confirmed, before any verification or archiving, also when a failed publish call is followed by a read
  showing `done`. A re-run is then a new version, never a second first publication. If that save fails the
  tool says so and tells you to set `zenodo_id` and `doi` by hand and **not** to re-run.
* **Timeouts and retries.** Every request has a bounded `(connect, read)` timeout. Only GET requests are
  retried (connection error, timeout, 429/502/503/504); POST, PUT and DELETE are never retried
  automatically, so a timeout on them goes through the uncertainty handling above.

### After a post-publish FAIL

The record is public, `zenodo.json` is saved, the PDF archive and the DOI cascade were skipped. Fix the
Software field with a reviewed `--update` or in the Zenodo UI, then run the skipped local steps offline:

```bash
python3 tools/zenodo_publish.py <paper_dir>/ --finish-after-fail [--no-cascade]
```

It reads `zenodo_id`/`doi` from `zenodo.json`, copies the newest PDF of the paper into `pdfs/` and runs
the DOI cascade; it needs no token and sends nothing. The PDF on disk must be the one that was deposited.

### Sandbox (`--sandbox`)

```bash
env -i PATH="$PATH" ZENODO_SANDBOX_TOKEN=... python3 tools/zenodo_publish.py <paper_dir>/ \
    --sandbox --sandbox-state "$TMPDIR/sandbox-result.json" [--sandbox-zenodo-id N] [--update]
```

* reads only `ZENODO_SANDBOX_TOKEN` (missing or empty: refuses to start, nothing sent);
* `--sandbox-state PATH` is mandatory and must lie outside the workspace (a repository could commit it);
  the sandbox result (ids, DOI, record) is written there and printed;
* a sandbox run never writes `zenodo.json`, never archives into `pdfs/`, never runs the DOI cascade, so no
  production identifier, DOI, archived PDF or bibliography changes (enforced by checksum tests);
* the production `zenodo_id` is **never** used on the sandbox: without `--sandbox-zenodo-id` the run is a
  first publication, with it a new version (or `--update`) of that sandbox record.

### The pilot rule

The deposit API's handling of `custom` was inferred from the zenodo-rdm sources and public records, not from
documentation. Before any batch, run the sandbox sequence (first publication with `code_url`, new version,
`--update`, existing-draft refusal, invalid custom value, interrupted upload, failed discard, post-publish
FAIL) and then **one** low-traffic production record, preferably with `--update` (no new DOI is minted). The
first production use follows an independent review of the exact commit of this tool and an explicit go.

### What only a real deposit can settle

That the deposit-API GET returns `metadata.custom` and `conceptrecid` (otherwise the guard refuses every
run: fail closed, unusable); that PUT accepts `custom` for `publication_type: workingpaper`; that an unknown
custom key gives a 400 rather than a silent drop; that `/records/<id>` returns `custom` right after publishing;
that a PUT without `custom` neither clears nor inherits it; that `status=draft` listings include edit sessions
and honour `size`/`page`; that a clean published record exposes no `latest_draft`; the status codes of
`actions/discard` for a draft and for an edit session; that the files list of a deposition uses `filename`.

## Credentials

`zenodo_publish.py` reads `ZENODO_TOKEN` (production) or `ZENODO_SANDBOX_TOKEN` (`--sandbox`) from its
environment; pushes and repository creation use `GH_TOKEN`; website deploys use `NETLIFY_PAT` as
`NETLIFY_AUTH_TOKEN`. These live in a local `tools/.env`, which is **git-ignored and must never be
committed** (this repository is public) and which no script of this directory loads by itself: the caller
exports what a command needs.

## Licence

CC BY 4.0, as for the rest of the programme.
