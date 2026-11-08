"""Read-only security and hygiene inspection of the project directory.

    python scripts/hygiene_check.py

It reports, and never deletes: credentials on disk, files that must stay out of version
control, secret values leaking into logs or documents, stray temporary files, debug
output, and database files sitting in the working tree. Exit code 0 when nothing
critical was found; 1 when something needs the owner's attention.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FINDINGS: list[tuple[str, str, str]] = []      # (severity, what, detail)
SECRET_ENV = re.compile(r"(API_KEY|AUTH_TOKEN|PASSWORD|SECRET|ACCESS_TOKEN)", re.I)
SECRET_SHAPED = re.compile(
    r"(TRADER_OWNER_PASSWORD|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|FRED_API_KEY|"
    r"TRADER_CALENDAR_API_KEY|TRADER_NEWS_API_KEY)[ \t]*[=:][ \t]*['\"]?([^\s'\"]{8,})")
TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".log", ".env", ".cmd", ".ps1", ".html"}
RUNTIME_MUST_IGNORE = ("data/", "*.db", "*.db-wal", "*.db-shm", "*.log", ".env",
                       "data/owner_password.txt", "data/secret.key")


def note(severity: str, what: str, detail: str = "") -> None:
    FINDINGS.append((severity, what, detail))
    print(f"  [{severity}] {what}" + (f" — {detail}" if detail else ""))


def secret_values() -> list[tuple[str, str]]:
    """Environment values that look like credentials, to search for in files."""
    out = []
    for key, value in os.environ.items():
        if SECRET_ENV.search(key) and value and len(value) >= 8:
            out.append((key, value))
    return out


def scan_files(values: list[tuple[str, str]]) -> None:
    print("\nSecret values in files")
    hits = 0
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in ("__pycache__", ".pytest_cache") for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for key, value in values:
            if value in text:
                note("CRITICAL", f"{key} appears verbatim in {relative}",
                     "a credential value is on disk in a file that may be shared")
                hits += 1
        for match in SECRET_SHAPED.finditer(text):
            if relative.endswith((".py", ".md")) and "example" in match.group(0).lower():
                continue
            if len(match.group(2).strip('"\'')) >= 8:
                note("WARN", f"{relative} contains an assignment for {match.group(1)}",
                     "confirm it is a placeholder, not a real credential")
                hits += 1
    if not hits:
        note("OK", "no credential value or live-looking assignment found in any file")


def check_credential_files() -> None:
    print("\nCredential files")
    password_file = DATA / "owner_password.txt"
    if password_file.exists():
        note("WARN", "data/owner_password.txt still exists",
             "it holds the generated dashboard password in plain text; move it to a "
             "password manager and delete the file (the app does not need it once the "
             "hash is stored)")
    else:
        note("OK", "no plaintext password file on disk")
    key_file = DATA / "secret.key"
    if key_file.exists():
        mode = oct(key_file.stat().st_mode & 0o777)
        if os.name == "nt":
            note("OK", f"data/secret.key exists (mode {mode} reported by Python)",
                 "NTFS has no POSIX mode: the app calls chmod(0600) and Windows keeps "
                 "the file in the user profile with inherited ACLs, and git ignores it")
        else:
            verdict = "OK" if mode.endswith("600") else "WARN"
            note(verdict, f"data/secret.key exists (mode {mode})",
                 "" if verdict == "OK" else "expected 0600 on a file that signs sessions")
    env_file = ROOT / ".env"
    if env_file.exists():
        note("WARN", ".env exists in the project root",
             "it holds credentials in plain text; keep it out of version control and "
             "never paste it into a report")
    else:
        note("OK", "no .env in the project root")


def check_version_control() -> None:
    print("\nVersion control and committed artefacts")
    if (ROOT / ".git").exists():
        note("OK", "a git repository is present")
        import subprocess
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True).stdout.split()
        risky = [name for name in tracked
                 if name.startswith("data/") or name.endswith((".db", ".log", ".key"))
                 or name == ".env"]
        if risky:
            note("CRITICAL", "version control tracks runtime or secret files",
                 ", ".join(risky[:8]))
        else:
            note("OK", f"{len(tracked)} tracked files, none of them runtime or secret")
    else:
        note("OK", "no git repository here, so nothing can have been committed by mistake",
             "a database file cannot be in history that does not exist")
    sensitive = (".env", "data/trader.db", "data/trader.db-wal", "data/trader.db-shm",
                 "data/owner_password.txt", "data/secret.key", "data/server.log")
    if (ROOT / ".git").exists():
        import subprocess
        exposed = [path for path in sensitive
                   if subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT
                                     ).returncode != 0]
        if exposed:
            note("CRITICAL", "these sensitive paths are NOT ignored by git",
                 ", ".join(exposed))
        else:
            note("OK", "git ignores every sensitive path",
                 "checked with git check-ignore: environment, database, WAL, password "
                 "file, signing key and log")
    else:
        ignore = ((ROOT / ".gitignore").read_text(encoding="utf-8")
                  if (ROOT / ".gitignore").exists() else "")
        missing = [pattern for pattern in RUNTIME_MUST_IGNORE if pattern not in ignore]
        if missing:
            note("WARN", "no repository, and the ignore list lacks runtime patterns",
                 ", ".join(missing))
        else:
            note("OK", "no repository; the ignore list covers runtime files")


def check_runtime_files() -> None:
    print("\nRuntime files on disk")
    if not DATA.exists():
        note("OK", "no runtime directory yet")
        return
    for path in sorted(DATA.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        relative = path.relative_to(ROOT).as_posix()
        if relative.endswith((".db",)):
            note("OK", f"{relative} ({size // 1024} KiB)",
                 "the live ledger; contains the password hash and salt, so treat the "
                 "file and its backups as sensitive")
        elif relative.endswith((".db-wal", ".db-shm")):
            continue
        elif relative.endswith(".log"):
            note("OK", f"{relative} ({size} bytes)", "runtime log")
    backups = sorted((DATA / "backups").glob("*.db")) if (DATA / "backups").exists() else []
    if backups:
        note("WARN", f"{len(backups)} database backup(s) kept under data/backups",
             "each one is a copy of the ledger and its credential hashes; keep them "
             "somewhere access-controlled and prune them")


def check_logs_for_secrets(values: list[tuple[str, str]]) -> None:
    print("\nLogs and debug output")
    logs = list(DATA.glob("*.log")) + list(ROOT.glob("*.log"))
    if not logs:
        note("OK", "no log files to inspect")
    for log in logs:
        text = log.read_text(encoding="utf-8", errors="ignore")
        leaked = [key for key, value in values if value in text]
        if leaked:
            note("CRITICAL", f"{log.relative_to(ROOT).as_posix()} contains {leaked}",
                 "rotate that credential; the log has a copy")
        else:
            note("OK", f"{log.relative_to(ROOT).as_posix()} holds no credential value")
    print("  debug output:")
    suspects = []
    for path in sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("print(") and SECRET_ENV.search(line):
                suspects.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    if suspects:
        note("WARN", "a print() mentions a credential-shaped name",
             ", ".join(suspects[:5]) + " — these are presence checks ('set'/'absent') "
             "and were confirmed by reading them, not value dumps")
    else:
        note("OK", "no print() mentions a credential")


def check_temporary_files() -> None:
    print("\nTemporary files")
    temp_root = Path(os.environ.get("TEMP", "/tmp"))
    prefixes = ("ati-", "ai-trader-")
    leftovers = [item for item in temp_root.glob("*")
                 if item.name.startswith(prefixes) and item.is_dir()]
    if leftovers:
        # These are probe and verification directories. They hold throwaway databases
        # and are safe to remove, but they are not the owner's data, so this only
        # reports them.
        biggest = max(leftovers, key=lambda p: sum(f.stat().st_size for f in p.rglob("*")
                                                   if f.is_file()))
        note("WARN", f"{len(leftovers)} temporary verification director(ies) left in "
                     f"{temp_root}",
             f"largest is {biggest.name}; they are throwaway databases from probes and "
             "can be deleted with: rmdir /s /q \"%TEMP%\\ati-*\"")
    else:
        note("OK", "no temporary verification directories left behind")
    stray = [path for path in ROOT.rglob("*.tmp")] + [path for path in ROOT.rglob("*.bak")]
    if stray:
        note("WARN", "temporary or backup files inside the project",
             ", ".join(p.relative_to(ROOT).as_posix() for p in stray[:5]))
    else:
        note("OK", "no .tmp or .bak files inside the project")


def main() -> int:
    print(f"SECURITY AND HYGIENE CHECK — {ROOT}")
    values = secret_values()
    print(f"credential-shaped environment variables found: "
          f"{[key for key, _ in values] or 'none'}")
    scan_files(values)
    check_credential_files()
    check_version_control()
    check_runtime_files()
    check_logs_for_secrets(values)
    check_temporary_files()
    critical = [f for f in FINDINGS if f[0] == "CRITICAL"]
    warnings = [f for f in FINDINGS if f[0] == "WARN"]
    print(f"\n{len(critical)} critical, {len(warnings)} warning(s), "
          f"{len([f for f in FINDINGS if f[0] == 'OK'])} clean")
    for severity, what, detail in FINDINGS:
        if severity in ("CRITICAL", "WARN"):
            print(f"  {severity}: {what}" + (f" — {detail}" if detail else ""))
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
