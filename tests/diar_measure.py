# -*- coding: utf-8 -*-
"""Мера согласия двух разметок говорящих — общая для проверок и инструментов.

Считаем не «похоже на глаз», а согласие по кадрам: идём по записи шагом 0,1 с и
смотрим, совпал ли говорящий. Это та же мера, по которой оценивают диаризацию.
"""
from __future__ import annotations

GRID = 0.1          # шаг сетки согласия, секунды


def speaker_grid(turns, total_s, grid=GRID):
    """Кто говорит в каждой десятой доле секунды. None — тишина."""
    cells = int(total_s / grid) + 1
    out = [None] * cells
    for turn in turns or []:
        start = float(turn.get("start") or 0.0)
        end = float(turn.get("end") or 0.0)
        who = turn.get("speaker")
        for i in range(int(start / grid), min(int(end / grid) + 1, cells)):
            out[i] = who
    return out


def label_mapping(a, b):
    """Какой говорящий из первой разметки какому из второй соответствует.

    Имена у двух прогонов свои, поэтому подбираем пары по тому, как часто они
    встречаются вместе, — сначала самые частые.
    """
    pairs: dict = {}
    for x, y in zip(a, b):
        if x is None and y is None:
            continue
        pairs[(x, y)] = pairs.get((x, y), 0) + 1
    mapping: dict = {}
    used = set()
    for (x, y), _ in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if x in mapping or y in used:
            continue
        mapping[x] = y
        used.add(y)
    return mapping


def agreement(a, b):
    """Доля кадров (в процентах), где обе разметки говорят одно и то же."""
    mapping = label_mapping(a, b)
    same = sum(1 for x, y in zip(a, b) if mapping.get(x, x) == y)
    return round(100.0 * same / max(1, len(a)), 1)
