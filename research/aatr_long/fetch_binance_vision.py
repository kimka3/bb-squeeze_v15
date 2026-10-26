"""Binance 공개 아카이브(data.binance.vision)에서 현물 4H 캔들을 내려받아 CSV 로 저장한다.
API 키 불필요. TradingView 'BINANCE:BTCUSDT' 와 같은 현물 데이터. 2017-08 부터 있다.
사용: python3 research/aatr_long/fetch_binance_vision.py BTCUSDT ETHUSDT
결과: data/spot4h/<SYMBOL>_4h.csv (time, open, high, low, close, volume)
※ 이 환경의 네트워크 정책에 data.binance.vision 이 허용돼 있어야 한다.
"""
import io, sys, zipfile, urllib.request, urllib.error
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE = 'https://data.binance.vision/data/spot/monthly/klines/{s}/4h/{s}-4h-{y}-{m:02d}.zip'
DAILY = 'https://data.binance.vision/data/spot/daily/klines/{s}/4h/{s}-4h-{d}.zip'


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
            return pd.read_csv(z.open(z.namelist()[0]), header=None).iloc[:, :6]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch(sym):
    frames = []
    now = pd.Timestamp.now(tz='UTC')
    for p in pd.period_range('2017-08', now.strftime('%Y-%m'), freq='M'):
        f = get(BASE.format(s=sym, y=p.year, m=p.month))
        if f is None:  # 이번 달처럼 월 파일이 아직 없으면 일별 파일로 채운다
            for d in pd.date_range(p.start_time, min(p.end_time, now.tz_localize(None) - pd.Timedelta(days=1)), freq='D'):
                g = get(DAILY.format(s=sym, d=d.strftime('%Y-%m-%d')))
                if g is not None:
                    frames.append(g)
        else:
            frames.append(f)
    d = pd.concat(frames, ignore_index=True)
    d.columns = ['time', 'open', 'high', 'low', 'close', 'volume']
    d['time'] = d.time.astype('int64')
    d.loc[d.time > 10**14, 'time'] //= 1000  # 2025 년부터 아카이브 시각이 마이크로초 단위
    d = d.drop_duplicates('time').sort_values('time')
    out = ROOT / 'data' / 'spot4h'; out.mkdir(parents=True, exist_ok=True)
    d.to_csv(out / f'{sym}_4h.csv', index=False)
    print(sym, len(d), pd.to_datetime(d.time.iloc[0], unit='ms'), pd.to_datetime(d.time.iloc[-1], unit='ms'))


if __name__ == '__main__':
    for s in sys.argv[1:] or ['BTCUSDT', 'ETHUSDT']:
        fetch(s)
