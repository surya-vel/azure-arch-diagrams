#!/usr/bin/env python3
"""
build_drawio.py - Turn azure_inventory.json into an editable draw.io architecture diagram.

Pages produced (each optional):
  1. Overview   - management groups -> subscriptions with resource counts
  2. Network    - Internet / on-prem -> hub VNets -> spoke VNets -> subnets -> resources,
                  plus PaaS reached via Private Endpoints / Service Endpoints, with
                  peering, private-link, load-balancer, UDR->firewall, VPN/ER and Internet flows
  3. One page per subscription - resource groups with every resource (grouped by type when busy)

Also writes <name>_ai_context.md : a compact, optionally anonymised description of the
estate that you can paste into Copilot / Claude to produce logical or data-flow views.

Usage
  python build_drawio.py azure_inventory.json
  python build_drawio.py azure_inventory.json -o our_azure.drawio --subscriptions "Prod-Clinical" "Connectivity"
  python build_drawio.py azure_inventory.json --pages network --hide-empty-subnets
  python build_drawio.py azure_inventory.json --anonymize      # AI context with names/IPs masked

No Graphviz or other system tools needed - only the Python standard library.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

# --------------------------------------------------------------------------------------
# Icon catalogue (paths verified against draw.io's built-in img/lib/azure2 library)
# --------------------------------------------------------------------------------------
IMG = "img/lib/azure2/"
TYPE_INFO: Dict[str, Tuple[str, str]] = {
    # networking
    "microsoft.network/virtualnetworks": ("networking/Virtual_Networks.svg", "Virtual network"),
    "microsoft.network/azurefirewalls": ("networking/Firewalls.svg", "Azure Firewall"),
    "microsoft.network/firewallpolicies": ("networking/Azure_Firewall_Policy.svg", "Firewall policy"),
    "microsoft.network/applicationgateways": ("networking/Application_Gateways.svg", "Application Gateway"),
    "microsoft.network/applicationgatewaywebapplicationfirewallpolicies": ("networking/Web_Application_Firewall_Policies_WAF.svg", "WAF policy"),
    "microsoft.network/bastionhosts": ("networking/Bastions.svg", "Bastion"),
    "microsoft.network/virtualnetworkgateways": ("networking/Virtual_Network_Gateways.svg", "VNet gateway"),
    "microsoft.network/localnetworkgateways": ("networking/Local_Network_Gateways.svg", "On-prem (local network gateway)"),
    "microsoft.network/connections": ("networking/Connections.svg", "Gateway connection"),
    "microsoft.network/expressroutecircuits": ("networking/ExpressRoute_Circuits.svg", "ExpressRoute circuit"),
    "microsoft.network/loadbalancers": ("networking/Load_Balancers.svg", "Load balancer"),
    "microsoft.network/publicipaddresses": ("networking/Public_IP_Addresses.svg", "Public IP"),
    "microsoft.network/publicipprefixes": ("networking/Public_IP_Prefixes.svg", "Public IP prefix"),
    "microsoft.network/privateendpoints": ("networking/Private_Endpoint.svg", "Private endpoint"),
    "microsoft.network/privatelinkservices": ("networking/Private_Link_Service.svg", "Private Link service"),
    "microsoft.network/privatednszones": ("networking/DNS_Zones.svg", "Private DNS zone"),
    "microsoft.network/dnszones": ("networking/DNS_Zones.svg", "DNS zone"),
    "microsoft.network/dnsresolvers": ("networking/DNS_Private_Resolver.svg", "DNS private resolver"),
    "microsoft.network/natgateways": ("networking/NAT.svg", "NAT gateway"),
    "microsoft.network/networksecuritygroups": ("networking/Network_Security_Groups.svg", "NSG"),
    "microsoft.network/applicationsecuritygroups": ("security/Application_Security_Groups.svg", "ASG"),
    "microsoft.network/routetables": ("networking/Route_Tables.svg", "Route table"),
    "microsoft.network/networkinterfaces": ("networking/Network_Interfaces.svg", "NIC"),
    "microsoft.network/networkwatchers": ("networking/Network_Watcher.svg", "Network Watcher"),
    "microsoft.network/frontdoors": ("networking/Front_Doors.svg", "Front Door (classic)"),
    "microsoft.network/frontdoorwebapplicationfirewallpolicies": ("networking/Web_Application_Firewall_Policies_WAF.svg", "Front Door WAF policy"),
    "microsoft.cdn/profiles": ("networking/Front_Doors.svg", "Front Door / CDN"),
    "microsoft.network/trafficmanagerprofiles": ("networking/Traffic_Manager_Profiles.svg", "Traffic Manager"),
    "microsoft.network/virtualwans": ("networking/Virtual_WANs.svg", "Virtual WAN"),
    "microsoft.network/virtualhubs": ("networking/Virtual_WAN_Hub.svg", "Virtual WAN hub"),
    "microsoft.network/ddosprotectionplans": ("networking/DDoS_Protection_Plans.svg", "DDoS plan"),
    "microsoft.network/ipgroups": ("networking/IP_Groups.svg", "IP group"),
    "microsoft.network/serviceendpointpolicies": ("networking/Service_Endpoint_Policies.svg", "Service endpoint policy"),
    # compute
    "microsoft.compute/virtualmachines": ("compute/Virtual_Machine.svg", "Virtual machine"),
    "microsoft.compute/virtualmachinescalesets": ("compute/VM_Scale_Sets.svg", "VM scale set"),
    "microsoft.compute/disks": ("compute/Disks.svg", "Managed disk"),
    "microsoft.compute/snapshots": ("compute/Disks_Snapshots.svg", "Snapshot"),
    "microsoft.compute/availabilitysets": ("compute/Availability_Sets.svg", "Availability set"),
    "microsoft.compute/galleries": ("compute/Azure_Compute_Galleries.svg", "Compute gallery"),
    "microsoft.compute/images": ("compute/Images.svg", "Image"),
    "microsoft.compute/diskencryptionsets": ("compute/Disk_Encryption_Sets.svg", "Disk encryption set"),
    "microsoft.compute/hostgroups": ("compute/Host_Groups.svg", "Dedicated host group"),
    "microsoft.desktopvirtualization/hostpools": ("compute/Host_Pools.svg", "AVD host pool"),
    "microsoft.desktopvirtualization/applicationgroups": ("compute/Application_Group.svg", "AVD app group"),
    "microsoft.desktopvirtualization/workspaces": ("compute/Workspaces.svg", "AVD workspace"),
    "microsoft.batch/batchaccounts": ("compute/Batch_Accounts.svg", "Batch account"),
    "microsoft.hybridcompute/machines": ("management_governance/Arc_Machines.svg", "Arc server"),
    # containers / app hosting
    "microsoft.containerservice/managedclusters": ("containers/Kubernetes_Services.svg", "AKS cluster"),
    "microsoft.containerregistry/registries": ("containers/Container_Registries.svg", "Container registry"),
    "microsoft.containerinstance/containergroups": ("containers/Container_Instances.svg", "Container instance"),
    "microsoft.app/managedenvironments": ("other/Container_App_Environments.svg", "Container Apps env"),
    "microsoft.app/containerapps": ("other/Worker_Container_App.svg", "Container app"),
    "microsoft.web/sites": ("app_services/App_Services.svg", "App Service"),
    "microsoft.web/serverfarms": ("app_services/App_Service_Plans.svg", "App Service plan"),
    "microsoft.web/staticsites": ("preview/Static_Apps.svg", "Static Web App"),
    "microsoft.web/hostingenvironments": ("app_services/App_Service_Environments.svg", "App Service Environment"),
    "microsoft.web/connections": ("devops/API_Connections.svg", "API connection"),
    "microsoft.web/certificates": ("app_services/App_Service_Certificates.svg", "App Service certificate"),
    # data
    "microsoft.sql/servers": ("databases/SQL_Server.svg", "Azure SQL server"),
    "microsoft.sql/servers/databases": ("databases/SQL_Database.svg", "Azure SQL database"),
    "microsoft.sql/servers/elasticpools": ("databases/SQL_Elastic_Pools.svg", "SQL elastic pool"),
    "microsoft.sql/managedinstances": ("databases/SQL_Managed_Instance.svg", "SQL Managed Instance"),
    "microsoft.sql/managedinstances/databases": ("databases/Managed_Database.svg", "SQL MI database"),
    "microsoft.sqlvirtualmachine/sqlvirtualmachines": ("databases/Azure_SQL_VM.svg", "SQL VM"),
    "microsoft.dbforpostgresql/flexibleservers": ("databases/Azure_Database_PostgreSQL_Server.svg", "PostgreSQL flexible"),
    "microsoft.dbforpostgresql/servers": ("databases/Azure_Database_PostgreSQL_Server.svg", "PostgreSQL"),
    "microsoft.dbformysql/flexibleservers": ("databases/Azure_Database_MySQL_Server.svg", "MySQL flexible"),
    "microsoft.dbformysql/servers": ("databases/Azure_Database_MySQL_Server.svg", "MySQL"),
    "microsoft.documentdb/databaseaccounts": ("databases/Azure_Cosmos_DB.svg", "Cosmos DB"),
    "microsoft.cache/redis": ("databases/Cache_Redis.svg", "Redis cache"),
    "microsoft.cache/redisenterprise": ("databases/Azure_Managed_Redis.svg", "Managed Redis"),
    "microsoft.kusto/clusters": ("databases/Azure_Data_Explorer_Clusters.svg", "Data Explorer"),
    "microsoft.datafactory/factories": ("databases/Data_Factory.svg", "Data Factory"),
    "microsoft.synapse/workspaces": ("analytics/Azure_Synapse_Analytics.svg", "Synapse workspace"),
    "microsoft.synapse/workspaces/sqlpools": ("databases/SQL_Data_Warehouses.svg", "Synapse SQL pool"),
    "microsoft.synapse/workspaces/bigdatapools": ("analytics/Azure_Synapse_Analytics.svg", "Synapse Spark pool"),
    "microsoft.databricks/workspaces": ("analytics/Azure_Databricks.svg", "Databricks workspace"),
    "microsoft.databricks/accessconnectors": ("analytics/Azure_Databricks.svg", "Databricks access connector"),
    "microsoft.purview/accounts": ("databases/Azure_Purview_Accounts.svg", "Purview"),
    "microsoft.powerbidedicated/capacities": ("analytics/Power_BI_Embedded.svg", "Power BI capacity"),
    "microsoft.analysisservices/servers": ("analytics/Analysis_Services.svg", "Analysis Services"),
    "microsoft.streamanalytics/streamingjobs": ("analytics/Stream_Analytics_Jobs.svg", "Stream Analytics"),
    "microsoft.eventhub/namespaces": ("analytics/Event_Hubs.svg", "Event Hubs"),
    "microsoft.hdinsight/clusters": ("analytics/HD_Insight_Clusters.svg", "HDInsight"),
    # storage / backup
    "microsoft.storage/storageaccounts": ("storage/Storage_Accounts.svg", "Storage account"),
    "microsoft.netapp/netappaccounts": ("storage/Azure_NetApp_Files.svg", "NetApp Files"),
    "microsoft.storagesync/storagesyncservices": ("storage/Storage_Sync_Services.svg", "File Sync"),
    "microsoft.recoveryservices/vaults": ("management_governance/Recovery_Services_Vaults.svg", "Recovery Services vault"),
    "microsoft.dataprotection/backupvaults": ("other/Backup_Vault.svg", "Backup vault"),
    "microsoft.datashare/accounts": ("storage/Data_Shares.svg", "Data Share"),
    # security / identity
    "microsoft.keyvault/vaults": ("security/Key_Vaults.svg", "Key Vault"),
    "microsoft.keyvault/managedhsms": ("security/Keys.svg", "Managed HSM"),
    "microsoft.managedidentity/userassignedidentities": ("identity/Managed_Identities.svg", "Managed identity"),
    # integration
    "microsoft.servicebus/namespaces": ("integration/Service_Bus.svg", "Service Bus"),
    "microsoft.eventgrid/topics": ("integration/Event_Grid_Topics.svg", "Event Grid topic"),
    "microsoft.eventgrid/systemtopics": ("integration/System_Topic.svg", "Event Grid system topic"),
    "microsoft.eventgrid/domains": ("integration/Event_Grid_Domains.svg", "Event Grid domain"),
    "microsoft.logic/workflows": ("integration/Logic_Apps.svg", "Logic App"),
    "microsoft.logic/integrationaccounts": ("integration/Integration_Accounts.svg", "Integration account"),
    "microsoft.apimanagement/service": ("integration/API_Management_Services.svg", "API Management"),
    "microsoft.appconfiguration/configurationstores": ("integration/App_Configuration.svg", "App Configuration"),
    "microsoft.healthcareapis/workspaces": ("integration/Azure_API_for_FHIR.svg", "Health Data Services"),
    "microsoft.healthcareapis/workspaces/fhirservices": ("integration/Azure_API_for_FHIR.svg", "FHIR service"),
    "microsoft.healthcareapis/workspaces/dicomservices": ("integration/Azure_API_for_FHIR.svg", "DICOM service"),
    "microsoft.healthcareapis/services": ("integration/Azure_API_for_FHIR.svg", "Azure API for FHIR"),
    "microsoft.signalrservice/signalr": ("web/SignalR.svg", "SignalR"),
    "microsoft.devices/iothubs": ("iot/IoT_Hub.svg", "IoT Hub"),
    # AI
    "microsoft.cognitiveservices/accounts": ("ai_machine_learning/Cognitive_Services.svg", "AI services"),
    "microsoft.machinelearningservices/workspaces": ("ai_machine_learning/Machine_Learning.svg", "Azure ML workspace"),
    "microsoft.search/searchservices": ("app_services/Search_Services.svg", "AI Search"),
    "microsoft.botservice/botservices": ("ai_machine_learning/Bot_Services.svg", "Bot service"),
    # monitoring / management
    "microsoft.operationalinsights/workspaces": ("management_governance/Log_Analytics_Workspaces.svg", "Log Analytics"),
    "microsoft.insights/components": ("devops/Application_Insights.svg", "Application Insights"),
    "microsoft.insights/actiongroups": ("management_governance/Alerts.svg", "Action group"),
    "microsoft.insights/datacollectionrules": ("management_governance/Monitor.svg", "Data collection rule"),
    "microsoft.insights/workbooks": ("analytics/Azure_Workbooks.svg", "Workbook"),
    "microsoft.operationsmanagement/solutions": ("management_governance/Solutions.svg", "Monitoring solution"),
    "microsoft.automation/automationaccounts": ("management_governance/Automation_Accounts.svg", "Automation account"),
    "microsoft.dashboard/grafana": ("other/Grafana.svg", "Managed Grafana"),
    "microsoft.devtestlab/labs": ("devops/DevTest_Labs.svg", "DevTest Lab"),
    "microsoft.maintenance/maintenanceconfigurations": ("compute/Maintenance_Configuration.svg", "Maintenance config"),
    "microsoft.app/jobs": ("other/Worker_Container_App.svg", "Container Apps job"),
    "microsoft.solutions/applications": ("management_governance/Managed_Applications_Center.svg", "Managed app (Marketplace)"),
    "microsoft.monitor/accounts": ("management_governance/Monitor.svg", "Azure Monitor workspace"),
    "microsoft.insights/datacollectionendpoints": ("management_governance/Monitor.svg", "Data collection endpoint"),
    "microsoft.automation/automationaccounts/runbooks": ("management_governance/Automation_Accounts.svg", "Runbook"),
    "microsoft.cognitiveservices/accounts/projects": ("ai_machine_learning/Foundry_Project.svg", "AI Foundry project"),
    "microsoft.compute/sshpublickeys": ("security/Keys.svg", "SSH public key"),
    "microsoft.app/managedenvironments/certificates": ("app_services/App_Service_Certificates.svg", "Container Apps certificate"),
}
FALLBACK_ICON = "general/All_Resources.svg"

KIND_OVERRIDES = [  # (type, kind-substring, icon, label)
    ("microsoft.web/sites", "workflowapp", "integration/Logic_Apps.svg", "Logic App (Standard)"),
    ("microsoft.web/sites", "functionapp", "compute/Function_Apps.svg", "Function App"),
    ("microsoft.cognitiveservices/accounts", "openai", "ai_machine_learning/Azure_OpenAI.svg", "Azure OpenAI"),
    ("microsoft.cognitiveservices/accounts", "aiservices", "ai_machine_learning/AI_Foundry.svg", "AI Foundry / AI services"),
    ("microsoft.machinelearningservices/workspaces", "hub", "ai_machine_learning/AI_Foundry.svg", "AI Foundry hub"),
    ("microsoft.machinelearningservices/workspaces", "project", "ai_machine_learning/Foundry_Project.svg", "AI Foundry project"),
]

# Never drawn on the network page (implied by placement or too noisy)
HIDE_ON_NETWORK = {
    "microsoft.network/networkinterfaces", "microsoft.network/networksecuritygroups",
    "microsoft.network/routetables", "microsoft.network/publicipaddresses",
    "microsoft.network/networkwatchers", "microsoft.compute/disks",
    "microsoft.network/privatednszones/virtualnetworklinks",
    "microsoft.network/dnsresolvers/inboundendpoints", "microsoft.network/dnsresolvers/outboundendpoints",
    "microsoft.network/connections", "microsoft.network/virtualnetworks",
}
# Never drawn on subscription inventory pages
HIDE_ON_INVENTORY = {
    "microsoft.network/networkinterfaces", "microsoft.network/networkwatchers",
    "microsoft.network/privatednszones/virtualnetworklinks",
    "microsoft.network/dnsresolvers/inboundendpoints", "microsoft.network/dnsresolvers/outboundendpoints",
    "microsoft.compute/virtualmachines/extensions",
}
SPECIAL_SUBNETS = {"gatewaysubnet", "azurefirewallsubnet", "azurefirewallmanagementsubnet",
                   "azurebastionsubnet", "routeserversubnet"}
SUBNET_REF_KEYS = {"subnet", "subnetid", "subnetresourceid", "vnetsubnetid", "infrastructuresubnetid",
                   "delegatedsubnetresourceid", "virtualnetworksubnetid", "podsubnetid"}
SKIP_WALK_KEYS = {"virtualnetworkrules", "networkacls", "subnets", "ipsecurityrestrictions",
                  "scmipsecurityrestrictions", "privateendpointconnections"}
SUBNET_RE = re.compile(r"(/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.network/"
                       r"virtualnetworks/[^/]+/subnets/[^/\s\"']+)", re.I)

# --------------------------------------------------------------------------------------
# Visual constants
# --------------------------------------------------------------------------------------
ICON = 48
CELL_W, CELL_H = 132, 104
PAD, GAP = 16, 14
FONT = "Segoe UI"

C = {  # muted palette - icons carry the colour
    "azure": "#0078D4", "text2": "#6B7280", "warn": "#DC2626",
    "hub_fill": "#EAF3FB", "hub_stroke": "#0078D4",
    "spoke_fill": "#F5F9FD", "spoke_stroke": "#5B9BD5",
    "iso_fill": "#FAFAFA", "iso_stroke": "#9CA3AF",
    "sub_fill": "#FFFFFF", "sub_stroke": "#A0AEC0",
    "special_fill": "#FFF8E6", "special_stroke": "#C9A227",
    "band_fill": "none", "band_stroke": "#CBD5E1",
    "rg_fill": "#F9FAFB", "rg_stroke": "#9CA3AF",
    "subscr_fill": "#FFFDF5", "subscr_stroke": "#E3B505",
}
EDGE = {
    "peering": ("#0078D4", "strokeWidth=2;startArrow=classic;endArrow=classic;"),
    "privatelink": ("#E67E22", "dashed=1;strokeWidth=1.5;endArrow=classic;"),
    "service_endpoint": ("#9CA3AF", "dashed=1;dashPattern=1 3;strokeWidth=1.5;endArrow=classic;"),
    "backend": ("#2E7D32", "strokeWidth=1.5;endArrow=classic;"),
    "internet": ("#16A34A", "strokeWidth=2;endArrow=classic;"),
    "exposure": ("#DC2626", "dashed=1;strokeWidth=2;endArrow=classic;"),
    "hybrid": ("#7C3AED", "dashed=1;strokeWidth=2;startArrow=classic;endArrow=classic;"),
    "udr": ("#B45309", "dashed=1;dashPattern=6 3;strokeWidth=1.5;endArrow=classic;"),
}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def lid(x: Optional[str]) -> str:
    return (x or "").strip().lower()


def name_from_id(rid: str) -> str:
    return rid.rstrip("/").split("/")[-1] if rid else ""


def vnet_of_subnet(subnet_id: str) -> str:
    return subnet_id.split("/subnets/")[0]


def rg_of(rid: str) -> str:
    m = re.search(r"/resourcegroups/([^/]+)", rid, re.I)
    return m.group(1) if m else ""


def sub_of(rid: str) -> str:
    m = re.search(r"/subscriptions/([^/]+)", rid, re.I)
    return m.group(1).lower() if m else ""


def props(r: dict) -> dict:
    p = r.get("properties")
    return p if isinstance(p, dict) else {}


def g(d, *path, default=None):
    """Safe nested get: g(x, 'a', 0, 'b')."""
    cur = d
    for p in path:
        if isinstance(p, int):
            if not isinstance(cur, list) or len(cur) <= p:
                return default
            cur = cur[p]
        else:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(p)
        if cur is None:
            return default
    return cur


def breakable(s: str) -> str:
    """Let long Azure names wrap at - _ . boundaries."""
    return re.sub(r"([-_.])", "\\1\u200b", s)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def type_info(r: dict) -> Tuple[str, str]:
    t = r.get("type", "")
    kind = lid(r.get("kind"))
    for ot, ks, icon, label in KIND_OVERRIDES:
        if t == ot and ks in kind:
            return icon, label
    icon, label = TYPE_INFO.get(t, (FALLBACK_ICON, t.split("/", 1)[-1] if t else "resource"))
    return icon, label


def subtitle(r: dict) -> str:
    t, p, sku = r.get("type", ""), props(r), r.get("sku") or {}
    _, label = type_info(r)
    extra = ""
    if t == "microsoft.compute/virtualmachines":
        extra = g(p, "hardwareProfile", "vmSize", default="")
        os_ = g(p, "storageProfile", "osDisk", "osType", default="")
        extra = " ".join(x for x in (extra, os_) if x)
    elif t == "microsoft.compute/virtualmachinescalesets":
        extra = f"{sku.get('name', '')} ×{sku.get('capacity', '?')}"
    elif t == "microsoft.containerservice/managedclusters":
        nodes = sum(int(x.get("count") or 0) for x in p.get("agentPoolProfiles") or [])
        extra = f"k8s {p.get('kubernetesVersion', '')} · {nodes} nodes"
    elif t == "microsoft.network/privateendpoints":
        gids = {gid for c in (p.get("privateLinkServiceConnections") or []) + (p.get("manualPrivateLinkServiceConnections") or [])
                for gid in (g(c, "properties", "groupIds") or [])}
        extra = ",".join(sorted(gids))
    elif isinstance(sku, dict) and sku.get("name"):
        extra = str(sku.get("name"))
    return f"{label}" + (f" · {extra}" if extra else "")


def tooltip_for(r: dict, sub_names: Dict[str, str]) -> str:
    lines = [
        f"{r.get('name')}",
        f"Type: {r.get('type')}" + (f" ({r.get('kind')})" if r.get("kind") else ""),
        f"Subscription: {sub_names.get(lid(r.get('subscriptionId')), r.get('subscriptionId'))}",
        f"Resource group: {r.get('resourceGroup')}",
        f"Location: {r.get('location')}",
    ]
    sku = r.get("sku")
    if isinstance(sku, dict) and sku:
        lines.append("SKU: " + ", ".join(f"{k}={v}" for k, v in sku.items() if v not in (None, "")))
    pna = props(r).get("publicNetworkAccess")
    if pna:
        lines.append(f"Public network access: {pna}")
    tags = r.get("tags") or {}
    if isinstance(tags, dict) and tags:
        lines.append("Tags: " + "; ".join(f"{k}={v}" for k, v in list(tags.items())[:8]))
    lines.append(f"ID: {r.get('id')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Topology analysis
# --------------------------------------------------------------------------------------
class Topology:
    def __init__(self, inv: dict, sub_filter: Optional[List[str]] = None):
        self.subs = {lid(s.get("subscriptionId")): s for s in inv.get("subscriptions", [])}
        if sub_filter:
            wanted = {lid(x) for x in sub_filter}
            self.subs = {k: v for k, v in self.subs.items() if k in wanted or lid(v.get("name")) in wanted}
            if not self.subs:
                sys.exit(f"No subscription matched {sub_filter}")
        self.sub_names = {k: v.get("name") or k for k, v in self.subs.items()}
        self.rgs = [rg for rg in inv.get("resourceGroups", []) if lid(rg.get("subscriptionId")) in self.subs]
        self.all_res = {lid(r["id"]): r for r in inv.get("resources", []) if r.get("id")}  # incl. other subs (PE targets)
        self.res = {k: r for k, r in self.all_res.items() if lid(r.get("subscriptionId")) in self.subs}

        self.vnets: Dict[str, dict] = {}
        self.subnets: Dict[str, dict] = {}         # subnet id -> {name, prefix, vnet, nsg, udr, delegations, ...}
        self.subnet_order: Dict[str, List[str]] = defaultdict(list)
        self.placement: Dict[str, Tuple[str, Optional[str]]] = {}   # res id -> (vnet id, subnet id|None)
        self.nic_subnets: Dict[str, List[str]] = defaultdict(list)
        self.nic_owner: Dict[str, str] = {}
        self.nic_public: Set[str] = set()
        self.ip_owner: Dict[str, str] = {}         # private IP -> resource id (for UDR next hops)
        self.peerings: List[Tuple[str, str, str]] = []
        self.hubs: Set[str] = set()
        self.spokes: Set[str] = set()
        self.private_links: List[Tuple[str, str, str]] = []        # (pe id, target id, groupIds)
        self.service_endpoints: List[Tuple[str, str]] = []         # (subnet id, resource id)
        self.backends: List[Tuple[str, str, str]] = []             # (frontend res, backend res, label)
        self.internet_in: List[Tuple[str, str, str]] = []          # (res id, kind, label)
        self.hybrid: List[Tuple[str, str, str]] = []               # (vng id, onprem id, label)
        self.udr: Dict[Tuple[str, str], Set[str]] = defaultdict(set)  # (vnet id, nexthop res) -> prefixes
        self.vm_public: Set[str] = set()
        self._analyse()

    # -- pass 1: networks ---------------------------------------------------------------
    def _analyse(self) -> None:
        for rid, r in self.res.items():
            if r["type"] == "microsoft.network/virtualnetworks":
                self._index_vnet(rid, r)
        for rid, r in self.res.items():
            if r["type"] == "microsoft.network/networkinterfaces":
                self._index_nic(rid, r)
        for rid, r in self.res.items():
            self._place(rid, r)
        self._index_ips()
        for rid, r in self.res.items():
            self._relations(rid, r)
        self._classify_hubs()

    def _index_vnet(self, rid: str, r: dict) -> None:
        p = props(r)
        self.vnets[rid] = r
        for s in p.get("subnets") or []:
            sid = lid(s.get("id"))
            sp = s.get("properties") or {}
            prefix = sp.get("addressPrefix") or ", ".join(sp.get("addressPrefixes") or [])
            self.subnets[sid] = {
                "id": sid, "name": s.get("name") or name_from_id(sid), "prefix": prefix, "vnet": rid,
                "nsg": lid(g(sp, "networkSecurityGroup", "id")), "udr": lid(g(sp, "routeTable", "id")),
                "nat": lid(g(sp, "natGateway", "id")),
                "delegations": [g(d, "properties", "serviceName") for d in sp.get("delegations") or []],
                "service_endpoints": [x.get("service") for x in sp.get("serviceEndpoints") or []],
            }
            self.subnet_order[rid].append(sid)
        for pe in p.get("virtualNetworkPeerings") or []:
            pp = pe.get("properties") or {}
            remote = lid(g(pp, "remoteVirtualNetwork", "id"))
            if remote:
                self.peerings.append((rid, remote, pp.get("peeringState") or ""))

    def _index_nic(self, rid: str, r: dict) -> None:
        p = props(r)
        owner = lid(g(p, "virtualMachine", "id"))
        if owner:
            self.nic_owner[rid] = owner
        for ipc in p.get("ipConfigurations") or []:
            ip = ipc.get("properties") or {}
            sid = lid(g(ip, "subnet", "id"))
            if sid:
                self.nic_subnets[rid].append(sid)
            if g(ip, "publicIPAddress", "id"):
                self.nic_public.add(rid)
            if ip.get("privateIPAddress") and owner:
                self.ip_owner[ip["privateIPAddress"]] = owner

    # -- pass 2: where does each resource live? ----------------------------------------
    def _find_subnet_refs(self, obj, out: List[str], depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if kl in SKIP_WALK_KEYS:
                    continue
                if kl in SUBNET_REF_KEYS:
                    val = v.get("id") if isinstance(v, dict) else v
                    if isinstance(val, str):
                        m = SUBNET_RE.search(val)
                        if m:
                            out.append(m.group(1).lower())
                            continue
                self._find_subnet_refs(v, out, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                self._find_subnet_refs(v, out, depth + 1)

    def _set_place(self, rid: str, subnet_ids: List[str], vnet_hint: str = "") -> None:
        subnet_ids = [s for s in dict.fromkeys(subnet_ids) if s in self.subnets]
        if subnet_ids:
            vnets = {self.subnets[s]["vnet"] for s in subnet_ids}
            vnet = next(iter(vnets))
            in_vnet = [s for s in subnet_ids if self.subnets[s]["vnet"] == vnet]
            self.placement[rid] = (vnet, in_vnet[0] if len(in_vnet) == 1 else None)
        elif vnet_hint and vnet_hint in self.vnets:
            self.placement[rid] = (vnet_hint, None)

    def _place(self, rid: str, r: dict) -> None:
        t, p = r["type"], props(r)
        if t in ("microsoft.network/virtualnetworks", "microsoft.network/networkinterfaces",
                 "microsoft.network/networksecuritygroups", "microsoft.network/routetables"):
            return
        if t == "microsoft.compute/virtualmachines":
            subs = []
            for n in g(p, "networkProfile", "networkInterfaces", default=[]) or []:
                nid = lid(n.get("id"))
                subs += self.nic_subnets.get(nid, [])
                if nid in self.nic_public:
                    self.vm_public.add(rid)
            self._set_place(rid, subs)
            return
        if t == "microsoft.network/natgateways":
            self._set_place(rid, [lid(s.get("id")) for s in p.get("subnets") or []])
            if rid in self.placement:
                self.placement[rid] = (self.placement[rid][0], None)
            return
        if t == "microsoft.network/dnsresolvers":
            self._set_place(rid, [], lid(g(p, "virtualNetwork", "id")))
            return
        if t == "microsoft.databricks/workspaces":
            prm = p.get("parameters") or {}
            vnet = lid(g(prm, "customVirtualNetworkId", "value"))
            names = [g(prm, "customPublicSubnetName", "value"), g(prm, "customPrivateSubnetName", "value")]
            subs = [f"{vnet}/subnets/{lid(n)}" for n in names if n]
            self._set_place(rid, subs, vnet)
            return
        if t == "microsoft.network/loadbalancers":
            refs: List[str] = []
            self._find_subnet_refs(p.get("frontendIPConfigurations") or [], refs)
            if not refs:  # public LB: place where its backends live
                for pool in p.get("backendAddressPools") or []:
                    for bic in g(pool, "properties", "backendIPConfigurations", default=[]) or []:
                        nic = lid(bic.get("id")).split("/ipconfigurations/")[0]
                        refs += self.nic_subnets.get(nic, [])
                self._set_place(rid, refs)
                if rid in self.placement:
                    self.placement[rid] = (self.placement[rid][0], None)
                return
            self._set_place(rid, refs)
            return
        refs = []
        self._find_subnet_refs(p, refs)
        if refs:
            self._set_place(rid, refs)

    def _index_ips(self) -> None:
        for rid, r in self.res.items():
            p = props(r)
            if r["type"] in ("microsoft.network/azurefirewalls", "microsoft.network/loadbalancers"):
                key = "ipConfigurations" if r["type"].endswith("azurefirewalls") else "frontendIPConfigurations"
                for ipc in p.get(key) or []:
                    ip = g(ipc, "properties", "privateIPAddress")
                    if ip:
                        self.ip_owner[ip] = rid
                hub_ip = g(p, "hubIPAddresses", "privateIPAddress")
                if hub_ip:
                    self.ip_owner[hub_ip] = rid

    # -- pass 3: relationships -----------------------------------------------------------
    def _relations(self, rid: str, r: dict) -> None:
        t, p = r["type"], props(r)
        if t == "microsoft.network/privateendpoints":
            conns = (p.get("privateLinkServiceConnections") or []) + (p.get("manualPrivateLinkServiceConnections") or [])
            for c in conns:
                cp = c.get("properties") or {}
                target = lid(cp.get("privateLinkServiceId"))
                if target:
                    self.private_links.append((rid, target, ",".join(cp.get("groupIds") or [])))
        elif t == "microsoft.network/loadbalancers":
            public = any(g(f, "properties", "publicIPAddress", "id") for f in p.get("frontendIPConfigurations") or [])
            if public:
                self.internet_in.append((rid, "internet", "Public LB"))
            for pool in p.get("backendAddressPools") or []:
                for bic in g(pool, "properties", "backendIPConfigurations", default=[]) or []:
                    bid = lid(bic.get("id"))
                    target = None
                    if "/virtualmachinescalesets/" in bid:
                        target = bid.split("/virtualmachines/")[0].split("/networkinterfaces/")[0]
                    elif "/networkinterfaces/" in bid:
                        target = self.nic_owner.get(bid.split("/ipconfigurations/")[0])
                    if target:
                        self.backends.append((rid, target, "backend"))
        elif t == "microsoft.network/applicationgateways":
            if any(g(f, "properties", "publicIPAddress", "id") for f in p.get("frontendIPConfigurations") or []):
                self.internet_in.append((rid, "internet", "HTTPS (public)"))
            hostmap = self._host_index()
            for pool in p.get("backendAddressPools") or []:
                pp = pool.get("properties") or {}
                for addr in pp.get("backendAddresses") or []:
                    target = hostmap.get(lid(addr.get("fqdn"))) or self.ip_owner.get(addr.get("ipAddress") or "")
                    if target:
                        self.backends.append((rid, target, "backend"))
                for bic in pp.get("backendIPConfigurations") or []:
                    target = self.nic_owner.get(lid(bic.get("id")).split("/ipconfigurations/")[0])
                    if target:
                        self.backends.append((rid, target, "backend"))
        elif t == "microsoft.network/azurefirewalls":
            if any(g(c, "properties", "publicIPAddress", "id") for c in p.get("ipConfigurations") or []):
                self.internet_in.append((rid, "internet", "Egress / DNAT"))
        elif t == "microsoft.network/bastionhosts":
            self.internet_in.append((rid, "internet", "RDP/SSH over TLS 443"))
        elif t in ("microsoft.cdn/profiles", "microsoft.network/frontdoors", "microsoft.network/trafficmanagerprofiles"):
            self.internet_in.append((rid, "internet", "Global entry"))
        elif t == "microsoft.network/connections":
            vng = lid(g(p, "virtualNetworkGateway1", "id"))
            other = lid(g(p, "localNetworkGateway2", "id")) or lid(g(p, "peer", "id")) or lid(g(p, "virtualNetworkGateway2", "id"))
            if vng and other:
                label = f"{p.get('connectionType', '')} {p.get('connectionStatus', '') or ''}".strip()
                self.hybrid.append((vng, other, label))
        elif t == "microsoft.network/routetables":
            routes = [x.get("properties") or {} for x in p.get("routes") or []]
            for s in p.get("subnets") or []:
                sid = lid(s.get("id"))
                vnet = vnet_of_subnet(sid)
                for rt in routes:
                    if rt.get("nextHopType") == "VirtualAppliance":
                        nh = self.ip_owner.get(rt.get("nextHopIpAddress") or "")
                        if nh and vnet in self.vnets:
                            self.udr[(vnet, nh)].add(rt.get("addressPrefix") or "?")
        # service endpoints (storage, key vault, cosmos ...)
        rules = g(p, "networkAcls", "virtualNetworkRules") or p.get("virtualNetworkRules") or []
        if isinstance(rules, list):
            for rule in rules:
                sid = lid(rule.get("id") or g(rule, "properties", "virtualNetworkSubnetId"))
                if sid in self.subnets:
                    self.service_endpoints.append((sid, rid))
        if rid in self.vm_public:
            self.internet_in.append((rid, "exposure", "Public IP on VM"))

    def _host_index(self) -> Dict[str, str]:
        if not hasattr(self, "_hosts"):
            self._hosts = {}
            for rid, r in self.res.items():
                if r["type"] == "microsoft.web/sites":
                    p = props(r)
                    for h in [p.get("defaultHostName")] + list(p.get("hostNames") or []):
                        if h:
                            self._hosts[lid(h)] = rid
        return self._hosts

    def _classify_hubs(self) -> None:
        placed_types = defaultdict(set)
        for rid, (vnet, _) in self.placement.items():
            placed_types[vnet].add(self.res[rid]["type"])
        for vid, v in self.vnets.items():
            sub_names = {self.subnets[s]["name"].lower() for s in self.subnet_order[vid]}
            if ("hub" in lid(v.get("name"))
                    or sub_names & {"azurefirewallsubnet", "gatewaysubnet", "routeserversubnet"}
                    or placed_types[vid] & {"microsoft.network/azurefirewalls", "microsoft.network/virtualnetworkgateways"}):
                self.hubs.add(vid)
        for a, b, _ in self.peerings:
            if a in self.hubs and b not in self.hubs:
                self.spokes.add(b)
            if b in self.hubs and a not in self.hubs:
                self.spokes.add(a)
        self.spokes &= set(self.vnets)


# --------------------------------------------------------------------------------------
# Layout primitives (shelf packing) and draw.io emitter
# --------------------------------------------------------------------------------------
@dataclass
class Leaf:
    label: str
    icon: str
    tooltip: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    w: int = CELL_W
    h: int = CELL_H
    x: int = 0
    y: int = 0
    cell_id: str = ""
    extra_style: str = ""
    ax: float = 0
    ay: float = 0


@dataclass
class Box:
    label: str
    style: str
    children: List[Union["Box", Leaf]] = field(default_factory=list)
    cols: int = 4                   # max leaf columns when only leaves
    max_inner: Optional[int] = None  # max content width when it holds boxes
    header: int = 44
    icon: Optional[str] = None
    tooltip: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    min_w: int = 0
    gap: int = GAP
    pad: int = PAD
    w: int = 0
    h: int = 0
    x: int = 0
    y: int = 0
    cell_id: str = ""
    ax: float = 0
    ay: float = 0

    def layout(self) -> None:
        for c in self.children:
            if isinstance(c, Box):
                c.layout()
        gap, pad = self.gap, self.pad
        limit = self.max_inner or (self.cols * CELL_W + (self.cols - 1) * gap)
        x = y = row_h = used_w = 0
        for c in self.children:
            if x > 0 and x + c.w > limit:
                x, y, row_h = 0, y + row_h + gap, 0
            c.x, c.y = pad + x, self.header + y
            x += c.w + gap
            row_h = max(row_h, c.h)
            used_w = max(used_w, x - gap)
        text_w = int(len(re.sub(r"<[^>]+>", "", self.label.split("<br>")[0])) * 7.2) + (60 if self.icon else 30)
        self.w = max(pad * 2 + used_w, self.min_w, text_w, 180)
        self.h = self.header + (y + row_h if self.children else 10) + pad


def container_style(fill: str, stroke: str, dashed: bool = False, font_size: int = 12, rounded: bool = True,
                    stroke_w: float = 1.5) -> str:
    return (f"rounded={1 if rounded else 0};arcSize=10;absoluteArcSize=1;whiteSpace=wrap;html=1;container=1;"
            f"collapsible=0;recursiveResize=0;fillColor={fill};strokeColor={stroke};strokeWidth={stroke_w};"
            f"dashed={1 if dashed else 0};verticalAlign=top;align=left;spacingLeft=40;spacingTop=2;"
            f"fontFamily={FONT};fontSize={font_size};fontColor=#1F2937;")


class DrawioWriter:
    def __init__(self, title: str):
        self.mxfile = ET.Element("mxfile", host="azure-arch-diagrams", type="device", version="24.0.0")
        self.title = title
        self.page_count = 0

    def new_page(self, name: str):
        self.page_count += 1
        diagram = ET.SubElement(self.mxfile, "diagram", id=f"page{self.page_count}", name=name[:60])
        model = ET.SubElement(diagram, "mxGraphModel", dx="1600", dy="900", grid="1", gridSize="10",
                              guides="1", tooltips="1", connect="1", arrows="1", fold="1", page="0",
                              pageScale="1", pageWidth="1654", pageHeight="1169", math="0", shadow="0")
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        ET.SubElement(root, "mxCell", id="1", parent="0")
        return Page(root, f"p{self.page_count}")

    def save(self, path: Path) -> None:
        ET.indent(self.mxfile, space=" ")
        ET.ElementTree(self.mxfile).write(path, encoding="utf-8", xml_declaration=False)


class Page:
    def __init__(self, root: ET.Element, prefix: str):
        self.root, self.prefix, self.n = root, prefix, 0
        self.edges_seen: Set[Tuple[str, str, str]] = set()

    def _id(self) -> str:
        self.n += 1
        return f"{self.prefix}-{self.n}"

    def _vertex(self, label: str, style: str, x: float, y: float, w: float, h: float, parent: str,
                tooltip: str = "", attrs: Optional[Dict[str, str]] = None) -> str:
        cid = self._id()
        if tooltip or attrs:
            obj = ET.SubElement(self.root, "UserObject", id=cid, label=label)
            if tooltip:
                obj.set("tooltip", tooltip)
            for k, v in (attrs or {}).items():
                if v not in (None, ""):
                    obj.set(k, str(v))
            cell = ET.SubElement(obj, "mxCell", style=style, vertex="1", parent=parent)
        else:
            cell = ET.SubElement(self.root, "mxCell", id=cid, value=label, style=style, vertex="1", parent=parent)
        ET.SubElement(cell, "mxGeometry", x=str(round(x)), y=str(round(y)), width=str(round(w)),
                      height=str(round(h)), **{"as": "geometry"})
        return cid

    def emit(self, node: Union[Box, Leaf], parent: str = "1", ox: float = 0, oy: float = 0) -> str:
        """ox/oy = absolute position of the parent (used to record node.ax/ay for edge routing)."""
        node.ax, node.ay = ox + node.x, oy + node.y
        if isinstance(node, Leaf):
            style = (f"aspect=fixed;html=1;image;image={IMG}{node.icon};verticalLabelPosition=bottom;"
                     f"verticalAlign=top;labelPosition=center;align=center;whiteSpace=wrap;"
                     f"labelWidth={CELL_W - 6};fontFamily={FONT};fontSize=10;fontColor=#111827;{node.extra_style}")
            node.cell_id = self._vertex(node.label, style, node.x + (CELL_W - ICON) / 2, node.y + 4,
                                        ICON, ICON, parent, node.tooltip, node.attrs)
            return node.cell_id
        node.cell_id = self._vertex(node.label, node.style, node.x, node.y, node.w, node.h, parent,
                                    node.tooltip, node.attrs)
        if node.icon:
            self._vertex("", f"aspect=fixed;html=1;image;image={IMG}{node.icon};", 8, 8, 26, 26, node.cell_id)
        for c in node.children:
            self.emit(c, node.cell_id, node.ax, node.ay)
        return node.cell_id

    def routed_edge(self, src: str, dst: str, color: str, points: List[Tuple[float, float]], label: str = "",
                    width: float = 2, extra: str = "", parent: str = "1") -> None:
        """Edge that follows explicit waypoints (absolute page coordinates) - used to keep lines in the gaps."""
        style = (f"edgeStyle=none;rounded=1;html=1;jumpStyle=none;strokeColor={color};strokeWidth={width};endArrow=classic;"
                 f"fontColor={color};fontSize=10;fontFamily={FONT};labelBackgroundColor=#FFFFFF;{extra}")
        cell = ET.SubElement(self.root, "mxCell", id=self._id(), value=esc(label), style=style, edge="1",
                             parent=parent, source=src, target=dst)
        geo = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
        arr = ET.SubElement(geo, "Array", **{"as": "points"})
        for x, y in points:
            ET.SubElement(arr, "mxPoint", x=str(round(x)), y=str(round(y)))

    def layer(self, cid: str, name: str) -> str:
        """Add a draw.io layer (layers added later are drawn on top)."""
        ET.SubElement(self.root, "mxCell", id=cid, value=name, parent="0")
        return cid

    def text(self, label: str, x: float, y: float, w: float, h: float, size: int = 12) -> None:
        self._vertex(label, f"text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;fontFamily={FONT};"
                            f"fontSize={size};fontColor=#1F2937;", x, y, w, h, "1")

    def edge(self, src: str, dst: str, kind: str, label: str = "") -> None:
        if not src or not dst or src == dst:
            return
        key = (min(src, dst), max(src, dst), kind) if kind in ("peering", "hybrid") else (src, dst, kind)
        if key in self.edges_seen:
            return
        self.edges_seen.add(key)
        color, extra = EDGE[kind]
        style = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jumpStyle=arc;jumpSize=8;strokeColor={color};"
                 f"fontColor={color};fontSize=9;fontFamily={FONT};labelBackgroundColor=#FFFFFF;{extra}")
        cell = ET.SubElement(self.root, "mxCell", id=self._id(), value=esc(label), style=style, edge="1",
                             parent="1", source=src, target=dst)
        ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})


# --------------------------------------------------------------------------------------
# Diagram builder
# --------------------------------------------------------------------------------------
class DiagramBuilder:
    def __init__(self, topo: Topology, args):
        self.t, self.args = topo, args
        self.w = DrawioWriter(args.title)

    # leaves -----------------------------------------------------------------------------
    def leaf(self, rid: str) -> Leaf:
        r = self.t.all_res.get(rid)
        if not r:  # referenced resource outside the export scope
            return Leaf(label=f"{esc(breakable(name_from_id(rid)))}<br><font color='{C['text2']}' style='font-size:9px'>"
                              f"outside export scope</font>", icon=FALLBACK_ICON, tooltip=rid, attrs={"azureId": rid})
        icon, _ = type_info(r)
        warn = ""
        if rid in self.t.vm_public:
            warn = f"<br><font color='{C['warn']}' style='font-size:9px'>⚠ public IP</font>"
        label = (f"{esc(breakable(r.get('name', '')))}<br><font color='{C['text2']}' style='font-size:9px'>"
                 f"{esc(subtitle(r))}</font>{warn}")
        return Leaf(label=label, icon=icon, tooltip=tooltip_for(r, self.t.sub_names),
                    attrs={"azureId": r.get("id"), "azureType": r.get("type"), "location": r.get("location"),
                           "resourceGroup": r.get("resourceGroup")})

    def leaves_collapsed(self, rids: List[str], threshold: int) -> Tuple[List[Leaf], Dict[str, Leaf]]:
        """One leaf per resource, or one '×N' leaf per type when a type exceeds the threshold."""
        by_type: Dict[str, List[str]] = defaultdict(list)
        for rid in rids:
            by_type[self.t.all_res.get(rid, {}).get("type", "?")].append(rid)
        out: List[Leaf] = []
        owner: Dict[str, Leaf] = {}
        for typ in sorted(by_type, key=lambda x: (TYPE_INFO.get(x, ("", x))[1].lower())):
            group = sorted(by_type[typ], key=lambda x: lid(self.t.all_res.get(x, {}).get("name")))
            if len(group) > threshold:
                r0 = self.t.all_res[group[0]]
                icon, label = type_info(r0)
                names = [self.t.all_res[x].get("name", "") for x in group]
                lf = Leaf(label=f"<b>{esc(label)} ×{len(group)}</b><br><font color='{C['text2']}' style='font-size:9px'>"
                                f"{esc(breakable(', '.join(names[:3])))}{' …' if len(names) > 3 else ''}</font>",
                          icon=icon, tooltip=f"{label} ({len(group)})\n" + "\n".join(names[:80]),
                          attrs={"azureType": typ, "count": str(len(group))})
                out.append(lf)
                for x in group:
                    owner[x] = lf
            else:
                for x in group:
                    lf = self.leaf(x)
                    out.append(lf)
                    owner[x] = lf
        return out, owner

    # pages ------------------------------------------------------------------------------
    def build(self) -> None:
        pages = set(self.args.pages)
        if "overview" in pages:
            self.overview_page()
        if "network" in pages:
            self.network_page()
        if "inventory" in pages:
            for sid in sorted(self.t.subs, key=lambda s: lid(self.t.sub_names[s])):
                self.subscription_page(sid)

    def overview_page(self) -> None:
        page = self.w.new_page("Overview")
        t = self.t
        counts = Counter(lid(r.get("subscriptionId")) for r in t.res.values())
        rg_counts = Counter(lid(rg.get("subscriptionId")) for rg in t.rgs)
        vnet_counts = Counter(lid(v.get("subscriptionId")) for v in t.vnets.values())
        groups: Dict[str, List[str]] = defaultdict(list)
        for sid, s in t.subs.items():
            chain = s.get("managementGroups") or []
            parent = (chain[0].get("displayName") or chain[0].get("name")) if chain else "No management group data"
            groups[parent].append(sid)
        boxes = []
        for mg in sorted(groups):
            leaves = []
            for sid in sorted(groups[mg], key=lambda s: lid(t.sub_names[s])):
                s = t.subs[sid]
                chain = " › ".join(reversed([c.get("displayName") or c.get("name") for c in s.get("managementGroups") or []]))
                leaves.append(Leaf(
                    label=f"<b>{esc(breakable(t.sub_names[sid]))}</b><br><font color='{C['text2']}' style='font-size:9px'>"
                          f"{rg_counts[sid]} RGs · {counts[sid]} resources · {vnet_counts[sid]} VNets</font>",
                    icon="general/Subscriptions.svg",
                    tooltip=f"{t.sub_names[sid]}\nID: {sid}\nState: {s.get('state')}\nMG path: {chain}",
                    attrs={"subscriptionId": sid}))
            boxes.append(Box(label=f"<b>{esc(mg)}</b>", icon="general/Management_Groups.svg", cols=5,
                             style=container_style(C["subscr_fill"], C["subscr_stroke"]), children=leaves, header=40))
        top = Box(label=f"<b>{esc(self.args.title)}</b> — tenant overview", children=boxes, max_inner=2600,
                  icon="identity/Tenant_Properties.svg", style=container_style("none", C["band_stroke"], font_size=16),
                  header=46)
        top.layout()
        page.emit(top)
        by_type = Counter(r["type"] for r in t.res.values())
        top_types = "<br>".join(f"{n} × {esc(TYPE_INFO.get(k, ('', k))[1])}" for k, n in by_type.most_common(15))
        page.text(f"<b>Totals</b><br>{len(t.subs)} subscriptions · {len(t.rgs)} resource groups · "
                  f"{len(t.res)} resources · {len(t.vnets)} VNets<br><br><b>Most common types</b><br>{top_types}",
                  top.w + 40, 0, 340, 420)

    def network_page(self) -> None:
        t, a = self.t, self.args
        page = self.w.new_page("Network topology")
        cell_of: Dict[str, str] = {}      # resource id -> cell id (after emit)
        leaf_of: Dict[str, Leaf] = {}
        box_of_vnet: Dict[str, Box] = {}
        box_of_subnet: Dict[str, Box] = {}

        # resources per subnet / per vnet
        in_subnet: Dict[str, List[str]] = defaultdict(list)
        in_vnet: Dict[str, List[str]] = defaultdict(list)
        for rid, (vnet, sid) in t.placement.items():
            if t.res[rid]["type"] in HIDE_ON_NETWORK:
                continue
            (in_subnet[sid] if sid else in_vnet[vnet]).append(rid)

        def vnet_box(vid: str) -> Box:
            v = t.vnets[vid]
            p = props(v)
            role = "HUB" if vid in t.hubs else ("SPOKE" if vid in t.spokes else "VNET")
            fill, stroke = {"HUB": (C["hub_fill"], C["hub_stroke"]), "SPOKE": (C["spoke_fill"], C["spoke_stroke"]),
                            "VNET": (C["iso_fill"], C["iso_stroke"])}[role]
            cidr = ", ".join(g(p, "addressSpace", "addressPrefixes", default=[]) or [])
            children: List[Union[Box, Leaf]] = []
            vl, owners = self.leaves_collapsed(in_vnet.get(vid, []), a.collapse)
            leaf_of.update(owners)
            children += vl
            subnet_ids = sorted(t.subnet_order[vid], key=lambda s: (t.subnets[s]["name"].lower() not in SPECIAL_SUBNETS,
                                                                    t.subnets[s]["name"].lower()))
            for sid in subnet_ids:
                sn = t.subnets[sid]
                items = in_subnet.get(sid, [])
                if not items and a.hide_empty_subnets:
                    continue
                special = sn["name"].lower() in SPECIAL_SUBNETS
                flags = []
                if sn["nsg"]:
                    flags.append("NSG")
                elif not special:
                    flags.append(f"<font color='{C['warn']}'>no NSG</font>")
                if sn["udr"]:
                    flags.append("UDR")
                if sn["nat"]:
                    flags.append("NAT")
                if sn["delegations"]:
                    flags.append("→ " + ", ".join(d.split("/")[-1] for d in sn["delegations"] if d))
                leaves, owners = self.leaves_collapsed(items, a.collapse if len(items) > a.max_per_subnet else 10 ** 6)
                leaf_of.update(owners)
                sb = Box(label=f"<b>{esc(sn['name'])}</b> <font color='{C['text2']}'>{esc(sn['prefix'])}</font>"
                               f"<br><font style='font-size:9px' color='{C['text2']}'>{' · '.join(flags)}</font>",
                         icon="networking/Subnet.svg", children=leaves, cols=3, header=42,
                         style=container_style(C["special_fill"] if special else C["sub_fill"],
                                               C["special_stroke"] if special else C["sub_stroke"], dashed=True,
                                               font_size=11, stroke_w=1),
                         tooltip=f"{sn['name']} {sn['prefix']}\nNSG: {name_from_id(sn['nsg']) or '-'}\n"
                                 f"Route table: {name_from_id(sn['udr']) or '-'}\n"
                                 f"Service endpoints: {', '.join(x for x in sn['service_endpoints'] if x) or '-'}\n"
                                 f"Delegations: {', '.join(x for x in sn['delegations'] if x) or '-'}",
                         attrs={"azureId": sid})
                box_of_subnet[sid] = sb
                children.append(sb)
            box = Box(label=f"<b>{role}: {esc(v.get('name'))}</b> <font color='{C['text2']}'>{esc(cidr)}</font><br>"
                            f"<font style='font-size:10px' color='{C['text2']}'>{esc(t.sub_names.get(lid(v.get('subscriptionId')), ''))}"
                            f" · {esc(v.get('resourceGroup'))} · {esc(v.get('location'))}</font>",
                      icon="networking/Virtual_Networks.svg", children=children, max_inner=a.vnet_width, header=48,
                      style=container_style(fill, stroke, stroke_w=2 if role == "HUB" else 1.5),
                      tooltip=tooltip_for(v, t.sub_names), attrs={"azureId": v.get("id"), "role": role})
            box_of_vnet[vid] = box
            return box

        sort_key = lambda vid: (lid(t.sub_names.get(lid(t.vnets[vid].get("subscriptionId")), "")), lid(t.vnets[vid].get("name")))
        bands: List[Box] = []

        # External band: Internet + on-prem + global edge services
        ext_leaves: List[Leaf] = []
        internet = Leaf(label="<b>Internet</b><br><font style='font-size:9px' color='#6B7280'>users / partners / sites</font>",
                        icon="general/Globe.svg")
        if t.internet_in:
            ext_leaves.append(internet)
        edge_types = {"microsoft.cdn/profiles", "microsoft.network/frontdoors", "microsoft.network/trafficmanagerprofiles",
                      "microsoft.network/localnetworkgateways", "microsoft.network/expressroutecircuits",
                      "microsoft.network/virtualwans", "microsoft.network/virtualhubs"}
        hybrid_targets = {o for _, o, _ in t.hybrid}
        for rid, r in sorted(t.res.items(), key=lambda kv: (kv[1]["type"], lid(kv[1].get("name")))):
            if (r["type"] in edge_types or rid in hybrid_targets) and rid not in t.placement:
                lf = self.leaf(rid)
                leaf_of[rid] = lf
                ext_leaves.append(lf)
        if ext_leaves:
            bands.append(Box(label="<b>External: Internet · On-premises · Global edge</b>", icon="general/Globe.svg",
                             children=ext_leaves, cols=8, style=container_style("#F3F4F6", C["band_stroke"], font_size=13),
                             header=44))

        hubs = sorted(t.hubs, key=sort_key)
        spokes = sorted(t.spokes, key=sort_key)
        others = sorted(set(t.vnets) - t.hubs - t.spokes, key=sort_key)
        for title, vids, icon in (("Hub / connectivity VNets", hubs, "networking/Virtual_WAN_Hub.svg"),
                                  ("Spoke VNets (peered to a hub)", spokes, "networking/Virtual_Networks.svg"),
                                  ("Other VNets", others, "networking/Virtual_Networks.svg")):
            if vids:
                bands.append(Box(label=f"<b>{title}</b> ({len(vids)})", icon=icon, children=[vnet_box(v) for v in vids],
                                 max_inner=a.page_width, style=container_style("none", C["band_stroke"], dashed=True, font_size=13),
                                 header=44))

        # PaaS reached privately (Private Endpoint / Service Endpoint) but not itself inside a VNet
        paas: Set[str] = set()
        for _, target, _ in t.private_links:
            if target not in t.placement:
                paas.add(target)
        for _, rid in t.service_endpoints:
            if rid not in t.placement:
                paas.add(rid)
        for _, target, _ in t.backends:
            if target not in t.placement:
                paas.add(target)
        if paas:
            per_sub: Dict[str, List[str]] = defaultdict(list)
            for rid in paas:
                per_sub[sub_of(rid)].append(rid)
            sub_boxes = []
            for sid in sorted(per_sub, key=lambda s: lid(t.sub_names.get(s, s))):
                leaves, owners = self.leaves_collapsed(per_sub[sid], max(a.collapse, 6))
                leaf_of.update(owners)
                sub_boxes.append(Box(label=f"<b>{esc(t.sub_names.get(sid, sid + ' (outside scope)'))}</b>",
                                     icon="general/Subscriptions.svg", children=leaves, cols=6, header=40,
                                     style=container_style(C["subscr_fill"], C["subscr_stroke"], font_size=11)))
            bands.append(Box(label="<b>PaaS services reached privately</b> (Private Endpoint / Service Endpoint / LB backend)",
                             icon="networking/Private_Link.svg", children=sub_boxes, max_inner=a.page_width,
                             style=container_style("none", C["band_stroke"], dashed=True, font_size=13), header=44))

        # emit bands stacked vertically
        y = 0
        for b in bands:
            b.layout()
            b.x, b.y = 0, y
            page.emit(b)
            y += b.h + 70
        width = max([b.w for b in bands] + [800])
        self._legend(page, width + 40)

        for rid, lf in leaf_of.items():
            cell_of[rid] = lf.cell_id
        for vid, b in box_of_vnet.items():
            cell_of[vid] = b.cell_id

        # edges
        for a_, b_, state in t.peerings:
            if a_ in box_of_vnet and b_ in box_of_vnet:
                style_kind = "peering"
                page.edge(cell_of[a_], cell_of[b_], style_kind, f"peering {state}".strip())
        for pe, target, gids in t.private_links:
            page.edge(cell_of.get(pe), cell_of.get(target), "privatelink", f"Private Link {gids}".strip())
        for sid, rid in t.service_endpoints:
            if sid in box_of_subnet:
                page.edge(box_of_subnet[sid].cell_id, cell_of.get(rid), "service_endpoint", "service endpoint")
        for fe, be, label in t.backends:
            page.edge(cell_of.get(fe), cell_of.get(be), "backend", label)
        for rid, kind, label in t.internet_in:
            page.edge(internet.cell_id, cell_of.get(rid), kind, label)
        for vng, other, label in t.hybrid:
            page.edge(cell_of.get(other), cell_of.get(vng), "hybrid", label)
        for (vnet, nh), prefixes in t.udr.items():
            if vnet in box_of_vnet and cell_of.get(nh) and t.placement.get(nh, (None,))[0] != vnet:
                pf = sorted(prefixes)
                page.edge(cell_of[vnet], cell_of[nh], "udr", "UDR " + ", ".join(pf[:3]) + (" …" if len(pf) > 3 else ""))

    def _legend(self, page: Page, x: float) -> None:
        rows = [("VNet peering", "peering"), ("Private Link (PE → service)", "privatelink"),
                ("Service endpoint", "service_endpoint"), ("LB / App GW → backend", "backend"),
                ("Internet ingress", "internet"), ("⚠ Public IP directly on VM", "exposure"),
                ("VPN / ExpressRoute", "hybrid"), ("UDR next hop (firewall / NVA)", "udr")]
        body = "".join(f"<font color='{EDGE[k][0]}'>━━━</font> {esc(lbl)}<br>" for lbl, k in rows)
        page.text(f"<b>Legend</b><br>{body}<br><font color='{C['warn']}'>no NSG</font> = subnet without NSG<br>"
                  f"Hover any icon for details · Edit ▸ Edit Data shows Azure IDs", x, 0, 280, 260, size=11)

    def subscription_page(self, sid: str) -> None:
        t, a = self.t, self.args
        rids = [rid for rid, r in t.res.items() if lid(r.get("subscriptionId")) == sid and r["type"] not in HIDE_ON_INVENTORY]
        if not rids:
            return
        by_rg: Dict[str, List[str]] = defaultdict(list)
        for rid in rids:
            by_rg[t.res[rid].get("resourceGroup") or rg_of(rid)].append(rid)
        page = self.w.new_page(f"Sub: {t.sub_names[sid]}")
        rg_meta = {lid(rg.get("name")): rg for rg in t.rgs if lid(rg.get("subscriptionId")) == sid}
        boxes = []
        for rg in sorted(by_rg, key=lambda x: x.lower()):
            leaves, _ = self.leaves_collapsed(by_rg[rg], a.collapse)
            meta = rg_meta.get(lid(rg), {})
            tags = meta.get("tags") or {}
            boxes.append(Box(label=f"<b>{esc(rg)}</b><br><font style='font-size:9px' color='{C['text2']}'>"
                                   f"{esc(meta.get('location', ''))} · {len(by_rg[rg])} resources</font>",
                             icon="general/Resource_Groups.svg", children=leaves, cols=4, header=42,
                             style=container_style(C["rg_fill"], C["rg_stroke"], font_size=11),
                             tooltip=f"{rg}\n" + "\n".join(f"{k}={v}" for k, v in list(tags.items())[:10]) if tags else rg,
                             attrs={"azureId": meta.get("id")}))
        top = Box(label=f"<b>{esc(t.sub_names[sid])}</b> <font color='{C['text2']}'>{sid}</font>",
                  icon="general/Subscriptions.svg", children=boxes, max_inner=a.page_width, header=46,
                  style=container_style(C["subscr_fill"], C["subscr_stroke"], font_size=15, stroke_w=2))
        top.layout()
        page.emit(top)


# --------------------------------------------------------------------------------------
# AI context export (paste into Copilot / Claude to generate logical / data-flow views)
# --------------------------------------------------------------------------------------
class Anonymiser:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.map: Dict[str, str] = {}
        self.counter: Counter = Counter()

    def name(self, real: str, kind: str) -> str:
        if not self.enabled or not real:
            return real
        key = f"{kind}:{real.lower()}"
        if key not in self.map:
            self.counter[kind] += 1
            self.map[key] = f"{kind}-{self.counter[kind]:02d}"
        return self.map[key]

    def cidr(self, s: str) -> str:
        if not self.enabled or not s:
            return s
        return re.sub(r"\d+\.\d+\.\d+\.\d+", "x.x.x.x", s)


def short_type(t: str) -> str:
    return TYPE_INFO.get(t, ("", t.split("/")[-1]))[1].lower().replace(" ", "-").replace("/", "-")[:20]


def write_ai_context(topo: Topology, path: Path, anonymize: bool) -> Optional[Path]:
    an = Anonymiser(anonymize)
    t = topo
    N = lambda rid: an.name(t.all_res.get(rid, {}).get("name") or name_from_id(rid),
                            short_type(t.all_res.get(rid, {}).get("type", "res")))
    S = lambda sid: an.name(t.sub_names.get(sid, sid), "subscription")
    L = []
    L.append(f"# Azure estate context ({'anonymised' if anonymize else 'internal - contains real names'})\n")
    L.append("Generated from Azure Resource Graph by build_drawio.py. Use it as ground truth; do not invent resources.\n")
    L.append(f"Totals: {len(t.subs)} subscriptions, {len(t.rgs)} resource groups, {len(t.res)} resources, {len(t.vnets)} VNets.\n")

    L.append("\n## Subscriptions\n")
    for sid in sorted(t.subs, key=lambda s: lid(t.sub_names[s])):
        n = sum(1 for r in t.res.values() if lid(r.get("subscriptionId")) == sid)
        chain = " > ".join(reversed([an.name(c.get("displayName") or c.get("name"), "mg")
                                     for c in t.subs[sid].get("managementGroups") or []]))
        L.append(f"- {S(sid)} ({n} resources){' - MG: ' + chain if chain else ''}\n")

    L.append("\n## Virtual networks\n")
    for vid in sorted(t.vnets, key=lambda v: lid(t.vnets[v].get("name"))):
        v = t.vnets[vid]
        role = "HUB" if vid in t.hubs else ("SPOKE" if vid in t.spokes else "standalone")
        cidr = an.cidr(", ".join(g(props(v), "addressSpace", "addressPrefixes", default=[]) or []))
        L.append(f"\n### {N(vid)} [{role}] {cidr} - {S(lid(v.get('subscriptionId')))} / {v.get('location')}\n")
        vnet_level = [rid for rid, (vn, sn) in t.placement.items() if vn == vid and sn is None
                      and t.res[rid]["type"] not in HIDE_ON_NETWORK]
        if vnet_level:
            L.append("- VNet-level: " + ", ".join(f"{N(r)} ({type_info(t.res[r])[1]})" for r in vnet_level) + "\n")
        for sid in t.subnet_order[vid]:
            sn = t.subnets[sid]
            items = [rid for rid, (vn, s) in t.placement.items() if s == sid and t.res[rid]["type"] not in HIDE_ON_NETWORK]
            desc = ", ".join(f"{N(r)} ({type_info(t.res[r])[1]})" for r in items) or "empty"
            nsg_flag = "NSG" if sn["nsg"] else ("platform subnet" if sn["name"].lower() in SPECIAL_SUBNETS else "no-NSG")
            flags = [x for x in (nsg_flag, "UDR" if sn["udr"] else "") if x]
            L.append(f"- subnet {an.name(sn['name'], 'subnet') if sn['name'].lower() not in SPECIAL_SUBNETS else sn['name']}"
                     f" {an.cidr(sn['prefix'])} [{', '.join(flags)}]: {desc}\n")

    if t.peerings:
        L.append("\n## VNet peerings\n")
        seen = set()
        for a, b, st in t.peerings:
            k = tuple(sorted((a, b)))
            if k not in seen:
                seen.add(k)
                L.append(f"- {N(a)} <-> {N(b)} ({st})\n")
    sections = [("Private endpoints (PE -> target service)", [f"{N(pe)} -> {N(tg)} [{gid}]" for pe, tg, gid in t.private_links]),
                ("Service endpoints (subnet -> service)", [f"{an.name(t.subnets[s]['name'], 'subnet')} -> {N(r)}" for s, r in t.service_endpoints]),
                ("Load balancer / App Gateway backends", [f"{N(a)} -> {N(b)}" for a, b, _ in t.backends]),
                ("Internet-facing entry points", [f"{N(r)}: {lbl}" for r, _, lbl in t.internet_in]),
                ("Hybrid connectivity", [f"{N(a)} <-> {N(b)} ({lbl})" for a, b, lbl in t.hybrid]),
                ("Forced routing (UDR -> firewall/NVA)", [f"{N(v)} -> {N(nh)} via {', '.join(sorted(p))}" for (v, nh), p in t.udr.items()])]
    for title, rows in sections:
        if rows:
            L.append(f"\n## {title}\n" + "".join(f"- {r}\n" for r in sorted(set(rows))))

    L.append("\n## Resources by subscription and resource group (type counts)\n")
    tree: Dict[str, Dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for r in t.res.values():
        if r["type"] in HIDE_ON_INVENTORY:
            continue
        tree[lid(r.get("subscriptionId"))][r.get("resourceGroup", "")][type_info(r)[1]] += 1
    for sid in sorted(tree, key=lambda s: lid(t.sub_names.get(s, s))):
        L.append(f"\n### {S(sid)}\n")
        for rg in sorted(tree[sid], key=str.lower):
            items = ", ".join(f"{n}× {k}" for k, n in tree[sid][rg].most_common())
            L.append(f"- {an.name(rg, 'rg')}: {items}\n")
    path.write_text("".join(L), encoding="utf-8")
    if anonymize:
        mp = path.with_name(path.stem + "_mapping.json")
        mp.write_text(json.dumps({v: k.split(":", 1)[1] for k, v in an.map.items()}, indent=1), encoding="utf-8")
        return mp
    return None


# --------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Generate draw.io architecture diagrams from azure_inventory.json")
    ap.add_argument("inventory", help="JSON produced by azure_inventory.py")
    ap.add_argument("-o", "--output", default="azure_architecture.drawio")
    ap.add_argument("--title", default="Azure architecture")
    ap.add_argument("--subscriptions", nargs="*", help="limit to these subscription names or IDs")
    ap.add_argument("--pages", nargs="*", default=["overview", "network", "inventory"],
                    choices=["overview", "network", "inventory"])
    ap.add_argument("--collapse", type=int, default=3,
                    help="group N+1 or more resources of the same type into one 'xN' icon (default 3)")
    ap.add_argument("--max-per-subnet", type=int, default=12, help="above this, collapse subnet contents by type")
    ap.add_argument("--hide-empty-subnets", action="store_true")
    ap.add_argument("--vnet-width", type=int, default=1500, help="max inner width of a VNet box (px)")
    ap.add_argument("--page-width", type=int, default=3600, help="max width before wrapping VNets/RGs (px)")
    ap.add_argument("--no-ai-context", action="store_true", help="skip the *_ai_context.md file")
    ap.add_argument("--anonymize", action="store_true", help="mask names and IPs in the AI context file")
    args = ap.parse_args()

    inv_path = Path(args.inventory)
    if not inv_path.exists():
        sys.exit(f"Inventory not found: {inv_path}")
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    topo = Topology(inv, args.subscriptions)
    print(f"Loaded {len(topo.res)} resources · {len(topo.vnets)} VNets ({len(topo.hubs)} hub, {len(topo.spokes)} spoke) · "
          f"{len(topo.private_links)} private links · {len(topo.peerings)} peering links")

    builder = DiagramBuilder(topo, args)
    builder.build()
    out = Path(args.output)
    if out.suffix != ".drawio":
        out = out.with_suffix(".drawio")
    builder.w.save(out)
    print(f"draw.io diagram  -> {out}  ({builder.w.page_count} pages)")

    if not args.no_ai_context:
        ctx = out.with_name(out.stem + "_ai_context.md")
        mp = write_ai_context(topo, ctx, args.anonymize)
        print(f"AI context       -> {ctx}")
        if mp:
            print(f"Name mapping     -> {mp}   (keep internal - reverses the anonymisation)")


if __name__ == "__main__":
    main()
