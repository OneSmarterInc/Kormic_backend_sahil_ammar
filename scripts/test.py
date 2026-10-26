"""Install test-only dependencies and run Django's suite in this interpreter."""
from pathlib import Path
import os
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements-scaling-test.txt"],
        cwd=root,
        check=True,
    )
    env = os.environ.copy()
    # Tests may explicitly override eager mode, but must never publish jobs to
    # a developer's or production broker inherited from .env.
    env.update(
        CELERY_BROKER_URL="memory://",
        CELERY_RESULT_BACKEND="cache+memory://",
        CELERY_TASK_ALWAYS_EAGER="true",
    )
    return subprocess.run(
        [sys.executable, "manage.py", "test", *sys.argv[1:]],
        cwd=root,
        env=env,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
