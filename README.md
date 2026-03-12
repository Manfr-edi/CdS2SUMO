# CdS2SUMO

<!-- Introduction intentionally left empty.
This repository represents only a preprocessing component of a larger system. -->



---

# Workflow

The overall system relies on a multi-stage workflow that integrates real-world
traffic data into a SUMO simulation environment.

One of the key steps of this pipeline is **data preprocessing**, which prepares
traffic observation datasets and maps them onto the simulation network.

## Preprocessing

The preprocessing stage converts heterogeneous traffic monitoring data into
elements that can be directly integrated into the SUMO network.

The process follows the pipeline below:
![preprocess_workflow.png](figures/preprocess_workflow.png)

### CSV–GIS Matching

Traffic measurements are provided as CSV files, each corresponding to a
monitoring location (e.g., traffic camera or counting station).

The filenames typically contain hints about the monitoring site, such as the
site identifier (`sitoXX`) or the street name.

These CSV files are automatically associated with monitoring points stored in
GIS shapefiles. The matching relies on attributes such as:

- `ID SITO` – monitoring site identifier
- `UBICAZIONE` – location description (street or area name)

A scoring mechanism combines site identifier matching and text similarity to
identify the most likely correspondence.

The result is a table (`csv_shapefile_matches.csv`) containing the best GIS
match for each CSV dataset.

### Match Filtering

Only reliable matches are retained before proceeding to the next step.

The filtering stage typically:

- keeps matches with **medium or high confidence**
- discards **ambiguous associations**
- ensures that each GIS monitoring point is linked to **at most one CSV file**

### SUMO POI Generation

After validation, the matched GIS features are converted into **SUMO Points of
Interest (POIs)**.

This step retrieves the coordinates of the monitoring locations from the
shapefiles and converts them into SUMO network coordinates using the SUMO
network file.

The resulting POIs are stored in an `additional.xml` file that can be loaded in
SUMO.

These POIs represent the physical locations where traffic measurements are
collected and act as anchors for linking real-world data with the simulation
network.

### POI–Edge Mapping

Each generated POI is then associated with the **closest edge in the SUMO
network**.

This step creates a mapping between monitoring locations and the road segments
that vehicles traverse in the simulation.

The resulting mapping is stored in a file such as:

```poi_edge_mapping.csv```


Each row links:

- a monitoring **CSV dataset**
- the corresponding **POI**
- the **SUMO edge** representing the measurement location.

This mapping allows traffic counts from sensors to be projected onto the
simulation network.

### EdgeData Generation

Once the mapping is available, traffic counts from the CSV files can be
aggregated to create **SUMO edgeData files**.

For a selected **date** and **one-hour time slot**, the system collects all
measurements from the mapped sensors and aggregates the number of vehicles
entering each monitored edge.

Separate edgeData files are generated for each vehicle category:

- bicycles
- motorcycles
- passenger cars
- trucks
- buses

Each file contains the number of vehicles entering each edge during the
selected time interval.

These files are later used to generate realistic traffic demand in SUMO.

### Route Generation

The edgeData files are then used to generate vehicle routes in two stages.

1. **Random trip generation**

   SUMO's `randomTrips.py` tool is used to create a large set of candidate
   routes across the network for each vehicle type.

2. **Route sampling**

   The generated trips are processed using `routeSampler.py`, which selects a
   subset of routes that best reproduces the edge counts specified in the
   edgeData files.

The output of this stage is a set of **route files**, one for each vehicle
category.

These routes represent the simulated traffic demand consistent with the
observed traffic measurements.

---

# Scenario Generation Dashboard

The repository also includes an **interactive dashboard** used to create and
analyze SUMO simulation scenarios based on the preprocessing pipeline.

The dashboard provides a graphical interface that guides the user through the
scenario creation process.

## Running the Dashboard

The dashboard is implemented using **Streamlit**.

It can be started with:
```streamlit run dashboard.py```


Once started, the interface allows users to configure simulation scenarios
without directly interacting with the preprocessing scripts.

## Scenario Creation Workflow

The dashboard executes the following steps:

1. **Selection of date and time slot**

   The system scans the available CSV datasets and identifies all valid
   combinations of date and hour for which traffic measurements exist.

   The user selects:

   - a **date**
   - a **one-hour time slot**

2. **Loading traffic measurements**

   The system reads the CSV files associated with the selected time interval
   and aggregates the vehicle counts per edge using the previously generated
   POI–edge mapping.

3. **Vehicle count scaling**

   The measured counts define the default demand level.

   The user can adjust the **total number of vehicles in the scenario** and
   modify the **modal split** between vehicle categories using interactive
   sliders.

   The dashboard automatically keeps the modal proportions consistent.

4. **Generation of simulation inputs**

   When the user starts scenario generation, the system automatically produces:

   - edgeData files for each vehicle category
   - vehicle type definitions (`types.add.xml`)
   - route files generated via SUMO tools
   - a simulation configuration file (`sumocfg`)

5. **Simulation execution**

   The dashboard can optionally launch the simulation directly using
   **SUMO-GUI**, allowing users to visualize the generated traffic scenario.

## Simulation Outputs

The SUMO configuration includes several output files, such as:

- `tripinfos.xml`
- `vehroute.xml`
- `summary.xml`
- `edgedata-output.xml`
- `emission-output.xml`

These files contain detailed information about vehicle trajectories,
performance metrics, and environmental impacts.

## Simulation Analytics

After the simulation finishes, the dashboard can load the output files and
display key performance indicators derived from `tripinfos.xml`.

The analytics module computes aggregated metrics per vehicle type, including:

- average **waiting time**
- average **time loss**
- average **trip duration**
- average **speed**
- average **fuel consumption**
- average **CO₂ emissions**

These metrics are presented through:

- summary KPI indicators
- tables grouped by vehicle category
- interactive charts comparing vehicle types.

This allows users to quickly evaluate the performance and environmental impact
of the generated scenario.