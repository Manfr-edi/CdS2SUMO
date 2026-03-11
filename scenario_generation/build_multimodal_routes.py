#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Multimodal SUMO Route Generator

This script creates:
1. a SUMO additional file defining the vehicle types involved in the simulation
   (types.add.xml)
2. one random trip file and one sampled route file for each transport mode,
   starting from the corresponding edgeData file.

The workflow follows the same logic used in Planner.py:
- first generate random candidate trips with randomTrips.py
- then sample consistent routes with routeSampler.py using edgeData constraints

For each mode, the generated trips and routes are tagged with the corresponding
vehicle type id defined in types.add.xml.
"""

from pathlib import Path
import os
import sys
import subprocess
import xml.etree.ElementTree as ET


# =========================================================
# CONFIG
# =========================================================
SUMO_TOOLS_PATH = r"C:/Program Files (x86)/Eclipse/Sumo/tools"   # <- cambia
SUMO_NET_FILE = r"../sumo_data/osm.net.xml"                       # <- cambia
EDGE_DATA_DIR = r"../processed/edgedata_out"                                # <- cambia
OUT_DIR = r"../sumo_data/routes"                                 # <- cambia

# Se vuoi usare sys.executable lascia True
USE_CURRENT_PYTHON = True

# Parametri randomTrips / routeSampler
RANDOMTRIPS_PERIOD = "0.1"
FRINGE_FACTOR = "10"
MIN_DISTANCE = "100"
MAX_DISTANCE = "1000"
RANDOM_ROUTING_FACTOR = "10"
THREADS = "8"

# Se None, viene calcolato come somma dei conteggi nell'edgedata
TOTAL_COUNT_OVERRIDE = {
    "bicycle": None,
    "motorcycle": None,
    "passenger": None,
    "truck": None,
    "bus": None,
}

# Config multimodale:
# edgeData file -> vType id -> SUMO vClass
MODE_CONFIG = {
    "bicycle": {
        "edgedata_file": "edgedata_bicycle.xml",
        "vtype_id": "bicycle",
        "vclass": "bicycle",
    },
    "motorcycle": {
        "edgedata_file": "edgedata_motorcycle.xml",
        "vtype_id": "motorcycle",
        "vclass": "motorcycle",
    },
    "passenger": {
        "edgedata_file": "edgedata_passenger.xml",
        "vtype_id": "car",
        "vclass": "passenger",
    },
    "truck": {
        "edgedata_file": "edgedata_truck.xml",
        "vtype_id": "heavy",
        "vclass": "truck",
    },
    "bus": {
        "edgedata_file": "edgedata_bus.xml",
        "vtype_id": "bus",
        "vclass": "bus",
    },
}


# =========================================================
# HELPERS
# =========================================================
def python_cmd():
    return sys.executable if USE_CURRENT_PYTHON else "python"


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def parse_total_count_from_edgedata(edgedata_path: Path) -> int:
    tree = ET.parse(edgedata_path)
    root = tree.getroot()
    total = 0
    for interval in root.findall("interval"):
        for edge in interval.findall("edge"):
            entered = edge.attrib.get("entered", "0")
            try:
                total += int(float(entered))
            except Exception:
                pass
    return total


def write_types_add_xml(out_file: Path, mode_config: dict):
    root = ET.Element("additional")

    for _, cfg in mode_config.items():
        attrs = {
            "id": cfg["vtype_id"],
            "vClass": cfg["vclass"],
        }

        # qualche default utile
        if cfg["vclass"] == "passenger":
            attrs.update({
                "length": "5.0",
                "minGap": "2.5",
                "maxSpeed": "13.9"
            })
        elif cfg["vclass"] == "truck":
            attrs.update({
                "length": "12.0",
                "minGap": "3.0",
                "maxSpeed": "11.1"
            })
        elif cfg["vclass"] == "bus":
            attrs.update({
                "length": "12.0",
                "minGap": "3.0",
                "maxSpeed": "11.1"
            })
        elif cfg["vclass"] == "motorcycle":
            attrs.update({
                "length": "2.2",
                "minGap": "1.5",
                "maxSpeed": "13.9"
            })
        elif cfg["vclass"] == "bicycle":
            attrs.update({
                "length": "1.8",
                "minGap": "0.5",
                "maxSpeed": "5.5"
            })

        ET.SubElement(root, "vType", attrs)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_file, encoding="utf-8", xml_declaration=True)


def run_cmd(cmd, desc):
    print(f"\n[RUN] {desc}")
    print(" ".join(cmd))
    result = subprocess.run(cmd,  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                     env=os.environ.copy(), bufsize=1)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {desc}")


def generate_random_trips_for_mode(
    net_file: Path,
    out_dir: Path,
    mode_name: str,
    vtype_id: str,
    vclass: str,
):
    random_trips_script = str(Path(SUMO_TOOLS_PATH) / "randomTrips.py")

    mode_dir = out_dir / mode_name
    ensure_dir(mode_dir)

    random_routes_file = mode_dir / "randomTrips.rou.xml"
    trips_file = mode_dir / "trips.rou.xml"

    cmd = [
        python_cmd(),
        random_trips_script,
        "-n", str(net_file),
        "-r", str(random_routes_file),
        "--output-trip-file", str(trips_file),
        "--trip-attributes", f"type='{vtype_id}'",
        #"--vehicle-class", vclass,
        "--random-departpos",
        "--random-arrivalpos",
        "--allow-fringe",
        "--random",
        "--remove-loops",
        "--fringe-factor", FRINGE_FACTOR,
        "--min-distance", MIN_DISTANCE,
        "--max-distance", MAX_DISTANCE,
        "--random-routing-factor", RANDOM_ROUTING_FACTOR,
        "--period", RANDOMTRIPS_PERIOD,
    ]

    run_cmd(cmd, f"randomTrips for {mode_name}")
    return random_routes_file, trips_file


def sample_routes_for_mode(
    edgedata_file: Path,
    random_routes_file: Path,
    out_dir: Path,
    mode_name: str,
    vtype_id: str,
    total_count: int,
):
    route_sampler_script = str(Path(SUMO_TOOLS_PATH) / "routeSampler.py")
    script = SUMO_TOOLS_PATH + "/routeSampler.py"
    mode_dir = out_dir / mode_name
    ensure_dir(mode_dir)

    output_route_file = mode_dir / f"generatedRoutes_{mode_name}.rou.xml"

    type = f"type='{vtype_id}'"
    cmd = [
        python_cmd(),
        script,
        "--r", str(random_routes_file),
        "--edgedata-files", str(edgedata_file),
        "-o", str(output_route_file),
        "--edgedata-attribute", "entered",
       # "--write-flows", "number",
        "--attributes", f"type='{vtype_id}'",
        "--prefix",  str(vtype_id),
        "--total-count", str(total_count),
        #"--optimize", "full",
        "--minimize-vehicles", "1",
        "--threads", THREADS,
        "--verbose",
    ]

    run_cmd(cmd, f"routeSampler for {mode_name}")
    return output_route_file


def write_sumocfg_example(out_dir: Path, net_file: Path, types_file: Path, route_files: list[Path]):
    cfg_file = out_dir / "multimodal_example.sumocfg"

    root = ET.Element("configuration")

    input_el = ET.SubElement(root, "input")
    ET.SubElement(input_el, "net-file", {"value": str(net_file)})
    ET.SubElement(
        input_el,
        "additional-files",
        {"value": str(types_file)}
    )
    ET.SubElement(
        input_el,
        "route-files",
        {"value": ",".join(str(p) for p in route_files)}
    )

    time_el = ET.SubElement(root, "time")
    ET.SubElement(time_el, "begin", {"value": "0"})
    ET.SubElement(time_el, "end", {"value": "3600"})

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(cfg_file, encoding="utf-8", xml_declaration=True)

    return cfg_file


# =========================================================
# MAIN
# =========================================================
def main():
    net_file = Path(SUMO_NET_FILE)
    edge_data_dir = Path(EDGE_DATA_DIR)
    out_dir = Path(OUT_DIR)

    if not net_file.exists():
        raise FileNotFoundError(f"SUMO net file not found: {net_file.resolve()}")
    if not edge_data_dir.exists():
        raise FileNotFoundError(f"edgeData directory not found: {edge_data_dir.resolve()}")
    if not Path(SUMO_TOOLS_PATH).exists():
        raise FileNotFoundError(f"SUMO_TOOLS_PATH not found: {Path(SUMO_TOOLS_PATH).resolve()}")

    ensure_dir(out_dir)

    # 1) types.add.xml
    types_file = out_dir / "types.add.xml"
    write_types_add_xml(types_file, MODE_CONFIG)
    print(f"✅ types.add.xml written to: {types_file.resolve()}")

    route_files = []

    # 2) randomTrips + routeSampler per modalità
    for mode_name, cfg in MODE_CONFIG.items():
        edgedata_file = edge_data_dir / cfg["edgedata_file"]

        if not edgedata_file.exists():
            print(f"⚠️ Skipping {mode_name}: missing {edgedata_file.name}")
            continue

        total_count = TOTAL_COUNT_OVERRIDE.get(mode_name)
        if total_count is None:
            total_count = parse_total_count_from_edgedata(edgedata_file)

        if total_count <= 0:
            print(f"⚠️ Skipping {mode_name}: total_count is 0")
            continue

        print(f"\n=== MODE: {mode_name} ===")
        print(f"edgeData: {edgedata_file}")
        print(f"vehicle type id: {cfg['vtype_id']}")
        print(f"vClass: {cfg['vclass']}")
        print(f"total_count: {total_count}")

        random_routes_file, _ = generate_random_trips_for_mode(
            net_file=net_file,
            out_dir=out_dir,
            mode_name=mode_name,
            vtype_id=cfg["vtype_id"],
            vclass=cfg["vclass"],
        )

        route_file = sample_routes_for_mode(
            edgedata_file=edgedata_file,
            random_routes_file=random_routes_file,
            out_dir=out_dir,
            mode_name=mode_name,
            vtype_id=cfg["vtype_id"],
            total_count=total_count,
        )

        route_files.append(route_file)

    # 3) sumocfg di esempio
    if route_files:
        cfg_file = write_sumocfg_example(out_dir, net_file, types_file, route_files)
        print(f"\n✅ Example SUMO config written to: {cfg_file.resolve()}")

    print("\nDone.")


if __name__ == "__main__":
    main()