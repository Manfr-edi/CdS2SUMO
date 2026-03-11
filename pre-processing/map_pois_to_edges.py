#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
POI → SUMO Edge Mapping

This script associates each SUMO POI corresponding to a matched traffic
monitoring location with the closest SUMO edge in the network.

Only POIs linked to a CSV dataset are considered. For each POI, the script
searches for the nearest non-internal edge and stores the mapping in a CSV file
that can later be used to transform TLC traffic measurements into SUMO
edge-based counts.

Output:
- poi_edge_mapping.csv: mapping between CSV files, POIs, and SUMO edges
- optionally, an enriched POI additional file with nearest_edge as parameter
"""

from pathlib import Path
import math
import xml.etree.ElementTree as ET
import pandas as pd
import sumolib


# =========================
# CONFIG
# =========================
NET_FILE = r"../sumo_data/osm.net.xml"
POI_FILE = r"./matched_csv_pois.add.xml"
OUT_MAPPING_CSV = r"../processed/poi_edge_mapping.csv"
OUT_POI_ENRICHED = r"../processed/matched_csv_pois_with_edges.add.xml"

SEARCH_RADII = [20, 40, 80, 120, 200]
PREFERRED_VCLASSES = ["passenger", "bus", "truck", "motorcycle", "bicycle"]


# =========================
# XML / POI HELPERS
# =========================
def parse_pois(poi_file: str):
    tree = ET.parse(poi_file)
    root = tree.getroot()

    pois = []
    for poi in root.findall("poi"):
        params = {}
        for p in poi.findall("param"):
            params[p.attrib.get("key")] = p.attrib.get("value")

        csv_file = params.get("csv_file")
        if not csv_file:
            continue

        pois.append({
            "id": poi.attrib.get("id"),
            "x": float(poi.attrib["x"]),
            "y": float(poi.attrib["y"]),
            "type": poi.attrib.get("type", ""),
            "color": poi.attrib.get("color", ""),
            "csv_file": csv_file,
            "params": params,
            "xml_elem": poi,
        })

    return tree, root, pois


def is_valid_edge(edge):
    eid = edge.getID()
    if eid.startswith(":"):
        return False
    try:
        if edge.isSpecial():
            return False
    except Exception:
        pass
    return True


def edge_distance(edge, x, y):
    shape = edge.getShape()
    if not shape:
        return float("inf")

    best = float("inf")
    for px, py in shape:
        d = math.hypot(px - x, py - y)
        if d < best:
            best = d
    return best


def choose_best_edge(net, x, y):
    """
    Cerca l'edge più vicino, preferendo edge stradali non interni.
    """
    for radius in SEARCH_RADII:
        try:
            neighbors = net.getNeighboringEdges(x, y, radius)
        except Exception:
            neighbors = []

        valid = []
        for edge, dist in neighbors:
            if not is_valid_edge(edge):
                continue
            valid.append((edge, dist))

        if valid:
            valid.sort(key=lambda t: t[1])
            return valid[0][0], valid[0][1]

    # fallback globale: scansione su tutti gli edge
    best_edge = None
    best_dist = float("inf")
    for edge in net.getEdges():
        if not is_valid_edge(edge):
            continue
        dist = edge_distance(edge, x, y)
        if dist < best_dist:
            best_edge = edge
            best_dist = dist

    return best_edge, best_dist


# =========================
# MAIN
# =========================
def main():
    net = sumolib.net.readNet(NET_FILE)
    tree, root, pois = parse_pois(POI_FILE)

    if not pois:
        raise RuntimeError("Nessun POI con parametro csv_file trovato.")

    rows = []

    for poi in pois:
        edge, dist = choose_best_edge(net, poi["x"], poi["y"])
        if edge is None:
            continue

        edge_id = edge.getID()
        rows.append({
            "csv_file": poi["csv_file"],
            "poi_id": poi["id"],
            "poi_type": poi["type"],
            "poi_x": poi["x"],
            "poi_y": poi["y"],
            "edge_id": edge_id,
            "distance_to_edge": round(float(dist), 3),
        })

        # arricchisce il poi xml con edge info
        ET.SubElement(poi["xml_elem"], "param", {"key": "nearest_edge", "value": edge_id})
        ET.SubElement(poi["xml_elem"], "param", {"key": "distance_to_edge", "value": str(round(float(dist), 3))})

    df = pd.DataFrame(rows)

    # un solo edge per CSV
    df = df.sort_values(["csv_file", "distance_to_edge"], ascending=[True, True])
    df = df.drop_duplicates(subset=["csv_file"], keep="first")

    df.to_csv(OUT_MAPPING_CSV, index=False, encoding="utf-8-sig")

    ET.indent(tree, space="  ", level=0)
    tree.write(OUT_POI_ENRICHED, encoding="utf-8", xml_declaration=True)

    print(f"✅ Mapping scritto in: {Path(OUT_MAPPING_CSV).resolve()}")
    print(f"✅ POI arricchiti scritti in: {Path(OUT_POI_ENRICHED).resolve()}")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()