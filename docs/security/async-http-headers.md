# Async HTTP request headers

`AsyncHttpStep.get_headers()` returns a new plaintext dictionary on every call,
including when no headers are configured. Preparing a request leaves the stored
mapping unchanged. Values supplied as `SecretStr` remain `SecretStr` after
success, failure, retries, and cancellation. Plain strings are still accepted.

The stored header field is excluded from step representations as well as model
serialization. This covers both `headers=` and its `header=` alias, including
ordinary string API keys. The default step input and error logs therefore omit
the header map. The dictionary returned by `get_headers()` contains the actual
wire values and must not be logged.

## Errors and retry diagnostics

Response errors retain their aiohttp exception class, status, message, response
headers, redirect history, and request metadata. For example, callers can still
catch `ClientResponseError` or `ContentTypeError` and read `.status`.

The `request_info` attached to a response error is a `RequestInfo` subclass whose
representation omits request headers. Its tuple fields and header values remain
available, and `error.args` references the same metadata. Consequently, ordinary
`repr(error)`, `%r` logging, and argument representations no longer include the
request header map. The error's normal string representation is preserved.

`aiohttp-retry` can log an exception before the step catches it. Its logger is
therefore restricted to numeric attempt counts and response status codes. Other
retry messages become `Async HTTP retry event (details omitted)`; exception
tracebacks, formatting arguments, and extra context are not forwarded. The
application's logger and other steps are unaffected.

This is a representation boundary, not a general secret scrubber. Directly
logging `step.headers`, `get_headers()`, or `error.request_info.headers` can expose
values. URLs, server-provided reason phrases and response headers, redirect
responses, custom tracing, and third-party formatters also remain caller-managed.
A server that echoes a credential into one of those fields can expose it if the
caller logs that field. Do not put credentials in URLs or assume arbitrary
exception serialization is safe.

Internally created sessions and connectors still close and reset after each
run. Caller-supplied resources retain their existing close-on-completion
behavior. Concurrent execution of the same step instance remains unsupported.
