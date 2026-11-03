# Azure tenant → draw.io architecture diagrams

Point it at your Azure tenant and get editable, multi-page **draw.io** diagrams of your network
topology and workloads, a written architecture document, and an AI-ready text description you can
give to Copilot or Claude.

```
Azure Resource Graph ──► azure_inventory.json ──► build_*.py ──► .drawio  +  .html  +  .md
   (read-only)            (raw export)
```

* **One Resource Graph query for the whole tenant.** No per-service SDK clients and no Graphviz.
* **Native draw.io XML** using draw.io's built-in Azure icons, so every shape stays editable and correctly nested.
* **Read-only.** You only need the built-in **Reader** role.
* The diagram builders use only the Python standard library.

## Quick start

Requirements: Python 3.9+, the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
and Reader access to the subscriptions you want to diagram.

```bash
git clone https://github.com/surya-vel/azure-arch-diagrams.git
cd azure-arch-diagrams
pip install -r requirements.txt
```

**Try it without Azure first.** A fictional tenant (Contoso) is included:

```bash
python build_drawio.py sample/sample_inventory.json
python build_solution.py sample/sample_inventory.json --config solution_config.example.json -o contoso
```

**Then run it against your own tenant:**

```bash
az login --tenant <YOUR_TENANT_ID>
python azure_inventory.py                       # -> azure_inventory.json (every subscription you can read)
python build_drawio.py azure_inventory.json --title "Our Azure"
```

Open the `.drawio` file in [draw.io desktop](https://www.drawio.com/), [app.diagrams.net](https://app.diagrams.net),
or the VS Code *Draw.io Integration* extension. Open the `.html` file in any browser.

> **Privacy:** `azure_inventory.json` and everything generated from it contain your real resource
> names, IP ranges and IDs. The included `.gitignore` keeps them out of git. Don't commit them to a
> public repository.

## The diagrams

| Script | Needs a config? | What you get |
|---|---|---|
| `build_drawio.py` | no | Physical inventory: overview, network topology and one page per subscription |
| `build_solution.py` | optional | Solution architecture page, plus network and subscription pages, an HTML viewer, `*_architecture.md` and `*_ai_context.md` |
| `build_network_view.py` | yes | Low-level network diagram: icons and lines, VNet → subnet → resource → backup, coloured zones |
| `build_audit_view.py` | yes | One-page overview for auditors: users → hub → workload cards → backup, DR and monitoring |
| `build_focus_view.py` | optional | Standard Azure hierarchy (Subscription → Resource group → VNet → Subnet → Resources) for the resource groups you choose, with every connection Azure knows about |

```bash
python build_solution.py      azure_inventory.json --config my_config.json -o my_azure
python build_network_view.py  azure_inventory.json --config my_config.json -o my_azure_network
python build_audit_view.py    azure_inventory.json --config my_config.json -o my_azure_audit
python build_focus_view.py    azure_inventory.json --config my_config.json -o my_azure_focus
```

### Focused view (`build_focus_view.py`)

The layout is calculated automatically. Without a config it shows every resource group. Lines sit
on their own layer behind the icons and text. These connections come straight from the export:
VNet peering / gateway transit, P2S and site-to-site VPN, private endpoints, load balancer and App
Gateway back ends, AVD (workspace → app group → host pool → session host), Azure Backup and Azure
Site Recovery (DR replicas appear faded in their recovery subnet). NICs, disks and attached public
IPs / NSGs are left out because the VM and subnet labels already show them
(`"showAllResources": true` brings them back).

The `focusView` config section chooses the scope and adds what Azure can't see:

```json
"focusView": {
  "title": "Hub & data platform",
  "resourceGroups": ["rg-connectivity", "rg-data@Prod subscription"],
  "subscriptions": ["Analytics"],
  "declaredFlows": [{"path": ["storage1", "vm-jump", "aks-1"], "labels": ["copy", "mount"], "note": "…"}],
  "notes": ["Scope: …"],
  "width": 3000
}
```

### Physical inventory (`build_drawio.py`)

| Page | Content |
|---|---|
| **Overview** | Management groups → subscriptions, with resource group, resource and VNet counts |
| **Network topology** | Internet, on-premises and global edge → **hub** VNets → **spoke** VNets → subnets (CIDR, NSG, UDR, NAT and delegation flags) → resources. PaaS reached privately sits in a separate band. |
| **Sub: \<name\>** (one per subscription) | Every resource group and resource. Busy types collapse to "×N" icons. |

Flows on the network page: VNet peering, private endpoint → service, service endpoints,
LB / App Gateway → back ends, Internet ingress, public IPs on VMs (⚠), VPN / ExpressRoute, and
UDR → firewall / NVA (forced tunnelling). Subnets without an NSG are flagged in red.

Every icon has a hover tooltip (type, SKU, resource group, location, tags, ID). The Azure resource
ID is also stored in the shape's data fields (`Edit ▸ Edit Data`).

## Your config file

Copy `solution_config.example.json` to e.g. `solution_config.mycompany.json`. Files named like that
are git-ignored. Then edit it. The config only holds what Azure can't tell you:

| Key | Used by | Purpose |
|---|---|---|
| `title`, `summary`, `story` | solution | Page title and the "How it works" narrative |
| `workloads` | solution, network | Which resource groups/resources form a workload, plus the box colour, icon and row (`access`, `primary`, `shared`, `dr`) |
| `labels` | solution, network, focus | Short role labels, e.g. `"vm-dc-01": "Primary DC + DNS"` |
| `links` | solution, network, focus | Documented relationships, e.g. AD replication between domain controllers |
| `ignoreResourceGroups` | solution, focus | Resource groups to leave out |
| `focusView` | focus | Scope (resource groups / subscriptions), declared data flows and notes |
| `auditView` | audit | Cards, their 2–3 key components and the arrows between them |
| `networkView` | network | Left-to-right VNet order and side zones (e.g. an isolated subscription) |
| `declaredControls` | all | Controls Resource Graph can't see, such as Entra ID Conditional Access |

These are re-read from Azure on every run, so you don't put them in the config: VNets, subnets,
IPs, peerings, private endpoints, backup items, ASR replication, AVD hosts and review findings.
A resource group that isn't in the config still shows up as its own workload box.

## Options

### Exporter (`azure_inventory.py`)
| Option | Purpose |
|---|---|
| `--subscriptions <id> ...` | Limit to specific subscriptions |
| `--management-group <mg> ...` | Everything under a management group |
| `--auth default\|cli\|browser` | `default` also covers service principals (`AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`) and managed identity, so it works in a pipeline |
| `--slim` | Keep full properties only for topology-relevant types (much smaller file) |
| `--exclude-tags` | Drop tags from the export |
| `--include-all` | Keep noisy types (VM extensions, alert rules, dashboards…) |

### Physical diagram (`build_drawio.py`)
| Option | Purpose |
|---|---|
| `--subscriptions "Prod" <id>` | Diagram a subset (names or IDs) |
| `--pages overview network inventory` | Choose which pages to build |
| `--collapse 3` | Group more than N resources of the same type into one "×N" icon |
| `--max-per-subnet 12` | Collapse a subnet's contents by type above this count |
| `--hide-empty-subnets` | Cleaner network page |
| `--vnet-width` / `--page-width` | Wrapping widths in px for very large estates |
| `--anonymize` | Mask names and IPs in the AI context. The mapping is saved to `*_mapping.json`; keep that file private. |

## How placement works
* VM → NIC → subnet. VMSS, AKS, App Service VNet integration, SQL MI, APIM, Container Apps, and
  PostgreSQL / MySQL flexible servers → their subnet reference. Databricks → VNet-injection parameters.
* Resources that span several subnets (NAT gateway, Databricks, public LB) sit at VNet level.
* A VNet counts as a hub if its name contains "hub", if it has a GatewaySubnet, AzureFirewallSubnet
  or RouteServerSubnet, or if it hosts a firewall or VNet gateway. A spoke is any VNet peered to a hub.
* Storage, Key Vault and Cosmos DB `virtualNetworkRules` → service-endpoint edges.

## Keeping it current
Schedule the exporter and a builder weekly in Azure DevOps or GitHub Actions, using a service
principal with Reader. Commit the output to a **private** repository. Git diffs of
`*_ai_context.md` then give you a readable change log of your infrastructure.

## Data handling
* The export holds **infrastructure metadata only** (no application data), but it does include
  resource names, IP ranges, tags and FQDNs. Treat it as internal/confidential.
* Use `--exclude-tags` at export time and `--anonymize` before you share the AI context with an
  AI service that isn't covered by your organisation's agreement.

## Extending
To add icons or labels, edit `TYPE_INFO` in `build_drawio.py`. Icon paths come from draw.io's
`img/lib/azure2/…` library. Colours are in `C` and `EDGE`, and new relationships go in
`Topology._relations`.

`COPILOT_PROMPTS.md` has prompts that turn the AI context into data-flow, security-review and
executive diagrams.

## License
[MIT](LICENSE)
