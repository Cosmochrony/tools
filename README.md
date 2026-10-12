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
| `pilot/` | Sandbox pilot preparation: `README-PILOT.md` (protocol P0-P7), `sandbox_launch.py` (token read from the terminal, never in an argument vector), `checksums_before_after.sh` (read-only), `inject_failures.py` (failure-injection harness, sandbox only) |
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

> **PRODUCTION USE REMAINS NO-GO UNTIL A SUCCESSFUL SANDBOX PILOT.** Everything below is tested offline against
> doubles of the Zenodo API; no line of it has been observed against a server (see
> [What is still UNPROVEN](#what-is-still-unproven-only-a-sandbox-pilot-or-a-real-deposit-can-settle-it)).
> The pilot protocol is [`pilot/README-PILOT.md`](pilot/README-PILOT.md); the first production use follows an
> independent review of the exact commit and an explicit go.

**Breaking changes of the third corrective pass.** `--yes-apply-diff` now REQUIRES the sha256 prefix (at least 12
hex characters) of the diff you reviewed: the bare flag, and any shorter value, are refused. A new blocking journal
outcome `PUBLISHED-UNSAVED` exists. `--reconcile ... --outcome not-published` now needs evidence
(`--record-url URL`, or `--i-checked-the-account --reason "..."`), and a production deposit is only reconciled as
`published` together with `--concept-id` and `--concept-doi`. `--finish-after-fail` is bound to one deposit journal
and must run with the paper at the deposited version. Environment settings above their upper bounds are refused.

**Breaking changes of the fourth corrective pass (review round 20).** A journal whose `outcome` is absent or not one
of the known names now BLOCKS (fail closed); its cure is `--lift-block` (fifth pass; the fourth pass had a rename).
A `--reconcile` refuses a journal whose identity digest is missing or does not match (journals written by earlier
versions of this tool, and hand-written ones, can no longer be reconciled: they are lifted by a decision, see below),
and a production `published` reconcile writes only `<paper_dir_real>/zenodo.json`. `--record-id` and `--concept-id`
must be positive integers written as digits, `--record-url` must be an https URL on the journal's host containing
the record id, `--reason` must have at least 10 characters once stripped. `--set-web-url` never replaces a live
related identifier that states no `resource_type` (see `--replace-related-identifier`). Secrets are masked by field
name as well as by value.

**Breaking changes of the fifth corrective pass (review round 21).**
* `--quarantine` is REMOVED (it is refused with a pointer): it renamed the journal, which was a dead end for a journal
  of known outcome and a modification of the evidence. It is replaced by two separate steps. `--backup-journal JOURNAL`
  only COPIES the journal beside itself to `<name>.quarantine-copy-<UTC stamp>-<sha256 prefix>.json` (exclusive create,
  fsynced, never overwrites; nothing is unblocked). `--lift-block JOURNAL --reason TEXT (--record-url URL |
  --i-checked-the-account)` records your decision in a SEPARATE file `<journal>.decision.json` (exclusive create)
  holding the journal's sha256, who, when, the reason, the evidence and the decision `UNBLOCK`; it makes the backup
  copy first if none with the same sha256 exists. The journal is never renamed, edited or deleted. The block check
  honours a decision only if its sha256 equals the journal's CURRENT bytes (edit the journal afterwards and it blocks
  again), only for this journal's name and block kind, and only for the kinds no reconcile can cure: unreadable;
  absent or unknown outcome; identity digest absent or mismatching (an older version, hand-written, edited); paper
  directory moved or retargeted. A journal with a known outcome, a consistent identity and an intact directory is
  reconciled (`--outcome ...`), not lifted.
* `--concept-doi` must be exactly `10.5281/zenodo.<concept id>` in ASCII (registrant `10.5072` on the sandbox): digits
  `0-9` only, no leading zero, no space, `zenodo` in lower case, the number EQUAL to the concept id (not a prefix of
  it), no other registrant. A `zenodo.json` that already holds a different `doi` or `zenodo_id` is refused with both
  values (an identical value is accepted and not rewritten); a DOI is never silently replaced.
* `--set-web-url` replaces a related identifier only when it is `isSupplementedBy`, scheme `url`, resource type
  `other` AND on `cosmochrony.org`, or equal to the new value. A same-role entry on another host is KEPT (the run says
  so) and the web entry is appended; `cites` DOIs and every other entry are always kept. For a CHANGE of host use
  `--replace-related-identifier OLD_IDENTIFIER`: exactly one live entry must match the identifier exactly AND be of the
  web entry's own role (relation `isSupplementedBy`, scheme `url`); a `cites` / `isCitedBy` / `references` /
  `isReferencedBy` entry, a DOI-scheme entry and every other relation or scheme (`isVersionOf`, `isPartOf`, `hasPart`,
  handle, urn, ...) is refused; the replaced entry is printed with the diff. (A `previous_web_url` key that an older
  `zenodo.json` may still hold is ignored: it is no longer read.)
* Redaction (below) applies scheme words first, decodes percent-encoded names, masks the ENTIRE value of an
  `Authorization` / `Proxy-Authorization` field whatever its scheme, masks compound credential names by a suffix rule,
  never registers a blank secret, masks a registered secret shorter than 8 characters only as a whole token,
  registers the environment tokens before the arguments are parsed, installs a redacting `excepthook` in `main()` (an
  exception that escapes it, also when `sandbox_launch.py` embeds the tool), and every output of the tool goes through it.
* Every child process gets an ALLOW-LIST environment, not a deny-list (see "The environment of the child processes").
* A `zenodo.json` or journal write that fails reports the temporary file it left (next to the target, because an
  atomic rename works within one filesystem) and keeps it: this tool deletes nothing.

```bash
# Production. The script reads the PROCESS ENVIRONMENT only: it does NOT load tools/.env itself.
cd /path/to/@cosmochrony
export $(grep -v '^#' tools/.env | xargs)          # standard invocation (CLAUDE.md); exports every variable
python3 tools/zenodo_publish.py <paper_dir>/ --dry-run   # no token, no network: metadata, version-label verdict
python3 tools/zenodo_publish.py <paper_dir>/             # new version (or first publication)
python3 tools/zenodo_publish.py <paper_dir>/ --update --set-code-url            # prints the diff, creates nothing
python3 tools/zenodo_publish.py <paper_dir>/ --update --set-code-url --yes-apply-diff <DIFF_SHA256_PREFIX_12_OR_MORE_HEX>
python3 tools/zenodo_publish.py <paper_dir>/ --finish-after-fail [--journal PATH]   # offline, see "After a post-publish FAIL"
python3 tools/zenodo_publish.py --reconcile <JOURNAL> --outcome published|not-published ...   # offline, see the run journal
```

Run in the foreground, from the version branch (the branch name is the version label), after the
pre-deposit checks and the independent review. `python3 -m unittest discover -s tests` runs the offline
suite (680 tests, no network: a socket guard fails any real connection). `python3 tests/mutation_check.py`
re-applies the in-memory mutants (403 mutants in all, of which 55 new in the seventh pass; `--count` prints these numbers from
the script as `mutants: 403, new: 55`; a full run takes more than an hour: use `--shard K/N` to spread it over processes, or
`--only-new` / `--ids ID1,ID2` for a subset); **its expected result is: every mutant killed except
exactly one survivor, `M16b`, a documented equivalent mutant** (it removes an unreachable defensive assertion; its
reachable counterpart M16 is killed). The script exits 0 only in that case and 1 for any other survivor, skipped
pattern or red baseline. No mutant has been added to the allowed survivors since: a survivor that is equivalent in
outcome but not in message (N6V1, N6V3, N6V6 of the sixth pass) is killed by a test that pins the message, because the
message names the cure. `python3 tests/mutation_check_review.py` re-applies the 43 mutants of the reviewer's round 19
(Y1-Y38, Z1-Z5) onto the current code; its only documented equivalent is `Y1` (see the script).
`python3 tests/mutation_check_review21.py` re-applies the 40 mutants of the reviewer's round 21; it has no
documented equivalent.

### Environment variables

| Variable | Used for |
|---|---|
| `ZENODO_TOKEN` | production host `zenodo.org` only |
| `ZENODO_SANDBOX_TOKEN` | `--sandbox` (`sandbox.zenodo.org`) only; there is **no fallback** to `ZENODO_TOKEN` |
| `ZENODO_CONNECT_TIMEOUT` / `ZENODO_READ_TIMEOUT` / `ZENODO_UPLOAD_TIMEOUT` | seconds, finite and > 0; defaults 10 / 60 / 900; upper bounds 300 / 3600 / 86400 (above: refused at startup) |
| `ZENODO_GET_ATTEMPTS` / `ZENODO_RETRY_BACKOFF` | GET attempts (default 3, **at most 10**, above: refused) and first back-off in seconds (0..60; 1, 2, 4... capped at 30) |
| `ZENODO_VERIFY_ATTEMPTS` | post-publication read-backs (default 5, clamped to 1..6; delays 2, 4, 8, 16 s) |
| `XDG_CACHE_HOME` | only to place the default `--state-dir` (`$XDG_CACHE_HOME` or `~/.cache`, then `cosmochrony-zenodo/`) |

Every numeric setting is validated at startup, **before any request** (NaN, infinity, non-numbers,
non-positive timeouts and values above their upper bound stop the run). `tools/.env` currently defines `ZENODO_TOKEN` (not `ZENODO_SANDBOX_TOKEN`).
The script never reads that file: whoever invokes it exports the variables first (the line above exports all of
them, including the production token, into the shell that runs the command; for a sandbox run use
`pilot/sandbox_launch.py`, which reads `ZENODO_SANDBOX_TOKEN` from the terminal and never puts it on a command
line: a token typed in an argument vector is visible to `ps`).
A missing or empty token stops the run before anything is parsed or sent. A token must be printable ASCII
without whitespace; one containing a newline, a space or a control character is refused **without being shown**.
A token is **masked by value and by field name** (and by shape and by URL), not guaranteed absent: every exception text,
HTTP body and URL that reaches the console, and every value written to a run journal, goes through one function
(`redact`) that masks (a) the exact value of every registered secret: both `ZENODO_TOKEN` and `ZENODO_SANDBOX_TOKEN` as
soon as either is present, whatever their length (an empty value is never registered), (b) any `Bearer ...`,
`Bearer: ...` or `Basic ...` string, (c) the ENTIRE value of any `Authorization` / `Proxy-Authorization` field, whatever
its scheme (`Bearer`, `Basic`, `Digest`, `Negotiate`, `Token`, `ApiKey`, `OAuth`, `GitHub`,
`AWS4-HMAC-SHA256 Credential=..., Signature=...`, any other word, or a bare credential), in the `Name: value`,
`Name=value`, quoted, JSON and repr forms, up to the end of the header value (a line end, a literal `\r`/`\n` in a repr,
the closing quote, or `&` in a query string) without eating the field that follows; an `Authorization` value is also
cut neither by an escaped pair (`Digest username=\"u\", response=\"S\"` inside a JSON string), nor by a FOLDED
continuation line (a *folded* header: a line that begins with a space or a tab continues the value of the line before); a
value that opens an *unclosed* quote (a truncated message) is masked to the end of the line, (d) the value of any field
whose NAME is a credential name by this rule (case-insensitive; the name is split into words on `_`, `-`, `.`, spaces and
camelCase boundaries, so a dotted name is judged by its last word; a name written with percent escapes such as
`access%5Ftoken` or `my.x%2Dapi%2Dkey` is decoded first, once for each level of encoding, three at most: `%252D` too):
it **ends in** `token`, `secret`, `password`, `passwd`, `passphrase`, `credential(s)`, `authorization`, `cookie` or
`signature`, or it **contains** `apikey`, or two consecutive words `access|secret|private` + `key`, or `session` + `id`,
or it is `auth|signing|encryption|ssh|session|master|client|license` + `_key`, or its LAST word (or the whole name) is
`jwt`, `auth`, `oauth`, `pwd` or `csrf`, or the whole name is `session`. Masked, for example: `token`, `access_token`,
`refresh_token`, `auth_token`, `id_token`, `authToken`, `bearer_token`, `x-amz-security-token`, `X-CSRF-Token`,
`X-Auth-Token`, `api_secret`, `client_secret`, `db_password`, `gpg_passphrase`, `session_cookie`, `Set-Cookie`,
`request_signature`, `credentials`, `x-api-key`, `API_KEY`, `api_key_id`, `apiKey`, `private_key`, `ssh_private_key`,
`AWS_SECRET_ACCESS_KEY`, `accessKeyId`, `signing_key`, `jwt`, `auth`, `HTTP_AUTH`, `oauth`, `pwd`, `db_pwd`, `csrf`,
`session`, `session_id`, `sessionId`, `ZENODO_TOKEN=` and `ZENODO_SANDBOX_TOKEN=` assignments (a colon after those two
names is ordinary prose in this tool's messages and is left readable). The separators and the blanks around them may
be any Unicode blank (no-break space, en and em spaces, narrow no-break space, ideographic space, tab, ...): `token`, NBSP,
`=` is as masked as `token=`. (e) A bare token SHAPE, wherever it stands, with no field name: `ghp_`, `gho_`, `ghu_`,
`ghs_`, `ghr_` followed by at least 16 letters or digits; `github_pat_`, `glpat-` and `xoxa-`/`xoxb-`/`xoxp-`/`xoxr-`/
`xoxs-` followed by at least 16 token characters; `AKIA` or `ASIA` followed by exactly 16 capitals or digits;
`sk-` followed by at least 20 token characters of which one is a digit (so a hyphenated word that starts with `sk-`
is not taken for a key); none of them when glued to a letter on the left. (f) The USERINFO of a URL, in any printed
text and journal: `https://user:password@host/p` is printed `https://user:***@host/p`, a token-only
`https://TOKEN@github.com/o/r` is printed `https://***@github.com/o/r`, `x-access-token:TOKEN@` and a percent-encoded
userinfo likewise, also in a text cut right after the `@`; scheme, host and path stay readable; the conventional
`ssh://git@host` is kept. NOT masked, on purpose (ordinary bibliographic or technical text): a bare `key`, `bib_key`,
`sort_key`, `cache_key`, `primary_key`, `public_key`, `zenodo_id`, `doi`, `keywords`, `publication_date`, `token_count`,
`tokens`, `max_tokens`, `author`, `authors`, `authorization_url`, `cookies`, `signature_algorithm`, `password_policy`,
`secretary`, `obsession`, `session_count`, `edit-session`, `x_session`, `auth_url`, `oauth_scope`, and `ghp_short`. The
unquoted value of a field written `Name=value` runs to the end of the line or the next `&`: a field written after it on
the same line, separated by a space, is masked with it (documented over-masking, the price of a value that may hold
spaces; `Authorization=<value>` consumes the rest of its line, and `Authorization: X, Content-Type: y` masks to the end of
the line). A bare `signature`, `credential`, `credentials`, `auth`, `session` or `pwd` is also ordinary prose (the
Lorentzian signature of a metric, in an abstract that `--dry-run` prints; an edit session): written alone it is masked
only where a field name can stand (start of the text or of a line, after a quote, a brace, a comma, `&`, `?`), never
after a word and a blank; the compound names (`x-amz-signature`, `request_signature`, `x_auth`) are masked everywhere. The
three name-based passes, the scheme pass, the URL pass and the shape pass are each complete on its own, so the result does
not depend on their order. Every `print` of the tool, every message of `sys.exit`, every argument-parsing error and every
exception that escapes `main()` passes through the same function (the environment tokens are registered before the
arguments are parsed, and the redacting `excepthook` is installed by `main()` itself, not only under `__main__`, because
`sandbox_launch.py` and `inject_failures.py` import the module and call it; both print the exit code of the tool through
`redact`); the only raw output calls of the module are the three that redact, and a test pins that by AST. The journal is
redacted before the file is written (all its strings, its keys, and the value of every credential-named key).
**Used values are never redacted.** The mask applies to what is PRINTED and to what is WRITTEN INTO A JOURNAL. The URL of
a request, the host check of the client, the metadata sent to Zenodo and the payload of a PUT never pass through it (a
test spies on the HTTP layer and on the redaction function separately and compares the URL byte for byte; mutants send
a redacted request URL, host check, `code_url` of the metadata and of the payload, and are killed). **One used value is
changed, on purpose, as a safety fix:** a `code_url` that carries credentials (`https://user:TOKEN@github.com/...`) is
never sent to Zenodo. A `code_url` WRITTEN in `zenodo.json` with a userinfo is refused (`Error: ... carries credentials`);
a code URL DERIVED from the git remote of a paper that has a `code/` directory is derived from the remote WITHOUT its
userinfo and the tool says so (`Note: the git remote of this paper carries credentials; the code URL is derived without
them`); before, the first was sent as written and the second ended in an error that printed the remote with its password.
**Single choke point.** An exception text leaves the process by `print` / `die` / the argument parser / the journal
serialiser / the excepthook only, and each of them redacts: `redact(exc)` written at a call site is a second line of
defence, which is why mutating that one argument alone (`tests/mutation_check.py`, historical mutant E1) was an
EQUIVALENT mutation; E1 now mutates the choke point (the module `print` on stderr) and is killed. The excepthook is tested
with a registered token through `zenodo_publish.main`, `sandbox_launch.py` and `inject_failures.py`, with a constructed
exception and with a run that really raises. **Limits (kept):** a secret that is neither registered nor attached to one of
those names, shapes or URLs (a token typed elsewhere, for instance in a `--reason`, or a PEM private-key block) is not
recognised; a differently named field whose name has no listed ending or last word (`mytok`, `x_session`, `pass`, `sid`, `PAT`) is not
either; a
credential written in prose after its noun (`Authorization header "Foo X"`, `headers["Authorization"] = "Foo X"`) and
`bearer:X` without a space are not masked; a registered secret **shorter than 8 characters** is masked only as a whole
token (not preceded or followed by a letter, digit or underscore) and never inside a dictionary key, because a blind
substring replacement of a one- or two-character secret would mangle every other text and the keys of a journal: such a
secret glued to other characters is not masked. The output channels that this module does not use are not redacted
either: `threading` excepthook, unraisable exceptions, `atexit` callbacks, `warnings` and `logging`. The output of
`predeposit_check.py` run as its own command is not redacted (it prints paths and log lines of the paper, and never reads
a token). The client refuses to be built with a token equal to the other host's variable, and refuses to send a request
(bucket upload links included) to any host but its own, so the production token cannot reach the sandbox, nor the sandbox
token production.

### The environment of the child processes (an allow-list)

Every child process of the tool receives an explicit ALLOW-LIST environment: a variable that is not named in the list is
not passed, whatever its name (a custom variable, a token under a name nobody thought of, `*_KEY`, `COOKIE`, `AUTH`,
`JWT`, `PASSPHRASE`, `HTTPS_PROXY` / `PIP_INDEX_URL` carrying a password, `SSH_AUTH_SOCK`, `GIT_ASKPASS`,
`GIT_SSH_COMMAND`, `GIT_CONFIG_VALUE_*`, `DOCKER_CONFIG`, `KUBECONFIG`, `DYLD_*`, `LD_*`...). The constant is
`predeposit_check.CHILD_ENV_ALLOW` (the commands of the pre-deposit battery: `grep -a`, `git`, `pdfinfo`, and whatever a
paper's own check runs through `sh()`: `pdflatex` and `bibtex` build a real paper under exactly this environment, a test
runs them) and `zenodo_publish._GIT_ENV_KEEP` (the local `git` calls of the tool). The names, by purpose:

| Purpose | Names passed |
|---|---|
| process | `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `TZ`, `TMPDIR` |
| locale | `LANG`, `LANGUAGE`, `LC_ALL`, `LC_COLLATE`, `LC_CTYPE`, `LC_MESSAGES`, `LC_MONETARY`, `LC_NUMERIC`, `LC_TIME` |
| TeX Live | `TEXINPUTS`, `BIBINPUTS`, `BSTINPUTS`, `TEXFONTS`, `TFMFONTS`, `TEXMFHOME`, `TEXMFVAR`, `TEXMFCONFIG`, `TEXMFCACHE`, `TEXMFCNF`, `TEXMFOUTPUT`, `TEXMFDOTDIR`, `TEXMFLOCAL`, `TEXMFSYSVAR`, `TEXMFSYSCONFIG`, `SOURCE_DATE_EPOCH`, `FORCE_SOURCE_DATE`, `openout_any`, `openin_any`, `shell_escape`, `max_print_line`, `error_line`, `half_error_line` |
| python | `PYTHONPATH`, `PYTHONHOME`, `PYTHONUTF8`, `PYTHONIOENCODING`, `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONPYCACHEPREFIX`, `PYTHONUNBUFFERED`, `PYTHONWARNINGS`, `VIRTUAL_ENV` |
| git (no credential) | `GIT_DIR`, `GIT_WORK_TREE`, `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_EXEC_PATH`, `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_TERMINAL_PROMPT`, `GIT_OPTIONAL_LOCKS` |

Deliberately NOT passed: the proxy variables (a proxy URL may embed a password; the battery needs none for a public
https remote), `SSL_CERT_*` / `CURL_CA_BUNDLE`, `SSH_AUTH_SOCK` (an agent socket is a credential: verify an ssh remote by
hand), `LD_LIBRARY_PATH` / `DYLD_*` / `LD_PRELOAD` (they change which code a child loads), and every `GIT_*` name not in
the table. A prefix is never an allow rule (`TEXMF_SECRET`, `PYTHONSECRET_X`, `LC_SECRET`, `GIT_SECRET_THING` are not
passed); tests prove: the 26 names the review listed and 34 more do not reach a child, a custom variable does not, every
allowed name that is set is passed, the allow-list holds no secret-looking name, the git names of the two lists agree,
and `pdflatex` + `bibtex` still build a small paper. `desync_audit.py` and `repo_metadata_sync.py` are separate scripts
(local `git` / `find` only, never reached from a deposit) and keep their own behaviour.

### The run journal (`--state-dir`)

Before any publication-related write request leaves the process, and after each step, the tool writes the known
state atomically (unique temp file + `fsync` + `os.replace` + `fsync` of the directory; `F_FULLFSYNC` on macOS in
addition) to `<--state-dir>/<paper>-<timestamp>-<pid>-<id>.run.json`
(default `$XDG_CACHE_HOME` or `~/.cache`, then `cosmochrony-zenodo/`; a directory inside the workspace or the
paper directory is refused, so a repository can never commit it). It records the phase, the deposition id, the
concept id, the intent flags, the host, the sha256/md5/size of the PDF actually uploaded and the outcome:

| outcome | meaning | blocks the next run |
|---|---|---|
| `RUNNING` | the run started and did not finish (killed, crashed) | yes |
| `UNRECONCILED` | a publication cannot be excluded (publish of unknown outcome, unreadable record, cleanup aborted) | yes |
| `DRAFT-LEFT-BEHIND` | a draft or edit session may remain: cleanup failed, was refused, or the draft could not be attributed | yes |
| `PUBLISHED-UNSAVED` | **the publication succeeded** (the publish answer, or a read showing `done`) but the local state is not saved and read back: a failed `zenodo.json` write, a response without concept ids, Ctrl-C right after publish returns, a sandbox state file not committed | **yes** |
| `PUBLISHED` | published; `zenodo.json` written **and read back** (sandbox: state file committed; `--update`: nothing to save) | no |
| `NOT-PUBLISHED` | nothing is published and nothing is left behind (refusal before any write, or a clean discard) | no |
| `*-RECONCILED` | set by `--reconcile` | no |
| anything else (absent, mistyped, a future name) | unknown to this version of the tool | **yes (fail closed)**; cure: `--lift-block JOURNAL --reason ...` (a decision file; the journal is not touched) |

The non-blocking outcomes are exactly `PUBLISHED`, `PUBLISHED-RECONCILED`, `NOT-PUBLISHED` and
`NOT-PUBLISHED-RECONCILED`; every other value blocks.

`PUBLISHED-UNSAVED` is set *inside* `publish()` (and again from the answer's content) before control returns to any
code that can be interrupted, and becomes `PUBLISHED` only after `zenodo.json` has been written and re-read; the
state before the answer is `UNRECONCILED`, also blocking, so there is no instant at which a successful publication
leaves a non-blocking journal.

**What blocks.** A blocking journal blocks a run when ANY of these holds: the same paper directory (compared by
`realpath`, so a symlink does not hide it); the same concept id **on the same host**; or, on the same host, the
same `bib_key`, the same LaTeX title, or the same `zenodo.json` content hash (so a copy or another checkout of the
paper is recognised even for a first publication, when no concept id exists yet). An unreadable journal whose file
name starts with the paper directory's name blocks too, and so does a journal with an absent or unknown outcome that
matches by any of those rules or by its file name. The printed message names the journal, says which rule matched and
prints the cure. Every
later run that is blocked **refuses to start**, prints what is known and "DO NOT RE-RUN".

**The cure** is local: no network is used, nothing is verified by the tool against the server, it is your assertion
after inspecting the record and `https://<host>/deposit`. A reconcile is bound to the paper the journal was written
for: (1) every journal the tool writes carries `identity_sha256`, a digest of its identity fields (`paper_dir`,
`paper_dir_real`, `host`, `kind`, `bib_key`, `title`, content hashes); a journal with no digest, or whose fields no
longer match it (edited, hand-written, written by an older version), is refused and nothing is written. The digest is
tamper evidence against an edit, not authentication; (2) a production `published` reconcile writes **only
`<paper_dir_real>/zenodo.json`**, where `paper_dir_real` is the realpath recorded in the journal: it must be present,
must still exist, must equal what `paper_dir` resolves to now (a retargeted symlink is refused) and must be the
directory given on the command line, if any; the journal must carry `bib_key` and a content hash, and the file must
have that `bib_key`, the same content apart from `zenodo_id`/`doi`, and a `zenodo_id` empty or equal to the concept you
assert (if `zenodo.json` was legitimately changed since the run, restore it, reconcile, then redo the change);
(3) every input is validated before anything is written: `--record-id` and `--concept-id` positive integers (digits
only, at most 12), `--concept-doi` exactly `10.5281/zenodo.<concept id>` (ASCII, the number equal to the concept id; an existing different `doi` in `zenodo.json` is refused),
`--record-url` an https URL on the journal's own host that contains the record id, `--reason` at least 10 characters
after stripping; a refusal is a message, never a traceback, and `zenodo.json` is written (temporary file then replace,
then read back) only after all of it:

```bash
# published (also for PUBLISHED-UNSAVED): production deposits need the identifiers to save
python3 tools/zenodo_publish.py --reconcile <JOURNAL> --outcome published --record-id N --concept-id C --concept-doi D
# not published: evidence is mandatory and stored in the journal; the consequences are printed
python3 tools/zenodo_publish.py --reconcile <JOURNAL> --outcome not-published --record-url https://zenodo.org/deposit/N
python3 tools/zenodo_publish.py --reconcile <JOURNAL> --outcome not-published --i-checked-the-account --reason "what you saw"
# a journal no reconcile can cure (unreadable; absent/unknown outcome; identity digest absent or wrong; directory moved):
python3 tools/zenodo_publish.py --backup-journal <JOURNAL>        # copy only: nothing is unblocked, the original is not touched
python3 tools/zenodo_publish.py --lift-block <JOURNAL> --reason "what you checked" --i-checked-the-account
python3 tools/zenodo_publish.py --lift-block <JOURNAL> --reason "what you checked" --record-url https://zenodo.org/deposit/N
# a journal that records a publication: zenodo.json must hold the published zenodo_id and doi first; --paper-dir when the directory moved
python3 tools/zenodo_publish.py --lift-block <JOURNAL> --paper-dir <DIR> --reason "what you checked" --i-checked-the-account [--record-id N --concept-id C --concept-doi D]
```

`--lift-block` never renames, edits or deletes the journal: it makes sure a copy with the same sha256 exists, then writes
`<JOURNAL>.decision.json` by exclusive create (a second lift is refused: move the old decision aside by hand if the
journal was edited after it and blocks again). A decision counts only while the journal has exactly the bytes it
recorded.

**A lift after a publication needs the published identity (sixth pass).** A journal that records a CONFIRMED or LIKELY
publication (`PUBLISHED-UNSAVED`, `UNRECONCILED`, any journal whose last write request was `.../actions/publish`, or one
that already carries a record id or a DOI) is lifted ONLY if the paper's `zenodo.json` already holds the published
`zenodo_id` (the concept record id) and `doi` (exactly `10.5281/zenodo.<concept id>`): the journal's own values when it
records them, otherwise the ones you give with `--concept-id C --concept-doi D` (and `--record-id N`), validated like a
reconcile's and equal to the journal's when both exist. The file checked is the one of the realpath recorded in the
journal, or of `--paper-dir DIR` (also the positional `paper_dir`) when the directory moved; it must have the journal's
`bib_key` and, when the journal is trustworthy, its content hash. Otherwise `--lift-block` REFUSES, before it writes the
backup or the decision, and prints what to restore (set `zenodo_id` and `doi` by hand from the record page, then run
`--lift-block` again; use `--reconcile --outcome published` when the directory is intact). The decision records the
verified ids, the `zenodo.json` path and its sha256 at lift time, and the block check honours it **only while the
`zenodo.json` of the run that asks STILL holds those ids** (an old decision without the verification, ids removed later,
another concept, a copy of the paper without ids: the run stays blocked and says why). After such a lift the next run is
an update or a NEW VERSION of the concept, never a first publication; if the journal said the publication SUCCEEDED, a
re-run publishes a further version, so do that on purpose only. A journal that proves nothing was published (`RUNNING`
before the publish request, `DRAFT-LEFT-BEHIND`, unreadable or unknown outcome) keeps the earlier rules: for a draft
left behind, discard the draft or edit session by hand BEFORE lifting (the tool says so; it does not clean up after a
lift); a SANDBOX journal has no production `zenodo.json` to restore, takes none of the identity options, and a next run
may create a new sandbox deposition. `--reconcile --outcome not-published` is refused for a `PUBLISHED-UNSAVED`
journal (the publication did succeed). A sandbox journal never
writes any `zenodo.json`. If the journal cannot be written before a write request, the request is not sent. The
journal file is created exclusively (written under a unique temp name, then hard-linked: never over an existing
file, never partial). `Ctrl-C` right after `publish` returns, or any case where publication cannot be excluded,
prints the same "DO NOT RE-RUN" instruction and re-raises.

**Corrective pass 7 (review round 23).** (1) Redaction: an Authorization or `*token` value that opens a quote the text never
closes, one that holds escaped pairs, one that continues on the following lines, and a separator written with a Unicode
blank are masked again (the fifth pass masked them, the sixth leaked them); names `jwt`, `auth`, `oauth`, `pwd`,
`csrf`, `session`, `session_id`; bare token shapes; the userinfo of a URL; a percent-encoded name is decoded once per level
of encoding. (2) **Behaviour change of a used value:** a `code_url` with credentials is refused, and one derived from a git
remote with credentials is derived without them (see "Used values are never redacted"). (3) The seven survivors of the
sixth pass are killed by tests; the README survivor sentence is the one the scripts compute. **Limits that remain,
together:** an unreadable journal or one with an unknown outcome and **without a record id** or DOI is lifted on the
operator's word (`--i-checked-the-account` or a record URL) alone, because nothing is recorded to verify; sandbox journals are lifted
without any identity check (a sandbox has no production identity); the digest of a journal and the decision file are
unauthenticated (an unkeyed sha256 that anyone who can write the state directory can recompute: they detect accident, not
an attacker); two simultaneous runs are not serialised by a lock; the output channels listed under the redaction limits;
and everything under "What is still UNPROVEN".

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
  read **once**: the bytes digested are the bytes journaled, uploaded **and archived** (a PDF that cannot be read,
  hence has no recorded digest, is never uploaded). When the upload answer carries a `checksum`, it is compared with
  the md5/sha256 of the bytes sent and a mismatch aborts (an answer without checksum is tolerated: UNPROVEN).
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
  showing `done`, and read back before the journal leaves `PUBLISHED-UNSAVED`. A re-run is then a new version,
  never a second first publication. If that save fails the journal STAYS `PUBLISHED-UNSAVED`, which **blocks every
  re-run** of the paper (also from another path), and the tool prints the `--reconcile` command that writes the
  identifiers into the journal's own paper directory (in `--sandbox`: the result is in the journal and printed,
  and no production `zenodo.json` is to be touched).
* **Timeouts and retries.** Every request has a bounded `(connect, read)` timeout. Only GET requests are
  retried (connection error, timeout, 429/502/503/504); POST, PUT and DELETE are never retried
  automatically, so a timeout on them goes through the uncertainty handling above.

### The version label

Independently of `--skip-checks` and of the pre-deposit gate, a non-dry run refuses an outgoing version that is
empty, is `main`/`master`/`HEAD` (any case), is not a plausible version string (letters, digits and `. _ + -`,
starting with a letter or digit, at most 64 characters, no `..`: the label becomes part of an archive file name), differs from `zenodo.json`'s
`version`, differs from the git branch (or the paper is on `main`/`master`/a detached head), or differs from
`CITATION.cff`'s version. A new version whose label equals the **live** version of the record is refused before
anything is created. The pre-deposit gate (`predeposit_check`) fails closed: if it cannot be imported or raises,
the run refuses; `--skip-checks` bypasses that battery only, never the label guard, the journal block or the diff
confirmation. (A `--dry-run` prints the verdict as `REFUSED, a real run would stop` instead of exiting.)

### `--update`: the live metadata is preserved

`--update` builds the PUT from the **live** deposition's metadata and replaces only the fields you name:
`--set-version`, `--set-title`, `--set-abstract` (description), `--set-keywords`, `--set-notes`, `--set-web-url`
(**only the web-page entry** of the related identifiers is replaced or inserted; `cites` and every other entry are
kept. The live entry with the same identifier and relation is replaced in place; otherwise the candidates are the
entries with the same relation (`isSupplementedBy`), scheme (`url`) and `resource_type` (`other`) that are on
`cosmochrony.org` or equal to the new value; several are
refused as ambiguous; a single candidate **without `resource_type` is never replaced implicitly**: the run is refused
naming it, and you pass `--replace-related-identifier <IDENTIFIER>` if it is the web entry; an entry of another host is
KEPT, the run says so and the web entry is appended. `--replace-related-identifier` needs exactly ONE live entry with
that exact identifier (none or several: refused), of relation `isSupplementedBy` and scheme `url` only (never a `cites` /
`isCitedBy` / `references` / `isReferencedBy`, DOI, `isVersionOf`, `isPartOf`... entry);
the replaced entry is printed and appears in the diff), `--set-publication-type`,
`--set-code-url` (the repository field, other `custom` keys kept),
or `--apply-local-metadata=version,title,...`. With none of them the run is refused. **Before anything is created**
the tool prints the whole-metadata unified diff `live -> to-be-sent` and its sha256, and stops unless
`--yes-apply-diff <sha256-prefix>` is given: the value must be at least 12 hex characters of the printed
`diff sha256:`, computed from exactly the printed diff text (UTF-8, no trailing newline), so the confirmation is a
statement about *that* diff; a bare flag or a shorter value is refused (review in one run, apply in a second). After
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

It is **bound to one deposit journal**: the newest production journal of the paper that is a deposit (not an
`--update`), published, and carries a PDF digest (`--journal PATH` names another; sandbox journals, `--update`
journals and runs that published nothing never shadow it, a journal still `PUBLISHED-UNSAVED` is refused). The
archive **label is that journal's version** (never the current configuration), the version-label guard is applied to
it, so the paper must be checked out at the deposited version; a journal whose concept differs from
`zenodo.json`'s `zenodo_id` is refused. It **refuses unless the newest PDF of the paper has the sha256 recorded in
that journal** (or the digest you pass with `--expect-pdf-sha256` when no journal survives), and, when the publish
response recorded one, the md5 Zenodo returned. It never overwrites a file of `pdfs/`: an identical archive is
reported as "already archived, identical", a different one is refused with both digests; a symlink or directory at
the destination is refused with a message. It needs no token and sends nothing.

**Archive and cascade (normal run and `--finish-after-fail`).** The DOI cascade runs only if the archive step
returned `archived` or `identical`; a refused or failed archive prints a message (never a traceback) and the
cascade is NOT run. The copy is written under a unique temp name next to the destination, fsynced, verified by
sha256, then hard-linked to the final name (`os.link` fails if the name exists): a failed write never leaves a
truncated file under the final name, the directory is fsynced, the linked copy is verified again. A leftover temp
file from an earlier failure is **reported (path, size) and never deleted by the tool**.

### Sandbox (`--sandbox`)

```bash
python3 tools/pilot/sandbox_launch.py -- <paper_dir>/ \
    --sandbox --sandbox-state "$SCRATCH/result-1.json" --state-dir "$SCRATCH/journals" \
    [--sandbox-zenodo-id N] [--update --set-code-url --yes-apply-diff <DIGEST_PREFIX>]
```

* reads only `ZENODO_SANDBOX_TOKEN` (missing or empty: refuses to start, nothing sent); `sandbox_launch.py` asks for
  it on the terminal (no echo) and keeps it in its own process: it is never in an argument vector (nothing for `ps`
  to show), and the launcher refuses if `ZENODO_TOKEN` is present. It is in the environment of that process; the
  four `git` calls of `zenodo_publish.py` run with an allow-list environment (`PATH`, `HOME`, locale, `TMPDIR` and the
  credential-free `GIT_*` names) that does not carry it; the commands of the pre-deposit battery (`predeposit_check.py`,
  run in-process unless `--skip-checks`) are child processes too and run with the allow-list environment of "The
  environment of the child processes", so none of the children of this tool sees the token;
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
* **Server semantics of DELETE vs `actions/discard`**, **custom acceptance for `workingpaper`**, **`latest_draft` on
  clean published records**, **`X-Total`/`Link` headers**, **`status=draft` including edit sessions**, **inheritance of
  `code:codeRepository`**, **checksums in the publish response** and **in the upload answer** (the tool compares one
  when present and tolerates its absence), **redirect targets** and **concurrent drafts by another actor**: all
  listed above or here, none observed.
* **The whole-live-metadata PUT.** Whether the deposit API accepts a PUT whose body is the entire live metadata of a
  published record (read-only keys such as `doi`, `prereserve_doi`); fail closed if rejected, untested on a server.
* **Journal durability.** `fsync` of the file and of the directory, plus `F_FULLFSYNC` on macOS, is best effort; a
  filesystem that cannot fsync a directory (`EINVAL`/`ENOTSUP`) is tolerated. Two simultaneous runs of the same paper
  are not serialised by a lock (both could pass the block check before either journal exists).
* **Not covered by design.** `Retry-After` is ignored; the version, title and related identifiers are not read back
  from the draft before publishing on the new-version path.

## Credentials

`zenodo_publish.py` reads `ZENODO_TOKEN` (production) or `ZENODO_SANDBOX_TOKEN` (`--sandbox`) from its
environment; pushes and repository creation use `GH_TOKEN`; website deploys use `NETLIFY_PAT` as
`NETLIFY_AUTH_TOKEN`. These live in a local `tools/.env`, which is **git-ignored and must never be
committed** (this repository is public) and which no script of this directory loads by itself: the caller
exports what a command needs.

## Licence

CC BY 4.0, as for the rest of the programme.
