#!/usr/bin/env python3
"""
azure_inventory.py - Export an Azure tenant inventory to JSON using Azure Resource Graph.

Why Resource Graph instead of ~30 per-service SDK clients?
  * One query covers every subscription you can read (whole tenant in seconds/minutes)
  * Read-only, needs only the built-in "Reader" role
  * Returns full ARM properties, so VNets/subnets/NICs/peerings/private endpoints are all there

Usage examples
  az login --tenant <TENANT_ID>
  python azure_inventory.py                                   # every subscription you can read
  python azure_inventory.py --subscriptions <id1> <id2>       # selected subscriptions
  python azure_inventory.py --management-group mg-platform    # everything under a management group
  python azure_inventory.py --slim -o inventory.json          # smaller file (drops unneeded properties)

Output: a single JSON file (default azure_inventory.json) with
  metadata, subscriptions, resourceGroups, resources
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

try:
    from azure.identity import AzureCliCredential, DefaultAzureCredential, InteractiveBrowserCredential
    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
    from azure.core.exceptions import HttpResponseError
except ImportError:  # pragma: no cover
    sys.exit("Missing packages. Run:  pip install -r requirements.txt")


# Types that add size/noise but no architectural value. Use --include-all to keep them.
NOISE_TYPES = [
    "microsoft.compute/virtualmachines/extensions",
    "microsoft.compute/virtualmachinescalesets/extensions",
    "microsoft.compute/restorepointcollections",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.portal/dashboards",
    "microsoft.security/automations",
    "microsoft.resources/deploymentscripts",
]

# Types whose full `properties` are needed to work out topology. With --slim, every other
# type keeps only a handful of light-weight properties.
TOPOLOGY_TYPES = {
    "microsoft.network/virtualnetworks",
    "microsoft.network/networkinterfaces",
    "microsoft.network/privateendpoints",
    "microsoft.network/loadbalancers",
    "microsoft.network/applicationgateways",
    "microsoft.network/azurefirewalls",
    "microsoft.network/bastionhosts",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/connections",
    "microsoft.network/localnetworkgateways",
    "microsoft.network/natgateways",
    "microsoft.network/publicipaddresses",
    "microsoft.network/privatednszones/virtualnetworklinks",
    "microsoft.network/virtualhubs",
    "microsoft.compute/virtualmachines",
    "microsoft.compute/virtualmachinescalesets",
    "microsoft.containerservice/managedclusters",
    "microsoft.web/sites",
    "microsoft.web/hostingenvironments",
    "microsoft.sql/managedinstances",
    "microsoft.databricks/workspaces",
    "microsoft.app/managedenvironments",
    "microsoft.apimanagement/service",
    "microsoft.dbforpostgresql/flexibleservers",
    "microsoft.dbformysql/flexibleservers",
    "microsoft.network/dnsresolvers",
    "microsoft.network/dnsresolvers/inboundendpoints",
    "microsoft.network/dnsresolvers/outboundendpoints",
}
SLIM_KEEP_KEYS = {
    "provisioningState", "state", "status", "version", "serverFarmId", "defaultHostName",
    "hostNames", "publicNetworkAccess", "minimalTlsVersion", "fullyQualifiedDomainName",
    "isHnsEnabled", "accessTier", "supportsHttpsTrafficOnly", "enableSoftDelete",
    "enablePurgeProtection", "kind", "sku", "managedResourceGroupId", "workspaceUrl",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def get_credential(mode: str):
    if mode == "cli":
        return AzureCliCredential()
    if mode == "browser":
        return InteractiveBrowserCredential()
    # 'default' covers env-var service principals, managed identity, az CLI, VS Code, ...
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class GraphExporter:
    PAGE_SIZE = 1000
    SUB_BATCH = 1000  # Resource Graph accepts up to 1000 subscriptions per request

    def __init__(self, credential, subscriptions: Optional[List[str]], management_groups: Optional[List[str]]):
        self.client = ResourceGraphClient(credential)
        self.subscriptions = subscriptions
        self.management_groups = management_groups

    # -- low level ---------------------------------------------------------
    def _run(self, query: str, subs: Optional[List[str]]) -> Iterable[Dict[str, Any]]:
        skip_token = None
        while True:
            req = QueryRequest(
                query=query,
                subscriptions=subs,
                management_groups=self.management_groups if not subs else None,
                options=QueryRequestOptions(top=self.PAGE_SIZE, skip_token=skip_token,
                                            result_format="objectArray", allow_partial_scopes=True),
            )
            for attempt in range(6):
                try:
                    resp = self.client.resources(req)
                    break
                except HttpResponseError as exc:  # throttling -> back off and retry
                    if exc.status_code in (429, 503) and attempt < 5:
                        wait = 2 ** attempt + 1
                        log(f"   ... throttled, retrying in {wait}s")
                        time.sleep(wait)
                        continue
                    raise
            for row in resp.data or []:
                yield row
            skip_token = resp.skip_token
            if not skip_token:
                return

    def query(self, query: str) -> List[Dict[str, Any]]:
        if self.subscriptions:
            rows: List[Dict[str, Any]] = []
            for i in range(0, len(self.subscriptions), self.SUB_BATCH):
                rows.extend(self._run(query, self.subscriptions[i:i + self.SUB_BATCH]))
            return rows
        # No explicit scope -> every subscription the identity can read (or the management groups)
        return list(self._run(query, None))


def slim_properties(res: Dict[str, Any]) -> Dict[str, Any]:
    if res.get("type", "").lower() in TOPOLOGY_TYPES:
        return res
    props = res.get("properties") or {}
    if isinstance(props, dict):
        res["properties"] = {k: v for k, v in props.items() if k in SLIM_KEEP_KEYS}
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Export Azure tenant inventory via Azure Resource Graph")
    ap.add_argument("-o", "--output", default="azure_inventory.json", help="output JSON file")
    ap.add_argument("--subscriptions", nargs="*", help="subscription IDs to include (default: all readable)")
    ap.add_argument("--management-group", nargs="*", dest="mgs", help="management group IDs to scope the export")
    ap.add_argument("--auth", choices=["default", "cli", "browser"], default="default",
                    help="credential type (default chain, az CLI, or interactive browser)")
    ap.add_argument("--slim", action="store_true", help="keep full properties only for topology-relevant types")
    ap.add_argument("--include-all", action="store_true", help="do not filter noisy types (extensions, alerts...)")
    ap.add_argument("--exclude-tags", action="store_true", help="drop tags from the export")
    args = ap.parse_args()

    cred = get_credential(args.auth)
    gx = GraphExporter(cred, args.subscriptions, args.mgs)

    log("Querying subscriptions ...")
    subs = gx.query(
        "resourcecontainers"
        " | where type == 'microsoft.resources/subscriptions'"
        " | project subscriptionId, name, tenantId, tags,"
        "   state = tostring(properties.state),"
        "   managementGroups = properties.managementGroupAncestorsChain"
    )
    log(f"   {len(subs)} subscription(s)")
    if not subs:
        sys.exit("No subscriptions visible. Check 'az login --tenant ...' and that you hold Reader access.")

    log("Querying resource groups ...")
    rgs = gx.query(
        "resourcecontainers"
        " | where type == 'microsoft.resources/subscriptions/resourcegroups'"
        " | project id, name, subscriptionId, location, tags, managedBy"
    )
    log(f"   {len(rgs)} resource group(s)")

    log("Querying resources (this is the big one) ...")
    where = ""
    if not args.include_all:
        quoted = ", ".join(f"'{t}'" for t in NOISE_TYPES)
        where = f" | where type !in~ ({quoted})"
    resources = gx.query(
        "resources" + where +
        " | project id, name, type = tolower(type), kind, location, resourceGroup, subscriptionId,"
        "   sku, tags, identity, zones, managedBy, properties"
    )
    log(f"   {len(resources)} resource(s)")

    # Extra tables used for the solution-architecture view (backup, DR replication, AVD).
    # Each is optional: if a table is unavailable the export simply continues without it.
    extras: Dict[str, List[Dict[str, Any]]] = {}
    extra_queries = {
        "backupItems": (
            "recoveryservicesresources"
            " | where type =~ 'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers/protecteditems'"
            " | project id, name, type, resourceGroup, subscriptionId, properties"),
        "replicationItems": (
            "recoveryservicesresources"
            " | where type =~ 'microsoft.recoveryservices/vaults/replicationfabrics/replicationprotectioncontainers/replicationprotecteditems'"
            " | project id, name, type, resourceGroup, subscriptionId, properties"),
        "avdSessionHosts": (
            "desktopvirtualizationresources"
            " | where type =~ 'microsoft.desktopvirtualization/hostpools/sessionhosts'"
            " | project id, name, type, resourceGroup, subscriptionId, properties"),
    }
    for key, q in extra_queries.items():
        log(f"Querying {key} ...")
        try:
            extras[key] = gx.query(q)
            log(f"   {len(extras[key])} item(s)")
        except Exception as exc:  # table missing / not permitted -> skip
            extras[key] = []
            log(f"   skipped ({str(exc).splitlines()[0][:120]})")

    if args.slim:
        resources = [slim_properties(r) for r in resources]
    if args.exclude_tags:
        for coll in (subs, rgs, resources):
            for item in coll:
                item.pop("tags", None)

    by_type: Dict[str, int] = {}
    for r in resources:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1

    out = {
        "metadata": {
            "exportedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "Azure Resource Graph",
            "tenantIds": sorted({s.get("tenantId") for s in subs if s.get("tenantId")}),
            "scope": {"subscriptions": args.subscriptions or "all-readable", "managementGroups": args.mgs},
            "counts": {"subscriptions": len(subs), "resourceGroups": len(rgs), "resources": len(resources),
                       **{k: len(v) for k, v in extras.items()}},
            "resourcesByType": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        },
        "subscriptions": subs,
        "resourceGroups": rgs,
        "resources": resources,
        **extras,
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=str)
    log(f"Done -> {args.output}")
    log("Next:  python build_drawio.py " + args.output)


if __name__ == "__main__":
    main()
