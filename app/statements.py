"""Deterministic statement extraction: every document becomes numbered requirement statements by rule ("shall/must/will",
numbered clauses, table rows), each with a stable id (hash of normalized text + document + section). The LLM may classify,
group and link these statements; it cannot add or drop them."""
from __future__ import annotations

import hashlib
import re

_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][^.;]{0,80})$")
# PDF extractors give a bullet as any symbol, or as a control or private-use character
_BULLETS = "\\-•*·‣⁃∙●○◦▪▫■□►▶➢✓\\x00-\\x08\\x0e-\\x1f\\x7f-\\x9f\\ue000-\\uf8ff"
_ITEM = re.compile(rf"^(?:[{_BULLETS}]|\d+[.)])\s+")
_MODULE_LINE = re.compile(r"^(?:Module|Part|Component|Sub-?system)\s+[\dA-Z]+\s*[:.\-–—]\s*[A-Z].{0,70}$")
_MODULE_TITLE = re.compile(r"^(?:(?:Module|Part|Component|Sub-?system)\s+[\dA-Z]+\s*[:.\-–—]|[A-Z]{2,6}-\d{1,4}:)\s*", re.I)
_MODULE_ROW = re.compile(r"^[A-Z]{2,6}-\d{1,4}:\s*\S.{0,70}$")     # a matrix row that names a module: "MOD-01: Identity &"
_MODULE_WORD = re.compile(r"\bmodule\b", re.I)
SCOPE_TITLE = re.compile(r"\bscope\b|\bmodules?\b|\bfunctional\b|\bfeatures?\b|\bcapabilit|\b(?:system|business|user|software|application) requirements\b|"
                         r"\brequirements? (?:matrix|specification|list)\b|^requirements\b", re.I)
_NOT_SCOPE_TITLE = re.compile(r"out[- ]of[- ]scope|not in scope|exclusions?\b|non[- ]?functional|technical|^(?:users?|user groups?|roles?|users? and roles?|stakeholders?|audiences?|actors?)\b(?! requirements)", re.I)
# a heading that only says what kind of section this is: "Requirements", "Core Functional Requirements Matrix", "Non-Functional & Security Requirements"
GENERIC_HEADING = re.compile(r"^(?:scope(?: of (?:work|services|the project))?|(?:[\w-]+[ &,/]+){0,4}requirements?(?: (?:matrix|specification|list))?|"
                             r"(?:functional|non[- ]functional|technical) (?:scope|specifications?)|specifications?|features?|modules?)$", re.I)
_LABEL = re.compile(r"^([A-Z][^:.;|]{1,60}?):\s+(\S.{8,})$")
_NOT_A_LABEL = re.compile(r"^(?:note|notes|remark|remarks|example|e\.g|i\.e|important|reference|see)\b", re.I)
_PARAGRAPH = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+(\S.{14,})$")
_REQUIREMENT_ID = re.compile(r"^(?!NFR)[A-Z]{1,5}[-_]?\d{1,4}(?:\.\d+)?$")
_ID_FIRST = re.compile(r"^((?!NFR)[A-Z]{1,5}[-_]\d{1,4}(?:\.\d+)?)\s+(\S.*)$")
_CELL_END = re.compile(r"^(.{3,50}?[a-z)])\s+[A-Z][a-z]")
_HEADER_WORDS = {"id", "no", "no.", "ref", "ref.", "reference", "requirement", "requirements", "description", "priority", "module", "feature", "features", "item", "items",
                 "remarks", "function", "functions", "capability", "capabilities", "details", "name", "title", "notes", "type", "category"}
_NAME_FROM_TEXT = "name it from its text"
_PAGE_LINE = re.compile(r"^(?:.{0,80}\s)?page\s+\d+\s+of\s+\d+$|^page\s+\d+$", re.I)     # also "CONFIDENTIAL - Name Page 2 of 4"
_SENTENCE = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z(\"])")
_TABLE_ROW = re.compile(r"\|\s*\|")
_WORD = re.compile(r"[a-z0-9]+")
_MODAL = re.compile(r"\b(?:shall|must|will|may|should|requires?|required)\b", re.I)
_REF = re.compile(r"\b(?:Section|Clause|Item)s?\s+(\d+(?:\.\d+)*)", re.I)
DOC_LABELS = {"tor": "TOR", "bid_bulletin": "Bid Bulletin", "minutes": "Minutes of meeting", "clarification": "Clarification",
              "other": "Other document"}
SUPPLEMENTARY = ("bid_bulletin", "minutes", "clarification", "other")


# clear cases by rule; the LLM only classifies the rest. Numbers about quality or the contract are not business rules.
_RULE_SIGNAL = re.compile(r"\b(?:only if|only when|at least|at most|except|no more than|no later than|not more than|"
                          r"more than|less than|exceed\w*|up to|within \w+ (?:\(\d+\) )?(?:working )?(?:days?|hours?)|\d+\s*%)", re.I)
_RULE_ONLY = re.compile(r"^only\b|\bonly approved\b", re.I)
_NOT_A_RULE = re.compile(r"\b(?:load|availability|uptime|concurrent|response time|encrypt\w*|backup|contract|payment|price|warranty|"
                         r"proposal|delivered|delivery|acceptance|notice to proceed|retained for)\b", re.I)
# a capability with a condition attached ("Employees shall file ... for more than two days") is not clear: the LLM decides
_ACTOR_CAPABILITY = re.compile(r"\bshall (?:be able to |be brought\b|file|receive|view|upload|create|maintain|search|download|generate|"
                               r"see|submit)\b", re.I)
_CAPABILITY = re.compile(r"\bthe system shall (?:provide|allow|enable)\b", re.I)


def rule_category(text: str) -> str | None:
    """business_rule or functional for clear statements; None leaves it to the LLM."""
    if _NOT_A_RULE.search(text) or _ACTOR_CAPABILITY.search(text):
        return None
    if _RULE_SIGNAL.search(text) or _RULE_ONLY.search(text):
        return "business_rule"
    return "functional" if _CAPABILITY.search(text) else None


def norm(text: str) -> str:
    return " ".join(text.split())


def clean_reference(reference: str) -> str:
    """"Bid Bulletin No. 02" and "02" are the same reference."""
    return re.sub(r"(?i)^.*?\bno\.?\s*", "", reference.strip()).strip()


def reference_order(reference: str) -> tuple:
    """Natural order, so No. 10 follows No. 2: the tie-breaker for bulletins of the same date."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|\D+", reference))


def doc_label(doc: dict) -> str:
    label = DOC_LABELS[doc["kind"]]
    if doc.get("number"):
        label += f" No. {clean_reference(doc['number'])}"
    return label + (f", {doc['date']}" if doc.get("date") else "")


def headings(pages: list[dict]) -> dict[str, str]:
    """Section number -> title."""
    return {e[1]: e[2].strip() for e in walk(pages) if e[0] == "heading"}


def statement_id(text: str, doc_key: str, section: str) -> str:
    return "ST-" + hashlib.sha256(f"{norm(text).lower()}|{doc_key}|{section}".encode()).hexdigest()[:10]


def _titled(text: str) -> bool:
    """"Customer Application (iOS & Android)" is a title; "Members create accounts by email" is a description."""
    words = [w for w in re.findall(r"[A-Za-z][\w'-]*", re.sub(r"\([^)]*\)", "", text)) if w.lower() not in ("a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with")]
    return bool(words) and len(words) <= 10 and sum(w[0].isupper() for w in words) / len(words) >= 0.75


def _numbered_heading(line: str) -> tuple[str, str] | None:
    m = _HEADING.match(line)
    if not m or (":" in m.group(2) and not _titled(m.group(2).split(":", 1)[1])):
        return None
    return m.group(1), m.group(2)


def _plain_heading(line: str, marked: bool) -> bool:
    """A heading without a number: Markdown, all capitals, "Module 1: Name", or a short title that names the scope."""
    if line.endswith((".", ",", ":", ";")) or "|" in line or len(line) > 90:
        return False
    if marked or _MODULE_LINE.match(line) or (line.isupper() and 2 <= len(line.split()) <= 10):
        return True
    return _titled(line) and bool(GENERIC_HEADING.match(line))     # a table's column title is not a heading, however it is worded


def is_scope_title(title: str) -> bool:
    return bool(SCOPE_TITLE.search(title)) and not _NOT_SCOPE_TITLE.search(title)


def is_module_title(title: str) -> bool:
    return bool(_MODULE_TITLE.match(title) or _MODULE_WORD.search(title))


def module_name(title: str) -> str:
    """"Module 1: Customer Application" -> "Customer Application"."""
    return section_title(_MODULE_TITLE.sub("", title)).strip()


def _furniture(pages: list[dict]) -> set[str]:
    """Running headers and footers: the same line near the top or bottom of at least half the pages."""
    if len(pages) < 3:
        return set()
    seen: dict[str, int] = {}
    for pg in pages:
        lines = [x.strip() for x in pg["text"].splitlines() if x.strip()]
        for line in set(lines[:2] + lines[-3:]):
            seen[line] = seen.get(line, 0) + 1
    return {line for line, n in seen.items() if n >= max(3, len(pages) / 2)}


def walk(pages: list[dict]):
    """The document as events, in order: ("heading", number, title, page), ("line", text, page) and ("break", "", page). Page furniture is left out.
    A numbered line that goes back in the numbering is a list item, not a section. A heading without a number gets one of its own (U1, U2)."""
    furniture = _furniture(pages)
    top, plain = 0, 0
    capitals = True     # top headings so far are all capitals: then a mixed-case numbered line is a table row or a list, not a section
    in_scope = False
    for pg in pages:
        lines = pg["text"].splitlines()
        skip = False
        for position, raw in enumerate(lines):
            if skip:
                skip = False
                continue
            stripped = raw.strip()
            line = stripped.lstrip("#").strip()     # Markdown headings count as headings
            if not line:
                yield ("break", "", pg["page"])
                continue
            if line in furniture or _PAGE_LINE.match(line):
                continue
            head = _numbered_heading(line)
            if head:
                first = int(head[0].split(".")[0])
                if "." in head[0] or (first > top and (top == 0 or head[1].isupper() or not capitals)):
                    if "." not in head[0]:
                        capitals = capitals and head[1].isupper()
                    top = max(top, first)
                    in_scope = is_scope_title(head[1]) or (in_scope and "." in head[0])
                    yield ("heading", head[0], head[1], pg["page"])
                    continue
            if in_scope and _MODULE_ROW.match(line):
                following = lines[position + 1].strip() if position + 1 < len(lines) else ""
                wrapped = following and len(following.split()) <= 3 and not _ITEM.match(following) and not _MODULE_ROW.match(following)
                skip = bool(wrapped)
                plain += 1
                yield ("heading", f"U{plain}", f"{line} {following}" if wrapped else line, pg["page"])
                continue
            if _plain_heading(line, stripped.startswith("#")):
                plain += 1
                in_scope = is_scope_title(line) or _MODULE_LINE.match(line) is not None
                yield ("heading", f"U{plain}", line, pg["page"])
                continue
            yield ("line", line, pg["page"])
        yield ("break", "", pg["page"])     # a paragraph ends with its page, as a table's repeated header starts the next one


def sections(pages: list[dict]) -> list[tuple[str, str, list[str]]]:
    """[(number, title, the lines directly under that heading)]."""
    out: list[tuple[str, str, list[str]]] = []
    for event in walk(pages):
        if event[0] == "heading":
            out.append((event[1], event[2], []))
        elif event[0] == "line" and out:
            out[-1][2].append(event[1])
    return out


def _table_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip(" |").split("|")]


def _row_label(cells: list[str]) -> str:
    """A requirements table row names its feature in its first text column; a header row names none."""
    if len(cells) < 2 or all(c.lower() in _HEADER_WORDS or not c for c in cells):
        return ""
    if not any(ch.isdigit() for ch in " ".join(cells)) and all(len(c.split()) <= 3 for c in cells):     # short cells and no figure: a header
        return ""
    title = cells[1] if (_REQUIREMENT_ID.match(cells[0]) or cells[0].isdigit()) and len(cells) >= 3 else cells[0]
    return title if 1 <= len(title.split()) <= 8 else ""


def is_item(line: str) -> bool:
    return bool(_ITEM.match(line) or _PARAGRAPH.match(line))


def item_text(line: str) -> str:
    numbered = _PARAGRAPH.match(line)
    return numbered.group(2) if numbered else _ITEM.sub("", line)


def short_title(text: str) -> str:
    words = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.I).split()
    title = " ".join(words[:7]).rstrip(",;:.") + (" ..." if len(words) > 7 else "")
    return title[:1].upper() + title[1:]


def parse_sentences(pages: list[dict]) -> list[dict]:
    """[{"page", "section", "section_no", "text", "item", "label"}] in document order. Headings set the section (which carries across
    pages); wrapped lines are joined; bullets, numbered items and table rows are their own sentences. Under a scope, module or
    functional heading, a "Label: description" item, a numbered paragraph, a table row or a module's bullet names one feature: label holds its name."""
    out: list[dict] = []
    section, number, scope, module = "", "", False, False
    titles: dict[str, str] = {}
    page = 0
    buf: list[str] = []
    item, label = False, ""

    def add(text: str, is_item: bool, name: str = "") -> None:
        out.append({"page": page, "section": section, "section_no": number, "text": text, "item": is_item, "label": name})

    def flush() -> None:
        nonlocal item, label
        if label:
            whole = norm(" ".join(buf))
            add(whole, True, short_title(whole) if label is _NAME_FROM_TEXT else label)     # one feature stays one statement, however many sentences it has
        elif buf and scope and (_REQUIREMENT_ID.match(buf[0]) or _ID_FIRST.match(buf[0])):     # a table row from a PDF: its ID, then its cells line by line
            same_line = _ID_FIRST.match(buf[0])
            name = same_line.group(2) if same_line else (buf[1] if len(buf) > 1 else "")
            cell = _CELL_END.match(name)     # extractors join cells on a line: "Walk-in queue board Queue number, ..."
            name = cell.group(1) if cell else name
            text = norm(" ".join(buf))
            add(text, True, name if 1 <= len(name.split()) <= 8 else short_title(_ID_FIRST.sub(r"\2", text)))
        else:
            for sentence in _SENTENCE.split(norm(" ".join(buf))):
                for s in _TABLE_ROW.split(sentence):
                    s = s.strip(" |")
                    if "---" not in s and len(_WORD.findall(s.lower())) >= 3:
                        name = _row_label(_table_cells(s)) if scope and " | " in s else ""
                        add(s, item or " | " in s, name)
        buf.clear()
        item, label = False, ""

    for event in walk(pages):
        kind, value, page_no = event[0], event[1], event[-1]
        if kind == "break":
            flush()
            page = page_no
        elif kind == "heading":
            flush()
            page = page_no
            title = event[2]
            titles[value.split(".")[0]] = titles.get(value.split(".")[0], title)
            section, number = norm(title if value.startswith("U") else f"{value}{'.' if '.' not in value else ''} {title}"), value
            top_title = titles[value.split(".")[0]]
            module = is_module_title(title)
            scope = module or is_scope_title(title) or (is_scope_title(top_title) and not _NOT_SCOPE_TITLE.search(title))
        else:
            line = value
            if line.startswith("|"):      # a table row is its own sentence
                flush()
                page = page_no
                buf.append(line)
                item = True
                flush()
            elif _ITEM.match(line):
                flush()
                page = page_no
                text = _ITEM.sub("", line)
                buf.append(text)
                item = True
                found = _LABEL.match(text) if scope else None
                if found and len(found.group(1).split()) <= 6 and not _NOT_A_LABEL.match(found.group(1)):
                    label = found.group(1).strip()
                elif module:
                    label = _NAME_FROM_TEXT     # its wrapped lines are still to come
            elif scope and (_ID_FIRST.match(line) or _REQUIREMENT_ID.match(line)):
                flush()
                page = page_no
                buf.append(line)
                item = True
            elif _PARAGRAPH.match(line):     # a numbered paragraph is a statement with or without "shall"
                flush()
                page = page_no
                text = _PARAGRAPH.match(line).group(2)
                buf.append(text)
                item = True
                if scope:
                    found = _LABEL.match(text)
                    label = found.group(1).strip() if found and len(found.group(1).split()) <= 6 else _NAME_FROM_TEXT
            elif scope and not buf and (found := _LABEL.match(line)) and len(found.group(1).split()) <= 6 and not _NOT_A_LABEL.match(found.group(1)):
                page = page_no
                buf.append(line)     # an item whose bullet the extractor lost
                item, label = True, found.group(1).strip()
            else:
                if not buf:
                    page = page_no
                buf.append(line)
    flush()
    return out


def parse_statements(docs: list[dict]) -> list[dict]:
    """Every requirement statement of the documents, in order. docs: {"key", "kind", "number", "date", "name", "pages"}."""
    out: list[dict] = []
    for doc in docs:
        seen: set[str] = set()
        for s in parse_sentences(doc["pages"]):
            if not (_MODAL.search(s["text"]) or s["item"]):
                continue
            if s["item"] and " | " in s["text"] and not re.search(r"\d", s["text"]) and not s.get("label"):
                continue     # a table header row
            sid = statement_id(s["text"], doc["key"], s["section"])
            if sid in seen:
                continue
            seen.add(sid)
            out.append({**s, "id": sid, "doc_key": doc["key"], "doc_kind": doc["kind"], "doc_label": doc_label(doc),
                        "doc_date": doc.get("date", ""), "file": doc.get("name", ""),
                        "refs": _REF.findall(s["text"]),
                        "top_section": s["section_no"].split(".")[0] if s["section_no"] else ""})
    return out


def section_title(section: str) -> str:
    """"5.3 Leave approval" -> "Leave approval"."""
    return re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", section).strip()
