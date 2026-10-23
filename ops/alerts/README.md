# Independent external alerts

This sidecar reads saved trading state. It does not import `src`, access exchange
credentials, submit/cancel orders, clear safety markers, or restart trading.
The trading source fingerprint and strategy parameters remain unchanged.

## User setup (Korean)

1. https://healthchecks.io/ 에 가입하고 이메일 로그인 링크로 접속한다.
2. https://t.me/HealthchecksBot 개인 대화에서 `/start`를 보낸다.
   답장의 링크에서 같은 Healthchecks 계정으로 로그인하고 프로젝트를 선택한 뒤
   **Connect Telegram**을 누른다. 별도 BotFather 토큰은 필요 없다.
3. **Checks**에서 `BB Squeeze`라는 감시 항목을 만든다. **Period 1분,
   Grace 2분**으로 설정하고 이 항목에 Telegram 연동이 활성화됐는지 확인한다.
   자동 생성된 다른 예시 항목과 혼동하지 않는다.
4. 아래 설치가 끝난 VPS root 터미널에서 `bb-alerts configure`를 실행한다.
   해당 항목의 비밀 Ping URL을 숨김 입력창에 붙여넣고 Enter를 누른다.
   URL은 채팅·스크린샷·Git·일반 셸 명령행에 넣지 않는다. 입력 문자가 보이지
   않는 것이 정상이다. `configured: true`를 확인한다.
5. `bb-alerts inspect`로 현재 저장 상태의 판정을 확인하고
   `bb-alerts start`로 **알림 타이머만** 활성화한다.

## Installation and isolation

Deployment requires Python 3 and systemd on Linux. Run as root from a reviewed
copy of this directory:

```sh
python3 install.py --fingerprint APPROVED_TRADING_FINGERPRINT --account-index ACCOUNT_INDEX
```

The installer checks the current activation identity, stages only notification
code, validates systemd units and reloads the systemd unit registry. It leaves
notification scheduling disabled and never touches the trading service. It
refuses conflicting pre-existing files instead of overwriting them.

Fixed paths:

- code: `/opt/bb-squeeze-alerts-20261023`
- configuration: `/etc/bb-squeeze-alerts/config.json` (root only)
- ping UUID: `/etc/bb-squeeze-alerts/healthchecks.uuid` (root only)
- observer state/events: `/var/lib/bb-squeeze-alerts`
- service/timer: `bb-squeeze-alerts.service`, `bb-squeeze-alerts.timer`
- command: `/usr/local/sbin/bb-alerts`

The unit has read-only filesystem protection with only its own output directory
writable. Trading credentials, SSH configuration, home directories and exchange
execution storage are inaccessible in this unit. Root is needed to read the
existing atomically replaced 0600 operational JSON files; their permissions are
not widened. No inbound port is opened.

## What is sent and what it means

A check runs approximately every 30 seconds plus execution duration. Only a
fixed label, up/down and allowlisted reason codes are sent to `hc-ping.com` over
verified HTTPS. Account numbers, balances, positions, raw errors, keys and
fingerprints are not transmitted. Redirects are not followed.

Current service/health timer failures, stale observations, current data gaps,
HALT, pauses, unsafe protection, unresolved orders or reconciliation, recovery
not ready and unreadable state produce `/fail`. Old cumulative errors, historical
coverage gaps, zero observation counters in live mode and missing individual fee
fields alone do not produce an incident. Recovery requires the runner's current
published ready state, fresh reconciliation, no blocking markers and entries
allowed. The observer neither implements nor bypasses the trading recovery
conditions.

The external check also detects absence of pings if the VPS, sidecar or network
fails. With Period 1 minute and Grace 2 minutes, nominal missing-ping detection is
three minutes after the last accepted ping, plus provider and Telegram delivery
latency. It does not prove the VPS lost power. This is periodic detection, not a
guarantee of instantaneous delivery.

HTTP 200 and the exact body `OK` are required. `OK (not found)` and rate-limited
responses are failures. `delivery: ping_accepted` only confirms Healthchecks
accepted the ping. It is **not proof that Telegram delivered a message**.
`telegram_delivery_verified: false` is intentionally never promoted by this
process; actual phone receipt must be recorded separately.

`bb-alerts status` reports the last local observation with its age. `inspect`
performs a fresh read without sending anything. Delivery failures are kept in
local status/events and the journal and never interrupt trading. If the external
provider or Telegram itself is unavailable, this single-channel design cannot
guarantee a notification; check provider status when investigating non-delivery.

## Required end-to-end acceptance test

Do not declare alerting complete from offline tests or `ping_accepted` alone.

1. Start the timer and verify a real healthy observation is accepted and the
   intended Healthchecks check is Up with Telegram enabled.
2. Run `bb-alerts test-notification`. This sends a clearly marked TEST failure,
   never an exchange request. Confirm the Down message arrives on the phone.
   The next actual healthy observer cycle sends Up; confirm that message too.
   If real state is unhealthy, do not force Up to finish the test. An already-Down
   check may not emit another Down transition.
3. Test external absence detection by `bb-alerts stop` (only notification units),
   wait beyond the configured period/grace and confirm the external missing-ping
   alert on the phone. Then `bb-alerts start`, verify an actual healthy accepted
   ping and recovery message. The trading service must remain running throughout.
4. Record check schedule, Telegram receipt times, accepted-ping times and trading
   PID/source/config/fingerprint before and after. Do not store the secret URL.

Stop/rollback affects only notification units. Preserve credentials privately.
No automatic deletion or modification of trading files is part of rollback.

## Verification

Run `tests/test_external_alert_*.py`. HTTP is mocked; no alerts or orders are sent.
Linux root tests additionally verify ownership, atomic secret writes and symlink
rejection. Test real saved state via `inspect`, then validate the installed unit
in its filesystem sandbox before notification activation.

Official references:
- https://healthchecks.io/integrations/telegram/
- https://healthchecks.io/docs/http_api/
- https://healthchecks.io/docs/signaling_failures/
