"""Bounded authenticated poll/service measurement. Cannot submit transactions.

Requires an empty account. Uses a new diagnostic state directory, not strategy
or execution-check state. SDK bootstrap traffic is outside the HTTP sample.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import time
from .authenticated import LighterTransport
from .config import LiveConfig
from .execution import positions
from .live_check import save
from .run import build_authenticated_strategy


def measure(transport,output,*,cycles=3,interval=15):
    if not 1<=cycles<=10 or not 15<=interval<=60: raise ValueError('bounded audit only')
    output=Path(output)
    if output.exists(): raise ValueError('use a fresh diagnostic output directory')
    if positions(transport.account()) or transport.active_orders():
        raise RuntimeError('account must be empty for this isolated read-only audit')
    config=LiveConfig(mode='live',base_url=transport.host,account_index=transport.account_index,
        api_key_index=transport.api_key_index,state_dir=output/'state')
    trader,_=build_authenticated_strategy(config,transport,start_ms=int(time.time()*1000),writes_enabled=False)
    results=[]
    try:
        for i in range(cycles):
            before=len(transport.request_samples); start=time.monotonic()
            trader.broker.poll(); out=trader.service()
            samples=list(transport.request_samples)[before:]
            results.append({'cycle':i+1,'seconds':time.monotonic()-start,'requests':len(samples),
                'endpoints':dict(Counter(r['endpoint'] for r in samples)),
                'halted':out.halted,'execution_changes_verified':trader.broker.last_report.get('execution_changes_verified'),
                'samples':samples})
            if i+1<cycles: time.sleep(interval)
        final=transport.account(); active=transport.active_orders()
        report={'checked_at_ms':int(time.time()*1000),'cycles':results,'measured_interval_seconds':interval,
            'default_strategy_poll_seconds':config.poll_seconds,'market_count':len(trader.markets),
            'equity':final['total_asset_value'],'open_positions':len(positions(final)),'active_orders':len(active),
            'orders_submitted':0,'strategy_live_enabled':False,
            'scope':'empty-account REST reads; excludes SDK bootstrap, writes and live fill publication latency'}
        save(output/'report.json',report)
        return report
    finally:trader.broker.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path); p.add_argument('--output',required=True,type=Path)
    args=p.parse_args(); c=json.loads(args.config.read_text(encoding='utf-8-sig'))
    t=LighterTransport(**{k:c[k] for k in ('network','account_index','api_key_index','key_file','expected_address')})
    try:print(json.dumps(measure(t,args.output),indent=2))
    finally:t.close()


if __name__=='__main__':main()
