#!/usr/bin/env python3
"""Unit tests for the daily-digest builders (scripts/daily_digest.py).

Guards the revenue formatting, the email-safe Unicode sparkline, and the
Stocks-only Revenue column — the bits most likely to regress silently.
Plain asserts so it runs with bare `python tests/test_digest.py` (no pytest).
"""
import os, sys

# Set env BEFORE importing so the module's top-level guards don't sys.exit(0).
os.environ.setdefault('DASHBOARD_URL', 'http://example.test')
os.environ.setdefault('DRY_RUN', '1')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import daily_digest as dd  # noqa: E402


def test_fmt_rev_b():
    assert dd.fmt_rev_b(119.58) == '$120B'      # >=100 → whole billions
    assert dd.fmt_rev_b(26.04) == '$26B'        # trailing .0 stripped
    assert dd.fmt_rev_b(15.2) == '$15.2B'
    assert dd.fmt_rev_b(0.45) == '$450M'        # sub-billion → millions
    assert dd.fmt_rev_b(None) == '—'


def test_spark_direction():
    up = dd.spark([1, 2, 3, 4])
    dn = dd.spark([4, 3, 2, 1])
    assert up[0] == '▁' and up[-1] == '█'       # rising: low→high
    assert dn[0] == '█' and dn[-1] == '▁'       # falling: high→low
    assert dd.spark([]) == ''


def test_rev_cell():
    a = {'rev': {'quarters': [{'b': 13.5}, {'b': 18.1}, {'b': 22.1}, {'b': 26.04}],
                 'up': True, 'change_pct': 92.9}}
    cell = dd.rev_cell(a)
    assert '$26B' in cell and '+92.9%' in cell and '▲' in cell
    assert dd.rev_cell({'rev': None}) == '<span style="color:#bbb">—</span>'


def test_table_revenue_column_is_stocks_only():
    items = [{'sym': 'NVDA', 'price': 1, 'level': 1, '_dist': 0.0, 'signal': 'BREAKOUT',
              'rs_rating': 85, 'score': 88,
              'rev': {'quarters': [{'b': 13.5}, {'b': 26.04}], 'up': True, 'change_pct': 92.9, 'n': 2}}]
    stock_tbl = dd.table('Top Stocks', items, show_rev=True)
    etf_tbl = dd.table('Top ETFs', items)        # default: no revenue column
    assert 'Revenue (4Q)' in stock_tbl
    assert 'Revenue (4Q)' not in etf_tbl


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"OK — {len(tests)} digest tests passed")


if __name__ == '__main__':
    run()
