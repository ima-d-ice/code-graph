"""Caller module 4 - uses compute_sum."""

from lib.utils import calculate_total, TWO, THRESHOLD


def use_sum_4(x, y):
    total = calculate_total(x, y)
    return total + TWO


def check_threshold_4(value):
    return value > THRESHOLD