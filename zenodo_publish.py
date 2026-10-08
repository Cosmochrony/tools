#!/usr/bin/env python3
"""zenodo_publish.py — Automated Zenodo publication for Cosmochrony papers.

Usage (production):
    export ZENODO_TOKEN=...            # the script reads the PROCESS ENVIRONMENT only; it does NOT load
                                       # tools/.env itself (export it first, e.g. `set -a; . tools/.env; set +a`)
    python tools/zenodo_publish.py bell-paper/ [--update] [--no-cascade] [--dry-run]

Sandbox (never touches a production id, DOI, pdfs/ or bibliography; see tools/README.md):
    ZENODO_SANDBOX_TOKEN=... python tools/zenodo_publish.py PAPER/ --sandbox --sandbox-state /tmp/state.json
    (--sandbox reads ONLY ZENODO_SANDBOX_TOKEN, with no fallback to ZENODO_TOKEN; the production token is
     never sent to the sandbox host, nor the sandbox token to production.)

Guards (all offline-tested): a run REFUSES when a draft/edit session of the concept already exists, and on
any failure cleans up ONLY the server state this run certainly created (an uncertain outcome is reported
and searched for, never assumed away; ambiguity discards nothing); every request has bounded timeouts, only
GETs are retried; the Software field (code:codeRepository) is verified on the draft before publishing and on
the public record afterwards; zenodo.json is saved the moment a publication is confirmed. After a
post-publish FAIL, `--finish-after-fail` runs the skipped PDF archive and DOI cascade offline.

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
import json
import os
import re
import datetime
import shutil
import subprocess
import sys
import time
from pathlib import Path
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
    raw = (environ if environ is not None else os.environ).get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        sys.exit(f"Error: environment variable {name} must be a number, got {raw!r}")


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
    return token


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
        if not token or (other and token == other):
            raise TokenHostMismatch(
                f"refusing to build a {'sandbox' if sandbox else 'production'} client: the token is "
                f"empty or equals {other_var}, i.e. it belongs to the other Zenodo host")
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
            print(f"  HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
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
            headers = resp.headers if isinstance(resp.headers, dict) else {}
            for key in ("X-Total", "X-Total-Count"):
                if headers.get(key) not in (None, ""):
                    try:
                        total = int(headers[key])
                    except ValueError:
                        pass
            links = resp.links if isinstance(resp.links, dict) else {}
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

    def discard(self, dep_id: int):
        """Documented actions/discard: deletes a draft, or ends an edit session without publishing."""
        return self._request("POST", f"{self.base}/deposit/depositions/{dep_id}/actions/discard")

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

    def upload_file(self, deposition: dict, file_path: Path) -> dict:
        """Upload via S3 bucket API (new depositions and new versions). A failure raises."""
        bucket_url = deposition["links"]["bucket"]
        with open(file_path, "rb") as fh:
            return self._ok(self._request(
                "PUT", f"{bucket_url}/{file_path.name}", data=fh,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self.upload_timeout))

    def set_metadata(self, dep_id: int, metadata: dict) -> dict:
        return self._ok(self._request(
            "PUT", f"{self.base}/deposit/depositions/{dep_id}", json={"metadata": metadata}))

    def publish(self, dep_id: int) -> dict:
        return self._ok(self._request(
            "POST", f"{self.base}/deposit/depositions/{dep_id}/actions/publish"))


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


def archive_pdf(repo_root: Path, pdf_path: Path, name_stem: str, version: str | None, rec_id) -> Path:
    """Copy the just-published PDF into the central <root>/pdfs/ archive.

    The archive name follows the repository convention <MainTexBasename>_<version>.pdf
    (e.g. program_1.69.pdf). Falls back to the Zenodo record id when no git-branch
    version label is available."""
    label = version or str(rec_id)
    dest_dir = repo_root / "pdfs"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{name_stem}_{label}.pdf"
    shutil.copy2(pdf_path, dest)
    print(f"  Archived PDF → {dest.relative_to(repo_root)}")
    return dest


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
            problems.append(f"record {latest_id} advertises a latest_draft: {link}")
    except Exception as exc:  # noqa: BLE001 - fail closed: cannot prove there is no draft
        sys.exit(f"Error: cannot determine whether a draft already exists ({exc}). "
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


def snapshot_before_first_publication(client, host: str):
    """First publication has no concept to guard, but the pre-run draft ids are still needed to tell
    this run's draft from anyone else's if its creation request ends in doubt."""
    try:
        client.pre_run_draft_ids = {d.get("id") for d in client.list_drafts()}
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Error: cannot snapshot the existing drafts ({exc}); without it an uncertain creation "
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
            return "unknown", None, [rid], f"cannot read record {rid} ({exc})"
        if dep.get("state") == "done":
            return "none", None, [], f"record {rid} is in state 'done': no edit session exists"
        return "own", rid, [], ""
    if snapshot is None:
        return "unknown", None, [], "no pre-run snapshot of the drafts was taken"
    try:
        drafts = client.list_drafts()
    except Exception as exc:  # noqa: BLE001
        return "unknown", None, [], f"cannot list the drafts ({exc})"
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
        except Exception:  # noqa: BLE001
            pass
        if k < attempts - 1:
            _sleep(_verify_delay(k))
    return last, False


def _cleanup(client, step: str, exc: BaseException, host: str, on_published):
    """Decide and perform the cleanup. Returns (message, recovered_deposition, local_save_error)."""
    state = client.created_state
    if not state:
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
            return "\n".join(lines), None, None
        dep_id = found
        print(f"!! Attributed to this run with certainty: {kind} {dep_id}", file=sys.stderr)
    manual = f"https://{host}/deposit/{dep_id}"
    print(f"!! This run created: {kind} {dep_id}", file=sys.stderr)
    if kind != "edit session" and snapshot is not None and dep_id in snapshot:
        return (f"!! REFUSING to discard {kind} {dep_id}: it existed before this run (pre-run snapshot). "
                f"Inspect {manual} by hand.", None, None)
    dep, read_err = None, None
    try:
        dep = client.get_deposition(dep_id)
    except Exception as exc2:  # noqa: BLE001
        read_err = exc2
    if step == "publish":
        if dep is None:
            return (f"!! CLEANUP SKIPPED: cannot read state of {dep_id} after the failed publish call "
                    f"({read_err}). Inspect {manual} by hand; it may be published or still a draft. "
                    "Nothing was discarded.", None, None)
        if dep.get("state") != "done" and not is_definitive_refusal(exc):
            # The call may still have gone through (timeout, dropped connection, interruption, 5xx).
            dep, done = _poll_published(client, dep_id)
            if not done:
                return (f"!! The publish call ended in doubt and {dep_id} is not (yet) published. "
                        f"NOTHING WAS DISCARDED: the publication may still complete. Inspect {manual} "
                        f"and https://{host}/record/{dep_id}; discard by hand only if it is a draft.",
                        None, None)
        if dep.get("state") == "done":
            save_error = None
            msg = (f"!! The publish call failed but {dep_id} is PUBLISHED (state done). No cleanup. "
                   f"Check https://{host}/record/{dep_id}.")
            if on_published is not None:
                try:
                    on_published(dep)
                except Exception as exc3:  # noqa: BLE001
                    save_error = exc3
                    msg += (f"\n!! LOCAL STATE NOT SAVED ({exc3}). Set zenodo_id (concept recid) and doi in "
                            "zenodo.json BY HAND and DO NOT re-run the deposit: it would publish twice.")
            return msg, dep, save_error
    else:
        if dep is not None and dep.get("state") == "done":
            return (f"!! {kind} {dep_id} is in state 'done': nothing to discard (it is not an unpublished "
                    f"state). Inspect {manual}.", None, None)
        if dep is not None and concept is not None and dep.get("conceptrecid") not in (None, "") \
                and str(dep.get("conceptrecid")) != str(concept):
            return (f"!! REFUSING to discard {dep_id}: it belongs to concept {dep.get('conceptrecid')}, "
                    f"not {concept}. Inspect {manual} by hand.", None, None)
        if dep is None and not certain:
            return (f"!! CLEANUP SKIPPED: cannot read {dep_id} ({read_err}). Inspect {manual}.", None, None)
    try:
        r = client.discard(dep_id)
        ok = r.ok
        detail = f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc2:  # noqa: BLE001
        ok, detail = False, str(exc2)
    if ok:
        return (f"Cleanup performed: POST actions/discard on {kind} {dep_id} ({detail}). "
                "Nothing published. Exiting non-zero.", None, None)
    return (f"!! CLEANUP FAILED for {kind} {dep_id} ({detail}).\n"
            f"!! MANUAL CLEANUP REQUIRED: discard it at {manual}. Nothing was published.", None, None)


def abort_and_cleanup(client, step: str, exc: BaseException, host: str, on_published=None):
    """Report a failure, clean up ONLY state this run certainly created, then exit non-zero.

    KeyboardInterrupt / SystemExit are handled like any failure and RE-RAISED afterwards. Returns
    (deposition, local_save_error) only when the failed publish call turned out to have published."""
    print(f"\n!! FAILED at step: {step}", file=sys.stderr)
    resp = getattr(exc, "response", None)
    if resp is not None:
        print(f"!! HTTP status: {resp.status_code}; body: {resp.text[:300]}", file=sys.stderr)
    print(f"!! Error: {type(exc).__name__}: {exc}", file=sys.stderr)
    interrupted = not isinstance(exc, Exception)
    try:
        msg, recovered, save_error = _cleanup(client, step, exc, host, on_published)
    except BaseException as exc2:  # noqa: BLE001 - a second interrupt or a bug must not hide the state
        state = client.created_state or {}
        msg = (f"!! CLEANUP ABORTED ({type(exc2).__name__}: {exc2}). State possibly created by this run: "
               f"{state.get('kind')} {state.get('dep_id')}. NOTHING MORE WAS DONE: inspect "
               f"https://{host}/deposit by hand.")
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
    sys.exit(msg)


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
            got = f"<unreadable: {exc}>"
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


def deposit_update(client, metadata_built: dict, code_url: str, zenodo_id: int, env_label: str,
                   host: str, on_published=None) -> tuple[int, str | None]:
    """--update: metadata-only edit in place. Returns (dep_id, doi)."""
    step = "resolve latest version"
    try:
        dep_id = client.resolve_latest_version_recid(int(zenodo_id))
    except BaseException as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if dep_id != int(zenodo_id):
        print(f"\nConcept record {zenodo_id} → latest version {dep_id} [{env_label}]")
    refuse_if_unpublished_state(client, zenodo_id, dep_id, host)
    step = "edit"
    result, save_error, expected = None, None, None
    try:
        print(f"Editing metadata of record {dep_id} in place [{env_label}]…")
        client.edit_deposition(dep_id)
        step = "read live metadata"
        # Zenodo's PUT replaces the whole metadata object, so an --update built only from the
        # LaTeX and zenodo.json would silently drop every field maintained elsewhere. Merge over
        # the live metadata instead: rebuilt fields win, unmanaged fields survive untouched.
        live = (client.get_deposition(dep_id).get("metadata") or {})
        dropped = sorted(set(live) - set(metadata_built))
        live_custom = live.get("custom") or {}
        metadata = {**live, **metadata_built}
        if live_custom or metadata.get("custom"):
            # Custom fields are merged key by key so that fields set by hand survive; a PUT
            # that omits custom therefore cannot clear an existing code:codeRepository.
            metadata["custom"] = {**live_custom, **(metadata.get("custom") or {})}
        expected = code_url or live_custom.get(CODE_REPOSITORY_FIELD)
        if dropped:
            print(f"  Preserved fields not managed by this script: {', '.join(dropped)}")
        step = "metadata PUT"
        print("Setting metadata…")
        client.set_metadata(dep_id, metadata)
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
        raise PostPublishFailure(f"\n!! local state not saved: {save_error}")
    return dep_id, result.get("doi")


def deposit_new(client, pdf_path: Path, metadata: dict, code_url: str, zenodo_id,
                env_label: str, host: str, on_published=None) -> dict:
    """New version (zenodo_id set) or first publication. Returns the publish response.

    on_published(result) runs the moment a publication is confirmed (publish returned, or a failed
    publish call was followed by a read showing state 'done'), BEFORE any verification or archiving."""
    step, latest_recid, dep_id = "resolve latest version", None, None
    try:
        if zenodo_id:
            latest_recid = client.resolve_latest_version_recid(int(zenodo_id))
            if latest_recid != int(zenodo_id):
                print(f"\nConcept record {zenodo_id} → latest version {latest_recid} [{env_label}]")
    except BaseException as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if zenodo_id:
        refuse_if_unpublished_state(client, zenodo_id, latest_recid, host)
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
        print(f"Uploading {pdf_path.name} …")
        client.upload_file(dep, pdf_path)               # a failure is blocking
        step = "verify draft files"
        verify_draft_files(client, dep_id, pdf_path.name)
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
        except Exception as exc:  # noqa: BLE001
            save_error = exc
            print(f"!! PUBLISHED but LOCAL STATE NOT SAVED ({exc}). Set zenodo_id and doi in zenodo.json "
                  "BY HAND and DO NOT re-run the deposit: it would publish twice.", file=sys.stderr)
    verify_published_code_repository(client, result.get("id", dep_id), code_url, host)
    if save_error is not None:
        raise PostPublishFailure(f"\n!! local state not saved: {save_error}")
    return result


# ── Local state / sandbox ──────────────────────────────────────────────────────

def record_sandbox_state(path: Path, host: str, paper: str, version, result: dict) -> None:
    """A sandbox run records its result HERE and nowhere else (never zenodo.json, pdfs/, bibliographies)."""
    data = {
        "sandbox": True, "host": host, "paper": paper, "version": version,
        "record_id": result.get("id"), "doi": result.get("doi"),
        "conceptrecid": result.get("conceptrecid") or (result.get("metadata") or {}).get("conceptrecid"),
        "conceptdoi": result.get("conceptdoi") or (result.get("metadata") or {}).get("conceptdoi"),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  [sandbox] result recorded in {path} (production zenodo.json, pdfs/ and bibliographies untouched)")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def validate_sandbox_state_path(path: Path, repo_root: Path, paper_dir: Path) -> Path:
    """The sandbox state file must live outside the workspace (a repo might commit it)."""
    p = path.expanduser().resolve()
    for root in (repo_root.resolve(), paper_dir.resolve()):
        if p == root or root in p.parents:
            sys.exit(f"Error: --sandbox-state {p} lies inside {root}: a repository could commit it. "
                     "Use a path under the OS temp/scratch directory.")
    if p.is_dir():
        sys.exit(f"Error: --sandbox-state {p} is a directory")
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

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Publish a Cosmochrony paper to Zenodo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paper_dir", type=Path, help="Paper directory (e.g. bell-paper/)")
    parser.add_argument("--sandbox", action="store_true",
                        help="Use sandbox.zenodo.org (reads ONLY ZENODO_SANDBOX_TOKEN; needs --sandbox-state; "
                             "touches no production file)")
    parser.add_argument("--sandbox-state", type=Path, metavar="PATH",
                        help="With --sandbox: JSON file (outside the workspace) recording the sandbox result")
    parser.add_argument("--sandbox-zenodo-id", type=int, metavar="ID",
                        help="With --sandbox: sandbox concept/record id for a new version or --update "
                             "(the production zenodo_id is never used on the sandbox)")
    parser.add_argument("--dry-run", action="store_true", help="Parse LaTeX only, do not upload")
    parser.add_argument("--update", action="store_true", help="Update metadata in place (same DOI, no new version — PDF unchanged; use without flag for new version with PDF)")
    parser.add_argument("--no-cascade", action="store_true", help="Skip DOI cascade after publish")
    parser.add_argument("--finish-after-fail", action="store_true",
                        help="Offline: after a post-publish FAIL (record published, zenodo.json saved), run the "
                             "skipped PDF archive and DOI cascade from zenodo.json; no network, no deposit")
    parser.add_argument("--skip-checks", action="store_true",
                        help="Bypass the pre-deposit check battery (not recommended)")
    args = parser.parse_args(argv)

    if args.finish_after_fail and (args.sandbox or args.update or args.dry_run):
        parser.error("--finish-after-fail is a production-only, offline step: no --sandbox/--update/--dry-run")
    networked = not (args.dry_run or args.finish_after_fail)
    if args.sandbox and networked and not args.sandbox_state:
        parser.error("--sandbox requires --sandbox-state PATH (a file outside the workspace)")

    # ── Auth first: refuse to start without the right token, before anything else ─────────────
    token = resolve_token(args.sandbox) if networked else None

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

    sandbox_state = None
    if args.sandbox and networked:
        sandbox_state = validate_sandbox_state_path(args.sandbox_state, repo_root, paper_dir)

    # ── Parse LaTeX ────────────────────────────────────────────────────────────
    tex_path = find_main_tex(paper_dir, config)
    print(f"Parsing  {tex_path.relative_to(repo_root)}")

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

    if args.dry_run:
        # Show the exact metadata a deposit would send (new-version / first-publication path;
        # an --update additionally merges these over the live record). No network access.
        dry_meta = build_metadata(title, abstract, keywords, config.get("web_url", ""), version,
                                  publication_type=config.get("publication_type"),
                                  notes=config.get("notes"),
                                  code_url=code_url)
        print("\n  Metadata that would be sent:")
        print(json.dumps(dry_meta, indent=2, ensure_ascii=False))
        print("\n[Dry run] Stopping here — no upload performed.")
        return

    # ── --finish-after-fail: the local steps a post-publish FAIL skipped (offline) ─────────────
    if args.finish_after_fail:
        zid, concept_doi = config.get("zenodo_id"), config.get("doi")
        if not zid or not concept_doi:
            sys.exit("Error: zenodo.json has no zenodo_id/doi: the publication was not saved locally, "
                     "so there is nothing to finish. Set them by hand from the record first.")
        pdf_path = find_pdf(paper_dir)
        print(f"  PDF:      {pdf_path.relative_to(repo_root)}  (must be the PDF that was deposited)")
        archive_pdf(repo_root, pdf_path, tex_path.stem, version, zid)
        run_cascade(repo_root, config, concept_doi, args.no_cascade)
        return

    # ── Pre-deposit gate ───────────────────────────────────────────────────────
    # A published Zenodo record cannot be deleted, so refuse to upload an artefact
    # the battery rejects. An unavailable checker warns but never blocks a
    # legitimate deposit; only actual findings do.
    if not args.skip_checks:
        try:
            from predeposit_check import check as _predeposit_check
            result = _predeposit_check(paper_dir, deposit=True)
            problems = result["problems"]
        except Exception as exc:  # noqa: BLE001
            print(f"  Warning: pre-deposit checks unavailable ({exc}); continuing")
        else:
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
    print(f"  PDF:      {pdf_path.relative_to(repo_root)}")

    try:
        client = ZenodoClient(token, sandbox=args.sandbox)
    except TokenHostMismatch as exc:
        sys.exit(f"Error: {exc}. Nothing was sent.")
    env_label = "sandbox" if args.sandbox else "production"
    # The production zenodo_id is meaningless on the sandbox: a sandbox run uses --sandbox-zenodo-id only.
    zenodo_id = args.sandbox_zenodo_id if args.sandbox else config.get("zenodo_id")

    host = SANDBOX_HOST if args.sandbox else PRODUCTION_HOST
    metadata = build_metadata(title, abstract, keywords, config.get("web_url", ""), version,
                              publication_type=config.get("publication_type"),
                              notes=config.get("notes"),
                              code_url=code_url)

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
        if not concept_doi or not concept_recid:
            raise RuntimeError(
                "Zenodo response did not include conceptdoi/conceptrecid; refusing to overwrite "
                "zenodo.json with version-specific identifiers. Inspect the publish response and "
                "update manually."
            )
        config["zenodo_id"] = int(concept_recid)
        config["doi"] = concept_doi
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  Saved zenodo_id + concept DOI → {config_path.relative_to(repo_root)}")
        published["concept_doi"] = concept_doi

    def save_sandbox_state(result: dict):
        print("\nPublished on the SANDBOX!")
        record_sandbox_state(sandbox_state, host, paper_dir.name, version, result)

    saver = save_sandbox_state if args.sandbox else save_local_state

    # ── --update: metadata-only edit in place via legacy deposit API (same DOI) ─
    # Note: file replacement in place is not possible via the Zenodo REST API;
    # the file bucket remains locked for published records. Use new version for files.
    if args.update:
        if not zenodo_id:
            sys.exit("Error: --update needs an existing record: zenodo_id in zenodo.json"
                     + (" (on the sandbox: --sandbox-zenodo-id)" if args.sandbox else "") + ".")
        try:
            dep_id, doi = deposit_update(client, metadata, code_url, int(zenodo_id), env_label, host,
                                         on_published=save_sandbox_state if args.sandbox else None)
        except PostPublishFailure as exc:
            sys.exit(str(exc))
        doi = doi or config.get("doi", "")
        print(f"\nUpdated! (metadata only — files unchanged)")
        print(f"  DOI:    {doi}")
        print(f"  Record: https://{host}/record/{dep_id}")
        return

    # ── New version or first publication via legacy deposit API ────────────────
    try:
        result = deposit_new(client, pdf_path, metadata, code_url, zenodo_id, env_label, host,
                             on_published=saver)
    except PostPublishFailure as exc:
        sys.exit(str(exc) + "\n!! PDF archive and DOI cascade were SKIPPED (a sandbox run skips them "
                 "always). Once the record is fixed, run them with --finish-after-fail.")
    if args.sandbox:
        print("\n[sandbox] PDF archive and DOI cascade skipped by contract: no production file was written.")
        return
    concept_doi = published["concept_doi"]

    # Archive the published PDF into the central pdfs/ store (systematic, every deposit).
    archive_pdf(repo_root, pdf_path, tex_path.stem, version, result["id"])
    run_cascade(repo_root, config, concept_doi, args.no_cascade)


if __name__ == "__main__":
    main()
