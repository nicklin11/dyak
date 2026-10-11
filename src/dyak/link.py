"""Stage 11: link. Inject wikilinks to the atomic term notes. Idempotent, never inside math.

Ported from `~/.local/bin/mscs-termlink` (issue #8), which was a single untracked file in
a bin directory. The port exists so the behaviour is reviewable and tested rather than
merely installed.

## The model, unchanged by the port

A term is an atomic note `mscs/_terms/<Canonical>.md` whose frontmatter lists every ASR
variant under `aliases:`. The filename is the canonical, so Obsidian resolves `[[МПЦ]]`
itself and collects the backlinks. This stage only finds a variant in the body of a lecture
and wraps it as `[[variant|canonical]]`; when the text already *is* the canonical, no pipe
is emitted, because `[[Open loop]]` reads better than `[[Open loop|Open loop]]` in source.

No index note is generated on the way through. Obsidian's backlinks panel is the index; a
generated `_index.md` is a second thing that can be stale, and it is only written on
explicit request.

## The two load-bearing properties

**Never inside math or code.** Every region that must not be touched is masked before
substitution and restored after: `$$...$$`, fenced blocks, inline code, inline math, existing
`[[wikilinks]]`, markdown links, bare URLs and HTML. Two namespaces are used (`\\x01` for
regions that fit on one line, `\\x00` for regions that do not) because a single-line mask
applied over a document would cut `$$...$$` in half and link a term inside a formula,
corrupting the LaTeX.

**Idempotent.** Existing `[[...]]` are masked before substitution, so a second run finds
nothing to do and returns byte-identical text. A linker that double-links is worse than no
linker: the second `[[...]]` renders as visible garbage.

## What the port changed

Behaviour is preserved; three things are not.

*Configuration instead of constants.* The original hardcoded `VAULT` and `TERMS_DIR` at
module level. They are resolved here from arguments, then `DYAK_TERMS_DIR`, then the
vault path, so the same code runs against any vault (issue #8; ADR-0001 D9).

*`_excluded` follows the configured terms dir.* The original compared against the module
global, so `--terms-dir` changed where terms were loaded from but not what was excluded from
the link sweep. Passing `--terms-dir` therefore did not do what it said.

*Lecture notes are read once.* The original re-read every lecture file once per term in
both `index` and `check` -- 34 terms x N lectures x 3 reads. The port loads them once.

## What this stage does not do

It does not decide what a note is called, where it lives, or which course it belongs to.
That is stage 12's job (issue #19). This stage takes one note and returns the same note with
links in it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


LINKED_NOTE = "note.linked.md"
REPORT_NAME = "link.json"
PROVENANCE_NAME = "link-provenance.json"
ALL_ARTEFACTS = (LINKED_NOTE, REPORT_NAME, PROVENANCE_NAME)

TERMS_ENV = "DYAK_TERMS_DIR"
DEFAULT_GLOB = "Лекция*.md"
#: The directory name under a vault that holds the courses, and the one that holds terms.
COURSES_DIRNAME = "mscs"
TERMS_DIRNAME = "_terms"


# --- configuration -------------------------------------------------------------------


def resolve_terms_dir(
    explicit: str | Path | None = None, vault: str | Path | None = None
) -> Path | None:
    """Terms directory, most specific source first.

    Explicit argument, then `$DYAK_TERMS_DIR`, then `<vault>/mscs/_terms`. Returns None
    when none of them names a directory that exists -- which the caller must report, because
    a link stage that ran against zero terms is not a link stage that found nothing to link.
    """
    for candidate in (explicit, os.environ.get(TERMS_ENV) or None, _under_vault(vault)):
        if candidate is None:
            continue
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return None


def _under_vault(vault: str | Path | None) -> Path | None:
    if vault is None:
        return None
    return Path(vault).expanduser() / COURSES_DIRNAME / TERMS_DIRNAME


# --- frontmatter ---------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict, int]:
    """Minimal YAML subset: scalars and lists. Returns (data, offset just past the block)."""
    if not text.startswith("---"):
        return {}, 0
    end = text.find("\n---", 3)
    if end == -1:
        return {}, 0
    data: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t", "-")) and key:
            item = line.strip().lstrip("-").strip()
            if item:
                data.setdefault(key, [])
                if isinstance(data[key], list):
                    data[key].append(item)
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if val.startswith("[") and val.endswith("]"):
                # Inline flow list: `aliases: [a, b]`. Quotes around items are stripped.
                items = [x.strip().strip("'\"") for x in val[1:-1].split(",")]
                data[key] = [x for x in items if x]
            elif val:
                data[key] = val
            else:
                data[key] = []
    return data, end + 4


# --- masking -------------------------------------------------------------------------

#: Regions that can span lines. Removed from the whole document before per-line work, or a
#: line mask would cut `$$...$$` in half and a term inside the formula would get a link.
MULTILINE = re.compile(r"\$\$.*?\$\$|^[ \t]*```.*?^[ \t]*```|^[ \t]*~~~.*?^[ \t]*~~~", re.S | re.M)
#: Regions guaranteed to fit on one line.
INLINE_CODE = re.compile(r"`[^`\n]+`")
INLINE_MATH = re.compile(r"\$[^$\n]+\$")
WIKILINK = re.compile(r"\[\[[^\]\n]*\]\]")
MDLINK = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")
BARE_URL = re.compile(r"https?://\S+")
HTML = re.compile(r"<[^>\n]+>")

INLINE_MASK = "\x01"
MULTILINE_MASK = "\x00"


def mask(text: str) -> tuple[str, list[str]]:
    store: list[str] = []

    def put(m: re.Match) -> str:
        store.append(m.group(0))
        return f"{INLINE_MASK}{len(store) - 1}{INLINE_MASK}"

    for rx in (INLINE_CODE, INLINE_MATH, WIKILINK, MDLINK, BARE_URL, HTML):
        text = rx.sub(put, text)
    return text, store


def unmask(text: str, store: list[str]) -> str:
    pattern = re.escape(INLINE_MASK) + r"(\d+)" + re.escape(INLINE_MASK)
    return re.sub(pattern, lambda m: store[int(m.group(1))], text)


# --- terms ---------------------------------------------------------------------------


@dataclass
class Term:
    canonical: str
    path: Path
    aliases: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    domain: str = ""

    @property
    def patterns(self) -> list[str]:
        """Every surface form to look for: canonical + aliases + word forms.

        The canonical is linked too, otherwise a term with no distinct ASR variant never
        gets a link at all. No double-linking follows: existing `[[...]]` are masked first.
        """
        seen: set[str] = set()
        out: list[str] = []
        for form in (self.canonical, *self.aliases, *self.forms):
            key = form.casefold()
            if key and key not in seen:
                seen.add(key)
                out.append(form)
        return out


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_terms(terms_dir: Path) -> list[Term]:
    terms: list[Term] = []
    for path in sorted(terms_dir.glob("*.md")):
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("type") != "term":
            continue
        terms.append(
            Term(
                canonical=path.stem,
                path=path,
                aliases=[str(x) for x in _as_list(meta.get("aliases"))],
                forms=[str(x) for x in _as_list(meta.get("forms"))],
                domain=meta.get("domain", ""),
            )
        )
    # Longest forms first, so "динамическое программирование" wins over "программирование".
    terms.sort(key=lambda t: -max((len(x) for x in t.patterns), default=0))
    return terms


def build_index(terms: list[Term]) -> tuple[re.Pattern, dict[str, Term]]:
    """One regex over every form -- one pass instead of O(terms x forms).

    Longest first, so the longest alternative wins at any given position. The lookarounds
    stop a form from matching inside a longer word or inside an existing `[[...]]`.
    """
    owner: dict[str, Term] = {}
    forms: list[str] = []
    for term in terms:
        for form in term.patterns:
            key = form.casefold()
            if key not in owner:
                owner[key] = term
                forms.append(form)
    if not forms:
        return re.compile(r"(?!x)x"), {}
    forms.sort(key=len, reverse=True)
    body = "|".join(re.escape(form) for form in forms)
    return re.compile(rf"(?<![\w\[])(?:{body})(?![\w\]])", re.IGNORECASE | re.UNICODE), owner


# --- substitution --------------------------------------------------------------------


#: A heading is not a link target: it becomes part of the link graph and renders as a link.
HEADING = re.compile(r"^#{1,6}[ \t]")


def substitute(text: str, rx: re.Pattern, owner: dict[str, Term]) -> tuple[str, dict[str, int]]:
    """Wrap every match outside masked regions as `[[matched|canonical]]`.

    Frontmatter, headings are left untouched. A link produced inside a table row has its pipe
    escaped as `\\|`, because an unescaped `|` splits the cell and shifts every later column.
    """
    counts: dict[str, int] = {}

    def make_repl(in_table: bool):
        def repl(m: re.Match) -> str:
            hit = m.group(0)
            term = owner[hit.casefold()]
            counts[term.canonical] = counts.get(term.canonical, 0) + 1
            # A match that already is the canonical needs no pipe.
            if hit.casefold() == term.canonical.casefold():
                return f"[[{hit}]]"
            sep = "\\|" if in_table else "|"
            return f"[[{hit}{sep}{term.canonical}]]"

        return repl

    # Frontmatter is opaque: its keys and values are data, not prose.
    _, head_end = parse_frontmatter(text)
    head, body = text[:head_end], text[head_end:]

    store: list[str] = []

    def stash(m: re.Match) -> str:
        store.append(m.group(0))
        return f"{MULTILINE_MASK}{len(store) - 1}{MULTILINE_MASK}"

    body = MULTILINE.sub(stash, body)

    out_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        if HEADING.match(line):
            out_lines.append(line)
            continue
        masked, store_line = mask(line)
        in_table = line.lstrip().startswith("|")
        masked = rx.sub(make_repl(in_table), masked)
        out_lines.append(unmask(masked, store_line))

    out = head + "".join(out_lines)
    for index, chunk in enumerate(store):
        out = out.replace(f"{MULTILINE_MASK}{index}{MULTILINE_MASK}", chunk)
    return out, counts


# --- targets -------------------------------------------------------------------------


def _excluded(path: Path, terms_dir: Path | None) -> bool:
    """Skip the term notes themselves and the raw-transcript directories."""
    if terms_dir is not None and terms_dir in path.parents:
        return True
    return bool({"raw", TERMS_DIRNAME} & {part.casefold() for part in path.parts})


def collect_targets(
    paths: list[str], terms_dir: Path | None, vault: Path | None = None
) -> list[Path]:
    if paths:
        out: list[Path] = []
        for raw in paths:
            path = Path(raw).expanduser()
            out.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
        return [p for p in out if p.is_file() and not _excluded(p, terms_dir)]
    if vault is None:
        return []
    root = Path(vault).expanduser() / COURSES_DIRNAME
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob(DEFAULT_GLOB)) if not _excluded(p, terms_dir)]


# --- reports (ported verbatim in behaviour, rewritten to read once) ------------------


@dataclass
class Report:
    terms: int = 0
    files: int = 0
    links: int = 0
    per_term: dict[str, int] = field(default_factory=dict)
    written: bool = False
    #: False when no terms directory resolved. A field rather than an inferred condition,
    #: because "no vocabulary was configured" and "the vocabulary matched nothing" produce
    #: identical numbers -- and only one of them is a clean result.
    configured: bool = True

    @property
    def changed(self) -> bool:
        return self.links > 0

    def summary(self) -> str:
        if not self.configured:
            return "not configured -- no terms directory"
        verb = "inserted" if self.written else "would insert"
        if not self.links:
            return f"{self.terms} terms, nothing to link"
        return f"{self.terms} terms, {verb} {self.links} links across {self.files} files"

    def as_dict(self) -> dict:
        return {
            "configured": self.configured,
            "terms": self.terms,
            "files": self.files,
            "links": self.links,
            "written": self.written,
            "per_term": dict(sorted(self.per_term.items())),
        }


def link_text(text: str, terms: list[Term]) -> tuple[str, dict[str, int]]:
    rx, owner = build_index(terms)
    return substitute(text, rx, owner)


def link_paths(paths: list[Path], terms: list[Term], *, write: bool) -> Report:
    rx, owner = build_index(terms)
    report = Report(terms=len(terms))
    for path in paths:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"dyak: cannot read {path}: {exc}", file=sys.stderr)
            continue
        new, counts = substitute(original, rx, owner)
        found = sum(counts.values())
        if not found:
            continue
        report.files += 1
        report.links += found
        report.per_term.update(counts)
        if write and new != original:
            write_atomic(path, new)
            report.written = True
    return report


def write_atomic(path: Path, text: str) -> None:
    """Write through a temp file in the same directory, then rename over the target.

    A crash mid-write leaves the previous note intact rather than a truncated one.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def index_report(lectures: list[Path], terms: list[Term], *, only_used: bool = False) -> str:
    """Term -> lectures table. Markdown; the caller decides whether to print or write it."""
    # Read once. The original read every lecture once per term.
    texts = {}
    for path in lectures:
        try:
            texts[path.stem] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    rows: list[tuple[Term, list[str]]] = []
    for term in terms:
        needle, plain = f"|{term.canonical}]]", f"[[{term.canonical}]]"
        hits = [stem for stem, text in texts.items() if needle in text or plain in text]
        rows.append((term, hits))
    rows.sort(key=lambda r: (-len(r[1]), r[0].canonical))
    lines = ["| термин | лекций | где |", "|---|---|---|"]
    for term, hits in rows:
        if not hits and only_used:
            continue
        where = ", ".join(f"[[{stem}]]" for stem in hits) if hits else "—"
        lines.append(f"| **{term.canonical}** | {len(hits)} | {where} |")
    return "\n".join(lines)


def check_report(terms: list[Term], lectures: list[Path], terms_dir: Path) -> str:
    texts: dict[str, str] = {}
    for path in lectures:
        try:
            texts[path.stem] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    lines = [f"терминов: {len(terms)}  в {terms_dir}"]
    noalias = [t for t in terms if not t.patterns]
    for term in noalias:
        lines.append(f"  ! без алиасов/форм: {term.canonical}")
    lines.append(f"лекционных заметок: {len(lectures)}")
    orphan = []
    for term in terms:
        needle, plain = f"]]|{term.canonical}]]", f"[[{term.canonical}]]"
        if not any(needle in text or plain in text for text in texts.values()):
            orphan.append(term.canonical)
    lines.append(
        f"терминов без упоминаний в лекциях: {len(orphan)}"
        + (": " + ", ".join(orphan) if orphan else "")
    )
    return "\n".join(lines)


# --- stage entry point ----------------------------------------------------------------


def run(
    note: Path,
    destination: Path,
    *,
    terms_dir: Path | None,
    stream=None,
) -> Report:
    """Link one note. Writes `note.linked.md` beside the pipeline artefacts, keeps the input.

    The original note is never modified. Stage 12 is what puts anything in the vault, and a
    stage that rewrote its own input in place would leave no evidence of what it changed.
    """
    out = stream if stream is not None else sys.stdout
    destination.mkdir(parents=True, exist_ok=True)
    markdown = note.read_text(encoding="utf-8") if note.is_file() else ""

    if terms_dir is None:
        report = Report(terms=0, configured=False)
        out.write(
            f"           note: link stage not configured -- no terms directory resolved "
            f"(pass --terms-dir, set {TERMS_ENV}, or pass --vault-path). "
            f"The note is written unlinked.\n"
        )
    else:
        terms = load_terms(terms_dir)
        linked, counts = link_text(markdown, terms)
        report = Report(terms=len(terms), files=1 if counts else 0, links=sum(counts.values()))
        report.per_term.update(counts)
        report.written = True
        for canonical, count in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
            out.write(f"           {count:>4}  {canonical}\n")

    (destination / LINKED_NOTE).write_text(
        linked if terms_dir is not None else markdown, encoding="utf-8"
    )
    (destination / REPORT_NAME).write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / PROVENANCE_NAME).write_text(
        provenance(report, terms_dir) + "\n", encoding="utf-8"
    )
    return report


def provenance(report: Report, terms_dir: Path | None) -> str:
    return json.dumps(
        {
            "stage": 11,
            "tool": "dyak-link-v1",
            "requires_model": False,
            "ported_from": "~/.local/bin/mscs-termlink",
            "terms_dir": str(terms_dir) if terms_dir else None,
            "output": LINKED_NOTE,
            "original_kept": True,
            "idempotent": True,
            "never_links_inside": [
                "$$...$$",
                "fenced code",
                "inline code",
                "inline math",
                "existing [[wikilinks]]",
                "markdown links",
                "bare URLs",
                "HTML tags",
            ],
            "links": report.links,
        },
        indent=2,
        ensure_ascii=False,
    )
