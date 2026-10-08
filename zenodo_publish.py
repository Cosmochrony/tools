#!/usr/bin/env python3
"""zenodo_publish.py — Automated Zenodo publication for Cosmochrony papers.

Usage:
    ZENODO_TOKEN=xxx python tools/zenodo_publish.py bell-paper/ [--sandbox] [--dry-run]

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
import shutil
import subprocess
import sys
from pathlib import Path

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

class ZenodoClient:
    def __init__(self, token: str, sandbox: bool = False):
        global requests
        if requests is None:
            import requests as _req
            requests = _req
        self.base = SANDBOX_URL if sandbox else ZENODO_URL
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.created_state: dict | None = None  # server-side state created by THIS run

    def _ok(self, resp: requests.Response) -> dict:
        if not resp.ok:
            print(f"  HTTP {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
            resp.raise_for_status()
        return resp.json()

    def create_deposition(self) -> dict:
        dep = self._ok(self.session.post(f"{self.base}/deposit/depositions", json={}))
        self.created_state = {"dep_id": dep["id"], "kind": "new deposition draft"}
        return dep

    def get_deposition(self, dep_id: int) -> dict:
        return self._ok(self.session.get(f"{self.base}/deposit/depositions/{dep_id}"))

    def edit_deposition(self, dep_id: int) -> dict:
        self._ok(self.session.post(
            f"{self.base}/deposit/depositions/{dep_id}/actions/edit"
        ))
        # The record is now in edit state because of THIS run: remember it before any further call.
        self.created_state = {"dep_id": dep_id, "kind": "edit session"}
        return self.get_deposition(dep_id)

    def new_version(self, dep_id: int) -> dict:
        resp = self._ok(self.session.post(
            f"{self.base}/deposit/depositions/{dep_id}/actions/newversion"
        ))
        draft_url = resp["links"]["latest_draft"]
        # The draft exists server-side because of THIS run: record its id before the follow-up GET.
        self.created_state = {"dep_id": int(draft_url.rstrip("/").rsplit("/", 1)[-1]),
                              "kind": "new-version draft"}
        return self._ok(self.session.get(draft_url))

    def list_drafts(self) -> list[dict]:
        """All unpublished depositions of the account (read-only)."""
        out: list[dict] = []
        for page in range(1, 21):
            batch = self._ok(self.session.get(
                f"{self.base}/deposit/depositions",
                params={"status": "draft", "size": 100, "page": page, "all_versions": "true"},
            ))
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def discard(self, dep_id: int) -> requests.Response:
        """Documented actions/discard: deletes a draft, or ends an edit session without publishing."""
        return self.session.post(f"{self.base}/deposit/depositions/{dep_id}/actions/discard")

    def get_record(self, rec_id: int) -> dict:
        """Public record (the same API host, read-only)."""
        return self._ok(self.session.get(f"{self.base}/records/{rec_id}"))

    def resolve_latest_version_recid(self, recid: int) -> int:
        """Resolve a Zenodo recid (concept or version) to the latest published version's recid.
        GET /api/records/{recid} redirects to the latest version when given a concept recid,
        and returns the record itself otherwise."""
        resp = self._ok(self.session.get(f"{self.base}/records/{recid}"))
        return int(resp["id"])

    def delete_file(self, dep_id: int, file_id: str) -> bool:
        resp = self.session.delete(
            f"{self.base}/deposit/depositions/{dep_id}/files/{file_id}"
        )
        if not resp.ok:
            print(f"  Warning: delete returned {resp.status_code}: {resp.text[:200]}",
                  file=sys.stderr)
            return False
        return True

    def upload_file(self, deposition: dict, file_path: Path) -> dict:
        """Upload via S3 bucket API (new depositions and new versions)."""
        bucket_url = deposition["links"]["bucket"]
        with open(file_path, "rb") as fh:
            return self._ok(self.session.put(
                f"{bucket_url}/{file_path.name}",
                data=fh,
                headers={"Content-Type": "application/octet-stream"},
            ))

    def set_metadata(self, dep_id: int, metadata: dict) -> dict:
        return self._ok(self.session.put(
            f"{self.base}/deposit/depositions/{dep_id}",
            json={"metadata": metadata},
        ))

    def publish(self, dep_id: int) -> dict:
        return self._ok(self.session.post(
            f"{self.base}/deposit/depositions/{dep_id}/actions/publish"
        ))


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
    """The record is published but its verification failed (never un-published automatically)."""


def custom_repository(meta: dict | None) -> str | None:
    return ((meta or {}).get("custom") or {}).get(CODE_REPOSITORY_FIELD)


def refuse_if_unpublished_state(client, concept_id: int, latest_id: int, host: str):
    """Refuse (nothing created) when a draft / edit session of this concept already exists."""
    problems = []
    try:
        for d in client.list_drafts():
            if str(d.get("conceptrecid")) == str(concept_id) or d.get("id") in (latest_id,):
                problems.append(f"unpublished deposition {d.get('id')} (state {d.get('state')})")
        latest = client.get_deposition(latest_id)
        if latest.get("state") not in (None, "done"):
            problems.append(f"record {latest_id} is already in state {latest.get('state')!r}")
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


def abort_and_cleanup(client, step: str, exc: BaseException, host: str):
    """Report a failure, clean up ONLY the state created by this run, then exit non-zero."""
    print(f"\n!! FAILED at step: {step}", file=sys.stderr)
    resp = getattr(exc, "response", None)
    if resp is not None:
        print(f"!! HTTP status: {resp.status_code}; body: {resp.text[:300]}", file=sys.stderr)
    print(f"!! Error: {exc}", file=sys.stderr)
    state = client.created_state
    if not state:
        sys.exit("No server-side state had been created by this run; nothing to clean up.")
    dep_id, kind = state["dep_id"], state["kind"]
    print(f"!! This run created: {kind} {dep_id}", file=sys.stderr)
    manual = f"https://{host}/deposit/{dep_id}"
    if step == "publish":
        # A failed publish call may still have published: never discard without proof it did not.
        try:
            st = client.get_deposition(dep_id).get("state")
        except Exception as exc2:  # noqa: BLE001
            sys.exit(f"!! CLEANUP SKIPPED: cannot read state of {dep_id} after failed publish ({exc2}). "
                     f"Inspect {manual} by hand; it may be published or still a draft.")
        if st == "done":
            sys.exit(f"!! The publish call failed but {dep_id} is PUBLISHED (state done). "
                     f"No cleanup done. Check https://{host}/record/{dep_id}.")
    try:
        r = client.discard(dep_id)
        ok = r.ok
        detail = f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc2:  # noqa: BLE001
        ok, detail = False, str(exc2)
    if ok:
        sys.exit(f"Cleanup performed: POST actions/discard on {kind} {dep_id} ({detail}). "
                 "Nothing published. Exiting non-zero.")
    sys.exit(f"!! CLEANUP FAILED for {kind} {dep_id} ({detail}).\n"
             f"!! MANUAL CLEANUP REQUIRED: discard it at {manual}. Nothing was published.")


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


def verify_published_code_repository(client, rec_id: int, expected: str | None, host: str):
    if not expected:
        return
    try:
        got = custom_repository(client.get_record(rec_id).get("metadata"))
    except Exception as exc:  # noqa: BLE001
        got = f"<unreadable: {exc}>"
    if got != expected:
        print(f"  Software field on published record: FAIL (got {got!r}, expected {expected!r})",
              file=sys.stderr)
        raise PostPublishFailure(
            f"\n!! RECORD {rec_id} IS PUBLISHED but its Software field is wrong or unverifiable.\n"
            f"!! Not un-publishing. Inspect https://{host}/record/{rec_id}; fix with a reviewed "
            "metadata-only edit (--update) or by hand in the Zenodo UI.")
    print(f"  Software field on published record: PASS ({expected})")


def deposit_update(client, metadata_built: dict, code_url: str, zenodo_id: int, env_label: str,
                   host: str) -> tuple[int, str | None]:
    """--update: metadata-only edit in place. Returns (dep_id, doi)."""
    dep_id = client.resolve_latest_version_recid(int(zenodo_id))
    if dep_id != int(zenodo_id):
        print(f"\nConcept record {zenodo_id} → latest version {dep_id} [{env_label}]")
    refuse_if_unpublished_state(client, int(zenodo_id), dep_id, host)
    step = "edit"
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
    except Exception as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    verify_published_code_repository(client, dep_id, expected, host)
    return dep_id, result.get("doi")


def deposit_new(client, pdf_path: Path, metadata: dict, code_url: str, zenodo_id,
                env_label: str, host: str, on_published=None) -> dict:
    """New version (zenodo_id set) or first publication. Returns the publish response."""
    step = "start"
    try:
        if zenodo_id:
            step = "resolve latest version"
            latest_recid = client.resolve_latest_version_recid(int(zenodo_id))
            if latest_recid != int(zenodo_id):
                print(f"\nConcept record {zenodo_id} → latest version {latest_recid} [{env_label}]")
        else:
            latest_recid = None
    except Exception as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if zenodo_id:
        refuse_if_unpublished_state(client, int(zenodo_id), latest_recid, host)
    try:
        if zenodo_id:
            step = "newversion"
            print(f"New version of record {latest_recid} [{env_label}]…")
            dep = client.new_version(latest_recid)
            dep_id = dep["id"]
            step = "delete old files"
            for f in dep.get("files", []):
                client.delete_file(dep_id, f["id"])
                print(f"  Removed old file: {f['filename']}")
        else:
            step = "create deposition"
            print(f"\nNew deposition [{env_label}]…")
            dep = client.create_deposition()
            dep_id = dep["id"]
        print(f"  Deposition ID: {dep_id}")
        step = "upload"
        print(f"Uploading {pdf_path.name} …")
        client.upload_file(dep, pdf_path)
        step = "metadata PUT"
        print("Setting metadata…")
        client.set_metadata(dep_id, metadata)
        step = "verify draft metadata"
        verify_draft_code_repository(client, dep_id, code_url)
        step = "publish"
        print("Publishing…")
        result = client.publish(dep_id)
    except Exception as exc:  # noqa: BLE001
        abort_and_cleanup(client, step, exc, host)
    if on_published:
        on_published(result)
    verify_published_code_repository(client, result.get("id", dep_id), code_url, host)
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Publish a Cosmochrony paper to Zenodo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paper_dir", type=Path, help="Paper directory (e.g. bell-paper/)")
    parser.add_argument("--sandbox", action="store_true", help="Use sandbox.zenodo.org for testing")
    parser.add_argument("--dry-run", action="store_true", help="Parse LaTeX only, do not upload")
    parser.add_argument("--update", action="store_true", help="Update metadata in place (same DOI, no new version — PDF unchanged; use without flag for new version with PDF)")
    parser.add_argument("--no-cascade", action="store_true", help="Skip DOI cascade after publish")
    parser.add_argument("--skip-checks", action="store_true",
                        help="Bypass the pre-deposit check battery (not recommended)")
    args = parser.parse_args()

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

    # ── Auth ───────────────────────────────────────────────────────────────────
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        sys.exit("Error: ZENODO_TOKEN environment variable is not set")

    client = ZenodoClient(token, sandbox=args.sandbox)
    env_label = "sandbox" if args.sandbox else "production"
    zenodo_id = config.get("zenodo_id")

    host = "sandbox.zenodo.org" if args.sandbox else "zenodo.org"
    metadata = build_metadata(title, abstract, keywords, config.get("web_url", ""), version,
                              publication_type=config.get("publication_type"),
                              notes=config.get("notes"),
                              code_url=code_url)

    # ── --update: metadata-only edit in place via legacy deposit API (same DOI) ─
    # Note: file replacement in place is not possible via the Zenodo REST API;
    # the file bucket remains locked for published records. Use new version for files.
    if args.update and zenodo_id:
        try:
            dep_id, doi = deposit_update(client, metadata, code_url, int(zenodo_id), env_label, host)
        except PostPublishFailure as exc:
            sys.exit(str(exc))
        doi = doi or config.get("doi", "")
        print(f"\nUpdated! (metadata only — files unchanged)")
        print(f"  DOI:    {doi}")
        print(f"  Record: https://{host}/record/{dep_id}")
        return

    # ── New version or first publication via legacy deposit API ────────────────
    published: dict = {}

    def save_local_state(result: dict):
        # Runs right after publish and BEFORE verification, so a later verification failure
        # cannot make a re-run create a duplicate deposition.
        doi = result["doi"]
        concept_doi = result.get("conceptdoi") or result.get("metadata", {}).get("conceptdoi")
        concept_recid = result.get("conceptrecid") or result.get("metadata", {}).get("conceptrecid")
        print(f"\nPublished!")
        print(f"  Version DOI:   {doi} (ephemeral, not stored)")
        print(f"  Concept DOI:   {concept_doi}")
        print(f"  Concept recid: {concept_recid}")
        print(f"  Record: https://{host}/record/{result['id']}")
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

    try:
        result = deposit_new(client, pdf_path, metadata, code_url, zenodo_id, env_label, host,
                             on_published=save_local_state)
    except PostPublishFailure as exc:
        sys.exit(str(exc) + "\n!! zenodo.json was saved; PDF archive and DOI cascade were SKIPPED: "
                 "run them by hand once the record is fixed.")
    concept_doi = published["concept_doi"]

    # Archive the published PDF into the central pdfs/ store (systematic, every deposit).
    archive_pdf(repo_root, pdf_path, tex_path.stem, version, result["id"])

    if not args.no_cascade:
        bib_key = config.get("bib_key")
        if bib_key:
            print(f"\nDOI cascade ({bib_key} → {concept_doi})…")
            doi_cascade(repo_root, bib_key, concept_doi)
        else:
            print("\nNo bib_key in zenodo.json — skipping DOI cascade.")


if __name__ == "__main__":
    main()
