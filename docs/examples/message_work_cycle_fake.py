"""Run the deterministic two-environment messaging/work-cycle fixture."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def main() -> int:
    names = (
        "tests.test_messaging.MessagingTests.test_two_environment_publish_read_reply_and_exact_references",
        "tests.test_work_cycle.WorkCycleTests.test_actionable_reply_memory_and_fresh_process_do_not_redispatch",
    )
    suite = unittest.TestLoader().loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("Offline fixture completed: publish/read/reply, exact State, memory recovery, and no redispatch.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
