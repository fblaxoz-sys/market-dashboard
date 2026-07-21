#!/usr/bin/env python3
"""
Turn a brokerage export into a paste block for the tracker's
"+ From what I own" box.

    python3 tools/portfolio_import.py statement.xlsx
    python3 tools/portfolio_import.py statement.csv --sheet "Positions"

Brokerage exports are messy in predictable ways — junk rows above the header,
disclaimer rows below it, "$1,234.56" strings, cost basis given as a total
instead of per-share, money-market funds masquerading as holdings. This reads
around all of that, and where it can't be sure it says so instead of guessing.

Output is `TICKER, percent-now, avg-cost` lines, which is exactly what
"+ From what I own" expects: the percent is what the holding is worth TODAY as
a share of the whole account, and the tracker solves entry weights backwards
from it. Cash is deliberately NOT emitted — the tracker derives cash as
whatever the weights leave short of 100%, so excluding it produces the right
number automatically.
"""
import sys, os, re, argparse

# ── column detection ─────────────────────────────────────────────────────────
# Longest/most-specific patterns first: "average cost per share" must beat "cost".
COLS = {
    'symbol':    ['symbol', 'ticker', 'security symbol', 'security id', 'sym'],
    'desc':      ['description', 'security description', 'security name',
                  'investment name', 'security', 'name'],
    'qty':       ['quantity', 'shares', 'share quantity', 'qty', 'units'],
    'value':     ['current market value', 'market value', 'current value',
                  'position value', 'mkt value', 'market val', 'mkt val',
                  'value', 'val'],
    'cost_ps':   ['average cost per share', 'avg cost per share', 'cost per share',
                  'average cost basis per share', 'average price paid',
                  'average cost', 'avg cost', 'avg price', 'unit cost', 'cost/share'],
    'cost_tot':  ['total cost basis', 'cost basis total', 'cost basis', 'total cost',
                  'book value', 'adjusted cost basis'],
    'price':     ['last price', 'current price', 'market price', 'closing price', 'price'],
    'pct':       ['% of account', 'percent of account', '% of portfolio', 'weight', 'allocation'],
}

# Money-market / sweep tickers that are cash in everything but name.
CASH_TICKERS = {'SPAXX', 'FDRXX', 'FZFXX', 'VMFXX', 'VMRXX', 'SWVXX', 'SNVXX', 'SNSXX',
                'TIMXX', 'MMDA', 'CASH', 'USD', 'FCASH', 'QACDS', 'BIL'}
CASH_WORDS = re.compile(r'\b(cash|money\s*market|sweep|settlement fund|deposit)\b', re.I)

# Totals/subtotals sit in the symbol column carrying the largest number in the
# file. Letting one through inflates the account and silently skews every
# percentage, so they're matched explicitly rather than inferred.
TOTAL_WORDS = re.compile(
    r'^\s*(grand\s+)?(total|subtotal|sub-total|net|sum|account\s+total|'
    r'portfolio\s+total|totals)\b', re.I)


def norm(s):
    return re.sub(r'[^a-z0-9% ]+', ' ', str(s or '').strip().lower())


def to_num(v):
    """'$1,234.56' / '(123.45)' / '12.3%' → float, or None."""
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip()
    if not s or s in {'-', '--', 'N/A', 'n/a'}: return None
    neg = s.startswith('(') and s.endswith(')')
    s = re.sub(r'[^0-9.\-]', '', s)
    if s in {'', '-', '.'}: return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


# Broker spellings Yahoo rejects. Can't be inferred — "BRKB" is indistinguishable
# from a real 4-letter ticker — so the common ones are listed explicitly.
TICKER_ALIASES = {'BRKB': 'BRK-B', 'BRKA': 'BRK-A', 'BFB': 'BF-B', 'BFA': 'BF-A',
                  'LENB': 'LEN-B', 'HEIA': 'HEI-A'}


def yahoo_ticker(sym):
    """Normalise a broker symbol to what Yahoo expects. Returns (symbol, note)."""
    s = str(sym or '').strip().upper()
    s = re.sub(r'\s+', '', s)
    if not s: return '', None
    if s in TICKER_ALIASES:
        return TICKER_ALIASES[s], f'{s} → {TICKER_ALIASES[s]} (Yahoo spelling)'
    # Class shares: BRK.B / BRKB → BRK-B  (Yahoo uses a dash)
    m = re.fullmatch(r'([A-Z]{1,5})[.\-/]([A-Z])', s)
    if m:
        out = f'{m.group(1)}-{m.group(2)}'
        return out, (f'{sym} → {out}' if out != s else None)
    if re.fullmatch(r'[0-9]{6,}[A-Z0-9]*', s):
        return s, f'{s} looks like a CUSIP, not a ticker — Yahoo will not price it'
    if not re.fullmatch(r'[A-Z0-9.\-^]{1,12}', s):
        return s, f'{s} is an unusual symbol — check it prices on Yahoo'
    return s, None


# ── reading ──────────────────────────────────────────────────────────────────
def read_rows(path, sheet=None):
    ext = os.path.splitext(path)[1].lower()
    if ext in {'.csv', '.tsv', '.txt'}:
        import csv
        delim = '\t' if ext == '.tsv' else ','
        with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
            return [r for r in csv.reader(f, delimiter=delim)]
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)   # data_only → formula results
    ws = wb[sheet] if sheet else wb.active
    return [[c for c in row] for row in ws.iter_rows(values_only=True)]


def find_header(rows):
    """The header is the first row that names a symbol column AND something numeric-ish."""
    for i, row in enumerate(rows[:40]):
        cells = [norm(c) for c in row]
        has_sym = any(any(cell == p or cell.startswith(p) for p in COLS['symbol']) for cell in cells)
        has_num = any(any(p in cell for p in COLS['value'] + COLS['qty'] + COLS['price'])
                      for cell in cells)
        if has_sym and has_num:
            return i
    for i, row in enumerate(rows[:40]):                  # fallback: symbol alone
        if any(norm(c) in COLS['symbol'] for c in row):
            return i
    return None


def map_columns(header):
    """header cell index → our field name. Most specific pattern wins."""
    out = {}
    cells = [norm(c) for c in header]
    for field, pats in COLS.items():
        best_i, best_len = None, -1
        for i, cell in enumerate(cells):
            if not cell or i in out: continue
            for p in pats:
                if (cell == p or cell.startswith(p) or p in cell) and len(p) > best_len:
                    best_i, best_len = i, len(p)
        if best_i is not None:
            out[best_i] = field
    return out


# ── conversion ───────────────────────────────────────────────────────────────
def convert(path, sheet=None):
    rows = read_rows(path, sheet)
    if not rows:
        raise SystemExit('That file has no rows.')
    hi = find_header(rows)
    if hi is None:
        raise SystemExit(
            'Could not find a header row with a Symbol/Ticker column.\n'
            'Check --sheet, or send the file and it can be mapped by hand.')
    colmap = map_columns(rows[hi])
    fields = set(colmap.values())
    if 'symbol' not in fields:
        raise SystemExit('No Symbol/Ticker column found.')

    def get(row, field):
        for i, f in colmap.items():
            if f == field and i < len(row):
                return row[i]
        return None

    holdings, cash_total, notes, skipped = [], 0.0, [], []
    for row in rows[hi + 1:]:
        if not row or all(c in (None, '') for c in row): continue
        raw_sym = get(row, 'symbol')
        desc    = str(get(row, 'desc') or '')
        if raw_sym in (None, '') and not desc.strip():
            continue

        sym_u = str(raw_sym or '').strip().upper()
        label = sym_u or desc.strip()
        value = to_num(get(row, 'value'))
        qty   = to_num(get(row, 'qty'))
        price = to_num(get(row, 'price'))
        if value is None and qty is not None and price is not None:
            value = qty * price

        # Classify BEFORE any numeric filtering. Cash and totals lines often
        # carry no quantity and no price; dropping them as "not a holding"
        # silently shrinks the account and skews every percentage.
        if TOTAL_WORDS.match(sym_u) or TOTAL_WORDS.match(desc):
            notes.append(f'{label}: totals row, ignored')
            continue
        if sym_u in CASH_TICKERS or CASH_WORDS.search(desc) or CASH_WORDS.search(sym_u):
            if value: cash_total += value
            notes.append(f'{label}: treated as cash'
                         + (f' (${value:,.2f})' if value else ''))
            continue

        if value is None and qty is None and price is None:
            # prose (disclaimers, section headings) rather than a position
            if len(label) > 14 or ' ' in label:
                continue
            skipped.append(f'{label}: no value, quantity or price — nothing to size it with')
            continue
        if value is None:
            skipped.append(f'{label}: no market value, and no quantity×price to derive one')
            continue
        if value < 0:
            skipped.append(f'{label}: negative value ({value:,.2f}) — short position, not supported')
            continue

        sym, note = yahoo_ticker(raw_sym)
        if note: notes.append(note)
        broker_pct = to_num(get(row, 'pct'))       # for the cross-check below

        # per-share cost: explicit column wins, else total ÷ shares
        cost_ps = to_num(get(row, 'cost_ps'))
        if cost_ps is None:
            tot = to_num(get(row, 'cost_tot'))
            if tot is not None and qty:
                cost_ps = abs(tot) / abs(qty)
        if cost_ps is not None and cost_ps <= 0:
            cost_ps = None
        if cost_ps is None:
            notes.append(f'{sym}: no cost basis found — will start flat at today\'s price')

        holdings.append({'sym': sym, 'value': value, 'cost': cost_ps,
                         'broker_pct': broker_pct})

    if not holdings:
        raise SystemExit('No priceable holdings found. Check --sheet or the column names.')

    # merge duplicate tickers (multi-lot exports): value adds, cost is value-weighted
    merged = {}
    for h in holdings:
        m = merged.get(h['sym'])
        if not m:
            merged[h['sym']] = dict(h); continue
        if m['cost'] is not None and h['cost'] is not None:
            m['cost'] = (m['cost'] * m['value'] + h['cost'] * h['value']) / (m['value'] + h['value'])
        else:
            m['cost'] = None
        m['value'] += h['value']
        notes.append(f'{h["sym"]}: multiple lots merged, cost basis value-weighted')
    holdings = list(merged.values())

    total = sum(h['value'] for h in holdings) + cash_total
    for h in holdings:
        h['pct'] = h['value'] / total * 100

    # If the broker states its own "% of account", check ours against it. A gap
    # means a row was missed — usually a cash or totals line — which is the one
    # failure mode that produces confidently wrong output.
    checked = [h for h in holdings if h.get('broker_pct')]
    for h in checked:
        if abs(h['pct'] - h['broker_pct']) > max(0.5, h['broker_pct'] * 0.05):
            skipped.append(
                f'{h["sym"]}: computed {h["pct"]:.2f}% but the file says '
                f'{h["broker_pct"]:.2f}% — a row was probably missed. DO NOT PASTE '
                f'until this is resolved.')

    holdings.sort(key=lambda h: -h['pct'])
    return holdings, cash_total, total, notes, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--sheet', help='worksheet name (default: first sheet)')
    ap.add_argument('--decimals', type=int, default=2, help='percent precision (default 2)')
    a = ap.parse_args()

    holdings, cash, total, notes, skipped = convert(a.file, a.sheet)
    d = a.decimals

    print('\n' + '=' * 62)
    print('PASTE THIS INTO  "+ From what I own"')
    print('=' * 62)
    for h in holdings:
        line = f'{h["sym"]}, {h["pct"]:.{d}f}'
        if h['cost'] is not None:
            line += f', {h["cost"]:.4f}'.rstrip('0').rstrip('.')
        print(line)

    print('\n' + '-' * 62)
    print(f'Account total   ${total:,.2f}')
    print(f'Holdings        {len(holdings)}  ({sum(h["pct"] for h in holdings):.{d}f}% of the account)')
    print(f'Cash            ${cash:,.2f}  ({cash / total * 100 if total else 0:.{d}f}%)'
          '  ← left out on purpose; the tracker derives it')
    if notes:
        print('\nNotes:')
        for n in dict.fromkeys(notes): print(f'  · {n}')
    if skipped:
        print('\nSKIPPED — check these:')
        for s in skipped: print(f'  ! {s}')
    print()


if __name__ == '__main__':
    main()
