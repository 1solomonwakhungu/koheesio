# HTTP response logging

HTTP steps log response status codes without response bodies. Raw responses, parsed JSON, and raw payload strings are omitted from output representations and the step's JSON/YAML representation helpers. This applies before an Okta access token has been identified, including nested response data and failed requests.

The returned data remains available through `output.response_raw`, `output.response_json` (also `json_payload`), and `output.raw_payload`. Serialization methods such as `model_dump()` still include these fields. Treat these objects and serialized data as sensitive when the endpoint returns credentials or personal data.

During a request, the `urllib3.connection`, `urllib3.connectionpool`, `urllib3.poolmanager`, `urllib3.response`, and `urllib3.util.retry` loggers emit generic event records without their diagnostic arguments or exception traces. This protects malformed-header and malformed-status diagnostics that the library emits before raising an exception. A context-local guard keeps this behavior out of unrelated threads and restores it after the request ends.

HTTP status errors retain their exception type and `response`/`request` attributes, but their displayed messages contain only the status code. Requests transport failures retain their exception type and attributes with a generic message; reflected server reason text and response headers are omitted. Applications can inspect retained attributes for diagnostics, but should avoid logging them without application-specific redaction.

Okta requires a 200 response with a JSON object containing a non-empty string `access_token`. The returned token remains a `SecretStr`. The token is cleared before each request, and invalid or missing token responses produce an error without embedding the response. A newly received empty or malformed JSON body clears the previous parsed JSON. If a request fails before a new response is processed, previous raw/JSON output may remain available for inspection, while the Okta token is cleared.

These protections cover response data in the default HTTP/Okta logs and representations. They do not redact arbitrary request URLs, request bodies, custom headers, application logging, custom adapters, or HTTP wire-debug output. Keep credentials out of URLs and use `SecretStr` for sensitive headers. Caller-visible raw response objects, exception attributes, and serialization results are not secret-free.
