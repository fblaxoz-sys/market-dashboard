#!/usr/bin/env python3
"""Unit tests for cache pre-warming + release-day freshness (proxy_server.py).

Everything is stubbed — no network, no model compute — so this runs in CI.
Guards the mechanism that keeps nowcasts warm and never serves a pre-release
number after a CPI/GDP print lands.
"""
import io, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import proxy_server as ps  # noqa: E402


def _stub(latest):
    """Stub both models + FRED's latest-observation endpoint."""
    calls = []
    ps.run_inflation_nowcast = lambda key, months=24, target_id='X': (
        calls.append(target_id), {'last_actual_month': '2026-06'})[1]
    ps.run_gdp_nowcast = lambda key, quarters=12: (
        calls.append('GDP'), {'last_actual_month': '2026-01'})[1]

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request
    def fake_urlopen(url, *a, **k):
        u = url if isinstance(url, str) else getattr(url, 'full_url', str(url))
        sid = u.split('series_id=')[1].split('&')[0]
        return FakeResp(json.dumps(
            {'observations': [{'date': latest[sid], 'value': '1'}]}).encode())
    urllib.request.urlopen = fake_urlopen
    return calls


def run():
    latest = {'CPIAUCSL': '2026-06-01', 'CPILFESL': '2026-06-01', 'GDPC1': '2026-01-01'}
    calls = _stub(latest)
    os.environ['FRED_KEY'] = 'test-key'
    ps._ML_CACHE.clear(); ps._FRESH_MEMO.clear()

    # 1) cold cache -> warms both inflation targets + GDP
    ps._maybe_warm_models(); time.sleep(0.4)
    assert sorted(calls) == ['CPIAUCSL', 'CPILFESL', 'GDP'], calls
    assert all(k in ps._ML_CACHE for k in ps._WARM_JOBS), list(ps._ML_CACHE)
    print("  ✓ cold warm computes inflation x2 + GDP into the route cache keys")

    # 2) fresh cache -> no recompute; busy-lock released
    calls.clear()
    ps._maybe_warm_models(); time.sleep(0.3)
    assert calls == [], calls
    assert ps._WARM_BUSY.acquire(blocking=False); ps._WARM_BUSY.release()
    print("  ✓ fresh cache is a no-op; busy-lock released")

    # 3) a new print landing invalidates + recomputes ONLY that model
    latest['GDPC1'] = '2026-04-01'; ps._FRESH_MEMO.clear()
    ps._maybe_warm_models(); time.sleep(0.4)
    assert calls == ['GDP'], calls
    print("  ✓ new GDP print recomputes only gdp:12")

    # 4) freshness probe is memoized (one HTTP call serves repeat checks)
    import urllib.request
    hits = []; real = urllib.request.urlopen
    urllib.request.urlopen = lambda u, *a, **k: (hits.append(1), real(u))[1]
    ps._FRESH_MEMO.clear()
    assert ps._release_stale('CPIAUCSL', 'k', '2026-05') is True    # 2026-06 > 2026-05
    assert ps._release_stale('CPIAUCSL', 'k', '2026-06') is False   # memo hit
    assert len(hits) == 1, hits
    print("  ✓ freshness probe memoized")

    # 5) no env key -> warming never fires
    calls.clear(); os.environ.pop('FRED_KEY')
    ps._ML_CACHE.clear()
    ps._maybe_warm_models(); time.sleep(0.2)
    assert calls == [], calls
    print("  ✓ no FRED_KEY env: warming no-ops")

    print("OK — 5 freshness/warming tests passed")


if __name__ == '__main__':
    run()
