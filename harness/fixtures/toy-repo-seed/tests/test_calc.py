import calc


def test_add():
    assert calc.add(2, 3) == 5


def test_sub():
    assert calc.sub(5, 2) == 3


def test_mul():
    assert calc.mul(4, 3) == 12
