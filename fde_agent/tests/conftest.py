"""Hide the store's DEBUG logs so test failures are readable."""
import re

try:
    from loguru import logger
    logger.remove()
    logger.add(lambda msg: None, level="WARNING")
except ImportError:
    pass


def pytest_terminal_summary(terminalreporter):
    """For the live tests (*_live.py): a pass rate per scenario (useful with FDE_REPEAT > 1).
    Runs of the same scenario are counted together: test_x[run2-case] counts as test_x[case]."""
    counts = {}
    for outcome in ("passed", "failed"):
        for report in terminalreporter.stats.get(outcome, []):
            if "_live.py::" not in getattr(report, "nodeid", "") or report.when != "call":
                continue
            name = re.sub(r"-?run\d+-?", "", report.nodeid.split("::")[1]).replace("[]", "")
            passed, total = counts.get(name, (0, 0))
            counts[name] = (passed + (outcome == "passed"), total + 1)
    if counts:
        terminalreporter.write_sep("=", "live scenarios: pass rate")
        for name, (passed, total) in sorted(counts.items()):
            terminalreporter.write_line(f"  {name}: {passed} of {total} passed")
