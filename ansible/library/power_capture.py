#!/usr/bin/env python3
"""Sample kepler_platform_watts from Thanos/Prometheus into a CSV."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ansible.module_utils.basic import AnsibleModule

QUERY = "sum by (node_name) (kepler_platform_watts)"

DOCUMENTATION = r"""
---
module: power_capture
short_description: Capture Kepler platform watts to CSV
options:
  label:
    type: str
    required: true
  interval:
    type: int
    default: 10
  duration:
    type: int
    required: true
  output:
    type: str
    default: ""
  kubeconfig:
    type: str
    default: ""
  prom_url:
    type: str
    default: ""
"""


def oc_env(kubeconfig: str) -> dict[str, str]:
    env = os.environ.copy()
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig
    return env


def oc(kubeconfig: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["oc", *args],
        capture_output=True,
        text=True,
        env=oc_env(kubeconfig),
    )
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"oc {' '.join(args)} failed: {err}")
    return result


def bearer(kubeconfig: str) -> str:
    return oc(kubeconfig, "whoami", "-t").stdout.strip()


def thanos_url(kubeconfig: str) -> str:
    host = oc(
        kubeconfig,
        "get",
        "route",
        "thanos-querier",
        "-n",
        "openshift-monitoring",
        "-o",
        "jsonpath={.spec.host}",
        check=False,
    ).stdout.strip()
    return f"https://{host}" if host else ""


def http_get(url: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exec_query(kubeconfig: str, encoded: str) -> dict[str, Any]:
    for ns, pod in (
        ("openshift-user-workload-monitoring", "prometheus-user-workload-0"),
        ("openshift-monitoring", "prometheus-k8s-0"),
    ):
        result = oc(
            kubeconfig,
            "exec",
            "-n",
            ns,
            pod,
            "-c",
            "prometheus",
            "--",
            "wget",
            "-qO-",
            f"http://localhost:9090/api/v1/query?query={encoded}",
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    raise RuntimeError("could not query Prometheus (no Thanos route and oc exec failed)")


def query_power(kubeconfig: str, prom_url: str, token: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(QUERY, safe="")
    if prom_url:
        return http_get(f"{prom_url.rstrip('/')}/api/v1/query?query={encoded}", token)
    host = thanos_url(kubeconfig)
    if host:
        return http_get(f"{host}/api/v1/query?query={encoded}", token)
    return exec_query(kubeconfig, encoded)


def rows_from_result(payload: dict[str, Any]) -> list[tuple[str, float]]:
    total = 0.0
    out: list[tuple[str, float]] = []
    for item in (payload.get("data") or {}).get("result") or []:
        metric = item.get("metric") or {}
        node = metric.get("node_name") or metric.get("instance") or metric.get("node") or "unknown"
        if node == "TOTAL":
            continue
        watts = float(item["value"][1])
        total += watts
        out.append((node, watts))
    out.append(("TOTAL", total))
    return out


def run(params: dict[str, Any]) -> dict[str, Any]:
    label = params["label"]
    interval = max(1, int(params["interval"]))
    duration = int(params["duration"])
    if duration <= 0:
        raise RuntimeError("duration must be > 0")
    kubeconfig = params.get("kubeconfig") or ""
    prom_url = (params.get("prom_url") or os.environ.get("PROM_URL") or "").strip()
    output = params.get("output") or ""
    if not output:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output = f"power-{stamp}-{label}.csv"
    token = bearer(kubeconfig)
    samples = 0
    start = time.time()
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write("timestamp,label,node,watts\n")
        while True:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = query_power(kubeconfig, prom_url, token)
            last_total = 0.0
            for node, watts in rows_from_result(payload):
                handle.write(f"{ts},{label},{node},{watts}\n")
                if node == "TOTAL":
                    last_total = watts
            handle.flush()
            samples += 1
            elapsed = time.time() - start
            if elapsed >= duration:
                break
            sleep_for = interval - (elapsed % interval)
            if sleep_for < 0.2:
                sleep_for = interval
            remaining = duration - (time.time() - start)
            if remaining <= 0:
                break
            time.sleep(min(sleep_for, remaining))
    return {
        "changed": True,
        "output": os.path.abspath(output),
        "samples": samples,
        "label": label,
        "query": QUERY,
        "msg": f"{samples} samples written to {os.path.abspath(output)}",
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            label=dict(type="str", required=True),
            interval=dict(type="int", default=10),
            duration=dict(type="int", required=True),
            output=dict(type="str", default=""),
            kubeconfig=dict(type="str", default=""),
            prom_url=dict(type="str", default=""),
        ),
        supports_check_mode=False,
    )
    try:
        result = run(module.params)
    except RuntimeError as exc:
        module.fail_json(msg=str(exc))
    except Exception as exc:  # noqa: BLE001
        module.fail_json(msg=str(exc))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
