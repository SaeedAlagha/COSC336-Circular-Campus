"""Cross-platform setup and run helper for Circular Campus (Windows, macOS, Linux).

    python scripts/bootstrap.py setup            # venv + dependencies + .env + migrate + demo data (if empty)
    python scripts/bootstrap.py run [--port N]   # start the local server
    python scripts/bootstrap.py setup --postgres # also install the PostgreSQL driver
    python scripts/bootstrap.py setup --real     # empty real installation: no demo data, you create the first admin

Safe by design: never overwrites an existing .env, never deletes the database,
and the demo seed refuses to run when data already exists. Every step stops
with a clear message on failure.
"""
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
SUPPORTED = ((3, 12), (3, 13))


def say(msg):
    print(f"[circular-campus] {msg}", flush=True)


def fail(msg, code=1):
    print(f"\n[circular-campus] ERROR: {msg}\n", file=sys.stderr, flush=True)
    sys.exit(code)


def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, what):
    say(what + " …")
    result = subprocess.run([str(c) for c in cmd], cwd=str(ROOT))
    if result.returncode != 0:
        fail(f"{what} failed (exit code {result.returncode}). Scroll up for the reason; see README → Troubleshooting.")


def check_python():
    v = sys.version_info[:2]
    if v not in SUPPORTED:
        fail(f"Python {v[0]}.{v[1]} detected. Install Python 3.13 (or 3.12) from python.org and run setup again.")
    say(f"Python {sys.version.split()[0]} OK")


def write_env(demo=True):
    env = ROOT / ".env"
    if env.exists():
        say(".env already exists — left unchanged")
        return
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = example.replace("DJANGO_SECRET_KEY=change-me-setup-generates-this", f"DJANGO_SECRET_KEY={secrets.token_urlsafe(48)}")
    if not demo:
        text = text.replace("CAMPUS_DEMO_MODE=1", "CAMPUS_DEMO_MODE=0")
    env.write_text(text, encoding="utf-8")
    say("Created .env with a new random secret key (edit it to add GEMINI_API_KEY privately)")


def setup(postgres=False, real=False):
    check_python()
    if not venv_python().exists():
        run([sys.executable, "-m", "venv", VENV], "Creating virtual environment .venv")
    py = venv_python()
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"], "Updating pip")
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", ROOT / "requirements.txt"], "Installing dependencies")
    if postgres:
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", ROOT / "requirements-postgres.txt"], "Installing PostgreSQL driver")
    write_env(demo=not real)
    (ROOT / "data").mkdir(exist_ok=True)
    run([py, "manage.py", "migrate", "--noinput"], "Applying database migrations")
    if real:
        say("Real installation: no demo data. Create the first System Administrator account now.")
        result = subprocess.run([str(py), "manage.py", "init_system"], cwd=str(ROOT))
        if result.returncode != 0:
            say("init_system did not create an admin (accounts may already exist) — continuing.")
    elif (ROOT / "apps" / "core" / "management" / "commands" / "seed_demo.py").exists():
        run([py, "manage.py", "seed_demo"], "Loading synthetic demo data (skipped automatically if data exists)")
    else:
        say("Demo data is not part of this build yet. Create the first administrator with: python manage.py init_system")
    run([py, "manage.py", "check"], "Checking the project")
    say("Setup complete. Start the app with the run launcher (or: python scripts/bootstrap.py run).")


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def serve(port):
    if not venv_python().exists() or not (ROOT / ".env").exists():
        fail("Setup has not been run yet. Run the setup launcher first.")
    if not port_free(port):
        alt = next((p for p in range(port + 1, port + 20) if port_free(p)), None)
        fail(f"Port {port} is already in use (another server is running?). Stop it, or run with --port {alt or 8010}.")
    say(f"Open http://127.0.0.1:{port}/ in your browser. Press Ctrl+C to stop.")
    try:
        subprocess.run([str(venv_python()), "manage.py", "runserver", f"127.0.0.1:{port}"], cwd=str(ROOT), check=False)
    except KeyboardInterrupt:
        pass
    say("Server stopped.")


def main(argv):
    if not argv or argv[0] not in ("setup", "run"):
        print(__doc__)
        return 2
    if argv[0] == "setup":
        setup(postgres="--postgres" in argv, real="--real" in argv)
    else:
        port = 8000
        if "--port" in argv:
            try:
                port = int(argv[argv.index("--port") + 1])
            except (IndexError, ValueError):
                fail("Use --port followed by a number, e.g. --port 8010")
        serve(port)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
