#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SUMO Scenario Dashboard

Interactive Streamlit dashboard to build a SUMO simulation scenario starting
from mapped TLC CSV measurements.

Main workflow:
1. Read the CSV->POI->edge mapping (poi_edge_mapping.csv).
2. Scan only the mapped CSV files and extract the available dates / hourly slots.
3. Let the user pick a valid date and a one-hour time slot.
4. Build one edgeData XML per traffic mode for the selected hour.
5. Estimate the default modal split from the measured counts.
6. Let the user choose a total number of vehicles and rebalance the modal split
   with sliders while keeping the total fixed.
7. Generate:
   - types.add.xml
   - one route file per mode via randomTrips.py + routeSampler.py
   - a SUMO config file
8. Optionally launch SUMO or SUMO-GUI.

The app is self-contained and does not require the earlier helper scripts,
although it follows the same logic and file formats.

Run with:
    streamlit run sumo_scenario_dashboard.py
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st


if "mode_edge_counts" not in st.session_state:
    st.session_state["mode_edge_counts"] = None

if "used_df" not in st.session_state:
    st.session_state["used_df"] = None

if "selected_date" not in st.session_state:
    st.session_state["selected_date"] = None

if "selected_hour" not in st.session_state:
    st.session_state["selected_hour"] = None

# ============================================================================
# CONFIGURATION
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "sumo_tools_path": r"C:/Program Files (x86)/Eclipse/Sumo/tools",
    "sumo_bin_dir": r"C:/Program Files (x86)/Eclipse/Sumo/bin",
    "net_file": str(BASE_DIR / "sumo_data" / "osm.net.xml"),
    "poi_edge_mapping": str(BASE_DIR / "pre-processing" / "poi_edge_mapping.csv"),
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
# DATA STRUCTURES
# ============================================================================
@dataclass
class ScenarioPaths:
    root: Path
    edgedata_dir: Path
    routes_dir: Path
    types_file: Path
    sumocfg_file: Path


# ============================================================================
# UTILS
# ============================================================================
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd: List[str], desc: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def normalize_weights_to_total(weights: Dict[str, int], total: int) -> Dict[str, int]:
    if total <= 0:
        return {k: 0 for k in weights}

    raw_sum = sum(max(0, int(v)) for v in weights.values())
    if raw_sum == 0:
        keys = list(weights.keys())
        out = {k: 0 for k in keys}
        if keys:
            out[keys[0]] = total
        return out

    scaled = {k: (max(0, int(v)) / raw_sum) * total for k, v in weights.items()}
    floored = {k: int(math.floor(v)) for k, v in scaled.items()}
    remainder = total - sum(floored.values())

    # distribute remainder to largest fractional parts
    fractions = sorted(
        ((k, scaled[k] - floored[k]) for k in scaled),
        key=lambda x: x[1],
        reverse=True,
    )
    for i in range(remainder):
        floored[fractions[i % len(fractions)][0]] += 1
    return floored


def parse_timestamp_column(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["Data e Ora"], format="%d/%m/%Y - %H:%M:%S", errors="coerce")


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

    slots_df = pd.DataFrame(rows)
    invalid_df = pd.DataFrame(invalid)
    return slots_df, invalid_df


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

    used_df = pd.DataFrame(used_rows)
    return mode_edge_counts, used_df


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
        "--total-count", str(total_count),
        #"--optimize", "full",
        "--minimize-vehicles", "1",
        "--threads", threads,
        "--verbose",
    ]
    res2 = run_cmd(cmd2, f"routeSampler {mode_label}")
    if res2.returncode != 0:
        raise RuntimeError(f"routeSampler failed for {mode_label}\nSTDOUT:\n{res2.stdout}\nSTDERR:\n{res2.stderr}")

    return sampled_routes, res1.stdout + "\n" + res1.stderr + "\n" + res2.stdout + "\n" + res2.stderr


def write_sumocfg(sumocfg_path: Path, net_file: Path, types_file: Path, route_files: List[Path], gui: bool) -> None:
    root = ET.Element("configuration")
    input_el = ET.SubElement(root, "input")
    ET.SubElement(input_el, "net-file", {"value": str(net_file)})
    ET.SubElement(input_el, "additional-files", {"value": str(types_file)})
    ET.SubElement(input_el, "route-files", {"value": ",".join(str(p) for p in route_files)})

    time_el = ET.SubElement(root, "time")
    ET.SubElement(time_el, "begin", {"value": "0"})
    ET.SubElement(time_el, "end", {"value": "3600"})

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ", level=0)
    tree.write(sumocfg_path, encoding="utf-8", xml_declaration=True)


def build_scenario_paths(output_root: Path, selected_date: str, selected_hour: int) -> ScenarioPaths:
    scenario_name = f"scenario_{selected_date}_{selected_hour:02d}00"
    root = output_root / scenario_name
    edgedata_dir = root / "edgedata"
    routes_dir = root / "routes"
    ensure_dir(root)
    ensure_dir(edgedata_dir)
    ensure_dir(routes_dir)
    return ScenarioPaths(
        root=root,
        edgedata_dir=edgedata_dir,
        routes_dir=routes_dir,
        types_file=root / "types.add.xml",
        sumocfg_file=root / "scenario.sumocfg",
    )


def launch_sumo(sumo_bin_dir: Path, sumocfg_file: Path, gui: bool) -> subprocess.Popen:
    exe = sumo_bin_dir / ("sumo-gui.exe" if gui else "sumo.exe")
    if not exe.exists():
        raise FileNotFoundError(f"SUMO executable not found: {exe}")
    return subprocess.Popen([str(exe), "-c", str(sumocfg_file)])


# ============================================================================
# STREAMLIT UI
# ============================================================================
st.set_page_config(page_title="SUMO Scenario Builder", layout="wide")
st.title("SUMO Scenario Builder")
st.caption("Create one-hour multimodal SUMO scenarios from mapped TLC measurements.")

with st.sidebar:
    st.header("Paths")
    sumo_tools_path = st.text_input("SUMO tools path", DEFAULTS["sumo_tools_path"])
    sumo_bin_dir = st.text_input("SUMO bin path", DEFAULTS["sumo_bin_dir"])
    net_file = st.text_input("SUMO net file", DEFAULTS["net_file"])
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
        st.dataframe(daily, width='stretch')
    if not invalid_df.empty:
        st.markdown("**Ignored mapped CSV files**")
        st.dataframe(invalid_df, width='stretch')

if not selected_date or selected_hour is None:
    st.warning("No valid date/hour combination available from the mapped CSV files.")
    st.stop()

if st.button("Load measured edge counts", type="primary"):
    mode_edge_counts, used_df = build_edge_counts_for_slot(poi_edge_mapping, csv_dir, selected_date, selected_hour)
    st.session_state["mode_edge_counts"] = mode_edge_counts
    st.session_state["used_df"] = used_df
    st.session_state["selected_date"] = selected_date
    st.session_state["selected_hour"] = selected_hour

if "mode_edge_counts" not in st.session_state:
    st.info("Select a date and a one-hour slot, then click 'Load measured edge counts'.")
    st.stop()

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

with st.expander("CSV/edge rows used for this slot", expanded=False):
    st.dataframe(used_df, use_container_width=True)

st.subheader("Scenario scaling")
default_total = measured_total_all if measured_total_all > 0 else 1000
scenario_total = st.number_input("Total vehicles in scenario", min_value=0, value=int(default_total), step=50)

# default weights from measurements
if measured_total_all > 0:
    default_weights = {cfg["key"]: max(1, measured_totals[cfg["key"]]) for _, cfg in MODE_CONFIG.items()}
else:
    default_weights = {cfg["key"]: int(cfg["default_ratio"] * 100) for _, cfg in MODE_CONFIG.items()}

st.markdown("Adjust modal proportions. The final per-mode counts will always sum to the selected total.")
slider_cols = st.columns(5)
weights = {}
for slider_col, (_, cfg) in zip(slider_cols, MODE_CONFIG.items()):
    mode_key = cfg["key"]
    weights[mode_key] = slider_col.slider(mode_key, min_value=0, max_value=100, value=min(100, int(default_weights[mode_key])), step=1)

scaled_totals = normalize_weights_to_total(weights, int(scenario_total))
preview_df = pd.DataFrame([
    {
        "mode": cfg["key"],
        "measured_total": measured_totals[cfg["key"]],
        "scenario_total": scaled_totals[cfg["key"]],
    }
    for _, cfg in MODE_CONFIG.items()
])
st.dataframe(preview_df, use_container_width=True)

st.subheader("Generate scenario")
if st.button("Build edgeData, routes, and SUMO config", type="primary"):
    try:
        scenario_paths = build_scenario_paths(Path(output_dir), selected_date, int(selected_hour))

        # 1) write scaled edgeData
        edgedata_files = write_scaled_edgedata_files(mode_edge_counts, scaled_totals, scenario_paths.edgedata_dir)

        # 2) write types.add.xml
        write_types_add_xml(scenario_paths.types_file)

        # 3) generate route files
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

        # 4) sumocfg
        write_sumocfg(scenario_paths.sumocfg_file, Path(net_file), scenario_paths.types_file, route_files, launch_gui)

        st.success(f"Scenario created in: {scenario_paths.root}")
        st.code(str(scenario_paths.root))

        st.markdown("**Generated files**")
        generated = []
        generated.append(str(scenario_paths.types_file))
        generated.append(str(scenario_paths.sumocfg_file))
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
