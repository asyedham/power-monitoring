#Power monitoring

Ansible playbooks that install **Kepler** (node / VM / platform watts) and **Grafana** on an OpenShift Virtualization cluster. Run everything from the bastion (or any host with `oc` and kubeconfig). Do not SSH into cluster nodes.

BMC user, password, and hosts are downloaded from QUADS `ocpinventory.json`. Grafana persistence uses the StorageClass you set in `ansible/group_vars/all.yml`.

## What install does

`ansible-playbook ansible/install.yml` runs these roles in order:

| Role | What it does |
|------|----------------|
| `check_cluster` | Verifies `oc` login and notes if OpenShift Virtualization is present |
| `user_workload_monitoring` | Enables OpenShift user-workload monitoring (Prometheus) |
| `bmc` | Downloads QUADS inventory and sets BMC user, password, and 3 cluster BMC URLs |
| `kepler` | Installs Kepler Operator + PowerMonitor; enables Redfish from that BMC map |
| `grafana` | Installs Grafana, dashboard, PVC on your StorageClass, reverse-proxy env |

## Prerequisites

On the machine that will run Ansible (this bastion):

- `oc` logged into the cluster
- Python 3 with `pip`
- Ansible 2.15+ (or the version already on the lab image)
- Network reachability to QUADS (for BMC) and the cluster API

```bash
cd /root/power-monitoring
export KUBECONFIG=/root/mno/kubeconfig   # or the path in group_vars
oc whoami
pip install kubernetes
ansible-galaxy collection install -r ansible/requirements.yml
```

`kubeconfig` can also be set in `ansible/group_vars/all.yml` (this repo already points at `/root/mno/kubeconfig`).

## 1. Set variables

Edit `ansible/group_vars/all.yml`. Required for this lab:

```yaml
kubeconfig: "/root/mno/kubeconfig"

lab: "performancelab"    # or scalelab
lab_cloud: "cloud08"     # QUADS cloud name

# Skip inventory nodes[0] (bastion); use the next 3 hosts
bmc_skip_bastion: true
bmc_host_count: 3

grafana_storage_class: "ocs-storagecluster-ceph-rbd"
grafana_admin_user: admin
grafana_admin_password: "admin"
```

| Variable | Purpose |
|----------|---------|
| `lab` / `lab_cloud` | Builds `http://<quads>/instack/<lab_cloud>_ocpinventory.json` |
| `bmc_skip_bastion` / `bmc_host_count` | Bastion excluded; next 3 machines are Kepler Redfish targets |
| `grafana_storage_class` | Exact StorageClass for PVC `grafana-data` (not auto-picked) |
| `grafana_admin_user` | Grafana login user (default `admin`) |
| `grafana_admin_password` | Grafana login password (default `admin`). Install writes it to secret `grafana-admin` and resets the Grafana database. |
| `grafana_proxy_port` | Local port for `grafana-proxy.yml` (default `3000`) |

`labs:` in the same file lists QUADS / DNS for `performancelab` and `scalelab`. Do not put BMC passwords in group vars; the `bmc` role fills them.

List StorageClasses if you need to change Grafana’s:

```bash
oc get sc
```

## 2. Install

From the repo root (uses root `ansible.cfg`):

```bash
cd /root/power-monitoring
ansible-playbook ansible/install.yml
```

Or from `ansible/`:

```bash
cd /root/power-monitoring/ansible
ansible-playbook install.yml
```

Install is idempotent. Re-run it to refresh BMC credentials, Kepler Redfish, Grafana dashboard, and the Grafana PVC.

### Optional tags

```bash
ansible-playbook ansible/install.yml --tags bmc
ansible-playbook ansible/install.yml --tags kepler
ansible-playbook ansible/install.yml --tags grafana
```

Skip pieces:

```bash
ansible-playbook ansible/install.yml -e skip_grafana=true
ansible-playbook ansible/install.yml -e skip_uwm=true
```

## 3. BMC (automatic)

You do not set `bmc_user`, `bmc_password`, or a hosts file.

The `bmc` role:

1. Downloads `http://{{ labs[lab].quads }}/instack/{{ lab_cloud }}_ocpinventory.json`
2. Takes user and password from `nodes[0]` (bastion)
3. Maps BMC URLs for `nodes[1]`, `nodes[2]`, `nodes[3]` only
4. Passes that map to Kepler as Secret `redfish-secret` in `power-monitor`

Example for `performancelab` / `cloud08`:

- URL: `http://quads2.rdu3.labs.perfscale.redhat.com/instack/cloud08_ocpinventory.json`
- Hosts: `aa37-h21-000-r740xd`, `aa37-h23-000-r740xd`, `aa37-h25-000-r740xd`
- BMC endpoint shape: `https://mgmt-<node>.rdu3.labs.perfscale.redhat.com`

Need at least 4 machines in the allocation (1 bastion + 3 cluster nodes). Extra allocation machines are ignored.

## 4. Grafana PVC

If `grafana_storage_class` is set, every install:

1. Creates PVC `grafana-data` in namespace `grafana` (2Gi, RWO, **that** class)
2. Mounts it on Deployment `grafana` at `/var/lib/grafana` (replaces `emptyDir`)

This runs even when Grafana already exists. You do not create the PVC by hand.

```bash
oc -n grafana get pvc grafana-data
```

`Bound` (or `Pending` until the pod mounts, if the class is WaitForFirstConsumer) is expected.

Leaving `grafana_storage_class` empty skips the PVC (ephemeral Grafana).

## 5. Open Grafana

Install starts a port-forward on this host. If `http://<fqdn>:3000/` refuses the connection, nothing is listening yet — start it:

```bash
ansible-playbook ansible/grafana-proxy.yml
```

That:

- Sets Grafana `GF_SERVER_ROOT_URL` to `http://<this-host-FQDN>:3000/`
- Port-forwards `svc/grafana:3000` to `0.0.0.0:3000` on this machine
- Prints the URL

Open in a browser:

```text
http://<hostname -f>:3000/
```

Example: `http://aa37-h19-000-r740xd.rdu3.labs.perfscale.redhat.com:3000/`

Login (set on every install from `all.yml`):

- User: **`admin`** (`grafana_admin_user`)
- Password: **`admin`** (`grafana_admin_password`)

Install updates both the Kubernetes secret and Grafana’s database so this password is what the login page accepts.

Dashboard: **OpenShift Virt → OpenShift Virtualization Power Monitoring Dashboard**. Datasource is in-cluster Thanos (user-workload / platform Prometheus).

Override host/port:

```bash
ansible-playbook ansible/grafana-proxy.yml \
  -e grafana_proxy_host=$(hostname -f) \
  -e grafana_proxy_port=3000
```

Stop the proxy:

```bash
kill $(pgrep -f 'oc port-forward .*svc/grafana')
```

## 6. Check Kepler

Wait about a minute after install for Prometheus to scrape.

```bash
oc -n power-monitor get ds,pods
oc -n power-monitor logs ds/power-monitor --tail=20
```

Useful PromQL (Observe → Metrics, or Grafana):

```text
sum(kepler_platform_watts)
sum(kepler_node_cpu_watts)
sum(kepler_pod_cpu_watts{pod_name=~"virt-launcher-.*"})
```

`kepler_platform_watts` needs BMC Redfish (the `bmc` role). RAPL / virt-launcher metrics still work without it.

Create VMs with your usual path; this repo does not create them. Grafana shows virt-launcher RAPL and Running VMI count when VMs exist.

## 7. Uninstall

Kepler only (Grafana stays):

```bash
ansible-playbook ansible/uninstall.yml
```

Kepler + Grafana (deletes namespace `grafana` and the PVC):

```bash
ansible-playbook ansible/uninstall.yml -e remove_grafana=true
```

Default `remove_grafana` is `false`. A Kepler-only uninstall leaves Grafana running; the next install will still attach `grafana-data` if `grafana_storage_class` is set.

## Layout

```text
ansible/
  install.yml           # full install
  grafana-proxy.yml     # hostname:port access
  uninstall.yml
  group_vars/all.yml    # lab, cloud, Grafana StorageClass, kubeconfig
  roles/
    bmc/                # QUADS inventory → BMC user/password/hosts
    kepler/
    grafana/            # app + PVC persist.yml + proxy.yml
    user_workload_monitoring/
    check_cluster/
grafana/                # Grafana manifests + OpenShift Virtualization Power Monitoring Dashboard (see grafana/README.md)
deploy/kepler/          # Kepler subscription / PowerMonitor
```

## Troubleshooting

**BMC / Redfish**

- `lab` must be `performancelab` or `scalelab`, and `lab_cloud` must match the QUADS allocation.
- Inventory must have at least four nodes.
- Playbook output shows BMC user and host count; passwords are not printed (`debug_secrets: true` only on a private terminal).

**Grafana PVC not found**

- Confirm `grafana_storage_class` in `all.yml` is a real class (`oc get sc`).
- Re-run `ansible-playbook ansible/install.yml` (PVC is created on every run, not only first install).
- `uninstall.yml` without `-e remove_grafana=true` does not delete Grafana.

**Grafana proxy**

- `oc -n grafana get svc grafana` must exist.
- If port 3000 is in use, `-e grafana_proxy_port=3001`.
- Open the **FQDN** (`hostname -f`), not only the short name, if that is what `GF_SERVER_ROOT_URL` used.

**Playbook Python**

```bash
pip install kubernetes
ansible-galaxy collection install -r ansible/requirements.yml
```
