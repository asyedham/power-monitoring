"""Jinja filter: QUADS ocpinventory.json nodes -> Kepler redfish.yaml / redfish.csv."""

from __future__ import annotations

from urllib.parse import urlparse


def origin(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return ""
    if "://" in address:
        vendor_scheme, rest = address.split("://", 1)
        if "+" in vendor_scheme:
            vendor_scheme = vendor_scheme.split("+", 1)[1]
            address = vendor_scheme + "://" + rest
    if "://" not in address:
        return "https://" + address.split("/")[0]
    parsed = urlparse(address)
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    netloc = parsed.netloc or parsed.path.split("/")[0]
    return f"{scheme}://{netloc}"


def _node_short_name(pm_addr: str) -> str:
    fqdn = (pm_addr or "").replace("mgmt-", "", 1)
    return fqdn.split(".")[0].strip()


def ocpinventory_to_redfish(
    nodes,
    username,
    password,
    insecure=True,
    skip_bastion=True,
    host_count=3,
):
    """Map QUADS ocpinventory.json nodes to Kepler Redfish docs.

    Kubernetes node name is the short hostname from pm_addr (mgmt- prefix
    stripped). Default: skip nodes[0] (bastion) and take the next 3 hosts.
    """
    if not nodes:
        return {"yaml": "", "csv": "", "count": 0}
    start = 1 if skip_bastion and len(nodes) > 1 else 0
    selected = list(nodes[start:])
    if host_count is not None and int(host_count) >= 0:
        selected = selected[: int(host_count)]
    rows = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        pm = (item.get("pm_addr") or "").strip()
        if not pm:
            continue
        short = _node_short_name(pm)
        if not short:
            continue
        endpoint = origin(pm if "://" in pm else "https://" + pm)
        user = (item.get("pm_user") or username or "").strip() or username
        pwd = item.get("pm_password") if item.get("pm_password") is not None else password
        rows.append((short, endpoint, user, pwd))
    return {
        "yaml": _render_yaml_per_node(rows, insecure=bool(insecure)),
        "csv": "\n".join(f"{n},{u},{p},{e}" for n, e, u, p in rows) + ("\n" if rows else ""),
        "count": len(rows),
    }


def _render_yaml_per_node(rows, insecure: bool) -> str:
    if not rows:
        return ""
    insecure_s = "true" if insecure else "false"
    lines = ["nodes:"]
    for node, _endpoint, _user, _pwd in rows:
        bmc_id = "bmc-" + node.replace(".", "-")
        lines.append(f"  {node}: {bmc_id}")
    lines.append("")
    lines.append("bmcs:")
    for node, endpoint, user, pwd in rows:
        bmc_id = "bmc-" + node.replace(".", "-")
        lines.append(f"  {bmc_id}:")
        lines.append(f"    endpoint: {endpoint}")
        lines.append(f"    username: {user}")
        lines.append(f'    password: "{pwd}"')
        lines.append(f"    insecure: {insecure_s}")
    return "\n".join(lines) + "\n"


class FilterModule:
    def filters(self):
        return {
            "ocpinventory_to_redfish": ocpinventory_to_redfish,
        }
