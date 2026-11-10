# Tableau connection security

`TableauServer` and `TableauHyperPublisher` require an explicit HTTPS URL and verify the server certificate. Bare hostnames, HTTP URLs, embedded credentials, query strings, and fragments are rejected. Supply `site_id` separately from the server URL.

Publicly trusted certificates use Requests' normal trust configuration. For a private certificate authority, pass `ca_bundle="/path/to/private-ca.pem"` (a `pathlib.Path` is also accepted). The path must identify an existing file. Requests validates its contents and the server certificate during the connection. There is no option to disable verification, and authentication does not suppress TLS warnings globally.

All redirects are rejected with `requests.exceptions.TooManyRedirects`, including redirects to another HTTPS host or to a different path on the same host. Configure the final HTTPS URL. This restriction covers both Requests' redirects and TSC's separate redirect handling, preventing replay of a password, PAT, or Tableau session token to a redirect destination. Deployments that require a redirect during API authentication or publishing must provide a direct endpoint.

Password authentication is unchanged. A complete `token_name` / `token_value` pair takes precedence, and the token is unwrapped only when creating TSC credentials. Incomplete token configuration retains the existing password fallback. `user` and `password` remain required inputs, including when a PAT is configured.

The TSC `server` object remains accessible on the step, but is excluded from step representations and serialization. TSC's own representation can fetch server information, so applications should avoid logging the client object directly.

Transport failures retain their Requests exception class with a fixed diagnostic message. The original transport text and attached request/response are omitted because TSC can log that text. Server API errors retain TSC's exception types and codes. These exceptions can contain raw response bodies, including data supplied by the server; applications should log the class and code instead of the exception text, response body, or exception attributes. Suppressed exception context also remains accessible through Python introspection and must not be treated as a redaction boundary for custom traceback tooling.

The regression tests cover TSC 0.25 and 0.42 with synthetic credentials and mocked HTTP adapters. They verify request settings, password/PAT serialization, redirect refusal, and Koheesio/TSC log output. They do not validate a deployment's certificate chain, proxy, credentials, or behavior in other TSC versions. Custom session hooks, adapters, and external HTTP debug instrumentation can log request material outside this integration's controls.
