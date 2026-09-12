"""Live configuration. Strategy parameters come from the backtest's own Strategy
dataclass so there is exactly one definition of the strategy."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from frontier_engine import Strategy

# Strategy symbol -> Lighter market symbol. The backtest universe is Binance
# USD-M perpetuals; Lighter names the same markets without the quote suffix.
SYMBOL_TO_LIGHTER = {
    'BTCUSDT': 'BTC', 'ETHUSDT': 'ETH', 'SOLUSDT': 'SOL', 'BNBUSDT': 'BNB',
    'XRPUSDT': 'XRP', 'ADAUSDT': 'ADA', 'DOGEUSDT': 'DOGE', 'LINKUSDT': 'LINK',
    'LTCUSDT': 'LTC', 'BCHUSDT': 'BCH', 'AVAXUSDT': 'AVAX',
}

MAINNET = 'https://mainnet.zklighter.elliot.ai'

# shadow  decide and journal only, never touch the exchange (default)
# paper   decide against live data, simulate fills locally
# live    place real orders
MODES = ('shadow', 'paper', 'live')


@dataclass
class LiveConfig:
    mode: str = 'shadow'
    base_url: str = MAINNET

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
        self.state_dir = Path(self.state_dir)
        if self.mode == 'live' and self.account_index is None:
            raise ValueError('live mode requires account_index')

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
            data = json.loads(Path(path).read_text())
            if 'strategy' in data:
                data['strategy'] = Strategy(**data['strategy'])
        data.update(overrides)
        return cls(**data)

    def describe(self) -> dict:
        d = asdict(self)
        d['strategy'] = asdict(self.strategy)
        d['state_dir'] = str(self.state_dir)
        return d
