"""Extend the raw dataset from Binance USD-M, in the exact shape data_io expects.

The committed data arrived through a connector rather than a script, so there was
nothing to re-run when the sample needed extending. This writes the same files:

    data/raw/{SYMBOL}_{start}_4h.json        /fapi/v1/klines
    data/raw/{SYMBOL}_{start}_mark4h.json    /fapi/v1/markPriceKlines
    data/raw/{SYMBOL}_{start}_funding.json   /fapi/v1/fundingRate
    data/micro/{SYMBOL}_{start}.json         /fapi/v1/klines interval=5m

Everything is validated before it is written, because data_io refuses a gap and a
silently short download would show up much later as a changed backtest rather
than as an error. A file is written only once its rows are complete and aligned.

    python src/fetch_data.py --survey                  what is on disk, what is missing
    python src/fetch_data.py --extend                  4h + mark + funding to the last closed bar
    python src/fetch_data.py --micro needs.json        the 5m paths the engine asked for
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_io import H4, SYMBOLS                                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data/raw'
MICRO = ROOT / 'data/micro'

# fapi.binance.com answers 451 from restricted regions; www.binance.com serves
# the same /fapi paths. A 451/403 is that host refusing by policy, so it is not
# retried — retrying would spend the whole backoff before the reachable host.
HOSTS = ('https://fapi.binance.com', 'https://www.binance.com')

M5 = 300_000
MICRO_RUN_BARS = 30          # 30 x 48 = 1440 rows, inside the 1500 limit


def get(path: str, params: dict, retries: int = 5, timeout: int = 40):
    url_q = urllib.parse.urlencode(params)
    last = None
    for host in HOSTS:
        for attempt in range(retries):
            try:
                req = urllib.request.Request(f'{host}/fapi/v1/{path}?{url_q}',
                                             headers={'User-Agent': 'curl/8'})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                last = f'HTTP {exc.code}'
                if exc.code in (451, 403):
                    break                     # restricted here; next host
                if exc.code == 429:
                    time.sleep(10 * (attempt + 1))
                    continue
                time.sleep(2 ** attempt)
            except Exception as exc:
                last = repr(exc)
                time.sleep(2 ** attempt)
    raise RuntimeError(f'{path} {params}: {last}')


def write(path: Path, source: str, request: dict, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'source': source, 'request': request, 'rows': rows}))
    tmp.replace(path)


def on_disk(symbol: str, suffix: str) -> list[int]:
    """Bar open times already stored for this symbol and kind."""
    out = []
    for p in sorted(RAW.glob(f'{symbol}_*_{suffix}.json')):
        out.extend(int(r[0]) for r in json.loads(p.read_text())['rows'])
    return sorted(out)


def last_closed_bar(now_ms: int | None = None) -> int:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now // H4) * H4 - H4


def fetch_bars(symbol: str, kind: str, start: int, end: int) -> list:
    """Closed 4h bars covering [start, end). Verified contiguous before return."""
    path = 'klines' if kind == '4h' else 'markPriceKlines'
    want = (end - start) // H4
    rows: list = []
    cursor = start
    while cursor < end:
        batch = get(path, {'symbol': symbol, 'interval': '4h',
                           'startTime': cursor, 'endTime': end - 1, 'limit': 1500})
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + H4
    times = [int(r[0]) for r in rows]
    if len(times) != want:
        raise RuntimeError(f'{symbol} {kind}: got {len(times)} bars, want {want}')
    if times[0] != start or times[-1] != end - H4:
        raise RuntimeError(f'{symbol} {kind}: range {times[0]}..{times[-1]} != '
                           f'{start}..{end - H4}')
    if any(b - a != H4 for a, b in zip(times, times[1:])):
        raise RuntimeError(f'{symbol} {kind}: gap in bars')
    return rows


def fetch_funding(symbol: str, start: int, end: int) -> list:
    rows: list = []
    cursor = start
    while True:
        batch = get('fundingRate', {'symbol': symbol, 'startTime': cursor,
                                    'endTime': end - 1, 'limit': 1000})
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1]['fundingTime']) + 1
    seen = set()
    for r in rows:
        minute = (int(r['fundingTime']) // 60000) * 60000
        if minute in seen:
            raise RuntimeError(f'{symbol} funding: duplicate settlement minute {minute}')
        seen.add(minute)
    return rows


def extend(end: int | None, dry: bool) -> int:
    target = end if end is not None else last_closed_bar() + H4
    if target % H4:
        raise SystemExit(f'end {target} is not a 4h boundary')
    print(f'목표 END {target}  ({time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(target / 1000))})')
    print(f'마지막 완성봉 {target - H4}\n')

    for symbol in SYMBOLS:
        have = on_disk(symbol, '4h')
        start = have[-1] + H4
        if start >= target:
            print(f'{symbol:9s} 이미 최신 (마지막 {have[-1]})')
            continue
        bars = (target - start) // H4
        print(f'{symbol:9s} {start} 부터 {bars}봉', end='', flush=True)
        if dry:
            print('  [dry]')
            continue
        k = fetch_bars(symbol, '4h', start, target)
        m = fetch_bars(symbol, 'mark4h', start, target)
        f = fetch_funding(symbol, start, target)
        write(RAW / f'{symbol}_{start}_4h.json', 'binance_fapi_klines',
              {'kind': 'klines', 'symbol': symbol, 'startTime': start,
               'endTime': target - 1}, k)
        write(RAW / f'{symbol}_{start}_mark4h.json', 'binance_fapi_mark_klines',
              {'symbol': symbol, 'startTime': start, 'endTime': target - 1}, m)
        write(RAW / f'{symbol}_{start}_funding.json', 'binance_fapi_funding',
              {'symbol': symbol, 'startTime': start, 'endTime': target - 1,
               'limit': 1000}, f)
        print(f'  4h {len(k)}  mark {len(m)}  funding {len(f)}')
    return 0


def fetch_micro(needs: list[tuple[str, int]]) -> int:
    """Fetch the 5m paths the engine asked for, as contiguous runs.

    data_io requires exactly 48 rows per 4h bar with no gap, so each run is
    verified bar by bar before anything is written.
    """
    have = {(p.name.split('_')[0], t)
            for p in MICRO.glob('*.json')
            for t in [int(r[0]) // H4 * H4 for r in json.loads(p.read_text())['rows']]}
    todo = sorted({(s, t) for s, t in needs if (s, t) not in have})
    print(f'요청 {len(set(needs))}개 중 {len(todo)}개가 없음\n')
    by_symbol: dict[str, list[int]] = {}
    for s, t in todo:
        by_symbol.setdefault(s, []).append(t)

    written = 0
    for symbol, times in by_symbol.items():
        times.sort()
        runs: list[list[int]] = []
        for t in times:
            if runs and t == runs[-1][-1] + H4 and len(runs[-1]) < MICRO_RUN_BARS:
                runs[-1].append(t)
            else:
                runs.append([t])
        print(f'{symbol:9s} {len(times)}봉 -> {len(runs)}개 구간', flush=True)
        for run in runs:
            start, end = run[0], run[-1] + H4
            rows: list = []
            cursor = start
            while cursor < end:
                batch = get('klines', {'symbol': symbol, 'interval': '5m',
                                       'startTime': cursor, 'endTime': end - 1,
                                       'limit': 1500})
                if not batch:
                    break
                rows.extend(batch)
                cursor = int(batch[-1][0]) + M5
            want = (end - start) // M5
            if len(rows) != want:
                raise RuntimeError(f'{symbol} 5m {start}: got {len(rows)}, want {want}')
            for off in range(0, len(rows), 48):
                g = rows[off:off + 48]
                if len(g) != 48 or any(int(g[k][0]) != int(g[0][0]) + k * M5
                                       for k in range(48)):
                    raise RuntimeError(f'{symbol} 5m {start}: 48봉 정렬 실패 @{off}')
            write(MICRO / f'{symbol}_{start}.json', 'binance_fapi_klines',
                  {'symbol': symbol, 'interval': '5m', 'startTime': start,
                   'endTime': end - 1, 'limit': 1500}, rows)
            written += 1
    print(f'\n{written}개 파일 기록')
    return 0


def run_jobs(path: Path) -> int:
    """Execute the 5m job list the engine writes (data/jobs_risk_sweep.json).

    Those jobs are already shaped the way data_io reads them back: each covers a
    whole number of 4h bars starting on a boundary, within the 1500-row limit.
    Taking them verbatim keeps one definition of what the run needs instead of
    re-deriving it here and drifting.
    """
    jobs = json.loads(path.read_text())
    print(f'{len(jobs)}개 작업\n')
    for i, j in enumerate(jobs, 1):
        symbol, start, end = j['symbol'], int(j['startTime']), int(j['endTime']) + 1
        bars = (end - start) // H4
        if (end - start) % H4:
            raise RuntimeError(f'{symbol} {start}: 4h 경계가 아니다')
        out = MICRO / f'{symbol}_{start}.json'
        print(f'{i:>3d}. {symbol:9s} {start} {bars:>3d}봉', end='', flush=True)
        rows: list = []
        cursor = start
        while cursor < end:
            batch = get('klines', {'symbol': symbol, 'interval': '5m',
                                   'startTime': cursor, 'endTime': end - 1,
                                   'limit': 1500})
            if not batch:
                break
            rows.extend(batch)
            cursor = int(batch[-1][0]) + M5
        want = (end - start) // M5
        if len(rows) != want:
            raise RuntimeError(f'{symbol} {start}: got {len(rows)}, want {want}')
        for off in range(0, len(rows), 48):
            g = rows[off:off + 48]
            if len(g) != 48 or any(int(g[k][0]) != int(g[0][0]) + k * M5
                                   for k in range(48)):
                raise RuntimeError(f'{symbol} {start}: 48봉 정렬 실패 @{off}')
        write(out, 'binance_fapi_klines', j, rows)
        print(f'  {len(rows)}행 -> {out.name}')
    return 0


def survey() -> int:
    closed = last_closed_bar()
    print(f'마지막 완성 4h봉 {closed} '
          f'({time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(closed / 1000))})\n')
    print(f"{'symbol':9s}{'4h 마지막':>16s}{'mark 마지막':>16s}{'부족봉':>8s}")
    for symbol in SYMBOLS:
        a, b = on_disk(symbol, '4h'), on_disk(symbol, 'mark4h')
        gap = (closed - a[-1]) // H4 if a else 0
        print(f'{symbol:9s}{a[-1] if a else 0:>16d}{b[-1] if b else 0:>16d}{gap:>8d}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--survey', action='store_true')
    ap.add_argument('--extend', action='store_true')
    ap.add_argument('--end', type=int, help='exclusive END in ms; default last closed bar + 4h')
    ap.add_argument('--micro', help='JSON list of [symbol, bar_ms] pairs')
    ap.add_argument('--jobs', help="the engine's own 5m job list, e.g. "
                                   'data/jobs_risk_sweep.json')
    ap.add_argument('--dry-run', action='store_true', dest='dry')
    a = ap.parse_args()
    if a.survey:
        raise SystemExit(survey())
    if a.extend:
        raise SystemExit(extend(a.end, a.dry))
    if a.jobs:
        raise SystemExit(run_jobs(Path(a.jobs)))
    if a.micro:
        pairs = [(s, int(t)) for s, t in json.loads(Path(a.micro).read_text())]
        raise SystemExit(fetch_micro(pairs))
    ap.print_help()
