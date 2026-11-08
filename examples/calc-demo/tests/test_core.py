from calc import add, mul, sub


def test_add(): assert add(2, 3) == 5
def test_add_neg(): assert add(-2, -3) == -5
def test_sub(): assert sub(5, 3) == 2
def test_sub_neg(): assert sub(-1, 1) == -2
def test_mul(): assert mul(4, 5) == 20
def test_mul_zero(): assert mul(7, 0) == 0
def test_mul_neg(): assert mul(-2, 3) == -6
def test_add_float(): assert abs(add(0.1, 0.2) - 0.3) < 1e-9
