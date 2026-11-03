#!/usr/bin/env python3
"""
build_focus_view.py - Focused Azure diagram using the standard hierarchy

        Subscription
          └ Resource group
              └ Virtual network
                  └ Subnet
                      └ Resources

for a chosen set of resource groups (or whole subscriptions), with the connections Azure knows about
(VNet peering / gateway transit, P2S + S2S VPN, private endpoints, load-balancer back ends, AVD,
Azure Backup, Azure Site Recovery) plus declared data flows and controls from the config.

Three draw.io layers keep it clean:
  "Background"  subscription / resource group / VNet / subnet boxes   (bottom)
  "Connections" all lines                                              (middle)
  "Resources"   icons and labels (labels have a background, so lines pass *behind* text)  (top)

The layout is calculated automatically. Everything is read from azure_inventory.json; the optional
"focusView" section of the config chooses the scope and adds what Azure cannot know:

  focusView.resourceGroups   resource groups to show ("name" or "name@subscription name")
  focusView.subscriptions    show every resource group in these subscriptions (names or IDs)
  focusView.declaredFlows    data flows Azure cannot detect: {"name", "path": [res, ...], "labels", "note"}
  focusView.notes            free-text notes printed under the diagram
  focusView.width            max page width in px (default 3000)
  focusView.showAllResources also show NICs, disks and attached public IPs / NSGs (default false)
Without resourceGroups / subscriptions every resource group is shown (minus ignoreResourceGroups).

Usage
  python build_focus_view.py azure_inventory.json --config solution_config.example.json -o my_focus_view
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from build_drawio import DrawioWriter, Page, breakable, esc, g, lid, props, tooltip_for, type_info
from build_solution import Facts, region_name, write_html

FONT = "Segoe UI"
AZ = "img/lib/azure2/"
LAPTOP = "img/lib/clip_art/computers/Laptop_128x128.png"

# ----------------------------------------------------------------------------------------------
# Colours (Azure diagram conventions: subscription blue, resource group grey dashed,
# VNet green, subnet blue dashed, DR orange)
# ----------------------------------------------------------------------------------------------
BOX = {
    "sub": ("#F7FAFF", "#0078D4", 2.2, False),
    "rg": ("#FFFFFF", "#7F8C8D", 1.4, True),
    "rg_dr": ("#FFF8F1", "#E8590C", 1.6, True),
    "vnet": ("#F1FAF3", "#2E8B57", 1.8, False),
    "subnet": ("#FFFFFF", "#4A90D9", 1.2, True),
    "users": ("#FFFBEB", "#CA8A04", 1.6, False),
}
BOX_ICON = {"sub": "general/Subscriptions.svg", "rg": "general/Resource_Groups.svg", "rg_dr": "general/Resource_Groups.svg",
            "vnet": "networking/Virtual_Networks.svg", "subnet": "networking/Subnet.svg"}
ORTHO = "edgeStyle=orthogonalEdgeStyle;rounded=1;jumpStyle=arc;jumpSize=6;"
EDGES = {
    "vpn": ("#16A34A", ORTHO + "strokeWidth=3;dashed=1;dashPattern=10 5;flowAnimation=1;endArrow=classic;shadow=1;"),
    "transit": ("#2563EB", ORTHO + "strokeWidth=3;dashed=1;dashPattern=8 4 1 4;flowAnimation=1;endArrow=none;shadow=1;"),
    "pe": ("#EA580C", ORTHO + "strokeWidth=2.2;dashed=1;dashPattern=6 3;flowAnimation=1;endArrow=classic;shadow=1;"),
    "backend": ("#0891B2", ORTHO + "strokeWidth=2;endArrow=classic;"),
    "repl": ("#DC2626", ORTHO + "shape=link;width=6;strokeWidth=2;endArrow=none;shadow=1;"),
    "asr": ("#DC2626", ORTHO + "strokeWidth=3;dashed=1;dashPattern=1 2;flowAnimation=1;endArrow=classic;shadow=1;"),
    "backup": ("#6B7280", ORTHO + "strokeWidth=1.4;dashed=1;dashPattern=3 3;endArrow=open;"),
    "avd": ("#15803D", ORTHO + "strokeWidth=2;endArrow=classic;shadow=1;"),
    "ca": ("#7C3AED", ORTHO + "strokeWidth=3;dashed=1;dashPattern=10 5;flowAnimation=1;endArrow=classic;shadow=1;"),
    # 3D pipe for declared data flows
    "data": ("#0F766E", ORTHO + "shape=flexArrow;endArrow=block;endWidth=14;endSize=5;width=7;fillColor=#2DD4BF;"
                        "gradientColor=#0F766E;gradientDirection=north;strokeWidth=1;shadow=1;"),
}
LEGEND = [("data", "Declared data flow (from the config)"), ("vpn", "VPN (P2S / site-to-site, encrypted)"),
          ("transit", "VNet peering / VPN gateway transit"), ("pe", "Private endpoint → service (Private Link)"),
          ("backend", "Load balancer / App Gateway → back end"), ("repl", "Declared link (e.g. AD replication)"),
          ("asr", "Azure Site Recovery replication"), ("backup", "Azure Backup"),
          ("avd", "AVD workspace → app group → host pool → session host"), ("ca", "Declared access control (e.g. Conditional Access)")]

# ----------------------------------------------------------------------------------------------
# Sizes (px). Every box is: header + padded content; content = child boxes, then an icon grid.
# ----------------------------------------------------------------------------------------------
HEADER = {"sub": 56, "rg": 50, "rg_dr": 50, "vnet": 62, "subnet": 52}
PAD = {"sub": 18, "rg": 16, "rg_dr": 16, "vnet": 16, "subnet": 10}
MIN_W = {"sub": 420, "rg": 330, "rg_dr": 330, "vnet": 340, "subnet": 210}
GAP = 22
CELL = {"subnet": (150, 140, 48), "strip": (130, 112, 40)}    # cell width, cell height, icon size
SUBNET_COLS = 4
MAX_INNER = {"vnet": 1350, "rg": 1500, "rg_dr": 1500}

IMPLIED_TYPES = {"microsoft.network/networkinterfaces", "microsoft.compute/disks"}
NEVER_LIST = {"microsoft.network/virtualnetworks", "microsoft.automation/automationaccounts/runbooks",
              "microsoft.network/privatednszones/virtualnetworklinks"}


@dataclass
class Item:
    """One icon in a box's grid (a resource, a collapsed group of resources, or a DR replica)."""
    img: str
    title: str
    lines: List[Tuple[str, str]]
    tip: str = ""
    rids: List[str] = field(default_factory=list)
    key: str = ""              # cell key for edges (defaults to the resource id)
    opacity: Optional[int] = None


@dataclass
class Node:
    kind: str                  # sub | rg | rg_dr | vnet | subnet
    title: str
    sub: str = ""
    key: str = ""
    children: List["Node"] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    cell: str = "subnet"       # which CELL size the grid uses
    max_inner: float = 1500
    w: float = 0
    h: float = 0
    x: float = 0
    y: float = 0
    _child_pos: List[Tuple[float, float]] = field(default_factory=list)
    _grid: Tuple[float, int] = (0, 1)      # (y offset of the icon grid, columns)


def pack(sizes: List[Tuple[float, float]], max_w: float, gap: float = GAP) -> Tuple[List[Tuple[float, float]], float, float]:
    """Shelf packing: left to right, wrap to a new row at max_w. Returns positions, width, height."""
    pos, x, y, row_h, width = [], 0.0, 0.0, 0.0, 0.0
    for w, h in sizes:
        if x > 0 and x + w > max_w:
            x, y, row_h = 0.0, y + row_h + gap, 0.0
        pos.append((x, y))
        x += w + gap
        row_h = max(row_h, h)
        width = max(width, x - gap)
    return pos, width, (y + row_h if sizes else 0.0)


def measure(n: Node) -> None:
    for c in n.children:
        measure(c)
    n._child_pos, cw, ch = pack([(c.w, c.h) for c in n.children], n.max_inner)
    cell_w, cell_h, _ = CELL[n.cell]
    k = len(n.items)
    if n.kind == "subnet":
        cols = min(k, SUBNET_COLS)
    else:
        cols = min(k, max(4, int(max(cw, 0) // cell_w)))
    cols = max(cols, 1)
    rows = math.ceil(k / cols) if k else 0
    inner_w = max(cw, cols * cell_w if k else 0, MIN_W[n.kind] - 2 * PAD[n.kind])
    grid_y = ch + (GAP if ch and k else 0)
    n._grid = (grid_y, cols)
    n.w = inner_w + 2 * PAD[n.kind]
    n.h = HEADER[n.kind] + grid_y + rows * cell_h + PAD[n.kind]


def place(n: Node, x: float, y: float) -> None:
    n.x, n.y = x, y
    for c, (cx, cy) in zip(n.children, n._child_pos):
        place(c, x + PAD[n.kind] + cx, y + HEADER[n.kind] + cy)


class FocusView:
    def __init__(self, f: Facts, cfg: dict, page: Page):
        self.f, self.t, self.cfg, self.p = f, f.t, cfg, page
        self.fv = cfg.get("focusView", {})
        self.labels = cfg.get("labels", {})
        self.cells: Dict[str, str] = {}
        self.edges_seen: Set[Tuple[str, str, str]] = set()
        self.used_kinds: Set[str] = set()

    # ------------------------------------------------------------------ scope
    def scope(self) -> List[dict]:
        t = self.t
        sub_of_name = {lid(n): s for s, n in t.sub_names.items()}
        want_subs = {sub_of_name.get(lid(s), lid(s)) for s in self.fv.get("subscriptions", [])}
        want_rgs = []
        for key in self.fv.get("resourceGroups", []):
            name, _, sub = key.partition("@")
            want_rgs.append((lid(name), sub_of_name.get(lid(sub), lid(sub)) if sub else ""))
        ignore = {lid(x) for x in self.cfg.get("ignoreResourceGroups", [])}
        order = {k: i for i, k in enumerate(want_rgs)}
        net_rank: Dict[Tuple[str, str], int] = {}
        for vid, v in t.vnets.items():
            k = (lid(v.get("resourceGroup")), lid(v.get("subscriptionId")))
            net_rank[k] = min(net_rank.get(k, 2), 0 if vid in t.hubs else 1)

        def rank(rg):
            # configured order first; otherwise hub networks, then other networks, then the rest
            n, s = lid(rg["name"]), lid(rg["subscriptionId"])
            return order.get((n, s), order.get((n, ""), len(order))), net_rank.get((n, s), 2), n

        picked = []
        for rg in t.rgs:
            n, s = lid(rg["name"]), lid(rg["subscriptionId"])
            explicit = any(n == wn and (not ws or ws == s) for wn, ws in want_rgs)
            if explicit or (s in want_subs and n not in ignore) or (not want_rgs and not want_subs and n not in ignore):
                picked.append(rg)
        return sorted(picked, key=rank)

    # ------------------------------------------------------------------ labels from live data
    def res_lines(self, r: dict, box_rg: str) -> List[Tuple[str, str]]:
        f, t = self.f, self.t
        rid = lid(r["id"])
        typ = r["type"]
        grey, red, purple = "#475569", "#B91C1C", "#6D28D9"
        out: List[Tuple[str, str]] = []
        note = self.labels.get(r["name"])
        if note:
            out.append((note, purple))
        if typ == "microsoft.compute/virtualmachines" and rid in f.vm:
            v = f.vm[rid]
            out.append((", ".join(v["ips"]), grey))
            if v["public"]:
                out.append((f"public IP {v['pip']}".strip(), red))
            img = v["image"].replace("WindowsServer ", "Windows Server ").replace("-datacenter-gensecond", " Datacenter")
            out.append((f"{v['size'].replace('Standard_', '')} · {img or v['os']}", grey))
            if v["power"] and v["power"] != "running":
                out.append((v["power"], red))
        elif typ == "microsoft.network/privateendpoints":
            tg = next(((tgt, gid) for pe, tgt, gid in t.private_links if pe == rid), None)
            out.append(("Private endpoint", grey))
            if tg:
                tname = (t.all_res.get(tg[0]) or {}).get("name") or tg[0].split("/")[-1]
                out.append((f"→ {tname} ({tg[1]})", "#C2410C"))
        elif typ == "microsoft.storage/storageaccounts":
            p = props(r)
            shares = [b["item"] for b in f.backup_rows if b["source"] == rid and b["kind"] == "AzureFileShare"]
            out.append((f"{r.get('kind', '')} · {g(r, 'sku', 'name', default='')}", grey))
            if shares:
                out.append(("file share: " + ", ".join(shares), grey))
            pna = p.get("publicNetworkAccess") or "Enabled"
            out.append((f"public network access: {pna}", red if pna != "Disabled" else "#15803D"))
        elif typ == "microsoft.recoveryservices/vaults":
            red_ = g(props(r), "redundancySettings", "standardTierStorageRedundancy", default="") or ""
            items = [b["item"] for b in f.backup_rows if lid(b["vault"]) == lid(r["name"])] + \
                    [a["vm"] for a in f.asr_rows if lid(a["vault"]) == lid(r["name"])]
            out.append((f"Recovery Services vault {('· ' + red_.replace('Redundant', '')) if red_ else ''}".strip(), grey))
            if items:
                out.append(("protects: " + ", ".join(items), grey))
        elif typ == "microsoft.desktopvirtualization/hostpools":
            p = props(r)
            out.append((f"Host pool · {p.get('hostPoolType', '')} · {p.get('loadBalancerType', '')} · max {p.get('maxSessionLimit', '')}", grey))
        elif typ == "microsoft.containerservice/managedclusters":
            p = props(r)
            pools = p.get("agentPoolProfiles") or []
            out.append((f"AKS {p.get('kubernetesVersion', '')} · {len(pools)} node pools · "
                        f"{sum(int(a.get('count') or 0) for a in pools)} nodes", grey))
        elif typ == "microsoft.network/virtualnetworkgateways":
            vpn = next((v for v in f.vpn() if v["id"] == rid), None)
            if vpn:
                out.append((f"{vpn['type'] or 'Gateway'} · {vpn['sku']}", grey))
                if vpn["pool"]:
                    out += [(f"P2S pool {vpn['pool']}", grey), (f"{vpn['protocols']} · {vpn['auth']}".strip(" ·"), grey)]
        elif typ == "microsoft.automation/automationaccounts":
            n = sum(1 for x in f.res.values() if x["type"].endswith("/runbooks") and lid(x["id"]).startswith(rid + "/"))
            out.append((f"Automation account · {n} runbooks", grey))
        else:
            out.append((type_info(r)[1], grey))
        if lid(r.get("resourceGroup")) != lid(box_rg):
            out.append((f"RG: {r.get('resourceGroup')}", "#64748B"))
        return [(x, c) for x, c in out if x]

    def items_for(self, rids: List[str], box_rg: str, collapse: int, max_lines: int) -> List[Item]:
        """Icons for a list of resources; types with more than `collapse` members become one ×N icon."""
        f = self.f
        by_type: Dict[str, List[str]] = defaultdict(list)
        for rid in rids:
            by_type[f.res[rid]["type"]].append(rid)
        out = []
        for typ, grp in sorted(by_type.items(), key=lambda kv: type_info(f.res[kv[1][0]])[1]):
            grp = sorted(grp, key=lambda x: lid(f.res[x]["name"]))
            if len(grp) > collapse:
                r0 = f.res[grp[0]]
                names = ", ".join(f.res[x]["name"] for x in grp)
                out.append(Item(type_info(r0)[0], f"{type_info(r0)[1]} ×{len(grp)}", [(names[:90], "#475569")],
                                "\n".join(f.res[x]["name"] for x in grp), grp, f"group:{grp[0]}"))
                continue
            for rid in grp:
                r = f.res[rid]
                out.append(Item(type_info(r)[0], r["name"], self.res_lines(r, box_rg)[:max_lines],
                                tooltip_for(r, self.t.sub_names), [rid], rid))
        return out

    # ------------------------------------------------------------------ tree
    def build_tree(self) -> List[Node]:
        f, t = self.f, self.t
        rgs = self.scope()
        in_scope = {(lid(rg["name"]), lid(rg["subscriptionId"])) for rg in rgs}

        def rg_key(r):
            return lid(r.get("resourceGroup")), lid(r.get("subscriptionId"))

        show_all = self.fv.get("showAllResources", False)
        collapse = int(self.fv.get("collapse", 3))
        max_per_subnet = int(self.fv.get("maxPerSubnet", 12))
        vnets_by_rg: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for vid, v in t.vnets.items():
            if rg_key(v) in in_scope:
                vnets_by_rg[rg_key(v)].append(vid)
        self.view_vnets = {vid for vs in vnets_by_rg.values() for vid in vs}
        dr_nets = {a["target_net"] for a in f.asr_rows if a.get("target_net")}
        dr_rgs = {lid(a["target_rg"]) for a in f.asr_rows if a.get("target_rg")}

        # what sits in each subnet / at VNet level
        in_subnet: Dict[str, List[str]] = defaultdict(list)
        at_vnet: Dict[str, List[str]] = defaultdict(list)
        hidden = self.hidden_resources(show_all)
        for rid, (vn, sn) in t.placement.items():
            if vn in self.view_vnets and rid not in hidden:
                (in_subnet[sn] if sn else at_vnet[vn]).append(rid)
        drawn_in_net = {rid for lst in list(in_subnet.values()) + list(at_vnet.values()) for rid in lst}

        # DR replicas go into their recovery subnet
        replicas: Dict[str, List[Item]] = defaultdict(list)
        for a in f.asr_rows:
            tn = a.get("target_net")
            if tn not in self.view_vnets:
                continue
            sid = f"{tn}/subnets/{lid(a.get('subnet'))}"
            if sid not in t.subnets:
                sid = next(iter(t.subnet_order.get(tn, [])), "")
            replicas[sid or tn].append(Item(
                "compute/Virtual_Machine.svg", f"{a['vm']} (replica)",
                [(a["ip"], "#475569"), (f"ASR from {a['from']}", "#B91C1C"), (f"health {a['health']} · standby", "#B91C1C")],
                f"ASR replica of {a['vm']}\nvault {a['vault']}\nRPO {a['rpo']} s", [], f"replica:{a['vm_id']}", 55))

        by_sub: Dict[str, List[Node]] = defaultdict(list)
        for rg in rgs:
            key = (lid(rg["name"]), lid(rg["subscriptionId"]))
            is_dr = lid(rg["name"]) in dr_rgs or any(v in dr_nets for v in vnets_by_rg[key])
            kind = "rg_dr" if is_dr else "rg"
            loc = region_name(rg.get("location"))
            sub = "AKS node resource group (managed by AKS) · " + loc if lid(rg["name"]).startswith("mc_") else \
                loc + (" · disaster recovery" if is_dr else "")
            node = Node(kind, f"Resource group · {rg['name']}", sub, cell="strip", max_inner=MAX_INNER[kind])
            for vid in sorted(vnets_by_rg[key], key=lambda x: lid(t.vnets[x]["name"])):
                node.children.append(self.vnet_node(vid, rg["name"], in_subnet, at_vnet, replicas, max_per_subnet))
            rest = [rid for rid, r in f.res.items() if rg_key(r) == key and rid not in drawn_in_net
                    and rid not in hidden and r["type"] not in NEVER_LIST]
            node.items = self.items_for(rest, rg["name"], collapse, 2)
            by_sub[lid(rg["subscriptionId"])].append(node)

        width = float(self.fv.get("width", 3000))
        subs = []
        for sid, rg_nodes in by_sub.items():
            rg_nodes.sort(key=lambda n: n.kind == "rg_dr")       # DR resource groups last
            subs.append(Node("sub", f"Subscription · {t.sub_names.get(sid, sid)}", sid, children=rg_nodes,
                             max_inner=width - 2 * PAD["sub"]))
        return subs

    def hidden_resources(self, show_all: bool) -> Set[str]:
        """Resources implied by others (NICs, disks, attached public IPs / NSGs) unless showAllResources."""
        if show_all:
            return set()
        f = self.f
        out = set()
        for rid, r in f.res.items():
            p = props(r)
            if r["type"] in IMPLIED_TYPES:
                out.add(rid)
            elif r["type"] == "microsoft.network/publicipaddresses" and p.get("ipConfiguration"):
                out.add(rid)
            elif r["type"] == "microsoft.network/networksecuritygroups" and (p.get("subnets") or p.get("networkInterfaces")):
                out.add(rid)
        return out

    def vnet_node(self, vid, rg_name, in_subnet, at_vnet, replicas, max_per_subnet) -> Node:
        t, f = self.t, self.f
        v = t.vnets[vid]
        pv = props(v)
        cidr = ", ".join(g(pv, "addressSpace", "addressPrefixes", default=[]) or [])
        dns = ", ".join(g(pv, "dhcpOptions", "dnsServers", default=[]) or []) or "Azure DNS"
        all_peers = sorted({t.vnets[b]["name"] for a, b, st in t.peerings if a == vid and b in t.vnets and st == "Connected"})
        peers = [n for n in all_peers if f.by_name.get(lid(n)) in self.view_vnets]
        hidden = len(all_peers) - len(peers)
        uses_gw = any(g(pp, "properties", "useRemoteGateways") for pp in pv.get("virtualNetworkPeerings") or [])
        sub = f"{cidr} · {region_name(v.get('location'))} · DNS {dns}"
        if all_peers:
            sub += " · peered: " + (", ".join(peers) or "-") + (f" (+{hidden} out of scope)" if hidden else "") + \
                   (" · uses remote gateway" if uses_gw else "")
        else:
            sub += " · not peered"
        node = Node("vnet", f"Virtual network · {v['name']}", sub, key=vid, cell="subnet", max_inner=MAX_INNER["vnet"])
        for sid in t.subnet_order.get(vid, []):
            sn = t.subnets[sid]
            rids = in_subnet.get(sid, [])
            flags = " · ".join(z for z in ("NSG" if sn["nsg"] else "",
                                            ", ".join(d.split("/")[-1] for d in sn["delegations"] if d)) if z)
            items = self.items_for(rids, rg_name, 1 if len(rids) > max_per_subnet else 10 ** 6, 4) + replicas.get(sid, [])
            node.children.append(Node("subnet", f"Subnet · {sn['name']}",
                                      sn["prefix"] + (f" · {flags}" if flags else "") + ("" if items else " · empty"),
                                      key=sid, items=items, cell="subnet"))
        node.items = self.items_for(at_vnet.get(vid, []), rg_name, 3, 3) + replicas.get(vid, [])
        return node

    # ------------------------------------------------------------------ drawing
    def draw_node(self, n: Node) -> None:
        fill, stroke, sw, dashed = BOX[n.kind]
        cid = self.p._vertex("", f"rounded=1;arcSize=1.5;absoluteArcSize=1;html=1;fillColor={fill};strokeColor={stroke};"
                                 f"strokeWidth={sw};dashed={1 if dashed else 0};shadow={1 if n.kind in ('sub', 'vnet') else 0};",
                             n.x, n.y, n.w, n.h, self.L_bg)
        if n.key:
            self.cells[n.key] = cid
        self.p._vertex("", f"image;html=1;aspect=fixed;image={AZ}{BOX_ICON[n.kind]};", n.x + 8, n.y + 7, 24, 24, self.L_fg)
        size = {"sub": 14, "rg": 12, "rg_dr": 12, "vnet": 12, "subnet": 10.5}[n.kind]
        small = size - 3 if n.kind == "vnet" else size - 2.5
        label = f"<b>{esc(breakable(n.title))}</b>" + \
                (f"<br><font style='font-size:{small}px' color='#475569'>{esc(n.sub)}</font>" if n.sub else "")
        self.p._vertex(label, f"text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;fontFamily={FONT};fontSize={size};"
                              f"fontColor=#0F172A;labelBackgroundColor={fill};spacing=0;",
                       n.x + 36, n.y + 6, n.w - 46, HEADER[n.kind] - 10, self.L_fg)
        for c in n.children:
            self.draw_node(c)
        grid_y, cols = n._grid
        cell_w, cell_h, icon = CELL[n.cell]
        x0, y0 = n.x + PAD[n.kind], n.y + HEADER[n.kind] + grid_y
        for i, it in enumerate(n.items):
            r_, c_ = divmod(i, cols)
            cid = self.icon(x0 + c_ * cell_w + (cell_w - icon) / 2, y0 + r_ * cell_h + 6, it.img, it.title, it.lines,
                            fill, it.tip, icon, cell_w - 8, it.opacity)
            for rid in it.rids:
                self.cells[rid] = cid
            if it.key:
                self.cells[it.key] = cid

    def icon(self, x, y, img, title, lines, bg="#FFFFFF", tip="", size=48, label_w=142, opacity=None, full_path=False) -> str:
        label = f"<b>{esc(breakable(title))}</b>" + \
                "".join(f"<br><font style='font-size:9.5px' color='{c}'>{esc(breakable(t))}</font>" for t, c in lines if t)
        style = (f"image;html=1;aspect=fixed;image={img if full_path else AZ + img};shadow=1;verticalLabelPosition=bottom;"
                 f"verticalAlign=top;labelPosition=center;align=center;whiteSpace=wrap;labelWidth={label_w};fontFamily={FONT};"
                 f"fontSize=10.5;fontColor=#0F172A;labelBackgroundColor={bg};" + (f"opacity={opacity};" if opacity else ""))
        return self.p._vertex(label, style, x, y, size, size, self.L_fg, tip)

    def edge(self, a: Optional[str], b: Optional[str], kind: str, label: str = "") -> None:
        if not a or not b or a == b:
            return
        key = (min(a, b), max(a, b), kind) if kind in ("transit", "repl") else (a, b, kind)
        if key in self.edges_seen:
            return
        self.edges_seen.add(key)
        self.used_kinds.add(kind)
        color, extra = EDGES[kind]
        self.p.routed_edge(a, b, color, [], label, 1, f"{extra}fontSize=10;fontStyle=1;labelBackgroundColor=#FFFFFF;",
                           parent=self.L_conn)

    def c(self, name_or_id: str) -> Optional[str]:
        """Cell of a resource given by name or ID."""
        key = lid(name_or_id)
        return self.cells.get(key) or self.cells.get(self.f.by_name.get(key, ""))

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        f, p = self.f, self.p
        self.L_bg = "1"
        self.L_conn = p.layer("conn", "Connections")
        self.L_fg = p.layer("fg", "Resources")

        subs = self.build_tree()
        for s in subs:
            measure(s)
        width = float(self.fv.get("width", 3000))
        positions, used_w, used_h = pack([(s.w, s.h) for s in subs], width, 40)
        total_w = max(used_w, 1400)

        # top band only when something reaches the estate from outside (P2S VPN or a declared control)
        self.p2s = [v for v in f.vpn() if v["pool"] and v["vnet"] in self.view_vnets]
        self.ctrl = next((c for c in self.cfg.get("declaredControls", []) if c.get("type") == "conditionalAccess"
                          and any(f.by_name.get(lid(a)) for a in c.get("appliesTo", []))), None)
        top = 300 if (self.p2s or self.ctrl) else 110
        for s, (x, y) in zip(subs, positions):
            place(s, 40 + x, top + y)
            self.draw_node(s)

        title = self.fv.get("title", "Focused architecture")
        p._vertex(f"<b style='font-size:24px'>{esc(title)}</b><br><span style='font-size:12px'>Subscription → Resource group → "
                  f"Virtual network → Subnet → Resources · generated from Azure Resource Graph · "
                  f"export {esc(f.exported[:16].replace('T', ' '))} UTC</span>",
                  f"rounded=1;arcSize=8;html=1;whiteSpace=wrap;fillColor=#0B3B8C;gradientColor=#2563EB;gradientDirection=east;"
                  f"strokeColor=none;shadow=1;fontColor=#FFFFFF;align=left;spacingLeft=24;fontFamily={FONT};",
                  0, 0, total_w + 80, 74, self.L_fg)
        self.top_band(total_w)
        self.connections()
        self.notes(top + used_h + 40)

    # ------------------------------------------------------------------ users / internet / declared control
    def top_band(self, total_w: float) -> None:
        self.lap_vpn = self.lap_web = self.inet = self.ca = None
        if not (self.p2s or self.ctrl):
            return
        ux, uy, uw, uh = total_w / 2 - 165, 95, 330, 160
        fill, stroke, sw, _ = BOX["users"]
        self.p._vertex("", f"rounded=1;arcSize=1.5;absoluteArcSize=1;html=1;fillColor={fill};strokeColor={stroke};strokeWidth={sw};shadow=1;",
                       ux, uy, uw, uh, self.L_bg)
        self.p._vertex("<b>Remote users</b>", f"text;html=1;align=left;verticalAlign=top;fontFamily={FONT};fontSize=12;"
                                              f"labelBackgroundColor={fill};", ux + 10, uy + 6, 200, 20, self.L_fg)
        if self.p2s:
            auth = self.p2s[0]["auth"]
            self.lap_vpn = self.icon(ux + 40, uy + 42, LAPTOP, "VPN client", [(f"{auth} sign-in" if auth else "P2S VPN", "#475569")],
                                     fill, size=52, full_path=True)
            self.inet = self.icon(ux - 260, uy + 40, "img/lib/active_directory/internet_cloud.svg", "Internet", [], "#FFFFFF",
                                  size=72, full_path=True)
        if self.ctrl:
            self.lap_web = self.icon(ux + 220, uy + 42, LAPTOP, "Browser", [("HTTPS", "#475569")], fill, size=52, full_path=True)
            self.ca = self.icon(ux + uw + 220, uy + 40, "security/Conditional_Access.svg", self.ctrl["name"],
                                [(x, "#6D28D9") for x in self.ctrl.get("diagramLines", [])] + [("declared control", "#94A3B8")],
                                "#FFFFFF", size=52, label_w=220)

    # ------------------------------------------------------------------ connections
    def connections(self) -> None:
        f, t, c = self.f, self.t, self.c
        # remote users: P2S VPN via the Internet; declared access control on the browser path
        for v in self.p2s:
            self.edge(self.lap_vpn, self.inet, "vpn")
            self.edge(self.inet, self.cells.get(v["id"]), "vpn", f"P2S VPN ({v['protocols']})" if v["protocols"] else "P2S VPN")
        if self.ctrl:
            self.edge(self.lap_web, self.ca, "ca", "HTTPS over Internet")
            for a in self.ctrl.get("appliesTo", []):
                self.edge(self.ca, c(a), "ca", self.ctrl.get("edgeLabel", ""))
        # site-to-site VPN / ExpressRoute
        for vng, other, label in t.hybrid:
            self.edge(self.cells.get(vng), self.cells.get(other), "vpn", label)
        # VNet peering (gateway transit when a side uses the remote gateway)
        for a, b, st in t.peerings:
            if st != "Connected" or a not in self.view_vnets or b not in self.view_vnets:
                continue
            transit = any(lid(g(pp, "properties", "remoteVirtualNetwork", "id")) in (a, b) and
                          (g(pp, "properties", "useRemoteGateways") or g(pp, "properties", "allowGatewayTransit"))
                          for vid in (a, b) for pp in props(t.vnets[vid]).get("virtualNetworkPeerings") or [])
            self.edge(self.cells.get(a), self.cells.get(b), "transit", "gateway transit" if transit else "VNet peering")
        # documented links (e.g. AD replication between domain controllers)
        for link in self.cfg.get("links", []):
            self.edge(c(link["from"]), c(link["to"]), "repl", link.get("label", ""))
        # private endpoints -> service, load balancer / app gateway -> back ends
        for pe, target, gid in t.private_links:
            self.edge(self.cells.get(pe), self.cells.get(target), "pe", f"Private Link ({gid})" if gid else "Private Link")
        for fe, be, label in t.backends:
            self.edge(self.cells.get(fe), self.cells.get(be), "backend", label)
        # AVD: workspace -> application group -> host pool -> session host
        for rid, r in f.res.items():
            p = props(r)
            if r["type"] == "microsoft.desktopvirtualization/applicationgroups":
                self.edge(self.cells.get(rid), self.cells.get(lid(p.get("hostPoolArmPath"))), "avd")
            elif r["type"] == "microsoft.desktopvirtualization/workspaces":
                for ag in p.get("applicationGroupReferences") or []:
                    self.edge(self.cells.get(rid), self.cells.get(lid(ag)), "avd")
        for sh in f.session_hosts:
            pool = lid(sh.get("id")).split("/sessionhosts/")[0]
            self.edge(self.cells.get(pool), self.cells.get(lid(g(sh, "properties", "resourceId", default=""))), "avd", "session host")
        # Azure Backup and Azure Site Recovery
        for b in f.backup_rows:
            self.edge(self.cells.get(b["source"]), self.cells.get(b["vault_id"]), "backup")
        for a in f.asr_rows:
            lbl = f"ASR {a['from']} → {a['to']}" + (f" · RPO {round((a['rpo'] or 0) / 60, 1)} min" if a.get("rpo") else "")
            self.edge(self.cells.get(a["vm_id"]), self.cells.get(f"replica:{a['vm_id']}"), "asr", lbl)
        # declared data flows (3D pipes)
        for flow in self.fv.get("declaredFlows", []):
            hops = [c(n) for n in flow.get("path", [])]
            labels = flow.get("labels", [])
            for i in range(len(hops) - 1):
                self.edge(hops[i], hops[i + 1], "data", labels[i] if i < len(labels) else "")

    # ------------------------------------------------------------------ notes + legend
    def notes(self, y: float) -> None:
        rows = [(k, l) for k, l in LEGEND if k in self.used_kinds]
        body = "".join(f"<font color='{EDGES[k][0]}'><b>━━━</b></font>&nbsp; {esc(l)}<br>" for k, l in rows)
        faded = "<font color='#B91C1C'>Faded VM</font> = DR replica (standby) · " if "asr" in self.used_kinds else ""
        self.p._vertex(f"<b style='font-size:13px'>Legend</b><br>{body}{faded}boxes follow Subscription → Resource group → VNet → Subnet",
                       f"rounded=1;arcSize=6;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=12;"
                       f"fillColor=#FFFFFF;strokeColor=#CBD5E1;shadow=1;fontFamily={FONT};fontSize=11;",
                       40, y, 560, 60 + 18 * len(rows), self.L_fg)
        notes = list(self.fv.get("notes", []))
        notes += [fl["note"] for fl in self.fv.get("declaredFlows", []) if fl.get("note")]
        if self.ctrl:
            notes.append(f"{self.ctrl['name']}: {self.ctrl.get('summary', '')} ({self.ctrl.get('source', 'declared control')})")
        if notes:
            body = "".join(f"<p style='margin:0 0 6px 0'>• {esc(n)}</p>" for n in notes)
            self.p._vertex(f"<b style='font-size:13px'>Notes</b><br>{body}",
                           f"rounded=1;arcSize=6;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=12;"
                           f"fillColor=#FFFBEB;strokeColor=#F59E0B;shadow=1;fontFamily={FONT};fontSize=11;",
                           630, y, 1200, 50 + 34 * len(notes), self.L_fg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Focused Subscription > RG > VNet > Subnet diagram")
    ap.add_argument("inventory")
    ap.add_argument("--config", help="config JSON with an optional focusView section (default: show everything)")
    ap.add_argument("-o", "--output", default="azure_focus_view", help="output name without extension")
    args = ap.parse_args()
    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    facts = Facts(inv, cfg)
    w = DrawioWriter(cfg.get("title", "Azure"))
    page = w.new_page(cfg.get("focusView", {}).get("pageName", "Focused view"))
    FocusView(facts, cfg, page).build()
    out = Path(args.output)
    w.save(out.with_suffix(".drawio"))
    write_html(out.with_suffix(".drawio"), out.with_suffix(".html"), cfg.get("focusView", {}).get("title", "Azure focused view"))
    print(f"Focused view -> {out.with_suffix('.drawio')} and {out.with_suffix('.html')}")


if __name__ == "__main__":
    main()
