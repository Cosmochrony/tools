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
| `zenodo_publish.py` | Deposits a paper to Zenodo (new version under its concept DOI), archives the versioned PDF, runs the DOI cascade |
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

## Credentials

`zenodo_publish.py` reads `ZENODO_TOKEN`; pushes and repository creation use `GH_TOKEN`;
website deploys use `NETLIFY_PAT` as `NETLIFY_AUTH_TOKEN`. These live in a local
`tools/.env`, which is **git-ignored and must never be committed** — this repository is
public.

## Licence

CC BY 4.0, as for the rest of the programme.
