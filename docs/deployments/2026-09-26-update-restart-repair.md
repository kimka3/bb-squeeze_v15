# Automatic update restart investigation and repair

The September 22 and September 26 paper service restarts were initiated during
Ubuntu unattended security upgrades. In both dated dpkg log sections, a
`libexpat1` update is followed by `systemctl restart bb-squeeze-paper.service`.
The system journal records matching orderly stop/start events. These records
resolve the previously unknown restart cause; they do not indicate a trading
worker crash or a host reboot.

All dates, versions, counts and timings below are operational observations or
new configuration values: **인수인계 문서에 없음**. They are not backtest results.
Raw host and authentication logs remain in the private local repair evidence,
not in this public repository.

## Cause and impact

- Installed `needrestart`: `3.6-7ubuntu4.5`, Ubuntu 24.04.
- The APT post-invoke hook calls `apt-pinvoke -m u`; affected services can be
  restarted automatically after library updates.
- September 22: `libexpat1` updated to `2.6.1-2ubuntu0.5`; the paper service
  stopped at 06:01:16 UTC and started at 06:01:20 UTC.
- September 26: `libexpat1` updated to `2.6.1-2ubuntu0.6`; the paper service
  stopped at 06:51:15 UTC and started at 06:51:18 UTC.
- The corresponding recorded unobserved interval upper bounds are 8.358 and
  6.999 seconds. They are not exact downtime measurements. Historical gaps stay
  in the runtime coverage ledger; `performance_complete` remains false.
- No simulated fills or positions were recorded at investigation time.

## Applied change

Installed [the narrowly scoped policy](../../ops/needrestart/bb-squeeze-paper.conf)
as `/etc/needrestart/conf.d/99-bb-squeeze-paper.conf`, root-owned, mode 0644.
It adds an exact service-name `override_rc` entry only before the existing fixed
deadline, **2026-10-10T23:21:39Z**. At and after that time, later needrestart
invocations use their normal policy. Expiration itself does not restart anything.

Security package installation, other services, restart notices, and the bot's
crash recovery remain enabled. The paper worker was not stopped or restarted to
apply this change. Campaign, frozen configuration, active configuration and
systemd unit hashes were unchanged, as were the supervisor PID and start time.
Runtime code remains at `78ff5a96e913697e01831aaea60953f8bfa5bd5a`; this repository's
new operational files are not deployed into the frozen runtime source tree.

This policy defers loading future changed libraries into an already running
paper process. Review urgent security fixes during the campaign; if immediate
activation is required, remove the exception and perform controlled maintenance,
recording the gap without extending the campaign. A kernel update is already
pending reboot; the live kernel was `6.8.0-139-generic`, and the installed expected
kernel was `6.8.0-142-generic`. No reboot was performed during this repair.
Campaign completion is a maintenance review point, not evidence that a reboot
or pending library activation has happened automatically.

## Validation and reproduction

```bash
# Read-only policy tests, including exact deadline and unrelated services.
perl ops/needrestart/verify-policy.pl ops/needrestart/bb-squeeze-paper.conf

# Only for this campaign on a reviewed Ubuntu host. Do not copy its fixed
# deadline into a future campaign or overwrite an existing policy blindly.
sudo install -o root -g root -m 0644 ops/needrestart/bb-squeeze-paper.conf \
  /etc/needrestart/conf.d/99-bb-squeeze-paper.conf

# List only: do not use restart mode 'a' to test a live campaign.
sudo needrestart -r l -m u -v
```

All 15 policy assertions passed on the server's Perl. Full production
configuration loading confirmed exactly one matching override with value zero.
The list-only scan completed successfully. The installed file SHA-256 is
`a8c59c1a5712aeb06f07f64f92228e416836314df0109c22b7520fdac28ed445`.
This validates the configured prevention path; no real package upgrade was
forced to demonstrate a restart being skipped.

Rollback removes only `/etc/needrestart/conf.d/99-bb-squeeze-paper.conf`; the next
needrestart invocation then uses the original policy. No daemon reload is
necessary. Removing it does not itself activate pending libraries.

References: [Ubuntu service restart behavior](https://discourse.ubuntu.com/t/needrestart-changes-in-ubuntu-24-04-service-restarts/44671),
[needrestart manual](https://manpages.ubuntu.com/manpages/noble/man1/needrestart.1.html).
