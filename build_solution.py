#!/usr/bin/env python3
"""
build_solution.py - Solution-architecture view + written architecture document from azure_inventory.json.

Produces (all from the same export, so they never drift apart):
  <name>.drawio                 page 1 "Solution architecture" (workloads, access path, backup, DR, findings)
                                page 2 "Network topology" and one page per subscription (from build_drawio.py)
  <name>.html                   same diagram in a browser viewer (like a draw.io "Export as HTML")
  <name>_architecture.md        written architecture document: how the estate is built and runs
  <name>_ai_context.md          compact context for Copilot / Claude

Workload grouping and the "how it works" narrative come from a small JSON config
(see solution_config.example.json). Everything else - VNets, IPs, peerings, private endpoints,
backup items, ASR replication, AVD, findings - is read from Azure on every run.

Usage
  python build_solution.py azure_inventory.json --config solution_config.example.json -o my_azure
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Set, Tuple


from build_drawio import (C, CELL_W, EDGE, FONT, SPECIAL_SUBNETS, TYPE_INFO, Box, DiagramBuilder,
                          Leaf, breakable, Page, Topology, container_style, esc, g, lid, name_from_id,
                          props, subtitle, tooltip_for, type_info, write_ai_context)

EDGE.update({
    "transit": ("#0078D4", "strokeWidth=2.5;startArrow=none;endArrow=classic;"),
    "peer_thin": ("#7FB2E5", "strokeWidth=1;startArrow=classic;endArrow=classic;dashed=1;"),
    "backup": ("#6B7280", "dashed=1;dashPattern=2 3;strokeWidth=1.2;endArrow=open;"),
    "asr": ("#C2185B", "dashed=1;strokeWidth=2.2;endArrow=classic;"),
    "custom": ("#374151", "strokeWidth=1.8;startArrow=classic;endArrow=classic;"),
    "users": ("#16A34A", "strokeWidth=2.5;endArrow=classic;"),
    "avd": ("#2E7D32", "strokeWidth=1.5;endArrow=classic;"),
})

# Shown nowhere on the solution page (implied, noisy, or rolled into a parent's label)
HIDE_SOLUTION = {
    "microsoft.compute/disks", "microsoft.network/networkinterfaces", "microsoft.network/networksecuritygroups",
    "microsoft.network/publicipaddresses", "microsoft.network/networkwatchers",
    "microsoft.network/privatednszones/virtualnetworklinks", "microsoft.network/privatednszones",
    "microsoft.insights/datacollectionrules", "microsoft.insights/datacollectionendpoints",
    "microsoft.automation/automationaccounts/runbooks", "microsoft.compute/sshpublickeys",
    "microsoft.billingbenefits/credits", "microsoft.app/managedenvironments/certificates",
    "microsoft.cognitiveservices/accounts/projects", "microsoft.syntex/accounts", "microsoft.web/connections",
    "microsoft.managedidentity/userassignedidentities", "microsoft.network/routetables",
    "microsoft.network/connections", "microsoft.network/virtualnetworks", "microsoft.compute/snapshots",
    "microsoft.insights/actiongroups", "microsoft.compute/restorepointcollections",
}
RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
SEV_ORDER = {"High": 0, "Medium": 1, "Low": 2, "Info": 3}
SEV_COLOR = {"High": "#DC2626", "Medium": "#D97706", "Low": "#2563EB", "Info": "#6B7280"}


def short(x: str, n: int = 60) -> str:
    return x if len(x) <= n else x[: n - 1] + "…"


def region_name(loc: str) -> str:
    return {"centralindia": "Central India", "eastus": "East US", "westus": "West US", "northeurope": "North Europe",
            "westeurope": "West Europe", "southcentralus": "South Central US", "southindia": "South India",
            "indiasouthcentral": "India South Central", "westus2": "West US 2", "denmarkeast": "Denmark East",
            "uksouth": "UK South", "eastus2": "East US 2", "global": "global"}.get((loc or "").lower(), loc or "")


# =====================================================================================
# Facts: everything the diagram and the document need, computed once
# =====================================================================================
class Facts:
    def __init__(self, inv: dict, cfg: dict):
        self.inv, self.cfg = inv, cfg
        self.t = Topology(inv)
        self.res = self.t.res
        self.exported = inv.get("metadata", {}).get("exportedAtUtc", "")
        self.backups = inv.get("backupItems", []) or []
        self.asr = inv.get("replicationItems", []) or []
        self.session_hosts = inv.get("avdSessionHosts", []) or []
        self.by_name: Dict[str, str] = {}
        for rid, r in self.res.items():
            self.by_name.setdefault(lid(r.get("name")), rid)
        self.nic_nsg = {rid: lid(g(props(r), "networkSecurityGroup", "id")) for rid, r in self.res.items()
                        if r["type"] == "microsoft.network/networkinterfaces"}
        self._vm_info()
        self._backup_info()
        self._asr_info()
        self._assign_workloads()
        self.findings = self._findings()

    # -- VMs ---------------------------------------------------------------------------
    def _vm_info(self) -> None:
        self.vm: Dict[str, dict] = {}
        for rid, r in self.res.items():
            if r["type"] != "microsoft.compute/virtualmachines":
                continue
            p = props(r)
            ips, nsgs, public = [], [], False
            for n in g(p, "networkProfile", "networkInterfaces", default=[]) or []:
                nid = lid(n.get("id"))
                nic = self.res.get(nid)
                if nsg := self.nic_nsg.get(nid):
                    nsgs.append(nsg)
                for ipc in props(nic or {}).get("ipConfigurations") or []:
                    ip = ipc.get("properties") or {}
                    if ip.get("privateIPAddress"):
                        ips.append(ip["privateIPAddress"])
                    if g(ip, "publicIPAddress", "id"):
                        public = True
            pip_addr = ""
            if public:
                for pr in self.res.values():
                    if pr["type"] == "microsoft.network/publicipaddresses":
                        cfgid = lid(g(props(pr), "ipConfiguration", "id"))
                        if any(cfgid.startswith(lid(n.get("id"))) for n in g(p, "networkProfile", "networkInterfaces", default=[]) or []):
                            pip_addr = props(pr).get("ipAddress", "")
            img = g(p, "storageProfile", "imageReference", default={}) or {}
            self.vm[rid] = {
                "ips": ips, "nic_nsgs": nsgs, "public": public, "pip": pip_addr,
                "size": g(p, "hardwareProfile", "vmSize", default=""),
                "os": g(p, "storageProfile", "osDisk", "osType", default=""),
                "image": " ".join(x for x in (img.get("offer"), img.get("sku")) if x),
                "power": (g(p, "extended", "instanceView", "powerState", "code", default="") or "").split("/")[-1],
            }

    # -- backup ------------------------------------------------------------------------
    def _backup_info(self) -> None:
        self.backup_rows = []
        self.protected: Dict[str, List[dict]] = defaultdict(list)
        for it in self.backups:
            p = it.get("properties") or {}
            vault_name = it["id"].split("/vaults/")[1].split("/")[0] if "/vaults/" in it["id"] else ""
            vault_id = self.by_name.get(lid(vault_name), "")
            src = lid(p.get("sourceResourceId"))
            row = {"item": p.get("friendlyName") or it.get("name"), "kind": p.get("workloadType") or p.get("backupManagementType"),
                   "source": src, "vault": vault_name, "vault_id": vault_id,
                   "policy": p.get("policyName") or "", "status": p.get("lastBackupStatus") or "",
                   "last": (p.get("lastBackupTime") or "")[:16].replace("T", " "),
                   "state": p.get("protectionState") or ""}
            self.backup_rows.append(row)
            if src:
                self.protected[src].append(row)

    # -- ASR ---------------------------------------------------------------------------
    def _asr_info(self) -> None:
        self.asr_rows = []
        self.asr_pairs: Set[Tuple[str, str]] = set()
        for it in self.asr:
            p = it.get("properties") or {}
            psd = p.get("providerSpecificDetails") or {}
            vm_id = lid(psd.get("fabricObjectId"))
            target_net = lid(psd.get("selectedRecoveryAzureNetworkId"))
            src_net = ""
            if vm_id in self.t.placement:
                src_net = self.t.placement[vm_id][0]
            if src_net and target_net:
                self.asr_pairs.add((src_net, target_net))
            ipcs = [ipc for n in psd.get("vmNics") or [] for ipc in n.get("ipConfigs") or []]
            ips = [ipc.get("recoveryStaticIPAddress") or "dynamic IP" for ipc in ipcs]
            rsub = next((ipc.get("recoverySubnetName") for ipc in ipcs if ipc.get("recoverySubnetName")), "")
            self.asr_rows.append({
                "vm": p.get("friendlyName"), "vm_id": vm_id, "vault": it["id"].split("/vaults/")[1].split("/")[0],
                "from": p.get("primaryFabricFriendlyName") or psd.get("primaryFabricLocation"),
                "to": p.get("recoveryFabricFriendlyName") or psd.get("recoveryFabricLocation"),
                "target_rg": name_from_id(psd.get("recoveryAzureResourceGroupId") or ""),
                "target_net": target_net, "rpo": psd.get("rpoInSeconds"), "health": p.get("replicationHealth"),
                "state": p.get("protectionState"), "active": p.get("activeLocation"), "size": psd.get("recoveryAzureVMSize"),
                "ip": ", ".join(ips), "tfo": p.get("testFailoverState") or "", "subnet": rsub,
            })

    # -- workload assignment -------------------------------------------------------------
    def _assign_workloads(self) -> None:
        cfg_w = self.cfg.get("workloads", [])
        ignore_rg = {lid(x) for x in self.cfg.get("ignoreResourceGroups", [])}
        self.workloads = [dict(w, members=[]) for w in cfg_w]
        by_rg, by_res = {}, {}
        for i, w in enumerate(self.workloads):
            for rg in w.get("resourceGroups", []):
                by_rg[lid(rg)] = i
            for n in w.get("resources", []):
                by_res[lid(n)] = i
        self.wl_of: Dict[str, int] = {}
        asr_targets = {tn for _, tn in self.asr_pairs}
        auto: Dict[str, int] = {}
        for rid, r in self.res.items():
            rg = lid(r.get("resourceGroup"))
            if rg in ignore_rg:
                continue
            idx = by_res.get(lid(r.get("name")), by_rg.get(rg))
            if idx is None:
                if r["type"] in HIDE_SOLUTION and r["type"] != "microsoft.network/virtualnetworks":
                    continue
                key = "dr" if rid in asr_targets else rg
                if key not in auto:
                    self.workloads.append({"name": "Disaster recovery (ASR targets)" if key == "dr" else r.get("resourceGroup"),
                                           "row": "dr" if key == "dr" else "primary", "members": [], "auto": True})
                    auto[key] = len(self.workloads) - 1
                idx = auto[key]
            self.wl_of[rid] = idx
            self.workloads[idx]["members"].append(rid)

    # -- findings ------------------------------------------------------------------------
    def _findings(self) -> List[dict]:
        F: List[dict] = []
        t = self.t

        def add(sev, area, finding, rec, resources=()):
            F.append({"sev": sev, "area": area, "finding": finding, "rec": rec, "resources": list(resources)})

        def nm(rid):
            return self.res.get(rid, {}).get("name") or name_from_id(rid)

        # NSG rules open to the Internet
        open_nsg: Dict[str, List[str]] = {}
        for rid, r in self.res.items():
            if r["type"] != "microsoft.network/networksecuritygroups":
                continue
            p = props(r)
            ports = []
            for rule in p.get("securityRules") or []:
                rp = rule.get("properties") or {}
                if rp.get("direction") == "Inbound" and rp.get("access") == "Allow" and lid(rp.get("protocol")) not in ("icmp",) and \
                        (rp.get("sourceAddressPrefix") in ("*", "Internet", "0.0.0.0/0")):
                    pr = rp.get("destinationPortRange") or ",".join(rp.get("destinationPortRanges") or [])
                    ports.append(pr)
            attached = bool(p.get("subnets") or p.get("networkInterfaces"))
            if ports and attached:
                open_nsg[rid] = ports
            if not attached:
                F.append({"sev": "Info", "area": "Hygiene", "finding": f"NSG {r['name']} is not attached to any subnet or NIC.",
                          "rec": "Delete it if no longer needed (leftover from a deleted VM or test).", "resources": [r["name"]], "_group": "unattached_nsg"})

        # VMs
        for vid, v in self.vm.items():
            name = nm(vid)
            mgmt_open = [p for n in v["nic_nsgs"] for p in open_nsg.get(n, []) if any(x in p for x in ("3389", "22", "*"))]
            sub_nsg = ""
            if vid in t.placement and t.placement[vid][1]:
                sub_nsg = t.subnets[t.placement[vid][1]]["nsg"]
            mgmt_open += [p for p in open_nsg.get(sub_nsg, []) if any(x in p for x in ("3389", "22", "*"))]
            if v["public"]:
                add("High" if mgmt_open else "Medium", "Exposure",
                    f"VM {name} has a public IP ({v['pip'] or 'assigned'})" + (f" and NSG allows {', '.join(sorted(set(mgmt_open)))} from the Internet." if mgmt_open else "."),
                    "Remove the public IP and reach the VM over the P2S VPN or Azure Bastion; if it must stay, restrict the NSG source to known IPs.", [name])
            elif mgmt_open:
                add("Low", "Exposure", f"VM {name}: NSG allows management port(s) {', '.join(sorted(set(mgmt_open)))} from any source. No public IP today, so not reachable from the Internet yet.",
                    "Change the source to the VPN client pool / internal ranges so a future public IP cannot expose it.", [name])
            if v["power"] == "running" and vid not in self.protected and vid not in {a["vm_id"] for a in self.asr_rows}:
                add("Medium", "Resilience", f"Running VM {name} is not protected by Azure Backup or ASR.",
                    "Add it to a backup policy, or document why it is disposable (e.g. rebuilt from code).", [name])
            if v["power"] == "deallocated":
                F.append({"sev": "Info", "area": "Cost", "finding": f"VM {name} is deallocated (no compute cost, disks still billed).",
                          "rec": "Confirm whether it is still needed.", "resources": [name], "_group": "dealloc"})

        # Address space
        nets = {}
        for vid, v in t.vnets.items():
            nets[vid] = [ipaddress.ip_network(x, strict=False) for x in g(props(v), "addressSpace", "addressPrefixes", default=[]) or []]
            for n in nets[vid]:
                if not any(n.subnet_of(r) for r in RFC1918 if n.version == r.version):
                    users = [d for vv in t.vnets.values() for d in g(props(vv), "dhcpOptions", "dnsServers", default=[]) or []
                             if ipaddress.ip_address(d) in n]
                    add("High", "Network", f"VNet {v['name']} uses {n}, which is public (non-RFC1918) address space registered to someone else"
                        + (f"; {', '.join(sorted(set(users)))} in this range is used as a DNS server by other VNets." if users else "."),
                        "Plan a migration to a private range (e.g. 10.x). Until then, any real public service in that range is unreachable from these VNets.", [v["name"]])
        vids = sorted(nets)
        overl: Dict[str, Set[str]] = defaultdict(set)
        for i, a in enumerate(vids):
            for b in vids[i + 1:]:
                if (a, b) in self.asr_pairs or (b, a) in self.asr_pairs:
                    continue
                if any(na.version == nb.version and na.overlaps(nb) for na in nets[a] for nb in nets[b]):
                    overl[a].add(b)
                    overl[b].add(a)
        done: Set[str] = set()
        for a in sorted(overl, key=lambda x: -len(overl[x])):
            rest = overl[a] - done
            if not rest or a in done:
                continue
            done.add(a)
            names = ", ".join(sorted(f"{t.vnets[b]['name']} ({', '.join(map(str, nets[b]))})" for b in rest))
            add("Medium", "Network", f"{t.vnets[a]['name']} ({', '.join(map(str, nets[a]))}) overlaps: {names}.",
                "Overlapping VNets can never be peered or routed together. Re-IP one side before you need to connect them.",
                [t.vnets[a]["name"]] + [t.vnets[b]["name"] for b in rest])

        # Peerings
        stale: Dict[str, List[str]] = defaultdict(list)
        for a, b, state in t.peerings:
            if b not in t.vnets or state != "Connected":
                stale[name_from_id(b) + ("|gone" if b not in t.all_res else f"|{state}")].append(t.vnets[a]["name"])
        for key, srcs in stale.items():
            remote, why = key.split("|")
            add("Low", "Network", f"Stale peerings to {remote} from {', '.join(sorted(srcs))}: "
                + ("remote VNet not found in this export (likely deleted)." if why == "gone" else f"state {why}."),
                "Delete the stale peerings so the topology reflects reality.", sorted(srcs))

        # DNS
        for vid, v in t.vnets.items():
            dns = g(props(v), "dhcpOptions", "dnsServers", default=[]) or []
            own = [n for ns in nets.values() for n in ns]
            pub = [d for d in dns if d != "168.63.129.16" and not ipaddress.ip_address(d).is_private
                   and not any(ipaddress.ip_address(d) in n for n in own)]
            if pub:
                add("Medium", "Identity/DNS", f"VNet {v['name']} lists public DNS server(s) {', '.join(pub)} next to the domain controllers.",
                    "Domain-joined machines may resolve AD names inconsistently, and DNS queries leave the network. Use only the DCs (and 168.63.129.16 via DC forwarders).", [v["name"]])

        # Storage
        for rid, r in self.res.items():
            if r["type"] != "microsoft.storage/storageaccounts":
                continue
            p = props(r)
            if p.get("publicNetworkAccess") != "Disabled" and g(p, "networkAcls", "defaultAction", default="Allow") == "Allow":
                cache = "asrcache" in lid(r["name"])
                has_pe = bool(p.get("privateEndpointConnections"))
                add("Info" if cache else "Medium", "Data", f"Storage account {r['name']} accepts connections from any network"
                    + (" even though it also has a private endpoint." if has_pe else "."),
                    "ASR-managed cache account; acceptable." if cache else
                    "Set public network access to Disabled (or Selected networks) so data is reachable only via the private endpoint / VPN.", [r["name"]])

        # AKS
        for rid, r in self.res.items():
            if r["type"] != "microsoft.containerservice/managedclusters":
                continue
            p = props(r)
            if not g(p, "apiServerAccessProfile", "enablePrivateCluster", default=False):
                ranges = g(p, "apiServerAccessProfile", "authorizedIPRanges", default=[]) or []
                add("Low" if ranges else "Medium", "Exposure", f"AKS {r['name']} API server is public" + (" (restricted to authorised IP ranges)." if ranges else " with no authorised IP ranges."),
                    "Restrict API access with authorised IP ranges or make the cluster private.", [r["name"]])
        for rid, ports in open_nsg.items():
            if "aks-agentpool" in lid(self.res[rid]["name"]):
                aks = [r for r in self.res.values() if r["type"] == "microsoft.containerservice/managedclusters"]
                mitig = next((c for c in self.cfg.get("declaredControls", [])
                              if any(lid(a["name"]) in {lid(x) for x in c.get("appliesTo", [])} for a in aks)), None)
                add("Info", "Exposure", f"AKS ingress is published to the Internet via the public load balancer (ports {', '.join(sorted(set(ports)))})."
                    + (f" Declared mitigation: {mitig['name']} – {mitig['summary']}" if mitig else ""),
                    "Keep the declared control enforced and reviewed; optionally also restrict loadBalancerSourceRanges."
                    if mitig else "Confirm this is intended and that ingress is limited to approved client IPs (loadBalancerSourceRanges).",
                    [self.res[rid]["name"]])

        # Private DNS duplicates
        zones = Counter(lid(r["name"]) for r in self.res.values() if r["type"] == "microsoft.network/privatednszones")
        for z, n in zones.items():
            if n > 1:
                add("Low", "Identity/DNS", f"Private DNS zone {z} exists {n} times in different resource groups, each linked to a different VNet.",
                    "Keep one zone per name, linked to all VNets (or forward from the DCs) so every client resolves private endpoints the same way.", [z])

        # Backup redundancy
        for rid, r in self.res.items():
            if r["type"] == "microsoft.recoveryservices/vaults":
                red = g(props(r), "redundancySettings", "standardTierStorageRedundancy", default="")
                items = [b for b in self.backup_rows if lid(b["vault"]) == lid(r["name"])]
                if red == "LocallyRedundant" and items:
                    F.append({"sev": "Low", "area": "Resilience", "finding": f"Backup vault {r['name']} is locally redundant (single region).",
                              "rec": "Consider geo-redundant storage with cross-region restore for production data.", "resources": [r["name"]], "_group": "lrs"})
        for b in self.backup_rows:
            if b["status"] and b["status"] != "Completed":
                add("High", "Resilience", f"Last backup of {b['item']} in {b['vault']} ended with status {b['status']}.", "Investigate the backup job.", [b["item"]])

        # AI / Cognitive public
        for rid, r in self.res.items():
            if r["type"] == "microsoft.cognitiveservices/accounts" and props(r).get("publicNetworkAccess") == "Enabled":
                F.append({"sev": "Low", "area": "Exposure", "finding": f"AI Services account {r['name']} allows public network access.",
                          "rec": "If only used from Azure workloads, restrict to selected networks or a private endpoint.", "resources": [r["name"]], "_group": "ai"})

        # Bastion subnets without Bastion
        bastions = {t.placement.get(rid, (None, None))[1] for rid, r in self.res.items() if r["type"] == "microsoft.network/bastionhosts"}
        for sid, sn in t.subnets.items():
            if sn["name"].lower() == "azurebastionsubnet" and sid not in bastions:
                F.append({"sev": "Info", "area": "Hygiene", "finding": f"{t.vnets[sn['vnet']]['name']} has an AzureBastionSubnet but no Bastion host.",
                          "rec": "Either deploy Bastion (and remove VM public IPs) or delete the empty subnet.", "resources": [t.vnets[sn["vnet"]]["name"]], "_group": "bastion"})

        # Unattached public IPs
        for rid, r in self.res.items():
            if r["type"] == "microsoft.network/publicipaddresses" and not props(r).get("ipConfiguration"):
                F.append({"sev": "Info", "area": "Cost", "finding": f"Public IP {r['name']} is not attached to anything.",
                          "rec": "Delete it unless it is reserved for a DR failover.", "resources": [r["name"]], "_group": "pip"})

        # merge grouped informational items
        merged, groups = [], defaultdict(list)
        for f in F:
            (groups[f["_group"]] if f.get("_group") else merged).append(f)
        titles = {"unattached_nsg": "NSGs not attached to anything", "dealloc": "Deallocated VMs",
                  "lrs": "Backup vaults that are locally redundant (single region)", "ai": "AI Services accounts with public network access",
                  "bastion": "AzureBastionSubnet present but no Bastion host", "pip": "Public IPs not attached to anything"}
        for k, items in groups.items():
            names = sorted({n for f in items for n in f["resources"]})
            merged.append({"sev": items[0]["sev"], "area": items[0]["area"], "finding": f"{titles[k]}: {', '.join(names)}.",
                           "rec": items[0]["rec"], "resources": names})
        merged.sort(key=lambda f: (SEV_ORDER[f["sev"]], f["area"], f["finding"]))
        return merged

    # -- small helpers used by both outputs --------------------------------------------------
    def vm_line(self, rid: str) -> str:
        v = self.vm.get(rid, {})
        bits = [", ".join(v.get("ips", []))]
        if v.get("power") and v["power"] != "running":
            bits.append(v["power"])
        return " · ".join(b for b in bits if b)

    def vpn(self) -> List[dict]:
        out = []
        for rid, r in self.res.items():
            if r["type"] == "microsoft.network/virtualnetworkgateways":
                p = props(r)
                vc = p.get("vpnClientConfiguration") or {}
                out.append({"id": rid, "name": r["name"], "region": region_name(r.get("location")),
                            "type": p.get("gatewayType"), "sku": g(p, "sku", "name", default=""),
                            "pool": ", ".join(g(vc, "vpnClientAddressPool", "addressPrefixes", default=[]) or []),
                            "auth": ", ".join({"AAD": "Microsoft Entra ID", "Certificate": "certificate", "Radius": "RADIUS"}.get(a, a)
                                  for a in vc.get("vpnAuthenticationTypes") or []),
                            "protocols": ", ".join(vc.get("vpnClientProtocols") or []),
                            "clients": g(vc, "vpnClientConnectionHealth", "vpnClientConnectionsCount"),
                            "vnet": self.t.placement.get(rid, ("", None))[0]})
        return out


# =====================================================================================
# Solution-architecture page
# =====================================================================================
class SolutionPage:
    def __init__(self, f: Facts, builder: DiagramBuilder, page: Page, cfg: dict):
        self.f, self.b, self.page, self.cfg = f, builder, page, cfg
        self.t = f.t
        self.cell: Dict[str, str] = {}
        self.leaf_of: Dict[str, Leaf] = {}
        self.vnet_box: Dict[str, Box] = {}
        self.wl_box_of_vnet: Dict[str, Box] = {}
        self.wl_boxes: List[Tuple[dict, Box]] = []
        self.notes = {lid(k): v for k, v in cfg.get("labels", {}).items()}
        self.tags: Dict[str, List[Tuple[str, str]]] = defaultdict(list)   # rid -> [(colour, text)]
        self._build_tags()

    def _build_tags(self) -> None:
        """Relationships shown as small coloured tags on icons instead of crossing lines."""
        f, t, tag = self.f, self.t, self.tags
        nm = lambda rid: f.res.get(rid, {}).get("name") or name_from_id(rid)
        for pe, target, gid in t.private_links:
            tag[pe].append((EDGE["privatelink"][0], f"→ {nm(target)} ({gid})"))
            tag[target].append((EDGE["privatelink"][0], f"via private endpoint {nm(pe)}"))
        for b in f.backup_rows:
            if b["source"] in f.res:
                tag[b["source"]].append((EDGE["backup"][0], f"Backup → {b['vault']}"))
        for a in f.asr_rows:
            rpo = f", RPO {round(a['rpo'] / 60, 1)} min" if a.get("rpo") else ""
            tag[a["vm_id"]].append((EDGE["asr"][0], f"ASR → {a['to']}{rpo}"))
        for sh in f.session_hosts:
            vm = lid(g(sh, "properties", "resourceId", default=""))
            hp = lid(sh["id"]).split("/sessionhosts/")[0]
            if vm in f.res:
                tag[vm].append((EDGE["avd"][0], f"session host of {nm(hp)} · {g(sh, 'properties', 'sessions', default=0)} sessions"))
            if hp in f.res:
                st = g(sh, "properties", "status", default="")
                tag[hp].append((EDGE["avd"][0], f"host: {name_from_id(vm)} ({st})"))
        for link in self.cfg.get("links", []):
            a, b = f.by_name.get(lid(link["from"])), f.by_name.get(lid(link["to"]))
            if a and b:
                tag[a].append((EDGE["custom"][0], f"↔ {nm(b)}: {link.get('label', '')}"))
                tag[b].append((EDGE["custom"][0], f"↔ {nm(a)}: {link.get('label', '')}"))
        for vid, v in f.vm.items():
            if v["public"]:
                tag[vid].append((EDGE["exposure"][0], f"⚠ public IP {v['pip']}"))
        for rid, r in f.res.items():
            if r["type"] == "microsoft.containerservice/managedclusters":
                tag[rid].append((EDGE["exposure"][0], "⚠ Internet ingress via AKS public LB"))
        for c in self.cfg.get("declaredControls", []):
            for name in c.get("appliesTo", []):
                rid = f.by_name.get(lid(name))
                if rid:
                    tag[rid].append(("#7C3AED", f"🔒 {c.get('name')}: {c.get('summary')}"))
        for v in f.vpn():
            if v["pool"]:
                tag[v["id"]].append((C["azure"], f"P2S {v['protocols']} · {v['auth']} · pool {v['pool']}"
                                     + (f" · {v['clients']} clients" if v["clients"] is not None else "")))

    # leaves ---------------------------------------------------------------------------------
    def leaf(self, rid: str) -> Leaf:
        r = self.f.res[rid]
        lf = self.b.leaf(rid)
        extra = []
        if r["type"] == "microsoft.compute/virtualmachines":
            v = self.f.vm[rid]
            extra.append(f"<b>{esc(', '.join(v['ips']))}</b>")
            if v["power"] and v["power"] != "running":
                extra.append(f"<font color='#9CA3AF'>{esc(v['power'])}</font>")
        elif r["type"] == "microsoft.desktopvirtualization/hostpools":
            p = props(r)
            extra.append(esc(f"{p.get('hostPoolType', '')} · {p.get('loadBalancerType', '')} · max {p.get('maxSessionLimit', '?')}/host"))
        elif r["type"] == "microsoft.automation/automationaccounts":
            n = sum(1 for x in self.f.res.values() if x["type"].endswith("/runbooks") and lid(x["id"]).startswith(rid + "/"))
            if n:
                extra.append(f"{n} runbooks")
        elif r["type"] == "microsoft.dbforpostgresql/flexibleservers":
            p = props(r)
            extra.append(esc(f"PostgreSQL {p.get('version', '')} · public access {g(p, 'network', 'publicNetworkAccess', default='?')}"))
        elif r["type"] == "microsoft.storage/storageaccounts":
            p = props(r)
            access = "private only" if p.get("publicNetworkAccess") == "Disabled" else "public endpoint on"
            extra.append(esc(f"{r.get('kind', '')} · {g(r, 'sku', 'name', default='')} · {access}"))
        elif r["type"] == "microsoft.recoveryservices/vaults":
            red = g(props(r), "redundancySettings", "standardTierStorageRedundancy", default="")
            n = sum(1 for b in self.f.backup_rows if lid(b["vault"]) == lid(r["name"]))
            n += sum(1 for a in self.f.asr_rows if lid(a["vault"]) == lid(r["name"]))
            extra.append(esc(f"{red.replace('Redundant', '-redundant')} · {n} protected item(s)"))
        elif r["type"] == "microsoft.app/managedenvironments":
            p = props(r)
            extra.append(esc(("internal" if g(p, "vnetConfiguration", "internal") else "external") + f" · ILB {p.get('staticIp', '')}"))
        elif r["type"] == "microsoft.app/containerapps":
            doms = [d.get("name") for d in g(props(r), "configuration", "ingress", "customDomains", default=[]) or []]
            if doms:
                extra.append(esc(", ".join(doms)))
        note = self.notes.get(lid(r.get("name")))
        if note:
            extra.append(f"<font color='#7C3AED'><i>{esc(note)}</i></font>")
        for colour, text in self.tags.get(rid, []):
            extra.append(f"<font color='{colour}'>{esc(text)}</font>")
        if extra:
            lf.label += "<br><font style='font-size:9px'>" + "<br>".join(extra) + "</font>"
            plain = [re.sub(r"<[^>]+>", "", x) for x in extra]
            lf.h = 110 + sum(13 * (1 + len(x) // 22) for x in plain)
        return lf

    def leaves(self, rids: List[str], threshold: int = 3) -> List[Leaf]:
        by_type: Dict[str, List[str]] = defaultdict(list)
        for rid in rids:
            by_type[self.f.res[rid]["type"]].append(rid)
        out = []
        order = ["microsoft.desktopvirtualization/workspaces", "microsoft.desktopvirtualization/applicationgroups",
                 "microsoft.desktopvirtualization/hostpools", "microsoft.compute/virtualmachines"]
        for typ in sorted(by_type, key=lambda x: (order.index(x) if x in order else 99, TYPE_INFO.get(x, ("", x))[1].lower())):
            group = sorted(by_type[typ], key=lambda x: lid(self.f.res[x].get("name")))
            if len(group) > threshold:
                lfs, owners = self.b.leaves_collapsed(group, threshold)
                out += lfs
                self.leaf_of.update(owners)
            else:
                for rid in group:
                    lf = self.leaf(rid)
                    self.leaf_of[rid] = lf
                    out.append(lf)
        return out

    # boxes ------------------------------------------------------------------------------------
    def vnet(self, vid: str, workload_members: Set[str]) -> Box:
        t, v = self.t, self.t.vnets[vid]
        p = props(v)
        in_sub: Dict[Optional[str], List[str]] = defaultdict(list)
        for rid, (vn, sid) in t.placement.items():
            if vn == vid and self.f.res[rid]["type"] not in HIDE_SOLUTION and not lid(self.f.res[rid].get("resourceGroup")).startswith("mc_"):
                in_sub[sid].append(rid)
        children = self.leaves(in_sub.get(None, []))
        replicas: Dict[str, List[Leaf]] = defaultdict(list)
        for a in self.f.asr_rows:
            if a["target_net"] == vid:
                sid_r = f"{vid}/subnets/{lid(a['subnet'])}" if a["subnet"] else None
                rep = Leaf(label=f"<i>{esc(breakable(a['vm']))} (replica)</i><br><font style='font-size:9px' color='{EDGE['asr'][0]}'>"
                                 f"{esc(a['ip'])}<br>ASR from {esc(a['from'])}</font>",
                           icon="compute/Virtual_Machine.svg", extra_style="opacity=45;textOpacity=100;", h=132,
                           tooltip=f"ASR replica of {a['vm']}\nFrom {a['from']} to {a['to']}\nVault {a['vault']}\n"
                                   f"Target RG {a['target_rg']}\nRPO {a['rpo']} s · health {a['health']}")
                replicas[sid_r if sid_r in t.subnets else None].append(rep)
        children = replicas.get(None, []) + children
        for sid in t.subnet_order[vid]:
            sn = t.subnets[sid]
            special = sn["name"].lower() in SPECIAL_SUBNETS
            dl = ", ".join(d.split("/")[-1] for d in sn["delegations"] if d)
            flags = " · ".join(x for x in ("NSG" if sn["nsg"] else "", f"→ {dl}" if dl else "") if x)
            items = replicas.get(sid, []) + self.leaves(in_sub.get(sid, []))
            children.append(Box(
                label=f"<b>{esc(sn['name'])}</b> <font color='{C['text2']}'>{esc(sn['prefix'])}</font>"
                      + (f"<br><font style='font-size:9px' color='{C['text2']}'>{esc(flags)}</font>" if flags else ""),
                icon="networking/Subnet.svg", children=items, cols=3, header=40 if flags else 34,
                style=container_style(C["special_fill"] if special else "#FFFFFF", C["special_stroke"] if special else C["sub_stroke"],
                                      dashed=True, font_size=10, stroke_w=1), min_w=200 if items else 190,
                attrs={"azureId": sid}))
        cidr = ", ".join(g(p, "addressSpace", "addressPrefixes", default=[]) or [])
        dns = ", ".join(g(p, "dhcpOptions", "dnsServers", default=[]) or []) or "Azure DNS"
        role = "HUB" if vid in self.f.vpn_vnets else ("DR" if vid in {tn for _, tn in self.f.asr_pairs} else "")
        uses_gw, peers = False, []
        for pp in p.get("virtualNetworkPeerings") or []:
            pr = pp.get("properties") or {}
            rv = lid(g(pr, "remoteVirtualNetwork", "id"))
            if pr.get("useRemoteGateways"):
                uses_gw = True
            elif rv in t.vnets and pr.get("peeringState") == "Connected" and rv not in self.f.vpn_vnets and vid not in self.f.vpn_vnets:
                peers.append(t.vnets[rv]["name"])
        conn = []
        if uses_gw:
            conn.append("uses VPN gateway (transit)")
        if peers:
            conn.append("↔ peered: " + ", ".join(sorted(peers)))
        if vid in self.f.vpn_vnets:
            spokes = [t.vnets[b]["name"] for a, b, st in t.peerings if a == vid and b in t.vnets and st == "Connected"]
            conn.append("gateway transit to: " + ", ".join(sorted(spokes)))
        conn_html = f"<br><font style='font-size:9px' color='{C['azure']}'>{esc(' · '.join(conn))}</font>" if conn else ""
        box = Box(label=f"<b>{(role + ': ') if role else ''}{esc(v['name'])}</b> <font color='{C['azure']}'>{esc(cidr)}</font><br>"
                        f"<font style='font-size:9px' color='{C['text2']}'>{esc(region_name(v.get('location')))} · DNS {esc(dns)}</font>{conn_html}",
                  icon="networking/Virtual_Networks.svg", children=children, max_inner=760, header=58 if conn else 44,
                  min_w=int(len(" · ".join(conn)) * 5.4) + 70 if conn else 0,
                  style=container_style(C["hub_fill"] if role == "HUB" else "#F5F9FD", C["hub_stroke"] if role == "HUB" else C["spoke_stroke"],
                                        stroke_w=2 if role == "HUB" else 1.4, font_size=11),
                  tooltip=tooltip_for(v, t.sub_names), attrs={"azureId": v.get("id")})
        self.vnet_box[vid] = box
        return box

    def workload(self, w: dict) -> Optional[Box]:
        members = set(w["members"])
        vnets = sorted([m for m in members if m in self.t.vnets], key=lambda x: lid(self.t.vnets[x]["name"]))
        children: List = [self.vnet(v, members) for v in vnets]
        self._pending_vnets = vnets
        others = [m for m in members if m not in self.t.vnets and m not in self.t.placement
                  and self.f.res[m]["type"] not in HIDE_SOLUTION and not lid(self.f.res[m].get("resourceGroup")).startswith("mc_")]
        if others:
            children.append(Box(label="<b>Services</b>", icon="general/All_Resources.svg", children=self.leaves(others),
                                cols=4 if len(others) > 4 else max(2, len(others)), header=34,
                                style=container_style("#FFFFFF", "#D1D5DB", font_size=10, stroke_w=1)))
        if not children:
            return None
        regions = sorted({region_name(self.f.res[m].get("location")) for m in members if self.f.res[m].get("location") not in (None, "global")})
        subs = sorted({self.t.sub_names.get(lid(self.f.res[m].get("subscriptionId")), "") for m in members})
        fill, stroke = w.get("fill", "#F0FDF4"), w.get("stroke", "#16A34A")
        if w.get("row") == "dr":
            fill, stroke = w.get("fill", "#FFF1F2"), w.get("stroke", "#E11D48")
        desc = w.get("summary", "")
        box = Box(label=f"<b style='font-size:14px'>{esc(w['name'])}</b><br><font style='font-size:10px' color='{C['text2']}'>"
                         f"{esc(' · '.join(regions))} · {esc(', '.join(subs))}</font>"
                         + (f"<br><font style='font-size:10px'>{esc(desc)}</font>" if desc else ""),
                   icon=w.get("icon"), children=children, max_inner=w.get("width", 1100),
                   header=66 if desc else 50, style=container_style(fill, stroke, stroke_w=2, font_size=12))
        for v in vnets:
            self.wl_box_of_vnet[v] = box
        self.wl_boxes.append((w, box))
        return box

    # page -------------------------------------------------------------------------------------
    def build(self) -> None:
        f, t, page = self.f, self.t, self.page
        f.vpn_vnets = {v["vnet"] for v in f.vpn()}
        title = self.cfg.get("title", "Azure solution architecture")
        page.text(f"<b style='font-size:22px'>{esc(title)}</b><br><font color='{C['text2']}'>Generated from Azure Resource Graph · "
                  f"export {esc(f.exported[:16].replace('T', ' '))} UTC · {len(t.subs)} subscriptions · {len(f.res)} resources</font>",
                  0, -90, 1600, 70, size=12)

        rows: Dict[str, List[Box]] = defaultdict(list)
        for w in f.workloads:
            box = self.workload(w)
            if box:
                rows[w.get("row", "primary")].append(box)

        # access row: users + internet + access workloads
        self.users = Leaf(label="<b>Remote users</b><br><font style='font-size:9px' color='#6B7280'>Azure VPN client / AVD client</font>",
                          icon="identity/Users.svg")
        access = Box(label="<b style='font-size:14px'>Access &amp; connectivity</b>", icon="networking/Virtual_Network_Gateways.svg",
                     children=[self.users] + rows.get("access", []), max_inner=2600, header=46, gap=90,
                     style=container_style("#F8FAFC", "#64748B", stroke_w=2, font_size=12))
        bands = [access]
        titles = {"primary": "Production & business workloads", "shared": "Shared data, backup & operations",
                  "dr": "Disaster recovery (Azure Site Recovery targets)"}
        for key in ("primary", "shared", "dr"):
            if rows.get(key):
                bands.append(Box(label=f"<b style='font-size:15px'>{titles[key]}</b>", children=rows[key], max_inner=self.cfg.get("pageWidth", 2900),
                                 header=40, gap=60, pad=40, style=container_style("none", "#CBD5E1", dashed=True, font_size=13)))
        y, width = 0, 0
        for bnd in bands:
            bnd.layout()
            bnd.y = y
            page.emit(bnd)
            y += bnd.h + 110
            width = max(width, bnd.w)
        self.access_band = access

        for rid, lf in self.leaf_of.items():
            self.cell[rid] = lf.cell_id
        for vid, bx in self.vnet_box.items():
            self.cell[vid] = bx.cell_id
        self._edges()
        self._side_panels(width + 60)

    def _edges(self) -> None:
        """Only the main traffic paths are drawn, routed through the gaps between boxes.
        Everything else (backup, private link, ASR, AVD hosts, peerings) is shown as tags."""
        f, page = self.f, self.page
        acc = self.access_band
        bus_transit = acc.ay + acc.h + 40          # horizontal bus lines in the gap under the access band
        bus_users = acc.ay + acc.h + 72
        hub_boxes = [self.vnet_box[v] for v in f.vpn_vnets if v in self.vnet_box]
        if hub_boxes:
            hub = hub_boxes[0]
            hx = hub.ax + hub.w / 2
            # users -> hub (same row, straight line through the gap)
            uy = self.users.ay + 4 + 24
            page.routed_edge(self.users.cell_id, hub.cell_id, EDGE["users"][0], [], "P2S VPN", 2.5,
                             f"exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY={max(0.05, min(0.95, (uy - hub.ay) / hub.h)):.3f};entryDx=0;entryDy=0;")
            # hub -> every spoke that uses the gateway, via a shared bus and the gap left of each workload
            for vid, box in self.vnet_box.items():
                uses = any(g(pp, "properties", "useRemoteGateways") for pp in props(self.t.vnets[vid]).get("virtualNetworkPeerings") or [])
                wl = self.wl_box_of_vnet.get(vid)
                if not uses or wl is None or vid in f.vpn_vnets:
                    continue
                ch_x = wl.ax - 34
                lane_y = box.ay - 9
                pts = [(hx, bus_transit), (ch_x, bus_transit), (ch_x, lane_y), (box.ax + 60, lane_y)]
                page.routed_edge(hub.cell_id, box.cell_id, EDGE["transit"][0], pts, "", 2,
                                 "exitX=0.5;exitY=1;exitDx=0;exitDy=0;entryX=0;entryY=0;entryDx=60;entryDy=0;")
            page.text(f"<font color='{EDGE['transit'][0]}'><b>VPN gateway transit</b> → spokes</font>", hx + 8, bus_transit - 22, 220, 20, size=10)
        # users -> AVD workspace workload (own bus, slightly lower)
        for w, box in self.wl_boxes:
            if w.get("row") == "dr":
                continue
            if any(f.res[m]["type"] == "microsoft.desktopvirtualization/workspaces" for m in w["members"]):
                ux = self.users.ax + CELL_W / 2
                ch_x = box.ax - 18
                pts = [(ux, bus_users), (ch_x, bus_users), (ch_x, box.ay + 24)]
                page.routed_edge(self.users.cell_id, box.cell_id, EDGE["avd"][0], pts, "", 2,
                                 "exitX=0.5;exitY=1;exitDx=0;exitDy=0;entryX=0;entryY=0;entryDx=0;entryDy=24;")
                page.text(f"<font color='{EDGE['avd'][0]}'><b>AVD</b> (reverse connect, 443)</font>", ux + 8, bus_users + 2, 220, 20, size=10)

    def _side_panels(self, x: float) -> None:
        page, f = self.page, self.f
        story = self.cfg.get("story", [])
        if story:
            body = "".join(f"<p style='margin:0 0 6px 0'><b>{i}.</b> {esc(s)}</p>" for i, s in enumerate(story, 1))
            page._vertex(f"<b style='font-size:15px'>How it works</b><br><br>{body}",
                         f"rounded=1;arcSize=4;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=12;"
                         f"fillColor=#FFFBEB;strokeColor=#F59E0B;fontFamily={FONT};fontSize=11;", x, 0, 520, 60 + 46 * len(story), "1")
        y = 80 + 46 * len(story)
        rows = [("━━━", "users", "Remote users → VPN (P2S)"), ("━━━", "transit", "VPN gateway transit to spokes"),
                ("━━━", "avd", "Users → Azure Virtual Desktop / AVD tags"), ("text", "privatelink", "Private Link tags (→ target / via PE)"),
                ("text", "backup", "Backup → vault"), ("text", "asr", "ASR replication; faded VM = DR replica"),
                ("text", "custom", "↔ documented relationship"), ("text", "exposure", "⚠ Internet exposure")]
        body = "".join((f"<font color='{EDGE[k][0]}'>━━━</font> " if kind == "━━━" else f"<font color='{EDGE[k][0]}'><b>Aa</b></font> ")
                       + f"{esc(lbl)}<br>" for kind, k, lbl in rows)
        page._vertex(f"<b style='font-size:13px'>Legend</b><br>{body}<i>Lines = traffic paths · coloured text under icons = relationships · hover for details</i>",
                     f"rounded=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=10;fillColor=#FFFFFF;strokeColor=#CBD5E1;"
                     f"fontFamily={FONT};fontSize=11;", x, y, 520, 230, "1")
        y += 250
        top = [fd for fd in f.findings if fd["sev"] in ("High", "Medium")][:12]
        if top:
            body = "".join(f"<p style='margin:0 0 5px 0'><font color='{SEV_COLOR[fd['sev']]}'><b>{fd['sev']}</b></font> · "
                           f"{esc(short(fd['finding'], 170))}</p>" for fd in top)
            page._vertex(f"<b style='font-size:15px'>Review points</b> <font color='#6B7280'>(full list in the architecture document)</font><br><br>{body}",
                         f"rounded=1;arcSize=4;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=12;"
                         f"fillColor=#FEF2F2;strokeColor=#FCA5A5;fontFamily={FONT};fontSize=11;", x, y, 520, 70 + 40 * len(top), "1")


# =====================================================================================
# Architecture document (markdown)
# =====================================================================================
def md_table(headers: List[str], rows: List[List]) -> str:
    def cell(v):
        return str(v if v not in (None, "") else "–").replace("|", "\\|").replace("\n", " ")
    out = "| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|\n"
    for r in rows:
        out += "| " + " | ".join(cell(c) for c in r) + " |\n"
    return out + "\n"


def write_document(f: Facts, cfg: dict, path: Path) -> None:
    t = f.t
    L: List[str] = []
    w = L.append
    w(f"# {cfg.get('title', 'Azure solution architecture')}\n\n")
    w(f"*Generated automatically from Azure Resource Graph. Export taken {f.exported[:16].replace('T', ' ')} UTC. "
      f"Values such as VPN client count, RPO and session counts are a snapshot at export time.*\n\n")
    regions = Counter(region_name(r.get("location")) for r in f.res.values() if r.get("location") not in (None, "global"))
    w("## 1. Summary\n\n")
    if cfg.get("summary"):
        w(cfg["summary"] + "\n\n")
    w(md_table(["Measure", "Value"], [
        ["Subscriptions", ", ".join(t.sub_names[s] for s in sorted(t.subs, key=lambda s: t.sub_names[s]))],
        ["Resource groups / resources", f"{len(t.rgs)} / {len(f.res)}"],
        ["Regions in use", ", ".join(f"{k} ({n})" for k, n in regions.most_common())],
        ["Virtual networks", len(t.vnets)],
        ["Virtual machines", f"{len(f.vm)} ({sum(1 for v in f.vm.values() if v['power'] == 'running')} running)"],
        ["Items protected by Azure Backup", len(f.backup_rows)],
        ["VMs replicated by Azure Site Recovery", len(f.asr_rows)],
        ["Review points (High / Medium)", f"{sum(1 for x in f.findings if x['sev'] == 'High')} / {sum(1 for x in f.findings if x['sev'] == 'Medium')}"],
    ]))
    if cfg.get("story"):
        w("### How the platform works end-to-end\n\n")
        for i, s in enumerate(cfg["story"], 1):
            w(f"{i}. {s}\n")
        w("\n")

    w("## 2. Subscriptions and resource organisation\n\n")
    rows = []
    for sid in sorted(t.subs, key=lambda s: t.sub_names[s]):
        rgs = [rg for rg in t.rgs if lid(rg.get("subscriptionId")) == sid]
        n = sum(1 for r in f.res.values() if lid(r.get("subscriptionId")) == sid)
        mg = " > ".join(reversed([m.get("displayName") or m.get("name") for m in t.subs[sid].get("managementGroups") or []]))
        rows.append([t.sub_names[sid], sid, mg, len(rgs), n])
    w(md_table(["Subscription", "ID", "Management group", "Resource groups", "Resources"], rows))
    w("### Workloads and their resource groups\n\n")
    rows = []
    for wl in f.workloads:
        rgs = sorted({f.res[m].get("resourceGroup") for m in wl["members"]})
        if rgs:
            rows.append([wl["name"], wl.get("row", "primary"), ", ".join(rgs), len(wl["members"])])
    w(md_table(["Workload", "Tier", "Resource groups", "Resources"], rows))

    w("## 3. Network and connectivity\n\n")
    vpn = f.vpn()
    if vpn:
        w("### Remote access\n\n")
        for v in vpn:
            w(f"- **{v['name']}** ({v['region']}, {v['type']} gateway, SKU {v['sku']}): point-to-site VPN using {v['protocols'] or '–'} "
              f"with {v['auth'] or '–'} authentication. Clients receive addresses from **{v['pool'] or '–'}**. "
              f"{v['clients'] if v['clients'] is not None else 'Unknown'} client(s) connected at export time.\n")
        sts = [r for r in f.res.values() if r["type"] == "microsoft.network/connections"]
        w(f"- Site-to-site / ExpressRoute connections: {len(sts) or 'none'}.\n\n")
    w("### Address plan\n\n")
    rows = []
    for vid in sorted(t.vnets, key=lambda v: (region_name(t.vnets[v].get("location")), lid(t.vnets[v]["name"]))):
        v = t.vnets[vid]
        p = props(v)
        role = "Connectivity hub (VPN)" if vid in f.vpn_vnets else ("DR (ASR target)" if vid in {x for _, x in f.asr_pairs} else "Spoke / workload")
        subs = "; ".join(f"{t.subnets[s]['name']} {t.subnets[s]['prefix']}" for s in t.subnet_order[vid])
        rows.append([v["name"], region_name(v.get("location")), ", ".join(g(p, "addressSpace", "addressPrefixes", default=[]) or []),
                     role, subs, ", ".join(g(p, "dhcpOptions", "dnsServers", default=[]) or []) or "Azure default"])
    w(md_table(["VNet", "Region", "Address space", "Role", "Subnets", "DNS servers"], rows))
    w("### Peerings\n\n")
    seen, rows = set(), []
    for a, b, state in t.peerings:
        k = tuple(sorted((a, b)))
        if k in seen:
            continue
        seen.add(k)
        pa = next((pp for pp in props(t.vnets[a]).get("virtualNetworkPeerings") or [] if lid(g(pp, "properties", "remoteVirtualNetwork", "id")) == b), {})
        pp = pa.get("properties") or {}
        mode = "uses remote gateway" if pp.get("useRemoteGateways") else ("offers gateway transit" if pp.get("allowGatewayTransit") else "")
        rows.append([t.vnets[a]["name"], t.vnets[b]["name"] if b in t.vnets else name_from_id(b) + " (missing)", state, mode])
    w(md_table(["VNet", "Peered with", "State", "Gateway"], rows))
    if cfg.get("declaredControls"):
        w("### Declared access controls\n\n")
        w("Controls that live outside Azure Resource Manager (for example Microsoft Entra ID policies) and are therefore "
          "documented here rather than detected automatically.\n\n")
        w(md_table(["Control", "Applies to", "Effect", "How it works", "Evidence source"],
                   [[c.get("name"), ", ".join(c.get("appliesTo", [])), c.get("summary"), c.get("detail"), c.get("source")]
                    for c in cfg["declaredControls"]]))
    if t.private_links:
        w("### Private endpoints\n\n")
        rows = [[f.res.get(pe, {}).get("name"), t.subnets.get(t.placement.get(pe, ("", ""))[1] or "", {}).get("name", ""),
                 f.res.get(tg, {}).get("name") or name_from_id(tg), gid] for pe, tg, gid in t.private_links]
        w(md_table(["Private endpoint", "Subnet", "Target", "Sub-resource"], rows))

    w("## 4. Workloads\n\n")
    for wl in f.workloads:
        members = [m for m in wl["members"] if f.res[m]["type"] not in HIDE_SOLUTION or f.res[m]["type"] == "microsoft.network/virtualnetworks"]
        if not members:
            continue
        w(f"### {wl['name']}\n\n")
        if wl.get("description"):
            w(wl["description"] + "\n\n")
        rows = []
        for m in sorted(members, key=lambda x: (f.res[x]["type"], lid(f.res[x]["name"]))):
            r = f.res[m]
            if lid(r.get("resourceGroup")).startswith("mc_") and r["type"] == "microsoft.compute/virtualmachinescalesets":
                continue
            loc = t.placement.get(m)
            where = ""
            if loc:
                where = t.vnets[loc[0]]["name"] + (f" / {t.subnets[loc[1]]['name']}" if loc[1] else "")
            detail = subtitle(r).split(" · ", 1)[-1] if " · " in subtitle(r) else ""
            if r["type"] == "microsoft.compute/virtualmachines":
                v = f.vm[m]
                detail = f"{v['size']} · {v['image'] or v['os']} · {', '.join(v['ips'])}" + (f" · public {v['pip']}" if v["public"] else "") + f" · {v['power']}"
            rows.append([r["name"], type_info(r)[1], detail, where, region_name(r.get("location"))])
        w(md_table(["Resource", "Type", "Details", "Network placement", "Region"], rows))

    w("## 5. Data, backup and disaster recovery\n\n")
    rows = []
    for rid, r in sorted(f.res.items(), key=lambda kv: lid(kv[1]["name"])):
        if r["type"] == "microsoft.storage/storageaccounts":
            p = props(r)
            pes = [name_from_id(g(c, "properties", "privateEndpoint", "id", default="")) for c in p.get("privateEndpointConnections") or []]
            rows.append([r["name"], r.get("kind"), g(r, "sku", "name", default=""), region_name(r.get("location")),
                         p.get("publicNetworkAccess") or "Enabled (default)", g(p, "networkAcls", "defaultAction", default=""),
                         ", ".join(x for x in pes if x) or "–", g(p, "azureFilesIdentityBasedAuthentication", "directoryServiceOptions", default="")])
    w("### Storage accounts\n\n")
    w(md_table(["Account", "Kind", "SKU", "Region", "Public network access", "Default action", "Private endpoints", "Files identity"], rows))
    vault_red = {lid(r["name"]): g(props(r), "redundancySettings", "standardTierStorageRedundancy", default="")
                 for r in f.res.values() if r["type"] == "microsoft.recoveryservices/vaults"}
    w("### Azure Backup\n\n")
    w(md_table(["Protected item", "Type", "Vault", "Vault redundancy", "Policy", "Last backup (UTC)", "Status"],
               [[b["item"], b["kind"], b["vault"], vault_red.get(lid(b["vault"]), ""), b["policy"], b["last"], b["status"]] for b in f.backup_rows]))
    unprotected = [f.res[v]["name"] for v, info in f.vm.items() if v not in f.protected and v not in {a["vm_id"] for a in f.asr_rows}]
    if unprotected:
        w(f"VMs with neither backup nor ASR: {', '.join(sorted(unprotected))}.\n\n")
    if f.asr_rows:
        w("### Azure Site Recovery (DR replication)\n\n")
        w(md_table(["VM", "From → To", "Vault", "Target RG / VNet", "DR IP", "RPO (s)", "Health", "Active site"],
                   [[a["vm"], f"{a['from']} → {a['to']}", a["vault"], f"{a['target_rg']} / {name_from_id(a['target_net'])}", a["ip"],
                     a["rpo"], a["health"], a["active"]] for a in f.asr_rows]))
    if f.session_hosts:
        w("### Azure Virtual Desktop session hosts\n\n")
        w(md_table(["Host pool / host", "Status", "Sessions", "Last heartbeat"],
                   [[s["name"], g(s, "properties", "status"), g(s, "properties", "sessions"), (g(s, "properties", "lastHeartBeat") or "")[:16]]
                    for s in f.session_hosts]))

    w("## 6. Operations and automation\n\n")
    rows = []
    for rid, r in sorted(f.res.items(), key=lambda kv: (kv[1]["type"], lid(kv[1]["name"]))):
        if r["type"] == "microsoft.logic/workflows":
            trig = list((g(props(r), "definition", "triggers", default={}) or {}).items())
            rec = ""
            if trig:
                rr = g(trig[0][1], "recurrence", default={}) or {}
                rec = f"{rr.get('frequency', '')} every {rr.get('interval', '')}"
                sch = rr.get("schedule") or {}
                if sch:
                    rec += " · " + ", ".join(f"{k}={v}" for k, v in sch.items())
                if rr.get("timeZone"):
                    rec += f" ({rr['timeZone']})"
            rows.append([r["name"], "Logic App", props(r).get("state"), rec, r.get("resourceGroup")])
        elif r["type"] == "microsoft.automation/automationaccounts/runbooks":
            rows.append([r["name"], "Runbook (" + str(props(r).get("runbookType")) + ")", props(r).get("state"), name_from_id(r["id"].split("/runbooks/")[0]), r.get("resourceGroup")])
        elif r["type"] in ("microsoft.operationalinsights/workspaces", "microsoft.monitor/accounts", "microsoft.insights/actiongroups"):
            rows.append([r["name"], type_info(r)[1], "", "", r.get("resourceGroup")])
    w(md_table(["Name", "Type", "State", "Schedule / parent", "Resource group"], rows))

    w("## 7. Security and resilience review points\n\n")
    w("These are automated observations from configuration data. Validate each one before acting; some may be intentional.\n\n")
    w(md_table(["Severity", "Area", "Observation", "Suggested action"], [[x["sev"], x["area"], x["finding"], x["rec"]] for x in f.findings]))
    good = []
    if f.backup_rows and all(b["status"] == "Completed" for b in f.backup_rows):
        good.append(f"All {len(f.backup_rows)} backup items completed their last backup successfully.")
    if f.asr_rows and all(a["health"] == "Normal" for a in f.asr_rows):
        good.append(f"ASR replication is healthy for all {len(f.asr_rows)} protected VMs (RPO between "
                    f"{min(a['rpo'] or 0 for a in f.asr_rows)} and {max(a['rpo'] or 0 for a in f.asr_rows)} seconds).")
    if any("Entra" in (v["auth"] or "") for v in vpn):
        good.append("Remote access VPN uses Microsoft Entra ID authentication (supports MFA / Conditional Access).")
    pg_private = [r["name"] for r in f.res.values() if r["type"] == "microsoft.dbforpostgresql/flexibleservers"
                  and g(props(r), "network", "publicNetworkAccess") == "Disabled"]
    if pg_private:
        good.append(f"PostgreSQL servers ({', '.join(pg_private)}) are VNet-integrated with public access disabled.")
    priv = [r["name"] for r in f.res.values() if r["type"] == "microsoft.storage/storageaccounts" and props(r).get("publicNetworkAccess") == "Disabled"]
    if priv:
        good.append(f"Storage accounts reachable only privately: {', '.join(priv)}.")
    if good:
        w("### What is working well\n\n" + "".join(f"- {x}\n" for x in good) + "\n")

    w("## Appendix: resources by type\n\n")
    w(md_table(["Type", "Count"], [[type_info({'type': k})[1] + f" ({k})", n] for k, n in Counter(r["type"] for r in f.res.values()).most_common()]))
    path.write_text("".join(L), encoding="utf-8")


# =====================================================================================
# HTML viewer (same format as draw.io "Export as HTML")
# =====================================================================================
def write_html(drawio_path: Path, html_path: Path, title: str) -> None:
    xml = drawio_path.read_text(encoding="utf-8")
    cfg = {"highlight": "#0000ff", "nav": True, "resize": True, "page": 0, "dark-mode": "light",
           "toolbar": "pages zoom layers tags lightbox", "edit": "_blank", "xml": xml}
    data = html.escape(json.dumps(cfg), quote=True)
    html_path.write_text(
        f"<!DOCTYPE html>\n<html>\n<head>\n<title>{html.escape(title)}</title>\n<meta charset=\"utf-8\"/>\n</head>\n<body>\n"
        f"<div class=\"mxgraph\" style=\"max-width:100%;border:1px solid transparent;\" data-mxgraph=\"{data}\"></div>\n"
        f"<script type=\"text/javascript\" src=\"https://viewer.diagrams.net/js/viewer-static.min.js\"></script>\n</body>\n</html>\n",
        encoding="utf-8")


# =====================================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Solution architecture diagram + document from azure_inventory.json")
    ap.add_argument("inventory")
    ap.add_argument("--config", help="workload/story config JSON (optional)")
    ap.add_argument("-o", "--output", default="azure_solution", help="output name without extension")
    ap.add_argument("--no-detail-pages", action="store_true", help="only the solution page (skip network + subscription pages)")
    args = ap.parse_args()

    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    facts = Facts(inv, cfg)
    facts.vpn_vnets = {v["vnet"] for v in facts.vpn()}

    bargs = SimpleNamespace(title=cfg.get("title", "Azure"), pages=["network", "inventory"], collapse=3, max_per_subnet=12,
                            hide_empty_subnets=False, vnet_width=1500, page_width=3600)
    builder = DiagramBuilder(facts.t, bargs)
    sol_page = builder.w.new_page("Solution architecture")
    SolutionPage(facts, builder, sol_page, cfg).build()
    if not args.no_detail_pages:
        builder.network_page()
        for sid in sorted(facts.t.subs, key=lambda s: lid(facts.t.sub_names[s])):
            builder.subscription_page(sid)

    out = Path(args.output)
    drawio = out.with_suffix(".drawio")
    builder.w.save(drawio)
    write_html(drawio, out.with_suffix(".html"), cfg.get("title", "Azure architecture"))
    doc = out.with_name(out.name + "_architecture.md")
    write_document(facts, cfg, doc)
    write_ai_context(facts.t, out.with_name(out.name + "_ai_context.md"), anonymize=False)
    sev = Counter(x["sev"] for x in facts.findings)
    print(f"Workloads: {len([w for w in facts.workloads if w['members']])} · VMs: {len(facts.vm)} · backup items: {len(facts.backup_rows)} · "
          f"ASR: {len(facts.asr_rows)} · review points: {dict(sev)}")
    print(f"Diagram   -> {drawio}  ({builder.w.page_count} pages)")
    print(f"Viewer    -> {out.with_suffix('.html')}  (open in a browser)")
    print(f"Document  -> {doc}")


if __name__ == "__main__":
    main()
