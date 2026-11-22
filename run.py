"""Launch the web service (kept as a stable path for scripts/systemd)."""

from app.main import run

if __name__ == "__main__":
    run()
