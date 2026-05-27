import time
from pathlib import Path


def maintain_logs(logs_dir: Path, max_lines: int, retention_days: int) -> None:
    if not logs_dir.is_dir():
        return

    cutoff = time.time() - retention_days * 24 * 3600

    for log_file in logs_dir.glob("*.log"):
        if log_file.stat().st_mtime < cutoff:
            log_file.unlink()
            continue

        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            log_file.write_text("\n".join(lines[-max_lines:]), encoding="utf-8")
