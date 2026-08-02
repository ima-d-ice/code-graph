"""Caller module 1 - uses compute_sum."""

from lib.utils import calculate_total, TWO, THRESHOLD


def use_sum_1(x, y):
    total = calculate_total(x, y)
    return total + TWO


def check_threshold_1(value):
    return value > THRESHOLD