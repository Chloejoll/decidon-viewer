"""Extraction des dates dans les sorties OCR Chandra du Journal officiel.

Trois types de dates sont distingués :
- date de publication du numéro du JO   (« Journal officiel du 16 Mai 1919 »)
- date de séance                          (« CHAMBRE — SÉANCE DU 15 MAI 1919 », « Séance du jeudi 15 mai. »)
- dates citées dans les débats            (« la loi du 5 avril 1884 »)

Les expressions régulières tolèrent les erreurs courantes de l'OCR :
accents manquants (SEANCE, aout, fevrier), « 1er » écrit « Ier » ou « 1° »,
majuscules aléatoires, espaces superflus.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
}
MONTH_NAMES = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
               "août", "septembre", "octobre", "novembre", "décembre"]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

_MONTH_RE = r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre"
_WEEKDAY_RE = "|".join(WEEKDAYS)
# « 15 », « 1er », « Ier » (OCR), « 1° »
_DAY_RE = r"(?P<day>[0-3]?\d|[Il](?=\s*er))\s*(?:er|re|°)?"
_YEAR_RE = r"(?P<year>1[89]\d\d)"

DATE_RE = re.compile(
    rf"(?<![\w])(?:(?P<wd>{_WEEKDAY_RE})\s+)?{_DAY_RE}\s+(?P<month>{_MONTH_RE})"
    rf"(?:\s*,?\s*{_YEAR_RE})?(?![\w])",
    re.IGNORECASE,
)
PUB_RE = re.compile(rf"journal\s+officiel\s+du\s+{DATE_RE.pattern}", re.IGNORECASE)
# « 2e SÉANCE DU 15 MAI 1919 », « SEANCE DU 10 NOVEMBRE », « Séance du jeudi 15 mai. »
SEANCE_RE = re.compile(
    rf"(?:(?P<ord>\d+)\s*(?:re|e|ère|ème|°)\s+)?(?P<kind>s[ée]ance|comit[ée]\s+secret)\s+du\s+{DATE_RE.pattern}",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SUP_RE = re.compile(r"<sup>\s*(er|re|e|ème|o|°|me|mes|lle|r)\s*</sup>", re.IGNORECASE)


def html_to_text(content: str) -> str:
    """Texte brut à partir du contenu HTML d'un bloc (gère <br/> et \\n)."""
    s = _SUP_RE.sub(r"\1", content or "")
    s = re.sub(r"<br\s*/?>|</p>|</li>|</tr>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</td>", " ", s, flags=re.IGNORECASE)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def normalize_sup(content: str) -> str:
    """Remplace 1<sup>er</sup> par 1er pour que les dates restent reconnaissables."""
    return _SUP_RE.sub(r"\1", content or "")


def _to_date(m: re.Match, default_year: int | None = None, ref: date | None = None) -> date | None:
    day_s = m.group("day")
    day = 1 if day_s in ("I", "l") else int(day_s)
    month = MONTHS.get(m.group("month").lower().replace("û", "u"))
    if month is None:
        month = MONTHS.get(m.group("month").lower())
    year_s = m.group("year")
    if year_s:
        year = int(year_s)
    elif default_year is not None:
        year = default_year
        # séance de décembre publiée en janvier
        if ref is not None and ref.month == 1 and month == 12:
            year -= 1
    else:
        return None
    try:
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


@dataclass
class FoundDate:
    date: date
    text: str
    has_year: bool
    weekday_ok: bool | None  # le jour de la semaine indiqué correspond-il ? (None si absent)


def find_dates(text: str, default_year: int | None = None, ref: date | None = None) -> list[FoundDate]:
    out = []
    for m in DATE_RE.finditer(text or ""):
        d = _to_date(m, default_year, ref)
        if d is None:
            continue
        wd = m.group("wd")
        ok = None if not wd else (WEEKDAYS.index(wd.lower()) == d.weekday())
        out.append(FoundDate(d, m.group(0), bool(m.group("year")), ok))
    return out


def find_publication_date(text: str) -> date | None:
    m = PUB_RE.search(text or "")
    return _to_date(m) if m else None


def find_seance(text: str, default_year: int | None = None, ref: date | None = None):
    """Retourne (date, ordinal) pour « 2e SÉANCE DU ... », sinon None."""
    m = SEANCE_RE.search(text or "")
    if not m:
        return None
    d = _to_date(m, default_year, ref)
    if d is None:
        return None
    return d, (m.group("ord") or None)


# ----------------------------------------------------------------------------- dates « brutes »
# On sépare l'extraction (ce que l'OCR a lu) de la résolution (année tranchée avec le CSV
# et le jour de la semaine), car l'OCR confond souvent 8 et 3 (1898 → 1893) ou 6 et 8.

MIN_YEAR, MAX_YEAR = 1870, 1949


@dataclass
class RawDate:
    day: int
    month: int
    year: int | None
    wd: str | None
    text: str


def _raw(m: re.Match) -> RawDate | None:
    day_s = m.group("day")
    day = 1 if day_s in ("I", "l") else int(day_s)
    month = MONTHS.get(m.group("month").lower()) or MONTHS.get(m.group("month").lower().replace("û", "u"))
    if not month or not 1 <= day <= 31:
        return None
    g = m.groupdict()
    year = int(g["year"]) if g.get("year") else None
    wd = g["wd"].lower() if g.get("wd") else None
    return RawDate(day, month, year, wd, m.group(0))


# « Samedi 7 Juillet 1923. ★ » : en-tête de première page réduit à une date (années 1920-1930)
_WD_HEADER_RE = re.compile(rf"^[\W_]*{DATE_RE.pattern}[\W_]*$", re.IGNORECASE)
# « JOURNAL OFFICIEL » puis « DU 11 FÉVRIER 1937 » dans le bloc suivant (texte concaténé)
_JO_SPLIT_RE = re.compile(
    rf"journal\s+officiel(?:\W+de\s+la\s+r\w*\s+fran\w*)?\W+du\s+{DATE_RE.pattern}", re.IGNORECASE)
# « 2 Avril [CHAMBRE] 717 », « 718 2 Avril [CHAMBRE] », « 12 Juillet (SÉNAT) 773 » (années 1880)
_OLD_HEADER_RE = re.compile(
    rf"^\s*(?:\d{{1,5}}\s+)?{_DAY_RE}\s+(?P<month>{_MONTH_RE})\s*[\[(]\s*(?:CHAMBRE|S[ÉE]NAT)[^\])]*[\])]\s*(?:\d{{1,5}})?\s*$",
    re.IGNORECASE)
_ANNEE_RE = re.compile(r"^\W*Ann[ée]e\s+(1[89]\d\d)\W*$", re.IGNORECASE)


def raw_publication(headers_by_page: list[list[str]], first_blocks: list[str]) -> list[RawDate]:
    """Toutes les lectures de la date de publication, par ordre de fiabilité décroissante."""
    out = []
    for hs in headers_by_page:                       # « Journal officiel du 16 Mai 1919 »
        for h in hs:
            m = PUB_RE.search(h)
            if m and (r := _raw(m)):
                out.append(r)
    if out:
        return out
    for h in (headers_by_page[0] if headers_by_page else []):   # « Samedi 7 Juillet 1923 ★ »
        m = _WD_HEADER_RE.match(h)
        if m and m.group("year") and (r := _raw(m)):
            out.append(r)
    joined = " ".join(first_blocks)
    m = _JO_SPLIT_RE.search(joined)                  # « JOURNAL OFFICIEL » / « DU 11 FÉVRIER 1937 »
    if m and (r := _raw(m)):
        out.append(r)
    if out:
        return out
    years = Counter()                                # années 1880 : jour-mois + « Année 1885 »
    for hs in headers_by_page:
        for h in hs:
            if (ma := _ANNEE_RE.match(h)):
                years[int(ma.group(1))] += 1
    y = years.most_common(1)[0][0] if years else None
    for hs in headers_by_page:
        for h in hs:
            m = _OLD_HEADER_RE.match(h)
            if m and (r := _raw(m)):
                r.year = y
                out.append(r)
    return out


def raw_seance(text: str):
    """(RawDate, ordinal, nature) pour « 2e SÉANCE DU … », « Séance du jeudi 15 mai. »,
    « COMITÉ SECRET DU 16 JUIN 1916 » ; sinon None."""
    m = SEANCE_RE.search(text or "")
    if not m or not (r := _raw(m)):
        return None
    kind = "comité secret" if m.group("kind").lower().startswith("comit") else "séance"
    return r, (m.group("ord") or None), kind


def seance_label(kind: str | None, ordinal: str | None = None) -> str:
    """« Séance », « 2e séance », « Comité secret »."""
    base = "Comité secret" if kind == "comité secret" else "Séance"
    return f"{ordinal}e {base.lower()}" if ordinal else base


def raw_weekday_dates(texts: list[str]) -> list[RawDate]:
    """Dates accompagnées d'un jour de la semaine, utiles pour trancher l'année."""
    out = []
    for t in texts:
        for m in DATE_RE.finditer(t or ""):
            if m.group("wd") and (r := _raw(m)):
                out.append(r)
    return out


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def weekday_score(raws: list[RawDate], year: int) -> int:
    """Nombre de dates dont le jour de la semaine est cohérent avec l'année proposée."""
    n = 0
    for r in raws:
        if r.wd and (d := _safe_date(year, r.month, r.day)) and WEEKDAYS[d.weekday()] == r.wd:
            n += 1
    return n


def resolve(raw: RawDate, anchor_year: int | None, anchor: date | None) -> date | None:
    """Transforme une lecture OCR en date, en corrigeant l'année puis le jour.

    - année absente ou à plus d'un an de l'année de référence → année la plus proche de la référence
      (gère la séance du 31 décembre publiée le 1er janvier) ;
    - jour de la semaine incohérent → jour le plus proche (±3) qui correspond (« vendredi 18 » → 16).
    """
    if raw.year and (anchor_year is None or abs(raw.year - anchor_year) <= 1):
        d = _safe_date(raw.year, raw.month, raw.day)
    elif anchor_year is not None:
        ref = anchor or date(anchor_year, 7, 1)
        cands = [c for y in (anchor_year - 1, anchor_year, anchor_year + 1)
                 if (c := _safe_date(y, raw.month, raw.day))]
        d = min(cands, key=lambda c: abs((c - ref).days)) if cands else None
    else:
        d = None
    if d is None or not raw.wd or WEEKDAYS[d.weekday()] == raw.wd:
        return d
    for k in (1, -1, 2, -2, 3, -3):
        c = d + timedelta(days=k)
        if c.month == d.month and WEEKDAYS[c.weekday()] == raw.wd:
            return c
    return d


def detect_chamber(text: str) -> str | None:
    t = (text or "").upper()
    if re.search(r"S[ÉE]NAT", t):
        return "senat"
    if "CHAMBRE" in t or "DÉPUTÉS" in t or "DEPUTES" in t:
        return "chambre"
    return None


def fr_date(d: date | str | None, weekday: bool = True) -> str:
    """2026-05-15 -> « jeudi 15 mai 1919 »."""
    if d is None or d == "" or d != d:  # d != d : NaT / NaN
        return "—"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    day = "1er" if d.day == 1 else str(d.day)
    s = f"{day} {MONTH_NAMES[d.month]} {d.year}"
    return f"{WEEKDAYS[d.weekday()]} {s}" if weekday else s
