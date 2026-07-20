#!/usr/bin/env python3
"""Walk-forward backfill for the Chronos foundation-model ensemble member.

Runs Amazon's Chronos-Bolt (open weights, CPU) over the same initial-release
CPI YoY series the inflation model trains on, producing true out-of-sample
next-month predictions keyed by target month. Output: models/chronos_vintages.json,
which proxy_server can score exactly like the Cleveland member (0 weight if it
can't beat the blend — the gate decides).

Too heavy for the 512MB server, so this runs locally / in GitHub Actions:
  uv run --python 3.12 --with chronos-forecasting --with pandas \
      python scripts/chronos_backfill.py <FRED_KEY>
"""
import json, os, sys, urllib.request

import pandas as pd
import torch
from chronos import BaseChronosPipeline

KEY = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('FRED_KEY', '')
L = 320
VINTAGE = "&output_type=4&realtime_start=1776-07-04&realtime_end=9999-12-31"

def fetch_yoy(sid):
    base = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
            f"&api_key={KEY}&sort_order=desc&limit={L}&file_type=json")
    def get(u):
        with urllib.request.urlopen(u, timeout=25) as r:
            return json.loads(r.read())
    d = get(base + VINTAGE)
    obs = [o for o in d.get('observations', []) if o['value'] != '.']
    if len(obs) < 0.8 * L:                     # vintage archive too short -> revised
        obs = [o for o in get(base).get('observations', []) if o['value'] != '.']
    s = pd.Series({pd.Timestamp(o['date']): float(o['value']) for o in obs}).sort_index()
    s = (s.reindex(pd.date_range(s.index.min(), s.index.max(), freq='MS'))
           .interpolate(method='linear', limit_direction='both'))
    return ((s / s.shift(12) - 1) * 100).dropna()

def main():
    pipe = BaseChronosPipeline.from_pretrained("amazon/chronos-bolt-small",
                                               device_map="cpu", torch_dtype=torch.float32)
    out = {}
    for sid in ('CPIAUCSL', 'CPILFESL'):
        y = fetch_yoy(sid)
        min_train = max(60, len(y) // 3)
        preds = {}
        for i in range(min_train, len(y) + 1):   # +1: last step predicts the unreleased month
            ctx = torch.tensor(y.values[:i], dtype=torch.float32)
            q, _ = pipe.predict_quantiles(ctx, prediction_length=1,
                                          quantile_levels=[0.5])
            target = (y.index[i - 1] + pd.DateOffset(months=1)).strftime('%Y-%m')
            preds[target] = round(float(q[0, 0, 0]), 3)
        out[sid] = preds
        print(f"{sid}: {len(preds)} walk-forward predictions "
              f"({min(preds)}..{max(preds)})")
    out['meta'] = {'model': 'amazon/chronos-bolt-small', 'built': pd.Timestamp.now().strftime('%Y-%m-%d')}
    os.makedirs('models', exist_ok=True)
    with open('models/chronos_vintages.json', 'w') as f:
        json.dump(out, f)
    print("wrote models/chronos_vintages.json")

if __name__ == '__main__':
    main()
