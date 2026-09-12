# 4주 페이퍼 운용 준비 및 실행

현재 상태: 상시 Linux VPS의 페이퍼 캠페인을 한국시간 2026-09-13 08:21:39에 시작했고, 예정 종료는 2026-10-11 08:21:39다. 서버 검증과 재부팅 후 자동 복구 기록은 [배포 기록](deployments/2026-09-13-paper-start.md)을 참고한다. 이 문서의 설치 절차는 새 서버용이며, 운영 중인 캠페인의 코드·설정·기록을 초기화하는 절차가 아니다.

아래 자원 사양, 재시작 횟수, 감시 주기와 같은 운영 설정은 이번 개선의 **신규 제안 또는 구현값**이다. 기존 인수인계 문서가 검증한 실측 요구량은 **문서에 없음**이다. 원문 성과나 용량 검증값으로 해석하지 않는다.

## 무엇을 실행하는가

- `supervisor.py`가 공개 시세를 사용하는 `run.py --mode paper --loop`만 실행한다. 실주문 확인 플래그가 없고 자식 프로세스에 Lighter 서명 개인키를 전달하지 않는다.
- 전략 전제는 기존 A안과 BCH 제외를 유지한다. 환경에 맞춘 운영 설정은 `paper.config.json`에서 시작 전에 확정한다.
- 최초 시작 시점부터 달력 기준 28일의 UTC 종료 시각을 `campaign.json`에 고정한다. 재부팅이나 재시작이 종료 시각을 연장하지 않는다. 중단된 시간은 관측 시간으로 보충하지 않는다.
- 설정 전체, 소스 해시, 의존성 버전, Git 커밋을 기록한다. 재시작 시 다른 코드·설정·의존성으로 몰래 이어가지 않는다. 종료된 캠페인을 다시 시작해도 새 기간을 생성하지 않는다.
- 페이퍼의 체결, 잔액과 펀딩 정산은 모의 결과다. 공개 호가·mark·공개 펀딩 원자료와 실제 거래소 체결의 검증은 구분한다. 4주 경과만으로 실거래 승인이 성립하지 않는다.

## 사용자가 준비할 조건

| 항목 | 준비 조건 |
|---|---|
| 서버 | 상시 가동 Linux VPS. 예시 기준은 Ubuntu 24.04 LTS와 systemd다. VPS 업체는 지정하지 않았으며 실제 접근 가능 여부로 선택한다. |
| CPU·메모리 | 시작용 권장 사양은 2 vCPU·RAM 4 GB. 실측 최소 사양은 문서에 없음이며 사전 점검 중 사용량을 확인한다. |
| 디스크 | 시작용 권장 여유 공간은 20 GB. 영속 디스크가 필요하며 실제 로그 증가량은 첫날 측정한다. 임시 인스턴스 저장소나 동기화 충돌이 생기는 폴더를 쓰지 않는다. |
| OS 접근 | SSH 접속 주소·사용자와 사용자가 관리하는 로그인 수단. 설치 때만 sudo 권한을 사용하고 봇은 `bbpaper` 일반 서비스 계정으로 실행한다. |
| Python | Python 3.11 또는 3.12와 `venv`. 고정 의존성 설치 후 해당 서버에서 전체 테스트가 통과해야 한다. 서버별 통과 여부는 실행 전에는 확인되지 않은 상태다. |
| 인터넷 | DNS·TLS 인증서 검증과 외부 HTTPS 접근이 정상이어야 한다. 기본 대상은 `fapi.binance.com`과 `mainnet.zklighter.elliot.ai`다. GitHub 및 Python 패키지 배포처도 설치 시 접근해야 한다. |
| 거래소 접근 | Binance USD-M 봉과 Lighter 공개 API 모두 서버 위치에서 실제 응답해야 한다. 지역·계정 약관 제한을 우회하지 않는다. 접근 차단 시 해당 위치에서 운용을 시작하지 않는다. |
| 시간 | 시스템 NTP 동기화가 정상이어야 한다. 앱은 UTC로 기록한다. 선행 점검의 시계 오차 기준은 새 운영 설정이다. |
| GitHub | 저장소가 비공개이면 clone 가능한 읽기 권한이 필요하다. 서버 배포용 읽기 권한으로 충분하며 거래소 키와는 별개다. |
| 거래 자금·키 | **불필요.** Lighter 입금, 지갑 연결, 실거래 API 키, Binance API 키를 준비하지 않는다. |
| 운영자 | 장애·디스크 부족·데이터 공백을 확인할 사람과 접속 수단이 필요하다. 이메일·메신저 알림 대상은 아직 설정되지 않았다. systemd 자체는 외부 메시지를 보내지 않는다. |
| 변경 정책 | 시작 후 코드·패키지·설정을 고정한다. 긴급 수정이 필요하면 원래 기록을 보존하고 별도 캠페인으로 새 버전을 평가한다. |

VPS 구매 전에 가능하면 해당 위치에서 두 공개 데이터 경로의 접근을 확인한다. CPU 사양만으로 실행 가능하다고 판단하지 않는다.

## 설치

아래 명령은 **새 VPS의 SSH 터미널**에서 실행한다. 이미 `/opt/bb-squeeze`에 저장소가 있다면 clone을 반복하지 말고 해당 저장소와 커밋을 먼저 확인한다.

```bash
sudo apt update
sudo apt install -y python3 python3-venv git ca-certificates
sudo timedatectl set-ntp true
timedatectl status

sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/bb-squeeze
git clone --branch codex/harden-paper-operations https://github.com/kimka3/bb-squeeze_v15.git /opt/bb-squeeze
cd /opt/bb-squeeze
git rev-parse HEAD
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-paper.lock
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
cp paper.config.example.json paper.config.json
```

테스트 실패 시 서비스를 시작하지 않는다. `paper.config.json`을 검토하고 최초 모의자본, 관측 주기, 데이터 경로가 원하는 값인지 확인한다. 이 파일에 비밀키를 추가하지 않는다. 수정된 실행 설정은 최초 시작 때 캠페인 폴더에 완전한 설정으로 동결된다.

현재 예시 모의자본은 USDC 가상 장부 기준이다. 인수인계의 USDT 연구자산과 동일한 환산 가치가 검증됐다는 뜻은 아니다. `passive_fill_model=crossed_depth`는 목표가 터치만으로 익절을 만들지 않는 보수적 모형이며 실제 maker 대기열 체결을 관측하지 않는다. 공개 펀딩 응답의 `direction/value/rate` 의미가 검증되지 않은 항목은 원자료를 보관하고 미해결 처리한다. 이런 항목이 있으면 `performance_complete=false`, `equity_excludes_unresolved_funding=true`로 표시되므로 보고서 잔액을 완전한 비용 차감 후 성과로 읽으면 안 된다. 펀딩 정산 의미 확인은 소액 실계좌 전의 별도 통과 조건이다.

실계좌 주문은 현재 코드에서 제출 전에 차단된다. 이전 어댑터가 주문 수락을 체결 완료로 간주하던 문제를 제거하기 위한 조치이며, 실제 종결 체결 조회·보호 확인 연동을 구현·검증하기 전에는 해제하지 않는다.

```bash
sudo bash scripts/install_paper_systemd.sh
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/run.py \
  --mode paper --config /opt/bb-squeeze/paper.config.json \
  --state-dir /var/lib/bb-squeeze/preflight --preflight
```

설치 스크립트는 서비스 파일과 전용 계정을 설치하고 서비스 정의를 검사한다. **시작하거나 활성화하지 않는다.** 선행 점검은 공개 시장 접근, 봉·mark·호가와 시계 등 런타임 조건을 확인한다. 실제 항목과 판정은 출력 JSON을 기준으로 한다. 펀딩 수집의 연속성과 모의 정산 범위는 실행 중 별도로 확인해야 한다. 선행 점검 실패만으로 28일 기간이 소모되지 않는다. 실패 원인 해결 후 같은 점검을 다시 실행한다.

테스트와 선행 점검을 통과한 뒤 다음 명령으로 처음 시작한다.

```bash
sudo systemctl enable --now bb-squeeze-paper.service
sudo systemctl status bb-squeeze-paper.service --no-pager
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/supervisor.py \
  status --campaign-dir /var/lib/bb-squeeze/campaign-01
```

서비스의 선행 점검도 새 캠페인 생성 전에 수행한다. `campaign.json`이 이미 있으면 복구 시작 때 불필요한 네트워크 점검을 강제하지 않아, 인터넷 장애 중에도 기존 종료 시각에 도달한 캠페인을 종료할 수 있다. 새 캠페인의 실제 시작·종료 시각은 최초 실행이 기록한 `campaign.json`을 기준으로 확인한다. 현재 캠페인의 시각은 위 배포 기록에 보존했다.

## 시작 직후 확인

`systemctl active`만으로 데이터 수집이 정상이라고 판단하지 않는다. 다음 세 수준을 함께 확인한다.

```bash
sudo journalctl -u bb-squeeze-paper.service -n 80 --no-pager
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/supervisor.py \
  status --campaign-dir /var/lib/bb-squeeze/campaign-01
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/run.py \
  --mode paper --state-dir /var/lib/bb-squeeze/campaign-01/state --status
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/run.py \
  --mode paper --state-dir /var/lib/bb-squeeze/campaign-01/state --report
```

- supervisor `phase=running`, `worker_process_alive=true`이며 시작·종료 UTC가 기대와 일치해야 한다.
- runner 상태와 보고서의 최근 기록 시각이 계속 갱신되고 데이터 오류·HALT 사유를 확인할 수 있어야 한다. supervisor 갱신은 제어 프로세스의 생존 신호이며 시장 데이터의 신선도를 대신하지 않는다.
- `logs/worker-*.log`, `state/` 내 저널과 관측 기록이 생성되는지 확인한다. 초기화 중에는 보고서가 아직 없을 수 있지만 지속적으로 없으면 성공으로 해석하지 않는다.
- 거래가 아직 없다는 사실과 시장 관측이 멈췄다는 사실을 구분한다. 진입 신호가 없으면 모의 체결이 없는 것이 정상일 수 있다.

## 4주 운영과 장애 대응

새 운영 제안으로 운영자는 매일 최근 데이터 시각, 오류·HALT, 프로세스 재시작, 디스크 잔량을 확인한다. 첫날 로그 증가량을 측정해 남은 기간의 저장 공간을 점검하고, 서비스 장애를 알릴 기존 VPS 모니터링이 있으면 연결한다. 별도의 자동 알림 채널은 구현·연결 완료로 가정하지 않는다.

```bash
df -h /var/lib/bb-squeeze
sudo du -sh /var/lib/bb-squeeze/campaign-01
sudo systemctl show bb-squeeze-paper.service -p ActiveState -p SubState -p NRestarts
sudo journalctl -u bb-squeeze-paper.service --since today --no-pager
```

신규 구현 정책은 작업 프로세스의 예기치 않은 종료에 대해 캠페인 전체에서 최대 12회 재시작한다. 대기는 30·60·120·240초 후 최대 300초로 제한된다. 종료 시각 전의 종료 코드 0도 조기 종료로 기록한다. 한도를 다 쓰면 `failed`로 보존하고 사람이 확인해야 한다. 무제한 자동 복구로 오류를 숨기지 않는다.

systemd는 supervisor 자체 비정상 종료를 별도로 복구하되 단위 서비스 재시작도 제한한다. `75`는 중복 프로세스, `78`은 설정·버전 불일치 또는 복구 한도 소진 등의 조치 필요 상태이며 반복 재시작을 억제한다. 신호로 정상 중지하면 `paused`, 운영자의 STOP 요청은 `stopped`, 고정 기간 도달은 `completed`다. `completed`는 관측 기간 종료일 뿐 데이터 품질 통과 의미가 아니다.

캠페인은 재시작 시 같은 경로를 사용한다. 최초 실행 커맨드를 다시 실행하면 동일한 UTC 종료 시각으로 이어간다. 서비스의 프로세스 그룹 정리가 고아 작업 프로세스를 함께 종료하고, supervisor와 runner의 OS 잠금이 같은 상태 디렉터리의 중복 쓰기를 막는다. 잠금 파일은 프로세스 종료 뒤 남아 있어도 정상이며 지우지 않는다.

인터넷 장애, API 제한, 데이터 불일치는 누락 구간으로 보고한다. 빠진 시세와 체결을 정상 관측으로 소급 생성하지 않는다. 서버 상태만 복구된 경우에는 다음 명령을 사용할 수 있다.

```bash
sudo systemctl reset-failed bb-squeeze-paper.service
sudo systemctl restart bb-squeeze-paper.service
```

캠페인이 이미 `failed`나 `stopped`이면 위 명령이 해당 캠페인을 새 기간으로 다시 열지는 않는다. 기록의 종료 시각이나 phase를 직접 수정해 우회하지 않는다. 원인을 해결한 뒤 별도 캠페인 경로로 시작하고 이전 캠페인의 공백과 실패를 보고한다.

## 잠시 중지와 영구 중지

잠시 정지한 뒤 **남아 있는 원래 기간**으로 재개:

```bash
sudo systemctl stop bb-squeeze-paper.service
sudo systemctl start bb-squeeze-paper.service
```

캠페인을 영구 중지하려면 먼저 supervisor에 STOP을 요청하고 `phase=stopped`를 확인한 뒤 서비스를 비활성화한다.

```bash
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/supervisor.py \
  stop --campaign-dir /var/lib/bb-squeeze/campaign-01
sudo -u bbpaper /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/supervisor.py \
  status --campaign-dir /var/lib/bb-squeeze/campaign-01
sudo systemctl disable --now bb-squeeze-paper.service
```

STOP 반영에는 현재 I/O 완료와 정상 저장 시간이 필요하다. 정상 종료를 우선 요청하고 유예 내 끝나지 않으면 강제 종료 여부를 기록한다. 최종 open position은 모의 장부에 보존하며 실제 시장 청산으로 간주하지 않는다.

## 보관할 결과와 종료 판정

| 파일·폴더 | 의미 |
|---|---|
| `campaign.json` | 변경하지 않는 시작·종료 시각, 버전·해시·재시작 정책 |
| `config.frozen.json` | 캠페인 시작 당시 완전한 실행 설정, 비밀키 없음 |
| `status.json` | supervisor 최근 상태, 자식 PID·시작 식별자, 누적 재시작·종료 사유 |
| `state/runner_status.json` | 작업 프로세스의 최근 건강 상태 |
| `state/report.json` | 저장된 최근 페이퍼 성과·측정 보고서 |
| `state/`의 나머지 파일 | 복구 가능한 모의 장부, 이벤트·원자료 관측 기록 |
| `logs/worker-*.log` | 작업 프로세스 출력. 파일명 날짜는 해당 파일을 연 날짜이며 상시 실행 중 자동 일별 분할을 보장하지 않는다. |

종료 후 서비스 중지를 확인하고 상태·원자료를 함께 보관한다. 서버의 전용 계정으로 보관 파일을 생성하는 예시다.

```bash
sudo systemctl stop bb-squeeze-paper.service
sudo -u bbpaper tar -czf /var/lib/bb-squeeze/campaign-01-results.tar.gz \
  -C /var/lib/bb-squeeze campaign-01
```

요약 손익만 보지 말고 CAGR·MDD·고점 이익 반납, 관측 누락, 데이터 지연·HALT·재시작, 매수·매도별 용량, 부분체결·잔량·보호 복구 사건, 펀딩 누락을 함께 검토한다. 측정치가 제공되지 않은 항목은 계산·관측되지 않았다고 밝힌다. 체결 표본이 부족하면 기간이 끝나도 해당 가설은 미검증 상태다. raw 상태와 로그를 GitHub에 자동 업로드하지 않으며, 보고서 공개 범위는 별도로 정한다.

## Windows 수동 실행 대안

현재 선택한 운용 환경은 Linux/VPS다. 다음은 같은 supervisor를 Windows에서 검증할 때의 대안이며 현재 컴퓨터에서 캠페인을 시작했다는 의미가 아니다. PowerShell에서 저장소로 이동한 뒤 실행한다.

```powershell
Set-Location 'C:\path\to\bb-squeeze'
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-paper.lock
Copy-Item paper.config.example.json paper.config.json
& .\.venv\Scripts\python.exe src/live/run.py --mode paper --config paper.config.json --state-dir paper_runs/preflight --preflight
& .\.venv\Scripts\python.exe src/live/supervisor.py start --campaign-dir paper_runs/campaign-01 --config paper.config.json
```

수동 실행 창 종료, PC 절전·재부팅은 관측 공백을 만든다. Windows 무인 작업 스케줄러는 이 절차에서 설치하지 않는다. Linux systemd 설정을 사용하는 것이 이번 배포 경로다.
