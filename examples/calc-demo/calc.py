"""Tiny calculator — shipped with deliberate bugs for the RVD demo (scenario A/B)."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    return a / b  # BUG: spec says div by zero must raise ValueError("division by zero")


def mean(xs):
    return sum(xs) / len(xs)  # BUG: spec says mean([]) must return None
