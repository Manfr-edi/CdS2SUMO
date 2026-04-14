#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SUMO Scenario Dashboard

Interactive Streamlit dashboard to build a SUMO simulation scenario starting
from mapped TLC CSV measurements.

Run with:
    streamlit run sumo_scenario_dashboard_fixed.py
"""

from __future__ import annotations

import math
import os
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from matplotlib.collections import LineCollection
from matplotlib.colors import LogNorm


# ============================================================================
# CONFIGURATION
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
HEATMAP_CO2_VMIN = 1e4
HEATMAP_CO2_VMAX = 1e7


def get_default_sumo_paths() -> Tuple[str, str]:
    system = platform.system().lower()

    if system == "windows":
        return (
            r"C:/Program Files (x86)/Eclipse/Sumo/tools",
            r"C:/Program Files (x86)/Eclipse/Sumo/bin",
        )

    if system == "linux":
        tools_path = Path("/usr/share/sumo/tools")
        bin_dir = Path("/usr/bin")
        if not tools_path.exists() and Path("/usr/share/sumo").exists():
            tools_path = Path("/usr/share/sumo") / "tools"
        if not (bin_dir / "sumo").exists() and (Path("/usr/share/sumo") / "bin").exists():
            bin_dir = Path("/usr/share/sumo") / "bin"
        return (str(tools_path), str(bin_dir))

    return (
        r"C:/Program Files (x86)/Eclipse/Sumo/tools",
        r"C:/Program Files (x86)/Eclipse/Sumo/bin",
    )


DEFAULT_SUMO_TOOLS_PATH, DEFAULT_SUMO_BIN_DIR = get_default_sumo_paths()

DEFAULTS = {
    "sumo_tools_path": DEFAULT_SUMO_TOOLS_PATH,
    "sumo_bin_dir": DEFAULT_SUMO_BIN_DIR,
    "net_file": str(BASE_DIR / "sumo_data" / "osm.net.xml"),
    "base_sumocfg": str(BASE_DIR / "sumo_data" / "osm.sumocfg"),
    "poi_edge_mapping": str(BASE_DIR / "processed" / "poi_edge_mapping.csv"),
    "csv_dir": str(BASE_DIR / "sensor_measures_castellammare"),
    "output_dir": str(BASE_DIR / "scenario_output"),
}

MODE_CONFIG = {
    "Bicicletta": {
        "key": "bicycle",
        "vtype_id": "bicycle",
        "vclass": "bicycle",
        "edgedata_file": "edgedata_bicycle.xml",
        "route_file": "generatedRoutes_bicycle.rou.xml",
        "default_ratio": 0.05,
    },
    "Moto": {
        "key": "motorcycle",
        "vtype_id": "motorcycle",
        "vclass": "motorcycle",
        "edgedata_file": "edgedata_motorcycle.xml",
        "route_file": "generatedRoutes_motorcycle.rou.xml",
        "default_ratio": 0.15,
    },
    "Auto": {
        "key": "passenger",
        "vtype_id": "car",
        "vclass": "passenger",
        "edgedata_file": "edgedata_passenger.xml",
        "route_file": "generatedRoutes_passenger.rou.xml",
        "default_ratio": 0.65,
    },
    "Camion": {
        "key": "truck",
        "vtype_id": "heavy",
        "vclass": "truck",
        "edgedata_file": "edgedata_truck.xml",
        "route_file": "generatedRoutes_truck.rou.xml",
        "default_ratio": 0.10,
    },
    "Autobus": {
        "key": "bus",
        "vtype_id": "bus",
        "vclass": "bus",
        "edgedata_file": "edgedata_bus.xml",
        "route_file": "generatedRoutes_bus.rou.xml",
        "default_ratio": 0.05,
    },
}

CSV_EXPECTED_COLUMNS = ["Data e Ora", "Bicicletta", "Moto", "Auto", "Camion", "Autobus"]
PYTHON_EXE = sys.executable


# ============================================================================
# SESSION STATE INIT
# ============================================================================
if "mode_edge_counts" not in st.session_state:
    st.session_state["mode_edge_counts"] = None
if "used_df" not in st.session_state:
    st.session_state["used_df"] = None
if "selected_date" not in st.session_state:
    st.session_state["selected_date"] = None
if "selected_hour" not in st.session_state:
    st.session_state["selected_hour"] = None
if "modal_percentages" not in st.session_state:
    st.session_state["modal_percentages"] = None


# ============================================================================
# DATA STRUCTURES
# ============================================================================
@dataclass
class ScenarioPaths:
    root: Path
    edgedata_dir: Path
    routes_dir: Path
    output_dir: Path
    types_file: Path
    sumocfg_file: Path


# ============================================================================
# UTILS
# ============================================================================
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd: List[str], desc: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def parse_timestamp_column(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["Data e Ora"], format="%d/%m/%Y - %H:%M:%S", errors="coerce")


def normalize_percentages_to_100(percentages: Dict[str, float]) -> Dict[str, int]:
    keys = list(percentages.keys())
    clipped = {k: max(0.0, min(100.0, float(v))) for k, v in percentages.items()}
    total = sum(clipped.values())

    if total <= 0:
        out = {k: 0 for k in keys}
        if keys:
            out[keys[0]] = 100
        return out

    scaled = {k: clipped[k] * 100.0 / total for k in keys}
    floored = {k: int(math.floor(v)) for k, v in scaled.items()}
    remainder = 100 - sum(floored.values())

    order = sorted(keys, key=lambda k: scaled[k] - floored[k], reverse=True)
    for i in range(remainder):
        floored[order[i % len(order)]] += 1

    return floored


def rebalance_percentages(changed_key: str, new_value: int, current: Dict[str, int]) -> Dict[str, int]:
    keys = list(current.keys())
    new_value = max(0, min(100, int(new_value)))

    if changed_key not in current:
        return current.copy()

    others = [k for k in keys if k != changed_key]
    remaining = 100 - new_value

    if remaining <= 0:
        return {k: (100 if k == changed_key else 0) for k in keys}

    others_sum = sum(current[k] for k in others)
    if others_sum <= 0:
        out = {k: 0 for k in keys}
        out[changed_key] = new_value
        if others:
            base = remaining // len(others)
            rem = remaining % len(others)
            for i, k in enumerate(others):
                out[k] = base + (1 if i < rem else 0)
        return out

    scaled = {k: current[k] * remaining / others_sum for k in others}
    floored = {k: int(math.floor(v)) for k, v in scaled.items()}
    rem = remaining - sum(floored.values())

    order = sorted(others, key=lambda k: scaled[k] - floored[k], reverse=True)
    for i in range(rem):
        floored[order[i % len(order)]] += 1

    out = {k: 0 for k in keys}
    out[changed_key] = new_value
    for k in others:
        out[k] = floored[k]
    return out


def percentages_to_counts(percentages: Dict[str, int], total: int) -> Dict[str, int]:
    if total <= 0:
        return {k: 0 for k in percentages}

    raw = {k: percentages[k] * total / 100.0 for k in percentages}
    base = {k: int(math.floor(v)) for k, v in raw.items()}
    remainder = total - sum(base.values())

    order = sorted(raw.keys(), key=lambda k: raw[k] - base[k], reverse=True)
    for i in range(remainder):
        base[order[i % len(order)]] += 1

    return base


def split_path_list(value: str) -> List[str]:
    return [p.strip() for p in str(value).split(",") if p.strip()]


def to_relpath(target: Path, base_dir: Path) -> str:
    try:
        return os.path.relpath(target, base_dir)
    except Exception:
        return str(target)


def resolve_input_path(item: str, original_cfg_dir: Path) -> Path:
    p = Path(item)
    if p.is_absolute():
        return p.resolve()

    candidates = [(original_cfg_dir / p).resolve()]

    rel_parts = list(p.parts)
    while rel_parts and rel_parts[0] == "..":
        rel_parts = rel_parts[1:]
        if rel_parts:
            candidates.append((BASE_DIR / Path(*rel_parts)).resolve())

    candidates.append((BASE_DIR / p.name).resolve())

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def resolve_existing_paths(raw_value: str, original_cfg_dir: Path, scenario_cfg_dir: Path) -> List[str]:
    resolved = []
    for item in split_path_list(raw_value):
        p = resolve_input_path(item, original_cfg_dir)
        resolved.append(to_relpath(p, scenario_cfg_dir))
    return resolved


def merge_path_lists(existing: List[str], new_items: List[str]) -> List[str]:
    merged = list(existing)
    seen = {x.replace('\\', '/').lower() for x in existing}
    for item in new_items:
        key = item.replace('\\', '/').lower()
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


# ============================================================================
# CSV / MAPPING DISCOVERY
# ============================================================================
@st.cache_data(show_spinner=False)
def load_mapping(mapping_csv: str) -> pd.DataFrame:
    df = pd.read_csv(mapping_csv)
    required = {"csv_file", "edge_id"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Missing columns in poi_edge_mapping.csv: {sorted(missing)}")
    df = df.dropna(subset=["csv_file", "edge_id"]).copy()
    df["csv_file"] = df["csv_file"].astype(str)
    df["edge_id"] = df["edge_id"].astype(str)
    return df


@st.cache_data(show_spinner=True)
def discover_available_slots(mapping_csv: str, csv_dir: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    mapping = load_mapping(mapping_csv)
    csv_root = Path(csv_dir)

    rows = []
    invalid = []

    for csv_name in sorted(mapping["csv_file"].unique()):
        csv_path = csv_root / csv_name
        if not csv_path.exists():
            invalid.append({"csv_file": csv_name, "reason": "missing file"})
            continue

        try:
            df = pd.read_csv(csv_path, sep=";")
            missing_cols = [c for c in CSV_EXPECTED_COLUMNS if c not in df.columns]
            if missing_cols:
                invalid.append({"csv_file": csv_name, "reason": f"missing columns: {missing_cols}"})
                continue

            ts = parse_timestamp_column(df)
            valid_mask = ts.notna()
            if not valid_mask.any():
                invalid.append({"csv_file": csv_name, "reason": "no valid timestamps"})
                continue

            valid_df = df.loc[valid_mask].copy()
            valid_df["timestamp"] = ts.loc[valid_mask]
            valid_df["date"] = valid_df["timestamp"].dt.date.astype(str)
            valid_df["hour"] = valid_df["timestamp"].dt.hour.astype(int)

            grouped = valid_df.groupby(["date", "hour"]).size().reset_index(name="rows")
            for _, r in grouped.iterrows():
                rows.append({
                    "csv_file": csv_name,
                    "date": r["date"],
                    "hour": int(r["hour"]),
                    "rows": int(r["rows"]),
                })
        except Exception as e:
            invalid.append({"csv_file": csv_name, "reason": str(e)})

    return pd.DataFrame(rows), pd.DataFrame(invalid)


@st.cache_data(show_spinner=False)
def valid_dates_for_mapped_csv(mapping_csv: str, csv_dir: str) -> List[str]:
    slots_df, _ = discover_available_slots(mapping_csv, csv_dir)
    if slots_df.empty:
        return []
    return sorted(slots_df["date"].unique().tolist())


@st.cache_data(show_spinner=False)
def valid_hours_for_date(mapping_csv: str, csv_dir: str, selected_date: str) -> List[int]:
    slots_df, _ = discover_available_slots(mapping_csv, csv_dir)
    if slots_df.empty:
        return []
    subset = slots_df[slots_df["date"] == selected_date]
    return sorted(subset["hour"].unique().tolist())


# ============================================================================
# EDGE DATA GENERATION
# ============================================================================
def load_tlc_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, sep=";")
    missing = [c for c in CSV_EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise RuntimeError(f"{csv_path.name}: missing columns {missing}")

    df["timestamp"] = parse_timestamp_column(df)
    df = df.dropna(subset=["timestamp"]).copy()

    for c in MODE_CONFIG.keys():
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

    return df


def build_edge_counts_for_slot(mapping_csv: str, csv_dir: str, target_date: str, target_hour: int) -> Tuple[Dict[str, Dict[str, int]], pd.DataFrame]:
    mapping_df = load_mapping(mapping_csv)
    csv_root = Path(csv_dir)

    mode_edge_counts = {cfg["key"]: {} for cfg in MODE_CONFIG.values()}
    used_rows = []

    for _, row in mapping_df.iterrows():
        csv_name = str(row["csv_file"])
        edge_id = str(row["edge_id"])
        csv_path = csv_root / csv_name
        if not csv_path.exists():
            continue

        df = load_tlc_csv(csv_path)
        sel = df[(df["timestamp"].dt.date.astype(str) == target_date) & (df["timestamp"].dt.hour == int(target_hour))]
        if sel.empty:
            continue

        rec = sel.iloc[0]
        entry = {"csv_file": csv_name, "edge_id": edge_id}
        for csv_mode, cfg in MODE_CONFIG.items():
            count = int(rec[csv_mode])
            mode_edge_counts[cfg["key"]][edge_id] = mode_edge_counts[cfg["key"]].get(edge_id, 0) + count
            entry[cfg["key"]] = count
        used_rows.append(entry)

    return mode_edge_counts, pd.DataFrame(used_rows)


def write_edgedata_xml(edge_counts: Dict[str, int], out_file: Path, begin: int = 0, end: int = 3600) -> None:
    root = ET.Element("data")
    interval = ET.SubElement(root, "interval", {"begin": str(begin), "end": str(end)})
    for edge_id, count in sorted(edge_counts.items()):
        ET.SubElement(interval, "edge", {"id": str(edge_id), "entered": str(int(count))})
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_file, encoding="utf-8", xml_declaration=True)


def write_scaled_edgedata_files(mode_edge_counts: Dict[str, Dict[str, int]], scaled_totals: Dict[str, int], out_dir: Path) -> Dict[str, Path]:
    ensure_dir(out_dir)
    out_files = {}

    for csv_mode, cfg in MODE_CONFIG.items():
        mode_key = cfg["key"]
        counts = mode_edge_counts.get(mode_key, {})
        measured_total = sum(counts.values())
        target_total = int(scaled_totals.get(mode_key, 0))

        if measured_total <= 0 or target_total <= 0:
            scaled_counts = {}
        else:
            raw = {edge: count * target_total / measured_total for edge, count in counts.items()}
            base = {edge: int(math.floor(v)) for edge, v in raw.items()}
            remainder = target_total - sum(base.values())
            if base:
                order = sorted(raw.keys(), key=lambda e: raw[e] - base[e], reverse=True)
                for i in range(remainder):
                    base[order[i % len(order)]] += 1
            scaled_counts = {edge: val for edge, val in base.items() if val > 0}

        out_path = out_dir / cfg["edgedata_file"]
        write_edgedata_xml(scaled_counts, out_path)
        out_files[mode_key] = out_path

    return out_files


# ============================================================================
# ROUTE GENERATION
# ============================================================================
def parse_total_from_edgedata(edgedata_path: Path) -> int:
    tree = ET.parse(edgedata_path)
    total = 0
    for interval in tree.getroot().findall("interval"):
        for edge in interval.findall("edge"):
            total += int(float(edge.attrib.get("entered", "0")))
    return total


def write_types_add_xml(out_file: Path) -> None:
    root = ET.Element("additional")
    for _, cfg in MODE_CONFIG.items():
        attrs = {"id": cfg["vtype_id"], "vClass": cfg["vclass"]}
        ET.SubElement(root, "vType", attrs)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_file, encoding="utf-8", xml_declaration=True)


def generate_routes_for_mode(
    sumo_tools_path: Path,
    net_file: Path,
    edgedata_file: Path,
    out_dir: Path,
    mode_label: str,
    vtype_id: str,
    vclass: str,
    period: str,
    fringe_factor: str,
    min_distance: str,
    max_distance: str,
    routing_factor: str,
    threads: str,
) -> Tuple[Path, str]:
    random_trips_script = sumo_tools_path / "randomTrips.py"
    route_sampler_script = sumo_tools_path / "routeSampler.py"

    mode_dir = out_dir / mode_label
    ensure_dir(mode_dir)

    random_routes = mode_dir / "randomTrips.rou.xml"
    trips_file = mode_dir / "trips.rou.xml"
    sampled_routes = mode_dir / MODE_CONFIG[mode_label]["route_file"]

    cmd1 = [
        PYTHON_EXE, str(random_trips_script),
        "-n", str(net_file),
        "-r", str(random_routes),
        "--output-trip-file", str(trips_file),
        "--trip-attributes", f"type='{vtype_id}'",
        "--random-departpos", "--random-arrivalpos",
        "--allow-fringe", "--random", "--remove-loops",
        "--fringe-factor", fringe_factor,
        "--min-distance", min_distance,
        "--max-distance", max_distance,
        "--random-routing-factor", routing_factor,
        "--period", period,
    ]
    res1 = run_cmd(cmd1, f"randomTrips {mode_label}")
    if res1.returncode != 0:
        raise RuntimeError(f"randomTrips failed for {mode_label}\nSTDOUT:\n{res1.stdout}\nSTDERR:\n{res1.stderr}")

    total_count = parse_total_from_edgedata(edgedata_file)
    if total_count <= 0:
        raise RuntimeError(f"No entered vehicles in {edgedata_file.name}")

    cmd2 = [
        PYTHON_EXE, str(route_sampler_script),
        "--r", str(random_routes),
        "--edgedata-files", str(edgedata_file),
        "-o", str(sampled_routes),
        "--edgedata-attribute", "entered",
        "--write-flows", "number",
        "--attributes", f"type='{vtype_id}'",
        "--prefix", str(vtype_id),
        "--total-count", str(total_count),
        "--optimize", "full",
        "--minimize-vehicles", "1",
        "--threads", threads,
        "--verbose",
    ]
    res2 = run_cmd(cmd2, f"routeSampler {mode_label}")
    if res2.returncode != 0:
        raise RuntimeError(f"routeSampler failed for {mode_label}\nSTDOUT:\n{res2.stdout}\nSTDERR:\n{res2.stderr}")

    return sampled_routes, res1.stdout + "\n" + res1.stderr + "\n" + res2.stdout + "\n" + res2.stderr


def copy_and_patch_base_sumocfg(base_sumocfg: Path, out_sumocfg: Path, net_file: Path, types_file: Path, route_files: List[Path]) -> None:
    """
    Copy the existing base SUMO config, preserve its structure, and patch only the
    path-based input entries so it works from the generated scenario folder.

    Existing additional-files and route-files are preserved and kept alongside
    the newly generated ones.
    """
    if not base_sumocfg.exists():
        raise FileNotFoundError(f"Base SUMO config not found: {base_sumocfg}")

    ensure_dir(out_sumocfg.parent)
    shutil.copy2(base_sumocfg, out_sumocfg)

    tree = ET.parse(out_sumocfg)
    root = tree.getroot()

    input_el = root.find("input")
    if input_el is None:
        input_el = ET.SubElement(root, "input")

    original_cfg_dir = base_sumocfg.parent.resolve()
    scenario_cfg_dir = out_sumocfg.parent.resolve()

    # net-file: always use the selected network, rewritten relative to scenario cfg
    net_el = input_el.find("net-file")
    if net_el is None:
        net_el = ET.SubElement(input_el, "net-file")
    net_el.set("value", to_relpath(net_file.resolve(), scenario_cfg_dir))

    # additional-files: preserve existing ones, but rewrite paths relative to scenario cfg,
    # then append the generated types.add.xml
    add_el = input_el.find("additional-files")
    existing_additional = []
    if add_el is not None and add_el.get("value"):
        existing_additional = resolve_existing_paths(add_el.get("value", ""), original_cfg_dir, scenario_cfg_dir)
    if add_el is None:
        add_el = ET.SubElement(input_el, "additional-files")
    merged_additional = merge_path_lists(
        existing_additional,
        [to_relpath(types_file.resolve(), scenario_cfg_dir)]
    )
    add_el.set("value", ",".join(merged_additional))

    # route-files: preserve existing ones if any, rewrite them, then append generated routes
    routes_el = input_el.find("route-files")
    # route-files: replace completely with the newly generated routes
    routes_el = input_el.find("route-files")
    if routes_el is None:
        routes_el = ET.SubElement(input_el, "route-files")

    generated_routes_rel = [to_relpath(p.resolve(), scenario_cfg_dir) for p in route_files]
    routes_el.set("value", ",".join(generated_routes_rel))

    # rewrite other common input file references relative to the new scenario cfg
    for child in list(input_el):
        if child.tag in {"net-file", "additional-files", "route-files"}:
            continue
        val = child.get("value")
        if not val:
            continue
        parts = split_path_list(val)
        if not parts:
            continue
        rewritten = resolve_existing_paths(val, original_cfg_dir, scenario_cfg_dir)
        child.set("value", ",".join(rewritten))

    ET.indent(tree, space="  ", level=0)
    tree.write(out_sumocfg, encoding="utf-8", xml_declaration=True)


def build_scenario_paths(output_root: Path, selected_date: str, selected_hour: int) -> ScenarioPaths:
    scenario_name = f"scenario_{selected_date}_{selected_hour:02d}00"
    root = output_root / scenario_name
    edgedata_dir = root / "edgedata"
    routes_dir = root / "routes"
    output_dir = root / "output"

    ensure_dir(root)
    ensure_dir(edgedata_dir)
    ensure_dir(routes_dir)
    ensure_dir(output_dir)

    return ScenarioPaths(
        root=root,
        edgedata_dir=edgedata_dir,
        routes_dir=routes_dir,
        output_dir=output_dir,
        types_file=root / "types.add.xml",
        sumocfg_file=root / "scenario.sumocfg",
    )


def launch_sumo(sumo_bin_dir: Path, sumocfg_file: Path, gui: bool) -> subprocess.Popen:
    exe_name = "sumo-gui.exe" if gui else "sumo.exe"
    if platform.system().lower() != "windows":
        exe_name = "sumo-gui" if gui else "sumo"
    exe = sumo_bin_dir / exe_name
    if not exe.exists():
        raise FileNotFoundError(f"SUMO executable not found: {exe}")
    return subprocess.Popen([str(exe), "-c", str(sumocfg_file)])


def on_percentage_change(changed_mode: str, mode_keys: List[str]) -> None:
    current = dict(st.session_state["modal_percentages"])
    new_value = int(st.session_state[f"slider_{changed_mode}"])
    updated = rebalance_percentages(changed_mode, new_value, current)
    st.session_state["modal_percentages"] = updated
    for key in mode_keys:
        st.session_state[f"slider_{key}"] = updated[key]


def summarize_tripinfos_global(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}

    return {
        "total_trips": int(df["id"].count()),
        "avg_waitingTime": float(df["waitingTime"].mean()),
        "avg_timeLoss": float(df["timeLoss"].mean()),
        "avg_duration": float(df["duration"].mean()),
        "avg_speed": float(df["avgSpeed"].mean()),
        "avg_CO2_abs": float(df["CO2_abs"].mean()),
        "avg_CO_abs": float(df["CO_abs"].mean()),
        "avg_fuel_abs": float(df["fuel_abs"].mean()),
    }

def parse_tripinfos_xml(tripinfos_path: Path) -> pd.DataFrame:
    tree = ET.parse(tripinfos_path)
    root = tree.getroot()

    rows = []

    for trip in root.findall("tripinfo"):
        row = {
            "id": trip.attrib.get("id"),
            "vType": trip.attrib.get("vType"),
            "depart": float(trip.attrib.get("depart", 0)),
            "arrival": float(trip.attrib.get("arrival", 0)),
            "duration": float(trip.attrib.get("duration", 0)),
            "routeLength": float(trip.attrib.get("routeLength", 0)),
            "waitingTime": float(trip.attrib.get("waitingTime", 0)),
            "waitingCount": float(trip.attrib.get("waitingCount", 0)),
            "stopTime": float(trip.attrib.get("stopTime", 0)),
            "timeLoss": float(trip.attrib.get("timeLoss", 0)),
            "departDelay": float(trip.attrib.get("departDelay", 0)),
            "speedFactor": float(trip.attrib.get("speedFactor", 0)),
        }

        emissions = trip.find("emissions")
        if emissions is not None:
            row.update({
                "CO_abs": float(emissions.attrib.get("CO_abs", 0)),
                "CO2_abs": float(emissions.attrib.get("CO2_abs", 0)),
                "HC_abs": float(emissions.attrib.get("HC_abs", 0)),
                "PMx_abs": float(emissions.attrib.get("PMx_abs", 0)),
                "NOx_abs": float(emissions.attrib.get("NOx_abs", 0)),
                "fuel_abs": float(emissions.attrib.get("fuel_abs", 0)),
                "electricity_abs": float(emissions.attrib.get("electricity_abs", 0)),
            })
        else:
            row.update({
                "CO_abs": 0.0,
                "CO2_abs": 0.0,
                "HC_abs": 0.0,
                "PMx_abs": 0.0,
                "NOx_abs": 0.0,
                "fuel_abs": 0.0,
                "electricity_abs": 0.0,
            })

        row["avgSpeed"] = row["routeLength"] / row["duration"] if row["duration"] > 0 else 0.0
        rows.append(row)

    return pd.DataFrame(rows)

def summarize_tripinfos_by_vtype(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    summary = (
        df.groupby("vType", dropna=False)
        .agg(
            trips=("id", "count"),
            avg_waitingTime=("waitingTime", "mean"),
            avg_timeLoss=("timeLoss", "mean"),
            avg_duration=("duration", "mean"),
            avg_routeLength=("routeLength", "mean"),
            avg_speed=("avgSpeed", "mean"),
            avg_CO_abs=("CO_abs", "mean"),
            avg_CO2_abs=("CO2_abs", "mean"),
            avg_HC_abs=("HC_abs", "mean"),
            avg_PMx_abs=("PMx_abs", "mean"),
            avg_NOx_abs=("NOx_abs", "mean"),
            avg_fuel_abs=("fuel_abs", "mean"),
            avg_electricity_abs=("electricity_abs", "mean"),
        )
        .reset_index()
    )

    return summary


def build_metric_bar_figure(summary_df: pd.DataFrame, metric: str, title: str, y_title: str):
    fig, ax = plt.subplots(figsize=(8, 4.8))
    chart_df = summary_df[["vType", metric]].copy()
    ax.bar(chart_df["vType"], chart_df[metric], color="#2a6f97")
    ax.set_title(title)
    ax.set_xlabel("Vehicle type")
    ax.set_ylabel(y_title)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def build_metric_histogram_figure(df: pd.DataFrame, metric: str, title: str, x_title: str):
    fig, ax = plt.subplots(figsize=(8, 4.8))
    series = pd.to_numeric(df[metric], errors="coerce").dropna()
    ax.hist(series, bins=20, color="#bc4749", edgecolor="white")
    ax.set_title(title)
    ax.set_xlabel(x_title)
    ax.set_ylabel("Trips")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def lane_to_edge_id(lane_id: str) -> str:
    if not lane_id:
        return ""
    return re.sub(r"_[0-9]+$", "", str(lane_id))


def parse_emission_by_edge(emission_output_path: Path) -> pd.DataFrame:
    tree = ET.parse(emission_output_path)
    rows = []

    for timestep in tree.getroot().findall("timestep"):
        for vehicle in timestep.findall("vehicle"):
            edge_id = lane_to_edge_id(vehicle.attrib.get("lane", ""))
            if not edge_id or edge_id.startswith(":"):
                continue
            rows.append({
                "edge_id": edge_id,
                "CO2": float(vehicle.attrib.get("CO2", 0)),
                "CO": float(vehicle.attrib.get("CO", 0)),
                "HC": float(vehicle.attrib.get("HC", 0)),
                "NOx": float(vehicle.attrib.get("NOx", 0)),
                "PMx": float(vehicle.attrib.get("PMx", 0)),
                "fuel": float(vehicle.attrib.get("fuel", 0)),
                "waiting": float(vehicle.attrib.get("waiting", 0)),
            })

    if not rows:
        return pd.DataFrame(columns=["edge_id", "CO2", "CO", "HC", "NOx", "PMx", "fuel", "waiting"])

    return pd.DataFrame(rows).groupby("edge_id", as_index=False).sum()


def build_network_metric_heatmap_figure(net_file: Path, edge_metric_df: pd.DataFrame, metric: str, title: str):
    try:
        import sumolib
    except ImportError as e:
        raise RuntimeError("sumolib is required to generate network heatmaps.") from e

    net = sumolib.net.readNet(str(net_file))
    all_segments = []
    for edge in net.getEdges():
        if edge.getID().startswith(":"):
            continue
        shape = edge.getShape()
        if len(shape) >= 2:
            all_segments.append(shape)

    metric_map = {
        str(row["edge_id"]): float(row[metric])
        for _, row in edge_metric_df.iterrows()
        if pd.notna(row.get(metric, None)) and float(row[metric]) > 0
    }

    colored_segments = []
    colored_values = []
    for edge_id, value in metric_map.items():
        try:
            edge = net.getEdge(edge_id)
        except Exception:
            continue
        if edge is None or edge.getID().startswith(":"):
            continue
        shape = edge.getShape()
        if len(shape) >= 2:
            colored_segments.append(shape)
            colored_values.append(value)

    fig, ax = plt.subplots(figsize=(10, 10))
    if all_segments:
        background = LineCollection(all_segments, colors="#d9d9d9", linewidths=0.4, alpha=0.8)
        ax.add_collection(background)

    if colored_segments:
        values_series = pd.Series(colored_values, dtype=float)
        positive_values = values_series[values_series > 0]

        if metric == "CO2":
            vmin = HEATMAP_CO2_VMIN
            vmax = HEATMAP_CO2_VMAX
        else:
            min_positive = float(positive_values.min()) if not positive_values.empty else 1.0
            vmax = float(positive_values.max()) if not positive_values.empty else 1.0
            vmin = max(min_positive, 1e-6)
            vmax = max(vmax, vmin * 1.01)

        clipped_values = values_series.clip(lower=vmin, upper=vmax).tolist()

        norm = LogNorm(vmin=vmin, vmax=vmax)
        overlay = LineCollection(colored_segments, cmap="inferno", norm=norm, linewidths=2.0)
        overlay.set_array(clipped_values)
        ax.add_collection(overlay)
        cbar = fig.colorbar(overlay, ax=ax, fraction=0.03, pad=0.01)
        if metric == "CO2":
            cbar.set_label(f"{metric} (fixed log scale: 1e4 to 1e6)")
        else:
            cbar.set_label(f"{metric} (log scale)")

    ax.autoscale()
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(title)
    ax.set_axis_off()
    fig.tight_layout()
    return fig


def build_network_segments(net_file: Path, edge_metric_df: pd.DataFrame, metric: str):
    try:
        import sumolib
    except ImportError as e:
        raise RuntimeError("sumolib is required to generate network heatmaps.") from e

    net = sumolib.net.readNet(str(net_file))
    all_segments = []
    for edge in net.getEdges():
        if edge.getID().startswith(":"):
            continue
        shape = edge.getShape()
        if len(shape) >= 2:
            all_segments.append(shape)

    colored_segments = []
    colored_values = []
    for _, row in edge_metric_df.iterrows():
        value = float(row.get(metric, 0))
        if value <= 0:
            continue
        try:
            edge = net.getEdge(str(row["edge_id"]))
        except Exception:
            continue
        if edge is None or edge.getID().startswith(":"):
            continue
        shape = edge.getShape()
        if len(shape) >= 2:
            colored_segments.append(shape)
            colored_values.append(value)

    return all_segments, colored_segments, colored_values


def get_metric_norm(metric: str, values: List[float]) -> LogNorm:
    if metric == "CO2":
        return LogNorm(vmin=HEATMAP_CO2_VMIN, vmax=HEATMAP_CO2_VMAX)

    positive_values = pd.Series(values, dtype=float)
    positive_values = positive_values[positive_values > 0]
    min_positive = float(positive_values.min()) if not positive_values.empty else 1.0
    vmax = float(positive_values.max()) if not positive_values.empty else 1.0
    vmin = max(min_positive, 1e-6)
    vmax = max(vmax, vmin * 1.01)
    return LogNorm(vmin=vmin, vmax=vmax)


def draw_network_heatmap(ax, all_segments, colored_segments, colored_values, norm: LogNorm, title: str):
    if all_segments:
        background = LineCollection(all_segments, colors="#8d8d8d", linewidths=0.8, alpha=0.8)
        ax.add_collection(background)

    overlay = None
    if colored_segments:
        clipped_values = pd.Series(colored_values, dtype=float).clip(lower=norm.vmin, upper=norm.vmax).tolist()
        overlay = LineCollection(colored_segments, cmap="plasma", norm=norm, linewidths=2.0)
        overlay.set_array(clipped_values)
        ax.add_collection(overlay)

    ax.autoscale()

    # 🔍 ZOOM IN
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()

    zoom_factor = 0.5  # prova 0.8 o 0.7 se vuoi più zoom

    x_center = (xlim[0] + xlim[1]) / 2
    y_center = (ylim[0] + ylim[1]) / 2

    x_range = (xlim[1] - xlim[0]) * zoom_factor
    y_range = (ylim[1] - ylim[0]) * zoom_factor

    ax.set_xlim(x_center - x_range / 2, x_center + x_range / 2)
    ax.set_ylim(y_center - y_range / 2, y_center + y_range / 2)

    ax.set_aspect("equal", adjustable="box")
    ax.set_title(title)
    ax.set_axis_off()
    return overlay


def build_comparison_heatmaps_figure(net_file: Path, left_df: pd.DataFrame, right_df: pd.DataFrame, metric: str, left_title: str, right_title: str):
    left_all_segments, left_colored_segments, left_values = build_network_segments(net_file, left_df, metric)
    right_all_segments, right_colored_segments, right_values = build_network_segments(net_file, right_df, metric)
    norm = get_metric_norm(metric, left_values + right_values)

    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    overlay_left = draw_network_heatmap(axes[0], left_all_segments, left_colored_segments, left_values, norm, left_title)
    overlay_right = draw_network_heatmap(axes[1], right_all_segments, right_colored_segments, right_values, norm, right_title)

    overlay_for_cbar = overlay_left or overlay_right
    if overlay_for_cbar is not None:
        # cbar = fig.colorbar(overlay_for_cbar, ax=axes, fraction=0.03, pad=0.02)
        cbar_ax = fig.add_axes([0.25, 0.08, 0.5, 0.03])  # [left, bottom, width, height]
        cbar = fig.colorbar(overlay_for_cbar, cax=cbar_ax, orientation="horizontal")
        if metric == "CO2":
            # cbar.set_label(f"{metric} (fixed log scale: 1e4 to 1e6)")
            cbar.set_label(f"{metric} (mg)")
        else:
            cbar.set_label(f"{metric} (log scale)")

    fig.suptitle(f"{metric} heatmap comparison", y=0.98)
    fig.tight_layout()
    return fig


def build_kpi_comparison_figure(left_name: str, right_name: str, left_kpis: dict, right_kpis: dict):
    metrics = [
        ("avg_waitingTime", "Avg waiting time", "s"),
        # ("avg_timeLoss", "Avg time loss", "s"),
        ("avg_speed", "Avg speed", "m/s"),
        ("avg_CO2_abs", "Avg CO2", "mg"),
        ("avg_CO_abs", "Avg CO", "mg"),
        # ("avg_fuel_abs", "Avg fuel"),
    ]
    n_cols = 2
    n_rows = math.ceil(len(metrics) / n_cols)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(10, 6))
    axes = axes.flatten()
    for ax, (metric, label, unit) in zip(axes, metrics):
        ax.bar(["01-26:08 am", "01-26:11 pm"], [left_kpis.get(metric, 0), right_kpis.get(metric, 0)], color=["#2a6f97", "#bc4749"])
        ax.set_title(label)
        ax.set_ylabel(unit)
        ax.tick_params(axis="x", rotation=20)
        ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    return fig


def list_available_scenarios(output_root: Path) -> List[str]:
    if not output_root.exists():
        return []

    scenarios = []
    for path in sorted(output_root.iterdir()):
        if not path.is_dir():
            continue
        if (path / "output" / "tripinfos.xml").exists():
            scenarios.append(path.name)
    return scenarios


def load_scenario_results(scenario_root: Path):
    output_path = scenario_root / "output"
    tripinfos_path = output_path / "tripinfos.xml"
    emission_output_path = output_path / "emission-output.xml"

    if not tripinfos_path.exists():
        raise FileNotFoundError(f"tripinfos.xml not found: {tripinfos_path}")

    trip_df = parse_tripinfos_xml(tripinfos_path)
    if trip_df.empty:
        raise RuntimeError(f"No tripinfo entries found in: {tripinfos_path}")

    summary_df = summarize_tripinfos_by_vtype(trip_df)
    global_kpis = summarize_tripinfos_global(trip_df)
    edge_emissions_df = parse_emission_by_edge(emission_output_path) if emission_output_path.exists() else pd.DataFrame()
    return trip_df, summary_df, global_kpis, edge_emissions_df


def save_figure(fig, out_path: Path) -> Path:
    fig.savefig(out_path, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out_path


def save_tripinfo_reports(
    output_dir: Path,
    trip_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    global_kpis: dict,
    net_file: Path,
    emission_output_path: Path,
) -> List[Path]:
    ensure_dir(output_dir)
    reports_dir = output_dir / "reports"
    ensure_dir(reports_dir)

    saved_files = []

    summary_csv = reports_dir / "tripinfo_summary_by_vtype.csv"
    summary_df.to_csv(summary_csv, index=False)
    saved_files.append(summary_csv)

    global_csv = reports_dir / "tripinfo_global_kpis.csv"
    pd.DataFrame([global_kpis]).to_csv(global_csv, index=False)
    saved_files.append(global_csv)

    bar_specs = [
        ("avg_waitingTime", "Average waiting time by vehicle type", "Waiting time (s)", "waiting_time_by_vtype.png"),
        ("avg_timeLoss", "Average time loss by vehicle type", "Time loss (s)", "time_loss_by_vtype.png"),
        ("avg_duration", "Average trip duration by vehicle type", "Duration (s)", "duration_by_vtype.png"),
        ("avg_speed", "Average speed by vehicle type", "Speed (m/s)", "speed_by_vtype.png"),
        ("avg_CO2_abs", "Average CO2 emissions by vehicle type", "CO2 (mg)", "co2_by_vtype.png"),
        ("avg_CO_abs", "Average CO emissions by vehicle type", "CO (mg)", "co_by_vtype.png"),
        ("avg_fuel_abs", "Average fuel consumption by vehicle type", "Fuel (mg/s)", "fuel_by_vtype.png"),
    ]
    for metric, title, y_title, filename in bar_specs:
        fig = build_metric_bar_figure(summary_df, metric, title, y_title)
        out_path = reports_dir / filename
        saved_files.append(save_figure(fig, out_path))

    hist_specs = [
        ("waitingTime", "Waiting time distribution", "Waiting time (s)", "waiting_time_histogram.png"),
        ("timeLoss", "Time loss distribution", "Time loss (s)", "time_loss_histogram.png"),
        ("duration", "Trip duration distribution", "Duration (s)", "duration_histogram.png"),
        ("avgSpeed", "Average speed distribution", "Speed (m/s)", "speed_histogram.png"),
        ("CO2_abs", "CO2 distribution", "CO2 (mg)", "co2_histogram.png"),
        ("CO_abs", "CO distribution", "CO (mg)", "co_histogram.png"),
        ("fuel_abs", "Fuel consumption distribution", "Fuel (mg/s)", "fuel_histogram.png"),
    ]
    for metric, title, x_title, filename in hist_specs:
        fig = build_metric_histogram_figure(trip_df, metric, title, x_title)
        out_path = reports_dir / filename
        saved_files.append(save_figure(fig, out_path))

    if emission_output_path.exists():
        edge_emissions_df = parse_emission_by_edge(emission_output_path)
        edge_emissions_csv = reports_dir / "edge_emissions_summary.csv"
        edge_emissions_df.sort_values("CO2", ascending=False).to_csv(edge_emissions_csv, index=False)
        saved_files.append(edge_emissions_csv)

        if not edge_emissions_df.empty:
            heatmap_path = reports_dir / "network_co2_heatmap.png"
            fig = build_network_metric_heatmap_figure(net_file, edge_emissions_df, "CO2", "Network CO2 heatmap")
            saved_files.append(save_figure(fig, heatmap_path))

    return saved_files

# ============================================================================
# STREAMLIT UI
# ============================================================================
st.set_page_config(page_title="SUMO Scenario Builder", layout="wide")
st.title("Scenario Reconstruction")
st.caption("Create one-hour multimodal SUMO scenarios from mapped TLC measurements.")

with st.sidebar:
    st.header("Paths")
    sumo_tools_path = st.text_input("SUMO tools path", DEFAULTS["sumo_tools_path"])
    sumo_bin_dir = st.text_input("SUMO bin path", DEFAULTS["sumo_bin_dir"])
    net_file = st.text_input("SUMO net file", DEFAULTS["net_file"])
    base_sumocfg = st.text_input("Base SUMO config", DEFAULTS["base_sumocfg"])
    poi_edge_mapping = st.text_input("POI-edge mapping CSV", DEFAULTS["poi_edge_mapping"])
    csv_dir = st.text_input("Mapped TLC CSV folder", DEFAULTS["csv_dir"])
    output_dir = st.text_input("Scenario output folder", DEFAULTS["output_dir"])

    st.header("Generation")
    random_period = st.text_input("randomTrips period", "0.1")
    fringe_factor = st.text_input("fringe factor", "10")
    min_distance = st.text_input("min distance", "100")
    max_distance = st.text_input("max distance", "2000")
    routing_factor = st.text_input("random routing factor", "10")
    threads = st.text_input("routeSampler threads", "8")
    launch_gui = st.checkbox("Launch SUMO-GUI after generation", value=True)

try:
    dates = valid_dates_for_mapped_csv(poi_edge_mapping, csv_dir)
    slots_df, invalid_df = discover_available_slots(poi_edge_mapping, csv_dir)
except Exception as e:
    st.error(f"Unable to inspect mapped CSV files: {e}")
    st.stop()

col_a, col_b, col_c = st.columns([1, 1, 1])
with col_a:
    selected_date = st.selectbox("Date", options=dates, index=0 if dates else None)
with col_b:
    hours = valid_hours_for_date(poi_edge_mapping, csv_dir, selected_date) if selected_date else []
    hour_labels = [f"{h:02d}:00 - {h:02d}:59" for h in hours]
    selected_label = st.selectbox("Time slot", options=hour_labels, index=0 if hour_labels else None)
    selected_hour = hours[hour_labels.index(selected_label)] if hour_labels else None
with col_c:
    mapping_df = load_mapping(poi_edge_mapping)
    st.metric("Mapped CSV with edge", int(mapping_df["csv_file"].nunique()))

with st.expander("Availability summary", expanded=False):
    if not slots_df.empty:
        daily = slots_df.groupby("date")["csv_file"].nunique().reset_index(name="mapped_csv_available")
        st.dataframe(daily, use_container_width=True)
    if not invalid_df.empty:
        st.markdown("**Ignored mapped CSV files**")
        st.dataframe(invalid_df, use_container_width=True)

if not selected_date or selected_hour is None:
    st.warning("No valid date/hour combination available from the mapped CSV files.")
    st.stop()

if st.button("Load measured edge counts", type="primary"):
    mode_edge_counts, used_df = build_edge_counts_for_slot(poi_edge_mapping, csv_dir, selected_date, selected_hour)
    st.session_state["mode_edge_counts"] = mode_edge_counts
    st.session_state["used_df"] = used_df
    st.session_state["selected_date"] = selected_date
    st.session_state["selected_hour"] = selected_hour
    st.session_state["modal_percentages"] = None
    for _, cfg in MODE_CONFIG.items():
        slider_key = f"slider_{cfg['key']}"
        if slider_key in st.session_state:
            del st.session_state[slider_key]

mode_edge_counts = st.session_state.get("mode_edge_counts", None)
used_df = st.session_state.get("used_df", None)
loaded_date = st.session_state.get("selected_date", None)
loaded_hour = st.session_state.get("selected_hour", None)

if not mode_edge_counts or not isinstance(mode_edge_counts, dict):
    st.info("Select a date and a one-hour slot, then click 'Load measured edge counts'.")
    st.stop()

measured_totals = {}
for _, cfg in MODE_CONFIG.items():
    mode_key = cfg["key"]
    mode_counts = mode_edge_counts.get(mode_key, {}) if isinstance(mode_edge_counts, dict) else {}
    measured_totals[mode_key] = int(sum(mode_counts.values()))
measured_total_all = sum(measured_totals.values())

st.subheader("Measured counts for selected slot")
mc1, mc2, mc3, mc4, mc5 = st.columns(5)
for col, (_, cfg) in zip([mc1, mc2, mc3, mc4, mc5], MODE_CONFIG.items()):
    col.metric(cfg["key"], measured_totals[cfg["key"]])

st.caption(f"Loaded slot: {loaded_date} | {loaded_hour:02d}:00 - {loaded_hour:02d}:59")

with st.expander("CSV/edge rows used for this slot", expanded=False):
    st.dataframe(used_df, use_container_width=True)

st.subheader("Scenario scaling")
default_total = measured_total_all if measured_total_all > 0 else 1000
scenario_total = st.number_input("Total vehicles in scenario", min_value=0, value=int(default_total), step=50)

mode_keys = [cfg["key"] for _, cfg in MODE_CONFIG.items()]
if st.session_state["modal_percentages"] is None:
    if measured_total_all > 0:
        init_percentages = {
            cfg["key"]: measured_totals[cfg["key"]] * 100.0 / measured_total_all
            for _, cfg in MODE_CONFIG.items()
        }
    else:
        init_percentages = {
            cfg["key"]: cfg["default_ratio"] * 100.0
            for _, cfg in MODE_CONFIG.items()
        }

    st.session_state["modal_percentages"] = normalize_percentages_to_100(init_percentages)
    for key, value in st.session_state["modal_percentages"].items():
        st.session_state[f"slider_{key}"] = value

st.markdown("Adjust modal proportions. Percentages are shared and always sum to 100%.")
st.caption("Changing one mode automatically redistributes the remaining share across the other modes.")

slider_cols = st.columns(len(mode_keys))
for slider_col, (_, cfg) in zip(slider_cols, MODE_CONFIG.items()):
    mode_key = cfg["key"]
    with slider_col:
        st.slider(
            mode_key,
            min_value=0,
            max_value=100,
            step=1,
            key=f"slider_{mode_key}",
            on_change=on_percentage_change,
            args=(mode_key, mode_keys),
        )

current_percentages = dict(st.session_state["modal_percentages"])
scaled_totals = percentages_to_counts(current_percentages, int(scenario_total))

preview_df = pd.DataFrame([
    {
        "mode": cfg["key"],
        "percentage": current_percentages[cfg["key"]],
        "measured_total": measured_totals[cfg["key"]],
        "scenario_total": scaled_totals[cfg["key"]],
    }
    for _, cfg in MODE_CONFIG.items()
])
st.dataframe(preview_df, use_container_width=True)

tab_run, tab_compare = st.tabs(["Run & Analyze", "Compare Simulations"])

with tab_run:
    st.subheader("Generate scenario")
    if st.button("Build edgeData, routes, and SUMO config", type="primary"):
        try:
            scenario_paths = build_scenario_paths(Path(output_dir), loaded_date, int(loaded_hour))

            edgedata_files = write_scaled_edgedata_files(mode_edge_counts, scaled_totals, scenario_paths.edgedata_dir)
            write_types_add_xml(scenario_paths.types_file)

            route_files = []
            logs = {}
            for csv_mode, cfg in MODE_CONFIG.items():
                mode_key = cfg["key"]
                edgedata_file = edgedata_files[mode_key]
                if parse_total_from_edgedata(edgedata_file) <= 0:
                    continue

                route_file, log_text = generate_routes_for_mode(
                    sumo_tools_path=Path(sumo_tools_path),
                    net_file=Path(net_file),
                    edgedata_file=edgedata_file,
                    out_dir=scenario_paths.routes_dir,
                    mode_label=csv_mode,
                    vtype_id=cfg["vtype_id"],
                    vclass=cfg["vclass"],
                    period=random_period,
                    fringe_factor=fringe_factor,
                    min_distance=min_distance,
                    max_distance=max_distance,
                    routing_factor=routing_factor,
                    threads=threads,
                )
                route_files.append(route_file)
                logs[mode_key] = log_text

            if not route_files:
                raise RuntimeError("No route files were generated. Check the measured counts and modal split.")

            copy_and_patch_base_sumocfg(
                Path(base_sumocfg),
                scenario_paths.sumocfg_file,
                Path(net_file),
                scenario_paths.types_file,
                route_files,
            )

            st.success(f"Scenario created in: {scenario_paths.root}")
            st.code(str(scenario_paths.root))

            st.markdown("**Generated files**")
            generated = [str(scenario_paths.types_file), str(scenario_paths.sumocfg_file)]
            generated.extend(str(p) for p in edgedata_files.values())
            generated.extend(str(p) for p in route_files)
            st.code("\n".join(generated))

            with st.expander("Generation logs", expanded=False):
                for mode_key, text in logs.items():
                    st.markdown(f"**{mode_key}**")
                    st.text(text[:20000])

            if launch_gui:
                proc = launch_sumo(Path(sumo_bin_dir), scenario_paths.sumocfg_file, gui=True)
                st.info(f"SUMO-GUI launched (PID: {proc.pid})")

        except Exception as e:
            st.error(str(e))

    st.subheader("Simulation outputs")

    if st.button("Load simulation outputs"):
        try:
            scenario_paths = build_scenario_paths(Path(output_dir), loaded_date, int(loaded_hour))
            tripinfos_path = scenario_paths.output_dir / "tripinfos.xml"

            if not tripinfos_path.exists():
                st.warning(f"tripinfos.xml not found: {tripinfos_path}")
            else:
                trip_df = parse_tripinfos_xml(tripinfos_path)

                if trip_df.empty:
                    st.warning("tripinfos.xml was found, but it does not contain any tripinfo entries.")
                else:
                    summary_df = summarize_tripinfos_by_vtype(trip_df)
                    global_kpis = summarize_tripinfos_global(trip_df)
                    emission_output_path = scenario_paths.output_dir / "emission-output.xml"
                    saved_report_files = save_tripinfo_reports(
                        scenario_paths.output_dir,
                        trip_df,
                        summary_df,
                        global_kpis,
                        Path(net_file),
                        emission_output_path,
                    )

                    st.success(f"Loaded simulation outputs from: {tripinfos_path}")

                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Trips", global_kpis["total_trips"])
                    c2.metric("Avg waiting time", f"{global_kpis['avg_waitingTime']:.2f} s")
                    c3.metric("Avg time loss", f"{global_kpis['avg_timeLoss']:.2f} s")
                    c4.metric("Avg speed", f"{global_kpis['avg_speed']:.2f} m/s")

                    c5, c6 = st.columns(2)
                    c5.metric("Avg CO2", f"{global_kpis['avg_CO2_abs']:.2f}")
                    c6.metric("Avg fuel", f"{global_kpis['avg_fuel_abs']:.2f}")

                    st.markdown("**Tripinfo summary by vehicle type**")
                    st.dataframe(summary_df, use_container_width=True)

                    with st.expander("Saved report files", expanded=False):
                        st.code("\n".join(str(path) for path in saved_report_files))

                    st.markdown("**Performance metrics**")
                    perf_cols = st.columns(2)
                    with perf_cols[0]:
                        st.markdown("**Average waiting time by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_waitingTime", "Average waiting time by vehicle type", "Waiting time (s)"))
                    with perf_cols[1]:
                        st.markdown("**Average time loss by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_timeLoss", "Average time loss by vehicle type", "Time loss (s)"))

                    st.markdown("**Mobility metrics**")
                    mob_cols = st.columns(2)
                    with mob_cols[0]:
                        st.markdown("**Average trip duration by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_duration", "Average trip duration by vehicle type", "Duration (s)"))
                    with mob_cols[1]:
                        st.markdown("**Average speed by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_speed", "Average speed by vehicle type", "Speed (m/s)"))

                    st.markdown("**Environmental metrics**")
                    env_cols = st.columns(2)
                    with env_cols[0]:
                        st.markdown("**Average CO2 emissions by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_CO2_abs", "Average CO2 emissions by vehicle type", "CO2 (mg/s)"))
                    with env_cols[1]:
                        st.markdown("**Average fuel consumption by vehicle type**")
                        st.pyplot(build_metric_bar_figure(summary_df, "avg_fuel_abs", "Average fuel consumption by vehicle type", "Fuel (mg/s)"))

                    st.markdown("**Distributions**")
                    dist_cols = st.columns(2)
                    with dist_cols[0]:
                        st.markdown("**Waiting time distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "waitingTime", "Waiting time distribution", "Waiting time (s)"))
                        st.markdown("**Trip duration distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "duration", "Trip duration distribution", "Duration (s)"))
                        st.markdown("**CO2 distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "CO2_abs", "CO2 distribution", "CO2 (mg/s)"))
                    with dist_cols[1]:
                        st.markdown("**Time loss distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "timeLoss", "Time loss distribution", "Time loss (s)"))
                        st.markdown("**Average speed distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "avgSpeed", "Average speed distribution", "Speed (m/s)"))
                        st.markdown("**Fuel consumption distribution**")
                        st.pyplot(build_metric_histogram_figure(trip_df, "fuel_abs", "Fuel consumption distribution", "Fuel (mg/s)"))

                    if emission_output_path.exists():
                        st.markdown("**Network CO2 heatmap**")
                        edge_emissions_df = parse_emission_by_edge(emission_output_path)
                        if edge_emissions_df.empty:
                            st.info("Emission output was found, but no non-internal edge emissions were available.")
                        else:
                            st.pyplot(build_network_metric_heatmap_figure(Path(net_file), edge_emissions_df, "CO2", "Network CO2 heatmap"))

        except Exception as e:
            st.error(str(e))

with tab_compare:
    st.subheader("Scenario comparison")
    available_scenarios = list_available_scenarios(Path(output_dir))
    if len(available_scenarios) < 2:
        st.info("At least two completed scenarios with `output/tripinfos.xml` are required to compare simulations.")
    else:
        compare_cols = st.columns(2)
        with compare_cols[0]:
            left_scenario = st.selectbox("Scenario A", options=available_scenarios, index=0, key="compare_left_scenario")
        with compare_cols[1]:
            default_right_index = 1 if len(available_scenarios) > 1 else 0
            right_scenario = st.selectbox("Scenario B", options=available_scenarios, index=default_right_index, key="compare_right_scenario")

        if left_scenario == right_scenario:
            st.warning("Select two different scenarios to compare.")
        else:
            try:
                left_root = Path(output_dir) / left_scenario
                right_root = Path(output_dir) / right_scenario
                left_trip_df, left_summary_df, left_kpis, left_edge_emissions_df = load_scenario_results(left_root)
                right_trip_df, right_summary_df, right_kpis, right_edge_emissions_df = load_scenario_results(right_root)

                kpi_cols = st.columns(4)
                kpi_cols[0].metric("Trips delta", f"{left_kpis['total_trips'] - right_kpis['total_trips']:+d}")
                kpi_cols[1].metric("Waiting delta", f"{left_kpis['avg_waitingTime'] - right_kpis['avg_waitingTime']:+.2f} s")
                kpi_cols[2].metric("CO2 delta", f"{left_kpis['avg_CO2_abs'] - right_kpis['avg_CO2_abs']:+.2f}")
                kpi_cols[3].metric("CO delta", f"{left_kpis['avg_CO_abs'] - right_kpis['avg_CO_abs']:+.2f}")
                # kpi_cols[3].metric("Fuel delta", f"{left_kpis['avg_fuel_abs'] - right_kpis['avg_fuel_abs']:+.2f}")

                st.markdown("**Global KPI comparison**")
                st.pyplot(build_kpi_comparison_figure(left_scenario, right_scenario, left_kpis, right_kpis))

                comparison_rows = []
                for metric, label in [
                    ("total_trips", "Trips"),
                    ("avg_waitingTime", "Avg waiting time"),
                    ("avg_timeLoss", "Avg time loss"),
                    ("avg_duration", "Avg duration"),
                    ("avg_speed", "Avg speed"),
                    ("avg_CO2_abs", "Avg CO2"),
                    ("avg_CO_abs", "Avg CO"),
                    ("avg_fuel_abs", "Avg fuel"),
                ]:
                    comparison_rows.append({
                        "metric": label,
                        left_scenario: left_kpis.get(metric, 0),
                        right_scenario: right_kpis.get(metric, 0),
                        "delta_A_minus_B": left_kpis.get(metric, 0) - right_kpis.get(metric, 0),
                    })
                st.dataframe(pd.DataFrame(comparison_rows), use_container_width=True)

                st.markdown("**CO2 heatmap comparison**")
                if left_edge_emissions_df.empty or right_edge_emissions_df.empty:
                    st.info("Both scenarios need `emission-output.xml` with usable edge emissions for the heatmap comparison.")
                else:
                    st.pyplot(
                        build_comparison_heatmaps_figure(
                            Path(net_file),
                            left_edge_emissions_df,
                            right_edge_emissions_df,
                            "CO2",
                            left_scenario,
                            right_scenario,
                        )
                    )

                st.markdown("**Vehicle-type comparison**")
                merged_summary = left_summary_df.merge(
                    right_summary_df,
                    on="vType",
                    how="outer",
                    suffixes=(f"_{left_scenario}", f"_{right_scenario}"),
                ).fillna(0)
                st.dataframe(merged_summary, use_container_width=True)

                hist_cols = st.columns(2)
                with hist_cols[0]:
                    st.markdown(f"**{left_scenario} CO2 distribution**")
                    st.pyplot(build_metric_histogram_figure(left_trip_df, "CO2_abs", f"{left_scenario} CO2 distribution", "CO2 (mg/s)"))
                with hist_cols[1]:
                    st.markdown(f"**{right_scenario} CO2 distribution**")
                    st.pyplot(build_metric_histogram_figure(right_trip_df, "CO2_abs", f"{right_scenario} CO2 distribution", "CO2 (mg/s)"))

            except Exception as e:
                st.error(str(e))
