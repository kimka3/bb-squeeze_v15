# 안전 중지 원인별 해결 절차

`bb-reconcile`은 검토한 원인을 대상으로만 실행기에 해결을 요청한다. 주문을 전송·취소하지 않고, 별도 프로세스가 실행기의 상태 파일을 고치지 않는다. 실거래 프로세스가 기존 계좌 잠금을 보유한 상태에서 요청을 처리한다.

## 먼저 확인

VPS에서 `bb-reconcile inspect`를 실행한다. 정확한 HALT 사유와 `reason_id`, 미해결 주문 ID, 실제 관리 중인 포지션, 격리 이력 후보를 확인한다. `release_category: null`인 사유는 이 도구의 해제 대상이 아니다. `bb-live check`를 실행 중인 계좌에서 병행하지 않는다.

원인 해결은 정상 실행기의 주문·계좌·장부 대조를 통해 확인해야 한다. 주문 조회에 없다는 사실이나 시간 경과만으로 주문을 미전송으로 간주할 수 없다. 결과 불명 주문의 재전송, 임의 손익·수수료 입력, 중지 파일 삭제 기능은 제공하지 않는다.

새 주문·보호 주문 오류는 유효한 진행 작업의 종목과 종류가 일치할 때 고정된 중지 원인으로 기록하며, 원래 오류 문구와 작업 내용은 `execution_halt_detail`에 보존한다. 과거의 작업 근거가 없는 임의 오류 문구는 고정 원인으로 추정하지 않는다. `release_category: null`인 과거 사유는 별도 원인 분석과 검증된 수정이 필요하다.

## 주문 결과 및 계좌 대조가 정상화된 HALT

아래 `<...>`는 inspect 결과와 실제 검토 이유로 바꾼다.

```sh
bb-reconcile prepare --action release-halt --reason-id <정확한_reason_id> --reason "주문 ID와 체결·계좌 잔량·장부의 대조 결과 및 해결 근거"
bb-reconcile show <반환된_case_id>
bb-reconcile submit <동일한_case_id>
bb-reconcile status <동일한_case_id>
```

필요하면 `--reason-id`를 반복해 검토한 사유만 선택한다. 요청은 생성 후 5분 동안 유효하다. 그사이 봉·포지션·주문·중지 상태가 바뀌면 거절되며, 새로 inspect/prepare 해야 한다.

실행기는 기존 보호 관리 이후 계좌를 새로 조회한다. 최신 확정봉, 소유 수량, 활성 주문, 네이티브 손절 보호, 이력 수집과 해석, 마진, 미해결 주문·잔여 청산·고아 주문·현재 수집 공백을 재검사한다. 수수료 필드만 미확인이라는 이유로 실제 위험 대조가 정상인 사유를 영구 차단하지는 않는다. 비용 장부가 완전해졌다는 뜻도 아니다.

`released`는 선택한 과거 HALT 사유가 해결 증거와 함께 제거됐다는 뜻이다. 남은 HALT 사유는 유지된다. 자동 복구는 연속 3회 정상 주기, 최소 120초 안정 구간, 주기 간격 최대 90초를 처음부터 확인한다. `ENTRY_PAUSED`는 자동 해제하지 않는다. 이 파일이 남아 있으면 원인을 검토하고 기존 `bb-live resume` 절차를 별도로 따른다. 승인 없이 그 파일을 직접 삭제하지 않는다.

## 미지원 과거 거래·펀딩 기록

`parser_supported: false`이면 현재 코드는 그 원본의 의미를 모른다. 이 도구로 의미를 추정하거나 무시해서 통과시킬 수 없다. 공급자 문서와 실제 주문·계좌 증거에 따라 파서를 수정·시험·배포한 뒤, 원본을 정상 해석할 수 있게 된 경우에만 다음 절차를 사용한다.

```sh
bb-reconcile prepare --action reparse-history --record <trade:ID_또는_funding:ID> --reason "원본 의미를 확인한 근거와 검증된 파서 수정"
bb-reconcile show <반환된_case_id>
bb-reconcile submit <동일한_case_id>
bb-reconcile status <동일한_case_id>
```

`history_reparsed`이면 격리 원본을 보존한 채 정상 이벤트와 해결 영수증을 한 트랜잭션으로 기록했다. HALT는 아직 유지된다. 정상 실행기의 대조가 끝난 뒤 새로운 `release-halt` 사례를 준비한다. 원본·정상 이벤트·해결 영수증이 불일치하거나 공급자 원본이 바뀌면 차단을 유지한다.

## 감사 기록과 장애 시 처리

- `/var/lib/bb-squeeze-live/strategy-01/safety-resolution/`: 검토 사례, 제출 요청, 해제 직전 증거, 결과. 기존 파일을 덮어쓰지 않는다.
- `snapshot.json`의 `safety_resolutions_applied`: 완료된 요청의 해시와 해제 증거 해시. 재시작 뒤 같은 요청으로 새로 발생한 중지를 지우지 않는다.
- 계좌 장부 `history_resolutions`, `history_resolution_requests`: 원본·파서 결과·계좌 식별자·fingerprint·이유·시각. 원래 `history_quarantine`은 남긴다.
- `journal.jsonl`: 준비 및 처리 결과.

`rejected`는 해제되지 않았다는 뜻이다. `queued`는 아직 처리 결과가 없다는 뜻이며 해제 성공이 아니다. 요청 예산 부족은 다음 정상 주기를 기다린다. 처리 중 증거 저장 실패·불일치가 발생하면 신규 진입을 차단하는 기존 안전 경로를 따른다. 실제 장애나 주문을 일부러 만들어 시험하지 않는다.

도구의 대상은 원인이 증명 가능하게 해결된 중지이다. 미지원 유형의 의미, 외부 거래, 설명되지 않는 계좌 변화, 결과 불명 주문, 손상된 저장소를 추측으로 해결하는 기능은 없다.
