"""Run the local regression suite and retain UTF-8 output without shell redirection."""
import contextlib
import io
import json
from pathlib import Path
import unittest


def main() -> int:
    output = io.StringIO()
    suite = unittest.defaultTestLoader.discover("week11/tests")
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    root = Path("week11/results")
    root.mkdir(exist_ok=True)
    (root / "tests-recovery-20260915.txt").write_text(output.getvalue(), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "passed": result.wasSuccessful()}
    print(json.dumps(summary))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
