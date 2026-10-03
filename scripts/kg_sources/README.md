# Catalog sources

Source files here are inputs only (gitignored). Only the generated catalogs in `src/memory_mcp/kg/catalog` are committed.

| Provider | Source | How to fetch |
|---|---|---|
| azure | ARM provider registry | `az provider list --expand "resourceTypes/resourceType" -o json > scripts/kg_sources/azure_providers.json` (needs `az login`) |
| aws | CloudFormation resource specification | `curl -sL https://d1uauaxba7bl26.cloudfront.net/latest/gzip/CloudFormationResourceSpecification.json --compressed -o scripts/kg_sources/aws_cfn_spec.json` |
| gcp | Cloud Asset Inventory supported types | `curl -sL https://cloud.google.com/asset-inventory/docs/asset-types`, extract tokens matching `[a-z0-9-]+\.googleapis\.com/[A-Za-z][A-Za-z0-9]*`, de-duplicate and sort into `scripts/kg_sources/gcp_asset_types.txt` |
| cloudflare | Terraform provider schema | In a temp dir with `required_providers { cloudflare = { source = "cloudflare/cloudflare" } }`: `terraform init && terraform providers schema -json > scripts/kg_sources/cloudflare_schema.json` |

Then run `python scripts/build_kg_catalog.py <provider> <source file>`.
