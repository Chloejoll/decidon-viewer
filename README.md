# DECIDON · lecture des débats parlementaires

Application Streamlit pour lire les transcriptions OCR (Chandra v2) des débats de la Chambre des députés
et du Sénat (1881–1940) publiées par le projet DECIDON, **classées et navigables par date**.

## Ce que fait l'application

- **Chronologie** : histogramme des documents par année (ou par mois sur une courte période) et tableau trié par
  date de séance. Un clic sur une ligne ouvre le compte rendu.
- **Lecture** : carte des dates en tête de document (séance, publication au JO, date du catalogue BnF), bandeau
  fixe rappelant à chaque page la date de séance, le numéro de page imprimé et un lien vers l'image Gallica.
  Navigation « précédent / suivant » dans l'ordre chronologique, sommaire, liste des dates citées, export Markdown.
- **Recherche** plein texte (sans accents ni majuscules), résultats triés par date ; les termes sont surlignés à la lecture.
- **Aller à une date** (barre latérale) : ouvre le document le plus proche de la date choisie.

## Les trois dates

L'OCR ne fournit aucun champ date ; elles sont extraites du texte (`decidon/dates.py`) :

| Date | Source | Exemple |
|---|---|---|
| **Séance** | en-têtes de page, à défaut « Séance du … » en début de compte rendu | `CHAMBRE — SÉANCE DU 15 MAI 1919` |
| **Publication JO** | en-tête majoritaire « Journal officiel du … » | `Journal officiel du 16 Mai 1919` |
| **Catalogue BnF** | colonne `date_iso` du CSV d'inventaire | `1919-05-15` |

Formats reconnus pour la date de publication, selon les époques :
`Journal officiel du 16 Mai 1919` (en-tête), `Samedi 7 Juillet 1923 ★` (première page, années 1920-1930),
`JOURNAL OFFICIEL` / `DU 11 FÉVRIER 1937` (deux blocs), `2 Avril [CHAMBRE] 717` + `Année 1885` (années 1880).

La date de séance est calculée **page par page**, ce qui permet de repérer les numéros contenant plusieurs séances.
Les **comités secrets** (`COMITÉ SECRET DU 16 JUIN 1916`) sont reconnus : publiés des années plus tard
(1919, 1933, 1948), leur date de publication n'est pas ramenée à l'année de la séance.

**Année.** L'OCR confond souvent 8 et 3 (1898 lu 1893), et le catalogue BnF contient lui aussi quelques erreurs.
Quand les deux divergent, le jour de la semaine (« Séance du vendredi 28 janvier ») départage : seule l'une des deux
années le rend cohérent. Le même contrôle corrige le jour (« vendredi 18 décembre 1887 » → 16, qui était un vendredi).
Un jour de publication manifestement mal lu (« 23 » pour « 28 », publication avant la séance) est corrigé si un
seul chiffre change et place la publication 0 à 3 jours après la séance.

Deux colonnes rendent compte de ces traitements :
- `flags` (**À vérifier** dans l'application) : contradictions non résolues (séance postérieure à la publication,
  année incertaine, date du catalogue incompatible…) ;
- `notes` (**Infos**) : corrections appliquées et cas normaux mais particuliers (comité secret, numéro sans
  séance — questions écrites, annexes —, date de publication non lue).

Sur l'export complet : 15 documents à vérifier sur 3 403.

Les numéros de page imprimés sont corrigés quand l'OCR se trompe (pagination continue du JO) ; une astérisque
signale une valeur corrigée, la lecture OCR brute apparaît au survol.

Dans le texte, les dates complètes sont surlignées en jaune ; les dates sans année sont soulignées en pointillé
(année déduite, visible au survol).

## Installation locale

```bash
pip install -r requirements.txt

# 1. Préparer les données (une seule fois, quelques minutes)
python prepare_data.py --source 2606-chandrav2.tar.xz \
    --csv "decidon_documents_status_20260402T180205Z - status_20260402.csv"
# → data/index.parquet (petit) et data/pages.parquet (gros)

# 2. Lancer
streamlit run app.py
```

`--source` accepte aussi le dossier décompressé, à n'importe quel niveau :

```bash
python prepare_data.py --source ocr-processing/2606-chandrav2 \
    --csv "decidon_documents_status_20260402T180205Z - status_20260402.csv"
```

Structure attendue : `ocr-processing/2606-chandrav2/<sous-dossier>/<numéro>.json`. Le couple
« sous-dossier/numéro » sert à retrouver la ligne du CSV (colonne `zip_path`, ex. `2025-10-14/1/6305545.zip`).
Les manifests IIIF (`manifests/`, `2606-manifests-chandrav2/`), les fichiers cachés macOS (`._*.json`) et les JSON
illisibles sont ignorés avec un message. `--limit 50` permet un essai rapide.

## Mise en ligne (GitHub + Streamlit Community Cloud)

`pages.parquet` dépasse la limite de 100 Mo des fichiers Git : il va dans une **Release GitHub**
(jusqu'à 2 Gio par fichier, bande passante non limitée).

```bash
git init && git add . && git commit -m "Application DECIDON"
git remote add origin https://github.com/VOTRE-COMPTE/decidon-viewer.git && git push -u origin main
git add -f data/index.parquet && git commit -m "Index" && git push    # l'index reste dans le dépôt
gh release create v1 data/pages.parquet --title "Données v1"
```

Puis sur [share.streamlit.io](https://share.streamlit.io) : *New app* → votre dépôt → `app.py`, et dans
*Advanced settings → Secrets* :

```toml
pages_url = "https://github.com/VOTRE-COMPTE/decidon-viewer/releases/download/v1/pages.parquet"
```

Au démarrage (ou au réveil après hibernation), l'application télécharge `pages.parquet` une fois, puis le lit
avec DuckDB sans le charger en mémoire.

Variables reconnues (secrets Streamlit ou variables d'environnement en majuscules) : `pages_url`, `index_url`,
`decidon_data_dir`.

## Structure

```
app.py               application Streamlit
prepare_data.py      tar.xz/JSON + CSV → data/index.parquet, data/pages.parquet
decidon/dates.py     extraction des dates (regex tolérantes à l'OCR)
decidon/extract.py   un JSON Chandra → ligne d'index + lignes de pages
decidon/render.py    rendu HTML des blocs, surlignage, export Markdown
```

## Crédits et licence des données

Carlinet, E., Perrinet, J., Chazalon, J., Duménieu, B., & Puren, M. (2026). *WP2.1 OCR Sample with IIIF
Manifests – ChandraV2* [Dataset]. Zenodo. https://doi.org/10.5281/zenodo.23019428 — CC BY 4.0,
projet DECIDON (ANR-25-CE38-4063). Source gallica.bnf.fr / Bibliothèque nationale de France.

La qualité de l'OCR n'a pas été évaluée par les producteurs : toute citation, date ou attribution de parole doit être
vérifiée sur l'image source (lien Gallica sur chaque page).
