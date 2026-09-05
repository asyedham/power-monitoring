#!/usr/bin/env python3
"""Set Kepler operator UWM scrape-token TTL and optionally recreate the token."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from ansible.module_utils.basic import AnsibleModule

FLAG = "--exp.uwm.token.ttl"
SECRET_NAME = "prometheus-user-workload-token"
SECRET_NS = "power-monitor"
EXP_ANNOTATION = "powermonitor.sustainable.computing.io/secret-token-expiration"
AUDIENCE = "power-monitor.power-monitor.svc"
UWM_SA_NS = "openshift-user-workload-monitoring"

DOCUMENTATION = r"""
---
module: kepler_uwm_token_ttl
short_description: Set Kepler UWM scrape-token TTL
description:
  - Patches the Kepler Operator CSV/Deployment --exp.uwm.token.ttl flag.
  - Optionally deletes prometheus-user-workload-token so a new token is issued.
options:
  ttl:
    type: str
    default: 8760h
  operator_namespace:
    type: str
    default: ""
  timeout:
    type: int
    default: 240
  skip_recreate:
    type: bool
    default: false
  kubeconfig:
    type: str
    default: ""
"""


def oc_env(kubeconfig: str) -> dict[str, str]:
    env = os.environ.copy()
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig
    return env


class Oc:
    def __init__(self, kubeconfig: str) -> None:
        self.env = oc_env(kubeconfig)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["oc", *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        if check and result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"oc {' '.join(args)} failed: {err}")
        return result

    def json(self, *args: str) -> Any:
        return json.loads(self.run("get", *args, "-o", "json").stdout)


def set_ttl_arg(args: list[str], ttl: str) -> tuple[list[str], bool]:
    desired = f"{FLAG}={ttl}"
    changed = False
    out: list[str] = []
    i = 0
    found = False
    while i < len(args):
        item = args[i]
        if item == FLAG or item.startswith(FLAG + "="):
            found = True
            if item == FLAG:
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    if args[i + 1] != ttl:
                        changed = True
                    out.append(FLAG)
                    out.append(ttl)
                    i += 2
                    continue
                out.append(desired)
                changed = True
                i += 1
                continue
            if item != desired:
                changed = True
            out.append(desired)
            i += 1
            continue
        out.append(item)
        i += 1
    if not found:
        out.append(desired)
        changed = True
    return out, changed


def find_kepler_csv(ocx: Oc, explicit_ns: str) -> tuple[str, str, dict[str, Any]]:
    namespaces = []
    if explicit_ns:
        namespaces.append(explicit_ns)
    namespaces.extend(["openshift-kepler-operator", "openshift-operators"])
    seen: set[str] = set()
    for ns in namespaces:
        if ns in seen:
            continue
        seen.add(ns)
        result = ocx.run("get", "csv", "-n", ns, "-o", "json", check=False)
        if result.returncode != 0:
            continue
        data = json.loads(result.stdout)
        for item in data.get("items", []):
            name = item.get("metadata", {}).get("name", "")
            if "kepler" in name.lower():
                return ns, name, item
    all_csv = ocx.json("csv", "-A")
    for item in all_csv.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if "kepler" in name.lower():
            ns = item["metadata"]["namespace"]
            return ns, name, item
    raise RuntimeError("Kepler Operator CSV not found")


def deployments_from_csv(csv: dict[str, Any]) -> list[dict[str, Any]]:
    return csv.get("spec", {}).get("install", {}).get("spec", {}).get("deployments") or []


def patch_csv_ttl(ocx: Oc, ns: str, name: str, csv: dict[str, Any], ttl: str) -> bool:
    changed_any = False
    for deploy in deployments_from_csv(csv):
        pod_spec = deploy.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("containers") or []:
            args = list(container.get("args") or [])
            new_args, changed = set_ttl_arg(args, ttl)
            if changed:
                container["args"] = new_args
                changed_any = True
    if not changed_any:
        return False
    patch = {"spec": {"install": csv["spec"]["install"]}}
    ocx.run(
        "patch",
        "csv",
        name,
        "-n",
        ns,
        "--type=merge",
        "--patch",
        json.dumps(patch),
    )
    return True


def find_operator_deploy(ocx: Oc, ns: str) -> tuple[str, dict[str, Any]]:
    data = ocx.json("deploy", "-n", ns)
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        if "kepler" in name.lower():
            return name, item
    raise RuntimeError(f"Kepler operator Deployment not found in {ns}")


def patch_deploy_ttl(ocx: Oc, ns: str, ttl: str) -> str:
    name, deploy = find_operator_deploy(ocx, ns)
    containers = deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    if not containers:
        raise RuntimeError(f"Deployment {name} has no containers")
    args = list(containers[0].get("args") or [])
    new_args, changed = set_ttl_arg(args, ttl)
    if changed:
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": containers[0]["name"],
                                "args": new_args,
                            }
                        ]
                    }
                }
            }
        }
        ocx.run(
            "patch",
            "deploy",
            name,
            "-n",
            ns,
            "--type=strategic",
            "--patch",
            json.dumps(patch),
        )
    return name


def wait_deploy_ttl(ocx: Oc, ns: str, deploy_name: str, ttl: str, timeout: int) -> None:
    wanted = f"{FLAG}={ttl}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        deploy = ocx.json("deploy", deploy_name, "-n", ns)
        args = (
            deploy.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [{}])[0]
            .get("args")
            or []
        )
        if wanted in args or FLAG in args and ttl in args:
            ocx.run(
                "rollout",
                "status",
                f"deploy/{deploy_name}",
                "-n",
                ns,
                "--timeout=180s",
                check=False,
            )
            return
        time.sleep(3)
    raise RuntimeError(f"Deployment {deploy_name} did not pick up {wanted}")


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def secret_info(ocx: Oc) -> dict[str, Any] | None:
    result = ocx.run("get", "secret", SECRET_NAME, "-n", SECRET_NS, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    secret = json.loads(result.stdout)
    token_b64 = (secret.get("data") or {}).get("token") or ""
    token = base64.b64decode(token_b64).decode("utf-8") if token_b64 else ""
    claims = jwt_claims(token) if token.count(".") >= 2 else {}
    annotation = (secret.get("metadata") or {}).get("annotations") or {}
    aud = claims.get("aud") or []
    if isinstance(aud, str):
        aud = [aud]
    return {
        "creation": secret.get("metadata", {}).get("creationTimestamp"),
        "annotation_exp": annotation.get(EXP_ANNOTATION),
        "jwt_exp": claims.get("exp"),
        "jwt_iat": claims.get("iat"),
        "aud": aud,
    }


def human_ts(unix: int | None) -> str:
    if not unix:
        return ""
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expected_seconds(ttl: str) -> float:
    total = 0.0
    num = ""
    for ch in ttl.strip():
        if ch.isdigit() or ch == ".":
            num += ch
            continue
        if not num:
            raise RuntimeError(f"invalid TTL {ttl!r}")
        value = float(num)
        num = ""
        if ch == "h":
            total += value * 3600
        elif ch == "m":
            total += value * 60
        elif ch == "s":
            total += value
        else:
            raise RuntimeError(f"unsupported TTL unit in {ttl!r}")
    if num:
        raise RuntimeError(f"invalid TTL {ttl!r}")
    return total


def wait_new_token(ocx: Oc, ttl: str, timeout: int) -> dict[str, Any]:
    want = expected_seconds(ttl)
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = secret_info(ocx)
        if last and last.get("jwt_exp") and last.get("jwt_iat"):
            lifetime = int(last["jwt_exp"]) - int(last["jwt_iat"])
            if abs(lifetime - want) <= 120:
                return last
        time.sleep(3)
    detail = ""
    if last and last.get("jwt_exp"):
        detail = (
            f" last jwt lifetime="
            f"{int(last['jwt_exp']) - int(last.get('jwt_iat') or last['jwt_exp'])}s"
            f" exp={human_ts(last.get('jwt_exp'))}"
        )
    raise RuntimeError(f"token secret did not recreate with TTL {ttl}.{detail}")


def recreate_secret(ocx: Oc) -> None:
    ocx.run("delete", "secret", SECRET_NAME, "-n", SECRET_NS, "--ignore-not-found=true")
    ocx.run(
        "annotate",
        "powermonitor",
        "power-monitor",
        f"power-monitoring/uwm-token-ttl-refresh={int(time.time())}",
        "--overwrite",
        check=False,
    )


def restart_uwm_prometheus_operator(ocx: Oc) -> None:
    ocx.run("rollout", "restart", "deploy/prometheus-operator", "-n", UWM_SA_NS, check=False)
    ocx.run(
        "rollout",
        "status",
        "deploy/prometheus-operator",
        "-n",
        UWM_SA_NS,
        "--timeout=180s",
        check=False,
    )


def run(params: dict[str, Any]) -> dict[str, Any]:
    ttl = (params["ttl"] or "8760h").strip()
    expected_seconds(ttl)
    ocx = Oc(params.get("kubeconfig") or "")
    messages: list[str] = []
    ns, csv_name, csv = find_kepler_csv(ocx, params.get("operator_namespace") or "")
    messages.append(f"CSV {csv_name} in namespace {ns}")
    csv_changed = patch_csv_ttl(ocx, ns, csv_name, csv, ttl)
    messages.append("Patched CSV args" if csv_changed else "CSV args already at target TTL")
    deploy_name = patch_deploy_ttl(ocx, ns, ttl)
    wait_deploy_ttl(ocx, ns, deploy_name, ttl, int(params.get("timeout") or 240))
    messages.append(f"Operator Deployment {deploy_name} has {FLAG}={ttl}")
    changed = csv_changed
    token_meta: dict[str, Any] | None = None
    if not params.get("skip_recreate"):
        recreate_secret(ocx)
        info = wait_new_token(ocx, ttl, int(params.get("timeout") or 240))
        lifetime_h = (int(info["jwt_exp"]) - int(info["jwt_iat"])) / 3600
        aud = info.get("aud") or []
        token_meta = {
            "iat": human_ts(info.get("jwt_iat")),
            "exp": human_ts(info.get("jwt_exp")),
            "lifetime_h": round(lifetime_h),
            "aud": aud,
            "annotation": info.get("annotation_exp"),
        }
        messages.append(
            "New scrape token: "
            f"iat={token_meta['iat']} exp={token_meta['exp']} "
            f"lifetime={token_meta['lifetime_h']}h aud={aud}"
        )
        if AUDIENCE not in aud:
            messages.append(f"WARNING: token audience {aud} does not include {AUDIENCE}")
        restart_uwm_prometheus_operator(ocx)
        changed = True
    return {
        "changed": changed,
        "msg": "; ".join(messages),
        "csv_namespace": ns,
        "csv_name": csv_name,
        "token": token_meta,
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            ttl=dict(type="str", default="8760h"),
            operator_namespace=dict(type="str", default=""),
            timeout=dict(type="int", default=240),
            skip_recreate=dict(type="bool", default=False),
            kubeconfig=dict(type="str", default=""),
        ),
        supports_check_mode=False,
    )
    try:
        result = run(module.params)
    except RuntimeError as exc:
        module.fail_json(msg=str(exc))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
