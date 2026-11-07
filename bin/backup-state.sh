#!/usr/bin/env bash
# Consistent backup of curated-digest's ledger.
#
# Do not include the state directory in a plain file or tar backup: tarring a
# live SQLite database in WAL mode can capture a torn write and produce a
# backup that restores to a corrupt store. `sqlite3 .backup` uses
# the backup API, which takes a consistent snapshot of a database being written
# to. So the exclusion stays and this script handles state properly.
#
# What is lost without this: every "already reported" record. R15's first-run
# cap means a rebuilt store re-reports at most 10 items rather than flooding,
# so this is a comfort backup, not a disaster backup..
set -euo pipefail

DEST="${CURATED_DIGEST_BACKUP_DEST:-${XDG_STATE_HOME:-$HOME/.local/state}/curated-digest-backups}"
KEEP="${CURATED_DIGEST_BACKUP_KEEP:-14}"
STATE="${CURATED_DIGEST_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/curated-digest}"
DB="$STATE/curated_digest.db"

[[ -f "$DB" ]] || { echo "no ledger at $DB" >&2; exit 0; }
mkdir -p "$DEST"

OUT="$DEST/curated-digest-$(date +%Y%m%d).db"
sqlite3 "$DB" ".backup '$OUT.tmp'"

# Prove the snapshot is usable before replacing yesterday's. A backup nobody
# has ever restored is a rumour.
if [[ "$(sqlite3 "$OUT.tmp" 'PRAGMA integrity_check;')" != "ok" ]]; then
    rm -f "$OUT.tmp"
    echo "curated-digest backup failed integrity check" >&2
    exit 1
fi
sqlite3 "$OUT.tmp" "SELECT COUNT(*) FROM reported;" >/dev/null
mv "$OUT.tmp" "$OUT"
gzip -f "$OUT"
# The ledger is not secret, but it is state and it may live on shared storage.
chmod 600 "$OUT.gz"

ls -1t "$DEST"/curated-digest-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backed up $DB -> $OUT.gz"
