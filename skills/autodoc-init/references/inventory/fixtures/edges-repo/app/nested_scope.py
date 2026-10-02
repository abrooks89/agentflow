"""Fixture: nested defs are visible only inside their enclosing function."""


def epsilon_fn():
    def epsilon_helper():
        return 1
    return epsilon_helper()


class Zeta:
    def run(self):
        return epsilon_helper()
