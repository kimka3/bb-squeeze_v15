"""Declared 2% per-trade risk, unchanged A regime and portfolio guards."""
from pathlib import Path
import sys,unittest
from dataclasses import replace
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from frontier_engine import Market,Strategy,run
from data_io import SYMBOLS,START,H4

def fixture(signals,bull=False):
    data={}
    for s in SYMBOLS:
        rows=[{'time':START+k*H4,'open':100.,'high':102.,'low':99.,'close':100.,
          's_atr':4.,'l_atr':4.,'short_signal':s in signals and k==0,'long_breakout':False,
          'l_bb_upper':110.,'l_bb_mid':90.,'l_ma200':95.,'l_ma200_slope':1.,
          'x_bb_lower':99.5,'x_ma200':90. if bull else 110.,'x_rvol':1.} for k in range(3)]
        data[s]=pd.DataFrame(rows).set_index('time',drop=False)
    return Market(data,{s:pd.DataFrame() for s in SYMBOLS},{s:pd.DataFrame() for s in SYMBOLS})

def replay(risk=None,signals=('ETHUSDT',),bull=False):
    kw={} if risk is None else {'risk':risk}
    return run(fixture(signals,bull),Strategy(short_btc_bull_risk=.5,fee=0.,slippage=0.,funding=False,**kw),START,START+3*H4)

class RiskTests(unittest.TestCase):
    def test_declared_per_trade_risk_is_two_percent_and_bull_short_is_one(self):
        self.assertAlmostEqual(Strategy().risk,.02)
        self.assertAlmostEqual(replay()['trades'].iloc[0].initial_risk,2000.)
        self.assertAlmostEqual(replay(bull=True)['trades'].iloc[0].initial_risk,1000.)

    def test_uncapped_quantity_scales_with_risk_and_bull_short_is_halved(self):
        # .0125 is the superseded per-trade risk, kept here as a sizing reference.
        base=replay(.0125)['trades'].iloc[0]
        high=replay(.02)['trades'].iloc[0]
        reduced=replay(.02,bull=True)['trades'].iloc[0]
        self.assertAlmostEqual(high.initial_qty/base.initial_qty,1.6)
        self.assertAlmostEqual(high.initial_risk,2000.)
        self.assertAlmostEqual(reduced.initial_risk,1000.)

    def test_two_percent_does_not_raise_total_four_percent_cap(self):
        r=replay(.02,signals=tuple(SYMBOLS))
        self.assertEqual(len(r['trades']),2)
        self.assertAlmostEqual(r['trades'].initial_risk.sum(),4000.)
        self.assertGreater(r['stats']['skipped_cap'],0)
        self.assertLessEqual((r['equity'].initial_risk/r['equity'].equity).max(),.04+1e-12)

    def test_six_percent_accepts_three_full_two_percent_positions(self):
        r=run(fixture(tuple(SYMBOLS)),Strategy(risk=.02,total_risk=.06,short_btc_bull_risk=.5,fee=0.,slippage=0.,funding=False),START,START+3*H4)
        self.assertEqual(len(r['trades']),3)
        self.assertAlmostEqual(r['trades'].initial_risk.sum(),6000.)
        self.assertGreater(r['stats']['skipped_cap'],0)
        self.assertLessEqual((r['equity'].initial_risk/r['equity'].equity).max(),.06+1e-12)

    def test_unrequested_risk_and_portfolio_limits_remain_rejected(self):
        for kw in [{'risk':.0201},{'risk':.02,'total_risk':.061},{'risk':.02,'gross_cap':5.1},{'risk':.02,'max_positions':7}]:
            with self.assertRaises(ValueError):Strategy(**kw)

if __name__=='__main__':unittest.main()
