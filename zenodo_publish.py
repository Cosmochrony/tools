#!/usr/bin/env python3
"""zenodo_publish.py — Automated Zenodo publication for Cosmochrony papers.

Usage (production):
    export ZENODO_TOKEN=...            # the script reads the PROCESS ENVIRONMENT only; it does NOT load
                                       # tools/.env itself (export it first, e.g. `set -a; . tools/.env; set +a`)
    python tools/zenodo_publish.py bell-paper/ [--no-cascade] [--dry-run]            # new version / first publication
    python tools/zenodo_publish.py bell-paper/ --update --set-code-url [--yes-apply-diff [DIGEST]]
    python tools/zenodo_publish.py bell-paper/ --finish-after-fail                   # offline
    python tools/zenodo_publish.py bell-paper/ --reconcile JOURNAL --outcome published|not-published   # offline

Sandbox (never touches a production id, DOI, pdfs/ or bibliography; see tools/README.md):
    ZENODO_SANDBOX_TOKEN=... python tools/zenodo_publish.py PAPER/ --sandbox --sandbox-state /tmp/new-state.json
    (--sandbox reads ONLY ZENODO_SANDBOX_TOKEN, with no fallback to ZENODO_TOKEN; the production token is
     never sent to the sandbox host, nor the sandbox token to production. The state file must not exist.)

Guards (all offline-tested; the server semantics they rely on are listed as UNPROVEN in tools/README.md):
  * Run journal: before any write request leaves, and after each step, the known state is written atomically
    to <--state-dir>/<paper>-<timestamp>.run.json (default: the user cache directory, never the workspace).
    While a journal says RUNNING / UNRECONCILED / DRAFT-LEFT-BEHIND, every later run for that paper or concept
    REFUSES to start ("DO NOT RE-RUN"); --reconcile records, locally and on the operator's word, what was found.
  * A run REFUSES when a draft/edit session of the concept already exists, and on any failure cleans up ONLY the
    server state this run certainly created (an uncertain outcome is reported and searched for, never assumed
    away; ambiguity discards nothing). The cleanup call comes from an explicit kind table (CLEANUP_CALLS):
    edit session -> POST actions/discard, unpublished draft -> DELETE; both UNVERIFIED until the sandbox pilot.
  * --update PRESERVES the live metadata: the PUT is the live record with only the fields named by
    --set-version/--set-title/--set-abstract/--set-keywords/--set-notes/--set-web-url/--set-publication-type/
    --set-code-url (or --apply-local-metadata=a,b) replaced; the whole-metadata diff is printed first and
    needs --yes-apply-diff (optionally pinning the diff's sha256 prefix).
  * The version label is guarded independently of the pre-deposit gate and of --skip-checks: never
    main/master/HEAD, equal to the branch, zenodo.json and CITATION.cff, different from the live version.
    The pre-deposit gate itself fails closed when it cannot run.
  * Every request has bounded timeouts, only GETs are retried; the Software field (code:codeRepository) is
    verified on the draft before publishing and on the public record afterwards; zenodo.json is saved the
    moment a publication is confirmed; the sha256 of the PDF actually uploaded is recorded in the journal.
  * The PDF archive never overwrites a file (identical -> "already archived"; different -> refused with both
    digests) and only archives the PDF that was uploaded. After a post-publish FAIL, --finish-after-fail runs
    the skipped archive and DOI cascade offline under the same rules.
  * No token ever appears in a message; a token with whitespace or control characters is refused unseen;
    every numeric environment setting is validated before any request.

Config file required at paper_dir/zenodo.json:
    {
        "bib_key":   "Beau2026b",                            <- BibTeX key in cosmochrony-bibliography.bib
        "web_url":   "https://cosmochrony.org/science/bell/", <- associated web page
        "main_tex":  "tex/Bell.tex",                          <- optional, auto-detected otherwise
        "code_url":  "https://github.com/Cosmochrony/bell-paper/tree/main/code",
                                                              <- optional; only needed when the paper has a
                                                                 code/ directory and its git remote is not the
                                                                 GitHub repo hosting it; sent as the record's
                                                                 Software "Repository URL" (code:codeRepository)
        "zenodo_id": null,                                    <- filled automatically after first pub
        "doi":       null                                     <- filled automatically
    }

Note: ORCID works are created automatically by Zenodo when your ORCID account is linked
at zenodo.org → Settings → Connected accounts.

Requirements: pip install requests
"""

from __future__ import annotations

import argparse
import collections.abc
import copy
import difflib
import errno
import hashlib
import json
import math
import os
import re
import datetime
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

# The Cosmochrony repository root (this script lives in <root>/tools/). Used as the
# stable anchor for the central pdfs/ archive and the DOI cascade, so both work
# regardless of how deeply a paper directory is nested under the root.
REPO_ROOT = Path(__file__).resolve().parent.parent

# requests is imported lazily in ZenodoClient so --dry-run works without it
requests = None

# ── Author / publisher constants ───────────────────────────────────────────────

CREATOR = {
    "name": "Beau, Jérôme",
    "orcid": "0009-0001-7697-7868",
    "affiliation": "Independent Researcher",
}
LICENSE_ID = "cc-by-4.0"
LANGUAGE = "eng"
UPLOAD_TYPE = "publication"
PUBLICATION_TYPE = "preprint"

ZENODO_URL = "https://zenodo.org/api"
SANDBOX_URL = "https://sandbox.zenodo.org/api"
CODE_REPOSITORY_FIELD = "code:codeRepository"  # InvenioRDM CodeMeta custom field


# ── LaTeX parsing ──────────────────────────────────────────────────────────────

def extract_braced_at(text: str, pos: int) -> tuple[str, int]:
    """Return (content, end) of '{...}' starting search at pos, skipping whitespace."""
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    if pos >= len(text) or text[pos] != "{":
        raise ValueError(f"Expected '{{' at pos {pos}, got {text[pos:pos+10]!r}")
    depth = 0
    i = pos
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i + 1
        i += 1
    raise ValueError("Unmatched brace")


def protect_math(text: str) -> tuple[str, list[str]]:
    """Replace math environments with placeholders to preserve them during cleaning."""
    stash: list[str] = []

    def _save(m):
        idx = len(stash)
        stash.append(m.group(0))
        return f"\x00M{idx}\x00"

    text = re.sub(r"\\\[.*?\\\]", _save, text, flags=re.DOTALL)
    text = re.sub(r"\$\$.*?\$\$", _save, text, flags=re.DOTALL)
    text = re.sub(r"\\\(.*?\\\)", _save, text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?!\$)[^$]+\$", _save, text)
    return text, stash


def restore_math(text: str, stash: list[str]) -> str:
    for i, ph in enumerate(stash):
        text = text.replace(f"\x00M{i}\x00", ph)
    return text


def _replace_cmd_html(text: str, cmd: str, tag: str) -> str:
    """Replace \\cmd{...} with <tag>...</tag>, handling nested braces."""
    result: list[str] = []
    pattern = re.compile(r"\\" + re.escape(cmd) + r"\b\s*")
    pos = 0
    for m in pattern.finditer(text):
        result.append(text[pos : m.start()])
        try:
            content, end = extract_braced_at(text, m.end())
            result.append(f"<{tag}>{content}</{tag}>")
            pos = end
        except ValueError:
            result.append(m.group())
            pos = m.end()
    result.append(text[pos:])
    return "".join(result)


def _strip_texorpdfstring(text: str) -> str:
    """\\texorpdfstring{latex}{plain} -> plain.

    Uses extract_braced_at (depth-counting) rather than a `[^}]*` regex: the
    latex argument routinely contains its own brace pairs once expand_macros
    has run, since that function wraps every substitution in `{}` to preserve
    TeX token boundaries (e.g. \\bstar -> {\\beta^*}). A non-recursive regex
    stops at that inner `}` and leaves \\texorpdfstring unstripped, corrupting
    downstream titles/abstracts (e.g. "delta-beta*delta-beta* Tension").
    """
    marker = r"\texorpdfstring"
    result = []
    pos = 0
    while True:
        idx = text.find(marker, pos)
        if idx == -1:
            result.append(text[pos:])
            break
        result.append(text[pos:idx])
        try:
            _latex_arg, after_first = extract_braced_at(text, idx + len(marker))
            plain_arg, after_second = extract_braced_at(text, after_first)
            result.append(plain_arg)
            pos = after_second
        except ValueError:
            result.append(marker)
            pos = idx + len(marker)
    return "".join(result)


# LaTeX accent commands -> Unicode. Applied BEFORE the generic command strippers
# (which would drop the backslash and leave "Schr\"odinger") and before the
# ~ -> space substitution (which would break \~n).
_ACCENT_COMBINING = {
    '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030C", "H": "\u030B",
    "r": "\u030A", "c": "\u0327", "k": "\u0328", "d": "\u0323", "b": "\u0331",
}
_LIGATURES = {
    "ss": "\u00df", "ae": "\u00e6", "AE": "\u00c6", "oe": "\u0153", "OE": "\u0152",
    "aa": "\u00e5", "AA": "\u00c5", "o": "\u00f8", "O": "\u00d8",
    "l": "\u0142", "L": "\u0141", "i": "i", "j": "j",
}


def _apply_accents(text: str) -> str:
    """Convert LaTeX accent/ligature commands to composed Unicode characters."""
    import unicodedata

    # Symbol accents: \"o, \"{o}, \'e, \`e, \^e, \~n, \=o, \.z
    def _sym(m):
        return m.group(2) + _ACCENT_COMBINING[m.group(1)]

    text = re.sub(r"""\\(["'`^~=.])\s*\{([a-zA-Z])\}""", _sym, text)
    text = re.sub(r"""\\(["'`^~=.])\s*([a-zA-Z])""", _sym, text)
    # Letter accents: braces required, so \correlation etc. are never touched
    text = re.sub(r"\\([uvHrckdb])\s*\{([a-zA-Z])\}", _sym, text)
    # Ligatures and stroked letters: \ss, \o{}, \aa, ... (optional empty braces)
    # LaTeX swallows whitespace after a control word (stra\ss e -> strasse with
    # sharp s, no space), but terminating braces end the command and a space
    # after them is real (\ss{} e -> "ss e").
    text = re.sub(
        r"\\(ss|ae|AE|oe|OE|aa|AA|[oOlLij])(?![a-zA-Z])(\{\})?[ \t]*",
        lambda m: _LIGATURES[m.group(1)] + (" " if m.group(2) and m.group(0).endswith((" ", "\t")) else ""),
        text,
    )
    return unicodedata.normalize("NFC", text)


def _apply_latex_subs(text: str, html: bool) -> str:
    """Apply common LaTeX → text/HTML substitutions (math already protected)."""
    # Accents first: the strippers below remove backslashes, and the ~ rule
    # below would otherwise consume the tilde of \~n.
    text = _apply_accents(text)
    # LaTeX double-quote ligatures (``text'') -> plain straight double quotes.
    # This project's .tex convention is ``text'' (renders as curly quotes under
    # pdflatex), but Zenodo's record fields are not LaTeX-typeset: left as-is,
    # the literal backtick/apostrophe pairs show up verbatim on the record page.
    text = text.replace("``", '"').replace("''", '"')
    # Non-breaking spaces
    text = text.replace("~", " ").replace(" ", " ")
    # Remove \cite variants
    text = re.sub(r"\\cite[tp]?\*?\{[^}]*\}", "", text)
    # Emphasis
    if html:
        text = _replace_cmd_html(text, "emph", "em")
        text = _replace_cmd_html(text, "textit", "em")
        text = _replace_cmd_html(text, "textbf", "strong")
    else:
        text = re.sub(r"\\emph\{([^}]*)\}", r"\1", text)
        text = re.sub(r"\\textit\{([^}]*)\}", r"\1", text)
        text = re.sub(r"\\textbf\{([^}]*)\}", r"\1", text)
    # \texorpdfstring{latex}{plain} → plain
    text = _strip_texorpdfstring(text)
    # \footnote{...} → remove
    text = re.sub(r"\\footnote\{[^}]*\}", "", text)
    # Title subtitle: \\[skip]\normalsize marks start of subtitle — drop subtitle for plain text
    if not html:
        text = re.sub(r"\\\\(?:\[[^\]]*\])?\s*\\normalsize\b.*", "", text, flags=re.DOTALL)
    # \\ (LaTeX line break in titles), optionally followed by [skip] → space or <br>
    text = re.sub(r"\\\\(?:\[[^\]]*\])?", "<br>" if html else " ", text)
    # Dashes
    text = text.replace("---", "—").replace("--", "–")
    # Spacing commands
    text = re.sub(r"\\(?:quad|qquad|,|;|:)\b", " ", text)
    text = re.sub(r"\\(?:noindent|par|medskip|bigskip|smallskip|clearpage)\b\s*", "", text)
    # \vspace{<dimen>}, \hspace{<dimen>} (and starred forms) → remove entirely,
    # including the dimension argument: unlike \textbf{...} etc., the argument
    # here is a length, not content, so the generic \cmd{arg}->arg fallback
    # below would otherwise leak literal dimensions (e.g. "0.5em") into
    # extracted text.
    text = re.sub(r"\\[hv]space\*?\{[^}]*\}", "", text)
    # \text{...} family
    text = re.sub(r"\\text(?:rm|bf|it|sf|tt|sc|up|sl|md|normal|wider|bold)?\{([^}]*)\}", r"\1", text)
    # \textbf, \mathbf, etc. remaining
    text = re.sub(r"\\(?:math|text)[a-z]+\{([^}]*)\}", r"\1", text)
    # Generic \cmd{arg} → arg (safe fallback)
    text = re.sub(r"\\[a-zA-Z]+\*?\{([^}]*)\}", r"\1", text)
    # Bare \cmd → remove
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    # Remove stray braces
    text = text.replace("{", "").replace("}", "")
    return text


def clean_for_title(text: str, macros: dict | None = None) -> str:
    """Strip all LaTeX including math content — for plain-text metadata fields like Zenodo title."""
    if macros:
        text = expand_macros(text, macros)
    text = re.sub(r"%[^\n]*", "", text)
    # Truncate at subtitle separator: \\[...] or \\ followed by a font-size command
    _size_cmds = r"large|Large|LARGE|small|normalsize|footnotesize|scriptsize|tiny|Huge|huge"
    text = re.sub(
        r"\\\\(\[.*?\])?\s*\{?\\(" + _size_cmds + r")\b.*", "", text, flags=re.DOTALL
    )
    # Fractions before generic command stripping: \tfrac/\frac/\dfrac{a}{b} -> a/b (vulgar-fraction Unicode)
    _vulgar = {("1", "2"): "½", ("1", "3"): "⅓", ("2", "3"): "⅔",
               ("1", "4"): "¼", ("3", "4"): "¾", ("1", "8"): "⅛"}
    text = re.sub(
        r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
        lambda m: _vulgar.get((m.group(1).strip(), m.group(2).strip()),
                              f"{m.group(1).strip()}/{m.group(2).strip()}"),
        text,
    )
    # Greek letters and common relation/operator symbols -> Unicode (before bare-command removal)
    _sym = {
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
        "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι",
        "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
        "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
        "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
        "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ",
        "Omega": "Ω", "simeq": "≃", "cong": "≅", "approx": "≈", "equiv": "≡", "neq": "≠",
        "leq": "≤", "geq": "≥", "ll": "≪", "gg": "≫", "times": "×", "otimes": "⊗",
        "oplus": "⊕", "in": "∈", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
        "to": "→", "rightarrow": "→", "Rightarrow": "⇒", "mapsto": "↦", "circ": "∘",
        "cdot": "·", "infty": "∞", "partial": "∂", "nabla": "∇", "pm": "±",
    }
    for _k, _v in sorted(_sym.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"\\" + _k + r"(?![a-zA-Z])", _v, text)
    text = _apply_latex_subs(text, html=False)
    # Superscripts -> Unicode (after command stripping leaves e.g. Sym^2, C^3)
    _sup = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
    text = re.sub(r"\^\{([0-9+\-=()ni]+)\}", lambda m: m.group(1).translate(_sup), text)
    text = re.sub(r"\^([0-9ni])", lambda m: m.group(1).translate(_sup), text)
    # Subscripts: drop the underscore marker, keep the content (V_{ρ} -> Vρ, Heis_3 -> Heis3)
    text = re.sub(r"_\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"_([0-9A-Za-zα-ωΑ-Ω])", r"\1", text)
    # Strip remaining math delimiters and stray braces/backslashes
    text = re.sub(r"[${}\\]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_for_html(text: str) -> str:
    text, stash = protect_math(text)
    text = re.sub(r"%[^\n]*", "", text)
    text = _apply_latex_subs(text, html=True)
    text = restore_math(text, stash)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)  # drop space before punctuation left by stripped \cite
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_for_text(text: str) -> str:
    text, stash = protect_math(text)
    text = re.sub(r"%[^\n]*", "", text)
    text = _apply_latex_subs(text, html=False)
    text = restore_math(text, stash)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def resolve_inputs(tex: str, base_dir: Path) -> str:
    """Recursively resolve \\input{} includes."""
    def _replace(m):
        name = m.group(1).strip()
        if not name.endswith(".tex"):
            name += ".tex"
        p = base_dir / name
        if p.exists():
            return resolve_inputs(p.read_text(encoding="utf-8"), p.parent)
        return ""
    return re.sub(r"\\input\{([^}]+)\}", _replace, tex)


def extract_macros(tex: str) -> dict[str, str]:
    """Extract 0-argument macro definitions from the LaTeX preamble."""
    macros: dict[str, str] = {}
    preamble = tex.split(r"\begin{document}")[0] if r"\begin{document}" in tex else tex
    # \newcommand{\name}{def}, \renewcommand, and \providecommand — skip if followed by [n] (has arguments)
    for m in re.finditer(r"\\(?:new|renew|provide)command\s*\{\\([a-zA-Z]+)\}\s*(?!\[)", preamble):
        try:
            definition, _ = extract_braced_at(preamble, m.end())
            macros[m.group(1)] = definition
        except ValueError:
            pass
    # \DeclareMathOperator[*]{\name}{text} → \operatorname{text}
    for m in re.finditer(r"\\DeclareMathOperator\*?\s*\{\\([a-zA-Z]+)\}\s*", preamble):
        try:
            text_val, _ = extract_braced_at(preamble, m.end())
            macros[m.group(1)] = r"\operatorname{" + text_val + "}"
        except ValueError:
            pass
    # \def\name{def}
    for m in re.finditer(r"\\def\\([a-zA-Z]+)\s*", preamble):
        try:
            definition, _ = extract_braced_at(preamble, m.end())
            macros[m.group(1)] = definition
        except ValueError:
            pass
    return macros


def expand_macros(text: str, macros: dict[str, str]) -> str:
    """Inline 0-argument macro definitions: \\name → {definition}.

    Wraps the expansion in braces to preserve the tokenization boundary. Without
    braces, source like `\\log\\rpair(n)` becomes `\\logr_{...}(n)` after expansion
    — `\\log` and the expansion's leading letter `r` merge into the undefined
    control sequence `\\logr`, which MathJax (and TeX) refuse to parse.

    Expansion is iterated to a fixpoint so macros whose definitions reference other
    macros (e.g. `\\Podd → \\Pi_{\\JPi\\text{-odd}}`) are fully resolved; otherwise a
    nested `\\JPi` would survive into the metadata. A pass-count guard avoids looping
    on cyclic definitions.
    """
    for _ in range(16):
        changed = False
        for name, definition in macros.items():
            new_text = re.sub(
                r"\\" + re.escape(name) + r"(?![a-zA-Z])",
                lambda _, d=definition: "{" + d + "}",
                text,
            )
            if new_text != text:
                text, changed = new_text, True
        if not changed:
            break
    return text


def find_main_tex(paper_dir: Path, config: dict) -> Path:
    if "main_tex" in config:
        p = paper_dir / config["main_tex"]
        if p.exists():
            return p
        raise FileNotFoundError(f"main_tex not found: {p}")
    tex_dir = paper_dir / "tex"
    if not tex_dir.exists():
        raise FileNotFoundError(f"No tex/ directory in {paper_dir}")
    candidates = list(tex_dir.glob("*.tex"))
    for c in candidates:
        if r"\begin{document}" in c.read_text(encoding="utf-8", errors="ignore"):
            return c
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"No .tex file in {tex_dir}")


def extract_title(tex: str, macros: dict | None = None) -> str:
    # MDPI: \Title{...}
    m = re.search(r"\\Title\b\s*", tex)
    if m:
        content, _ = extract_braced_at(tex, m.end())
        return clean_for_title(content, macros)
    # sn-jnl / standard: \title[short]{long}
    m = re.search(r"\\title\b\s*(?:\[[^\]]*\])?\s*", tex)
    if m:
        content, _ = extract_braced_at(tex, m.end())
        return clean_for_title(content, macros)
    raise ValueError("No \\title or \\Title found in LaTeX")


def extract_abstract(tex: str) -> str:
    # \abstract{...} — used by both MDPI and sn-jnl
    m = re.search(r"\\abstract\b\s*", tex)
    if m:
        content, _ = extract_braced_at(tex, m.end())
        return clean_for_html(content)
    # Fallback: \begin{abstract}...\end{abstract}
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.DOTALL)
    if m:
        return clean_for_html(m.group(1))
    raise ValueError("No abstract found in LaTeX")


def extract_keywords(tex: str) -> list[str]:
    # \keyword{...} (MDPI, semicolons) or \keywords{...} (sn-jnl, commas)
    m = re.search(r"\\keywords?\b\s*", tex)
    if m:
        content, _ = extract_braced_at(tex, m.end())
        content = clean_for_text(content)
        sep = ";" if ";" in content else ","
        return [k.strip() for k in content.split(sep) if k.strip()]
    # Fallback: \begin{keywords}...\end{keywords}
    m = re.search(r"\\begin\{keywords?\}(.*?)\\end\{keywords?\}", tex, re.DOTALL)
    if m:
        content = clean_for_text(m.group(1))
        return [k.strip() for k in re.split(r"[;,]", content) if k.strip()]
    # Fallback: \noindent\textbf{Keywords:} or \textbf{Keywords.} kw1, kw2, ...
    m = re.search(r"\\textbf\{Keywords?[.:]?\}\s*(.*?)(?=\n\n|\n\\|\Z)", tex, re.DOTALL | re.IGNORECASE)
    if m:
        content = clean_for_text(m.group(1)).rstrip(".")
        sep = ";" if ";" in content else ","
        return [k.strip().rstrip(".") for k in content.split(sep) if k.strip()]
    # Fallback: \paragraph{Keywords.} or \paragraph{Keywords:} kw1; kw2; ...
    m = re.search(r"\\paragraph\{Keywords?[.:]?\}\s*(.*?)(?=\n\n|\n\\(?:paragraph|section|subsection|clearpage|newpage)|\Z)", tex, re.DOTALL | re.IGNORECASE)
    if m:
        content = clean_for_text(m.group(1))
        content = content.rstrip(".")
        sep = ";" if ";" in content else ","
        return [k.strip().rstrip(".") for k in content.split(sep) if k.strip()]
    return []


def find_pdf(paper_dir: Path) -> Path:
    for subdir in ("out", "pdf", "tex", "."):
        pdfs = list((paper_dir / subdir).glob("*.pdf"))
        if pdfs:
            return max(pdfs, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError(f"No PDF found under {paper_dir}")


# ── Zenodo API client ──────────────────────────────────────────────────────────

PRODUCTION_HOST = "zenodo.org"
SANDBOX_HOST = "sandbox.zenodo.org"
PRODUCTION_TOKEN_VAR = "ZENODO_TOKEN"
SANDBOX_TOKEN_VAR = "ZENODO_SANDBOX_TOKEN"

# Network bounds (seconds). Overridable by environment variables, read when a client is built.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_UPLOAD_READ_TIMEOUT = 900.0
DEFAULT_GET_ATTEMPTS = 3          # GET only (idempotent); POST/PUT/DELETE are never retried
MAX_GET_ATTEMPTS = 10             # upper bound validated at startup (F13)
DEFAULT_RETRY_BACKOFF = 1.0       # 1, 2, 4 ... seconds, capped at 30
MAX_LIST_PAGES = 50               # pagination cap; hitting it without an empty page fails closed
LIST_PAGE_SIZE = 100

# Post-publication verification: bounded retries (attempts, delay before attempt k+1 = VERIFY_DELAYS[k]).
DEFAULT_VERIFY_ATTEMPTS = 5
VERIFY_DELAYS = (2.0, 4.0, 8.0, 16.0)   # total wait <= 30 s


def _sleep(seconds: float) -> None:
    """Single sleeping hook (tests replace it)."""
    time.sleep(seconds)


def _env_number(name: str, default: float, environ=None) -> float:
    """A finite number from the environment, or ``default`` when unset/empty; anything else exits."""
    raw = (environ if environ is not None else os.environ).get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError:
        sys.exit(f"Error: environment variable {name} must be a number, got {raw!r}")
    if not math.isfinite(value):
        sys.exit(f"Error: environment variable {name} must be a finite number, got {raw!r}")
    return value


# (name, default, minimum accepted, exclusive minimum, maximum accepted or None). A value below 1 for an
# attempt count is clamped to 1 later; ZENODO_VERIFY_ATTEMPTS above its cap is clamped (verify_attempts),
# every other upper bound REFUSES at startup (F13).
NUMERIC_ENV = (
    ("ZENODO_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT, 0.0, True, 300.0),
    ("ZENODO_READ_TIMEOUT", DEFAULT_READ_TIMEOUT, 0.0, True, 3600.0),
    ("ZENODO_UPLOAD_TIMEOUT", DEFAULT_UPLOAD_READ_TIMEOUT, 0.0, True, 86400.0),
    ("ZENODO_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF, 0.0, False, 60.0),
    ("ZENODO_GET_ATTEMPTS", DEFAULT_GET_ATTEMPTS, None, False, float(MAX_GET_ATTEMPTS)),
    ("ZENODO_VERIFY_ATTEMPTS", DEFAULT_VERIFY_ATTEMPTS, None, False, None),
)


def validate_numeric_env(environ=None) -> None:
    """Parse EVERY tunable numeric setting at startup, before any request (R11, F13). Exits on a bad value."""
    for name, default, minimum, exclusive, maximum in NUMERIC_ENV:
        value = _env_number(name, default, environ)
        if minimum is not None and (value <= minimum if exclusive else value < minimum):
            sys.exit(f"Error: environment variable {name} must be "
                     f"{'>' if exclusive else '>='} {minimum:g}, got {value:g}")
        if maximum is not None and value > maximum:
            sys.exit(f"Error: environment variable {name} must be <= {maximum:g}, got {value:g}")


# ── Secrets never reach a message (R10) ────────────────────────────────────────

_SECRETS: set = set()
TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")      # printable ASCII, no whitespace, no control character


class InvalidToken(Exception):
    """The token cannot be used as a header value. The message never contains the token."""


def register_secret(value) -> None:
    if isinstance(value, str) and value:
        _SECRETS.add(value)


_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_TOKEN_QUERY = re.compile(r"(?i)\b(access_token|token|api_key|apikey)=[^&\s\"'<>]+")
_AUTH_HEADER = re.compile(r"(?i)\bauthorization(['\"]?\s*[:=]\s*['\"]?)(?!bearer\b)\S+")


def redact(text) -> str:
    """str(text) with every registered secret, any ``Bearer ...`` string, any ``access_token=...`` query
    parameter and any ``Authorization: <value>`` header replaced. This is the ONLY redaction function:
    messages, HTTP bodies, exception texts and every value persisted in a run journal go through it."""
    out = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        out = out.replace(secret, "[REDACTED]")
    out = _BEARER.sub("Bearer [REDACTED]", out)
    out = _TOKEN_QUERY.sub(lambda m: m.group(1) + "=[REDACTED]", out)
    return _AUTH_HEADER.sub(lambda m: "Authorization" + m.group(1) + "[REDACTED]", out)


def redact_obj(obj):
    """``redact`` applied to every string (keys and values) of a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def register_environment_secrets(environ=None) -> None:
    """Register the exact tokens loaded in this process (both variables, whatever the mode), so that a token of
    the other environment is also masked if it ever appears in a message or a journal."""
    environ = os.environ if environ is None else environ
    for name in (PRODUCTION_TOKEN_VAR, SANDBOX_TOKEN_VAR):
        raw = environ.get(name)
        if isinstance(raw, str) and len(raw.strip()) >= 8:
            register_secret(raw)
            register_secret(raw.strip())


def die(message) -> "NoReturn":  # noqa: F821
    """sys.exit with a redacted message."""
    sys.exit(redact(message))


def _redacting_excepthook(etype, value, tb):
    import traceback
    print(redact("".join(traceback.format_exception(etype, value, tb))), file=sys.stderr)


# ── File digests ───────────────────────────────────────────────────────────────

def bytes_digests(data: bytes, name: str) -> dict:
    """sha256, md5 (Zenodo's file checksum) and size of ``data``."""
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(), "size": len(data)}


def file_digests(path) -> dict:
    """sha256, md5 and size of the bytes currently in ``path`` (read once)."""
    return bytes_digests(Path(path).read_bytes(), Path(path).name)


def read_pdf_once(path) -> tuple:
    """(bytes, digests): the PDF is read ONCE; the digest, the upload and the archive all use these bytes."""
    data = Path(path).read_bytes()
    return data, bytes_digests(data, Path(path).name)


class ZenodoAPIError(Exception):
    """An HTTP error answer (4xx/5xx). ``response`` carries the raw answer."""

    def __init__(self, response):
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


class MalformedResponse(Exception):
    """A 2xx answer that is not the expected JSON (or lacks a required field)."""

    def __init__(self, message, response=None):
        self.response = response
        super().__init__(message)


class CompletenessError(Exception):
    """A listing could not be proven complete (fail closed)."""


class TokenHostMismatch(Exception):
    """A token would be sent to the wrong Zenodo host (or to a foreign host)."""


def is_definitive_refusal(exc: BaseException) -> bool:
    """True when the server demonstrably refused the request (4xx other than 408): no effect."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code != 408


def resolve_token(sandbox: bool, environ=None) -> str:
    """The token for the requested host. --sandbox reads ONLY ZENODO_SANDBOX_TOKEN, never ZENODO_TOKEN."""
    environ = os.environ if environ is None else environ
    name = SANDBOX_TOKEN_VAR if sandbox else PRODUCTION_TOKEN_VAR
    token = (environ.get(name) or "").strip()
    if not token:
        extra = (f" --sandbox never falls back to {PRODUCTION_TOKEN_VAR}: the production token is "
                 "never sent to the sandbox." if sandbox else "")
        sys.exit(f"Error: {name} environment variable is not set or is empty.{extra} "
                 "(This script reads the process environment only; it does not load tools/.env.) "
                 "Nothing was sent.")
    if not TOKEN_SHAPE.fullmatch(token):
        sys.exit(f"Error: the value of {name} contains a space, a control character or a non-ASCII "
                 "character (a token is printable ASCII without whitespace); check how it was exported. "
                 "The value is not shown. Nothing was sent.")
    register_secret(token)
    return token


# ── Durable run journal ────────────────────────────────────────────────────────

# A run's known state is written atomically (unique temp + fsync + os.replace + directory fsync) BEFORE any
# publication-related write is sent and after each step, to a directory OUTSIDE the workspace (--state-dir).
# While a journal of a paper (or of its concept) says RUNNING / UNRECONCILED / DRAFT-LEFT-BEHIND /
# PUBLISHED-UNSAVED, every later run refuses to start.
#
# PUBLISHED-UNSAVED is set the moment a publication is known to have succeeded (the publish answer, or a read
# showing state 'done') and becomes PUBLISHED only after zenodo.json has been written AND read back (sandbox:
# after the sandbox state file was committed). A failed local save, a Ctrl-C right after publish returns, a
# crash: the journal stays PUBLISHED-UNSAVED and blocks, so a re-run cannot publish twice.
UNSAVED_OUTCOME = "PUBLISHED-UNSAVED"
BLOCKING_OUTCOMES = ("RUNNING", "UNRECONCILED", "DRAFT-LEFT-BEHIND", UNSAVED_OUTCOME)
PUBLISHED_OUTCOMES = ("PUBLISHED", "PUBLISHED-RECONCILED")
JOURNAL_SUFFIX = ".run.json"
QUARANTINE_SUFFIX = ".quarantined"


class JournalError(Exception):
    """The journal could not be written: no publication-related request is sent."""


def default_state_dir(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "cosmochrony-zenodo"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _realpath(p) -> str:
    return os.path.realpath(str(p))


def _fsync_file(fh) -> None:
    """Flush ``fh`` to disk: fsync, then (macOS) F_FULLFSYNC as well, best effort."""
    fh.flush()
    os.fsync(fh.fileno())
    try:
        import fcntl
        fcntl.fcntl(fh.fileno(), fcntl.F_FULLFSYNC)      # macOS: also flush the drive cache
    except (ImportError, AttributeError, OSError):
        pass


def _fsync_dir(directory: Path) -> None:
    """fsync the directory so a rename/link survives a power loss. A filesystem that cannot fsync a
    directory (EINVAL/ENOTSUP) is tolerated; any other error is raised."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically: unique temp in the same directory, fsync, os.replace, fsync of the directory."""
    tmp = _temp_sibling(path)
    with open(tmp, "x", encoding="utf-8") as fh:
        fh.write(text)
        _fsync_file(fh)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _create_exclusive_text(path: Path, text: str) -> None:
    """Create ``path`` with ``text`` only if it does not exist: the content is written and fsynced under a unique
    temp name, then hard-linked to ``path`` (os.link fails with FileExistsError if it exists, never
    overwrites); the temp name this call created is then released. A partial file is never visible under ``path``."""
    tmp = _temp_sibling(path)
    with open(tmp, "x", encoding="utf-8") as fh:
        fh.write(text)
        _fsync_file(fh)
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)           # the temp THIS call just created (not a stray found on disk)
    _fsync_dir(path.parent)


def _journal_text(data: dict) -> str:
    """The ONLY serialisation of a journal: every string is redacted BEFORE it is written (F7)."""
    return json.dumps(redact_obj(data), indent=2, ensure_ascii=False) + "\n"


def config_fingerprint(config: dict) -> str:
    """sha256 of the canonical zenodo.json content (used, with bib_key and title, to recognise the same paper
    from another path)."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Journal:
    def __init__(self, path: Path, data: dict):
        self.path, self.data, self.degraded = path, data, False

    @classmethod
    def create(cls, state_dir: Path, paper_dir: Path, host: str, sandbox: bool, intent: dict,
               version=None, concept_id=None, identity=None) -> "Journal":
        state_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", paper_dir.name)
        if concept_id:
            base += f"-c{re.sub(r'[^0-9A-Za-z]', '_', str(concept_id))}"
        path = state_dir / f"{base}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:6]}{JOURNAL_SUFFIX}"
        identity = identity or {}
        data = {"schema": 2, "paper": paper_dir.name, "paper_dir": str(paper_dir),
                "paper_dir_real": _realpath(paper_dir), "host": host,
                "sandbox": bool(sandbox), "kind": "update" if (intent or {}).get("update") else "deposit",
                "intent": intent, "version": version,
                "bib_key": identity.get("bib_key"), "title": identity.get("title"),
                "config_sha256": identity.get("config_sha256"),
                "phase": "start", "outcome": "RUNNING", "deposition_id": None, "record_id": None,
                "concept_id": None if concept_id in (None, "") else str(concept_id),
                "pdf": None, "writes_sent": 0, "last_write": None, "started_at": _now(), "notes": []}
        journal = cls(path, data)
        data["updated_at"] = _now()
        _create_exclusive_text(path, _journal_text(data))      # exclusive: never over an existing journal
        return journal

    def _write(self) -> None:
        self.data["updated_at"] = _now()
        _atomic_write_text(self.path, _journal_text(self.data))

    def update(self, **fields) -> None:
        """Merge and persist. A write failure here (after an answer) is LOUD but never masks the outcome."""
        note = fields.pop("note", None)
        self.data.update(fields)
        if note:
            self.data["notes"].append(f"{_now()} {note}")
        try:
            self._write()
        except OSError as exc:
            self.degraded = True
            print(f"!! JOURNAL WRITE FAILED ({redact(exc)}): {self.path}. The durable record is stale; "
                  "check the server state by hand.", file=sys.stderr)

    def write_intent(self, method: str, path: str) -> None:
        """Strict: called before a write request leaves; if it cannot be persisted, the request is not sent."""
        self.data["writes_sent"] = int(self.data.get("writes_sent") or 0) + 1
        self.data["last_write"] = f"{method} {path}"
        try:
            self._write()
        except OSError as exc:
            raise JournalError(f"cannot write the run journal {self.path} ({redact(exc)}): "
                               "refusing to send a publication-related request") from exc

    def mark_published(self, dep_id, body: dict) -> None:
        """A publication is known to have succeeded. Outcome PUBLISHED-UNSAVED (BLOCKING) until the local
        state is saved and read back (``mark_saved``)."""
        body = body if isinstance(body, dict) else {}
        meta = body.get("metadata") or {}
        files = [{"filename": f.get("filename") or f.get("key"), "checksum": f.get("checksum"),
                  "filesize": f.get("filesize")} for f in (body.get("files") or []) if isinstance(f, dict)]
        self.update(outcome=UNSAVED_OUTCOME, phase="published; local state not yet saved", deposition_id=dep_id,
                    record_id=body.get("id", dep_id), doi=body.get("doi"),
                    concept_id=str(body.get("conceptrecid") or meta.get("conceptrecid")
                                   or self.data.get("concept_id") or "") or None,
                    concept_doi=body.get("conceptdoi") or meta.get("conceptdoi"),
                    published_files=files)

    def mark_saved(self, **extra) -> None:
        """The local state was written and read back: the block is lifted."""
        self.update(outcome="PUBLISHED", phase="local state saved", **extra)

    def outcome(self) -> str:
        return self.data.get("outcome")


def journal_of(client):
    return getattr(client, "journal", None)


def load_journals(state_dir: Path) -> list:
    """[(path, data|None)] for every journal in state_dir; unreadable ones give None (treated as blocking)."""
    out = []
    if not state_dir.is_dir():
        return out
    for path in sorted(state_dir.glob(f"*{JOURNAL_SUFFIX}")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append((path, None))
            continue
        out.append((path, data if isinstance(data, dict) else None))
    return out


def journal_identity(config: dict, title=None) -> dict:
    return {"bib_key": config.get("bib_key"), "title": title, "config_sha256": config_fingerprint(config)}


def find_blocking_journals(state_dir: Path, paper_dir: Path, concept_ids=(), host=None, identity=None) -> list:
    """Blocking journals that concern this paper. A journal matches when ANY of these holds (all documented):
    the same paper directory (realpath); the same concept id on the same host; on the same host, the same
    bib_key, the same title or the same zenodo.json content hash (so a copy/another checkout of the paper is
    recognised even for a first publication, when no concept id exists yet)."""
    concepts = {str(c) for c in concept_ids if c not in (None, "")}
    identity = identity or {}
    here = _realpath(paper_dir)
    blocking = []
    for path, data in load_journals(state_dir):
        if data is None:
            if path.name.startswith(re.sub(r"[^A-Za-z0-9_.-]", "_", paper_dir.name) + "-"):
                blocking.append((path, {"outcome": "UNREADABLE", "paper_dir": str(paper_dir)}))
            continue
        if data.get("outcome") not in BLOCKING_OUTCOMES:
            continue
        why = []
        if data.get("paper_dir") and _realpath(data.get("paper_dir_real") or data["paper_dir"]) == here:
            why.append("same paper directory")
        if concepts and str(data.get("concept_id")) in concepts \
                and (host is None or data.get("host") == host):
            why.append("same concept")
        if data.get("host") == host:
            for key, label in (("bib_key", "same bib_key"), ("title", "same title"),
                               ("config_sha256", "same zenodo.json content")):
                if identity.get(key) and data.get(key) == identity[key]:
                    why.append(label)
        if why:
            blocking.append((path, dict(data, matched_by=why)))
    return blocking


def blocking_message(blocking: list) -> str:
    lines = ["Error: an UNRECONCILED run journal exists for this paper/concept. REFUSING TO START. "
             "DO NOT RE-RUN until it is reconciled."]
    for path, data in blocking:
        lines.append(f"  journal: {path}")
        lines.append(f"    outcome={data.get('outcome')} phase={data.get('phase')!r} host={data.get('host')} "
                     f"deposition={data.get('deposition_id')} concept={data.get('concept_id')} "
                     f"pdf_sha256={(data.get('pdf') or {}).get('sha256')}")
        if data.get("matched_by"):
            lines.append(f"    matched by: {', '.join(data['matched_by'])}")
        host = data.get("host") or PRODUCTION_HOST
        for note in (data.get("notes") or [])[-3:]:
            lines.append(f"    note: {note}")
        if data.get("outcome") == "UNREADABLE":
            lines.append("    this journal cannot be read: its content is unknown. After checking "
                         "https://zenodo.org/deposit yourself:")
            lines.append(f"      zenodo_publish.py --reconcile {path} --quarantine --reason \"what you checked\" "
                         "(renames it to *.quarantined in the same directory; nothing is deleted)")
            continue
        lines.append(f"    inspect: https://{host}/deposit  and  https://{host}/record/{data.get('deposition_id')}")
        if data.get("outcome") == UNSAVED_OUTCOME:
            lines.append("    The publication SUCCEEDED but the local state was not saved: a re-run would publish a "
                         "duplicate. Write it down with (this tool writes only the paper directory recorded in "
                         "the journal, nothing is sent):")
            lines.append(f"      zenodo_publish.py --reconcile {path} --outcome published --record-id N "
                         "--concept-id C --concept-doi D")
            continue
        lines.append("    Then state what you found, locally and without any network access from this tool:")
        lines.append(f"      zenodo_publish.py --reconcile {path} --outcome published --record-id N "
                     "--concept-id C --concept-doi D")
        lines.append(f"      zenodo_publish.py --reconcile {path} --outcome not-published "
                     "(--record-url URL | --i-checked-the-account --reason \"...\") "
                     "(only after any leftover draft was discarded by hand)")
    return "\n".join(lines)


class ReconcileRefused(Exception):
    """A reconcile request that must not be applied (message for the operator)."""


def read_journal_strict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReconcileRefused(
            f"cannot read the journal {path} ({redact(exc)}). If it is corrupt, after checking the account "
            f"yourself: --reconcile {path} --quarantine --reason \"...\" (renames it, deletes nothing)") from exc
    if not isinstance(data, dict):
        raise ReconcileRefused(f"the journal {path} is not a JSON object. Use --quarantine (see --help).")
    return data


def reconcile_journal(path: Path, outcome: str, record_id=None, concept_id=None, concept_doi=None,
                      record_url=None, checked_account=False, reason=None, paper_dir_arg=None) -> dict:
    """Operator-asserted, local only. Returns the updated journal data.

    It writes ONLY the paper directory recorded in the journal (a ``paper_dir_arg`` that is a different
    directory is refused), and only after checking that zenodo.json's bib_key and zenodo_id are consistent with
    the journal. ``not-published`` needs evidence (a record URL, or --i-checked-the-account with a reason)."""
    if not path.name.endswith(JOURNAL_SUFFIX):
        raise ReconcileRefused(f"{path} is not a run journal (*{JOURNAL_SUFFIX})")
    data = read_journal_strict(path)
    if data.get("outcome") not in BLOCKING_OUTCOMES:
        raise ReconcileRefused(f"the journal {path.name} is not unreconciled (outcome {data.get('outcome')!r}).")
    jdir = data.get("paper_dir")
    if not jdir:
        raise ReconcileRefused(f"the journal {path.name} records no paper directory: it cannot be reconciled "
                               "against any zenodo.json")
    if paper_dir_arg is not None and _realpath(paper_dir_arg) != _realpath(data.get("paper_dir_real") or jdir):
        raise ReconcileRefused(
            f"the journal belongs to the paper directory {jdir}, not {paper_dir_arg}: refusing. A reconcile "
            "writes only the paper directory recorded in the journal.")
    host = data.get("host") or PRODUCTION_HOST
    evidence = {}
    if outcome == "published":
        if not record_id:
            raise ReconcileRefused("--outcome published needs --record-id N (the record you inspected).")
        cid = str(concept_id or data.get("concept_id") or "") or None
        cdoi = concept_doi or data.get("concept_doi")
        writes_config = not data.get("sandbox") and data.get("kind") != "update"
        if writes_config:
            if not cid or not cdoi:
                raise ReconcileRefused(
                    "a production deposit is only reconciled as published together with the identifiers to save: "
                    "pass --concept-id C --concept-doi D (the concept record id and DOI you read on the record "
                    "page); they are written to the zenodo.json of the paper directory recorded in the journal.")
            cfg_path = Path(jdir) / "zenodo.json"
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReconcileRefused(f"cannot read {cfg_path} ({redact(exc)})") from exc
            if data.get("bib_key") and cfg.get("bib_key") != data["bib_key"]:
                raise ReconcileRefused(f"{cfg_path} has bib_key {cfg.get('bib_key')!r} but the journal was "
                                       f"written for {data['bib_key']!r}: not the same paper, nothing written.")
            if cfg.get("zenodo_id") not in (None, "") and str(cfg["zenodo_id"]) != str(cid):
                raise ReconcileRefused(f"{cfg_path} already has zenodo_id {cfg['zenodo_id']} but you assert "
                                       f"concept {cid}: another concept, nothing written.")
            if str(cfg.get("zenodo_id")) != str(cid) or cfg.get("doi") != cdoi:
                cfg["zenodo_id"], cfg["doi"] = int(cid), cdoi
                _atomic_write_text(cfg_path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
            check = json.loads(cfg_path.read_text(encoding="utf-8"))
            if str(check.get("zenodo_id")) != str(cid) or check.get("doi") != cdoi:
                raise ReconcileRefused(f"{cfg_path} does not hold the identifiers after writing: check it by hand")
            evidence["config_written"] = str(cfg_path)
        data.update(outcome="PUBLISHED-RECONCILED", record_id=record_id, operator_asserted=True)
        if cid:
            data["concept_id"] = str(cid)
        if cdoi:
            data["concept_doi"] = cdoi
    else:
        if data.get("outcome") == UNSAVED_OUTCOME:
            raise ReconcileRefused(
                "the journal says the publish call SUCCEEDED (PUBLISHED-UNSAVED): it cannot be reconciled as "
                "not-published. Use --outcome published with the record id, concept id and concept DOI.")
        if record_url:
            if not str(record_url).startswith(f"https://{host}/"):
                raise ReconcileRefused(f"--record-url must be a https://{host}/... URL (the page you inspected)")
            evidence["record_url"] = record_url
        elif checked_account and reason:
            evidence.update(checked_account=True)
        else:
            raise ReconcileRefused(
                "--outcome not-published clears the block on the operator's word alone: give --record-url URL "
                "(the record/draft page you inspected) or --i-checked-the-account together with "
                "--reason \"free text\".")
        data.update(outcome="NOT-PUBLISHED-RECONCILED", operator_asserted=True)
    if reason:
        evidence["reason"] = reason
    data["operator_evidence"] = evidence
    data["reconciled_at"] = _now()
    data.setdefault("notes", []).append(f"{_now()} reconciled by the operator: {outcome} {evidence}")
    _atomic_write_text(path, _journal_text(data))
    return data


def quarantine_journal(path: Path, reason: str) -> Path:
    """Rename an UNREADABLE journal to <name>.quarantined (same directory, never deleted, never over a file).
    Prints the path and the size of the corrupt content first. A readable journal is refused."""
    if not path.name.endswith(JOURNAL_SUFFIX):
        raise ReconcileRefused(f"{path} is not a run journal (*{JOURNAL_SUFFIX})")
    if not reason or not reason.strip():
        raise ReconcileRefused("--quarantine needs --reason \"what you checked on the account\"")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReconcileRefused(f"cannot stat {path} ({redact(exc)})") from exc
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    else:
        raise ReconcileRefused(f"{path} is a readable journal: use --outcome published|not-published")
    print(f"Unreadable journal: {path} ({size} bytes). It will be renamed, not deleted.")
    target = path.with_name(path.name + QUARANTINE_SUFFIX)
    n = 0
    while target.exists() or target.is_symlink():
        n += 1
        target = path.with_name(f"{path.name}{QUARANTINE_SUFFIX}.{n}")
    os.rename(path, target)
    _fsync_dir(path.parent)
    note = target.with_name(target.name + ".note.txt")
    _create_exclusive_text(note, redact(f"quarantined {_now()}: {reason}\n"))
    return target


def select_deposit_journal(state_dir: Path, paper_dir: Path, explicit=None):
    """The journal ``--finish-after-fail`` is bound to: the NEWEST production journal of this paper that is a
    deposit (not --update) and carries a PDF digest and a publication (PUBLISHED, PUBLISHED-RECONCILED or
    PUBLISHED-UNSAVED); journals of other kinds, sandbox journals and runs that published nothing never
    shadow it. ``explicit`` (--journal PATH) overrides the choice but is held to the same rules.
    Returns (path, data) or (None, None)."""
    def eligible(data):
        return (data and _realpath(data.get("paper_dir_real") or data.get("paper_dir") or "") == _realpath(paper_dir)
                and not data.get("sandbox") and data.get("kind", "update" if (data.get("intent") or {}).get("update")
                                                        else "deposit") == "deposit"
                and (data.get("pdf") or {}).get("sha256")
                and data.get("outcome") in PUBLISHED_OUTCOMES + (UNSAVED_OUTCOME,))
    if explicit is not None:
        data = read_journal_strict(explicit)
        if data.get("sandbox"):
            die(f"Error: the journal {explicit.name} is a SANDBOX journal: --finish-after-fail never uses one. "
                "Nothing was archived.")
        if not eligible(data):
            die(f"Error: the journal {explicit.name} is not a published production deposit of {paper_dir.name} "
                "with a recorded PDF digest. Nothing was archived.")
        return explicit, data
    best = None
    for path, data in load_journals(state_dir):
        if eligible(data):
            if best is None or str(data.get("started_at")) >= str(best[1].get("started_at")):
                best = (path, data)
    return best if best else (None, None)


def do_not_rerun_notice(client, host: str, dep_id=None) -> str:
    j = journal_of(client)
    where = f" Journal: {j.path}." if j is not None else ""
    return (f"!! DO NOT RE-RUN this deposit: its publication cannot be excluded (deposition {dep_id}).{where} "
            f"Inspect https://{host}/record/{dep_id} and https://{host}/deposit first. If it is published "
            "state so with --reconcile <journal> --outcome published --record-id N --concept-id C "
            "--concept-doi D; if it is not, discard any leftover draft by hand, then --reconcile <journal> "
            "--outcome not-published (--record-url URL | --i-checked-the-account --reason \"...\").")


# ── Cleanup calls: kind -> (method, path). Selectable, explicit, never guessed ─────────────

class UnknownCleanupKind(Exception):
    """A cleanup was requested for a kind that has no entry in CLEANUP_CALLS."""


# Documented semantics (developers.zenodo.org): actions/discard ends an EDITING SESSION; an UNPUBLISHED
# deposition is removed with DELETE /deposit/depositions/:id (403 on a published one).
# Every entry is UNVERIFIED until a sandbox pilot (README-PILOT P5) confirms the status codes and effects.
CLEANUP_CALLS = {
    "edit-session": ("POST", "/deposit/depositions/{id}/actions/discard"),
    "unpublished-new-version-draft": ("DELETE", "/deposit/depositions/{id}"),
    "unpublished-first-draft": ("DELETE", "/deposit/depositions/{id}"),
}
CLEANUP_VERIFIED = {kind: False for kind in CLEANUP_CALLS}      # flip a kind only with pilot evidence
# created_state kind (what the client records) -> CLEANUP_CALLS key
CLEANUP_KIND_OF_STATE = {
    "edit session": "edit-session",
    "new-version draft": "unpublished-new-version-draft",
    "new deposition draft": "unpublished-first-draft",
}


def cleanup_call(state_kind: str) -> tuple[str, str]:
    key = CLEANUP_KIND_OF_STATE.get(state_kind)
    if key is None or key not in CLEANUP_CALLS:
        raise UnknownCleanupKind(f"no cleanup call is defined for {state_kind!r}: nothing will be applied")
    return CLEANUP_CALLS[key]


class ZenodoClient:
    def __init__(self, token: str, sandbox: bool = False, session=None, environ=None):
        global requests
        if requests is None:
            import requests as _req
            requests = _req
        environ = os.environ if environ is None else environ
        self.sandbox = sandbox
        self.base = SANDBOX_URL if sandbox else ZENODO_URL
        self.host = SANDBOX_HOST if sandbox else PRODUCTION_HOST
        # Host/token pairing: the token handed to this client must not be the other environment's.
        other_var = PRODUCTION_TOKEN_VAR if sandbox else SANDBOX_TOKEN_VAR
        other = (environ.get(other_var) or "").strip()
        if isinstance(token, str) and token and not TOKEN_SHAPE.fullmatch(token):
            raise InvalidToken("the token is not printable ASCII without whitespace (value not shown)")
        register_secret(token)
        if not token or (other and token == other):
            raise TokenHostMismatch(
                f"refusing to build a {'sandbox' if sandbox else 'production'} client: the token is "
                f"empty or equals {other_var}, i.e. it belongs to the other Zenodo host")
        validate_numeric_env(environ)
        self.session = session if session is not None else requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        connect = _env_number("ZENODO_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT, environ)
        self.timeout = (connect, _env_number("ZENODO_READ_TIMEOUT", DEFAULT_READ_TIMEOUT, environ))
        self.upload_timeout = (connect, _env_number("ZENODO_UPLOAD_TIMEOUT",
                                                    DEFAULT_UPLOAD_READ_TIMEOUT, environ))
        self.get_attempts = max(1, int(_env_number("ZENODO_GET_ATTEMPTS", DEFAULT_GET_ATTEMPTS, environ)))
        self.retry_backoff = _env_number("ZENODO_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF, environ)
        self._sleep = _sleep
        # Server-side state created, or POSSIBLY created, by THIS run:
        # {"dep_id": int|None, "kind": str, "certain": bool}. Set BEFORE the effect, refined after.
        self.created_state: dict | None = None
        # Filled by the existence guard / first-publication snapshot (see refuse_if_unpublished_state).
        self.pre_run_draft_ids: set | None = None
        self.pre_run_latest_state: str | None = None
        self.concept_id = None
        # Durable run journal (see Journal); None in unit tests that do not need one.
        self.journal = None
        self.uploaded_pdf: dict | None = None
        self.uploaded_pdf_bytes: bytes | None = None

    # ── transport ──
    def _check_url(self, url: str) -> None:
        """Never send the Authorization header anywhere but this client's own Zenodo host."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != self.host:
            raise TokenHostMismatch(
                f"refusing to send a request to {parsed.scheme}://{parsed.hostname}: "
                f"this client talks to https://{self.host} only")

    def _request(self, method: str, url: str, **kw):
        self._check_url(url)
        if method != "GET" and self.journal is not None:
            # Durable record BEFORE the write leaves this process; if it cannot be written, nothing is sent.
            self.journal.write_intent(method, urlparse(url).path)
        kw.setdefault("timeout", self.timeout)
        fn = getattr(self.session, method.lower())
        attempts = self.get_attempts if method == "GET" else 1   # no automatic retry of writes
        for i in range(attempts):
            last = i + 1 == attempts
            try:
                resp = fn(url, **kw)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if last:
                    raise
                self._sleep(min(30.0, self.retry_backoff * (2 ** i)))
                continue
            if method == "GET" and not last and resp.status_code in (429, 502, 503, 504):
                self._sleep(min(30.0, self.retry_backoff * (2 ** i)))
                continue
            return resp

    def _check(self, resp):
        if not resp.ok:
            print(f"  HTTP {resp.status_code}: {redact(resp.text[:500])}", file=sys.stderr)
            raise ZenodoAPIError(resp)

    def _json(self, resp):
        try:
            return resp.json()
        except ValueError as exc:
            raise MalformedResponse(
                f"HTTP {resp.status_code} answer is not JSON ({exc})", resp) from exc

    def _ok(self, resp) -> dict:
        self._check(resp)
        return self._json(resp)

    def _post_creating(self, url: str, kind: str, known_id=None, extract=None):
        """POST that may create server-side state. Intent is recorded BEFORE the request is sent."""
        previous = self.created_state
        self.created_state = {"dep_id": known_id, "kind": kind, "certain": False}
        if self.journal is not None:
            self.journal.update(phase=f"{kind}: creation request about to be sent",
                                deposition_id=known_id, created_kind=kind)
        resp = self._request("POST", url)      # timeout / dropped connection: state stays 'possible'
        if not resp.ok:
            if is_definitive_refusal(ZenodoAPIError(resp)):
                self.created_state = previous   # the server demonstrably did nothing
            self._check(resp)
        body = self._json(resp)
        if extract is not None:
            try:
                new_id = extract(body)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise MalformedResponse(f"answer lacks the expected field ({exc!r})", resp) from exc
            self.created_state = {"dep_id": new_id, "kind": kind, "certain": True}
        else:
            self.created_state["certain"] = True
        if self.journal is not None:
            self.journal.update(phase=f"{kind}: created", deposition_id=self.created_state.get("dep_id"))
        return body

    # ── API ──
    def create_deposition(self) -> dict:
        return self._post_creating(f"{self.base}/deposit/depositions", "new deposition draft",
                                   extract=lambda b: int(b["id"]))

    def get_deposition(self, dep_id: int) -> dict:
        return self._ok(self._request("GET", f"{self.base}/deposit/depositions/{dep_id}"))

    def edit_deposition(self, dep_id: int) -> dict:
        self._post_creating(f"{self.base}/deposit/depositions/{dep_id}/actions/edit",
                            "edit session", known_id=dep_id)
        return self.get_deposition(dep_id)

    def new_version(self, dep_id: int) -> dict:
        body = self._post_creating(
            f"{self.base}/deposit/depositions/{dep_id}/actions/newversion", "new-version draft",
            extract=lambda b: int(b["links"]["latest_draft"].rstrip("/").rsplit("/", 1)[-1]))
        return self._ok(self._request("GET", body["links"]["latest_draft"]))

    def list_drafts(self) -> list[dict]:
        """ALL unpublished depositions of the account (read-only), or CompletenessError.

        Reads pages until an EMPTY page (a short page proves nothing: the server may cap `size`).
        Fails closed when: the cap is hit without an empty page; a page brings nothing new (the
        server ignores `page`); a `next` link exists on an empty page; the server states a total
        that differs from what was read."""
        out: list[dict] = []
        seen: set = set()
        total = None
        for page in range(1, MAX_LIST_PAGES + 1):
            resp = self._request("GET", f"{self.base}/deposit/depositions", params={
                "status": "draft", "size": LIST_PAGE_SIZE, "page": page, "all_versions": "true"})
            batch = self._ok(resp)
            if not isinstance(batch, list):
                raise MalformedResponse("deposition listing is not a JSON list", resp)
            # requests.structures.CaseInsensitiveDict is a Mapping but NOT a dict: look keys up lower-cased.
            raw_headers = resp.headers if isinstance(resp.headers, collections.abc.Mapping) else {}
            headers = {str(k).lower(): v for k, v in raw_headers.items()}
            for key in ("x-total", "x-total-count"):
                if headers.get(key) not in (None, ""):
                    try:
                        total = int(headers[key])
                    except (TypeError, ValueError):
                        pass
            links = resp.links if isinstance(resp.links, collections.abc.Mapping) else {}
            if not batch:
                if links.get("next"):
                    raise CompletenessError("the server returned an empty page but advertises a next page")
                if total is not None and total != len(out):
                    raise CompletenessError(
                        f"the server reports {total} depositions but {len(out)} were read")
                return out
            ids = {d.get("id") for d in batch}
            if ids <= seen:
                raise CompletenessError(f"page {page} brought no new deposition (the server ignores `page`)")
            seen |= ids
            out.extend(batch)
        raise CompletenessError(f"more than {MAX_LIST_PAGES} pages of drafts: cannot prove completeness")

    def discard(self, dep_id: int, kind: str):
        """Clean up server state this run created, with the call the CLEANUP_CALLS table selects for ``kind``.

        ``kind`` is a created_state kind; an unknown kind is refused (UnknownCleanupKind), never guessed."""
        method, path = cleanup_call(kind)
        return self._request(method, f"{self.base}{path.format(id=dep_id)}")

    def get_record(self, rec_id: int) -> dict:
        """Public record (the same API host, read-only)."""
        return self._ok(self._request("GET", f"{self.base}/records/{rec_id}"))

    def resolve_latest_version_recid(self, recid: int) -> int:
        """Resolve a Zenodo recid (concept or version) to the latest published version's recid.
        GET /api/records/{recid} redirects to the latest version when given a concept recid,
        and returns the record itself otherwise."""
        resp = self._ok(self._request("GET", f"{self.base}/records/{recid}"))
        try:
            return int(resp["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponse(f"record {recid} answer has no usable id", None) from exc

    def delete_file(self, dep_id: int, file_id: str) -> None:
        """Remove a file from the draft. A failure is BLOCKING (raises)."""
        resp = self._request("DELETE", f"{self.base}/deposit/depositions/{dep_id}/files/{file_id}")
        self._check(resp)

    def upload_file(self, deposition: dict, file_path: Path, expected_sha256: str | None = None,
                    data: bytes | None = None) -> dict:
        """Upload via S3 bucket API (new depositions and new versions). A failure raises.

        The bytes sent are the bytes digested: ``data`` (read once by the caller, who also journaled their
        digest and will archive the same bytes) or, without it, the file read here. With ``expected_sha256``
        (the digest the journal recorded) different bytes are refused."""
        bucket_url = deposition["links"]["bucket"]
        if data is None:
            data = Path(file_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise VerificationError(f"{Path(file_path).name} changed between the digest and the upload "
                                    f"({expected_sha256} -> {digest}): refusing to upload")
        return self._ok(self._request(
            "PUT", f"{bucket_url}/{Path(file_path).name}", data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.upload_timeout))

    def set_metadata(self, dep_id: int, metadata: dict) -> dict:
        return self._ok(self._request(
            "PUT", f"{self.base}/deposit/depositions/{dep_id}", json={"metadata": metadata}))

    def publish(self, dep_id: int) -> dict:
        """Publish. The journal says UNRECONCILED from before the request leaves until an answer settles it."""
        j = self.journal
        if j is not None:
            j.update(outcome="UNRECONCILED", phase="publish request about to be sent",
                     deposition_id=dep_id,
                     note="a publish request was (about to be) sent: the outcome is unknown until reconciled")
        resp = self._request("POST", f"{self.base}/deposit/depositions/{dep_id}/actions/publish")
        if not resp.ok and is_definitive_refusal(ZenodoAPIError(resp)) and j is not None:
            j.update(outcome="RUNNING", phase=f"publish refused by the server (HTTP {resp.status_code})",
                     note="the server refused the publish request: nothing was published")
        if resp.ok and j is not None:
            # Signal-safe order: a success answer is recorded as a BLOCKING outcome before anything else can
            # be interrupted (until now the journal said UNRECONCILED, also blocking).
            j.update(outcome=UNSAVED_OUTCOME, phase="publish answered; local state not yet saved",
                     deposition_id=dep_id)
        body = self._ok(resp)
        if j is not None:
            j.mark_published(dep_id, body)
        return body


# ── BibTeX DOI cascade ─────────────────────────────────────────────────────────

def update_bib_file(bib_path: Path, bib_key: str, doi: str) -> tuple[bool, bool]:
    """Return ``(found, changed)`` after checking one bibliography entry."""
    text = bib_path.read_text(encoding="utf-8")
    entry_re = re.compile(
        r"(@\w+\{" + re.escape(bib_key) + r"\b.*?\n\})",
        re.DOTALL,
    )
    m = entry_re.search(text)
    if not m:
        return False, False
    updated = re.sub(r"\\doi\{[^}]*\}", rf"\\doi{{{doi}}}", m.group(1))
    if updated == m.group(1):
        return True, False
    bib_path.write_text(text[: m.start()] + updated + text[m.end() :], encoding="utf-8")
    return True, True


def doi_cascade(repo_root: Path, bib_key: str, doi: str):
    updated: list[Path] = []
    found: list[Path] = []
    for bib in sorted(repo_root.rglob("cosmochrony-bibliography.bib")):
        is_found, is_changed = update_bib_file(bib, bib_key, doi)
        if is_found:
            found.append(bib.relative_to(repo_root))
        if is_changed:
            updated.append(bib.relative_to(repo_root))
    if updated:
        print(f"  Updated {len(updated)} bibliography file(s):")
        for p in updated:
            print(f"    {p}")
    elif found:
        print(f"  Found key in {len(found)} bibliography file(s); DOI already current.")
    else:
        print(f"  Warning: key '{bib_key}' not found in any cosmochrony-bibliography.bib")


class ArchiveRefused(Exception):
    """The archive step would overwrite a different file, or the PDF is not the one that was uploaded, or the
    copy could not be written and verified."""


class ArchiveResult(NamedTuple):
    path: Path
    status: str            # 'archived' (written now) or 'identical' (already there, same sha256)


def stray_archive_files(dest_dir: Path, dest_name: str) -> list:
    """(path, size) of leftover temp files of an interrupted archive write. Only REPORTED, never deleted."""
    if not dest_dir.is_dir():
        return []
    out = []
    for p in sorted(dest_dir.glob(f".{dest_name}.*.tmp")):
        try:
            out.append((p, p.stat().st_size))
        except OSError:
            out.append((p, None))
    return out


def archive_pdf(repo_root: Path, pdf_path: Path, name_stem: str, version: str | None, rec_id,
                expected_sha256: str | None = None, data: bytes | None = None) -> ArchiveResult:
    """Copy the just-published PDF into the central <root>/pdfs/ archive, NEVER over an existing file.

    The archive name follows the repository convention <MainTexBasename>_<version>.pdf
    (e.g. program_1.69.pdf). Falls back to the Zenodo record id when no version label is available.

    ``data`` (the bytes that were digested and uploaded, held in memory) is what gets written; without it the
    file is read ONCE here. ``expected_sha256`` is the digest of the PDF that was UPLOADED: different bytes are
    refused. An existing destination is compared by full sha256: identical -> ArchiveResult 'identical',
    different -> ArchiveRefused with both digests and nothing written; a symlink or directory at the
    destination is refused. The copy is written under a unique temp name next to the destination, fsynced and
    verified, then hard-linked to the final name (os.link fails if the name exists): a failed write never
    leaves a truncated file under the final name. A leftover temp file from an earlier failure is REPORTED
    (path, size) and never deleted by this tool."""
    if data is None:
        data = Path(pdf_path).read_bytes()
    src = bytes_digests(data, Path(pdf_path).name)
    if expected_sha256 is not None and src["sha256"] != expected_sha256:
        raise ArchiveRefused(
            f"{pdf_path} (sha256 {src['sha256']}) is not the PDF that was uploaded (sha256 {expected_sha256}): "
            "nothing was archived. Rebuild/restore the deposited PDF, or archive it by hand.")
    label = version or str(rec_id)
    dest_dir = repo_root / "pdfs"
    dest = dest_dir / f"{name_stem}_{label}.pdf"
    if dest_dir.is_symlink() or (dest_dir.exists() and not dest_dir.is_dir()):
        raise ArchiveRefused(f"{dest_dir} is a symbolic link or not a directory: refusing to archive into it.")
    if dest.is_symlink():
        raise ArchiveRefused(f"{dest} is a symbolic link: refusing to archive through it. Nothing was written.")
    if dest.is_dir():
        raise ArchiveRefused(f"{dest} is a directory: refusing to archive. Nothing was written.")
    for stray, size in stray_archive_files(dest_dir, dest.name):
        print(f"  !! stray partial archive file from an earlier failed write: {stray} ({size} bytes); "
              "it is NOT deleted by this tool: inspect it and remove it yourself.", file=sys.stderr)

    def judge_existing() -> ArchiveResult:
        existing = file_digests(dest)
        if existing["sha256"] == src["sha256"]:
            print(f"  Already archived, identical → {_rel(dest, repo_root)} (sha256 {src['sha256']})")
            return ArchiveResult(dest, "identical")
        raise ArchiveRefused(
            f"{dest} already exists with sha256 {existing['sha256']} but the PDF to archive has sha256 "
            f"{src['sha256']}: refusing to overwrite an archive. Nothing was written.")

    if dest.exists():
        return judge_existing()
    dest_dir.mkdir(exist_ok=True)
    tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            _fsync_file(fh)
    except OSError as exc:
        size = tmp.stat().st_size if tmp.exists() else 0
        raise ArchiveRefused(
            f"writing the archive copy failed ({redact(exc)}). A partial temp file ({size} bytes) was left at "
            f"{tmp}: it is NOT deleted, inspect it. Nothing exists under the final name {dest.name}.") from exc
    if file_digests(tmp)["sha256"] != src["sha256"]:
        raise ArchiveRefused(f"the archive copy {tmp} does not match the source after writing; it is left in "
                             f"place (not deleted) and nothing was created under {dest.name}.")
    try:
        os.link(tmp, dest)                                 # exclusive: fails if the name exists
    except FileExistsError:
        os.unlink(tmp)                                     # our own, complete, just-created temp
        if dest.is_symlink() or dest.is_dir():
            raise ArchiveRefused(f"{dest} appeared as a symbolic link or directory: refusing.")
        return judge_existing()
    os.unlink(tmp)                                         # our own temp; the final name now holds the bytes
    _fsync_dir(dest_dir)
    if file_digests(dest)["sha256"] != src["sha256"]:
        raise ArchiveRefused(f"the archived copy {dest} does not match the source after linking")
    print(f"  Archived PDF → {_rel(dest, repo_root)} (sha256 {src['sha256']})")
    return ArchiveResult(dest, "archived")


# ── Metadata builder ───────────────────────────────────────────────────────────

def get_git_branch(paper_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=paper_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_code_url(paper_dir: Path, config: dict) -> str:
    """Return the GitHub URL of the paper's code directory, or "" when it has none.

    A paper that ships reproduction code must expose it from its Zenodo record, so a reader
    reaching the deposited PDF can reach the code without guessing a repository name. The URL
    comes from zenodo.json's "code_url"; when that is absent it is derived from the paper's own
    git remote, which is the normal case and keeps existing configs working. A paper that has a
    code/ directory but no resolvable URL is a configuration error, not a silent skip.
    """
    code_url = (config.get("code_url") or "").strip()
    has_code_dir = (paper_dir / "code").is_dir()
    if code_url:
        return code_url
    if not has_code_dir:
        return ""

    remote = subprocess.run(
        ["git", "-C", str(paper_dir), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True,
    ).stdout.strip()
    slug = re.sub(r"^(?:https://github\.com/|git@github\.com:)|(?:\.git)$", "", remote)
    if remote and re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        return f"https://github.com/{slug}/tree/main/code"

    sys.exit(
        f"Error: {paper_dir.name} has a code/ directory but no resolvable code URL.\n"
        f"Its git remote is {remote or '(none)'}, which is not a GitHub repository.\n"
        'Add "code_url": "https://github.com/Cosmochrony/<repo>/tree/main/code" to zenodo.json.'
    )


def build_metadata(title: str, abstract: str, keywords: list[str], web_url: str,
                   version: str | None = None,
                   publication_type: str | None = None,
                   notes: str | None = None,
                   code_url: str | None = None) -> dict:
    meta: dict = {
        "title": title,
        "description": abstract,
        "keywords": keywords,
        "creators": [CREATOR],
        "upload_type": UPLOAD_TYPE,
        "publication_type": publication_type or PUBLICATION_TYPE,
        "language": LANGUAGE,
        "license": LICENSE_ID,
    }
    if version:
        meta["version"] = version
    if notes:
        meta["notes"] = notes
    related: list[dict] = []
    if web_url:
        related.append({
            "identifier": web_url,
            "relation": "isSupplementedBy",
            "scheme": "url",
            "resource_type": "other",
        })
    if related:
        meta["related_identifiers"] = related
    if code_url:
        # The code link belongs in the record's "Software" section ("Repository URL"), not in
        # the related-works list. The legacy deposit API forwards metadata["custom"] verbatim
        # into the InvenioRDM custom_fields (zenodo-rdm legacy/deserializers/schemas.py), and
        # "code:codeRepository" is the CodeMeta URL field behind that section; the section is
        # rendered on publication/preprint records too (e.g. record 15666809).
        meta["custom"] = {CODE_REPOSITORY_FIELD: code_url}
    return meta




# ── Version-label guard (independent of the pre-deposit gate and of --skip-checks) ──────────

FORBIDDEN_LABELS = ("main", "master", "head")
VERSION_SHAPE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
UPDATABLE_FIELDS = {"version": "version", "title": "title", "abstract": "description", "keywords": "keywords",
                    "notes": "notes", "web-url": "related_identifiers", "publication-type": "publication_type",
                    "code-url": "custom"}


class VersionLabelError(Exception):
    pass


def git_branch_raw(paper_dir: Path):
    """Current branch name; 'HEAD' for a detached head; None when paper_dir is not in a git repository."""
    try:
        inside = subprocess.run(["git", "-C", str(paper_dir), "rev-parse", "--git-dir"],
                                capture_output=True, text=True)
        if inside.returncode != 0:
            return None
        res = subprocess.run(["git", "-C", str(paper_dir), "symbolic-ref", "--short", "-q", "HEAD"],
                             capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "HEAD"


def cff_version(paper_dir: Path):
    cff = paper_dir / "CITATION.cff"
    if not cff.is_file():
        return None
    m = re.search(r'^version:\s*["\']?([^"\'\s#]+)', cff.read_text(encoding="utf-8", errors="ignore"), re.M)
    return m.group(1) if m else None


def check_version_label(outgoing, paper_dir: Path, config: dict, branch_rule: bool = True) -> list:
    """Refuse a label that must not be published. Returns notes; raises VersionLabelError.

    Rules: not empty; never main/master/HEAD; plausible characters; equal to zenodo.json's ``version`` when
    present; when the paper is in a git repository: equal to the branch name (and the branch is not
    main/master/detached); equal to CITATION.cff's version when present."""
    notes = []
    v = str(outgoing or "").strip()
    if not v:
        raise VersionLabelError("no version label (neither zenodo.json 'version' nor a version branch)")
    if v.lower() in FORBIDDEN_LABELS:
        raise VersionLabelError(f"the version label {v!r} must never be published (it is not a version)")
    if not VERSION_SHAPE.fullmatch(v) or ".." in v:
        raise VersionLabelError(f"the version label {v!r} is not a plausible version string "
                                "(letters, digits and . _ + - only, starting with a letter or digit, at most 64 "
                                "characters, no '..': it becomes part of an archive file name)")
    if config.get("version") is not None and str(config["version"]).strip() != v:
        raise VersionLabelError(f"outgoing version {v!r} != zenodo.json version {config['version']!r}")
    if branch_rule:
        branch = git_branch_raw(paper_dir)
        if branch is None:
            notes.append("not a git repository: the branch rule was not checked")
        elif branch.lower() in FORBIDDEN_LABELS:
            raise VersionLabelError(f"the paper is on {branch!r}: main/master/a detached HEAD is never "
                                    "published; cut the version branch first")
        elif branch != v:
            raise VersionLabelError(f"outgoing version {v!r} != git branch {branch!r}")
    cff = cff_version(paper_dir)
    if cff is not None and cff != v:
        raise VersionLabelError(f"outgoing version {v!r} != CITATION.cff version {cff!r}")
    return notes


# ── --update: preserve the live metadata, change only what was explicitly requested ─────────

CONFIRM_MIN_HEX = 12
CONFIRM_SHAPE = re.compile(rf"[0-9a-f]{{{CONFIRM_MIN_HEX},64}}")


class NothingToUpdate(Exception):
    pass


def merge_related_web_url(live_list, entry: dict) -> list:
    """The live ``related_identifiers`` with ONLY the web-page entry replaced or inserted; every other entry
    (cites, isSupplementedBy of another resource, ...) is kept untouched and in place.

    The entry to replace is the live one with the same identifier and relation; failing that, the single live
    entry with the same relation and scheme whose resource_type equals ``entry``'s (a live entry that states no
    resource_type matches too). Several candidates are ambiguous: refused. None: the entry is appended."""
    items = copy.deepcopy(live_list) if isinstance(live_list, list) else []
    dicts = [i for i, e in enumerate(items) if isinstance(e, dict)]
    exact = [i for i in dicts if items[i].get("identifier") == entry.get("identifier")
             and items[i].get("relation") == entry.get("relation")]
    if exact:
        items[exact[0]] = copy.deepcopy(entry)
        return items
    same_role = [i for i in dicts if items[i].get("relation") == entry.get("relation")
                 and items[i].get("scheme") == entry.get("scheme")
                 and items[i].get("resource_type") in (None, entry.get("resource_type"))]
    if len(same_role) > 1:
        raise ValueError(f"{len(same_role)} live related identifiers have the same relation/scheme/resource_type "
                         "as the web page entry: cannot tell which one is the web url; edit it by hand")
    if same_role:
        items[same_role[0]] = copy.deepcopy(entry)
    else:
        items.append(copy.deepcopy(entry))
    return items


def build_update_payload(live: dict, built: dict, fields, code_url: str = "") -> dict:
    """The metadata to PUT: a deep copy of ``live`` with ONLY the requested fields replaced."""
    payload = copy.deepcopy(live)
    for field in sorted(fields):
        if field not in UPDATABLE_FIELDS:
            raise ValueError(f"unknown updatable field {field!r}")
        key = UPDATABLE_FIELDS[field]
        if field == "code-url":
            if not code_url:
                raise ValueError("--set-code-url was requested but this paper has no code_url")
            payload["custom"] = {**(live.get("custom") or {}), CODE_REPOSITORY_FIELD: code_url}
        elif field == "web-url":
            entries = built.get("related_identifiers") or []
            if len(entries) != 1:
                raise ValueError("field 'web-url' was requested but the local state has no web_url")
            payload[key] = merge_related_web_url(live.get(key), entries[0])
        elif key in built:
            payload[key] = copy.deepcopy(built[key])
        else:
            raise ValueError(f"field {field!r} was requested but the local state has no value for it")
    return payload


def metadata_diff(live: dict, payload: dict) -> tuple[str, str]:
    """(unified diff live -> to-be-sent, its sha256). Offline, whole metadata. Empty diff: ('', digest)."""
    def lines(m):
        return json.dumps(m, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    text = "\n".join(difflib.unified_diff(lines(live), lines(payload), "live", "to-be-sent", lineterm="", n=3))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalised_metadata(m: dict) -> dict:
    out = copy.deepcopy(m or {})
    if not out.get("custom"):
        out.pop("custom", None)
    return out


# ── Guarded deposit flows ──────────────────────────────────────────────────────

class VerificationError(Exception):
    """A pre- or post-publication check failed."""


class PostPublishFailure(Exception):
    """The record is published but a check failed (never un-published automatically)."""


def custom_repository(meta: dict | None) -> str | None:
    return ((meta or {}).get("custom") or {}).get(CODE_REPOSITORY_FIELD)


def _norm_url(url):
    return url.rstrip("/") if isinstance(url, str) else url


def verify_attempts() -> int:
    """Post-publication attempts: ZENODO_VERIFY_ATTEMPTS (default 5), clamped to 1..6 (total wait <= 46 s)."""
    return max(1, min(6, int(_env_number("ZENODO_VERIFY_ATTEMPTS", DEFAULT_VERIFY_ATTEMPTS))))


def _verify_delay(k: int) -> float:
    return VERIFY_DELAYS[min(k, len(VERIFY_DELAYS) - 1)] if VERIFY_DELAYS else 0.0


def _latest_draft_problems(client, link, latest_id: int) -> list:
    """``links.latest_draft`` of a published deposition. The documented sample of an unpublished deposition
    points to ITSELF, so the link alone proves nothing: it counts as a draft only if it points to ANOTHER id
    whose own GET shows an unpublished state. A link that cannot be read or judged refuses (safe)."""
    try:
        lid = int(str(link).rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return [f"record {latest_id} advertises a latest_draft that cannot be parsed: {link}"]
    if lid == latest_id:
        return []                                  # self-pointing: not a draft
    try:
        dep = client.get_deposition(lid)
    except Exception as exc:  # noqa: BLE001
        return [f"record {latest_id} advertises a latest_draft {link} that cannot be read ({redact(exc)})"]
    state, submitted = dep.get("state"), dep.get("submitted")
    if state is None:
        return [f"latest_draft {link} has no state: ambiguous"]
    if state == "done" and submitted is not False:
        return []                                  # another PUBLISHED deposition: not a draft
    return [f"record {latest_id} advertises a latest_draft {link}, unpublished (state {state!r}, "
            f"submitted {submitted!r})"]


def refuse_if_unpublished_state(client, zenodo_id, latest_id: int, host: str):
    """Refuse (nothing created) when a draft / edit session of this concept already exists.

    The concept id is taken from the latest record itself (zenodo_id may be a version id), and the
    record must be in state 'done' (a missing state refuses). On success the pre-run snapshot of ALL
    draft ids is stored on the client: it is what lets a later cleanup tell this run's drafts from others."""
    problems = []
    try:
        latest = client.get_deposition(latest_id)
        concept = latest.get("conceptrecid")
        if concept in (None, ""):
            raise RuntimeError(f"record {latest_id} does not state its conceptrecid")
        drafts = client.list_drafts()
        ids = {d.get("id") for d in drafts}
        for d in drafts:
            if str(d.get("conceptrecid")) == str(concept) or d.get("id") == latest_id:
                problems.append(f"unpublished deposition {d.get('id')} (state {d.get('state')})")
        if latest.get("state") != "done":
            problems.append(f"record {latest_id} is in state {latest.get('state')!r}, not 'done'")
        link = (latest.get("links") or {}).get("latest_draft")
        if link:
            problems.extend(_latest_draft_problems(client, link, latest_id))
    except Exception as exc:  # noqa: BLE001 - fail closed: cannot prove there is no draft
        die(f"Error: cannot determine whether a draft already exists ({redact(exc)}). "
            "Nothing was created. Refusing to proceed.")
    if problems:
        sys.exit(
            "Error: an unpublished draft/edit session of this concept already exists:\n  "
            + "\n  ".join(problems)
            + f"\nNothing was created and nothing was discarded. Inspect https://{host}/deposit "
            "and resolve it by hand (publish or discard it yourself), then re-run."
        )
    client.pre_run_draft_ids = ids
    client.concept_id = concept
    client.pre_run_latest_state = "done"
    j = journal_of(client)
    if j is not None:
        j.update(concept_id=str(concept), phase="guard passed: no draft of the concept")
    return latest


def snapshot_before_first_publication(client, host: str):
    """First publication has no concept to guard, but the pre-run draft ids are still needed to tell
    this run's draft from anyone else's if its creation request ends in doubt."""
    try:
        client.pre_run_draft_ids = {d.get("id") for d in client.list_drafts()}
    except Exception as exc:  # noqa: BLE001
        die(f"Error: cannot snapshot the existing drafts ({redact(exc)}); without it an uncertain creation "
            "could not be attributed safely. Nothing was created. Refusing to proceed.")
    client.concept_id = None


UNPUBLISHED_STATES = ("unsubmitted", "inprogress")


def locate_own_state(client, state: dict):
    """Resolve a state-creating request of unknown outcome.

    Returns (verdict, dep_id, candidates, why) with verdict 'own' (CERTAINLY this run's, discardable),
    'none' (nothing found) or 'unknown' (not certain: delete nothing). Draft kinds: a draft is ours
    only if its id is absent from the pre-run snapshot AND it belongs to the concept AND it is in an
    unpublished state AND it is the only such draft. An edit session: the record was proven 'done'
    before this run and is not 'done' now."""
    kind = state["kind"]
    snapshot, concept = client.pre_run_draft_ids, client.concept_id
    if kind == "edit session":
        rid = state["dep_id"]
        if client.pre_run_latest_state != "done":
            return "unknown", None, [rid], "the state of the record before this run was not established"
        try:
            dep = client.get_deposition(rid)
        except Exception as exc:  # noqa: BLE001
            return "unknown", None, [rid], f"cannot read record {rid} ({redact(exc)})"
        if dep.get("state") == "done":
            return "none", None, [], f"record {rid} is in state 'done': no edit session exists"
        return "own", rid, [], ""
    if snapshot is None:
        return "unknown", None, [], "no pre-run snapshot of the drafts was taken"
    try:
        drafts = client.list_drafts()
    except Exception as exc:  # noqa: BLE001
        return "unknown", None, [], f"cannot list the drafts ({redact(exc)})"
    cands = [d for d in drafts
             if d.get("id") not in snapshot
             and d.get("state") in UNPUBLISHED_STATES
             and (concept is None or str(d.get("conceptrecid")) == str(concept))]
    ids = [d.get("id") for d in cands]
    if len(cands) == 1:
        return "own", ids[0], [], ""
    if not cands:
        return "none", None, [], "no draft absent from the pre-run snapshot was found"
    return "unknown", None, ids, f"{len(cands)} drafts are absent from the pre-run snapshot"


def _poll_published(client, dep_id):
    """Bounded re-reads of a deposition after a publish call of unknown outcome. -> (dep|None, done)."""
    attempts, last = verify_attempts(), None
    for k in range(attempts):
        try:
            last = client.get_deposition(dep_id)
            if last.get("state") == "done":
                return last, True
        except Exception as exc:  # noqa: BLE001
            print(f"  (re-read {k + 1}/{attempts} of deposition {dep_id} failed: {redact(exc)})", file=sys.stderr)
        if k < attempts - 1:
            _sleep(_verify_delay(k))
    return last, False


def _cleanup(client, step: str, exc: BaseException, host: str, on_published):
    """Decide and perform the cleanup. Returns (message, recovered_deposition, local_save_error).

    Every verdict is also written to the run journal: NOT-PUBLISHED (nothing left on the server),
    PUBLISHED, UNRECONCILED (a publication cannot be excluded) or DRAFT-LEFT-BEHIND (a draft/edit session
    may remain and was not, or could not be, removed). The last two block every later run."""
    j = journal_of(client)

    def mark(outcome: str, note: str, **extra) -> None:
        if j is not None:
            j.update(outcome=outcome, note=note, **extra)

    sandbox = host == SANDBOX_HOST
    state = client.created_state
    if not state:
        mark("NOT-PUBLISHED", "no server-side state was created by this run")
        return ("No server-side state was created, or possibly created, by this run (no state-creating "
                "request was sent, or the server demonstrably refused it); nothing to clean up.",
                None, None)
    kind, dep_id, certain = state["kind"], state.get("dep_id"), bool(state.get("certain"))
    snapshot, concept = client.pre_run_draft_ids, client.concept_id
    if not certain:
        print(f"!! UNCERTAINTY: the request creating a {kind} was sent but its outcome is unknown "
              "(timeout, dropped connection, unreadable or incomplete answer). The server MAY have "
              "created it. Searching for the state this run may have created...", file=sys.stderr)
        verdict, found, cands, why = locate_own_state(client, state)
        if verdict == "none":
            mark("NOT-PUBLISHED", f"no {kind} attributable to this run was found ({why})")
            return (f"!! No {kind} attributable to this run was found ({why}). Nothing was discarded. "
                    f"The server may still show it later: check https://{host}/deposit before any retry.",
                    None, None)
        if verdict == "unknown":
            lines = [f"!! UNCERTAIN: a {kind} may exist and cannot be attributed with certainty ({why}).",
                     "!! NOTHING WAS DISCARDED."]
            if cands:
                lines.append("!! Candidate ids: " + ", ".join(f"https://{host}/deposit/{c}" for c in cands))
            lines.append(f"!! MANUAL CHECK REQUIRED: open https://{host}/deposit, identify what this run "
                         "created (unpublished, created just now) and discard it yourself if it is yours.")
            mark("DRAFT-LEFT-BEHIND", f"{kind} possibly left behind, unattributable ({why})",
                 manual_url=f"https://{host}/deposit", candidates=cands)
            lines.append(do_not_rerun_notice(client, host, dep_id or (cands[0] if cands else None)))
            return "\n".join(lines), None, None
        dep_id = found
        print(f"!! Attributed to this run with certainty: {kind} {dep_id}", file=sys.stderr)
    manual = f"https://{host}/deposit/{dep_id}"
    print(f"!! This run created: {kind} {dep_id}", file=sys.stderr)
    if kind != "edit session" and snapshot is not None and dep_id in snapshot:
        mark("DRAFT-LEFT-BEHIND", f"{kind} {dep_id} existed before this run: not discarded", manual_url=manual)
        return (f"!! REFUSING to discard {kind} {dep_id}: it existed before this run (pre-run snapshot). "
                f"Inspect {manual} by hand.", None, None)
    dep, read_err = None, None
    try:
        dep = client.get_deposition(dep_id)
    except Exception as exc2:  # noqa: BLE001
        read_err = exc2
    if step == "publish":
        if dep is None:
            mark("UNRECONCILED", f"state of {dep_id} unreadable after the failed publish call",
                 manual_url=manual)
            return (f"!! CLEANUP SKIPPED: cannot read state of {dep_id} after the failed publish call "
                    f"({redact(read_err)}). Inspect {manual} by hand; it may be published or still a draft. "
                    f"Nothing was discarded.\n{do_not_rerun_notice(client, host, dep_id)}", None, None)
        if dep.get("state") != "done" and not is_definitive_refusal(exc):
            # The call may still have gone through (timeout, dropped connection, interruption, 5xx).
            dep, done = _poll_published(client, dep_id)
            if not done:
                mark("UNRECONCILED", f"publish of {dep_id} ended in doubt and the record is not (yet) done",
                     manual_url=manual)
                return (f"!! The publish call ended in doubt and {dep_id} is not (yet) published. "
                        f"NOTHING WAS DISCARDED: the publication may still complete. Inspect {manual} "
                        f"and https://{host}/record/{dep_id}; discard by hand only if it is a draft.\n"
                        f"{do_not_rerun_notice(client, host, dep_id)}", None, None)
        if dep.get("state") == "done":
            save_error = None
            if j is not None:
                j.mark_published(dep_id, dep)
            msg = (f"!! The publish call failed but {dep_id} is PUBLISHED (state done). No cleanup. "
                   f"Check https://{host}/record/{dep_id}.")
            if on_published is not None:
                try:
                    on_published(dep)
                except Exception as exc3:  # noqa: BLE001
                    save_error = exc3
                    msg += (f"\n!! LOCAL STATE NOT SAVED ({redact(exc3)}). " + _manual_state_hint(sandbox, client)
                            + " DO NOT re-run the deposit: it would publish twice.")
            return msg, dep, save_error
    else:
        if dep is not None and dep.get("state") == "done":
            mark("UNRECONCILED", f"{kind} {dep_id} is unexpectedly in state done", manual_url=manual)
            return (f"!! {kind} {dep_id} is in state 'done': nothing to discard (it is not an unpublished "
                    f"state). Inspect {manual}.\n{do_not_rerun_notice(client, host, dep_id)}", None, None)
        if dep is not None and concept is not None and dep.get("conceptrecid") not in (None, "") \
                and str(dep.get("conceptrecid")) != str(concept):
            mark("DRAFT-LEFT-BEHIND", f"{dep_id} belongs to another concept: not discarded", manual_url=manual)
            return (f"!! REFUSING to discard {dep_id}: it belongs to concept {dep.get('conceptrecid')}, "
                    f"not {concept}. Inspect {manual} by hand.", None, None)
        if dep is None and not certain:
            mark("DRAFT-LEFT-BEHIND", f"{dep_id} unreadable: not discarded", manual_url=manual)
            return (f"!! CLEANUP SKIPPED: cannot read {dep_id} ({redact(read_err)}). Inspect {manual}.",
                    None, None)
    try:
        method, path = cleanup_call(kind)
        r = client.discard(dep_id, kind)
        ok = r.ok
        detail = f"HTTP {r.status_code}: {redact(r.text[:200])}"
        what = f"{method} {path.format(id=dep_id)}"
    except Exception as exc2:  # noqa: BLE001
        ok, detail, what = False, redact(exc2), "cleanup call"
    unverified = "" if CLEANUP_VERIFIED.get(CLEANUP_KIND_OF_STATE.get(kind), False) else \
        " [server semantics of this call are UNVERIFIED until the sandbox pilot]"
    if ok:
        mark("NOT-PUBLISHED", f"{kind} {dep_id} cleaned up with {what}", discarded=dep_id)
        return (f"Cleanup performed: {what} on {kind} {dep_id} ({detail}).{unverified} "
                "Nothing published. Exiting non-zero.", None, None)
    mark("DRAFT-LEFT-BEHIND", f"cleanup of {kind} {dep_id} failed ({detail})", manual_url=manual)
    return (f"!! CLEANUP FAILED for {kind} {dep_id} ({detail}).{unverified}\n"
            f"!! MANUAL CLEANUP REQUIRED: discard it at {manual}. Nothing was published.\n"
            f"!! The run journal says DRAFT-LEFT-BEHIND: later runs refuse until you reconcile it "
            f"(--reconcile <journal> --outcome not-published).", None, None)


def _manual_state_hint(sandbox: bool, client=None) -> str:
    j = journal_of(client) if client is not None else None
    ref = str(j.path) if j is not None else "<journal>"
    if sandbox:
        return ("This was a SANDBOX run: do NOT touch any production zenodo.json. The result is in the run "
                f"journal ({ref}), which stays {UNSAVED_OUTCOME} and blocks later runs until you "
                f"`--reconcile {ref} --outcome published --record-id N`.")
    return (f"The run journal ({ref}) says {UNSAVED_OUTCOME} and BLOCKS every later run of this paper. Settle it "
            f"with `--reconcile {ref} --outcome published --record-id N --concept-id C --concept-doi D`, "
            "which writes zenodo_id and doi into the zenodo.json of the paper directory recorded in the "
            "journal (nothing is sent); if you set them by hand first, run the same command to lift the block.")


def abort_and_cleanup(client, step: str, exc: BaseException, host: str, on_published=None):
    """Report a failure, clean up ONLY state this run certainly created, then exit non-zero.

    KeyboardInterrupt / SystemExit are handled like any failure and RE-RAISED afterwards. Returns
    (deposition, local_save_error) only when the failed publish call turned out to have published."""
    print(f"\n!! FAILED at step: {step}", file=sys.stderr)
    resp = getattr(exc, "response", None)
    if resp is not None:
        print(f"!! HTTP status: {resp.status_code}; body: {redact(resp.text[:300])}", file=sys.stderr)
    print(f"!! Error: {type(exc).__name__}: {redact(exc)}", file=sys.stderr)
    interrupted = not isinstance(exc, Exception)
    try:
        msg, recovered, save_error = _cleanup(client, step, exc, host, on_published)
    except BaseException as exc2:  # noqa: BLE001 - a second interrupt or a bug must not hide the state
        state = client.created_state or {}
        msg = (f"!! CLEANUP ABORTED ({type(exc2).__name__}: {redact(exc2)}). State possibly created by this run: "
               f"{state.get('kind')} {state.get('dep_id')}. NOTHING MORE WAS DONE: inspect "
               f"https://{host}/deposit by hand.")
        j = journal_of(client)
        if j is not None:
            j.update(outcome="UNRECONCILED", note="cleanup aborted; server state unknown",
                     manual_url=f"https://{host}/deposit")
        msg += "\n" + do_not_rerun_notice(client, host, state.get("dep_id"))
        if not isinstance(exc2, Exception):
            print(msg, file=sys.stderr)
            raise
        recovered, save_error = None, None
    if recovered is not None and not interrupted:
        print(msg, file=sys.stderr)
        return recovered, save_error
    if interrupted:
        print(msg, file=sys.stderr)
        raise exc
    die(msg)


def verify_draft_code_repository(client, dep_id: int, expected: str | None):
    """After the metadata PUT: the draft must carry the expected code repository."""
    if not expected:
        return
    got = custom_repository(client.get_deposition(dep_id).get("metadata"))
    if got != expected:
        raise VerificationError(
            f"draft {dep_id} custom[{CODE_REPOSITORY_FIELD!r}] is {got!r}, expected {expected!r}: "
            "the deposit API did not keep the Software field; refusing to publish")
    print(f"  Software field verified on draft: PASS ({expected})")


def verify_draft_files(client, dep_id: int, pdf_name: str):
    """After upload / old-file removal: the draft must hold exactly the PDF this run uploaded."""
    names = sorted(str(f.get("filename")) for f in (client.get_deposition(dep_id).get("files") or []))
    if names != [pdf_name]:
        raise VerificationError(f"draft {dep_id} holds files {names}, expected exactly [{pdf_name!r}]: "
                                "refusing to publish")
    print(f"  Draft files verified: PASS ({pdf_name})")


def verify_published_code_repository(client, rec_id: int, expected: str | None, host: str):
    """Post-publication read-back with BOUNDED retries (a fresh record may be served stale)."""
    if not expected:
        return
    attempts, got = verify_attempts(), None
    for k in range(attempts):
        try:
            got = custom_repository(client.get_record(rec_id).get("metadata"))
        except Exception as exc:  # noqa: BLE001
            got = f"<unreadable: {redact(exc)}>"
        if _norm_url(got) == _norm_url(expected):
            print(f"  Software field on published record: PASS ({expected})")
            return
        if k < attempts - 1:
            _sleep(_verify_delay(k))
    print(f"  Software field on published record: FAIL after {attempts} attempt(s) "
          f"(got {got!r}, expected {expected!r})", file=sys.stderr)
    raise PostPublishFailure(
        f"\n!! RECORD {rec_id} IS PUBLISHED but its Software field is wrong or unverifiable.\n"
        f"!! Not un-publishing. Inspect https://{host}/record/{rec_id}; fix with a reviewed "
        "metadata-only edit (--update) or by hand in the Zenodo UI.")


def verify_upload_answer(answer, digest: dict):
    """Compare the checksum Zenodo returns for the uploaded file (``md5:<hex>``, or a bare md5/sha256 hex) with
    the local digest of the bytes sent. Raises VerificationError on a mismatch. Returns a note for the journal
    (None when the answer carries no checksum: that is UNPROVEN to happen, see README)."""
    if not isinstance(answer, dict) or not answer.get("checksum"):
        return None
    raw = str(answer["checksum"]).strip().lower()
    algo, _, value = raw.rpartition(":")
    if algo in ("", "md5") and len(value) == 32:
        want, name = digest["md5"], "md5"
    elif algo in ("", "sha256") and len(value) == 64:
        want, name = digest["sha256"], "sha256"
    else:
        return f"checksum returned in an unrecognised form ({redact(raw)}): not compared"
    if value != want:
        raise VerificationError(f"Zenodo returned {name} {value} for the uploaded file but the bytes sent have "
                                f"{name} {want}: the stored file is not the one sent; refusing to publish")
    return f"{name} {value} matches the bytes sent"


def deposit_update(client, metadata_built: dict, code_url: str, zenodo_id: int, env_label: str,
                   host: str, on_published=None, fields=(), confirm=None) -> tuple[int, str | None]:
    """--update: metadata-only edit in place. Returns (dep_id, doi).

    The PUT is the LIVE metadata with ONLY ``fields`` replaced. Before anything is created the whole-metadata
    unified diff live -> to-be-sent is printed; without ``confirm`` (None) the run stops there. ``confirm`` must
    be a hex prefix (at least 12 characters) of the sha256 of the diff printed in this run (the UTF-8 bytes of
    the printed diff text); an empty, short or non-matching value refuses."""
    fields = frozenset(fields)
    step = "resolve latest version"
    try:
        dep_id = client.resolve_latest_version_recid(int(zenodo_id))
    except BaseException as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if dep_id != int(zenodo_id):
        print(f"\nConcept record {zenodo_id} → latest version {dep_id} [{env_label}]")
    latest = refuse_if_unpublished_state(client, zenodo_id, dep_id, host)
    live = latest.get("metadata") if isinstance(latest, dict) else None
    if not isinstance(live, dict) or not live:
        die(f"Error: the live metadata of record {dep_id} cannot be read: refusing to build an update "
            "from anything else. Nothing was created.")
    try:
        payload = build_update_payload(live, metadata_built, fields, code_url)
    except ValueError as exc:
        die(f"Error: {exc}. Nothing was created.")
    diff, digest = metadata_diff(live, payload)
    if not diff:
        raise NothingToUpdate(f"the requested fields {sorted(fields)} already equal the live metadata of "
                              f"record {dep_id}")
    print(f"\n  Whole-metadata diff, live record {dep_id} -> to be sent (fields applied: {sorted(fields)}):")
    print(diff)
    print(f"  diff sha256: {digest}")
    if confirm is None:
        die("Diff printed above. Nothing was created. Read it, then re-run with "
            f"--yes-apply-diff {digest[:16]} (the sha256 prefix of THIS diff) to apply exactly it.")
    pin = str(confirm).strip().lower()
    if not CONFIRM_SHAPE.fullmatch(pin):
        die(f"Error: --yes-apply-diff needs the sha256 prefix of the diff you reviewed (at least "
            f"{CONFIRM_MIN_HEX} hex characters; a bare flag or a short value is not a review). This diff's "
            f"digest is {digest}. Nothing was created.")
    if not digest.startswith(pin):
        die(f"Error: --yes-apply-diff {confirm} does not match the diff of this run (sha256 {digest}). "
            "The live record may have changed. Nothing was created.")
    step = "edit"
    result, save_error, expected = None, None, None
    try:
        print(f"Editing metadata of record {dep_id} in place [{env_label}]…")
        client.edit_deposition(dep_id)
        step = "read live metadata"
        live_now = client.get_deposition(dep_id).get("metadata") or {}
        if normalised_metadata(live_now) != normalised_metadata(live):
            raise VerificationError("the live metadata changed between the printed diff and the edit "
                                    "session: refusing to send a PUT built from a stale read")
        expected = code_url if "code-url" in fields else (live.get("custom") or {}).get(CODE_REPOSITORY_FIELD)
        step = "metadata PUT"
        print("Setting metadata…")
        client.set_metadata(dep_id, payload)
        step = "verify draft metadata"
        verify_draft_code_repository(client, dep_id, expected)
        step = "publish"
        print("Publishing…")
        result = client.publish(dep_id)
    except BaseException as exc:  # noqa: BLE001
        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)
    else:
        if on_published:
            on_published(result)
    verify_published_code_repository(client, dep_id, expected, host)
    if save_error is not None:
        raise PostPublishFailure(f"\n!! local state not saved: {redact(save_error)}")
    return dep_id, result.get("doi")


def deposit_new(client, pdf_path: Path, metadata: dict, code_url: str, zenodo_id,
                env_label: str, host: str, on_published=None) -> dict:
    """New version (zenodo_id set) or first publication. Returns the publish response.

    on_published(result) runs the moment a publication is confirmed (publish returned, or a failed
    publish call was followed by a read showing state 'done'), BEFORE any verification or archiving."""
    step, latest_recid, dep_id, latest = "resolve latest version", None, None, None
    try:
        if zenodo_id:
            latest_recid = client.resolve_latest_version_recid(int(zenodo_id))
            if latest_recid != int(zenodo_id):
                print(f"\nConcept record {zenodo_id} → latest version {latest_recid} [{env_label}]")
    except BaseException as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if zenodo_id:
        latest = refuse_if_unpublished_state(client, zenodo_id, latest_recid, host)
        live_version = ((latest.get("metadata") or {}).get("version") if isinstance(latest, dict) else None)
        outgoing = metadata.get("version")
        if outgoing and live_version and str(outgoing).strip() == str(live_version).strip():
            die(f"Error: the version to publish ({outgoing!r}) equals the live version of record "
                f"{latest_recid}: a second identical version would be minted. Nothing was created.")
    else:
        snapshot_before_first_publication(client, host)
    if not code_url:
        print("  Note: no code_url for this paper: the Software field is neither set nor verified "
              "(a new-version draft may or may not inherit the previous version's repository).")
    result, save_error, recovered = None, None, False
    try:
        if zenodo_id:
            step = "newversion"
            print(f"New version of record {latest_recid} [{env_label}]…")
            dep = client.new_version(latest_recid)
            dep_id = dep["id"]
            step = "delete old files"
            for f in dep.get("files", []):
                client.delete_file(dep_id, f["id"])      # a failure is blocking
                print(f"  Removed old file: {f['filename']}")
        else:
            step = "create deposition"
            print(f"\nNew deposition [{env_label}]…")
            dep = client.create_deposition()
            dep_id = dep["id"]
        print(f"  Deposition ID: {dep_id}")
        step = "upload"
        try:
            pdf_bytes, digest = read_pdf_once(pdf_path)       # the PDF is read ONCE: digest, upload, archive
        except OSError as exc:
            raise VerificationError(f"cannot read the PDF {pdf_path} ({redact(exc)}): without a recorded "
                                    "digest nothing is uploaded") from exc
        client.uploaded_pdf, client.uploaded_pdf_bytes = digest, pdf_bytes
        j = journal_of(client)
        if j is not None:
            j.update(pdf=digest, phase="upload about to be sent", deposition_id=dep_id)
        print(f"Uploading {Path(pdf_path).name} …")
        answer = client.upload_file(dep, pdf_path, expected_sha256=digest["sha256"], data=pdf_bytes)
        note = verify_upload_answer(answer, digest)
        if j is not None and note:
            j.update(upload_checksum=note)
        step = "verify draft files"
        verify_draft_files(client, dep_id, Path(pdf_path).name)
        step = "metadata PUT"
        print("Setting metadata…")
        client.set_metadata(dep_id, metadata)
        step = "verify draft metadata"
        verify_draft_code_repository(client, dep_id, code_url)
        step = "publish"
        print("Publishing…")
        result = client.publish(dep_id)
    except BaseException as exc:  # noqa: BLE001
        result, save_error = abort_and_cleanup(client, step, exc, host, on_published)
        recovered = True
    if not recovered and on_published:
        try:
            on_published(result)
        except BaseException as exc:  # noqa: BLE001 - Ctrl-C right after publish returns (R12) included
            save_error = exc
            print(f"!! PUBLISHED but LOCAL STATE NOT SAVED ({type(exc).__name__}: {redact(exc)}). "
                  + _manual_state_hint(host == SANDBOX_HOST, client) + " DO NOT re-run the deposit: it would "
                  "publish twice. " + do_not_rerun_notice(client, host, (result or {}).get("id", dep_id)),
                  file=sys.stderr)
            if not isinstance(exc, Exception):
                raise
    verify_published_code_repository(client, result.get("id", dep_id), code_url, host)
    if save_error is not None:
        raise PostPublishFailure(f"\n!! local state not saved: {redact(save_error)}")
    return result


# ── Local state / sandbox ──────────────────────────────────────────────────────

class SandboxStateSlot:
    """The sandbox result file. Reserved (a unique temp name next to it is created) BEFORE any request; the
    result is committed with os.replace of that temp file, and never over an existing file."""

    def __init__(self, path: Path):
        self.path = path
        self.tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")

    def reserve(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.tmp, "x", encoding="utf-8") as fh:     # writability probe; no deletion needed
            fh.write("")

    def commit(self, text: str) -> None:
        """Write the reserved temp and hard-link it to the final name: os.link fails with FileExistsError if the
        name exists (no exists-then-replace window), so an existing file is never overwritten."""
        with open(self.tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            _fsync_file(fh)
        try:
            os.link(self.tmp, self.path)
        except FileExistsError as exc:
            raise FileExistsError(f"{self.path} appeared in the meantime: refusing to overwrite it") from exc
        os.unlink(self.tmp)                          # the reserved temp this slot created itself
        _fsync_dir(self.path.parent)


def record_sandbox_state(slot, host: str, paper: str, version, result: dict) -> bool:
    """A sandbox run records its result HERE and nowhere else (never zenodo.json, pdfs/, bibliographies).

    The full result is ALWAYS printed; if the file cannot be written the message says so and never
    suggests touching a production file. Returns True only when the state file was committed."""
    if isinstance(slot, Path):
        slot = SandboxStateSlot(slot)
    data = {
        "sandbox": True, "host": host, "paper": paper, "version": version,
        "record_id": result.get("id"), "doi": result.get("doi"),
        "conceptrecid": result.get("conceptrecid") or (result.get("metadata") or {}).get("conceptrecid"),
        "conceptdoi": result.get("conceptdoi") or (result.get("metadata") or {}).get("conceptdoi"),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    text = json.dumps(data, indent=2, ensure_ascii=False)
    print(text)
    try:
        slot.commit(text + "\n")
    except OSError as exc:
        print(f"!! [sandbox] the state file {slot.path} could NOT be written ({redact(exc)}). The full result "
              "is printed above and is in the run journal. This is a SANDBOX run: do NOT edit any production "
              "zenodo.json.", file=sys.stderr)
        return False
    print(f"  [sandbox] result recorded in {slot.path} (production zenodo.json, pdfs/ and bibliographies untouched)")
    return True


def validate_state_dir(path: Path, repo_root: Path, paper_dir: Path) -> Path:
    """The run-journal directory must live outside the workspace (a repository could commit it)."""
    p = path.expanduser().resolve()
    for root in (repo_root.resolve(), paper_dir.resolve()):
        if p == root or root in p.parents:
            die(f"Error: --state-dir {p} lies inside {root}: a repository could commit the run journals. "
                "Use a directory outside the workspace (default: the user cache directory).")
    if p.exists() and not p.is_dir():
        die(f"Error: --state-dir {p} is not a directory")
    return p


def validate_sandbox_state_path(path: Path, repo_root: Path, paper_dir: Path) -> Path:
    """The sandbox state file must live outside the workspace and must not exist yet."""
    p = path.expanduser().resolve()
    for root in (repo_root.resolve(), paper_dir.resolve()):
        if p == root or root in p.parents:
            sys.exit(f"Error: --sandbox-state {p} lies inside {root}: a repository could commit it. "
                     "Use a path under the OS temp/scratch directory.")
    if p.is_dir():
        sys.exit(f"Error: --sandbox-state {p} is a directory")
    if p.exists():
        sys.exit(f"Error: --sandbox-state {p} already exists: refusing to overwrite it. Choose a new path.")
    return p


def run_cascade(repo_root: Path, config: dict, concept_doi: str, no_cascade: bool) -> None:
    if no_cascade:
        return
    bib_key = config.get("bib_key")
    if bib_key:
        print(f"\nDOI cascade ({bib_key} → {concept_doi})…")
        doi_cascade(repo_root, bib_key, concept_doi)
    else:
        print("\nNo bib_key in zenodo.json — skipping DOI cascade.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def _rel(path: Path, root: Path) -> str:
    """Path relative to the workspace when it lies inside it (a pilot may use a scratch copy outside)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a Cosmochrony paper to Zenodo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paper_dir", type=Path, nargs="?", help="Paper directory (e.g. bell-paper/)")
    parser.add_argument("--sandbox", action="store_true",
                        help="Use sandbox.zenodo.org (reads ONLY ZENODO_SANDBOX_TOKEN; needs --sandbox-state; "
                             "touches no production file)")
    parser.add_argument("--sandbox-state", type=Path, metavar="PATH",
                        help="With --sandbox: result file (outside the workspace, must not exist; an early "
                             "writability probe runs before any request)")
    parser.add_argument("--sandbox-zenodo-id", type=int, metavar="ID",
                        help="With --sandbox: sandbox concept/record id for a new version or --update "
                             "(the production zenodo_id is never used on the sandbox)")
    parser.add_argument("--state-dir", type=Path, metavar="DIR",
                        help="Directory of the durable run journals (<paper>-<ts>.run.json), OUTSIDE the "
                             "workspace. Default: $XDG_CACHE_HOME or ~/.cache, then cosmochrony-zenodo/")
    parser.add_argument("--dry-run", action="store_true", help="Parse LaTeX only, do not upload (offline)")
    parser.add_argument("--update", action="store_true",
                        help="Metadata-only edit of the published record (same DOI, PDF unchanged). The LIVE "
                             "metadata is preserved: only the fields named by --set-*/--apply-local-metadata "
                             "are replaced, the whole-metadata diff is printed first and needs --yes-apply-diff")
    for field in UPDATABLE_FIELDS:
        parser.add_argument(f"--set-{field}", action="store_true", dest="set_" + field.replace("-", "_"),
                            help=f"--update: replace the live {UPDATABLE_FIELDS[field]!r} with the local value")
    parser.add_argument("--apply-local-metadata", metavar="F1,F2", default="",
                        help="--update: comma list of fields to take from local state "
                             f"({', '.join(UPDATABLE_FIELDS)}); equivalent to the --set-* flags")
    parser.add_argument("--yes-apply-diff", nargs="?", const="", default=None, metavar="DIGEST",
                        help="--update: apply the printed diff. REQUIRES the sha256 prefix (at least 12 hex "
                             "characters) of the diff you reviewed, printed as 'diff sha256: ...'; a bare flag or "
                             "a shorter value is refused (breaking change of the third corrective pass)")
    parser.add_argument("--live-snapshot", type=Path, metavar="PATH",
                        help="--update --dry-run: JSON file (a deposition or its metadata) used as the live "
                             "record so the diff can be shown offline")
    parser.add_argument("--no-cascade", action="store_true", help="Skip DOI cascade after publish")
    parser.add_argument("--finish-after-fail", action="store_true",
                        help="Offline: after a post-publish FAIL (record published, zenodo.json saved), run the "
                             "skipped PDF archive and DOI cascade. Bound to ONE deposit journal (the newest "
                             "production deposit journal of the paper that carries a PDF digest, or --journal): "
                             "the archive label is that journal's version, the PDF must have its sha256, a "
                             "sandbox journal or a concept mismatch is refused, an archive is never overwritten, "
                             "the cascade runs only after the archive step succeeded; no network, no deposit")
    parser.add_argument("--journal", type=Path, metavar="PATH",
                        help="--finish-after-fail: the deposit journal to finish (default: the newest one)")
    parser.add_argument("--expect-pdf-sha256", metavar="HEX",
                        help="--finish-after-fail: digest of the uploaded PDF when no run journal records it")
    parser.add_argument("--reconcile", type=Path, metavar="JOURNAL",
                        help="Record, locally, what you found on the server for a blocking journal "
                             "(needs --outcome, or --quarantine for an unreadable one); no network. Writes only "
                             "the paper directory recorded IN THE JOURNAL")
    parser.add_argument("--outcome", choices=("published", "not-published"),
                        help="--reconcile: what you verified on the server")
    parser.add_argument("--record-id", type=int, metavar="N", help="--reconcile --outcome published: record id")
    parser.add_argument("--concept-id", metavar="C",
                        help="--reconcile published (production deposit): concept recid, written to zenodo.json")
    parser.add_argument("--concept-doi", metavar="D",
                        help="--reconcile published (production deposit): concept DOI, written to zenodo.json")
    parser.add_argument("--record-url", metavar="URL",
                        help="--reconcile not-published: the record/draft page you inspected")
    parser.add_argument("--i-checked-the-account", action="store_true",
                        help="--reconcile not-published without --record-url: you checked the account yourself "
                             "(needs --reason)")
    parser.add_argument("--reason", metavar="TEXT",
                        help="--reconcile: free text stored in the journal (needed with "
                             "--i-checked-the-account and --quarantine)")
    parser.add_argument("--quarantine", action="store_true",
                        help="--reconcile JOURNAL --quarantine --reason TEXT: rename an UNREADABLE journal to "
                             "*.quarantined in its directory (prints its size and path; nothing is deleted)")
    parser.add_argument("--skip-checks", action="store_true",
                        help="Bypass the pre-deposit check battery only (never the version-label guard, "
                             "the journal block or the diff confirmation)")
    return parser


def requested_fields(args, parser) -> set:
    fields = {f for f in UPDATABLE_FIELDS if getattr(args, "set_" + f.replace("-", "_"))}
    for name in (n.strip() for n in args.apply_local_metadata.split(",") if n.strip()):
        if name not in UPDATABLE_FIELDS:
            parser.error(f"--apply-local-metadata: unknown field {name!r} (known: {', '.join(UPDATABLE_FIELDS)})")
        fields.add(name)
    return fields


def run_reconcile(args, parser) -> None:
    path = args.reconcile.expanduser().resolve()
    if args.quarantine:
        try:
            target = quarantine_journal(path, args.reason or "")
        except ReconcileRefused as exc:
            die(f"Error: {exc}")
        print(f"Quarantined: {path.name} -> {target.name} (same directory; nothing deleted). The journal no "
              "longer blocks: make sure no draft or edit session is left at https://zenodo.org/deposit and that "
              "the record is not published before the next run.")
        return
    if not args.outcome:
        parser.error("--reconcile needs --outcome published|not-published (or --quarantine for an unreadable journal)")
    try:
        data = reconcile_journal(path, args.outcome, args.record_id, args.concept_id, args.concept_doi,
                                 record_url=args.record_url, checked_account=args.i_checked_the_account,
                                 reason=args.reason, paper_dir_arg=args.paper_dir)
    except ReconcileRefused as exc:
        die(f"Error: {exc}")
    host = data.get("host") or PRODUCTION_HOST
    print(f"Journal {path.name} reconciled locally: outcome {data['outcome']} (operator-asserted, nothing "
          "was checked or sent by this tool).")
    if args.outcome == "not-published":
        print("Consequences: this journal no longer blocks. The next run for this paper MAY CREATE A NEW "
              "DEPOSITION. If a publication did happen, it will be published a SECOND time and nothing in this "
              "tool can detect it. Make sure no draft or edit session is left at "
              f"https://{host}/deposit. Evidence stored in the journal: {data.get('operator_evidence')}")
        return
    if data.get("sandbox"):
        print("Sandbox journal: no production file is touched.")
    elif data.get("operator_evidence", {}).get("config_written"):
        print(f"Saved zenodo_id {data.get('concept_id')} and doi {data.get('concept_doi')} (operator-asserted) -> "
              f"{data['operator_evidence']['config_written']} (the paper directory recorded in the journal)")
    else:
        print("--update journal: there is no local state to save.")


def finalize_journal(journal, client=None) -> None:
    """Never leave RUNNING behind: no write was sent -> NOT-PUBLISHED; otherwise UNRECONCILED."""
    if journal is None or journal.outcome() != "RUNNING":
        return
    if int(journal.data.get("writes_sent") or 0) == 0 and not (client is not None and client.created_state):
        journal.update(outcome="NOT-PUBLISHED", phase="exited", note="exited before any write request")
    else:
        journal.update(outcome="UNRECONCILED", phase="exited",
                       note="exited after a write request without a settled outcome")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    register_environment_secrets()        # the exact tokens loaded in this process are masked everywhere

    if not args.reconcile and (args.outcome or args.quarantine or args.record_url or args.i_checked_the_account
                               or args.reason or args.record_id or args.concept_id or args.concept_doi):
        parser.error("--outcome/--quarantine/--record-id/--concept-id/--concept-doi/--record-url/"
                     "--i-checked-the-account/--reason only make sense with --reconcile")
    if args.journal and not args.finish_after_fail:
        parser.error("--journal only makes sense with --finish-after-fail")
    if args.reconcile:
        run_reconcile(args, parser)
        return
    if args.paper_dir is None:
        parser.error("paper_dir is required")
    fields = requested_fields(args, parser)
    if fields and not args.update:
        parser.error("--set-*/--apply-local-metadata only make sense with --update")
    if args.finish_after_fail and (args.sandbox or args.update or args.dry_run):
        parser.error("--finish-after-fail is a production-only, offline step: no --sandbox/--update/--dry-run")
    if args.update and not fields and not args.dry_run:
        parser.error("--update preserves the live metadata and changes nothing by itself: name the fields to "
                     "apply (--set-version, --set-title, --set-abstract, --set-keywords, --set-notes, "
                     "--set-web-url, --set-publication-type, --set-code-url, or --apply-local-metadata=...)")
    networked = not (args.dry_run or args.finish_after_fail)
    if args.sandbox and networked and not args.sandbox_state:
        parser.error("--sandbox requires --sandbox-state PATH (a file outside the workspace)")

    # ── Auth first: refuse to start without the right token, before anything else ─────────────
    token = resolve_token(args.sandbox) if networked else None
    if networked:
        validate_numeric_env()        # every tunable, before any request

    paper_dir = args.paper_dir.resolve()
    if not paper_dir.is_dir():
        sys.exit(f"Error: {paper_dir} is not a directory")

    config_path = paper_dir / "zenodo.json"
    if not config_path.exists():
        sys.exit(
            f"Error: {config_path} not found.\n"
            "Create it from tools/zenodo.example.json and fill in bib_key / web_url."
        )

    config = json.loads(config_path.read_text(encoding="utf-8"))

    # A retired paper has no live deposit: its content was absorbed elsewhere and its own record was
    # withdrawn. Its zenodo_id is therefore null, which would otherwise make this script create a
    # spurious NEW deposition for material that is already published under another DOI.
    if config.get("retired"):
        superseded = config.get("superseded_by")
        sys.exit(
            f"Error: {paper_dir.name} is marked retired in zenodo.json and must not be deposited.\n"
            + (f"Reason: {config['retired_reason']}\n" if config.get("retired_reason") else "")
            + (f"Cite {superseded} instead.\n" if superseded else "")
        )

    # Anchor the repo root to this script's location, not paper_dir.parent, so the
    # central pdfs/ archive and the DOI cascade work for nested paper directories
    # (e.g. condensed-matter/quantum-fisher-witness/) as well as top-level ones.
    repo_root = REPO_ROOT
    host = SANDBOX_HOST if args.sandbox else PRODUCTION_HOST
    zenodo_id = args.sandbox_zenodo_id if args.sandbox else config.get("zenodo_id")

    state_dir = None
    if networked or args.finish_after_fail:
        state_dir = validate_state_dir(args.state_dir or default_state_dir(), repo_root, paper_dir)
    identity = journal_identity(config)
    if networked:
        blocking = find_blocking_journals(state_dir, paper_dir, [zenodo_id], host, identity)
        if blocking:
            die(blocking_message(blocking))

    sandbox_slot = None
    if args.sandbox and networked:
        sandbox_slot = SandboxStateSlot(validate_sandbox_state_path(args.sandbox_state, repo_root, paper_dir))
        try:
            sandbox_slot.reserve()       # writability probe BEFORE any request
        except OSError as exc:
            die(f"Error: --sandbox-state {sandbox_slot.path} cannot be created ({redact(exc)}). "
                "Nothing was sent.")

    # ── Parse LaTeX ────────────────────────────────────────────────────────────
    tex_path = find_main_tex(paper_dir, config)
    print(f"Parsing  {_rel(tex_path, repo_root)}")

    raw = resolve_inputs(
        tex_path.read_text(encoding="utf-8"),
        tex_path.parent,
    )

    macros = extract_macros(raw)
    if macros:
        # Expand only in the document body to avoid corrupting preamble definitions
        doc_start = raw.find(r"\begin{document}")
        if doc_start >= 0:
            raw = raw[:doc_start] + expand_macros(raw[doc_start:], macros)

    title = extract_title(raw, macros)
    identity = journal_identity(config, title)
    if networked:
        blocking = find_blocking_journals(state_dir, paper_dir, [zenodo_id], host, identity)
        if blocking:
            die(blocking_message(blocking))
    abstract = extract_abstract(raw)
    keywords = extract_keywords(raw)
    # Version label: prefer the explicit semantic version in zenodo.json ("version": "1.1");
    # fall back to the git branch only for legacy configs that lack it.
    version = config.get("version") or get_git_branch(paper_dir)
    code_url = resolve_code_url(paper_dir, config)

    print(f"  Title:    {title}")
    print(f"  Abstract: {abstract[:90].replace(chr(10), ' ')}…")
    print(f"  Keywords: {keywords}")
    print(f"  Version:  {version or '(none)'}")
    if config.get("notes"):
        print(f"  Notes:    {config['notes']}")
    if code_url:
        print(f"  Code:     {code_url}")

    # ── Version-label guard: independent of the pre-deposit gate and of --skip-checks ───────────
    if not args.finish_after_fail and not (args.update and "version" not in fields):
        try:
            for note in check_version_label(version, paper_dir, config):
                print(f"  -- {note}")
            print(f"  Version label check: PASS ({version})")
        except VersionLabelError as exc:
            if args.dry_run:
                print(f"  Version label check: REFUSED, a real run would stop: {exc}")
            else:
                die(f"Error: version label refused: {exc}. Nothing was sent.")

    if args.dry_run:
        # Show the exact metadata a deposit would send (new-version / first-publication path).
        dry_meta = build_metadata(title, abstract, keywords, config.get("web_url", ""), version,
                                  publication_type=config.get("publication_type"),
                                  notes=config.get("notes"),
                                  code_url=code_url)
        print("\n  Metadata that would be sent (new version / first publication):")
        print(json.dumps(dry_meta, indent=2, ensure_ascii=False))
        if args.update:
            print(f"\n  --update would apply ONLY these fields over the live record: {sorted(fields) or '(none)'}")
            if args.live_snapshot:
                snap = json.loads(args.live_snapshot.read_text(encoding="utf-8"))
                live = snap.get("metadata") if isinstance(snap.get("metadata"), dict) else snap
                try:
                    payload = build_update_payload(live, dry_meta, fields, code_url)
                    diff, digest = metadata_diff(live, payload)
                    print("  Whole-metadata diff, live snapshot -> to be sent:")
                    print(diff or "  (no difference)")
                    print(f"  diff sha256: {digest}")
                except ValueError as exc:
                    print(f"  Cannot build the update payload: {exc}")
            else:
                print("  (no --live-snapshot: the diff against the live record cannot be shown offline)")
        print("\n[Dry run] Stopping here — no upload performed.")
        return

    # ── --finish-after-fail: the local steps a post-publish FAIL skipped (offline) ─────────────
    # Bound to ONE deposit journal (paper, version, PDF sha256), never to the current configuration.
    if args.finish_after_fail:
        zid, concept_doi = config.get("zenodo_id"), config.get("doi")
        if not zid or not concept_doi:
            sys.exit("Error: zenodo.json has no zenodo_id/doi: the publication was not saved locally, "
                     "so there is nothing to finish. Reconcile the journal (it says PUBLISHED-UNSAVED) or set "
                     "them from the record first.")
        pdf_path = find_pdf(paper_dir)
        print(f"  PDF:      {_rel(pdf_path, repo_root)}  (must be the PDF that was uploaded)")
        try:
            jpath, jdata = select_deposit_journal(state_dir, paper_dir,
                                                  args.journal.expanduser().resolve() if args.journal else None)
        except ReconcileRefused as exc:
            die(f"Error: {exc}. Nothing was archived.")
        jdata = jdata or {}
        label = version
        if jpath is not None:
            print(f"  Journal:  {jpath}  (deposit of version {jdata.get('version')!r})")
            if jdata.get("outcome") == UNSAVED_OUTCOME:
                die(f"Error: the journal {jpath.name} is still {UNSAVED_OUTCOME}: reconcile it first "
                    "(--reconcile ... --outcome published ...). Nothing was archived.")
            if jdata.get("concept_id") not in (None, "") and str(jdata["concept_id"]) != str(zid):
                die(f"Error: the journal {jpath.name} records concept {jdata['concept_id']} but zenodo.json "
                    f"says {zid}. Nothing was archived.")
            label = jdata.get("version")
            if not label:
                die(f"Error: the journal {jpath.name} records no version: the archive label cannot be derived. "
                    "Nothing was archived.")
        try:
            check_version_label(label, paper_dir, config)
        except VersionLabelError as exc:
            die(f"Error: version label refused for the archive: {exc}. --finish-after-fail must run with the "
                "paper at the DEPOSITED version (its branch, zenodo.json and CITATION.cff). Nothing was archived.")
        recorded = (jdata.get("pdf") or {}).get("sha256")
        given = args.expect_pdf_sha256.strip().lower() if args.expect_pdf_sha256 else None
        if recorded and given and recorded != given:
            die(f"Error: --expect-pdf-sha256 {given} disagrees with the digest {recorded} recorded at upload "
                "time. Nothing was archived.")
        expected_sha = recorded or given
        if not expected_sha:
            die(f"Error: no digest of the uploaded PDF is known (no published production deposit journal with a "
                f"PDF digest for {paper_dir.name} in {state_dir}). Pass --expect-pdf-sha256 HEX if you hold it. "
                "Nothing was archived.")
        pdf_bytes, local = read_pdf_once(pdf_path)
        for rec in jdata.get("published_files") or []:
            checksum = str(rec.get("checksum") or "").split(":")[-1].lower()
            if rec.get("filename") == pdf_path.name and checksum and checksum != local["md5"]:
                die(f"Error: the checksum Zenodo returned for {pdf_path.name} ({checksum}) differs from the "
                    f"local file's md5 ({local['md5']}). Nothing was archived.")
        try:
            archived = archive_pdf(repo_root, pdf_path, tex_path.stem, label, zid, expected_sha256=expected_sha,
                                   data=pdf_bytes)
        except (ArchiveRefused, OSError) as exc:
            die(f"Error: archive refused: {redact(exc)}. The DOI cascade was NOT run.")
        else:
            if archived.status in ("archived", "identical"):
                run_cascade(repo_root, config, concept_doi, args.no_cascade)
        return

    # ── Pre-deposit gate (fails CLOSED) ────────────────────────────────────────
    # A published Zenodo record cannot be deleted, so refuse to upload an artefact the battery rejects, and
    # refuse as well when the battery itself cannot run. --skip-checks bypasses only this battery.
    if not args.skip_checks:
        try:
            from predeposit_check import check as _predeposit_check
            result = _predeposit_check(paper_dir, deposit=True)
            problems = result["problems"]
        except Exception as exc:  # noqa: BLE001
            die(f"Error: the pre-deposit checks could not run ({type(exc).__name__}: {redact(exc)}). "
                "Refusing to deposit (fail closed). Fix the checker, or --skip-checks if you have judged "
                "the battery not to apply.")
        for note in result["notes"]:
            print(f"  -- {note}")
        if problems:
            print("\nPre-deposit checks FAILED:")
            for prob in problems:
                print(f"  !! {prob}")
            sys.exit(
                "\nRefusing to deposit. Fix the above, or re-run with --skip-checks "
                "if you have judged them not to apply."
            )
        print("  Pre-deposit checks: PASS")

    # ── Find PDF ───────────────────────────────────────────────────────────────
    pdf_path = find_pdf(paper_dir)
    print(f"  PDF:      {_rel(pdf_path, repo_root)}")

    try:
        client = ZenodoClient(token, sandbox=args.sandbox)
    except (TokenHostMismatch, InvalidToken) as exc:
        die(f"Error: {exc}. Nothing was sent.")
    env_label = "sandbox" if args.sandbox else "production"

    metadata = build_metadata(title, abstract, keywords, config.get("web_url", ""), version,
                              publication_type=config.get("publication_type"),
                              notes=config.get("notes"),
                              code_url=code_url)

    try:
        journal = Journal.create(
            state_dir, paper_dir, host, args.sandbox,
            {"update": args.update, "fields": sorted(fields), "sandbox": args.sandbox,
             "zenodo_id": zenodo_id, "no_cascade": args.no_cascade}, version, zenodo_id, identity)
    except (OSError, JournalError) as exc:
        die(f"Error: the run journal cannot be created in {state_dir} ({redact(exc)}). Nothing was sent.")
    client.journal = journal
    print(f"  Journal:  {journal.path}")

    published: dict = {}

    def save_local_state(result: dict):
        # Runs right after publish is confirmed and BEFORE any verification or archiving, so a later
        # failure cannot make a re-run create a duplicate deposition.
        if args.sandbox:
            raise AssertionError("production state must never be written by a sandbox run")
        doi = result.get("doi")
        concept_doi = result.get("conceptdoi") or result.get("metadata", {}).get("conceptdoi")
        concept_recid = result.get("conceptrecid") or result.get("metadata", {}).get("conceptrecid")
        print(f"\nPublished!")
        print(f"  Version DOI:   {doi} (ephemeral, not stored)")
        print(f"  Concept DOI:   {concept_doi}")
        print(f"  Concept recid: {concept_recid}")
        print(f"  Record: https://{host}/record/{result.get('id')}")
        if journal.outcome() not in (UNSAVED_OUTCOME, "PUBLISHED"):
            journal.mark_published(result.get("id"), result)         # blocking until the file is saved
        if not concept_doi or not concept_recid:
            raise RuntimeError(
                "Zenodo response did not include conceptdoi/conceptrecid; refusing to overwrite "
                "zenodo.json with version-specific identifiers. Inspect the publish response and "
                "update manually."
            )
        config["zenodo_id"] = int(concept_recid)
        config["doi"] = concept_doi
        _atomic_write_text(config_path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
        saved = json.loads(config_path.read_text(encoding="utf-8"))       # read back before lifting the block
        if str(saved.get("zenodo_id")) != str(int(concept_recid)) or saved.get("doi") != concept_doi:
            raise RuntimeError(f"{config_path} does not hold zenodo_id/doi after writing: check it by hand")
        print(f"  Saved zenodo_id + concept DOI → {_rel(config_path, repo_root)}")
        published["concept_doi"] = concept_doi
        journal.mark_saved(config_sha256_after=config_fingerprint(saved))

    def save_sandbox_state(result: dict):
        print("\nPublished on the SANDBOX!")
        if journal.outcome() not in (UNSAVED_OUTCOME, "PUBLISHED"):
            journal.mark_published(result.get("id"), result)
        if record_sandbox_state(sandbox_slot, host, paper_dir.name, version, result):
            journal.mark_saved()
        else:
            print(f"!! [sandbox] the journal stays {UNSAVED_OUTCOME} (it holds the result) and blocks later runs "
                  "of this paper until you --reconcile it.", file=sys.stderr)

    def settle_update(result: dict):
        # An --update has no local state to save: the publication is settled the moment it is confirmed.
        if journal.outcome() not in (UNSAVED_OUTCOME, "PUBLISHED"):
            journal.mark_published(result.get("id"), result)
        journal.mark_saved()

    saver = save_sandbox_state if args.sandbox else save_local_state

    try:
        # ── --update: metadata-only edit in place via legacy deposit API (same DOI) ─
        # Note: file replacement in place is not possible via the Zenodo REST API;
        # the file bucket remains locked for published records. Use new version for files.
        if args.update:
            if not zenodo_id:
                sys.exit("Error: --update needs an existing record: zenodo_id in zenodo.json"
                         + (" (on the sandbox: --sandbox-zenodo-id)" if args.sandbox else "") + ".")
            try:
                dep_id, doi = deposit_update(client, metadata, code_url, int(zenodo_id), env_label, host,
                                             on_published=save_sandbox_state if args.sandbox else settle_update,
                                             fields=fields, confirm=args.yes_apply_diff)
            except NothingToUpdate as exc:
                print(f"Nothing to update: {exc}.")
                finalize_journal(journal, client)
                return
            except PostPublishFailure as exc:
                sys.exit(str(exc))
            doi = doi or config.get("doi", "")
            print(f"\nUpdated! (metadata only — files unchanged)")
            print(f"  DOI:    {doi}")
            print(f"  Record: https://{host}/record/{dep_id}")
            finalize_journal(journal, client)
            return

        # ── New version or first publication via legacy deposit API ────────────────
        try:
            result = deposit_new(client, pdf_path, metadata, code_url, zenodo_id, env_label, host,
                                 on_published=saver)
        except PostPublishFailure as exc:
            if journal.outcome() == UNSAVED_OUTCOME:
                sys.exit(str(exc) + f"\n!! The publication SUCCEEDED but the local state is not saved: the journal "
                         f"({journal.path}) says {UNSAVED_OUTCOME} and blocks every re-run. "
                         + _manual_state_hint(args.sandbox, client))
            sys.exit(str(exc) + "\n!! PDF archive and DOI cascade were SKIPPED (a sandbox run skips them "
                     "always). Once the record is fixed, run them with --finish-after-fail.")
        if args.sandbox:
            print("\n[sandbox] PDF archive and DOI cascade skipped by contract: no production file was written.")
            finalize_journal(journal, client)
            return
        concept_doi = published["concept_doi"]

        # Archive the published PDF into the central pdfs/ store (systematic, every deposit), never over
        # an existing file and only if it is the PDF that was uploaded.
        uploaded = getattr(client, "uploaded_pdf", None)
        if not uploaded:
            die("!! PUBLISHED, but the digest of the uploaded PDF is unknown: the archive was SKIPPED. "
                "Run --finish-after-fail --expect-pdf-sha256 HEX once you know it.")
        try:
            # The bytes that were digested and uploaded are the bytes archived (the PDF is not re-read).
            archived = archive_pdf(repo_root, pdf_path, tex_path.stem, version, result["id"],
                                   expected_sha256=uploaded["sha256"],
                                   data=getattr(client, "uploaded_pdf_bytes", None))
        except (ArchiveRefused, OSError) as exc:
            journal.update(phase="archive refused", note=f"archive refused: {exc}")
            die(f"!! PUBLISHED (journal: PUBLISHED, zenodo.json saved) but the PDF archive failed or was "
                f"REFUSED: {redact(exc)}\n"
                "!! The DOI cascade was NOT run. Resolve it, then run --finish-after-fail.")
        else:
            if archived.status in ("archived", "identical"):
                run_cascade(repo_root, config, concept_doi, args.no_cascade)
        journal.update(phase="complete")
        finalize_journal(journal, client)
    except BaseException:
        finalize_journal(journal, client)
        raise


if __name__ == "__main__":
    sys.excepthook = _redacting_excepthook
    main()
