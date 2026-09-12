"""Export audited cap comparison without network access."""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
r=json.loads((ROOT/'results/analysis.json').read_text())
labels={'binance':'Binance · 슬리피지 2bp/side','zero_fee':'수수료 0 가정 · 슬리피지 2bp/side','binance_slip5':'Binance · 슬리피지 5bp/side','zero_fee_slip5':'수수료 0 가정 · 슬리피지 5bp/side'}
lines=['# A안: 거래당 위험 2%, 총 초기 위험 한도 4% 대 6%', '',
'6% 한도는 전체 기간 CAGR을 높였지만 MDD와 수익·낙폭 비율을 악화시켰습니다. 기존에 정한 목표인 동일 낙폭에서 수익 개선, 동일 수익에서 낙폭 축소, 또는 두 지표 동시 개선에는 해당하지 않습니다.', '',
'## 설정', '',
'- 기간: 2021-01-01~2026-08-31, 종료 경계 2026-09-01 UTC. 초기자산 100,000 USDT, 복리, 실제 경과일/365.25로 CAGR 계산.',
'- A안: BTC 4시간봉 확정 종가가 SMA200 이상이면 신규 숏 위험 절반. 기본 위험 2%, 상승장 숏 위험 1%. 기존 포지션을 레짐 변경만으로 재조정하지 않음.',
'- 변경 변수는 총 초기 위험 한도 4% → 6% 하나. 최대 6개 포지션, 진입 시 총 명목 노출 5배 제한 유지.',
'- 롱 BTC/ETH/SOL, 숏 BTC/ETH/SOL/BNB/XRP/ADA/DOGE/LINK/LTC/BCH/AVAX. 기존 신호 우선순위 유지.',
'- 확정 4시간봉 신호 후 다음 봉 시가 진입. 5분봉 체결 재현과 5분봉 종가 자산곡선. 같은 봉 손절/익절 충돌은 손절 우선.',
'- Binance 수수료 편도 0.05%. 모든 시나리오에 Binance 실측 펀딩 적용. 수수료 0 모델은 Lighter 비용 가정이며 실제 Lighter 가격·펀딩·체결 백테스트가 아님.',
'- 최초 손절가 기준 위험을 잔여 수량에 비례해 집계. 손절가를 올려도 초기 위험 예산을 환급하지 않음.', '',
'## 전체 기간', '',
'| 비용 가정 | 총 위험 한도 | CAGR | MDD | PF | 거래 수 | CAGR / MDD 절댓값 |',
'|---|---:|---:|---:|---:|---:|---:|']
for m,label in labels.items():
 for cap in [4,6]:
  x=r[f'{m}_cap{cap}_risk200']
  lines.append(f"| {label} | {cap}% | {x['cagr_pct']:.2f}% | {x['mdd_5m_close_pct']:.2f}% | {x['profit_factor']:.3f} | {x['trades']} | {x['cagr_per_absolute_mdd']:.3f} |")
lines+=['','## 4%에서 6%로 바꾼 효과','','| 비용 가정 | CAGR 변화 | 낙폭 절댓값 증가 |','|---|---:|---:|']
for m,label in labels.items():
 x=r[f'{m}_cap6_risk200'];lines.append(f"| {label} | {x['delta_cagr_pp']:+.2f}%p | {x['additional_mdd_pp']:+.2f}%p |")
lines+=['','Binance 기본 비용에서 거래 수는 427건에서 500건으로 늘었습니다. 롱은 54건으로 같고 숏은 373건에서 446건으로 증가했습니다. 한도 때문에 건너뛴 진입 시도는 345회에서 198회로 감소했습니다. 한도 확대는 기존 거래의 일괄 배수 확대가 아니라 거래 선택과 이후 복리 자산 경로도 바꿉니다.','','## 2024년 이후 구간','','| 비용 가정 | 한도 | CAGR | MDD |','|---|---:|---:|---:|']
for m,label in labels.items():
 for cap in [4,6]:
  x=r[f'{m}_cap{cap}_risk200']['validation'];lines.append(f"| {label} | {cap}% | {x['cagr_pct']:.2f}% | {x['mdd_pct']:.2f}% |")
lines+=['','이 구간은 이미 검토한 과거 자료이며 미사용 표본 밖 검증으로 간주하지 않습니다. 모든 구간에서 개선해야 한다는 기준을 적용하지 않았습니다. 다만 전체 기간의 수익 대비 낙폭 악화와 최근 구간의 결과를 함께 보면 6% 확대의 효율은 4%보다 낮았습니다.','','## 연도별 수익률','','| 연도 | Binance 4% | Binance 6% | 수수료 0 4% | 수수료 0 6% |','|---|---:|---:|---:|---:|']
for y in range(2021,2027):
 vals=[r[f'{m}_cap{cap}_risk200']['year_returns'][str(y)] for m in ['binance','zero_fee'] for cap in [4,6]]
 lines.append('| '+('2026년 1~8월' if y==2026 else str(y))+' | '+' | '.join(f'{v:.2f}%' for v in vals)+' |')
lines+=['','2026년은 8개월 누적수익률이며 연환산하지 않았습니다. 기본 비용 네 시나리오 모두 최대 낙폭의 고점은 2025-12-01 16:20 UTC, 저점은 2026-05-02 05:20 UTC였습니다.','','## 검증 및 한계','','- 8개 시나리오 재실행. 보유 기간 5분봉 누락 없음. 자산곡선 시작·종료 시점, MDD, 거래 수량, 수수료, 펀딩 및 최종 현금 대사 통과.',
'- 4% 한도 시나리오 4개 모두 이전 위험 증액 실험의 CAGR·MDD·최종자산·거래 수·비용과 일치.',
'- 단위 검증 4개 통과: 거래당 수량 비례, 상승장 숏 위험 절반, 총 4%에서 2% 거래 2개 및 총 6%에서 3개 허용, 요청 범위 밖 입력 거절.',
'- 총 위험 한도와 총 노출 한도는 새 진입 시 적용됩니다. 이후 가격·자산 변화에 따라 비율이 넘을 수 있고 즉시 강제 재조정하지 않습니다. Binance 6% 시나리오의 4시간봉 종가 최대 총 노출은 약 5.24배였습니다.',
'- MDD는 5분봉 종가 기준입니다. 봉 내부 최악 평가손실, 실제 호가 충격, 거래소 최소 주문 수량 및 청산 엔진은 포함하지 않습니다. 과거 MDD가 미래 손실 상한은 아닙니다.',
'- 매매 규칙 변경이나 실전 설정 배포는 수행하지 않았습니다. 이 프로젝트는 요청된 위험 한도 비교용 오프라인 백테스트입니다.','']
(ROOT/'results/bb_squeeze_A_risk2_cap6_report.md').write_text('\n'.join(lines),encoding='utf-8')
