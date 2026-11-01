# Network Diagnostic and Path Analysis Tool

A Computer Networks project that combines network diagnosis, topology/path analysis, and Software-Defined Networking (SDN) routing experiments.

The repository contains two connected parts:

- **D1 – Network Diagnostic Dashboard:** measures network performance and displays diagnostic information such as packet loss, round-trip time (RTT), jitter, and network paths.
- **D2 – SDN and Adaptive Routing:** uses a Ryu OpenFlow controller and a Mininet diamond topology to compare paths, apply routing policies, demonstrate link-failure failover, and monitor traffic for loss-aware adaptive routing.

## Repository contents

| File or folder | Purpose |
|---|---|
| `analyzer.py` | Network/topology analysis helpers |
| `dashboard_server.py` | D1 dashboard backend |
| `dashboard_server_d2.py` | D2 live dashboard backend |
| `controller.py` | Ryu SDN controller and REST API |
| `topo.py` | D1 Mininet topology |
| `topo_d2.py` | D2 diamond Mininet topology |
| `udp_probe.py`, `udp_server.py` | UDP measurement client and server |
| `d2_experiments.py` | D2 path, loss, and failover experiments |
| `d2_adaptive.py` | Loss-aware adaptive-routing experiment |
| `plot_results.py` | Generates evaluation graphs from experiment JSON |
| `static/` | Dashboard pages, JavaScript, and CSS |
| `graphs/` | Generated D2 evaluation graphs |
| `archive/` | Older controller implementation kept for reference |
| `d2_results.json`, `d2_adaptive_results.json` | Saved experiment results |

The active Python scripts are kept in the repository root because they use paths and imports relative to one another.

## Requirements

This project is designed for Ubuntu with Mininet and Open vSwitch.

System packages and tools:
- Python 3
- Mininet
- Open vSwitch
- Git

Python packages:
- Ryu
- NetworkX
- WebOb
- Matplotlib (only needed to regenerate the graphs)

Install Mininet and Open vSwitch using your Ubuntu distribution's packages, then install the Python dependencies in a virtual environment. Ryu is kept in a virtual environment to avoid conflicts with system Python packages.

Example setup:

```bash
sudo apt update
sudo apt install mininet openvswitch-switch python3-venv python3-pip
python3 -m venv ~/ryuenv
source ~/ryuenv/bin/activate
python -m pip install --upgrade pip
python -m pip install ryu networkx webob matplotlib
```

If Ryu is already installed in `~/ryuenv`, activate that environment instead of creating it again.

## Clone the repository

```bash
git clone https://github.com/meghashree787/Network-Diagnostic-and-Path-Analysis-Tool.git
cd Network-Diagnostic-and-Path-Analysis-Tool
```

All team members should work from this repository so that the source code, dashboards, experiment scripts, and results stay together.

## Run the D2 live SDN dashboard

Use three terminals. Run the commands from the repository root.

### Terminal 1 – Start the Ryu controller

```bash
cd Network-Diagnostic-and-Path-Analysis-Tool
source ~/ryuenv/bin/activate
ryu-manager --observe-links controller.py
```

Keep this terminal running. The controller exposes its REST API on port `8080` and OpenFlow on port `6653`.

### Terminal 2 – Start the Mininet topology

```bash
cd Network-Diagnostic-and-Path-Analysis-Tool
sudo mn -c
sudo python3 topo_d2.py
```

At the Mininet prompt, check connectivity and start the UDP server:

```text
mininet> pingall
mininet> h2 python3 udp_server.py &
```

Keep Mininet running.

### Terminal 3 – Start the D2 dashboard backend

```bash
cd Network-Diagnostic-and-Path-Analysis-Tool
source ~/ryuenv/bin/activate
sudo -E ~/ryuenv/bin/python dashboard_server_d2.py
```

Open this address in a browser running in the same Ubuntu environment:

```text
http://127.0.0.1:5000/d2.html
```

The dashboard displays the detected topology, active route, path cost, routing policy, link controls, diagnosis results, and adaptive-monitoring information.

## D2 demonstration sequence

1. Confirm that the controller is connected and that the four switches and links are detected.
2. Run a diagnosis to measure packet delivery, packet loss, RTT, and jitter.
3. Apply the **Avoid link s1-s2** policy and check that traffic uses the alternate path `s1 → s3 → s4`.
4. Clear the policy to return to the preferred path.
5. Fail the `s1-s2` link and verify that the controller selects the alternate path.
6. Run diagnosis again to check that traffic still reaches the destination.
7. Start adaptive monitoring and observe the loss/RTT measurements and monitoring table.
8. Stop monitoring, restore the failed link, and verify connectivity again with `pingall`.

Adaptive monitoring observes measurements over time. A threshold-triggered adaptive reroute should only be claimed when the experiment actually records that trigger and route change.

## Run the D2 experiment scripts

These are separate automated experiments; do not run them at the same time as another Mininet topology.

Start the controller in one terminal:

```bash
source ~/ryuenv/bin/activate
ryu-manager --observe-links controller.py
```

Then, in another terminal, from the repository root:

```bash
sudo mn -c
sudo /usr/bin/python3 d2_experiments.py
```

For the loss-aware adaptive experiment, use:

```bash
sudo mn -c
sudo /usr/bin/python3 d2_adaptive.py
```

The scripts save their results as `d2_results.json` and `d2_adaptive_results.json` in the repository root.

Regenerate the evaluation graphs outside Mininet:

```bash
python3 -m pip install matplotlib
python3 plot_results.py
```

Generated images are saved in `graphs/`.

## D1 dashboard

The D1 dashboard files are also included:

- Backend: `dashboard_server.py`
- Dashboard page: `static/index.html`
- Supporting scripts: `analyzer.py` and `udp_probe.py`

The D1 backend uses the same root-level `static/` folder and UDP probe script. Start the required D1 Mininet topology and Ryu controller before launching the backend:

```bash
sudo python3 topo.py
```

In a separate terminal, launch the D1 dashboard backend:

```bash
sudo python3 dashboard_server.py
```

Then open:

```text
http://127.0.0.1:5000
```

## Troubleshooting

- **`ryu-manager: command not found`:** activate the Ryu environment with `source ~/ryuenv/bin/activate`.
- **Controller not reachable:** make sure the Ryu controller is running before starting Mininet and that the controller REST API is available on port `8080`.
- **Stale Mininet processes or switches:** stop the previous topology and run `sudo mn -c` before restarting.
- **Dashboard cannot control links:** start the dashboard backend with `sudo -E` so it has permission to change Mininet interface state.
- **Dashboard shows demo/fallback topology:** check that the controller is running and reachable at `127.0.0.1:8080`.

## Notes for the team

- Pull the latest changes before starting work: `git pull origin main`.
- Keep active scripts and their relative dependencies together in the repository root unless all affected paths/imports are updated and tested.
- Do not commit virtual environments, Python cache files, or temporary runtime logs.
- Stop old Mininet topologies before starting a new experiment.
