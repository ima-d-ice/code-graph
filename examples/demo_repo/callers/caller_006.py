"""Caller module 6 - uses compute_sum."""

from lib.utils import calculate_total, TWO, THRESHOLD


def use_sum_6(x, y):
    total = calculate_total(x, y)
    return total + TWO


def check_threshold_6(value):
    return value > THRESHOLD