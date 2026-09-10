# Backup, restore, and rollback authority

These commands are source controls; they do not authorize a production change.

Before a migration or rollout, an authorized operator runs
`operations/recovery/backup-postgres.sh` with libpq `PGHOST`, `PGPORT`,
`PGDATABASE`, `PGUSER`, and an owner-protected `PGPASSFILE`, a
root-owned mode-0700 backup directory, the exact release SHA, exact image digest,
an approved OpenPGP recovery recipient, and a pinned backup-signing fingerprint.
The command creates a custom-format dump in a verified tmpfs work root,
PostgreSQL dump, validates its catalog, encrypts it, removes the plaintext, and
atomically publishes a signed manifest, checksums, immutable release metadata,
and `LAST_SUCCESS`. Freshness binds the marker to authenticated metadata.
It keeps database credentials out of process arguments and never prints the
passfile or encryption recipient.

Restore verification must use a disposable database whose name explicitly
contains `restore`, is empty, uses a verified tmpfs restore work root, and
differs from the source database recorded in
the backup. The caller supplies the exact expected release SHA and image digest.
Set `ALLOW_ISOLATED_RESTORE=true` and run
`operations/recovery/verify-isolated-restore.sh`. It verifies the pinned signer,
checksums, and exact release tuple,
decrypts only inside a temporary mode-0700 directory, restores with
`--exit-on-error`, and checks the table, required columns, and tenant/idempotency
index before atomically publishing a checksum-bearing result.

`operations/recovery/check-recovery-freshness.sh` evaluates either a published
backup directory or checksum-bearing restore result against an explicitly
supplied maximum age. The deployment owner remains responsible for approving
the actual RPO/RTO thresholds and scheduling the checks.

Rollback requires the release record to identify both current and previous Git
SHA/image digest tuples. Restore is not the default rollback for an application
failure: first redeploy the reviewed previous immutable image after confirming
schema compatibility. Use database restore only under an approved recovery
decision, against a separately created empty database first, with retained
pre-change backup and reconciliation evidence. Never run a down migration or
restore against production automatically.

Required runtime certification remains:

- scheduled encrypted backups and off-host copy;
- freshness within the approved RPO;
- an isolated restore result within the approved RTO;
- current and previous immutable release tuples; and
- a reviewed deployment rollback rehearsal.
