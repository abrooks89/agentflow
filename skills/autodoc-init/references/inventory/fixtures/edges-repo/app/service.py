"""Fixture: class with methods, self-calls."""


class Service:
    def run(self):
        return self.helper()

    def helper(self):
        return 1


def compute():
    return Service
