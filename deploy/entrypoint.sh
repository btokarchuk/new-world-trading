#!/bin/sh
# Trust store assembly. A corporate TLS-intercepting proxy needs its CA
# trusted IN ADDITION to the public roots — replacing the bundle with the
# corp CA alone fails verification the moment the host leaves the corp
# network (the real chain no longer verifies). Build the union at start.
set -e
BUNDLE=/tmp/nwt-ca-bundle.pem
cat /etc/ssl/certs/ca-certificates.crt > "$BUNDLE"
for cert in /certs/*.pem /certs/*.crt; do
    [ -f "$cert" ] && cat "$cert" >> "$BUNDLE"
done
export SSL_CERT_FILE="$BUNDLE"
exec /usr/bin/tini -- "$@"
