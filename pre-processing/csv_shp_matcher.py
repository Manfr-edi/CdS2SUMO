#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSV–Shapefile Matching Script

This script performs an automatic association between traffic observation CSV files
and GIS points contained in one or more shapefiles.

The goal is to identify which monitoring location (e.g., traffic camera, counting
device, or license plate reader) generated each CSV file. The script scans all
shapefiles in a given directory and compares their attributes with the names of
CSV files stored in another directory.

The matching process relies primarily on two attributes commonly present in the
GIS datasets:

- ID SITO: identifier of the monitoring site (e.g., "Sito 59")
- UBICAZIONE: textual description of the location (e.g., "Via Benedetto Brin")

From the CSV filenames, the script extracts:
- the site identifier (e.g., from "sito59_...")
- a location hint derived from the street name or description

Each CSV file is then compared against all shapefile features using a scoring
system that combines:

1. Exact site identifier match (e.g., CSV "sito59" ↔ shapefile "Sito 59")
2. Text similarity between the CSV filename and the UBICAZIONE field
3. Fuzzy string similarity on the remaining attributes

The script outputs a table (csv_shapefile_matches.csv) containing the best match
for each CSV file, along with:

- the matched shapefile
- the feature row index
- site identifier and location description
- coordinates
- matching score
- confidence level (high / medium / low)
- ambiguity flag if multiple matches have similar scores

This table is later used to create SUMO POIs associated with the traffic
measurement locations.
"""
import os
from pathlib import Path
import re
import unicodedata
import pandas as pd
import geopandas as gpd

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher
    HAS_RAPIDFUZZ = False


SHAPE_DIR = Path(r"../gis_data/TLC")
CSV_DIR = Path(r"../sensor_measures_castellammare")
if not os.path.exists("../processed"):
    os.makedirs("../processed")
OUTPUT_CSV = Path(r"../processed/csv_shapefile_matches.csv")

MIN_FUZZY_SCORE = 60


def normalize_text(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("-", " ").replace("_", " ").replace("/", " ")
    s = re.sub(r"\btlc\d+\b", " ", s)
    s = re.sub(r"\bsito\s*\d+\b", " ", s)
    s = re.sub(r"\bexss\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def similarity(a, b):
    a = normalize_text(a)
    b = normalize_text(b)
    if not a or not b:
        return 0
    if HAS_RAPIDFUZZ:
        return int(fuzz.token_set_ratio(a, b))
    return int(100 * SequenceMatcher(None, a, b).ratio())


def parse_csv_filename(filename):
    stem = Path(filename).stem
    lower = stem.lower()

    site_num = None
    tlc_unit = None

    m = re.search(r"sito\s*(\d+)", lower.replace("_", " "))
    if m:
        site_num = int(m.group(1))

    m2 = re.search(r"(tlc\d+)", lower)
    if m2:
        tlc_unit = m2.group(1)

    name_wo_site = re.sub(r"sito\s*\d+", " ", lower.replace("_", " "))
    name_wo_site = re.sub(r"tlc\d+", " ", name_wo_site)
    name_wo_site = re.sub(r"\s+", " ", name_wo_site).strip()

    return {
        "csv_file": filename,
        "csv_stem": stem,
        "csv_site_num": site_num,
        "csv_tlc_unit": tlc_unit,
        "csv_location_hint": normalize_text(name_wo_site),
    }


def extract_numeric_site_id(value):
    if value is None or pd.isna(value):
        return None
    m = re.search(r"(\d+)", str(value))
    return int(m.group(1)) if m else None


def geometry_xy(geom):
    if geom is None or geom.is_empty:
        return None, None
    try:
        p = geom.representative_point()
        return p.x, p.y
    except Exception:
        return None, None


def read_all_shapefiles(shape_dir: Path):
    shp_files = list(shape_dir.rglob("*.shp"))
    if not shp_files:
        raise FileNotFoundError(f"Nessun shapefile trovato in: {shape_dir.resolve()}")

    rows = []

    for shp in shp_files:
        try:
            gdf = gpd.read_file(shp)
        except Exception as e:
            print(f"[WARN] Errore lettura {shp.name}: {e}")
            continue

        if gdf.empty:
            continue

        geom_col = gdf.geometry.name

        for idx, row in gdf.iterrows():
            all_attrs = {c: row.get(c) for c in gdf.columns if c != geom_col}

            id_sito_raw = all_attrs.get("ID SITO", all_attrs.get("Id Sito", all_attrs.get("id sito")))
            ubicazione_raw = all_attrs.get("UBICAZIONE", all_attrs.get("Ubicazione", all_attrs.get("ubicazione")))

            id_sito_num = extract_numeric_site_id(id_sito_raw)
            ubicazione_norm = normalize_text(ubicazione_raw)

            x, y = geometry_xy(row.geometry)

            rows.append({
                "shape_file": shp.name,
                "row_index": idx,
                "id_sito_raw": id_sito_raw,
                "id_sito_num": id_sito_num,
                "ubicazione_raw": ubicazione_raw,
                "ubicazione_norm": ubicazione_norm,
                "x": x,
                "y": y,
                "all_attrs": all_attrs,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("Nessuna feature valida letta dagli shapefile.")
    return df


def score_match(csv_info, shp_row):
    score = 0
    reasons = []

    # 1) Match su numero sito
    if csv_info["csv_site_num"] is not None and shp_row["id_sito_num"] is not None:
        if csv_info["csv_site_num"] == shp_row["id_sito_num"]:
            score += 60
            reasons.append("site_id exact")
        else:
            diff = abs(csv_info["csv_site_num"] - shp_row["id_sito_num"])
            if diff == 1:
                score += 5
                reasons.append("site_id near")

    # 2) Match tra location hint del CSV e UBICAZIONE
    loc_hint = csv_info["csv_location_hint"]
    ubicazione = shp_row["ubicazione_norm"]

    if loc_hint and ubicazione:
        if loc_hint in ubicazione or ubicazione in loc_hint:
            score += 30
            reasons.append("ubicazione contains")

        fuzzy_loc = similarity(loc_hint, ubicazione)
        score += int(fuzzy_loc * 0.5)
        reasons.append(f"ubicazione fuzzy={fuzzy_loc}")
    else:
        fuzzy_loc = 0

    # 3) fallback sul testo completo degli attributi
    attrs_text = " ".join(
        normalize_text(v) for v in shp_row["all_attrs"].values()
        if v is not None and not pd.isna(v)
    )
    fuzzy_attrs = similarity(csv_info["csv_stem"], attrs_text)
    score += int(fuzzy_attrs * 0.2)
    reasons.append(f"attrs fuzzy={fuzzy_attrs}")

    return score, fuzzy_loc, fuzzy_attrs, "; ".join(reasons)


def classify_confidence(score, exact_site, fuzzy_loc):
    if exact_site and fuzzy_loc >= 70:
        return "high"
    if exact_site:
        return "high"
    if score >= 70:
        return "medium"
    if score >= 45:
        return "low"
    return "very_low"


def find_best_matches(csv_dir: Path, shp_df: pd.DataFrame):
    csv_files = sorted(csv_dir.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"Nessun CSV trovato in: {csv_dir.resolve()}")

    out = []

    for csvf in csv_files:
        info = parse_csv_filename(csvf.name)
        candidates = []

        for _, shp_row in shp_df.iterrows():
            score, fuzzy_loc, fuzzy_attrs, reasons = score_match(info, shp_row)

            if score <= 0 and max(fuzzy_loc, fuzzy_attrs) < MIN_FUZZY_SCORE:
                continue

            exact_site = (
                info["csv_site_num"] is not None
                and shp_row["id_sito_num"] is not None
                and info["csv_site_num"] == shp_row["id_sito_num"]
            )

            candidates.append({
                "csv_file": info["csv_file"],
                "csv_site_num": info["csv_site_num"],
                "csv_tlc_unit": info["csv_tlc_unit"],
                "csv_location_hint": info["csv_location_hint"],

                "matched_shape_file": shp_row["shape_file"],
                "matched_row_index": shp_row["row_index"],
                "matched_id_sito": shp_row["id_sito_raw"],
                "matched_ubicazione": shp_row["ubicazione_raw"],
                "x": shp_row["x"],
                "y": shp_row["y"],

                "score": score,
                "fuzzy_ubicazione": fuzzy_loc,
                "fuzzy_attrs": fuzzy_attrs,
                "exact_site_match": exact_site,
                "match_reasons": reasons,
            })

        if not candidates:
            out.append({
                "csv_file": info["csv_file"],
                "csv_site_num": info["csv_site_num"],
                "csv_tlc_unit": info["csv_tlc_unit"],
                "csv_location_hint": info["csv_location_hint"],
                "matched_shape_file": None,
                "matched_row_index": None,
                "matched_id_sito": None,
                "matched_ubicazione": None,
                "x": None,
                "y": None,
                "score": 0,
                "fuzzy_ubicazione": 0,
                "fuzzy_attrs": 0,
                "exact_site_match": False,
                "match_reasons": "no match",
                "confidence": "none",
                "ambiguity": "",
            })
            continue

        candidates = sorted(
            candidates,
            key=lambda r: (r["exact_site_match"], r["score"], r["fuzzy_ubicazione"], r["fuzzy_attrs"]),
            reverse=True
        )

        best = candidates[0]
        second = candidates[1] if len(candidates) > 1 else None

        best["confidence"] = classify_confidence(
            best["score"], best["exact_site_match"], best["fuzzy_ubicazione"]
        )
        best["ambiguity"] = ""
        if second and abs(best["score"] - second["score"]) <= 10:
            best["ambiguity"] = "ambiguous"

        out.append(best)

    return pd.DataFrame(out)


def main():
    print("[1/3] Lettura shapefile...")
    shp_df = read_all_shapefiles(SHAPE_DIR)
    print(f"Feature lette: {len(shp_df)}")

    print("[2/3] Matching CSV ↔ shapefile...")
    result_df = find_best_matches(CSV_DIR, shp_df)

    print("[3/3] Scrittura output...")
    result_df = result_df.sort_values(
        by=["exact_site_match", "score", "fuzzy_ubicazione"],
        ascending=[False, False, False]
    )
    result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\n✅ Output scritto in: {OUTPUT_CSV.resolve()}")
    print(result_df.head(30).to_string(index=False))


if __name__ == "__main__":
    main()