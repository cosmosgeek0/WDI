#!/bin/sh
# Repocket 1.2.0 prints RP_API_KEY at startup. Filter before any log sink.
set -eu
umask 077
: "${RP_API_KEY:?RP_API_KEY is required}"
bb=/bin/busybox
work=$("$bb" mktemp -d)
trap '"$bb" rm -rf "$work"' EXIT
"$bb" mkfifo "$work/output"
"$bb" awk '
    BEGIN { key = ENVIRON["RP_API_KEY"] }
    {
        line = $0; out = ""
        while ((pos = index(line, key))) {
            out = out substr(line, 1, pos - 1) "[REDACTED]"
            line = substr(line, pos + length(key))
        }
        print out line; fflush()
    }
' < "$work/output" &
filter=$!
/usr/local/bin/repocket > "$work/output" 2>&1 &
client=$!
trap 'kill -TERM "$client" "$filter" 2>/dev/null || true' TERM INT
status=0
wait "$client" || status=$?
wait "$filter" || true
exit "$status"
