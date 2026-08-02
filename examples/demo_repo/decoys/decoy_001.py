"""Decoy module 1 - must NOT be touched by the rename."""

from lib.utils import calculate_product
from lib.utils_v2 import legacy_add


def use_product_1(a, b):
    return calculate_product(a, b) + legacy_add(a, b)
