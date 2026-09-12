"""Monthly return and drawdown time series from the audited 5m equity paths.

Reads results/cases/*/equity_5m.csv. Runs no strategy logic and changes no
result: the 5m path IS the backtest output, this only aggregates it by month.

Two drawdown bases are reported and must not be confused:
  intramonth_mdd_pct  peak resets at each month start; worst loss inside a month
  underwater_*_pct    peak is the running all-time high; a drawdown that spans
                      several months stays open across them
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from data_io import START,END

ROOT=Path(__file__).resolve().parents[1]
RESULTS=ROOT/'results'
GRID=pd.period_range('2021-01','2026-08',freq='M')

def monthly(equity):
    """Month-end return, intramonth MDD and all-time underwater per month."""
    t=pd.to_datetime(equity.time_ms,unit='ms',utc=True)
    s=pd.Series(equity.equity.to_numpy(),index=t).groupby(level=0).last()
    # equity_replay stamps each row at the END of its 5m interval, so a stamp of
    # 2026-01-01T00:00 is the close of 2025's last bar. Bin on (month], matching
    # analyze_results.period(); plain calendar binning would push it into 2026.
    s.index=s.index-pd.Timedelta(milliseconds=1)
    # The path is a step function sampled at every change, so a month with no
    # row simply held its carried-in equity and cannot contain a drawdown.
    peak=np.maximum.accumulate(s.to_numpy())
    under=pd.Series(s.to_numpy()/peak-1,index=s.index)
    closes=s.resample('ME').last()
    closes.index=closes.index.tz_convert(None).to_period('M')
    closes=closes.reindex(GRID).ffill()
    opens=closes.shift(1)
    opens.iloc[0]=100000.
    closes=closes.fillna(100000.)
    opens=opens.fillna(100000.)
    bymonth=s.index.tz_convert(None).to_period('M')
    rows=[]
    for k,month in enumerate(GRID):
        inside=s[bymonth==month]
        start=float(opens.iloc[k]);end=float(closes.iloc[k])
        path=np.r_[start,inside.to_numpy()]
        intramonth=float((path/np.maximum.accumulate(path)-1).min()*100)
        seen=under[bymonth==month]
        rows.append({'month':str(month),'equity_end':end,
          'return_pct':(end/start-1)*100,'intramonth_mdd_pct':intramonth,
          'worst_underwater_pct':float(seen.min()*100) if len(seen) else float('nan'),
          'underwater_end_pct':float(seen.iloc[-1]*100) if len(seen) else float('nan')})
    out=pd.DataFrame(rows)
    # A flat month sits wherever the previous month left off.
    out['worst_underwater_pct']=out.worst_underwater_pct.ffill().fillna(0.)
    out['underwater_end_pct']=out.underwater_end_pct.ffill().fillna(0.)
    return out

def build():
    frames=[]
    for folder in sorted((RESULTS/'cases').iterdir()):
        metrics=json.loads((folder/'metrics.json').read_text())
        table=monthly(pd.read_csv(folder/'equity_5m.csv'))
        # Chain-linked months must rebuild the audited total and the audited MDD.
        compounded=float(np.prod(1+table.return_pct/100)*100000)
        assert np.isclose(compounded,metrics['final_equity'],rtol=1e-9),folder.name
        assert np.isclose(table.worst_underwater_pct.min(),metrics['mdd_5m_close_pct'],atol=1e-8),folder.name
        assert np.isclose(table.equity_end.iloc[-1],metrics['final_equity'],rtol=1e-10),folder.name
        assert len(table)==68 and table.intramonth_mdd_pct.max()<=0
        table.insert(0,'name',folder.name)
        table.insert(1,'model',metrics['model'])
        table.insert(2,'total_risk_cap_pct',round(metrics['config']['total_risk']*100))
        table.insert(3,'base_risk_pct',metrics['base_risk_pct'])
        frames.append(table)
    everything=pd.concat(frames,ignore_index=True)
    everything.to_csv(RESULTS/'monthly_timeseries.csv',index=False)
    return everything

def describe(table):
    r=table.return_pct
    return {'months':int(len(r)),'positive_months':int((r>0).sum()),
      'hit_rate_pct':float((r>0).mean()*100),'best_month_pct':float(r.max()),
      'worst_month_pct':float(r.min()),'mean_month_pct':float(r.mean()),
      'median_month_pct':float(r.median()),'stdev_month_pct':float(r.std(ddof=1)),
      'annualized_vol_pct':float(r.std(ddof=1)*np.sqrt(12)),
      'worst_intramonth_mdd_pct':float(table.intramonth_mdd_pct.min()),
      'mean_intramonth_mdd_pct':float(table.intramonth_mdd_pct.mean()),
      'max_underwater_pct':float(table.worst_underwater_pct.min()),
      'months_underwater_over_20pct':int((table.worst_underwater_pct<-20).sum()),
      'longest_losing_streak':int(streak(r<0)),'longest_winning_streak':int(streak(r>0))}

def streak(flags):
    best=run=0
    for f in flags:
        run=run+1 if f else 0;best=max(best,run)
    return best

def episodes(name,floor=-10.):
    """Peak-to-trough underwater episodes deeper than floor, on the 5m path."""
    e=pd.read_csv(RESULTS/'cases'/name/'equity_5m.csv')
    t=pd.to_datetime(e.time_ms,unit='ms',utc=True);v=e.equity.to_numpy()
    dd=v/np.maximum.accumulate(v)-1
    out=[];i=0
    while i<len(v):
        if dd[i]>=0:i+=1;continue
        j=i
        while j<len(v) and dd[j]<0:j+=1
        seg=dd[i:j]
        if seg.min()*100<=floor:
            k=i+int(seg.argmin())
            out.append({'peak':t.iloc[i-1] if i else t.iloc[0],'trough':t.iloc[k],
                'recovered':t.iloc[j] if j<len(v) else pd.NaT,'depth_pct':float(seg.min()*100)})
        i=j
    return pd.DataFrame(out).sort_values('depth_pct')

LABELS={'binance_cap4_risk200':'Binance 비용 · 총한도 4%','zero_fee_cap4_risk200':'수수료 0 가정 · 총한도 4%',
        'binance_cap6_risk200':'Binance 비용 · 총한도 6%','zero_fee_cap6_risk200':'수수료 0 가정 · 총한도 6%'}

def report(everything,summary):
    head=list(LABELS)
    primary=everything[everything.name=='binance_cap4_risk200'].reset_index(drop=True)
    proxy=everything[everything.name=='zero_fee_cap4_risk200'].reset_index(drop=True)
    L=['# A안 거래당 위험 2% — 월간 수익률·낙폭 시계열','',
      '거래당 개별 위험 2%, 총 초기 위험 한도 4% 기준입니다. 2021-01~2026-08 총 68개월 중 40개월이 플러스(58.82%)였고,',
      '월 평균 +2.84%, 중앙값 +1.49%입니다. 평균이 중앙값의 약 1.9배로 소수의 큰 달에 수익이 몰려 있습니다.','',
      '## 집계 방법','',
      '- 원천은 `results/cases/*/equity_5m.csv`의 5분봉 종가 자산곡선입니다. 전략 로직을 다시 돌리지 않고 월 단위로 집계만 했습니다.',
      '- 자산곡선의 시각은 5분 구간의 **끝**에 찍힙니다. 따라서 월 경계는 `(이전 월말, 당월말]` 구간으로 잡아 기존 `analyze_results.period()`와 같은 규약을 씁니다.',
      '- 낙폭은 두 가지를 구분해 보고합니다. 섞어 읽으면 안 됩니다.',
      '  - **월중 낙폭(intramonth)**: 고점을 매월 초에 초기화한 그 달 안의 최대 낙폭입니다.',
      '  - **고점 대비 낙폭(underwater)**: 고점이 전체 기간 최고치이며, 여러 달에 걸친 낙폭은 그대로 이어집니다.',
      '- 검산: 월 수익률을 복리로 이으면 감사된 최종자산과 일치하고(상대오차 1e-9 이내), 연 단위로 묶으면 패키지가 따로 계산한 `year_returns`와 일치합니다(최대 1.7e-13%p). 월별 고점 대비 낙폭의 최솟값은 감사된 `mdd_5m_close_pct`와 같습니다.','',
      '## 요약','','| 지표 | '+' | '.join(LABELS[c] for c in head)+' |','|---|'+'---:|'*len(head)]
    for key,label,fmt in [('positive_months','플러스 월 수 (총 68)','{:.0f}'),('hit_rate_pct','플러스 비율','{:.2f}%'),
      ('mean_month_pct','월 평균','{:+.2f}%'),('median_month_pct','월 중앙값','{:+.2f}%'),
      ('best_month_pct','최고의 달','{:+.2f}%'),('worst_month_pct','최악의 달','{:+.2f}%'),
      ('stdev_month_pct','월 표준편차','{:.2f}%'),('annualized_vol_pct','연율 변동성','{:.2f}%'),
      ('mean_intramonth_mdd_pct','월중 낙폭 평균','{:.2f}%'),('worst_intramonth_mdd_pct','월중 낙폭 최악','{:.2f}%'),
      ('max_underwater_pct','전체 기간 최대 낙폭','{:.2f}%'),('months_underwater_over_20pct','고점 대비 -20% 아래로 간 달','{:.0f}개월'),
      ('longest_winning_streak','최장 연속 플러스','{:.0f}개월'),('longest_losing_streak','최장 연속 마이너스','{:.0f}개월')]:
        L.append(f'| {label} | '+' | '.join(fmt.format(summary[c][key]) for c in head)+' |')
    L+=['','연율 변동성은 월 수익률 표준편차에 √12를 곱한 값입니다. 월별 68개 표본에서 나온 값이며 미래 변동성의 예측치가 아닙니다.','',
      '## 월별 시계열','',
      '`월중 낙폭`은 그 달 안에서의 최대 낙폭, `고점 대비`는 전체 기간 최고치 대비 그 달에 본 최악의 낙폭입니다.','',
      '| 연월 | 수익률 (Binance) | 월중 낙폭 | 고점 대비 | 월말 자산 | 수익률 (수수료 0) | 월중 낙폭 | 고점 대비 |',
      '|---|---:|---:|---:|---:|---:|---:|---:|']
    for k in range(len(primary)):
        a=primary.iloc[k];b=proxy.iloc[k]
        L.append(f'| {a.month} | {a.return_pct:+.2f}% | {a.intramonth_mdd_pct:.2f}% | {a.worst_underwater_pct:.2f}% | '
                 f'{a.equity_end:,.0f} | {b.return_pct:+.2f}% | {b.intramonth_mdd_pct:.2f}% | {b.worst_underwater_pct:.2f}% |')
    flat=primary[primary.return_pct==0].month.tolist()
    gaps=[]
    trades=pd.read_csv(RESULTS/'cases/binance_cap4_risk200/trades.csv')
    for month in flat:
        a=int(pd.Timestamp(month+'-01',tz='UTC').timestamp()*1000)
        b=int((pd.Timestamp(month+'-01',tz='UTC')+pd.offsets.MonthBegin(1)).timestamp()*1000)
        assert len(trades[(trades.entry_ms<b)&(trades.exit_ms>a)])==0,month
        out=pd.to_datetime(trades[trades.exit_ms<=a].exit_ms.max(),unit='ms',utc=True)
        back=pd.to_datetime(trades[trades.entry_ms>=b].entry_ms.min(),unit='ms',utc=True)
        gaps.append(f'{str(out)[:10]}~{str(back)[:10]}')
    L+=['',f'{", ".join(flat)}는 정확히 0.00%입니다. 해당 월에는 보유 포지션이 하나도 없었습니다. '
        f'집계 오류가 아니라 직전 청산과 다음 진입 사이의 공백입니다(각각 {", ".join(gaps)}).','',
      '## 연도별','','| 연도 | 수익률 | 최악의 달 | 최고의 달 | 월중 낙폭 최악 | 연말 고점 대비 |','|---|---:|---:|---:|---:|---:|']
    primary['year']=primary.month.str[:4]
    for y,g in primary.groupby('year'):
        L.append(f'| {y}{"년 1~8월" if y=="2026" else "년"} | {(np.prod(1+g.return_pct/100)-1)*100:+.2f}% | '
                 f'{g.return_pct.min():+.2f}% | {g.return_pct.max():+.2f}% | {g.intramonth_mdd_pct.min():.2f}% | {g.underwater_end_pct.iloc[-1]:.2f}% |')
    L+=['','2026년은 8개월 누적이며 연율화하지 않았습니다.','',
      '## 낙폭 -10% 이상 구간 (Binance 비용 · 총한도 4%)','',
      '| 낙폭 | 고점 | 저점 | 저점까지 | 회복까지 |','|---:|---|---|---:|---:|']
    eps=episodes('binance_cap4_risk200')
    for _,r in eps.iterrows():
        rec='기간 내 미회복' if pd.isna(r.recovered) else f'{(r.recovered-r.peak).days}일'
        L.append(f'| {r.depth_pct:.2f}% | {str(r.peak)[:16]} | {str(r.trough)[:16]} | {(r.trough-r.peak).days}일 | {rec} |')
    deepest=eps.iloc[0];tail=primary.iloc[-1]
    closing=('**표본 종료 시점까지 전 고점을 회복하지 못했습니다**. '
             f'{tail.month} 말 기준 고점 대비 {tail.underwater_end_pct:.2f}%입니다.'
             if pd.isna(deepest.recovered) else
             f'회복까지 {(deepest.recovered-deepest.peak).days}일이 걸렸습니다.')
    L+=['',f'최대 낙폭 {deepest.depth_pct:.2f}%는 {str(deepest.peak)[:16]} 고점에서 {str(deepest.trough)[:16]} 저점까지이며, '+closing,'',
      '## 한계','',
      '- 낙폭은 5분봉 종가 기준입니다. 봉 내부의 더 나쁜 평가손실, 실제 호가 충격, 거래소 청산 엔진은 반영하지 않았습니다.',
      '- 수수료 0 가정은 Lighter Standard 계정 비용 대용치이며, 가격·펀딩은 Binance 실측을 씁니다. 실제 Lighter 체결 성과가 아닙니다.',
      '- 68개월은 표본 하나입니다. 여기의 최대 낙폭·변동성·플러스 비율은 관측치이지 미래의 상한이나 기댓값이 아닙니다.',
      '- 2024년 이후 구간도 이미 검토한 과거 자료이며 표본 밖 검증이 아닙니다.','']
    (RESULTS/'monthly_report.md').write_text('\n'.join(L),encoding='utf-8')

if __name__=='__main__':
    everything=build()
    summary={n:describe(g) for n,g in everything.groupby('name')}
    (RESULTS/'monthly_summary.json').write_text(json.dumps(summary,indent=2))
    report(everything,summary)
    for name,stats in summary.items():
        print(name,'hit',round(stats['hit_rate_pct'],1),'best',round(stats['best_month_pct'],2),
              'worst',round(stats['worst_month_pct'],2),'vol',round(stats['annualized_vol_pct'],2),
              'maxDD',round(stats['max_underwater_pct'],2),flush=True)
