# Behavior changes and regression tests

- When behavior changes, update its regression tests in the same commit.
- Keep assertions tied to the intended behavior; do not weaken checks or skip
  failures merely to make the suite pass. Mock external services deterministically.
- Run backend tests with `python scripts/test.py --noinput` (optional Django
  test labels may be supplied). This installs `requirements-scaling-test.txt`,
  including fakeredis with Lua support, in the same interpreter before testing.
- Any CI or container test runner must install both `requirements.txt` and
  `requirements-scaling-test.txt`; test-only dependencies do not belong in the
  production runtime image.
