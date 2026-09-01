import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BACKUP = (ROOT / "operations/recovery/backup-postgres.sh").read_text()
RESTORE = (ROOT / "operations/recovery/verify-isolated-restore.sh").read_text()
FRESHNESS = ROOT / "operations/recovery/check-recovery-freshness.sh"


def test_backup_is_encrypted_atomic_and_release_bound():
    assert "pg_dump" in BACKUP and "--format=custom" in BACKUP
    assert "pg_restore --list" in BACKUP
    assert "gpg --batch" in BACKUP and "--encrypt" in BACKUP
    assert 'shred -u "$work/database.dump"' in BACKUP
    assert "RELEASE_SHA=$CODESTRA_RELEASE_SHA" in BACKUP
    assert "IMAGE_DIGEST=$CODESTRA_IMAGE_DIGEST" in BACKUP
    assert "sha256sum database.dump.gpg METADATA >SHA256SUMS" in BACKUP
    assert 'sha256sum "$work/database.dump.gpg"' not in BACKUP
    assert 'mv "$marker" "$backup_root/LAST_SUCCESS"' in BACKUP
    assert "echo \"$POSTGRES_DSN\"" not in BACKUP


def test_restore_is_explicit_isolated_and_verifying():
    assert 'ALLOW_ISOLATED_RESTORE:-false' in RESTORE
    assert '[[ "$target_database" != "$source_database" ]]' in RESTORE
    assert '[[ "$target_database" =~ (^|_)restore(_|$) ]]' in RESTORE
    assert "sha256sum -c SHA256SUMS" in RESTORE
    assert "pg_restore" in RESTORE and "--exit-on-error" in RESTORE
    assert "ai_requests" in RESTORE
    assert "uq_ai_request_idempotency" in RESTORE
    assert "RESTORE=PASS" in RESTORE
    assert 'sha256sum "$result_name"' in RESTORE
    assert 'sha256sum "$result"' not in RESTORE
    assert "echo \"$POSTGRES_DSN\"" not in RESTORE


def test_no_automatic_destructive_production_path():
    combined = BACKUP + RESTORE
    assert "drop database" not in combined.lower()
    assert "createdb" not in combined.lower()
    assert "migrations/001_stage4.down.sql" not in combined
    assert "migrations/002_stage5.down.sql" not in combined


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o700)


def _mock_tools(root: Path) -> Path:
    tools = root / "bin"
    tools.mkdir()
    _executable(tools / "psql", 'echo "${MOCK_PSQL_VALUE:-codestra_ai}"\n')
    _executable(
        tools / "pg_dump",
        'for arg in "$@"; do case "$arg" in --file=*) out=${arg#--file=};; esac; done\n'
        ': "${out:?}"\nprintf "synthetic-dump" >"$out"\n',
    )
    _executable(tools / "pg_restore", "exit 0\n")
    _executable(
        tools / "gpg",
        'out=\ninput=\nwhile [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --output) out=$2; shift 2;; --recipient) shift 2;; --*) shift;; *) input=$1; shift;; esac\n'
        'done\n: "${out:?}"\n: "${input:?}"\ncp "$input" "$out"\n',
    )
    _executable(tools / "shred", 'rm -f "${2:?}"\n')
    _executable(tools / "sync", "exit 0\n")
    return tools


def test_backup_publishes_relocatable_verified_manifest():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = _mock_tools(root)
        backup_root = root / "backups"
        result = subprocess.run(
            [str(ROOT / "operations/recovery/backup-postgres.sh")],
            env={
                **os.environ,
                "PATH": f"{tools}:{os.environ['PATH']}",
                "POSTGRES_DSN": "postgresql://synthetic.invalid/codestra_ai",
                "CODESTRA_AI_BACKUP_ROOT": str(backup_root),
                "CODESTRA_RELEASE_SHA": "1" * 40,
                "CODESTRA_IMAGE_DIGEST": "sha256:" + "2" * 64,
                "CODESTRA_BACKUP_GPG_RECIPIENT": "synthetic-test-recipient",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        stamp = (backup_root / "LAST_SUCCESS").read_text().strip()
        published = backup_root / stamp
        assert not (published / "database.dump").exists()
        assert (published / "database.dump.gpg").is_file()
        verified = subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"],
            cwd=published,
            text=True,
            capture_output=True,
            check=False,
        )
        assert verified.returncode == 0, verified.stderr
        assert str(root) not in (published / "SHA256SUMS").read_text()


def test_restore_refuses_source_database_identity_before_pg_restore():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = _mock_tools(root)
        backup = root / "backup"
        backup.mkdir()
        (backup / "database.dump.gpg").write_text("synthetic")
        (backup / "METADATA").write_text(
            "SCHEMA=codestra-ai-backup.v1\nSTAMP=20260901T000000Z\n"
            f"DATABASE=codestra_ai\nRELEASE_SHA={'1' * 40}\n"
            f"IMAGE_DIGEST=sha256:{'2' * 64}\nENCRYPTION=OPENPGP\n"
        )
        subprocess.run(
            ["sha256sum", "database.dump.gpg", "METADATA"],
            cwd=backup,
            text=True,
            stdout=(backup / "SHA256SUMS").open("w"),
            check=True,
        )
        result = subprocess.run(
            [str(ROOT / "operations/recovery/verify-isolated-restore.sh")],
            env={
                **os.environ,
                "PATH": f"{tools}:{os.environ['PATH']}",
                "POSTGRES_DSN": "postgresql://synthetic.invalid/codestra_ai",
                "CODESTRA_AI_BACKUP_DIR": str(backup),
                "CODESTRA_AI_RESTORE_EVIDENCE_DIR": str(root / "evidence"),
                "ALLOW_ISOLATED_RESTORE": "true",
                "MOCK_PSQL_VALUE": "codestra_ai",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "refusing restore into source database identity" in result.stderr


def test_freshness_passes_current_marker_and_fails_stale_marker():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current = subprocess.run(
            ["date", "-u", "+%Y%m%dT%H%M%SZ"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        (root / current).mkdir()
        (root / "LAST_SUCCESS").write_text(current + "\n")
        env = {
            **os.environ,
            "CODESTRA_RECOVERY_ROOT": str(root),
            "CODESTRA_RECOVERY_MAX_AGE_SECONDS": "120",
        }
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 0
        stale = "20200101T000000Z"
        (root / stale).mkdir()
        (root / "LAST_SUCCESS").write_text(stale + "\n")
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 1
