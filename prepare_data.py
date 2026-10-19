"""Prépare les données de l'application à partir de l'export Chandra DECIDON.

Entrées
  --source   l'archive 2606-chandrav2.tar.xz (lue en flux, sans décompression sur disque)
             OU un dossier contenant les JSON (recherche récursive)
  --csv      decidon_documents_status_20260402T180205Z - status_20260402.csv (facultatif mais conseillé)

Sorties (dans --out, par défaut ./data)
  index.parquet   une ligne par document : dates, chambre, ARK… (~quelques centaines de Ko, à versionner)
  pages.parquet   une ligne par page : blocs OCR + texte brut (gros fichier → Release GitHub)

Exemple
  python prepare_data.py --source 2606-chandrav2.tar.xz \
      --csv "decidon_documents_status_20260402T180205Z - status_20260402.csv"

  ou, si l'archive est déjà décompressée :
  python prepare_data.py --source ocr-processing/2606-chandrav2 \
      --csv "decidon_documents_status_20260402T180205Z - status_20260402.csv"
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decidon.extract import process_document

PAGE_SCHEMA = pa.schema([
    ("doc_id", pa.string()),
    ("page_num", pa.int32()),
    ("printed_page", pa.string()),
    ("printed_page_ocr", pa.string()),
    ("seance_date", pa.string()),
    ("seance_kind", pa.string()),
    ("blocks", pa.string()),
    ("text", pa.string()),
])


def load_csv(path: str | None) -> dict[str, dict]:
    """Inventaire indexé par « sous-dossier/numéro » (ex. 1/6305545) et par numéro seul."""
    if not path:
        return {}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    rows = {}
    for r in df.to_dict("records"):
        zp = r.get("zip_path", "")
        stem = PurePosixPath(zp).stem
        sub = PurePosixPath(zp).parent.name
        rows[f"{sub}/{stem}"] = r
        rows.setdefault(stem, r)
    return rows


def _is_ocr_json(path: PurePosixPath) -> bool:
    """Garde .../2606-chandrav2/<n>/<id>.json ; écarte les manifests IIIF et les fichiers cachés macOS."""
    if path.suffix != ".json" or path.name.startswith("."):  # ._6218709.json, .DS_Store…
        return False
    if any("manifest" in part for part in path.parts):  # manifests/ et 2606-manifests-chandrav2/
        return False
    return path.stem.isdigit()


def _parse(raw: bytes, label: str):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"  ! ignoré (JSON illisible) : {label} — {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or "pages" not in data:
        print(f"  ! ignoré (pas une sortie Chandra) : {label}", file=sys.stderr)
        return None
    return data


def iter_json_sources(source: Path):
    """Produit (sous-dossier, doc_id, dict JSON) depuis une archive .tar.xz ou un dossier.

    Structure attendue (archive ou dossier décompressé) :
        ocr-processing/2606-chandrav2/<sous-dossier>/<numéro>.json
    Le dossier passé en --source peut être n'importe quel niveau au-dessus.
    """
    if source.is_dir():
        for p in sorted(source.rglob("*.json")):
            rel = PurePosixPath(p.relative_to(source).as_posix())
            if not _is_ocr_json(rel):
                continue
            data = _parse(p.read_bytes(), str(rel))
            if data is not None:
                yield p.parent.name, p.stem, data
        return
    # mode flux « r|* » : lecture séquentielle, mémoire constante
    with tarfile.open(source, mode="r|*") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if not member.isfile() or not _is_ocr_json(path):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            data = _parse(f.read(), member.name)
            if data is not None:
                yield path.parent.name, path.stem, data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="2606-chandrav2.tar.xz ou dossier de JSON")
    ap.add_argument("--csv", help="inventaire status_20260402.csv")
    ap.add_argument("--out", default="data", help="dossier de sortie (défaut : data)")
    ap.add_argument("--limit", type=int, default=0, help="ne traiter que N documents (test)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_rows = load_csv(args.csv)
    tmp_pages = out / "pages.unsorted.parquet"

    t0 = time.time()
    index_rows, buffer, n_pages, seen = [], [], 0, set()
    writer = pq.ParquetWriter(tmp_pages, PAGE_SCHEMA, compression="zstd")
    try:
        for i, (sub, doc_id, data) in enumerate(iter_json_sources(Path(args.source)), 1):
            if doc_id in seen:
                print(f"  ! doublon ignoré : {sub}/{doc_id}.json", file=sys.stderr)
                continue
            seen.add(doc_id)
            csv_row = csv_rows.get(f"{sub}/{doc_id}") or csv_rows.get(doc_id)
            if csv_row is None and csv_rows:
                print(f"  ! absent du CSV : {sub}/{doc_id}.json (dates tirées du seul texte)", file=sys.stderr)
            idx, pages = process_document(doc_id, sub, data, csv_row)
            index_rows.append(idx)
            buffer.extend(pages)
            n_pages += len(pages)
            if len(buffer) >= 5000:
                writer.write_table(pa.Table.from_pylist(buffer, schema=PAGE_SCHEMA))
                buffer = []
            if i % 100 == 0:
                print(f"  {i} documents, {n_pages} pages ({time.time() - t0:.0f} s)", flush=True)
            if args.limit and i >= args.limit:
                break
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=PAGE_SCHEMA))
    finally:
        writer.close()

    if not index_rows:
        sys.exit("Aucun JSON trouvé dans la source.")

    # Tri par document pour que l'application lise un document sans parcourir tout le fichier
    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT * FROM read_parquet('{tmp_pages.as_posix()}') ORDER BY doc_id, page_num)
        TO '{(out / 'pages.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 2048)
    """)
    tmp_pages.unlink()

    idx = pd.DataFrame(index_rows).sort_values(["main_date", "doc_id"], na_position="last")
    idx.to_parquet(out / "index.parquet", index=False)

    size = (out / "pages.parquet").stat().st_size / 1e6
    print(f"\n{len(idx)} documents, {n_pages} pages en {time.time() - t0:.0f} s")
    print(f"  {out / 'index.parquet'}")
    print(f"  {out / 'pages.parquet'} ({size:.1f} Mo)")
    flagged = idx[idx["flags"] != ""]
    print(f"  {len(flagged)} documents à vérifier (colonne flags)")
    if "notes" in idx:
        print(f"  {int((idx['notes'] != '').sum())} documents avec une information sur les dates (colonne notes)")


if __name__ == "__main__":
    main()
