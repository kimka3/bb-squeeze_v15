"""Read immutable connector downloads; fail visibly on missing bars."""
from pathlib import Path
import json,hashlib,sys
import numpy as np
import pandas as pd

SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT',
         'DOGEUSDT','LINKUSDT','LTCUSDT','BCHUSDT','AVAXUSDT']
LONGS=SYMBOLS[:3]
H4=14400000
START=int(pd.Timestamp('2021-01-01',tz='UTC').timestamp()*1000)
END=int(pd.Timestamp('2026-09-01',tz='UTC').timestamp()*1000)

def records(path,symbol,suffix):
    rows=[]
    for p in sorted(Path(path).glob(f'{symbol}_*_{suffix}.json')):
        rows.extend(json.loads(p.read_text())['rows'])
    if not rows: raise FileNotFoundError(f'Missing {symbol} {suffix}')
    return rows

def load_data(root):
    import bb_squeeze_combined_v6e_dynamic as frozen
    root=Path(root);out={};funds={};marks={};audit=[]
    for symbol in SYMBOLS:
        rows=records(root/'raw',symbol,'4h')
        d=pd.DataFrame([r[:6] for r in rows],columns=['time','open','high','low','close','volume'])
        if d.time.duplicated().any(): raise ValueError(f'{symbol}: duplicate candle')
        d=d.sort_values('time'); d['timestamp']=pd.to_datetime(d.time,unit='ms',utc=True)
        for k in ['open','high','low','close','volume']: d[k]=pd.to_numeric(d[k])
        if d.time.diff().dropna().ne(H4).any(): raise ValueError(f'{symbol}: missing 4h candle')
        if d.iloc[-1].time != END-H4: raise ValueError(f'{symbol}: incomplete period')
        p=frozen.prepare_symbol_data(d, symbol in LONGS,True)
        p['time']=d.time.to_numpy(dtype='int64')
        p=p[p.time>=START].set_index('time',drop=False)
        out[symbol]=p
        fr=pd.DataFrame(records(root/'raw',symbol,'funding')).sort_values('fundingTime')
        if fr.fundingTime.duplicated().any(): raise ValueError('duplicate funding')
        fr['fundingRate']=pd.to_numeric(fr.fundingRate)
        fr['markPrice']=pd.to_numeric(fr.markPrice,errors='coerce')
        # Historical exchange stamps may be several milliseconds after scheduled
        # settlement. Record funding before entries at that minute boundary.
        fr['settlement_ms']=(fr.fundingTime.astype('int64')//60000)*60000
        if fr.settlement_ms.duplicated().any(): raise ValueError('duplicate settlement minute')
        funds[symbol]=fr
        mr=records(root/'raw',symbol,'mark4h')
        md=pd.DataFrame([r[:5] for r in mr],columns=['time','open','high','low','close']).astype(float)
        md['time']=md.time.astype('int64');md=md.sort_values('time').set_index('time')
        if md.index.duplicated().any():raise ValueError('duplicate mark bar')
        marks[symbol]=md
        audit.append({'symbol':symbol,'signal_bars':len(p),'warmup_bars':len(d)-len(p),
           'funding_records':len(fr),'funding_missing_mark':int(fr.markPrice.isna().sum()),
           'funding_largest_gap_hours':float(fr.settlement_ms.diff().max()/3600000)})
    timeline=out[SYMBOLS[0]].index
    for d in out.values():
        if not d.index.equals(timeline):raise ValueError('unaligned symbols')
    return out,funds,marks,audit

def load_micro(root):
    result={}
    for p in (Path(root)/'micro').glob('*.json'):
        o=json.loads(p.read_text()); rows=o['rows'];j=o['request']
        expected=(j['endTime']+1-j['startTime'])//300000
        if len(rows)!=expected or rows[0][0]!=j['startTime'] or rows[-1][0]!=j['endTime']+1-300000:
            raise ValueError(f'Incomplete micro candle: {p.name}')
        for offset in range(0,len(rows),48):
            group=rows[offset:offset+48]
            if len(group)!=48 or any(group[k][0]!=group[0][0]+k*300000 for k in range(48)):
                raise ValueError(f'5m gap in {p.name}')
            key=(j['symbol'],int(group[0][0]))
            arr=np.array([[int(r[0]),*map(float,r[1:5])] for r in group])
            if key in result and not np.array_equal(result[key],arr):raise ValueError('Conflicting micro data')
            result[key]=arr
    return result
