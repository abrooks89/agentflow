"""Fixture: a local rebinding of a module-level function name is NOT a call edge."""


def rebound_fn():
    return 1


def rebinds_locally():
    rebound_fn = compute_factory()
    return rebound_fn()
