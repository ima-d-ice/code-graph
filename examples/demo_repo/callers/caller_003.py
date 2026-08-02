"""Caller module 3 - uses compute_sum."""

from lib.utils import calculate_total, TWO, THRESHOLD


def use_sum_3(x, y):
    total = calculate_total(x, y)
    return total + TWO


def check_threshold_3(value):
    return value > THRESHOLD