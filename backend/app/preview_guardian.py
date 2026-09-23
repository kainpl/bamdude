"""Legacy entrypoint; worker ownership lives in the shared guardian."""

from backend.app.worker_guardian import main as main

if __name__ == "__main__":
    raise SystemExit(main())
