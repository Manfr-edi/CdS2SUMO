#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Matched CSV → SUMO POI Generator

This script generates SUMO POIs (Points of Interest) corresponding to traffic
monitoring locations that have been successfully associated with CSV datasets.

It uses the output produced by the CSV–Shapefile matching step
(csv_shapefile_matches.csv) and converts the matched GIS points into SUMO
coordinates.

Workflow:

1. Load the CSV–shapefile matching results.
2. Keep only reliable matches (confidence level "medium" or "high").
3. Optionally discard ambiguous matches.
4. Ensure that each shapefile feature is associated with at most one CSV file
   (to avoid duplicate assignments such as multiple CSVs mapped to the same
   monitoring point).
5. Retrieve the corresponding geometry from the original shapefile.
6. Convert geographic coordinates (WGS84) into SUMO network coordinates using
   the SUMO network file.
7. Generate a SUMO additional file containing POIs representing the monitoring
   locations.

Each generated POI contains:

- a descriptive identifier derived from the dataset type, site identifier,
  location name, and CSV filename
- SUMO coordinates
- parameters storing the original CSV file, confidence score, shapefile
  reference, and original GIS attributes

The resulting additional file can be visualized in SUMO using:

    sumo-gui -n <network.net.xml> -a matched_csv_pois.add.xml

This allows the traffic measurement points to be inspected directly in the
simulation environment and used as reference locations for traffic demand
calibration or detector placement.
"""
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET

import pandas as pd
import geopandas as gpd
import sumolib
from shapely.geometry import Point


# =========================
# CONFIG
# =========================
NET_FILE = r"../sumo_data/osm.net.xml"
GIS_DIR = r"../gis_data/TLC"
MATCHES_CSV = r"../processed/csv_shapefile_matches.csv"
OUT_ADDITIONAL = r"../processed/matched_csv_pois.add.xml"

# tieni solo questi livelli di confidenza
ALLOWED_CONFIDENCE = {"medium", "high"}

# se True, esclude anche i match marcati come ambigui
DROP_AMBIGUOUS = True

# colori per tipo dataset
COLORS = {
    "anpr": "1,0.4,0",
    "tlc": "0,0.6,1",
    "tlc_plugin": "0.2,1,0.2",
    "gis": "1,1,0"
}


# =========================
# UTILS
# =========================
def normalize_text(s):
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("-", "_").replace(" ", "_").replace("/", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        return gdf.set_crs(epsg=4326, allow_override=True)
    return gdf.to_crs(epsg=4326)


def to_xy(net, lon, lat):
    x, y = net.convertLonLat2XY(lon, lat)
    return float(x), float(y)


def add_params(elem, props: dict):
    for k, v in props.items():
        if v is None:
            continue
        s = str(v)
        if len(s) > 300:
            s = s[:300]
        ET.SubElement(elem, "param", {"key": str(k), "value": s})


def detect_dataset_prefix(shape_file_name: str) -> str:
    name = shape_file_name.lower()
    if "targ" in name or "lettura" in name:
        return "anpr"
    elif "plugin" in name or "plug-in" in name:
        return "tlc_plugin"
    elif "tlc" in name or "telecamere" in name:
        return "tlc"
    return "gis"


def load_all_shapefiles(gis_dir: Path):
    shp_map = {}
    for shp in gis_dir.rglob("*.shp"):
        gdf = gpd.read_file(shp)
        gdf = ensure_wgs84(gdf)
        shp_map[shp.name] = {"path": shp, "gdf": gdf}
    if not shp_map:
        raise FileNotFoundError(f"Nessun shapefile trovato in {gis_dir.resolve()}")
    return shp_map


def choose_unique_matches(df: pd.DataFrame) -> pd.DataFrame:
    # filtro base
    df = df.copy()
    df["confidence"] = df["confidence"].astype(str).str.lower().str.strip()
    df = df[df["confidence"].isin(ALLOWED_CONFIDENCE)]

    if DROP_AMBIGUOUS and "ambiguity" in df.columns:
        df = df[df["ambiguity"].fillna("").astype(str).str.lower().str.strip() != "ambiguous"]

    # rimuovi righe senza riferimento shapefile
    df = df[
        df["matched_shape_file"].notna() &
        df["matched_row_index"].notna()
    ].copy()

    # normalizza score
    if "score" not in df.columns:
        df["score"] = 0
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)

    # chiave univoca del punto shapefile
    df["shape_key"] = (
        df["matched_shape_file"].astype(str) + "::" +
        df["matched_row_index"].astype(str)
    )

    # prima: per ogni CSV tieni il match migliore
    df = df.sort_values(["csv_file", "score"], ascending=[True, False])
    df = df.drop_duplicates(subset=["csv_file"], keep="first")

    # poi: per ogni punto shapefile tieni un solo CSV
    df = df.sort_values(["shape_key", "score"], ascending=[True, False])
    df = df.drop_duplicates(subset=["shape_key"], keep="first")

    return df


def build_poi_id(row):
    shape_file = str(row["matched_shape_file"])
    prefix = detect_dataset_prefix(shape_file)

    csv_stem = Path(str(row["csv_file"])).stem
    csv_norm = normalize_text(csv_stem)

    site = normalize_text(row.get("matched_id_sito", ""))
    ubic = normalize_text(row.get("matched_ubicazione", ""))

    parts = [prefix]
    if site:
        parts.append(site)
    if ubic:
        parts.append(ubic)
    parts.append(csv_norm)

    poi_id = "_".join([p for p in parts if p])
    poi_id = re.sub(r"_+", "_", poi_id).strip("_")
    return poi_id[:180]


# =========================
# MAIN
# =========================
def main():
    net = sumolib.net.readNet(NET_FILE)
    gis_dir = Path(GIS_DIR)

    if not gis_dir.exists():
        raise FileNotFoundError(f"Cartella GIS non trovata: {gis_dir.resolve()}")

    matches = pd.read_csv(MATCHES_CSV)
    selected = choose_unique_matches(matches)

    if selected.empty:
        raise RuntimeError("Nessun match valido trovato dopo i filtri.")

    shp_map = load_all_shapefiles(gis_dir)

    root = ET.Element("additional")
    created = 0
    skipped = 0

    for _, m in selected.iterrows():
        shape_file = str(m["matched_shape_file"])
        row_index = m["matched_row_index"]

        if shape_file not in shp_map:
            print(f"[WARN] Shapefile non trovato in GIS_DIR: {shape_file}")
            skipped += 1
            continue

        gdf = shp_map[shape_file]["gdf"]

        try:
            row_index = int(row_index)
        except Exception:
            print(f"[WARN] matched_row_index non valido: {row_index}")
            skipped += 1
            continue

        if row_index not in gdf.index:
            print(f"[WARN] row_index {row_index} non presente in {shape_file}")
            skipped += 1
            continue

        feat = gdf.loc[row_index]
        geom = feat.geometry

        if geom is None or geom.is_empty:
            skipped += 1
            continue

        # se non è Point, prova col representative_point
        if isinstance(geom, Point):
            pt = geom
        else:
            pt = geom.representative_point()

        lon, lat = float(pt.x), float(pt.y)
        x, y = to_xy(net, lon, lat)

        poi_id = build_poi_id(m)
        poi_type = detect_dataset_prefix(shape_file)
        color = COLORS.get(poi_type, COLORS["gis"])

        poi = ET.SubElement(root, "poi", {
            "id": poi_id,
            "x": f"{x:.3f}",
            "y": f"{y:.3f}",
            "type": poi_type,
            "color": color
        })

        # param dal match
        match_params = {
            "csv_file": m.get("csv_file"),
            "confidence": m.get("confidence"),
            "score": m.get("score"),
            "matched_shape_file": m.get("matched_shape_file"),
            "matched_row_index": m.get("matched_row_index"),
            "matched_id_sito": m.get("matched_id_sito"),
            "matched_ubicazione": m.get("matched_ubicazione"),
        }

        # param dagli attributi originali shapefile
        feat_params = {}
        for c in gdf.columns:
            if c == gdf.geometry.name:
                continue
            feat_params[f"shp_{c}"] = feat.get(c)

        add_params(poi, {**match_params, **feat_params})
        created += 1

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(OUT_ADDITIONAL, encoding="utf-8", xml_declaration=True)

    print(f"✅ POI creati: {created}")
    print(f"⚠️  Elementi saltati: {skipped}")
    print(f"📄 File scritto: {OUT_ADDITIONAL}")
    print(f"Per visualizzarlo:")
    print(f"  sumo-gui -n {NET_FILE} -a {OUT_ADDITIONAL}")


if __name__ == "__main__":
    main()