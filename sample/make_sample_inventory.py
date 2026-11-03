#!/usr/bin/env python3
"""Builds sample_inventory.json - a fictional CRO tenant in Azure Resource Graph shape (for testing)."""
import json
from pathlib import Path

TENANT = "00000000-aaaa-bbbb-cccc-000000000000"
SUBS = {
    "conn": ("11111111-0000-0000-0000-000000000001", "sub-platform-connectivity", ["Platform", "Contoso-CRO"]),
    "mgmt": ("11111111-0000-0000-0000-000000000002", "sub-platform-management", ["Platform", "Contoso-CRO"]),
    "cdp": ("11111111-0000-0000-0000-000000000003", "sub-clinical-data-prod", ["Corp", "Landing Zones", "Contoso-CRO"]),
    "apps": ("11111111-0000-0000-0000-000000000004", "sub-trial-apps-prod", ["Online", "Landing Zones", "Contoso-CRO"]),
    "dev": ("11111111-0000-0000-0000-000000000005", "sub-analytics-sandbox", ["Sandbox", "Contoso-CRO"]),
}
LOC = "westeurope"
resources, rgs = [], {}


def rid(sub, rg, provider, name):
    return f"/subscriptions/{SUBS[sub][0]}/resourceGroups/{rg}/providers/{provider}/{name}"


def add(sub, rg, provider, name, props=None, kind=None, sku=None, tags=None):
    rgs.setdefault((sub, rg), None)
    r = {"id": rid(sub, rg, provider, name), "name": name.split("/")[-1], "type": provider.lower(),
         "kind": kind, "location": LOC, "resourceGroup": rg, "subscriptionId": SUBS[sub][0],
         "sku": sku, "tags": tags or {"env": "prod" if sub != "dev" else "dev", "dataClass": "GxP" if sub == "cdp" else "internal"},
         "identity": None, "zones": None, "managedBy": None, "properties": props or {"provisioningState": "Succeeded"}}
    resources.append(r)
    return r["id"]


def ref(x):
    return {"id": x}


def vnet(sub, rg, name, cidr, subnets, peers=()):
    vid = rid(sub, rg, "Microsoft.Network/virtualNetworks", name)
    sn = []
    for s in subnets:
        p = {"addressPrefix": s["prefix"]}
        if s.get("nsg"):
            p["networkSecurityGroup"] = ref(s["nsg"])
        if s.get("rt"):
            p["routeTable"] = ref(s["rt"])
        if s.get("deleg"):
            p["delegations"] = [{"name": "d", "properties": {"serviceName": s["deleg"]}}]
        if s.get("se"):
            p["serviceEndpoints"] = [{"service": x} for x in s["se"]]
        sn.append({"id": f"{vid}/subnets/{s['name']}", "name": s["name"], "properties": p})
    add(sub, rg, "Microsoft.Network/virtualNetworks", name, {
        "addressSpace": {"addressPrefixes": [cidr]}, "subnets": sn,
        "virtualNetworkPeerings": [{"name": f"peer-{p.split('/')[-1]}", "properties": {
            "remoteVirtualNetwork": ref(p), "peeringState": "Connected", "allowForwardedTraffic": True}} for p in peers]})
    return vid


def snet(vid, name):
    return f"{vid}/subnets/{name}"


def nsg(sub, rg, name):
    return add(sub, rg, "Microsoft.Network/networkSecurityGroups", name)


def vm(sub, rg, name, subnet, ip, size="Standard_D4s_v5", os_="Linux", public=False):
    nic = rid(sub, rg, "Microsoft.Network/networkInterfaces", f"{name}-nic")
    vmid = rid(sub, rg, "Microsoft.Compute/virtualMachines", name)
    ipc = {"name": "ipconfig1", "id": f"{nic}/ipConfigurations/ipconfig1",
           "properties": {"subnet": ref(subnet), "privateIPAddress": ip}}
    if public:
        pip = add(sub, rg, "Microsoft.Network/publicIPAddresses", f"{name}-pip")
        ipc["properties"]["publicIPAddress"] = ref(pip)
    add(sub, rg, "Microsoft.Network/networkInterfaces", f"{name}-nic",
        {"ipConfigurations": [ipc], "virtualMachine": ref(vmid)})
    add(sub, rg, "Microsoft.Compute/virtualMachines", name, {
        "hardwareProfile": {"vmSize": size}, "storageProfile": {"osDisk": {"osType": os_}},
        "networkProfile": {"networkInterfaces": [ref(nic)]}})
    add(sub, rg, "Microsoft.Compute/disks", f"{name}-osdisk", sku={"name": "Premium_LRS"})
    return vmid, nic


def pe(sub, rg, name, subnet, target, group):
    return add(sub, rg, "Microsoft.Network/privateEndpoints", name, {
        "subnet": ref(subnet), "privateLinkServiceConnections": [{"name": name, "properties": {
            "privateLinkServiceId": target, "groupIds": [group]}}]})


# ---------------- connectivity (hub) ----------------
C_RG = "rg-connectivity-weu"
hub = rid("conn", C_RG, "Microsoft.Network/virtualNetworks", "vnet-hub-weu")
clin = rid("cdp", "rg-network-clinical", "Microsoft.Network/virtualNetworks", "vnet-clinical-prod-weu")
apps = rid("apps", "rg-network-apps", "Microsoft.Network/virtualNetworks", "vnet-trialapps-prod-weu")
nsg_mgmt = nsg("conn", C_RG, "nsg-hub-mgmt")
nsg_dns = nsg("conn", C_RG, "nsg-hub-dns")
vnet("conn", C_RG, "vnet-hub-weu", "10.0.0.0/22", [
    {"name": "GatewaySubnet", "prefix": "10.0.0.0/27"},
    {"name": "AzureFirewallSubnet", "prefix": "10.0.0.64/26"},
    {"name": "AzureBastionSubnet", "prefix": "10.0.0.128/26"},
    {"name": "snet-dns-inbound", "prefix": "10.0.1.0/28", "nsg": nsg_dns, "deleg": "Microsoft.Network/dnsResolvers"},
    {"name": "snet-mgmt", "prefix": "10.0.2.0/24", "nsg": nsg_mgmt},
], peers=[clin, apps])
fw_pip = add("conn", C_RG, "Microsoft.Network/publicIPAddresses", "pip-afw-hub-weu")
add("conn", C_RG, "Microsoft.Network/firewallPolicies", "afwp-hub-weu")
afw = add("conn", C_RG, "Microsoft.Network/azureFirewalls", "afw-hub-weu", {
    "sku": {"name": "AZFW_VNet", "tier": "Premium"},
    "ipConfigurations": [{"name": "c", "properties": {"subnet": ref(snet(hub, "AzureFirewallSubnet")),
                                                      "publicIPAddress": ref(fw_pip), "privateIPAddress": "10.0.0.68"}}]})
vgw = add("conn", C_RG, "Microsoft.Network/virtualNetworkGateways", "vgw-hub-weu", {
    "gatewayType": "Vpn", "ipConfigurations": [{"name": "c", "properties": {"subnet": ref(snet(hub, "GatewaySubnet"))}}]})
ergw = add("conn", C_RG, "Microsoft.Network/virtualNetworkGateways", "ergw-hub-weu", {
    "gatewayType": "ExpressRoute", "ipConfigurations": [{"name": "c", "properties": {"subnet": ref(snet(hub, "GatewaySubnet"))}}]})
lng = add("conn", C_RG, "Microsoft.Network/localNetworkGateways", "lng-hq-london", {"gatewayIpAddress": "203.0.113.10"})
erc = add("conn", C_RG, "Microsoft.Network/expressRouteCircuits", "erc-equinix-ams", sku={"name": "Standard_MeteredData"})
add("conn", C_RG, "Microsoft.Network/connections", "cn-vpn-hq-london", {
    "connectionType": "IPsec", "connectionStatus": "Connected", "virtualNetworkGateway1": ref(vgw), "localNetworkGateway2": ref(lng)})
add("conn", C_RG, "Microsoft.Network/connections", "cn-er-equinix", {
    "connectionType": "ExpressRoute", "connectionStatus": "Connected", "virtualNetworkGateway1": ref(ergw), "peer": ref(erc)})
add("conn", C_RG, "Microsoft.Network/bastionHosts", "bas-hub-weu", {
    "ipConfigurations": [{"name": "c", "properties": {"subnet": ref(snet(hub, "AzureBastionSubnet"))}}]}, sku={"name": "Standard"})
add("conn", C_RG, "Microsoft.Network/dnsResolvers", "dnspr-hub-weu", {"virtualNetwork": ref(hub)})
add("conn", C_RG, "Microsoft.Network/dnsResolvers/inboundEndpoints", "dnspr-hub-weu/in", {
    "ipConfigurations": [{"subnet": ref(snet(hub, "snet-dns-inbound"))}]})
vm("conn", C_RG, "vm-jumpbox-01", snet(hub, "snet-mgmt"), "10.0.2.4", "Standard_B2ms", "Windows")
for z in ["privatelink.blob.core.windows.net", "privatelink.dfs.core.windows.net", "privatelink.vaultcore.azure.net",
          "privatelink.database.windows.net", "privatelink.azurewebsites.net", "privatelink.datafactory.azure.net",
          "privatelink.purview.azure.com", "privatelink.fhir.azurehealthcareapis.com"]:
    add("conn", "rg-dns-weu", "Microsoft.Network/privateDnsZones", z)
add("conn", "rg-ddos", "Microsoft.Network/ddosProtectionPlans", "ddos-cro-global")

# ---------------- management ----------------
M_RG = "rg-management-weu"
add("mgmt", M_RG, "Microsoft.OperationalInsights/workspaces", "law-cro-central", sku={"name": "PerGB2018"})
add("mgmt", M_RG, "Microsoft.Automation/automationAccounts", "aa-cro-ops")
add("mgmt", M_RG, "Microsoft.Insights/actionGroups", "ag-platform-oncall")
add("mgmt", M_RG, "Microsoft.Insights/dataCollectionRules", "dcr-vm-baseline")
add("mgmt", "rg-backup-weu", "Microsoft.RecoveryServices/vaults", "rsv-cro-prod-weu", sku={"name": "RS0"})
add("mgmt", "rg-backup-weu", "Microsoft.DataProtection/backupVaults", "bvault-cro-blob")

# ---------------- clinical data platform (spoke) ----------------
N_RG, D_RG = "rg-network-clinical", "rg-clinical-data-platform"
rt_c = rid("cdp", N_RG, "Microsoft.Network/routeTables", "rt-clinical-to-fw")
nsgs = {n: nsg("cdp", N_RG, f"nsg-{n}") for n in ["dbx-public", "dbx-private", "pe", "sas", "sqlmi"]}
vnet("cdp", N_RG, "vnet-clinical-prod-weu", "10.10.0.0/20", [
    {"name": "snet-dbx-public", "prefix": "10.10.0.0/24", "nsg": nsgs["dbx-public"], "rt": rt_c, "deleg": "Microsoft.Databricks/workspaces"},
    {"name": "snet-dbx-private", "prefix": "10.10.1.0/24", "nsg": nsgs["dbx-private"], "rt": rt_c, "deleg": "Microsoft.Databricks/workspaces"},
    {"name": "snet-privateendpoints", "prefix": "10.10.2.0/24", "nsg": nsgs["pe"]},
    {"name": "snet-sqlmi", "prefix": "10.10.3.0/24", "nsg": nsgs["sqlmi"], "rt": rt_c, "deleg": "Microsoft.Sql/managedInstances"},
    {"name": "snet-statcompute", "prefix": "10.10.4.0/24", "nsg": nsgs["sas"], "rt": rt_c},
], peers=[hub])
add("cdp", N_RG, "Microsoft.Network/routeTables", "rt-clinical-to-fw", {
    "routes": [{"name": "default", "properties": {"addressPrefix": "0.0.0.0/0", "nextHopType": "VirtualAppliance", "nextHopIpAddress": "10.0.0.68"}}],
    "subnets": [ref(snet(clin, s)) for s in ["snet-dbx-public", "snet-dbx-private", "snet-sqlmi", "snet-statcompute"]]})
lake = add("cdp", D_RG, "Microsoft.Storage/storageAccounts", "stclinicallakeprod", {"isHnsEnabled": True, "publicNetworkAccess": "Disabled"},
           kind="StorageV2", sku={"name": "Standard_ZRS"})
raw = add("cdp", D_RG, "Microsoft.Storage/storageAccounts", "stclinicallandingprod", {"isHnsEnabled": True, "publicNetworkAccess": "Disabled"},
          kind="StorageV2", sku={"name": "Standard_ZRS"})
kv = add("cdp", D_RG, "Microsoft.KeyVault/vaults", "kv-clinical-prod", {"enablePurgeProtection": True})
adf = add("cdp", D_RG, "Microsoft.DataFactory/factories", "adf-clinical-ingest-prod")
pview = add("cdp", "rg-governance", "Microsoft.Purview/accounts", "pview-cro-catalog")
add("cdp", D_RG, "Microsoft.Databricks/workspaces", "dbw-clinical-prod", {"parameters": {
    "customVirtualNetworkId": {"value": clin}, "customPublicSubnetName": {"value": "snet-dbx-public"},
    "customPrivateSubnetName": {"value": "snet-dbx-private"}}}, sku={"name": "premium"})
add("cdp", D_RG, "Microsoft.Databricks/accessConnectors", "dbac-clinical-prod")
add("cdp", D_RG, "Microsoft.ManagedIdentity/userAssignedIdentities", "id-adf-clinical")
sqlmi = add("cdp", D_RG, "Microsoft.Sql/managedInstances", "sqlmi-clinical-prod", {"subnetId": snet(clin, "snet-sqlmi")},
            sku={"name": "GP_Gen5", "capacity": 8})
add("cdp", D_RG, "Microsoft.Sql/managedInstances/databases", "sqlmi-clinical-prod/CDMS")
pes = "snet-privateendpoints"
pe("cdp", D_RG, "pe-stlake-dfs", snet(clin, pes), lake, "dfs")
pe("cdp", D_RG, "pe-stlake-blob", snet(clin, pes), lake, "blob")
pe("cdp", D_RG, "pe-stlanding-blob", snet(clin, pes), raw, "blob")
pe("cdp", D_RG, "pe-kv-clinical", snet(clin, pes), kv, "vault")
pe("cdp", D_RG, "pe-adf-clinical", snet(clin, pes), adf, "dataFactory")
pe("cdp", "rg-governance", "pe-purview", snet(clin, pes), pview, "account")
# statistical compute: 3 SAS grid nodes behind internal LB
lb = rid("cdp", D_RG, "Microsoft.Network/loadBalancers", "lbi-sas-grid")
nics = []
for i in range(1, 4):
    _, nic = vm("cdp", D_RG, f"vm-sasgrid-0{i}", snet(clin, "snet-statcompute"), f"10.10.4.{10 + i}", "Standard_E16s_v5", "Linux")
    nics.append(nic)
add("cdp", D_RG, "Microsoft.Network/loadBalancers", "lbi-sas-grid", {
    "frontendIPConfigurations": [{"name": "fe", "properties": {"subnet": ref(snet(clin, "snet-statcompute")), "privateIPAddress": "10.10.4.5"}}],
    "backendAddressPools": [{"name": "be", "properties": {"backendIPConfigurations": [ref(f"{n}/ipConfigurations/ipconfig1") for n in nics]}}]},
    sku={"name": "Standard"})

# ---------------- trial applications (spoke) ----------------
AN, A_RG = "rg-network-apps", "rg-trial-apps-prod"
rt_a = rid("apps", AN, "Microsoft.Network/routeTables", "rt-apps-to-fw")
ansg = {n: nsg("apps", AN, f"nsg-{n}") for n in ["appgw", "appint", "pe", "aks"]}
vnet("apps", AN, "vnet-trialapps-prod-weu", "10.20.0.0/20", [
    {"name": "snet-appgw", "prefix": "10.20.0.0/24", "nsg": ansg["appgw"]},
    {"name": "snet-appservice-int", "prefix": "10.20.1.0/24", "nsg": ansg["appint"], "rt": rt_a, "deleg": "Microsoft.Web/serverFarms"},
    {"name": "snet-privateendpoints", "prefix": "10.20.2.0/24", "nsg": ansg["pe"]},
    {"name": "snet-aks", "prefix": "10.20.4.0/22", "nsg": ansg["aks"], "rt": rt_a},
], peers=[hub])
add("apps", AN, "Microsoft.Network/routeTables", "rt-apps-to-fw", {
    "routes": [{"name": "default", "properties": {"addressPrefix": "0.0.0.0/0", "nextHopType": "VirtualAppliance", "nextHopIpAddress": "10.0.0.68"}},
               {"name": "to-clinical", "properties": {"addressPrefix": "10.10.0.0/20", "nextHopType": "VirtualAppliance", "nextHopIpAddress": "10.0.0.68"}}],
    "subnets": [ref(snet(apps, s)) for s in ["snet-appservice-int", "snet-aks"]]})
asp = add("apps", A_RG, "Microsoft.Web/serverfarms", "asp-trialapps-prod", sku={"name": "P1v3", "capacity": 3})
web = {}
for n in ["app-edc-portal-prod", "app-epro-api-prod", "app-rtsm-prod"]:
    web[n] = add("apps", A_RG, "Microsoft.Web/sites", n, {
        "serverFarmId": asp, "defaultHostName": f"{n}.azurewebsites.net", "hostNames": [f"{n}.azurewebsites.net"],
        "virtualNetworkSubnetId": snet(apps, "snet-appservice-int"), "publicNetworkAccess": "Disabled"}, kind="app,linux")
func = add("apps", A_RG, "Microsoft.Web/sites", "func-epro-notify-prod", {
    "serverFarmId": asp, "defaultHostName": "func-epro-notify-prod.azurewebsites.net",
    "virtualNetworkSubnetId": snet(apps, "snet-appservice-int")}, kind="functionapp,linux")
waf = add("apps", A_RG, "Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies", "wafp-trialapps")
agw_pip = add("apps", A_RG, "Microsoft.Network/publicIPAddresses", "pip-agw-trialapps")
add("apps", A_RG, "Microsoft.Network/applicationGateways", "agw-trialapps-prod", {
    "sku": {"name": "WAF_v2", "tier": "WAF_v2"},
    "gatewayIPConfigurations": [{"name": "g", "properties": {"subnet": ref(snet(apps, "snet-appgw"))}}],
    "frontendIPConfigurations": [{"name": "pub", "properties": {"publicIPAddress": ref(agw_pip)}}],
    "backendAddressPools": [{"name": n, "properties": {"backendAddresses": [{"fqdn": f"{n}.azurewebsites.net"}]}}
                            for n in ["app-edc-portal-prod", "app-epro-api-prod"]]})
sqls = add("apps", A_RG, "Microsoft.Sql/servers", "sql-trialapps-prod", {"publicNetworkAccess": "Disabled"})
for db in ["sqldb-edc", "sqldb-epro", "sqldb-rtsm"]:
    add("apps", A_RG, "Microsoft.Sql/servers/databases", f"sql-trialapps-prod/{db}", sku={"name": "GP_Gen5_4"})
kva = add("apps", A_RG, "Microsoft.KeyVault/vaults", "kv-trialapps-prod")
fhir = add("apps", "rg-health-data", "Microsoft.HealthcareApis/workspaces", "ahdsepro")
add("apps", "rg-health-data", "Microsoft.HealthcareApis/workspaces/fhirservices", "ahdsepro/fhir-epro")
add("apps", A_RG, "Microsoft.Insights/components", "appi-trialapps-prod", kind="web")
add("apps", A_RG, "Microsoft.ContainerRegistry/registries", "acrtrialappsprod", sku={"name": "Premium"})
add("apps", A_RG, "Microsoft.ContainerService/managedClusters", "aks-randomisation-prod", {
    "kubernetesVersion": "1.30.4",
    "agentPoolProfiles": [{"name": "system", "count": 3, "vnetSubnetID": snet(apps, "snet-aks")},
                          {"name": "user", "count": 4, "vnetSubnetID": snet(apps, "snet-aks")}]})
add("apps", "rg-edge", "Microsoft.Cdn/profiles", "afd-patient-portal", sku={"name": "Premium_AzureFrontDoor"})
pa = "snet-privateendpoints"
for n, t in web.items():
    pe("apps", A_RG, f"pe-{n}", snet(apps, pa), t, "sites")
pe("apps", A_RG, "pe-sql-trialapps", snet(apps, pa), sqls, "sqlServer")
pe("apps", A_RG, "pe-kv-trialapps", snet(apps, pa), kva, "vault")
pe("apps", "rg-health-data", "pe-ahds-fhir", snet(apps, pa), fhir, "healthcareworkspace")

# ---------------- analytics sandbox (standalone, with findings) ----------------
S_RG = "rg-sandbox-analytics"
dev = rid("dev", S_RG, "Microsoft.Network/virtualNetworks", "vnet-sandbox")
vnet("dev", S_RG, "vnet-sandbox", "10.50.0.0/16", [
    {"name": "default", "prefix": "10.50.0.0/24", "se": ["Microsoft.Storage"]},
    {"name": "snet-unused", "prefix": "10.50.1.0/24"},
])
vm("dev", S_RG, "vm-rstudio-test", snet(dev, "default"), "10.50.0.4", "Standard_D8s_v5", "Linux", public=True)
add("dev", S_RG, "Microsoft.Storage/storageAccounts", "stsandboxanalytics", {
    "networkAcls": {"defaultAction": "Deny", "virtualNetworkRules": [{"id": snet(dev, "default"), "action": "Allow"}]},
    "publicNetworkAccess": "Enabled"}, kind="StorageV2", sku={"name": "Standard_LRS"})
add("dev", S_RG, "Microsoft.MachineLearningServices/workspaces", "mlw-sandbox", kind="Default")
add("dev", S_RG, "Microsoft.CognitiveServices/accounts", "oai-sandbox-swe", kind="OpenAI", sku={"name": "S0"})
for i in range(1, 6):
    add("dev", "rg-sandbox-scratch", "Microsoft.Storage/storageAccounts", f"stscratch0{i}", kind="StorageV2", sku={"name": "Standard_LRS"})

# ---------------- assemble ----------------
subs = [{"subscriptionId": sid, "name": nm, "tenantId": TENANT, "state": "Enabled", "tags": {},
         "managementGroups": [{"displayName": m, "name": m.lower().replace(" ", "-")} for m in chain]}
        for sid, nm, chain in SUBS.values()]
rg_list = [{"id": f"/subscriptions/{SUBS[s][0]}/resourceGroups/{rg}", "name": rg, "subscriptionId": SUBS[s][0],
            "location": LOC, "tags": {"owner": "platform-team"}} for (s, rg) in rgs]
by_type = {}
for r in resources:
    by_type[r["type"]] = by_type.get(r["type"], 0) + 1
out = {"metadata": {"exportedAtUtc": "2026-10-08T09:00:00+00:00", "source": "SAMPLE - fictional tenant",
                    "tenantIds": [TENANT], "counts": {"subscriptions": len(subs), "resourceGroups": len(rg_list),
                                                      "resources": len(resources)}, "resourcesByType": by_type},
       "subscriptions": subs, "resourceGroups": rg_list, "resources": resources}
Path(__file__).with_name("sample_inventory.json").write_text(json.dumps(out, indent=1))
print(f"sample_inventory.json: {len(subs)} subs, {len(rg_list)} RGs, {len(resources)} resources")
