"""Fixture: self.method() must resolve ONLY within the enclosing class."""


class Alpha:
    def helper(self):
        return 1


class Beta:
    def run(self):
        return self.helper()
