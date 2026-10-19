"""DECIDON — lecture chronologique des débats parlementaires (OCR Chandra v2).

Lancer :  streamlit run app.py
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from decidon.dates import find_dates, fr_date, html_to_text, seance_label
from decidon.render import (CHAMBER_LABEL, doc_to_markdown, fold, gallica_url,
                            render_pages)

st.set_page_config(page_title="DECIDON · débats parlementaires", page_icon="📜", layout="wide")

CREDITS = (
    "Transcriptions : Carlinet, Perrinet, Chazalon, Duménieu, Puren (2026), *WP2.1 OCR Sample with IIIF "
    "Manifests – ChandraV2*, Zenodo, [doi:10.5281/zenodo.23019428](https://doi.org/10.5281/zenodo.23019428), "
    "CC BY 4.0 — projet DECIDON (ANR-25-CE38-4063). Source gallica.bnf.fr / Bibliothèque nationale de France. "
    "OCR non vérifié : confronter toute citation à l'image source."
)
APP_DIR = Path(__file__).parent


def _setting(name: str, default: str | None = None) -> str | None:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # pas de secrets.toml en local
        pass
    return os.environ.get(name.upper(), default)


DATA_DIR = Path(_setting("decidon_data_dir", str(APP_DIR / "data")))


# ----------------------------------------------------------------------------- données

def _download_failed(url: str, label: str, reason: str) -> None:
    st.error(f"**Impossible de télécharger `{label}`** — {reason}\n\nAdresse essayée :\n```\n{url}\n```")
    st.markdown(
        "Points à vérifier :\n"
        "1. **Secret `pages_url`** (*Manage app → Settings → Secrets*) : l'adresse doit être celle du fichier "
        "dans la release, sans `VOTRE-COMPTE`, avec le bon nom de compte ou d'organisation.\n"
        "2. **Dépôt public** : *Settings → General → Danger Zone → Change visibility*. "
        "Un dépôt privé renvoie 404.\n"
        "3. **Release publiée**, pas en brouillon (*Draft*), et envoi du fichier terminé.\n"
        "4. **Nom du fichier** dans la release : exactement `pages.parquet`.\n\n"
        "Astuce : collez l'adresse dans une fenêtre de navigation privée. Elle doit lancer un téléchargement."
    )
    st.stop()


def _download(url: str, dest: Path, label: str) -> None:
    url = url.strip().strip('"').strip("'")
    if "VOTRE-COMPTE" in url:
        _download_failed(url, label, "l'adresse contient encore `VOTRE-COMPTE` ; remplacez-le par votre nom GitHub.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    bar = st.progress(0.0, text=f"Téléchargement de {label}…")
    req = urllib.request.Request(url, headers={"User-Agent": "decidon-viewer"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    bar.progress(min(done / total, 1.0),
                                 text=f"Téléchargement de {label} : {done / 1e6:.0f}/{total / 1e6:.0f} Mo")
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        bar.empty()
        why = {404: "erreur 404 : fichier introuvable à cette adresse",
               403: "erreur 403 : accès refusé (dépôt privé ou limite GitHub)"}.get(e.code, f"erreur HTTP {e.code}")
        _download_failed(url, label, why + ".")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        tmp.unlink(missing_ok=True)
        bar.empty()
        _download_failed(url, label, f"problème réseau ({e}). Réessayez avec *Manage app → Reboot app*.")
    tmp.rename(dest)
    bar.empty()


@st.cache_resource(show_spinner=False)
def data_file(name: str) -> Path | None:
    """Fichier local s'il existe, sinon téléchargé depuis l'URL configurée (Release GitHub)."""
    local = DATA_DIR / name
    if local.exists():
        return local
    url = _setting(f"{name.split('.')[0]}_url")  # pages_url / index_url
    if not url:
        return None
    target = local
    if not os.access(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent, os.W_OK):
        target = Path(tempfile.gettempdir()) / "decidon" / name
    if not target.exists():
        _download(url, target, name)
    return target


@st.cache_data(show_spinner=False)
def load_index(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["main_date"] = pd.to_datetime(df["main_date"], errors="coerce")
    df["chambre"] = df["chamber"].map(CHAMBER_LABEL).fillna("?")
    df = df.sort_values(["main_date", "chamber", "doc_id"], na_position="last").reset_index(drop=True)
    return df


@st.cache_resource(show_spinner=False)
def db() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


@st.cache_data(show_spinner=False, max_entries=64)
def load_pages(path: str, doc_id: str) -> pd.DataFrame:
    df = db().execute(
        "SELECT * FROM read_parquet(?) WHERE doc_id = ? ORDER BY page_num", [path, doc_id]
    ).df()
    return df.astype(object).where(df.notna(), None)  # valeurs absentes → None


@st.cache_data(show_spinner="Recherche dans le corpus…", max_entries=32)
def search(path: str, query: str, limit: int = 400) -> pd.DataFrame:
    q = fold(query.strip())
    return db().execute(
        """SELECT doc_id, page_num, text FROM read_parquet(?)
           WHERE strip_accents(lower(text)) LIKE '%' || ? || '%'
           ORDER BY doc_id, page_num LIMIT ?""",
        [path, q, limit],
    ).df()


def snippet(text: str, query: str, width: int = 110) -> str:
    f, q = fold(text), fold(query.strip())
    k = f.find(q)
    if k < 0:
        return text[: 2 * width]
    a, b = max(0, k - width), min(len(text), k + len(q) + width)
    s = text[a:b].replace("\n", " ")
    return ("…" if a else "") + s + ("…" if b < len(text) else "")


# ----------------------------------------------------------------------------- style

DARK = getattr(getattr(st.context, "theme", None), "type", "light") == "dark"
CSS = """
<style>
.ocr-doc { max-width: 52rem; font-family: Georgia, 'Times New Roman', serif; font-size: 1.02rem; line-height: 1.6; }
.ocr-page { margin: 0 0 2.2rem 0; }
.pg-banner { position: sticky; top: 3.75rem; z-index: 3; padding: .35rem .7rem; margin: 0 0 .8rem 0;
  font-family: system-ui, sans-serif; font-size: .82rem; border-radius: 6px;
  background: var(--bn-bg); border-left: 4px solid var(--accent); }
.pg-banner a { color: var(--accent); }
.bn-seance { font-weight: 600; }
.blk { margin: 0 0 .55rem 0; }
.blk p { margin: 0; }
.blk-section-header { font-family: system-ui, sans-serif; font-weight: 700; font-size: 1.05rem; margin-top: 1.1rem;
  letter-spacing: .01em; }
.blk-section-header h1, .blk-section-header h2, .blk-section-header h3, .blk-section-header h4 { font-size: inherit; margin: 0; }
.blk-page-header, .blk-page-footer { font-family: system-ui, sans-serif; font-size: .75rem; opacity: .55; }
.blk-footnote, .blk-caption { font-size: .85rem; opacity: .85; }
.blk-list-group ul, .blk-list-group ol { margin: 0; padding-left: 1.2rem; }
.blk-table table { border-collapse: collapse; font-family: system-ui, sans-serif; font-size: .88rem; }
.blk-table td, .blk-table th { border: 1px solid var(--line); padding: .15rem .5rem; }
.blk-empty { font-style: italic; opacity: .6; }
.blk b.spk { color: var(--speaker); }
mark.dt { background: var(--dt-bg); color: inherit; border-radius: 3px; padding: 0 .15em; }
mark.dt-noyear { background: transparent; border-bottom: 2px dotted var(--dt-line); }
mark.hit { background: var(--hit-bg); color: inherit; border-radius: 3px; }
.date-card { padding: .9rem 1.1rem; border-radius: 10px; background: var(--bn-bg); margin-bottom: .6rem; }
.date-card .big { font-size: 1.45rem; font-weight: 700; line-height: 1.25; }
.date-card .sub { opacity: .8; font-size: .92rem; }
.chip { display: inline-block; padding: .1rem .55rem; margin: .25rem .3rem 0 0; border-radius: 999px;
  font-size: .8rem; border: 1px solid var(--line); }
.toc-item { font-size: .8rem; line-height: 1.35; margin: 0 0 .35rem 0; }
.chip.note { opacity: .75; }
.chip.warn { border-color: #d97706; color: #b45309; }
</style>
"""
VARS_LIGHT = ":root{--accent:#8a3b12;--bn-bg:#f6f1e7;--line:#d9d2c3;--dt-bg:#fde68a;--dt-line:#ca8a04;--hit-bg:#a7f3d0;--speaker:#7c2d12;}"
VARS_DARK = ":root{--accent:#f0a46b;--bn-bg:#2a2520;--line:#4a4339;--dt-bg:#5b4a12;--dt-line:#eab308;--hit-bg:#14532d;--speaker:#fdba74;}"
st.markdown(CSS + f"<style>{VARS_DARK if DARK else VARS_LIGHT}</style>", unsafe_allow_html=True)


# ----------------------------------------------------------------------------- chargement

index_path = data_file("index.parquet")
pages_path = data_file("pages.parquet")
if index_path is None:
    st.title("📜 DECIDON — lecture des débats")
    st.error(
        "Aucune donnée trouvée. Lancez d'abord `python prepare_data.py --source 2606-chandrav2.tar.xz "
        "--csv \"decidon_documents_status_20260402T180205Z - status_20260402.csv\"`, "
        "ou configurez `index_url` / `pages_url` dans les secrets Streamlit (voir README)."
    )
    st.stop()

idx = load_index(str(index_path))
def _none_if_missing(v):
    """pandas ≥ 3 renvoie NaN (et non None) pour une chaîne absente : on uniformise en None."""
    try:
        return None if v is None or pd.isna(v) else v
    except (TypeError, ValueError):  # listes, tableaux…
        return v


docs_by_id = {r["doc_id"]: {k: _none_if_missing(v) for k, v in r.items()} for r in idx.to_dict("records")}

ss = st.session_state
qp = st.query_params
if "doc_id" not in ss:
    ss.doc_id = qp.get("doc") if qp.get("doc") in docs_by_id else None
    if ss.doc_id:
        ss.view = "📖 Lecture"
if "page" not in ss:
    ss.page = int(qp.get("page", "1")) if str(qp.get("page", "1")).isdigit() else 1


def open_doc(doc_id: str, page: int = 1, page_mode: bool = False, switch: bool = True):
    """À n'appeler qu'avant la création des widgets concernés, ou dans un callback."""
    ss.doc_id = doc_id
    ss.page = page
    if switch:
        ss.view = "📖 Lecture"
    if page_mode:
        ss.read_mode = "Page par page"


def step_page(delta: int):
    ss.page = int(ss.page) + delta


# ----------------------------------------------------------------------------- barre latérale

with st.sidebar:
    st.markdown("### 📜 DECIDON")
    st.caption("Débats parlementaires 1881–1940 · OCR Chandra v2")

    chambers = st.multiselect("Chambre", ["Chambre des députés", "Sénat"],
                              default=["Chambre des députés", "Sénat"])
    years = idx["main_date"].dt.year.dropna().astype(int)
    y0, y1 = (int(years.min()), int(years.max())) if len(years) else (1881, 1940)
    if y0 == y1:
        y1 = y0 + 1
    yr = st.slider("Période", y0, y1, (y0, y1))
    only_flags = st.toggle("Seulement les dates à vérifier", help="Documents dont les dates (séance, JO, catalogue) se contredisent ou manquent.")

    st.markdown("---")
    st.markdown("**Aller à une date**")
    target = st.date_input("Date", value=None, min_value=date(1880, 1, 1), max_value=date(1949, 12, 31),
                           format="DD/MM/YYYY", label_visibility="collapsed")

    st.markdown("---")
    st.markdown("**Affichage**")
    show_headers = st.toggle("En-têtes et pieds de page", value=False)
    show_dates = st.toggle("Surligner les dates", value=True)
    show_speakers = st.toggle("Orateurs en évidence", value=True,
                              help="Met en gras « M. X. » en début de paragraphe quand l'OCR ne l'a pas fait (heuristique).")

    st.markdown("---")
    st.caption(CREDITS)

flt = idx[idx["chambre"].isin(chambers) & idx["main_date"].dt.year.between(yr[0], yr[1])]
if only_flags:
    flt = flt[flt["flags"].fillna("") != ""]
flt = flt.reset_index(drop=True)

if target is not None and len(flt):
    gap = (flt["main_date"] - pd.Timestamp(target)).abs()
    nearest = flt.loc[gap.idxmin()]
    if ss.get("_last_target") != target:
        ss._last_target = target
        open_doc(nearest["doc_id"])
        d = gap.min().days
        if d:
            st.toast(f"Pas de document le {fr_date(target)} : document le plus proche ({d} j d'écart).")

if "view" not in ss:
    ss.view = "📅 Chronologie"
view = st.segmented_control("Vue", ["📅 Chronologie", "📖 Lecture", "🔎 Recherche"], key="view",
                            required=True, label_visibility="collapsed")


# ----------------------------------------------------------------------------- chronologie

def doc_label(r) -> str:
    when = fr_date(r["main_date"].date()) if pd.notna(r["main_date"]) else "date inconnue"
    return f"{when} · {r['chambre']} · {r['num_pages']} p. · n° {r['doc_id']}"


if view == "📅 Chronologie":
    st.subheader("Chronologie des séances")
    c1, c2, c3 = st.columns(3)
    c1.metric("Documents", f"{len(flt):,}".replace(",", " "))
    c2.metric("Pages", f"{int(flt['num_pages'].sum()):,}".replace(",", " "))
    c3.metric("Période", f"{yr[0]}–{yr[1]}")

    if len(flt):
        span = yr[1] - yr[0]
        g = flt.dropna(subset=["main_date"]).copy()
        if span <= 3:
            g["période"] = g["main_date"].dt.to_period("M").dt.to_timestamp()
            x = alt.X("yearmonth(période):T", title=None)
        else:
            g["période"] = g["main_date"].dt.year
            x = alt.X("période:O", title=None)
        counts = g.groupby(["période", "chambre"]).size().reset_index(name="documents")
        chart = alt.Chart(counts).mark_bar().encode(
            x=x, y=alt.Y("documents:Q", title="documents"),
            color=alt.Color("chambre:N", title=None,
                            scale=alt.Scale(domain=["Chambre des députés", "Sénat"], range=["#b45309", "#1d4ed8"])),
            tooltip=["période", "chambre", "documents"],
        ).properties(height=180)
        st.altair_chart(chart, width="stretch")

        table = pd.DataFrame({
            "Séance": [fr_date(d.date()) if pd.notna(d) else "—" for d in flt["main_date"]],
            "Date": flt["main_date"],
            "Nature": [("Comité secret" if k == "comité secret" else "Séance") if isinstance(k, str) else "Sans séance"
                       for k in (flt["seance_kind"] if "seance_kind" in flt else [None] * len(flt))],
            "Chambre": flt["chambre"],
            "Séances": flt["n_seances"],
            "JO du": [fr_date(d, weekday=False) if d else "—" for d in flt["date_pub"]],
            "Catalogue BnF": [fr_date(d, weekday=False) if d else "—" for d in flt["date_csv"]],
            "Pages": flt["num_pages"],
            "À vérifier": flt["flags"].fillna(""),
            "Infos": flt["notes"].fillna("") if "notes" in flt else "",
            "N°": flt["doc_id"],
        })

        def _on_select():
            rows = ss.chrono_table.selection.rows
            if rows:
                open_doc(flt.iloc[rows[0]]["doc_id"])

        st.caption("Cliquez sur une ligne pour lire le compte rendu. La date principale est la date de séance "
                   "(à défaut, celle du JO, puis celle du catalogue BnF).")
        st.dataframe(table, key="chrono_table", on_select=_on_select, selection_mode="single-row",
                     hide_index=True, width="stretch", height=min(560, 38 + 35 * len(table)),
                     column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                                    "Séances": st.column_config.NumberColumn(help="Nombre de séances dans ce numéro")})
    else:
        st.info("Aucun document ne correspond aux filtres.")


# ----------------------------------------------------------------------------- lecture

elif view == "📖 Lecture":
    if not len(flt):
        st.info("Aucun document ne correspond aux filtres.")
        st.stop()
    if pages_path is None:
        st.error("Le fichier pages.parquet est introuvable (voir README : `pages_url`).")
        st.stop()
    ids = flt["doc_id"].tolist()
    if ss.doc_id not in ids:
        ss.doc_id = ss.doc_id if ss.doc_id in docs_by_id else ids[0]
    in_filter = ss.doc_id in ids
    pos = ids.index(ss.doc_id) if in_filter else 0

    nav = st.columns([1, 6, 1])
    if nav[0].button("◀ Précédent", width="stretch", disabled=not in_filter or pos == 0):
        open_doc(ids[pos - 1], switch=False); st.rerun()
    labels = {r["doc_id"]: doc_label(r) for _, r in flt.iterrows()}
    if not in_filter:
        labels = {ss.doc_id: doc_label(idx[idx.doc_id == ss.doc_id].iloc[0]), **labels}
    choice = nav[1].selectbox("Document", list(labels), index=list(labels).index(ss.doc_id),
                              format_func=labels.get, label_visibility="collapsed")
    if choice != ss.doc_id:
        open_doc(choice, switch=False); st.rerun()
    if nav[2].button("Suivant ▶", width="stretch", disabled=not in_filter or pos >= len(ids) - 1):
        open_doc(ids[pos + 1], switch=False); st.rerun()

    doc = docs_by_id[ss.doc_id]
    st.query_params["doc"] = ss.doc_id
    pages = load_pages(str(pages_path), ss.doc_id)
    seances = json.loads(doc.get("seances") or "[]")

    # --- carte des dates
    main_title = (f"{seance_label(doc.get('seance_kind'))} du {fr_date(doc['date_seance'])}" if doc.get("date_seance")
                  else f"Numéro du {fr_date(doc.get('main_date').date())}" if pd.notna(doc.get("main_date")) else "Date inconnue")
    chips = []
    if doc.get("date_pub"):
        chips.append(f'<span class="chip">📰 Journal officiel du {fr_date(doc["date_pub"])}</span>')
    if doc.get("date_csv"):
        chips.append(f'<span class="chip">🗂 Catalogue BnF : {fr_date(doc["date_csv"], weekday=False)}</span>')
    url = gallica_url(doc.get("ark"))
    if url:
        chips.append(f'<span class="chip"><a href="{url}" target="_blank">Gallica ↗</a></span>')
    for fl in filter(None, (doc.get("flags") or "").split("; ")):
        chips.append(f'<span class="chip warn">⚠ {fl}</span>')
    for nt in filter(None, (doc.get("notes") or "").split("; ")):
        chips.append(f'<span class="chip note">ℹ {nt}</span>')
    st.markdown(
        f'<div class="date-card"><div class="big">{main_title}</div>'
        f'<div class="sub">{CHAMBER_LABEL.get(doc.get("chamber"), "?")} · {doc["num_pages"]} pages · n° {doc["doc_id"]}</div>'
        f'{"".join(chips)}</div>', unsafe_allow_html=True)
    if len(seances) > 1:
        st.markdown("**Séances de ce numéro :** " + " · ".join(
            f"{seance_label(s.get('kind'), s.get('ordinal'))} du {fr_date(s['date'])} "
            f"(p. {s['first_page'] + 1}–{s['last_page'] + 1})" for s in seances))

    left, right = st.columns([3, 1], gap="large")

    # --- panneau latéral : sommaire, dates citées, export
    with right:
        read_mode = st.radio("Lecture", ["Document entier", "Page par page"], key="read_mode", horizontal=True)
        ref = date.fromisoformat(doc["date_pub"]) if doc.get("date_pub") else None
        year = ref.year if ref else doc.get("year")
        toc, cited = [], defaultdict(set)
        for p in pages.itertuples():
            for label, content in json.loads(p.blocks):
                if label in ("Page-Header", "Page-Footer"):
                    continue
                t = html_to_text(content)
                if label == "Section-Header" and t:
                    toc.append((int(p.page_num) + 1, t.replace("\n", " ")))
                for fd in find_dates(t, year, ref):
                    if fd.has_year:
                        cited[fd.date].add(int(p.page_num) + 1)
        with st.expander(f"Sommaire ({len(toc)})", expanded=True):
            for k, (pg, t) in enumerate(toc[:150]):
                if read_mode == "Page par page":
                    st.button(f"p. {pg} — {t[:70]}", key=f"toc{k}", type="tertiary",
                              on_click=lambda pg=pg: ss.update(page=pg))
                else:
                    st.markdown(f'<div class="toc-item"><a href="#p{pg}">p.&nbsp;{pg}</a> — {t[:90]}</div>', unsafe_allow_html=True)
        with st.expander(f"Dates citées ({len(cited)})"):
            st.caption("Dates complètes (avec année) mentionnées dans le texte.")
            for d in sorted(cited):
                pgs = ", ".join(str(x) for x in sorted(cited[d]))
                st.markdown(f"<small><b>{fr_date(d, weekday=False)}</b> — p. {pgs}</small>", unsafe_allow_html=True)
        st.download_button("⬇ Exporter en Markdown", doc_to_markdown(doc, pages, CREDITS),
                           file_name=f"{(doc.get('date_seance') or doc['doc_id'])}_{doc.get('chamber')}_{doc['doc_id']}.md",
                           mime="text/markdown", width="stretch")

    with left:
        terms = [ss.get("last_query")] if ss.get("last_query") else None
        opts = dict(terms=terms, show_headers=show_headers, dates=show_dates, speakers=show_speakers)
        if read_mode == "Page par page":
            n = len(pages)
            ss.page = max(1, min(int(ss.page), n))
            pc = st.columns([1, 2, 1])
            pc[0].button("◀ page", disabled=ss.page <= 1, width="stretch", on_click=step_page, args=(-1,))
            pc[1].slider("Page", 1, max(n, 2), key="page", label_visibility="collapsed")
            pc[2].button("page ▶", disabled=ss.page >= n, width="stretch", on_click=step_page, args=(1,))
            st.query_params["page"] = ss.page
            st.markdown(render_pages(pages.iloc[[ss.page - 1]], doc, **opts), unsafe_allow_html=True)
        else:
            st.markdown(render_pages(pages, doc, **opts), unsafe_allow_html=True)


# ----------------------------------------------------------------------------- recherche

elif view == "🔎 Recherche":
    st.subheader("Recherche plein texte")
    if pages_path is None:
        st.error("Le fichier pages.parquet est introuvable (voir README : `pages_url`).")
        st.stop()
    q = st.text_input("Mot ou expression (sans tenir compte des accents ni des majuscules)",
                      value=ss.get("last_query", ""), placeholder="ex. vote des femmes, Jaurès, sulfate de cuivre")
    ss.last_query = q.strip()
    if len(q.strip()) >= 3:
        res = search(str(pages_path), q)
        res = res.merge(flt[["doc_id", "main_date", "chambre"]], on="doc_id", how="inner")
        if res.empty:
            st.info("Aucun résultat dans la sélection (chambre / période).")
        else:
            res = res.sort_values(["main_date", "page_num"])
            per_doc = Counter(res["doc_id"])
            st.caption(f"{len(res)} pages dans {len(per_doc)} documents"
                       + (" (résultats limités aux 400 premières pages)" if len(res) >= 400 else "")
                       + " · les termes trouvés sont surlignés en vert dans la vue Lecture.")
            for d_id, grp in res.groupby("doc_id", sort=False):
                r0 = grp.iloc[0]
                when = fr_date(r0["main_date"].date()) if pd.notna(r0["main_date"]) else "?"
                with st.container(border=True):
                    st.markdown(f"**{when}** · {r0['chambre']} · n° {d_id} · {len(grp)} page(s)")
                    for r in grp.head(3).itertuples():
                        cols = st.columns([5, 1])
                        cols[0].markdown(f"<small>p. {r.page_num + 1} — {snippet(r.text, q)}</small>",
                                         unsafe_allow_html=True)
                        cols[1].button("Lire", key=f"go{d_id}-{r.page_num}", on_click=open_doc,
                                       args=(d_id, int(r.page_num) + 1, True))
    else:
        st.caption("Tapez au moins 3 caractères.")
