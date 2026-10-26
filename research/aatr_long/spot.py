"""data/spot4h 의 Binance 현물 4H CSV 를 backtest.run 이 받는 형태로 읽는다 (펀딩 없음)."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def load_spot(symbol):
    d = pd.read_csv(ROOT / 'data' / 'spot4h' / f'{symbol}_4h.csv')
    d['fund'] = 0.0
    return d.reset_index(drop=True)
