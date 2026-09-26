"""Live configuration. Strategy parameters come from the backtest's own Strategy
dataclass so there is exactly one definition of the strategy."""
from __future__ import annotations

import json
import os
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlsplit

from frontier_engine import Strategy

# Strategy symbol -> Lighter market symbol. The backtest universe is Binance
# USD-M perpetuals; Lighter names the same markets without the quote suffix.
SYMBOL_TO_LIGHTER = {
    'BTCUSDT': 'BTC', 'ETHUSDT': 'ETH', 'SOLUSDT': 'SOL', 'BNBUSDT': 'BNB',
    'XRPUSDT': 'XRP', 'ADAUSDT': 'ADA', 'DOGEUSDT': 'DOGE', 'LINKUSDT': 'LINK',
    'LTCUSDT': 'LTC', 'BCHUSDT': 'BCH', 'AVAXUSDT': 'AVAX',
}

# Excluded from live trading on COST grounds, not on backtest profit.
# BCH round-trip slippage measured 44.6bp at 100k equity, which is 0.383R — a
# round trip hands 38% of the 2% risk budget to the book before the trade has a
# view. src/live/universe_study.py shows that dropping further symbols does keep
# improving the sample, but roughly half of that gain is post-hoc selection and
# should not be expected to repeat; BCH alone is disqualified by cost arithmetic
# that does not reference its P&L at all.
#
# This is a LIVE setting. The audited backtest keeps all eleven symbols so its
# published results stay reproducible — compare like with like by passing the
# same universe to both (src/live/replay.py does).
EXCLUDED = ('BCHUSDT',)
DEFAULT_UNIVERSE = tuple(s for s in SYMBOL_TO_LIGHTER if s not in EXCLUDED)

MAINNET = 'https://mainnet.zklighter.elliot.ai'

# shadow  decide and journal only, never touch the exchange (default)
# paper   decide against live data, simulate fills locally
# live    place real orders
MODES = ('shadow', 'paper', 'live')


@dataclass
class LiveConfig:
    mode: str = 'shadow'
    base_url: str = MAINNET

    # Symbols actually traded. Defaults to everything except EXCLUDED.
    universe: tuple = DEFAULT_UNIVERSE

    # Lighter account. account_index identifies the sub-account; the private key
    # never appears in this file — it is read from LIGHTER_API_PRIVATE_KEY.
    account_index: int | None = None
    api_key_index: int = 0

    # Strategy. Defaults are the audited A configuration: 2% per trade, 4% total,
    # 5x gross, 6 positions, bull-regime shorts halved.
    strategy: Strategy = field(default_factory=lambda: Strategy(
        name='live_A_risk200', risk=.02, short_btc_bull_risk=.5,
        fee=0., slippage=.0002, total_risk=.04))

    # Execution
    max_slippage_pct: float = .005      # market-order slippage guard, fraction of price
    bar_grace_seconds: int = 60         # how late a Binance bar may be before we skip
    min_notional_usd: float = 10.0      # Lighter min_quote_amount on every market

    # Paper mode (stage 2)
    poll_seconds: int = 60              # how often resting stops are checked against mark
    breakeven_on_fill: bool = True      # move the stop on the TP fill, not at the next bar

    # Execution. Only the take-profit leg is worth making passive — see
    # src/live/execution_study.py. Entries and stops must cross the book.
    passive_take_profit: bool = True
    passive_fill_model: str = 'crossed_depth'  # simulation, never an observed maker fill
    paper_equity: float = 100_000.0
    # Operational defaults, not historical performance measurements.
    observation_seconds: int = 900
    observation_notional_multiple: float = .86
    binance_base_url: str = 'https://fapi.binance.com'
    request_timeout_seconds: int = 10
    feed_retries: int = 2
    max_clock_skew_seconds: float = 5.0

    # Guards
    margin_ratio_block_entries: float = 3.0   # equity / maintenance margin
    margin_ratio_alert: float = 2.0
    max_basis_divergence_pct: float = 1.0     # Binance close vs Lighter mark
    max_consecutive_rejects: int = 3

    # Paths
    state_dir: Path = Path('live_state')

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}')
        self.universe = tuple(self.universe)
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError('universe must be non-empty and have no duplicate symbols')
        unknown = [s for s in self.universe if s not in SYMBOL_TO_LIGHTER]
        if unknown:
            raise ValueError(f'universe has symbols with no Lighter market: {unknown}')
        if 'BTCUSDT' not in self.universe:
            # Every new short consults BTC's SMA200 regime. Trading BTC is
            # optional; having its bars is not.
            raise ValueError('BTCUSDT must stay in the universe: the regime filter reads it')
        self.state_dir = Path(self.state_dir)
        for name in ('base_url', 'binance_base_url'):
            url = urlsplit(getattr(self, name))
            if (url.scheme != 'https' or not url.hostname or url.username or
                    url.password or url.query or url.fragment or url.path not in ('', '/')):
                raise ValueError(f'{name} must be an HTTPS origin without credentials')
            setattr(self, name, getattr(self, name).rstrip('/'))
        for name in ('poll_seconds', 'bar_grace_seconds', 'paper_equity',
                     'observation_seconds', 'observation_notional_multiple',
                     'request_timeout_seconds', 'feed_retries', 'max_clock_skew_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if self.passive_fill_model not in ('crossed_depth', 'observe_only'):
            raise ValueError('passive_fill_model must be crossed_depth or observe_only')
        if self.mode == 'live' and self.account_index is None:
            raise ValueError('live mode requires account_index')

    @property
    def lighter_universe(self) -> dict:
        """SYMBOL_TO_LIGHTER restricted to what this config trades."""
        return {s: SYMBOL_TO_LIGHTER[s] for s in self.universe}

    @property
    def private_key(self) -> str:
        key = os.environ.get('LIGHTER_API_PRIVATE_KEY', '')
        if not key and self.mode == 'live':
            raise RuntimeError(
                'LIGHTER_API_PRIVATE_KEY is not set. Live mode will not start without it.')
        return key

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides) -> 'LiveConfig':
        data = {}
        if path:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            # Human annotations are allowed; all other unknown keys still fail.
            data = {k: v for k, v in data.items() if not k.startswith('_')}
            if 'strategy' in data:
                data['strategy'] = Strategy(**data['strategy'])
        data.update(overrides)
        return cls(**data)

    def describe(self) -> dict:
        d = asdict(self)
        d['strategy'] = asdict(self.strategy)
        d['state_dir'] = str(self.state_dir)
        return d
