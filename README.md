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
SUMO:
