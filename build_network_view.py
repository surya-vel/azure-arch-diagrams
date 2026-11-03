#!/usr/bin/env python3
"""
build_network_view.py - Low-level network architecture diagram (icon-and-line style).

Style: no large boxes, just icons with labels arranged
as small trees (VNet -> subnet -> resource -> backup), coloured region zones in the background,
and animated, shadowed connector lines (peering, VPN, replication, ASR, Conditional Access).

Everything is read from azure_inventory.json; the config supplies:
  networkView.order          preferred left-to-right order of the production VNets
  networkView.sideZones      VNets drawn in their own zone (e.g. an isolated analytics subscription)
  declaredControls           controls Azure Resource Graph cannot see (e.g. Entra ID Conditional Access)
  links                      documented relationships (e.g. AD replication between DCs)

Usage
  python build_network_view.py azure_inventory.json --config solution_config.example.json -o my_azure_network
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from build_drawio import DrawioWriter, Page, esc, g, lid, props, tooltip_for, type_info
from build_solution import HIDE_SOLUTION, Facts, region_name, write_html

FONT = "Segoe UI"
AZ = "img/lib/azure2/"
ICONS = {
    "vnet": AZ + "networking/Virtual_Networks.svg",
    "subnet": AZ + "networking/Subnet.svg",
    "vpngw": "img/lib/mscae/VPN_Gateway.svg",
    "p2s": "img/lib/mscae/VPNPointToSite.svg",
    "cloud": "img/lib/active_directory/internet_cloud.svg",
    "laptop": "img/lib/clip_art/computers/Laptop_128x128.png",
    "share": AZ + "other/Managed_File_Shares.svg",
    "backup": AZ + "other/Backup_Vault.svg",
    "ca": AZ + "security/Conditional_Access.svg",
    "entra": AZ + "identity/Azure_Active_Directory.svg",
    "pools": AZ + "compute/VM_Scale_Sets.svg",
}
COLW, COLGAP = 310, 80
GREY_LINE = "#94A3B8"
STY = {
    "tree": ("#94A3B8", "strokeWidth=1.3;endArrow=none;"),
    "peering": ("#2563EB", "strokeWidth=3;dashed=1;dashPattern=8 4 1 4;flowAnimation=1;endArrow=none;shadow=1;"),
    "peer_side": ("#2563EB", "strokeWidth=2;dashed=1;dashPattern=8 4 1 4;flowAnimation=1;endArrow=none;shadow=1;"),
    "vpn": ("#16A34A", "strokeWidth=3;dashed=1;dashPattern=10 5;flowAnimation=1;endArrow=classic;shadow=1;"),
    "repl": ("#DC2626", "shape=link;width=5;strokeWidth=2;flowAnimation=1;endArrow=none;shadow=1;"),
    "asr": ("#DC2626", "strokeWidth=3;dashed=1;dashPattern=1 2;flowAnimation=1;endArrow=classic;shadow=1;"),
    "backup": ("#6B7280", "strokeWidth=1.3;dashed=1;dashPattern=3 3;endArrow=none;"),
    "ca": ("#7C3AED", "strokeWidth=3;dashed=1;dashPattern=10 5;flowAnimation=1;endArrow=classic;shadow=1;"),
}


def icon_of(r: dict) -> str:
    return AZ + type_info(r)[0]


class NetworkView:
    def __init__(self, f: Facts, cfg: dict, page: Page):
        self.f, self.t, self.cfg, self.p = f, f.t, cfg, page
        self.nv = cfg.get("networkView", {})
        self.geo: Dict[str, Tuple[float, float, float, float]] = {}
        self.cell_of: Dict[str, str] = {}          # resource id -> cell
        self.col_of_cell: Dict[str, Tuple[float, float]] = {}   # cell -> (column x, column width)
        self.controls = cfg.get("declaredControls", [])

    # ------------------------------------------------------------------ primitives
    def node(self, x, y, w, h, img, label, tip="", right=True, opacity=None, lw=190, parent="1", font=10) -> str:
        pos = ("labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;spacingLeft=6;"
               if right else "labelPosition=center;verticalLabelPosition=bottom;align=center;verticalAlign=top;")
        style = (f"image;html=1;aspect=fixed;image={img};shadow=1;{pos}whiteSpace=wrap;labelWidth={lw};"
                 f"fontFamily={FONT};fontSize={font};fontColor=#0F172A;" + (f"opacity={opacity};" if opacity else ""))
        cid = self.p._vertex(label, style, x, y, w, h, parent, tip)
        self.geo[cid] = (x, y, w, h)
        return cid

    def zone(self, label, fill, grad, stroke) -> str:
        style = (f"rounded=1;arcSize=2;absoluteArcSize=1;html=1;whiteSpace=wrap;fillColor={fill};gradientColor={grad};"
                 f"gradientDirection=south;strokeColor={stroke};strokeWidth=2;shadow=1;verticalAlign=top;align=left;"
                 f"spacingLeft=16;spacingTop=10;fontFamily={FONT};fontSize=15;fontStyle=1;fontColor=#1F2937;")
        return self.p._vertex(label, style, 0, 0, 10, 10, "1")

    def set_geo(self, cid, x, y, w, h):
        for el in self.p.root:
            if el.get("id") == cid:
                c = el if el.tag == "mxCell" else el.find("mxCell")
                gg = c.find("mxGeometry")
                gg.set("x", str(round(x))); gg.set("y", str(round(y)))
                gg.set("width", str(round(w))); gg.set("height", str(round(h)))
        self.geo[cid] = (x, y, w, h)

    def line(self, a, b, kind, pts=(), label="", exit_="exitX=0.5;exitY=1;", entry="entryX=0;entryY=0.5;"):
        color, extra = STY[kind]
        self.p.routed_edge(a, b, color, list(pts), label, 1, f"{exit_}exitDx=0;exitDy=0;{entry}entryDx=0;entryDy=0;{extra}"
                           f"fontSize=10;fontStyle=1;labelBackgroundColor=#FFFFFF;")

    def text(self, x, y, w, h, html_, size=11, color="#334155"):
        self.p._vertex(html_, f"text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontFamily={FONT};fontSize={size};fontColor={color};",
                       x, y, w, h, "1")

    def cx(self, cid):
        x, y, w, h = self.geo[cid]
        return x + w / 2, y + h / 2

    # ------------------------------------------------------------------ content
    def lab(self, name, *lines, color="#475569") -> str:
        return f"<b>{esc(name)}</b>" + "".join(f"<br><font color='{color}' style='font-size:9.5px'>{esc(x)}</font>" for x in lines if x)

    def items_for_subnet(self, sid: Optional[str], vid: str) -> List[str]:
        f, t = self.f, self.t
        out = []
        for rid, (vn, sn) in t.placement.items():
            r = f.res[rid]
            if vn != vid or sn != sid:
                continue
            rg = lid(r.get("resourceGroup"))
            if r["type"] in HIDE_SOLUTION or rg.startswith("mc_") or rg.startswith("me_"):
                continue
            if r["type"] in ("microsoft.network/virtualnetworkgateways",):
                continue
            out.append(rid)
        order = {"microsoft.compute/virtualmachines": 0, "microsoft.network/privateendpoints": 2}
        return sorted(out, key=lambda x: (order.get(f.res[x]["type"], 1), lid(f.res[x]["name"])))

    def describe(self, rid: str) -> Tuple[str, str, str, List[Tuple[str, str, str, str]]]:
        """icon, label, tooltip, children[(icon, label, tooltip, kind)] for a resource placed in a subnet."""
        f, t = self.f, self.t
        r = f.res[rid]
        typ = r["type"]
        kids: List[Tuple[str, str, str, str]] = []
        if typ == "microsoft.network/privateendpoints":            # show the service behind the endpoint
            tgt = next((tg for pe, tg, _ in t.private_links if pe == rid), "")
            gid = next((gi for pe, _, gi in t.private_links if pe == rid), "")
            tr = f.res.get(tgt)
            if tr:
                kind = "Azure Files" if gid == "file" else type_info(tr)[1]
                for b in f.backup_rows:
                    if b["source"] == tgt:
                        kids.append((ICONS["share"], self.lab(f"share: {b['item']}"), "", "tree"))
                        kids.append((ICONS["backup"], self.lab(b["vault"], f"Backup · {b['policy']}"), "", "backup"))
                self.cell_target_alias = tgt
                return icon_of(tr), self.lab(tr["name"], f"{kind} · via private endpoint", region_name(tr.get("location"))), tooltip_for(tr, t.sub_names), kids
            return icon_of(r), self.lab(r["name"], "Private endpoint"), "", kids
        self.cell_target_alias = None
        if typ == "microsoft.compute/virtualmachines":
            v = f.vm[rid]
            img = v["image"].replace("WindowsServer ", "Windows Server ").replace("-datacenter-gensecond", " Datacenter")
            lines = [", ".join(v["ips"]) + (f" · public {v['pip']}" if v["public"] else ""), f"{v['size'].replace('Standard_', '')} · {img or v['os']}"]
            if v["power"] and v["power"] != "running":
                lines.append(v["power"])
            note = self.cfg.get("labels", {}).get(r["name"])
            if note:
                lines.insert(0, note)
            for sh in f.session_hosts:
                if lid(g(sh, "properties", "resourceId", default="")) == rid:
                    hp = f.res.get(lid(sh["id"]).split("/sessionhosts/")[0])
                    if hp:
                        p = props(hp)
                        kids.append((AZ + "compute/Host_Pools.svg",
                                     self.lab(hp["name"], f"AVD host pool · {p.get('hostPoolType', '')} · {g(sh, 'properties', 'sessions', default=0)} sessions"), "", "tree"))
            for b in f.protected.get(rid, []):
                kids.append((ICONS["backup"], self.lab(b["vault"], f"Backup · {b['policy']}"), "", "backup"))
            return icon_of(r), self.lab(r["name"], *lines), tooltip_for(r, t.sub_names), kids
        if typ == "microsoft.app/managedenvironments":
            p = props(r)
            for a in f.res.values():
                if a["type"] in ("microsoft.app/containerapps",) and lid(props(a).get("managedEnvironmentId")) == rid:
                    doms = [d.get("name") for d in g(props(a), "configuration", "ingress", "customDomains", default=[]) or []]
                    kids.append((icon_of(a), self.lab(a["name"], "Container app", ", ".join(doms)), "", "tree"))
            jobs = [a for a in f.res.values() if a["type"] == "microsoft.app/jobs" and lid(a.get("resourceGroup")) == lid(r.get("resourceGroup"))]
            if jobs:
                kids.append((icon_of(jobs[0]), self.lab(f"{len(jobs)} Container Apps jobs", ", ".join(sorted(j['name'] for j in jobs))), "", "tree"))
            return icon_of(r), self.lab(r["name"], f"Container Apps env · {'internal' if g(p, 'vnetConfiguration', 'internal') else 'external'}",
                                        f"ILB {p.get('staticIp', '')}"), tooltip_for(r, t.sub_names), kids
        if typ == "microsoft.dbforpostgresql/flexibleservers":
            p = props(r)
            return icon_of(r), self.lab(r["name"], f"PostgreSQL {p.get('version', '')} · public access {g(p, 'network', 'publicNetworkAccess', default='?')}"), tooltip_for(r, t.sub_names), kids
        if typ == "microsoft.containerservice/managedclusters":
            p = props(r)
            pools = [a.get("name") for a in p.get("agentPoolProfiles") or []]
            nodes = sum(int(a.get("count") or 0) for a in p.get("agentPoolProfiles") or [])
            kids.append((ICONS["pools"], self.lab(f"{len(pools)} node pools · {nodes} nodes", ", ".join(pools)), "", "tree"))
            note = self.cfg.get("labels", {}).get(r["name"], "")
            return icon_of(r), self.lab(r["name"], f"AKS · Kubernetes {p.get('kubernetesVersion', '')}", note if "·" not in note else ""), tooltip_for(r, t.sub_names), kids
        return icon_of(r), self.lab(r["name"], type_info(r)[1], self.cfg.get("labels", {}).get(r["name"], "")), tooltip_for(r, t.sub_names), kids

    # ------------------------------------------------------------------ one VNet as a tree
    def column(self, vid: str, x: float, y: float, dr: bool = False) -> float:
        f, t = self.f, self.t
        v = t.vnets[vid]
        p = props(v)
        cidr = ", ".join(g(p, "addressSpace", "addressPrefixes", default=[]) or [])
        extra = []
        if dr:
            extra.append("DR network (ASR target)")
        others = []
        for pp in p.get("virtualNetworkPeerings") or []:
            pr = pp.get("properties") or {}
            rv = lid(g(pr, "remoteVirtualNetwork", "id"))
            if rv in t.vnets and pr.get("peeringState") == "Connected" and rv not in f.vpn_vnets and rv not in self.adjacent.get(vid, set()):
                others.append(t.vnets[rv]["name"])
        if others:
            extra.append("also peered: " + ", ".join(sorted(others)))
        vcell = self.node(x + 8, y, 50, 30, ICONS["vnet"],
                          self.lab(v["name"], cidr, f"{region_name(v.get('location'))} · {t.sub_names.get(lid(v.get('subscriptionId')), '')}", *extra,
                                   color="#1D4ED8"), tooltip_for(v, t.sub_names), lw=230, font=11)
        self.cell_of[vid] = vcell
        self.col_of_cell[vcell] = (x, COLW)
        trunk = x + 33
        y += 66 + (12 * len(extra))
        replicas = [a for a in f.asr_rows if a["target_net"] == vid]

        def add_items(rids, parent_cell, parent_trunk, indent, y):
            for rid in rids:
                img, label, tip, kids = self.describe(rid)
                alias = self.cell_target_alias
                cell = self.node(x + indent, y, 40, 40, img, label, tip, lw=COLW - indent - 52)
                self.cell_of[rid] = cell
                if alias:
                    self.cell_of[alias] = cell
                self.col_of_cell[cell] = (x, COLW)
                self.line(parent_cell, cell, "tree", [(parent_trunk, y + 20)])
                y += 62
                for img2, lab2, tip2, kind in kids:
                    k = self.node(x + indent + 42, y, 30, 30, img2, lab2, tip2, lw=COLW - indent - 82, font=9)
                    self.line(cell, k, kind, [(x + indent + 20, y + 15)])
                    y += 44
            return y

        y = add_items(self.items_for_subnet(None, vid), vcell, trunk, 44, y)
        for sid in t.subnet_order[vid]:
            sn = t.subnets[sid]
            flags = " · ".join(z for z in ("NSG" if sn["nsg"] else "", ", ".join(d.split("/")[-1] for d in sn["delegations"] if d)) if z)
            scell = self.node(x + 44, y, 36, 22, ICONS["subnet"], self.lab(sn["name"], sn["prefix"] + (f" · {flags}" if flags else "")),
                              lw=COLW - 90, font=10)
            self.line(vcell, scell, "tree", [(trunk, y + 11)])
            y += 40
            for a in replicas:
                if lid(a.get("subnet")) == sn["name"].lower():
                    rc = self.node(x + 80, y, 40, 40, AZ + "compute/Virtual_Machine.svg",
                                   self.lab(f"{a['vm']} (replica)", a["ip"], f"ASR from {a['from']} · health {a['health']}", color="#B91C1C"),
                                   opacity=55, lw=COLW - 132)
                    self.line(scell, rc, "tree", [(x + 62, y + 20)])
                    self.replica_cell[(a["vm_id"])] = rc
                    y += 62
            y = add_items(self.items_for_subnet(sid, vid), scell, x + 62, 80, y)
            y += 6
        return y

    # ------------------------------------------------------------------ strip of platform services (no lines)
    def strip(self, rids: List[str], x: float, y: float, width: float, title: str) -> float:
        if not rids:
            return y
        self.text(x, y, width, 22, f"<b>{esc(title)}</b>", size=11, color="#475569")
        y += 26
        by_type = defaultdict(list)
        for rid in rids:
            by_type[self.f.res[rid]["type"]].append(rid)
        items = []
        for typ, group in sorted(by_type.items(), key=lambda kv: type_info(self.f.res[kv[1][0]])[1]):
            if len(group) > 2:
                items.append((icon_of(self.f.res[group[0]]), self.lab(f"{type_info(self.f.res[group[0]])[1]} ×{len(group)}",
                                                                       ", ".join(sorted(self.f.res[r]['name'] for r in group)))))
            else:
                for rid in sorted(group, key=lambda r: lid(self.f.res[r]["name"])):
                    r = self.f.res[rid]
                    items.append((icon_of(r), self.lab(r["name"], type_info(r)[1])))
        per_row = max(1, int(width // 150))
        for i, (img, lab) in enumerate(items):
            r_, c_ = divmod(i, per_row)
            self.node(x + c_ * 150 + 50, y + r_ * 110, 40, 40, img, lab, right=False, lw=140, font=9)
        return y + ((len(items) - 1) // per_row + 1) * 110

    # ------------------------------------------------------------------ page
    def build(self) -> None:
        f, t, nv = self.f, self.t, self.nv
        f.vpn_vnets = {v["vnet"] for v in f.vpn()}
        self.replica_cell: Dict[str, str] = {}
        side = {f.by_name.get(lid(n)) for n in nv.get("sideZones", {}).get("vnets", [])} - {None}
        dr_nets = {tn for _, tn in f.asr_pairs}
        prod = [v for v in t.vnets if v not in f.vpn_vnets and v not in side and v not in dr_nets]
        order = [f.by_name.get(lid(n)) for n in nv.get("order", [])]
        prod = [v for v in order if v in prod] + sorted([v for v in prod if v not in order], key=lambda v: lid(t.vnets[v]["name"]))
        # adjacency for drawn spoke-to-spoke peerings
        peers = {(a, b) for a, b, st in t.peerings if st == "Connected"}
        self.adjacent = defaultdict(set)
        for a, b in zip(prod, prod[1:]):
            if (a, b) in peers or (b, a) in peers:
                self.adjacent[a].add(b)
                self.adjacent[b].add(a)

        title = nv.get("title", self.cfg.get("title", "Azure") + " – Network")
        self.banner = self.p._vertex(f"<b style='font-size:24px'>{esc(title)}</b><br><span style='font-size:12px'>Generated from Azure Resource Graph · "
                       f"export {esc(f.exported[:16].replace('T', ' '))} UTC · {len(t.subs)} subscriptions · {len(t.vnets)} VNets · {len(f.vm)} VMs</span>",
                       f"rounded=1;arcSize=8;html=1;whiteSpace=wrap;fillColor=#1E3A8A;gradientColor=#4338CA;gradientDirection=east;strokeColor=none;"
                       f"shadow=1;fontColor=#FFFFFF;align=left;spacingLeft=24;fontFamily={FONT};", 0, 0, 1500, 76, "1")

        # zones first (geometry fixed later) so they sit behind everything
        z_prod = self.zone(f"PRODUCTION · {esc(nv.get('prodLabel', 'primary regions'))}", "#F0FDF4", "#DCFCE7", "#16A34A")
        z_dr = self.zone(f"DISASTER RECOVERY · {esc(nv.get('drLabel', 'Azure Site Recovery targets'))}", "#FFF7ED", "#FFEDD5", "#EA580C") if dr_nets else None
        z_side = self.zone(esc(nv.get("sideZones", {}).get("title", "Separate environment")), "#EEF2FF", "#E0E7FF", "#4F46E5") if side else None

        top = 300
        hub_y = top + 60
        # ---- hub chain
        hubs = [v for v in t.vnets if v in f.vpn_vnets]
        x0 = 40
        prod_w = len(prod) * COLW + (len(prod) - 1) * COLGAP + 80
        hub_cells = {}
        if hubs:
            hv = hubs[0]
            vpn = f.vpn()[0]
            hx = x0 + prod_w / 2 - 330
            v = t.vnets[hv]
            hc = self.node(hx, hub_y, 56, 34, ICONS["vnet"], self.lab(v["name"], ", ".join(g(props(v), "addressSpace", "addressPrefixes", default=[]) or []),
                                                                      f"{region_name(v.get('location'))} · HUB", color="#1D4ED8"), right=False, lw=170, font=11)
            gws = t.subnet_order[hv]
            sc = self.node(hx + 200, hub_y + 6, 40, 24, ICONS["subnet"], self.lab(t.subnets[gws[0]]["name"], t.subnets[gws[0]]["prefix"]), right=False, lw=150) if gws else hc
            gc = self.node(hx + 380, hub_y - 8, 46, 50, ICONS["vpngw"], self.lab(vpn["name"], f"VPN gateway · {vpn['sku']}", vpn["region"]), right=False, lw=170, font=11)
            pc = self.node(hx + 560, hub_y - 4, 50, 42, ICONS["p2s"], self.lab("Point-to-site", f"pool {vpn['pool']}", f"{vpn['protocols']} · {vpn['auth']}",
                                                                              f"{vpn['clients']} clients at export" if vpn["clients"] is not None else ""),
                           right=False, lw=180)
            for a, b in ((hc, sc), (sc, gc), (gc, pc)):
                self.line(a, b, "tree", [], exit_="exitX=1;exitY=0.5;", entry="entryX=0;entryY=0.5;")
            hub_cells = {"vnet": hc, "gw": gc}
            self.cell_of[hv] = hc

        # platform services strip: top-left of the production zone, beside the hub (no lines pass here)
        prod_wl = [w for w in f.workloads if w.get("row") in ("primary", "shared", "access") and w["name"] not in nv.get("sideZones", {}).get("workloads", [])]
        strip_types = set(nv.get("stripTypes", ["microsoft.storage/storageaccounts", "microsoft.keyvault/vaults", "microsoft.containerregistry/registries",
                                                 "microsoft.cognitiveservices/accounts", "microsoft.operationalinsights/workspaces", "microsoft.monitor/accounts",
                                                 "microsoft.logic/workflows", "microsoft.automation/automationaccounts"]))
        backed_vaults = {b["vault_id"] for b in f.backup_rows}
        pe_targets = {tg for _, tg, _ in t.private_links}
        strip_ids = [m for w in prod_wl for m in w["members"] if f.res[m]["type"] in strip_types and m not in pe_targets
                     and m not in backed_vaults and not lid(f.res[m].get("resourceGroup")).startswith(("mc_", "me_"))]
        hub_left = (x0 + prod_w / 2 - 330) if hubs else x0 + prod_w
        strip_end = self.strip(sorted(set(strip_ids)), x0 + 30, top + 50, hub_left - x0 - 90, "Platform services (no VNet placement)")

        # ---- production columns
        col_top = max(hub_y + 200, strip_end + 90)
        bottoms = {}
        col_x = {}
        for i, vid in enumerate(prod):
            x = x0 + 40 + i * (COLW + COLGAP)
            col_x[vid] = x
            bottoms[vid] = self.column(vid, x, col_top)
        prod_bottom = max(bottoms.values(), default=col_top) + 70

        # gateway-transit bus
        if hub_cells:
            hcx, _ = self.cx(hub_cells["vnet"])
            bus_y = col_top - 50
            for vid in prod:
                uses = any(g(pp, "properties", "useRemoteGateways") for pp in props(t.vnets[vid]).get("virtualNetworkPeerings") or [])
                if uses:
                    vx, _ = self.cx(self.cell_of[vid])
                    self.line(hub_cells["vnet"], self.cell_of[vid], "peering", [(hcx, bus_y), (vx, bus_y)], "",
                              exit_="exitX=0.5;exitY=1;", entry="entryX=0.5;entryY=0;")
            self.text(hcx + 10, bus_y - 26, 360, 20, "<font color='#2563EB'><b>VNet peering · VPN gateway transit</b></font>", size=11)
        # adjacent spoke peerings (lane just above the VNet icons)
        lane = col_top - 18
        for a, b in zip(prod, prod[1:]):
            if b in self.adjacent.get(a, set()):
                ax_, _ = self.cx(self.cell_of[a])
                bx_, _ = self.cx(self.cell_of[b])
                self.line(self.cell_of[a], self.cell_of[b], "peer_side", [(ax_ + 14, lane), (bx_ - 14, lane)], "",
                          exit_="exitX=0.78;exitY=0;", entry="entryX=0.22;entryY=0;")

        # documented links (e.g. AD replication between DCs): drawn between their VNets in a lane above the icons
        repl_lane = col_top - 34
        for link in self.cfg.get("links", []):
            a, b = f.by_name.get(lid(link["from"])), f.by_name.get(lid(link["to"]))
            va, vb = t.placement.get(a, (None,))[0], t.placement.get(b, (None,))[0]
            ca, cb = self.cell_of.get(va), self.cell_of.get(vb)
            if not (ca and cb) or ca == cb:
                continue
            if self.geo[ca][0] > self.geo[cb][0]:
                ca, cb, a, b = cb, ca, b, a
            xa, xb = self.geo[ca][0] + 46, self.geo[cb][0] + 4
            label = f"{link.get('label', '')} ({f.res[a]['name']} ↔ {f.res[b]['name']})"
            self.line(ca, cb, "repl", [(xa, repl_lane), (xb, repl_lane)], label, exit_="exitX=0.92;exitY=0;", entry="entryX=0.08;entryY=0;")

        self.set_geo(z_prod, x0, top, prod_w, prod_bottom - top)
        prod_end = prod_bottom

        # ---- DR zone (columns aligned under their source VNets)
        y_dr = prod_end + 60
        dr_bottom = y_dr
        if z_dr:
            used_x = set()
            for src, tgt in sorted(f.asr_pairs):
                x = col_x.get(src, x0 + 40)
                while x in used_x:
                    x += COLW + COLGAP
                used_x.add(x)
                dr_bottom = max(dr_bottom, self.column(tgt, x, y_dr + 70, dr=True))
            dr_w = [w for w in f.workloads if w.get("row") == "dr"]
            dr_ids = [m for w in dr_w for m in w["members"] if f.res[m]["type"] in
                      ("microsoft.recoveryservices/vaults", "microsoft.desktopvirtualization/hostpools", "microsoft.network/publicipaddresses")]
            dr_bottom = self.strip(sorted(set(dr_ids)), x0 + 40, dr_bottom + 10, prod_w - 80, "DR services")
            self.set_geo(z_dr, x0, y_dr, prod_w, dr_bottom - y_dr + 30)
            # ASR replication lines: source VM -> replica, via the gap right of the column
            for a in f.asr_rows:
                s, r = self.cell_of.get(a["vm_id"]), self.replica_cell.get(a["vm_id"])
                if s and r:
                    (_, sy, _, sh), (rx, ry, rw, _) = self.geo[s], self.geo[r]
                    ch = self.col_of_cell[s][0] - 22
                    rpo = f" · RPO {round((a['rpo'] or 0) / 60, 1)} min" if a.get("rpo") else ""
                    self.line(s, r, "asr", [(ch, sy + sh / 2), (ch, ry - 12), (rx + rw / 2, ry - 12)], f"ASR{rpo}",
                              exit_="exitX=0;exitY=0.5;", entry="entryX=0.5;entryY=0;")

        # ---- side zone (e.g. an isolated subscription) + users / internet / conditional access
        sx0 = x0 + prod_w + 120
        side_w = COLW + 160
        users_y, cloud_y = 110, 120
        users_x = sx0 + side_w - 260
        self.p._vertex("<b>Remote users</b>", f"rounded=1;arcSize=10;html=1;whiteSpace=wrap;fillColor=#FEF9C3;strokeColor=#CA8A04;strokeWidth=2;shadow=1;"
                                                     f"verticalAlign=bottom;spacingBottom=6;fontFamily={FONT};fontSize=12;", users_x, users_y - 20, 260, 170, "1")
        lap1 = self.node(users_x + 20, users_y, 60, 60, ICONS["laptop"], self.lab("Azure VPN Client", "Entra ID sign-in"), right=False, lw=110)
        lap2 = self.node(users_x + 160, users_y, 60, 60, ICONS["laptop"], self.lab("Browser", "managed device"), right=False, lw=110)
        cloud = self.node(sx0 - 60, cloud_y, 90, 56, ICONS["cloud"], self.lab("Internet"), right=False, lw=100, font=11)
        self.line(lap1, cloud, "vpn", [], "", exit_="exitX=0;exitY=0.5;", entry="entryX=1;entryY=0.5;")
        if hub_cells:
            gx, gy = self.cx(hub_cells["gw"])
            cx_, cy_ = self.cx(cloud)
            self.line(cloud, hub_cells["gw"], "vpn", [(gx, cy_)], "P2S VPN (encrypted)", exit_="exitX=0;exitY=0.5;", entry="entryX=0.5;entryY=0;")
        if z_side:
            zy = top
            ca_ctrl = next((c for c in self.controls if c.get("type") == "conditionalAccess"), None)
            y = zy + 60
            ca_cell = None
            if ca_ctrl:
                ca_cell = self.node(sx0 + 40, y, 48, 48, ICONS["ca"],
                                    self.lab(ca_ctrl.get("name", "Conditional Access"), *ca_ctrl.get("diagramLines", [ca_ctrl.get("summary", "")]),
                                             color="#6D28D9"), lw=side_w - 120, font=11)
                cx_, cy_ = self.cx(cloud)
                self.line(lap2, ca_cell, "ca", [(self.cx(lap2)[0], cy_ + 60), (self.cx(ca_cell)[0], cy_ + 60)], "",
                          exit_="exitX=0.5;exitY=1;", entry="entryX=0.5;entryY=0;")
                y += 120
            bottom = y
            for vid in sorted(side, key=lambda v: lid(t.vnets[v]["name"])):
                bottom = self.column(vid, sx0 + 40, y)
                y = bottom + 30
            if ca_cell:
                for app in ca_ctrl.get("appliesTo", []):
                    tc = self.cell_of.get(f.by_name.get(lid(app)))
                    if tc:
                        tx, ty, tw, th = self.geo[tc]
                        ch = sx0 + side_w - 30
                        self.line(ca_cell, tc, "ca", [(ch, self.cx(ca_cell)[1]), (ch, ty - 12), (tx + tw / 2, ty - 12)],
                                  ca_ctrl.get("edgeLabel", "authenticated + managed device"), exit_="exitX=1;exitY=0.5;", entry="entryX=0.5;entryY=0;")
                        break
            side_ids = [m for w in f.workloads if w["name"] in nv.get("sideZones", {}).get("workloads", []) for m in w["members"]
                        if f.res[m]["type"] in ("microsoft.keyvault/vaults", "microsoft.storage/storageaccounts", "microsoft.automation/automationaccounts",
                                                 "microsoft.solutions/applications") and m not in self.cell_of]
            bottom = self.strip(sorted(set(side_ids)), sx0 + 20, bottom + 10, side_w - 40, "Services")
            self.set_geo(z_side, sx0, top, side_w, bottom - top + 30)

        self.legend(sx0, max(dr_bottom, top) + 60 if not z_side else self.geo[z_side][1] + self.geo[z_side][3] + 40, side_w)
        self.set_geo(self.banner, 0, 0, sx0 + side_w, 76)

    def legend(self, x, y, w):
        rows = [("peering", "VNet peering / VPN gateway transit"), ("vpn", "Remote user VPN (P2S, encrypted)"),
                ("ca", "Entra ID Conditional Access path"), ("repl", "AD DS replication (documented)"),
                ("asr", "Azure Site Recovery replication"), ("backup", "Azure Backup"), ("tree", "Contains / connects to")]
        body = "".join(f"<font color='{STY[k][0]}'><b>━━━</b></font>&nbsp; {esc(lbl)}<br>" for k, lbl in rows)
        self.p._vertex(f"<b style='font-size:13px'>Legend</b><br>{body}<font color='#B91C1C'>Faded VM</font> = DR replica (standby)",
                       f"rounded=1;arcSize=6;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=12;fillColor=#FFFFFF;"
                       f"strokeColor=#CBD5E1;shadow=1;fontFamily={FONT};fontSize=11;", x, y, w, 210, "1")


def main() -> None:
    ap = argparse.ArgumentParser(description="Low-level network diagram (icon-and-line style)")
    ap.add_argument("inventory")
    ap.add_argument("--config", required=True)
    ap.add_argument("-o", "--output", default="azure_network_view")
    args = ap.parse_args()
    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    facts = Facts(inv, cfg)
    w = DrawioWriter(cfg.get("title", "Azure"))
    page = w.new_page("Network architecture")
    NetworkView(facts, cfg, page).build()
    out = Path(args.output)
    w.save(out.with_suffix(".drawio"))
    write_html(out.with_suffix(".drawio"), out.with_suffix(".html"), cfg.get("networkView", {}).get("title", "Azure network"))
    print(f"Network view -> {out.with_suffix('.drawio')} and {out.with_suffix('.html')}")


if __name__ == "__main__":
    main()
