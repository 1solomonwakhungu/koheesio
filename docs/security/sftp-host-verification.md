# SFTP server identity verification

`SFTPWriter`, `SendCsvToSftp`, and `SendJsonToSftp` require an explicit
`known_hosts` file before connecting. Existing callers must add this setting.
Construction without it remains valid, but connecting raises `ValueError` before
opening a socket.

```python
writer = SendCsvToSftp(
    host="sftp.example.com",
    port=2222,
    username=username,
    password=password,
    known_hosts="/etc/pipeline/sftp_known_hosts",
    path="/incoming/export.csv",
)
writer.write(df)
```

Provision the server's public key through a trusted channel, such as your server
administrator or reviewed deployment configuration. The file uses OpenSSH
`known_hosts` syntax. For port 22, the host field is `sftp.example.com`. For port
2222, it is `[sftp.example.com]:2222`. Hashed host entries are supported.
An IP address and a DNS name are separate identities; the entry must match the
configured `host` after removing its optional `sftp://` prefix and trailing slash.

The writer loads only the supplied path. It does not search user or system SSH
files, contact an SSH agent, discover private keys, or add unknown keys. Keep the
file under the same access controls as other trusted pipeline configuration.
Fetching a key from an unverified connection alone does not establish trust.

An endpoint absent from this file is rejected before opening a connection. After
SSH negotiation, the negotiated server key must match the trusted entry before
the writer sends password authentication. A changed key, missing negotiated key
type, unreadable file, or invalid key data fails the operation. There is no
setting to disable verification. Password authentication retains Paramiko's
keyboard-interactive fallback after host verification.

List the server's permitted key types in the file. Paramiko chooses the host-key
algorithm during SSH negotiation; the selected key must be trusted. The writer
does not retry with a different algorithm after a mismatch. Use one key per
endpoint and key type. This API does not implement OpenSSH host certificates,
`@cert-authority` / `@revoked` directives, wildcard host patterns, or an OpenSSH
configuration parser.

For key rotation, verify the replacement key separately, replace the corresponding
entry in the managed file, and start a new transfer. Each new connection reloads
the file. An already open connection keeps the identity it authenticated during
its handshake. Coordinate same-type key replacements with the server rollout;
adding a second line of the same key type is not an overlap mechanism.

Transfer modes and buffer serialization are unchanged. Successful execution and
failures close existing SFTP channels and transports and clear their caches.
Code that opens `writer.client` or `writer.transport` directly must close those
resources, or call `writer._close_client()`, when finished. Cleanup never opens
another connection.

See Paramiko's [host-key API](https://docs.paramiko.org/en/stable/api/hostkeys.html)
and [transport API](https://docs.paramiko.org/en/stable/api/transport.html) for the
underlying file parsing, negotiation, and authentication behavior.
