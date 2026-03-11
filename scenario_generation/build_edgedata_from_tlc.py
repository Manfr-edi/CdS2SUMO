#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TLC CSV → SUMO edgeData Generator

This script aggregates hourly traffic counts collected by TLC monitoring points
and converts them into SUMO edgeData files.

It uses a previously generated CSV-to-edge mapping file, selects the traffic
measurements for a given day and one-hour time slot, and writes one edgeData
file for each traffic mode available in the CSV datasets.

Supported modes:
- bicycle
- motorcycle
- passenger car
- truck
- bus
"""

from pathlib import Path
import xml.etree.ElementTree as ET
import pandas as pd


# =========================
# CONFIG
# =========================
CSV_DIR = Path(r"../sensor_measures_castellammare")
MAPPING_CSV = Path(r"../processed/poi_edge_mapping.csv")
OUT_DIR = Path(r"../processed/edgedata_out")

# giorno e timeslot scelti
TARGET_DATE = "2026-01-30"   # formato YYYY-MM-DD
TARGET_HOUR = 15             # 0..23

# intervallo SUMO del file edgedata
INTERVAL_BEGIN = 0
INTERVAL_END = 3600

# mapping colonne CSV -> nome file/mode
MODE_CONFIG = {
    "Bicicletta": "bicycle",
    "Moto": "motorcycle",
    "Auto": "passenger",
    "Camion": "truck",
    "Autobus": "bus",
}


# =========================
# CSV HELPERS
# =========================
def load_mapping(mapping_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(mapping_csv)
    required = {"csv_file", "edge_id"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Colonne mancanti nel mapping: {missing}")
    return df


def load_tlc_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, sep=";")

    expected = ["Data e Ora", "Intervallo", "Bicicletta", "Moto", "Auto", "Camion", "Autobus"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise RuntimeError(f"{csv_path.name}: colonne mancanti {missing}")

    df["timestamp"] = pd.to_datetime(df["Data e Ora"], format="%d/%m/%Y - %H:%M:%S", errors="coerce")
    df = df.dropna(subset=["timestamp"])

    for c in ["Bicicletta", "Moto", "Auto", "Camion", "Autobus"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    return df


def select_hour_row(df: pd.DataFrame, target_date: str, target_hour: int) -> pd.DataFrame:
    target_date = pd.to_datetime(target_date).date()
    sel = df[
        (df["timestamp"].dt.date == target_date) &
        (df["timestamp"].dt.hour == target_hour)
    ].copy()
    return sel


# =========================
# XML WRITER
# =========================
def write_edgedata_xml(edge_counts: dict, out_file: Path, begin=0, end=3600):
    root = ET.Element("data")
    interval = ET.SubElement(root, "interval", {
        "begin": str(begin),
        "end": str(end),
    })

    for edge_id, count in sorted(edge_counts.items()):
        ET.SubElement(interval, "edge", {
            "id": str(edge_id),
            "entered": str(int(count)),
        })

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_file, encoding="utf-8", xml_declaration=True)


# =========================
# MAIN
# =========================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mapping_df = load_mapping(MAPPING_CSV)

    # accumulatore counts per modalità
    mode_edge_counts = {mode_out: {} for mode_out in MODE_CONFIG.values()}

    processed = []
    skipped = []

    for _, row in mapping_df.iterrows():
        csv_name = str(row["csv_file"])
        edge_id = str(row["edge_id"])
        csv_path = CSV_DIR / csv_name

        if not csv_path.exists():
            skipped.append((csv_name, "csv non trovato"))
            continue

        try:
            df = load_tlc_csv(csv_path)
            hour_df = select_hour_row(df, TARGET_DATE, TARGET_HOUR)

            if hour_df.empty:
                skipped.append((csv_name, "nessuna riga per giorno/ora"))
                continue

            # dovrebbe esserci una sola riga per quell'ora
            rec = hour_df.iloc[0]

            for csv_col, mode_out in MODE_CONFIG.items():
                count = int(rec[csv_col])
                mode_edge_counts[mode_out][edge_id] = mode_edge_counts[mode_out].get(edge_id, 0) + count

            processed.append((csv_name, edge_id))

        except Exception as e:
            skipped.append((csv_name, str(e)))

    # scrittura file
    out_files = []
    for mode_out, edge_counts in mode_edge_counts.items():
        out_file = OUT_DIR / f"edgedata_{mode_out}.xml"
        write_edgedata_xml(edge_counts, out_file, INTERVAL_BEGIN, INTERVAL_END)
        out_files.append(out_file)

    print(f"✅ Giorno selezionato: {TARGET_DATE}")
    print(f"✅ Ora selezionata: {TARGET_HOUR:02d}:00 - {TARGET_HOUR:02d}:59")
    print(f"✅ CSV processati: {len(processed)}")
    print(f"⚠️  CSV saltati: {len(skipped)}")

    if skipped:
        print("\nDettaglio saltati:")
        for item in skipped:
            print(" -", item)

    print("\nFile creati:")
    for f in out_files:
        print(" -", f.resolve())


if __name__ == "__main__":
    main()