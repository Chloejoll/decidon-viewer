"""Mise en forme des blocs OCR (HTML pour Streamlit, Markdown pour l'export)."""
from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import date

from .dates import DATE_RE, _to_date, fr_date, normalize_sup, seance_label

CHAMBER_LABEL = {"chambre": "Chambre des députés", "senat": "Sénat"}

_DANGEROUS_TAGS = re.compile(r"<\s*(script|style|iframe|object|embed|form|link|meta)\b.*?(?:</\s*\1\s*>|/?>)",
                             re.IGNORECASE | re.DOTALL)
_EVENT_ATTR = re.compile(r"\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)
_SPLIT_TAGS = re.compile(r"(<[^>]+>)")
# Orateur en début de paragraphe : « M. le président. », « M. J. Thierry. »,
# « M. Jean Dupuy, ministre du commerce et de l'industrie. », « M. Périer (Saône-et-Loire). »
_CAP = r"[A-ZÉÈÀÂ][\w'’\-]+"
_PART = r"(?:de |du |des |d'|d’|la |le )"
_INIT = r"(?:[A-ZÉ]\.(?:-[A-ZÉ]\.)?\s)"
_NAME = rf"(?:{_INIT}*{_PART}?{_CAP}(?:\s+{_PART}?{_CAP}){{0,3}})"
_ROLE = r"(?:le|la|l')\s+[a-zàâéèêîôûç][^.,()<>]{1,80}"
_SPEAKER = re.compile(
    rf"^(\s*)((?:M\.|Mme|Mlle)\s+(?:{_ROLE}|{_NAME}(?:\s*\([^)<>]{{1,40}}\))?"
    rf"(?:,\s*[^.<>]{{3,90}})?)(?<!\bM)\.)(?=\s+\S)"
)


def sanitize(content: str) -> str:
    s = _DANGEROUS_TAGS.sub("", content or "")
    s = _EVENT_ATTR.sub("", s)
    s = _JS_URL.sub(r'\1="#"', s)
    return s


def fold(s: str) -> str:
    """Minuscules sans accents, caractère par caractère (même longueur que l'entrée)."""
    out = []
    for ch in s:
        base = unicodedata.normalize("NFD", ch)[0].lower()
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


def _map_text_segments(content: str, fn) -> str:
    parts = _SPLIT_TAGS.split(content)
    depth_mark = 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            low = part.lower()
            if low.startswith("<mark"):
                depth_mark += 1
            elif low.startswith("</mark"):
                depth_mark = max(0, depth_mark - 1)
            continue
        if part and depth_mark == 0:
            parts[i] = fn(part)
    return "".join(parts)


def highlight_dates(content: str, default_year: int | None, ref: date | None) -> str:
    def repl(m: re.Match) -> str:
        d = _to_date(m, default_year, ref)
        if d is None:
            return m.group(0)
        cls = "dt" if m.group("year") else "dt dt-noyear"
        title = fr_date(d) + ("" if m.group("year") else " (année déduite)")
        return f'<mark class="{cls}" title="{title}">{m.group(0)}</mark>'
    return _map_text_segments(content, lambda t: DATE_RE.sub(repl, t))


def highlight_terms(content: str, terms: list[str]) -> str:
    terms = [fold(t) for t in terms if t and len(t) >= 2]
    if not terms:
        return content

    def fn(text: str) -> str:
        f = fold(text)
        spans = []
        for t in terms:
            start = 0
            while (k := f.find(t, start)) != -1:
                spans.append((k, k + len(t)))
                start = k + len(t)
        if not spans:
            return text
        spans.sort()
        out, pos = [], 0
        for a, b in spans:
            if a < pos:
                continue
            out.append(text[pos:a])
            out.append(f'<mark class="hit">{text[a:b]}</mark>')
            pos = b
        out.append(text[pos:])
        return "".join(out)
    return _map_text_segments(content, fn)


def bold_speaker(content: str) -> str:
    """Met l'orateur en gras s'il ne l'est pas déjà (heuristique)."""
    parts = _SPLIT_TAGS.split(content)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            if part.lower().startswith(("<b", "<strong")):
                return content  # déjà en gras dans l'OCR
            continue
        if not part.strip():
            continue
        m = _SPEAKER.match(part)
        if m:
            parts[i] = m.group(1) + f'<b class="spk">{m.group(2)}</b>' + part[m.end():]
        return "".join(parts)
    return content


def render_block(label: str, content: str, *, default_year=None, ref=None, terms=None,
                 show_headers=False, dates=True, speakers=True, anchor: str | None = None) -> str:
    if label in ("Page-Header", "Page-Footer") and not show_headers:
        return ""
    c = sanitize(normalize_sup(content))
    if not c.strip():
        return ""
    if label == "Text" and speakers:
        c = bold_speaker(c)
    if dates:
        c = highlight_dates(c, default_year, ref)
    if terms:
        c = highlight_terms(c, terms)
    if "\n" in c and "<" not in c:
        c = c.replace("\n", "<br/>")
    cls = "blk-" + re.sub(r"[^a-z]+", "-", label.lower()).strip("-")
    aid = f' id="{anchor}"' if anchor else ""
    if label == "Image":
        c = c if "<img" in c else f"[image] {c}"
    return f'<div class="blk {cls}"{aid}>{c}</div>'


def gallica_url(ark: str | None, page_num: int | None = None) -> str | None:
    if not ark:
        return None
    base = f"https://gallica.bnf.fr/{ark}"
    return f"{base}/f{page_num + 1}.item" if page_num is not None else base


def page_banner(page, doc, n_pages: int) -> str:
    bits = [f"<b>Page {int(page.page_num) + 1}/{n_pages}</b>"]
    if page.printed_page:
        guess = page.printed_page_ocr != page.printed_page
        title = f' title="lu par l\'OCR : {page.printed_page_ocr or "rien"}"' if guess else ""
        bits.append(f"<span{title}>p. {page.printed_page}{' *' if guess else ''}</span>")
    if page.seance_date:
        kind = getattr(page, "seance_kind", None)
        bits.append(f'<span class="bn-seance">{seance_label(kind)} du {fr_date(page.seance_date)}</span>')
    if doc.get("date_pub"):
        bits.append(f"JO du {fr_date(doc['date_pub'], weekday=False)}")
    url = gallica_url(doc.get("ark"), int(page.page_num))
    if url:
        bits.append(f'<a href="{url}" target="_blank">image Gallica ↗</a>')
    return f'<div class="pg-banner" id="p{int(page.page_num) + 1}">' + " · ".join(bits) + "</div>"


def render_pages(pages_df, doc: dict, *, terms=None, show_headers=False, dates=True, speakers=True) -> str:
    ref = date.fromisoformat(doc["date_pub"]) if doc.get("date_pub") else None
    year = ref.year if ref else doc.get("year")
    n = int(doc.get("num_pages") or len(pages_df))
    out = []
    for page in pages_df.itertuples():
        blocks = json.loads(page.blocks)
        body = []
        for j, (label, content) in enumerate(blocks):
            anchor = f"s{int(page.page_num) + 1}-{j}" if label == "Section-Header" else None
            body.append(render_block(label, content, default_year=year, ref=ref, terms=terms,
                                     show_headers=show_headers, dates=dates, speakers=speakers,
                                     anchor=anchor))
        inner = "".join(body) or '<div class="blk blk-empty">Aucun bloc reconnu sur cette page (page blanche ou échec de l\'OCR).</div>'
        out.append(f'<section class="ocr-page">{page_banner(page, doc, n)}{inner}</section>')
    return '<div class="ocr-doc">' + "".join(out) + "</div>"


# ----------------------------------------------------------------------------- Markdown

def _html_to_md(c: str) -> str:
    c = normalize_sup(c)
    if "<table" in c.lower():
        return c.strip()  # le Markdown accepte le HTML pour les tableaux
    c = re.sub(r"<li[^>]*>", "\n- ", c, flags=re.IGNORECASE)
    c = re.sub(r"</?(b|strong)>", "**", c, flags=re.IGNORECASE)
    c = re.sub(r"</?(i|em)>", "*", c, flags=re.IGNORECASE)
    c = re.sub(r"<br\s*/?>", "  \n", c, flags=re.IGNORECASE)
    c = re.sub(r"<sup>(.*?)</sup>", r"^\1^", c, flags=re.IGNORECASE)
    c = re.sub(r"<[^>]+>", "", c)
    c = html.unescape(c)
    c = re.sub(r"\*\*\s*\*\*", "", c)
    return re.sub(r"\n{3,}", "\n\n", c).strip()


def doc_to_markdown(doc: dict, pages_df, credits: str) -> str:
    seances = json.loads(doc.get("seances") or "[]")
    lines = ["---",
             f"document: {doc['doc_id']}",
             f"ark: {doc.get('ark') or ''}",
             f"chambre: {CHAMBER_LABEL.get(doc.get('chamber'), doc.get('chamber') or '')}",
             f"date_seance: {doc.get('date_seance') or ''}",
             f"date_publication_jo: {doc.get('date_pub') or ''}",
             f"date_catalogue_bnf: {doc.get('date_csv') or ''}",
             "seances: [" + ", ".join(s["date"] for s in seances) + "]",
             "---", "",
             f"# {CHAMBER_LABEL.get(doc.get('chamber'), '')} — {seance_label(doc.get('seance_kind')).lower()} du {fr_date(doc.get('date_seance') or doc.get('main_date'))}",
             "", f"*Journal officiel du {fr_date(doc.get('date_pub'))}*", ""]
    current = None
    for page in pages_df.itertuples():
        if page.seance_date and page.seance_date != current:
            current = page.seance_date
            lines += [f"## {seance_label(getattr(page, 'seance_kind', None))} du {fr_date(current)}", ""]
        pp = f" (p. {page.printed_page})" if page.printed_page else ""
        url = gallica_url(doc.get("ark"), int(page.page_num))
        lines += [f"### Page {int(page.page_num) + 1}{pp}" + (f" — [Gallica]({url})" if url else ""), ""]
        for label, content in json.loads(page.blocks):
            if label in ("Page-Header", "Page-Footer"):
                continue
            md = _html_to_md(content)
            if not md:
                continue
            lines += [f"#### {md}" if label == "Section-Header" else md, ""]
    lines += ["---", credits, ""]
    return "\n".join(lines)
