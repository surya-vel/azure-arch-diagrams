# Azure estate context (internal - contains real names)
Generated from Azure Resource Graph by build_drawio.py. Use it as ground truth; do not invent resources.
Totals: 5 subscriptions, 14 resource groups, 110 resources, 4 VNets.

## Subscriptions
- sub-analytics-sandbox (13 resources) - MG: Contoso-CRO > Sandbox
- sub-clinical-data-prod (33 resources) - MG: Contoso-CRO > Landing Zones > Corp
- sub-platform-connectivity (27 resources) - MG: Contoso-CRO > Platform
- sub-platform-management (6 resources) - MG: Contoso-CRO > Platform
- sub-trial-apps-prod (31 resources) - MG: Contoso-CRO > Landing Zones > Online

## Virtual networks

### vnet-clinical-prod-weu [SPOKE] 10.10.0.0/20 - sub-clinical-data-prod / westeurope
- VNet-level: dbw-clinical-prod (Databricks workspace)
- subnet snet-dbx-public 10.10.0.0/24 [NSG, UDR]: empty
- subnet snet-dbx-private 10.10.1.0/24 [NSG, UDR]: empty
- subnet snet-privateendpoints 10.10.2.0/24 [NSG]: pe-stlake-dfs (Private endpoint), pe-stlake-blob (Private endpoint), pe-stlanding-blob (Private endpoint), pe-kv-clinical (Private endpoint), pe-adf-clinical (Private endpoint), pe-purview (Private endpoint)
- subnet snet-sqlmi 10.10.3.0/24 [NSG, UDR]: sqlmi-clinical-prod (SQL Managed Instance)
- subnet snet-statcompute 10.10.4.0/24 [NSG, UDR]: vm-sasgrid-01 (Virtual machine), vm-sasgrid-02 (Virtual machine), vm-sasgrid-03 (Virtual machine), lbi-sas-grid (Load balancer)

### vnet-hub-weu [HUB] 10.0.0.0/22 - sub-platform-connectivity / westeurope
- VNet-level: dnspr-hub-weu (DNS private resolver)
- subnet GatewaySubnet 10.0.0.0/27 [platform subnet]: vgw-hub-weu (VNet gateway), ergw-hub-weu (VNet gateway)
- subnet AzureFirewallSubnet 10.0.0.64/26 [platform subnet]: afw-hub-weu (Azure Firewall)
- subnet AzureBastionSubnet 10.0.0.128/26 [platform subnet]: bas-hub-weu (Bastion)
- subnet snet-dns-inbound 10.0.1.0/28 [NSG]: empty
- subnet snet-mgmt 10.0.2.0/24 [NSG]: vm-jumpbox-01 (Virtual machine)

### vnet-sandbox [standalone] 10.50.0.0/16 - sub-analytics-sandbox / westeurope
- subnet default 10.50.0.0/24 [no-NSG]: vm-rstudio-test (Virtual machine)
- subnet snet-unused 10.50.1.0/24 [no-NSG]: empty

### vnet-trialapps-prod-weu [SPOKE] 10.20.0.0/20 - sub-trial-apps-prod / westeurope
- subnet snet-appgw 10.20.0.0/24 [NSG]: agw-trialapps-prod (Application Gateway)
- subnet snet-appservice-int 10.20.1.0/24 [NSG, UDR]: app-edc-portal-prod (App Service), app-epro-api-prod (App Service), app-rtsm-prod (App Service), func-epro-notify-prod (Function App)
- subnet snet-privateendpoints 10.20.2.0/24 [NSG]: pe-app-edc-portal-prod (Private endpoint), pe-app-epro-api-prod (Private endpoint), pe-app-rtsm-prod (Private endpoint), pe-sql-trialapps (Private endpoint), pe-kv-trialapps (Private endpoint), pe-ahds-fhir (Private endpoint)
- subnet snet-aks 10.20.4.0/22 [NSG, UDR]: aks-randomisation-prod (AKS cluster)

## VNet peerings
- vnet-hub-weu <-> vnet-clinical-prod-weu (Connected)
- vnet-hub-weu <-> vnet-trialapps-prod-weu (Connected)

## Private endpoints (PE -> target service)
- pe-adf-clinical -> adf-clinical-ingest-prod [dataFactory]
- pe-ahds-fhir -> ahdsepro [healthcareworkspace]
- pe-app-edc-portal-prod -> app-edc-portal-prod [sites]
- pe-app-epro-api-prod -> app-epro-api-prod [sites]
- pe-app-rtsm-prod -> app-rtsm-prod [sites]
- pe-kv-clinical -> kv-clinical-prod [vault]
- pe-kv-trialapps -> kv-trialapps-prod [vault]
- pe-purview -> pview-cro-catalog [account]
- pe-sql-trialapps -> sql-trialapps-prod [sqlServer]
- pe-stlake-blob -> stclinicallakeprod [blob]
- pe-stlake-dfs -> stclinicallakeprod [dfs]
- pe-stlanding-blob -> stclinicallandingprod [blob]

## Service endpoints (subnet -> service)
- default -> stsandboxanalytics

## Load balancer / App Gateway backends
- agw-trialapps-prod -> app-edc-portal-prod
- agw-trialapps-prod -> app-epro-api-prod
- lbi-sas-grid -> vm-sasgrid-01
- lbi-sas-grid -> vm-sasgrid-02
- lbi-sas-grid -> vm-sasgrid-03

## Internet-facing entry points
- afd-patient-portal: Global entry
- afw-hub-weu: Egress / DNAT
- agw-trialapps-prod: HTTPS (public)
- bas-hub-weu: RDP/SSH over TLS 443
- vm-rstudio-test: Public IP on VM

## Hybrid connectivity
- ergw-hub-weu <-> erc-equinix-ams (ExpressRoute Connected)
- vgw-hub-weu <-> lng-hq-london (IPsec Connected)

## Forced routing (UDR -> firewall/NVA)
- vnet-clinical-prod-weu -> afw-hub-weu via 0.0.0.0/0
- vnet-trialapps-prod-weu -> afw-hub-weu via 0.0.0.0/0, 10.10.0.0/20

## Resources by subscription and resource group (type counts)

### sub-analytics-sandbox
- rg-sandbox-analytics: 1× Virtual network, 1× Public IP, 1× Virtual machine, 1× Managed disk, 1× Storage account, 1× Azure ML workspace, 1× Azure OpenAI
- rg-sandbox-scratch: 5× Storage account

### sub-clinical-data-prod
- rg-clinical-data-platform: 5× Private endpoint, 3× Virtual machine, 3× Managed disk, 2× Storage account, 1× Key Vault, 1× Data Factory, 1× Databricks workspace, 1× Databricks access connector, 1× Managed identity, 1× SQL Managed Instance, 1× SQL MI database, 1× Load balancer
- rg-governance: 1× Purview, 1× Private endpoint
- rg-network-clinical: 5× NSG, 1× Virtual network, 1× Route table

### sub-platform-connectivity
- rg-connectivity-weu: 2× NSG, 2× VNet gateway, 2× Gateway connection, 1× Virtual network, 1× Public IP, 1× Firewall policy, 1× Azure Firewall, 1× On-prem (local network gateway), 1× ExpressRoute circuit, 1× Bastion, 1× DNS private resolver, 1× Virtual machine, 1× Managed disk
- rg-ddos: 1× DDoS plan
- rg-dns-weu: 8× Private DNS zone

### sub-platform-management
- rg-backup-weu: 1× Recovery Services vault, 1× Backup vault
- rg-management-weu: 1× Log Analytics, 1× Automation account, 1× Action group, 1× Data collection rule

### sub-trial-apps-prod
- rg-edge: 1× Front Door / CDN
- rg-health-data: 1× Health Data Services, 1× FHIR service, 1× Private endpoint
- rg-network-apps: 4× NSG, 1× Virtual network, 1× Route table
- rg-trial-apps-prod: 5× Private endpoint, 3× App Service, 3× Azure SQL database, 1× App Service plan, 1× Function App, 1× WAF policy, 1× Public IP, 1× Application Gateway, 1× Azure SQL server, 1× Key Vault, 1× Application Insights, 1× Container registry, 1× AKS cluster
