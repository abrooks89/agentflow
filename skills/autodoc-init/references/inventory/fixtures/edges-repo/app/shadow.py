"""Fixture: a parameter shadowing a module-level function is NOT a call edge."""


def shadowed_fn():
    return 1


def takes_param(shadowed_fn):
    return shadowed_fn()
