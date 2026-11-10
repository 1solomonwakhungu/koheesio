# Dependency support and security floors

This fork keeps the core package on Python 3.9–3.12 and restricts the `snowflake`
extra to Python 3.11–3.12. Use a maintained patch release of Python; cryptography
50 excludes Python 3.9.0 and 3.9.1. The package metadata requires the connector
unconditionally within the extra, so requesting `koheesio[snowflake]` on Python
3.9 or 3.10 fails resolution. It does not install an older vulnerable connector
or silently omit the dependency.

## Required versions

| Dependency | Minimum | Scope | Reason |
|---|---:|---|---|
| cryptography | 50.0.0 | Core and every extra | Includes the fix for [GHSA-g6cj-pr64-35w5](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5) and earlier advisories. |
| pydantic | 2.4.0 | Core and every extra | Fixes [CVE-2024-3772](https://github.com/pydantic/pydantic/pull/7360), a regular-expression denial of service. |
| snowflake-connector-python | 5.1.0, below 6 | `snowflake` | Includes the audited driver fixes and the bundled certificate-validation fix described below. |
| protobuf | 6.33.5, below 7 | `snowflake` | Fixes [CVE-2026-0994](https://github.com/protocolbuffers/protobuf/pull/25239), a JSON recursion-depth bypass. |

The cryptography and pydantic floors apply to the core because the original
requirements admitted affected releases in ordinary installations. They are not
conditional on Snowflake. This is a dependency-policy change, not a claim that
Koheesio reaches every vulnerable operation in those packages.

## Why Snowflake requires Python 3.11

The vendor's September 2026 bulletins require connector 4.7.3 for
[diagnostic-log disclosures](https://www.snowflake.com/en/why-snowflake/snowflake-security-hub/security-bulletins/CVE-2026-86597/),
[workload-identity host validation](https://www.snowflake.com/en/why-snowflake/snowflake-security-hub/security-bulletins/CVE-2026-86600/),
and [OCSP response validation](https://www.snowflake.com/en/why-snowflake/snowflake-security-hub/security-bulletins/CVE-2026-85525/).
The Python 3.9-compatible 3.18.1 release only backports the
[TLS hostname-verification fix](https://www.snowflake.com/en/why-snowflake/snowflake-security-hub/security-bulletins/CVE-2026-15925/).
It does not fix all three September issues.

Connector 4.7.3–4.7.5 and 4.8.0 support Python 3.10 but require `filelock<4`.
The applicable [Unix lock-file advisory](https://github.com/tox-dev/filelock/security/advisories/GHSA-69hj-f68j-2879)
and [Windows permissions advisory](https://github.com/tox-dev/filelock/security/advisories/GHSA-8mx5-7m5m-r77g)
are fixed in filelock 4.2.1, which requires Python 3.11. Connector 4.8.1 allows
filelock 4.x but also requires Python 3.11. No compatible patched combination was
found for Python 3.9 or 3.10 as of 2026-11-10; this fork carries no exception.

The selected 5.x connector removes filelock and pyOpenSSL from its dependencies.
Its [5.1.0 release](https://docs.snowflake.com/en/release-notes/clients-drivers/python-connector-2026#version-5-1-0-oct-21-2026)
fixes bundled certificate validation covered by
[RUSTSEC-2026-0382](https://rustsec.org/advisories/RUSTSEC-2026-0382.html).
Version 5.0.0 is therefore excluded even though a Python package audit may not
report the bundled Rust issue. The major-version ceiling requires review before
adopting connector 6.

## Compatibility and validation

The 5.x connector uses Snowflake's Universal Core and has
[documented behavior changes](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-upgrade).
Certificate revocation uses CRLs instead of OCSP and is disabled by default;
this patch does not enable revocation checking or add a new connection policy.
The tested Koheesio surface preserves authentication parameters, private-key
input, query execution, result collection, and close-on-error behavior.
Version 5.x also enables temporary-credential caching by default and can conflict
with Snowpark, Snowflake ML, or SQLAlchemy releases that require connector <5.
Check those applications in separate environments before upgrading.
Authentication and queries are mocked; these checks do not validate a deployed
Snowflake account, cloud identity, network, or JVM connector.

Fresh wheel consumers were resolved on Python 3.9.25, 3.10.22, 3.11.15 and
3.12.11. The core was tested with cryptography 50.0.0 / pydantic 2.4.0 and current
versions. Snowflake integration tests passed on 3.11 and 3.12 with both connector
5.1.0 / protobuf 6.33.5 and current connector 5.2.0. Unsupported extra requests
failed on 3.9 and 3.10. The resolved dependency versions were checked against
OSV, alongside the vendor bulletins; no advisory exceptions were used.

The existing native Snowflake module imports a Spark utility, so its import
currently also requires PySpark. The integration tests installed PySpark 3.5.4
explicitly. Resolving the `snowflake` extra alone does not prove that module
imports without PySpark. Spark's JVM Snowflake connector is supplied by the
consumer and remains outside this Python dependency policy.

The default development environment uses Python 3.12. The Hatch test matrix
keeps the Python 3.10 Spark combinations, excludes the native Snowflake suite
there, and enables the `snowflake` extra on Python 3.11 and 3.12. Core-only
metadata checks run on all four supported minor versions with
`hatch run dependency-check:run`.

Other extras and deployment-specific dependencies still need their own
inventories and audits. An advisory-free resolution is a dated observation,
not a guarantee against undiscovered issues or future vulnerable releases.
