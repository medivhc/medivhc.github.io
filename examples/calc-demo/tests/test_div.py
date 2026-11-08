import pytest

from calc import div, mean


def test_div_by_zero_raises():
    with pytest.raises(ValueError, match="division by zero"):
        div(1, 0)


def test_mean_empty_is_none():
    assert mean([]) is None
