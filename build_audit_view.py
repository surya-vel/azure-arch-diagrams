#!/usr/bin/env python3
"""
build_audit_view.py - One clean page for auditors: how the estate is connected, nothing more.

Layout (fixed grid, straight lines only):
    Remote staff ──VPN──► Connectivity hub
                              │ (private access over VPN)
        ┌─────────────┬───────┴──────┬──────────────┐
     card 1        card 2          card 3      (row A)
     card 4        card 5          card 6  ◄── Internet (optional)
    [ Backup ]  [ Disaster recovery ]  [ Monitoring ]   (protection row)

Cards, their key components and the arrows inside each card come from the "auditView" section
of the config. Names, IPs, regions, address ranges, backup and DR status are read from the
Azure export on every run.

Usage
  python build_audit_view.py azure_inventory.json --config solution_config.example.json -o my_azure_audit
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

from build_drawio import IMG, DrawioWriter, Page, esc, g, lid, props, type_info
from build_solution import Facts, region_name, write_html

FONT = "Segoe UI"
COL_W, COL_GAP, CARD_H, ROW_GAP = 600, 80, 300, 100
X0, HEAD_Y, ROW_A = 60, 100, 400
GREY = "#6B7280"


class AuditView:
    def __init__(self, facts: Facts, cfg: dict, page: Page):
        self.f, self.cfg, self.p = facts, cfg, page
        self.av = cfg.get("auditView", {})

    # ---------- primitives -------------------------------------------------------------
    def card(self, x, y, w, h, title, sub, fill, stroke) -> str:
        label = (f"<b style='font-size:15px'>{esc(title)}</b>"
                 + (f"<br><font style='font-size:10.5px' color='{GREY}'>{sub}</font>" if sub else ""))
        style = (f"rounded=1;arcSize=6;absoluteArcSize=1;html=1;whiteSpace=wrap;container=1;collapsible=0;"
                 f"fillColor={fill};strokeColor={stroke};strokeWidth=2;verticalAlign=top;align=left;"
                 f"spacingLeft=14;spacingTop=8;fontFamily={FONT};fontSize=12;fontColor=#111827;")
        return self.p._vertex(label, style, x, y, w, h, "1")

    def icon(self, parent: str, x, y, icon, title, lines: List[str], tooltip="", size=56) -> str:
        label = f"<b>{esc(title)}</b>" + "".join(f"<br><font style='font-size:10px' color='{GREY}'>{esc(l)}</font>" for l in lines if l)
        style = (f"aspect=fixed;html=1;image;image={IMG}{icon};verticalLabelPosition=bottom;verticalAlign=top;"
                 f"labelPosition=center;align=center;whiteSpace=wrap;labelWidth=170;fontFamily={FONT};fontSize=11;fontColor=#111827;")
        return self.p._vertex(label, style, x, y, size, size, parent, tooltip)

    def line(self, a, b, color, label="", both=False, points=None, exit_="exitX=1;exitY=0.5;", entry="entryX=0;entryY=0.5;",
             dashed=False, width=2.2):
        extra = f"{exit_}exitDx=0;exitDy=0;{entry}entryDx=0;entryDy=0;" + ("startArrow=classic;" if both else "") + \
                ("dashed=1;" if dashed else "") + "labelBackgroundColor=#FFFFFF;fontSize=10;"
        self.p.routed_edge(a, b, color, points or [], label, width, extra)

    # ---------- content helpers -----------------------------------------------------------
    def item_info(self, item: dict) -> Tuple[str, str, List[str], str]:
        """returns icon, title, detail lines, tooltip"""
        f = self.f
        if "resources" in item:                       # group of resources, e.g. several VMs
            ids = [f.by_name.get(lid(n)) for n in item["resources"]]
            ids = [i for i in ids if i]
            icon = item.get("icon") or (type_info(f.res[ids[0]])[0] if ids else "general/All_Resources.svg")
            running = sum(1 for i in ids if f.vm.get(i, {}).get("power") == "running")
            vms = sum(1 for i in ids if i in f.vm)
            detail = f"{len(ids)} item(s)" + (f" · {running} running" if vms else "")
            return icon, item.get("label", ""), [item.get("role", ""), detail], "\n".join(f.res[i]["name"] for i in ids)
        if "resource" in item:
            rid = f.by_name.get(lid(item["resource"]))
            if not rid:
                return item.get("icon", "general/All_Resources.svg"), item.get("label", item["resource"]), [item.get("role", ""), "(not found in export)"], ""
            r = f.res[rid]
            icon = item.get("icon") or type_info(r)[0]
            lines = [item.get("role") or type_info(r)[1]]
            if rid in f.vm:
                v = f.vm[rid]
                lines.append(" · ".join(x for x in (", ".join(v["ips"]), region_name(r.get("location")),
                                                    v["power"] if v["power"] != "running" else "") if x))
            else:
                lines.append(region_name(r.get("location")))
            if item.get("note"):
                lines.append(item["note"])
            return icon, item.get("label", r["name"]), lines, f"{r['name']}\n{r['type']}\n{r.get('id')}"
        return item.get("icon", "general/All_Resources.svg"), item.get("label", ""), [item.get("role", ""), item.get("note", "")], ""

    def workload_sub(self, card: dict) -> str:
        """regions · subscription · networks for a card, from its resources' VNets"""
        f, t = self.f, self.f.t
        ids = []
        for it in card.get("items", []):
            for n in ([it["resource"]] if "resource" in it else it.get("resources", [])):
                if f.by_name.get(lid(n)):
                    ids.append(f.by_name[lid(n)])
        regions = sorted({region_name(f.res[i].get("location")) for i in ids if f.res[i].get("location") not in (None, "global")})
        subs = sorted({t.sub_names.get(lid(f.res[i].get("subscriptionId")), "") for i in ids})
        vnets = []
        for i in ids:
            if i in t.placement:
                v = t.vnets[t.placement[i][0]]
                cidr = ", ".join(g(props(v), "addressSpace", "addressPrefixes", default=[]) or [])
                vnets.append(f"{v['name']} {cidr}")
        for n in card.get("networks", []):
            vid = f.by_name.get(lid(n))
            if vid in t.vnets:
                vnets.append(f"{t.vnets[vid]['name']} " + ", ".join(g(props(t.vnets[vid]), "addressSpace", "addressPrefixes", default=[]) or []))
        parts = [esc(" · ".join(regions)), esc(", ".join(subs))]
        sub = " · ".join(p for p in parts if p)
        if vnets:
            sub += "<br>Network: " + esc(" · ".join(dict.fromkeys(vnets)))
        if card.get("summary"):
            sub += f"<br><font color='#111827'>{esc(card['summary'])}</font>"
        return sub

    def fill_card(self, cid: str, card: dict, w: float, top: float = 112) -> Dict[int, str]:
        items = card.get("items", [])
        n = max(1, len(items))
        slot = (w - 40) / n
        ids = {}
        for i, it in enumerate(items):
            icon, title, lines, tip = self.item_info(it)
            ids[i] = self.icon(cid, 20 + slot * i + slot / 2 - 28, top, icon, title, lines, tip)
        for fl in card.get("flows", []):
            a, b = fl[0], fl[1]
            if a in ids and b in ids:
                fwd = a < b      # arrows always connect neighbours left/right, never through another icon
                self.line(ids[a], ids[b], "#374151", fl[2] if len(fl) > 2 else "", both=bool(fl[3]) if len(fl) > 3 else False,
                          width=1.8, exit_="exitX=1;exitY=0.5;" if fwd else "exitX=0;exitY=0.5;",
                          entry="entryX=0;entryY=0.5;" if fwd else "entryX=1;entryY=0.5;")
        return ids

    # ---------- page ------------------------------------------------------------------------
    def build(self) -> None:
        f, p, av = self.f, self.p, self.av
        title = av.get("title", self.cfg.get("title", "Azure architecture") + " – overview")
        p.text(f"<b style='font-size:26px'>{esc(title)}</b><br><font color='{GREY}' style='font-size:12px'>"
               f"Current state from Azure Resource Graph · {esc(f.exported[:10])} · {len(f.t.subs)} subscriptions · "
               f"{len(f.t.vnets)} virtual networks · {len(f.vm)} virtual machines</font>", X0, 0, 1800, 70, size=12)

        # users + hub
        users = self.card(X0, HEAD_Y, 230, 200, "Remote staff", "", "#F0FDF4", "#16A34A")
        u_icon = self.icon(users, 87, 70, "identity/Users.svg", "Employees", ["Azure VPN Client"])
        hub_cfg = av.get("hub", {})
        vpn = f.vpn()[0] if f.vpn() else None
        hub_x = X0 + 230 + 150
        hub_w = COL_W
        hub_sub = ""
        if vpn:
            hub_sub = (f"{esc(vpn['region'])} · P2S VPN ({esc(vpn['protocols'])}) · sign-in with {esc(vpn['auth'])}"
                       f"<br>VPN client range {esc(vpn['pool'])} · shared to the workload networks")
        hub = self.card(hub_x, HEAD_Y, hub_w, 200, hub_cfg.get("title", "Connectivity hub"), hub_sub, "#EFF6FF", "#0078D4")
        hub_items = self.fill_card(hub, hub_cfg, hub_w, top=96)
        self.line(u_icon, hub, "#16A34A", "Encrypted VPN", width=2.6, exit_="exitX=1;exitY=0.5;", entry="entryX=0;entryY=0.5;")
        _ = hub_items

        # workload cards
        cards = av.get("cards", [])
        hx, hy = hub_x + hub_w / 2, HEAD_Y + 200
        bus_y = (hy + ROW_A) / 2
        row_b = ROW_A + CARD_H + ROW_GAP
        internet_target = None
        for k, card in enumerate(cards[:6]):
            row, col = divmod(k, 3)
            x = X0 + col * (COL_W + COL_GAP)
            y = ROW_A if row == 0 else row_b
            fill, stroke = card.get("fill", "#F8FAFC"), card.get("stroke", "#94A3B8")
            cid = self.card(x, y, COL_W, CARD_H, card["name"], self.workload_sub(card), fill, stroke)
            self.fill_card(cid, card, COL_W)
            cx = x + COL_W / 2
            if card.get("viaVpn", True):
                if row == 0:
                    pts = [(hx, bus_y), (cx, bus_y)]
                else:
                    gap_x = x - COL_GAP / 2
                    pts = [(hx, bus_y), (gap_x, bus_y), (gap_x, y - 30), (cx, y - 30)]
                self.line(hub, cid, "#0078D4", "", points=pts, exit_="exitX=0.5;exitY=1;", entry="entryX=0.5;entryY=0;", width=2.2)
            if card.get("internet"):
                internet_target = (cid, x + COL_W, y, card["internet"])
        p.text("<font color='#0078D4'><b>Private access over the VPN</b> (gateway transit)</font>", hx + 10, bus_y - 22, 330, 20, size=11)

        if internet_target:
            cid, rx, ry, label = internet_target
            ctrl = next((c for c in self.cfg.get("declaredControls", []) if c.get("type") == "conditionalAccess"), None)
            inet_x = rx + (330 if ctrl else 110)
            inet = p._vertex("<b>Internet</b><br><font style='font-size:10px' color='#6B7280'>users on managed devices</font>",
                             f"aspect=fixed;html=1;image;image={IMG}general/Globe.svg;verticalLabelPosition=bottom;verticalAlign=top;"
                             f"labelPosition=center;align=center;fontFamily={FONT};fontSize=11;", inet_x, ry + CARD_H / 2 - 28, 56, 56, "1")
            if ctrl:
                ca = p._vertex(f"<b>{esc(ctrl['name'])}</b>" + "".join(f"<br><font style='font-size:10px' color='#6D28D9'>{esc(x)}</font>"
                                                                       for x in ctrl.get("diagramLines", [])),
                               f"aspect=fixed;html=1;image;image={IMG}security/Conditional_Access.svg;verticalLabelPosition=bottom;verticalAlign=top;"
                               f"labelPosition=center;align=center;whiteSpace=wrap;labelWidth=200;shadow=1;fontFamily={FONT};fontSize=11;",
                               rx + 140, ry + CARD_H / 2 - 30, 60, 60, "1")
                self.line(inet, ca, "#7C3AED", "", exit_="exitX=0;exitY=0.5;", entry="entryX=1;entryY=0.5;", dashed=True)
                self.line(ca, cid, "#7C3AED", "HTTPS", exit_="exitX=0;exitY=0.5;", entry="entryX=1;entryY=0.5;", dashed=True)
            else:
                self.line(inet, cid, "#D97706", label, exit_="exitX=0;exitY=0.5;", entry="entryX=1;entryY=0.5;", dashed=True)

        # protection row
        row_c = row_b + CARD_H + ROW_GAP
        p.text("<b style='font-size:16px'>Protection &amp; operations</b>", X0, row_c - 40, 600, 30)
        self.protection(row_c)
        self.facts_panel(X0 + 3 * (COL_W + COL_GAP) + 420, HEAD_Y)

    def protection(self, y: float) -> None:
        f = self.f
        # Backup
        kinds = Counter(b["kind"] for b in f.backup_rows)
        ok = sum(1 for b in f.backup_rows if b["status"] == "Completed")
        last = max((b["last"] for b in f.backup_rows if b["last"]), default="")
        what = ", ".join(f"{n} {('VMs' if k == 'VM' else 'file shares' if k == 'AzureFileShare' else k)}" for k, n in kinds.items())
        vaults = sorted({b["vault"] for b in f.backup_rows})
        c1 = self.card(X0, y, COL_W, 230, "Azure Backup", f"{len(f.backup_rows)} items protected: {esc(what)}", "#F9FAFB", "#6B7280")
        self.icon(c1, 40, 100, "management_governance/Recovery_Services_Vaults.svg", f"{len(vaults)} backup vaults",
                  ["Daily backup policies", f"Last run: {ok}/{len(f.backup_rows)} successful", f"Latest: {last[:10]}"])
        names = sorted({f.res.get(b['source'], {}).get('name', b['item']) for b in f.backup_rows})
        self.p._vertex(f"<font style='font-size:11px'><b>Protected</b><br>{esc(', '.join(names))}</font>",
                       f"text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontFamily={FONT};", 270, 70, 310, 150, c1)
        # DR
        c2 = self.card(X0 + COL_W + COL_GAP, y, COL_W, 230, "Disaster recovery (Azure Site Recovery)",
                       f"{len(f.asr_rows)} servers continuously replicated to a second region", "#FFF1F2", "#E11D48")
        rows = "".join(f"<br><b>{esc(a['vm'])}</b>: {esc(a['from'])} → {esc(a['to'])} · RPO {round((a['rpo'] or 0) / 60, 1)} min · {esc(a['health'] or '')}"
                       for a in f.asr_rows)
        self.icon(c2, 40, 100, "management_governance/Recovery_Services_Vaults.svg", "Site Recovery", ["Replicas in standby networks"])
        self.p._vertex(f"<font style='font-size:11px'><b>Replicated servers</b>{rows}</font>",
                       f"text;html=1;whiteSpace=wrap;align=left;verticalAlign=top;fontFamily={FONT};", 230, 80, 350, 140, c2)
        # Monitoring
        mon = self.av.get("monitoring", {})
        c3 = self.card(X0 + 2 * (COL_W + COL_GAP), y, COL_W, 230, mon.get("title", "Monitoring & alerting"), esc(mon.get("summary", "")),
                       "#F9FAFB", "#6B7280")
        ids = {}
        for i, it in enumerate(mon.get("items", [])[:3]):
            icon, title, lines, tip = self.item_info(it)
            ids[i] = self.icon(c3, 20 + (COL_W - 40) / 3 * i + (COL_W - 40) / 6 - 28, 100, icon, title, lines, tip)

    def facts_panel(self, x: float, y: float) -> None:
        f = self.f
        vpn = f.vpn()[0] if f.vpn() else {}
        facts = list(self.av.get("keyFacts", []))
        if vpn:
            facts.insert(0, f"Staff reach the corporate networks through a point-to-site VPN with {vpn.get('auth')} sign-in.")
        for c in self.cfg.get("declaredControls", []):
            facts.append(f"{c.get('name')}: {c.get('summary')}")
        pe = len(f.t.private_links)
        if pe:
            facts.append(f"{pe} private endpoints connect file shares and storage privately to the networks.")
        if f.backup_rows:
            facts.append(f"{len(f.backup_rows)} backup items; last backups {'all successful' if all(b['status'] == 'Completed' for b in f.backup_rows) else 'need review'}.")
        if f.asr_rows:
            facts.append(f"{len(f.asr_rows)} servers replicated to a secondary region with Azure Site Recovery.")
        body = "".join(f"<p style='margin:0 0 8px 0'>• {esc(x)}</p>" for x in facts)
        legend = ("<br><b>Legend</b><br><font color='#16A34A'>━━</font> staff VPN connection<br>"
                  "<font color='#0078D4'>━━</font> private network access over VPN<br>"
                  "<font color='#374151'>━━</font> connection inside a workload<br>"
                  "<font color='#7C3AED'>╍╍</font> Internet access through Entra ID Conditional Access")
        self.p._vertex(f"<b style='font-size:15px'>Key facts</b><br><br>{body}{legend}",
                       f"rounded=1;arcSize=4;absoluteArcSize=1;html=1;whiteSpace=wrap;align=left;verticalAlign=top;spacing=14;"
                       f"fillColor=#FFFBEB;strokeColor=#F59E0B;fontFamily={FONT};fontSize=12;", x, y, 440, 230 + sum(19 * (1 + len(t) // 52) + 8 for t in facts), "1")


def main() -> None:
    ap = argparse.ArgumentParser(description="One-page auditor view of the Azure estate")
    ap.add_argument("inventory")
    ap.add_argument("--config", required=True)
    ap.add_argument("-o", "--output", default="azure_audit_view")
    args = ap.parse_args()
    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    facts = Facts(inv, cfg)
    w = DrawioWriter(cfg.get("title", "Azure"))
    page = w.new_page("Architecture overview")
    AuditView(facts, cfg, page).build()
    out = Path(args.output)
    w.save(out.with_suffix(".drawio"))
    write_html(out.with_suffix(".drawio"), out.with_suffix(".html"), cfg.get("auditView", {}).get("title", "Azure overview"))
    print(f"Audit view -> {out.with_suffix('.drawio')} and {out.with_suffix('.html')}")


if __name__ == "__main__":
    main()
