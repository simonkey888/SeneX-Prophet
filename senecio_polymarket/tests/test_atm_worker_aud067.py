import unittest


def load_tests(loader, tests, pattern):
    # Repository-wide discovery loads the part modules directly; keep this
    # aggregator empty there to preserve globally unique unittest IDs.
    if pattern is not None:
        return unittest.TestSuite()
    try:
        from .test_atm_worker_aud067_part1 import WorkerR1Part1
        from .test_atm_worker_aud067_part2 import WorkerR1Part2
    except ImportError:
        from test_atm_worker_aud067_part1 import WorkerR1Part1
        from test_atm_worker_aud067_part2 import WorkerR1Part2
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(WorkerR1Part1))
    suite.addTests(loader.loadTestsFromTestCase(WorkerR1Part2))
    return suite
