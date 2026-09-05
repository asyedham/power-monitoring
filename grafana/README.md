# Grafana dashboards and metrics

This directory is the Grafana app, Thanos datasource, and the **OpenShift Virtualization Power Monitoring Dashboard** (`dashboard-ioscale-power.json`). Install copies the JSON into ConfigMap `grafana-ioscale-dashboard`. In Grafana the board is **OpenShift Virt → OpenShift Virtualization Power Monitoring Dashboard**.

Queries go to in-cluster Thanos (`thanos-querier.openshift-monitoring`). Kepler scrapes are in user-workload monitoring; kubelet / kube-state-metrics / KubeVirt series come from platform Prometheus. Thanos federates both.

## Two kinds of watts

Do not treat these as the same number.

| Layer | Metric | What it is |
| --- | --- | --- |
| **Redfish (BMC)** | `kepler_platform_watts` | Whole chassis / platform watts from the BMC (PSU, fans, CPU, memory, disks). This is the **cluster total**. |
| **RAPL (Kepler)** | `kepler_node_cpu_watts`, `kepler_pod_cpu_watts` | Intel RAPL for CPU package + DRAM only. Typically about **42–49%** of platform watts. Use this for **shape** (which node or VM moved), not as a substitute for BMC energy. |

Empty Redfish series usually means BMC auth failed (401) or Redfish is off in Kepler. RAPL can still be present in that case.

Kepler **pod** power is RAPL attributed to a workload. The workload name is label **`pod_name`**, not `pod` (`pod` is the Kepler exporter).

## How to read a run

Set the Grafana time range to one phase at a time:

1. **Idle** — no VMs (or none Running)
2. **VMs-up** — virt-launchers Running, no FIO / workload yet
3. **Workload** — steady state; skip ramp-up

Use **mean** (or median) from the legend for consumption. Max is a spike, not energy.

## Power Consumption

Watt panels in **Power Consumption** use one naming pattern: **(Redfish)** for BMC chassis watts, **RAPL (Kepler)** for CPU+DRAM. **Running VMIs** is the live count (stat). **Per-VM RAPL (Kepler)** is one series per virt-launcher.

| Panel | Metric (high level) | Meaning |
| --- | --- | --- |
| Cluster total (Redfish) | `sum(kepler_platform_watts)` | Live BMC watts for the whole cluster |
| Cluster RAPL (Kepler) | `sum(kepler_node_cpu_watts)` | Cluster CPU+DRAM RAPL over time (paired with BMC) |
| Per-node (Redfish) | `kepler_platform_watts` by `node_name` | Same BMC signal split per node |
| RAPL per node (Kepler) | `kepler_node_cpu_watts` by `node_name` | CPU+DRAM RAPL per node |
| Total VM RAPL (Kepler) | `sum(kepler_pod_cpu_watts{pod_name=~"virt-launcher-.*"})` | All virt-launcher RAPL combined |
| VM RAPL by node (Kepler) | same, `sum by (node_name)` | Which node the VMs are burning RAPL on |
| Per-VM RAPL (Kepler) | same, one series per virt-launcher (VM name from pod) | RAPL (Kepler) for each VM |
| Running VMIs | `kubevirt_vmi_info` phase Running | How many VMs are Running (stat only) |

VM RAPL will stay near zero if there are no Running virt-launchers, or if the query used the wrong label (`pod` instead of `pod_name`).

## Grafana pod overhead

This row is the **observer** (namespace `grafana`, container `grafana`). It is not cluster or VM power.

| Panel | Metric (high level) | Meaning |
| --- | --- | --- |
| CPU | kubelet CPU usage | Cores Grafana is using (dashboard refresh + Thanos queries) |
| Memory (working set) | kubelet working set | Memory counted toward the cgroup limit (OOM uses this) |
| RAPL (Kepler) | `kepler_pod_cpu_watts` for the Grafana pod | RAPL watts attributed to Grafana — not BMC chassis watts |
| RAPL package vs DRAM (Kepler) | same, split by `zone` | CPU package vs DRAM share of that RAPL |
| Restarts | kube-state-metrics restart count | `0` is healthy; a bump is a crash, OOM, or rollout |
| CPU throttled | CFS throttled periods / all periods | Fraction of time Grafana hit its CPU limit |

## Files

| File | Role |
| --- | --- |
| `dashboard-ioscale-power.json` | OpenShift Virtualization Power Monitoring Dashboard |
| `grafana-app.yaml` | Namespace, Grafana deploy, datasource, dashboard provider |
| `datasource.yaml` | Thanos datasource (bearer token for thanos-querier); reapplied on every install |
