#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import hashlib
import xml.etree.ElementTree as ET

import geopandas as gpd
from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon
import sumolib


# ---------------------------
# CONFIG
# ---------------------------
NET_FILE = r"./sumo_data/osm.net.xml"           # <-- cambia se necessario
GIS_DIR  = r"./gis_data"          # <-- cartella dove estrai i .rar (shp/gpkg/kml/geojson)
OUT_ADDITIONAL = r"./mapped_gis.add.xml"      # output per SUMO

# Tipi che vuoi usare in SUMO (in base al nome file/layer)
TYPE_RULES = [
    ("targa",   "anpr"),
    ("lettura", "anpr"),
    ("camera",  "camera"),
    ("tlc",     "camera"),
    ("conteggio", "counter"),
]

DEFAULT_TYPE = "gis_object"

# Colori (SUMO: "r,g,b" 0..1)
COLORS = {
    "anpr":    "1,0.4,0",   # arancio
    "camera":  "0,0.6,1",   # azzurro
    "counter": "0.2,1,0.2", # verde
    "gis_object": "1,1,0"   # giallo
}

# ---------------------------
# UTILS
# ---------------------------
def pick_type(name: str) -> str:
    n = name.lower()
    for key, t in TYPE_RULES:
        if key in n:
            return t
    return DEFAULT_TYPE

def stable_id(prefix: str, layer_name: str, idx: int, geom_wkt: str) -> str:
    h = hashlib.md5((layer_name + str(idx) + geom_wkt).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}"

def to_xy(net, lon, lat):
    x, y = net.convertLonLat2XY(lon, lat)
    return float(x), float(y)

def add_params(elem, props: dict):
    # salva qualche attributo come <param key="..." value="..."/>
    for k, v in props.items():
        if v is None:
            continue
        s = str(v)
        if len(s) > 200:
            s = s[:200]
        ET.SubElement(elem, "param", {"key": str(k), "value": s})

def geom_to_lonlat(g):
    # ritorna lista di (lon,lat) per LineString/Polygon esterni
    if isinstance(g, LineString):
        return list(g.coords)
    if isinstance(g, Polygon):
        return list(g.exterior.coords)
    return []

def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    # Se manca CRS, non possiamo riproiettare: proviamo ad assumere EPSG:4326 (WGS84)
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326, allow_override=True)
        return gdf
    # porta tutto a WGS84 (lon/lat)
    return gdf.to_crs(epsg=4326)

def iter_vector_files(folder: Path):
    exts = (".shp", ".geojson", ".json", ".gpkg", ".kml")
    for p in folder.rglob("*"):
        if p.suffix.lower() in exts:
            yield p

def read_any_vector(path: Path) -> gpd.GeoDataFrame:
    # geopandas legge shapefile/geojson/gpkg/kml (kml richiede driver disponibile)
    try:
        return gpd.read_file(path)
    except Exception as e:
        raise RuntimeError(f"Impossibile leggere {path}: {e}")

def detect_dataset_prefix(path: Path) -> str:
    name = path.name.lower()

    if "targ" in name or "lettura" in name:
        return "anpr"
    elif "plugin" in name or "plug-in" in name:
        return "tlc_plugin"
    elif "tlc" in name or "telecamere" in name:
        return "tlc"
    else:
        return "gis"


# ---------------------------
# MAIN
# ---------------------------
def main():
    net = sumolib.net.readNet(NET_FILE)

    gis_dir = Path(GIS_DIR)
    if not gis_dir.exists():
        raise FileNotFoundError(f"Cartella GIS non trovata: {gis_dir.resolve()}")

    root = ET.Element("additional")

    files = list(iter_vector_files(gis_dir))
    if not files:
        raise RuntimeError(f"Nessun file GIS trovato in {gis_dir.resolve()} (shp/gpkg/geojson/kml).")

    for vf in files:
        layer_name = vf.stem
        obj_type = pick_type(vf.name)
        color = COLORS.get(obj_type, COLORS[DEFAULT_TYPE])

        gdf = read_any_vector(vf)
        gdf = ensure_wgs84(gdf)

        # normalizza colonne proprietà
        cols = [c for c in gdf.columns if c != "geometry"]

        for i, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            props = {c: row.get(c) for c in cols}

            # --- Points
            if isinstance(geom, Point):
                dataset_prefix = detect_dataset_prefix(vf)

                # prova a prendere un campo ID reale dal layer
                possible_id_fields = ["id", "ID", "Id", "OBJECTID", "objectid", "camera_id", "tlc_id"]
                real_id = None

                for f in possible_id_fields:
                    if f in row and row[f] is not None:
                        real_id = str(row[f])
                        break

                if real_id:
                    poi_id = f"{dataset_prefix}_{real_id}"
                else:
                    poi_id = stable_id(dataset_prefix, layer_name, int(i), geom.wkt)

                lon, lat = geom.x, geom.y
                x, y = to_xy(net, lon, lat)

                #pid = stable_id("poi", layer_name, int(i), geom.wkt)
                poi = ET.SubElement(root, "poi", {
                    "id": poi_id,
                    "x": f"{x:.3f}",
                    "y": f"{y:.3f}",
                    "type": dataset_prefix,
                    "color": color
                })
                add_params(poi, {"source_file": vf.name, "layer": layer_name, **props})

            elif isinstance(geom, MultiPoint):
                for j, pt in enumerate(geom.geoms):
                    lon, lat = pt.x, pt.y
                    x, y = to_xy(net, lon, lat)
                    pid = stable_id("poi", layer_name, int(i)*1000 + j, pt.wkt)
                    poi = ET.SubElement(root, "poi", {
                        "id": pid, "x": f"{x:.3f}", "y": f"{y:.3f}",
                        "type": obj_type, "color": color
                    })
                    add_params(poi, {"source_file": vf.name, "layer": layer_name, **props})

            # --- Lines / Polygons -> <poly shape="x1,y1 x2,y2 ...">
            elif isinstance(geom, (LineString, Polygon)):
                coords = geom_to_lonlat(geom)
                if len(coords) < 2:
                    continue
                shape_xy = []
                for lon, lat in coords:
                    x, y = to_xy(net, lon, lat)
                    shape_xy.append(f"{x:.3f},{y:.3f}")

                poly_id = stable_id("poly", layer_name, int(i), geom.wkt)
                poly = ET.SubElement(root, "poly", {
                    "id": poly_id,
                    "type": obj_type,
                    "color": color,
                    "fill": "0",          # contorno (0) / pieno (1)
                    "layer": "100",       # sopra la rete
                    "shape": " ".join(shape_xy)
                })
                add_params(poly, {"source_file": vf.name, "layer": layer_name, **props})

            elif isinstance(geom, (MultiLineString, MultiPolygon)):
                for j, gg in enumerate(geom.geoms):
                    coords = geom_to_lonlat(gg)
                    if len(coords) < 2:
                        continue
                    shape_xy = []
                    for lon, lat in coords:
                        x, y = to_xy(net, lon, lat)
                        shape_xy.append(f"{x:.3f},{y:.3f}")

                    poly_id = stable_id("poly", layer_name, int(i)*1000 + j, gg.wkt)
                    poly = ET.SubElement(root, "poly", {
                        "id": poly_id,
                        "type": obj_type,
                        "color": color,
                        "fill": "0",
                        "layer": "100",
                        "shape": " ".join(shape_xy)
                    })
                    add_params(poly, {"source_file": vf.name, "layer": layer_name, **props})

            else:
                # altri tipi: ignoriamo
                continue

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(OUT_ADDITIONAL, encoding="utf-8", xml_declaration=True)

    print(f"✅ Scritto: {OUT_ADDITIONAL}")
    print("\nPer visualizzarlo:")
    print(f"  sumo-gui -n {NET_FILE} -a {OUT_ADDITIONAL}")


if __name__ == "__main__":
    main()