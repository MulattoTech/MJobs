# Requirement / Issue Catalogue Policy

The source prompt contains **301 numbered requirements (0–300)**. All are preserved as stable IDs
`MJ-REQ-000` … `MJ-REQ-300`.

The catalogue also contains **65 independently shippable child issues** for provider-specific
integrations that would otherwise be buried inside broad parent requirements.

Total issue seed entries: **366**.

Examples/enum values are acceptance details, not separate features. Independently deployable adapters/providers
are split. If implementation reveals another independently shippable capability, create a child issue under the
source requirement and add traceability before coding it.
