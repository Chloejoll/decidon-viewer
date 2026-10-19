"""Transformation d'un JSON Chandra en lignes « document » et « pages »."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date

from .dates import (MAX_YEAR, fr_date, MIN_YEAR, PUB_RE, _raw, _safe_date, detect_chamber,
                    html_to_text, raw_publication, raw_seance, raw_weekday_dates,
                    resolve, weekday_score)

_PRINTED_PAGE_RE = re.compile(r"^\s*(?:Session\s.*?)?(\d{1,5})\s*$", re.IGNORECASE)


def _printed_page(header_texts: list[str]) -> str | None:
    """Numéro de page imprimé, repéré dans les en-têtes (« 2298 », « Session ordinaire de 1919 2287 »)."""
    for t in header_texts:
        m = _PRINTED_PAGE_RE.match(t)
        if m:
            return m.group(1)
    return None


_CONFUSABLE = {"3": "85", "8": "306", "0": "86", "6": "850", "5": "63", "1": "74", "7": "1", "4": "1", "9": "0"}


def _fix_digit(pub: date, seance: date) -> date | None:
    """Corrige un chiffre du jour de publication confondu par l'OCR (une seule substitution)."""
    day = f"{pub.day:02d}"
    cands = []
    for i, ch in enumerate(day):
        for alt in _CONFUSABLE.get(ch, ""):
            d_s = (day[:i] + alt + day[i + 1:]).lstrip("0")
            if not d_s:
                continue
            c = _safe_date(pub.year, pub.month, int(d_s))
            if c and 0 <= (c - seance).days <= 3:
                cands.append(c)
    if not cands:
        return None
    return min(cands, key=lambda c: abs((c - seance).days - 1))


def process_document(doc_id: str, subdir: str, data: dict, csv_row: dict | None = None):
    """Retourne (ligne d'index, liste de lignes de pages)."""
    pages = data.get("pages", [])

    # 1) Textes des en-têtes de chaque page
    headers_by_page = []
    for p in pages:
        hs = [html_to_text(b.get("content", "")) for b in p.get("blocks", [])
              if b.get("label") == "Page-Header"]
        headers_by_page.append(hs)

    # 2) Lectures brutes (ce que l'OCR a lu, sans interprétation)
    def first_texts(k, n=12):
        return [html_to_text(b.get("content", "")) for b in pages[k].get("blocks", [])[:n]] if k < len(pages) else []

    page0 = first_texts(0)
    raw_pub = raw_publication(headers_by_page, page0)
    if not raw_pub:  # repli : « Journal officiel du … » dans les tout premiers blocs de la première page
        for t in page0[:6]:
            m = PUB_RE.search(t)
            if m and (r := _raw(m)):
                raw_pub.append(r)

    raw_seances = []
    for i, hs in enumerate(headers_by_page):
        found = None
        for h in hs:
            if (found := raw_seance(h)):
                break
        if not found and i < 3:
            for b in pages[i].get("blocks", [])[:15]:
                if b.get("label") in ("Text", "Section-Header"):
                    if (found := raw_seance(html_to_text(b.get("content", "")))):
                        break
        raw_seances.append(found)

    # 3) Année du document : OCR (en-têtes) contre catalogue BnF, arbitrée par le jour de la semaine
    csv_date = None
    if csv_row and csv_row.get("date_iso"):
        try:
            csv_date = date.fromisoformat(str(csv_row["date_iso"])[:10])
        except ValueError:
            csv_date = None
    csv_year = csv_date.year if csv_date else (
        int(csv_row["year"]) if csv_row and str(csv_row.get("year", "")).isdigit() else None)

    # Comités secrets : publiés des années plus tard (1916 → 1919, 1917 → 1933, 1940 → 1948).
    # L'année de publication ne doit alors ni voter pour l'année du document, ni être ramenée à celle-ci.
    is_secret = any(f and f[2] == "comité secret" for f in raw_seances)
    ocr_years = Counter(f[0].year for f in raw_seances if f and f[0].year)
    if not is_secret:
        ocr_years.update(r.year for r in raw_pub if r.year)
    ocr_year = ocr_years.most_common(1)[0][0] if ocr_years else None

    flags, notes = [], []
    if ocr_year is None or csv_year is None:
        year = csv_year or ocr_year
    elif abs(ocr_year - csv_year) <= 1:
        year = csv_year
    elif not MIN_YEAR <= ocr_year <= MAX_YEAR:
        year = csv_year
        notes.append(f"année OCR corrigée ({ocr_year} → {csv_year})")
    else:
        wd_raws = [r for r in raw_pub if r.wd] + [f[0] for f in raw_seances if f and f[0].wd]
        wd_raws += raw_weekday_dates(page0 + first_texts(1))
        s_ocr, s_csv = weekday_score(wd_raws, ocr_year), weekday_score(wd_raws, csv_year)
        if s_ocr > s_csv:
            year = ocr_year
            notes.append(f"année du catalogue BnF corrigée ({csv_year} → {ocr_year}, d'après les jours de la semaine)")
        elif s_csv > s_ocr:
            year = csv_year
            notes.append(f"année OCR corrigée ({ocr_year} → {csv_year}, d'après les jours de la semaine)")
        else:
            year = csv_year
            flags.append(f"année incertaine : {ocr_year} dans le texte, {csv_year} au catalogue BnF")

    if csv_date and year == csv_date.year:
        anchor = csv_date
    elif csv_date and year is not None:
        anchor = _safe_date(year, csv_date.month, csv_date.day) or date(year, csv_date.month, 1)
    else:
        anchor = None

    # 4) Résolution des dates avec l'année retenue
    if is_secret:
        pub_years = Counter(r.year for r in raw_pub if r.year and MIN_YEAR <= r.year <= MAX_YEAR)
        pub_year = pub_years.most_common(1)[0][0] if pub_years else year
        pub_votes = Counter(d for r in raw_pub if (d := resolve(r, pub_year, None)))
    else:
        pub_votes = Counter(d for r in raw_pub if (d := resolve(r, year, anchor)))
    date_pub = pub_votes.most_common(1)[0][0] if pub_votes else None
    ref = (anchor or date_pub) if is_secret else (date_pub or anchor)
    page_seance = []
    for f in raw_seances:
        d = resolve(f[0], year, ref) if f else None
        page_seance.append((d, f[1], f[2]) if d else None)

    # propager aux pages sans en-tête lisible (pages d'annexes, en-têtes mal reconnus)
    last = None
    filled = []
    for f in page_seance:
        if f:
            last = f
        filled.append(last)
    first_known = next((f for f in page_seance if f), None)
    filled = [f or first_known for f in filled]

    # 5) Regrouper en séances successives
    seances = []
    for i, f in enumerate(filled):
        if f is None:
            continue
        d, ordn, kind = f
        key = (d.isoformat(), ordn, kind)
        if seances and seances[-1]["key"] == key:
            seances[-1]["last_page"] = i
        elif seances and seances[-1]["date"] == d.isoformat() and seances[-1]["kind"] == kind and ordn is None:
            seances[-1]["last_page"] = i  # même jour, ordinal perdu par l'OCR
        else:
            seances.append({"key": key, "date": d.isoformat(), "ordinal": ordn, "kind": kind,
                            "first_page": i, "last_page": i})
    for s_ in seances:
        s_.pop("key")

    # 6) Chambre : CSV, sinon en-têtes
    chamber = csv_row.get("source_chamber") if csv_row else None
    if not chamber:
        votes = Counter(c for hs in headers_by_page for h in hs if (c := detect_chamber(h)))
        chamber = votes.most_common(1)[0][0] if votes else None

    date_seance = date.fromisoformat(seances[0]["date"]) if seances else None
    main = date_seance or date_pub or csv_date

    # 7) Publication antérieure à la séance ou très tardive : l'OCR confond souvent des chiffres
    #    (28 → 23, 27 → 17, 26 → 20). On essaie UNE substitution plausible qui place la publication
    #    0 à 3 jours après la séance ; la correction est signalée dans les notes.
    if date_seance and date_pub and not is_secret and not 0 <= (date_pub - date_seance).days <= 7:
        fixed = _fix_digit(date_pub, date_seance)
        if fixed:
            notes.append(f"jour de publication corrigé ({date_pub.day} → {fixed.day})")
            date_pub = fixed

    # 8) Signalements : « flags » = à vérifier, « notes » = information
    if date_seance and date_pub:
        gap = (date_pub - date_seance).days
        if gap < 0:
            flags.append("séance postérieure à la publication")
        elif gap > 7 and not is_secret:
            flags.append(f"publication {gap} j après la séance")
    if is_secret and date_pub:
        notes.append(f"comité secret publié au Journal officiel du {fr_date(date_pub, weekday=False)}")
    if not date_seance:
        notes.append("aucune séance identifiée (questions écrites, annexes, tables…)")
    if not date_pub:
        notes.append("date de publication non lue")
    if not main:
        flags.append("aucune date trouvée")
    seance_dates = {date.fromisoformat(x["date"]) for x in seances}
    known = seance_dates | ({date_pub} if date_pub else set())
    # comparaison avec la date du catalogue, année éventuellement corrigée (anchor) ;
    # un comité secret s'étale souvent sur plusieurs jours
    tol = 7 if is_secret else 1
    if anchor and known and not any(abs((anchor - k).days) <= tol for k in known):
        flags.append("date catalogue BnF différente")

    index_row = {
        "doc_id": doc_id,
        "subdir": subdir,
        "num_pages": len(pages),
        "chamber": chamber,
        "ark": (csv_row or {}).get("ark_full"),
        "date_csv": csv_date.isoformat() if csv_date else None,
        "date_pub": date_pub.isoformat() if date_pub else None,
        "date_seance": date_seance.isoformat() if date_seance else None,
        "main_date": main.isoformat() if main else None,
        "year": main.year if main else (int(csv_row["year"]) if csv_row and csv_row.get("year") else None),
        "n_seances": len(seances),
        "seance_kind": seances[0]["kind"] if seances else None,
        "seances": json.dumps(seances, ensure_ascii=False),
        "flags": "; ".join(flags),
        "notes": "; ".join(notes),
    }

    # Numéros de page imprimés : l'OCR se trompe parfois (« 9304 » pour « 2301 »).
    # Le JO a une pagination continue : on retient le décalage majoritaire
    # (numéro imprimé − index de page) et on corrige les pages qui s'en écartent.
    raw_printed = [_printed_page(hs) for hs in headers_by_page]
    offsets = Counter(int(n) - i for i, n in enumerate(raw_printed) if n)
    inferred = list(raw_printed)
    if offsets:
        off, count = offsets.most_common(1)[0]
        if count >= max(2, len(pages) // 2):
            inferred = [str(i + off) for i in range(len(pages))]

    page_rows = []
    for i, p in enumerate(pages):
        blocks = [[b.get("label", ""), b.get("content", "")] for b in p.get("blocks", [])]
        text = "\n".join(html_to_text(c) for lab, c in blocks
                         if lab not in ("Page-Header", "Page-Footer"))
        f = filled[i]
        page_rows.append({
            "doc_id": doc_id,
            "page_num": int(p.get("page_num", i)),
            "printed_page": inferred[i],
            "printed_page_ocr": raw_printed[i],
            "seance_date": f[0].isoformat() if f else None,
            "seance_kind": f[2] if f else None,
            "blocks": json.dumps(blocks, ensure_ascii=False),
            "text": text,
        })
    return index_row, page_rows
