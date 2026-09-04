"""Minimal traceable analysis example."""

from __future__ import annotations


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


if __name__ == "__main__":
    print(mean([1.0, 2.0, 3.0]))
