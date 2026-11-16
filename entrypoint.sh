#!/bin/sh
# Start as root only long enough to make sure the data folder belongs to the app's user, then drop to it.
# PUID / PGID (default 1000) choose that user, so the files in ./data are yours on the host.
set -e
if [ "$(id -u)" = "0" ]; then
    uid="${PUID:-1000}" gid="${PGID:-1000}"
    mkdir -p /data
    # only touch what isn't already right: quick on every start after the first
    find /data \( ! -user "$uid" -o ! -group "$gid" \) -exec chown "$uid:$gid" {} +
    exec setpriv --reuid "$uid" --regid "$gid" --clear-groups "$@"
fi
exec "$@"
