# A안 거래당 위험 2% · 총 초기 위험 한도 4%/6% 비교

Python 3.11 이상에서 `pip install -r requirements.txt` 후 `python run_backtest.py`를 실행하세요.
Windows에서는 run_windows.bat를 실행할 수 있습니다. 거래소 키가 필요하지 않으며 주문을 보내지 않습니다.

원본 데이터와 모든 거래·펀딩·5분봉 자산곡선 및 결과 보고서를 포함합니다.
전략의 거래당 기본 위험(개별 위험한도)은 기존 1.25%에서 2%로 변경했습니다. BTC 4시간봉 SMA200 이상 국면의
신규 숏은 절반인 1%입니다. 총 초기 위험 한도 4%, 총 명목 노출 5배, 동시 보유 6개는 그대로입니다.
엔진의 허용 입력 범위는 거래당 위험 2% 이하, 총 초기 위험 6% 이하입니다.
실행 가능한 사전 정의 비교는 2% 위험에서 4%/6% 한도 × 4개 비용 가정, 총 8개입니다.
다른 전략 실험용으로 계승된 run_frontier.py의 CLI 대신 반드시 run_backtest.py를 사용하세요.

## TradingView Pine Script 이식본

`bb_squeeze_lighter_bot_risk2 v0.2.0` 의 신호·사이징·청산 로직을 TradingView Pine Script v6 전략으로
이식했습니다. **4시간봉 차트에 적용하세요.**

- 스크립트: [`pine/bb_squeeze_v15_candidateA_risk2.pine`](pine/bb_squeeze_v15_candidateA_risk2.pine)
- 전략 해설 + 이식 노트: [`docs/strategy_explained_and_pine_port.md`](docs/strategy_explained_and_pine_port.md)

지표·신호·사이징은 원본 파이썬 구현과 대조 검증했습니다(불일치 0). 다만 TradingView 전략은 심볼당
독립 실행이므로 **4% 공유 초기위험 / 5배 총명목 / 동시 6포지션 같은 교차-심볼 포트폴리오 한도는
재현되지 않습니다.** 펀딩비와 운영 안전장치(HALT 등)도 포함되지 않습니다. 자세한 차이는 해설 문서의
"원본과의 알려진 차이"를 확인하세요.
