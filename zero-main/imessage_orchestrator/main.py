"""Backward-compatible entrypoint.

Use orchestrator.py going forward.
"""

from orchestrator import run


if __name__ == "__main__":
    run()
