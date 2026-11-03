# Prompts for Copilot / Claude (step 3 - optional)

The `.drawio` file is generated deterministically from Azure, so it is accurate but "physical".
Use `*_ai_context.md` (ground truth) with an AI assistant to produce the *logical* views that
auditors, sponsors and architects usually ask for. Use `--anonymize` before pasting into any
AI tool that is not covered by your enterprise data-protection agreement.

## 1. Clinical data-flow view
> Using ONLY the resources in the attached Azure context, draw a draw.io (mxGraph XML) data-flow
> diagram showing how clinical trial data moves from ingestion (landing storage / Data Factory)
> through processing (Databricks / SQL MI / statistical compute) to consumption (portals, reports).
> Use draw.io Azure icons (image=img/lib/azure2/...). Mark every hop as Private Link, Service
> Endpoint or public. Do not invent resources; list any assumptions separately.

## 2. Security / exposure review
> From the attached Azure context, list every Internet entry point, every subnet marked
> "no-NSG", every VM with a public IP, and any PaaS service reached by service endpoint instead of
> Private Link. Rank findings by risk for a regulated organisation and suggest the fix for each.

## 3. Executive one-pager
> Summarise this Azure estate in one landscape draw.io page: management groups, landing zones,
> hub-spoke network, and the 6-8 key workloads, with one sentence each. Keep icons from
> img/lib/azure2/. No IP addresses.

## 4. Validation / IQ documentation support
> Produce an infrastructure inventory table (subscription, resource group, resource, type, SKU,
> region, network placement, private/public access) suitable for an Installation Qualification
> appendix, using only the attached context.

## 5. Diff two exports (change control)
> Here are two Azure context files from <date A> and <date B>. List resources added, removed or
> moved between subnets/VNets, and any new Internet exposure.
