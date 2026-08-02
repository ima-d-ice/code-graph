"""Caller module 8 - uses compute_sum."""

from lib.utils import calculate_total, TWO, THRESHOLD


def use_sum_8(x, y):
    total = calculate_total(x, y)
    return total + TWO


def check_threshold_8(value):
    return value > THRESHOLD