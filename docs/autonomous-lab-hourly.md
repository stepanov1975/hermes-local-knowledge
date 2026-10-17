# One-shot hourly lab controller (#71)

This adds **no active schedule**. Nothing installs a timer, sends a message,
launches a pilot, modifies Hermes, updates production or changes the frozen
campaign. Use a separately approved campaign and the trusted controller checkout.
The [manual cycle](autonomous-lab-manual-cycle.md) retains its model, disk,
recovery, scoring and promotion gates. Lab promotion is not deployment.

## Explicit controls

Use the approved interpreter and lab-only environment described in the manual.
The following commands use placeholders, not an existing private campaign:

```sh
python -m scripts.lab_automation init --campaign /private/approved-campaign
python -m scripts.lab_automation status --campaign /private/approved-campaign
python -m scripts.lab_automation pause --campaign /private/approved-campaign
python -m scripts.lab_automation resume --campaign /private/approved-campaign
# Only after authorization to run an experiment:
python -m scripts.lab_automation tick --campaign /private/approved-campaign
```

`init` creates only private local control state (directory 0700, files 0600).
`status` is strictly read-only, including before initialization. `pause` and
`resume` use a short control lock independent of the campaign lock: they work
while a provider call is blocked. Pause does not interrupt an admitted experiment;
it stops subsequent admissions. Resume clears **only the manual latch** and never
launches work, clears a safety pause, reconciles pending work or retries delivery.

A tick takes the existing nonblocking campaign flock. Busy means immediate return,
not waiting or queuing. Under that same lock, it checks manual/safety pauses and
atomically fsyncs an integer UTC-hour high-water reservation **before dispatch**.
The flock remains held throughout the action, with the existing inherited-fd
fencing intact. Equal or older hours are skipped, including after process death
or backward clock changes. Forward jumps admit only the current hour; no missed
slots are replayed. A reservation interrupted before dispatch can lose a slot;
this conservative at-most-once behavior is intentional. A long experiment can
span hours without a second admission. There is no recursive continuation chain.

An unpaused pending attempt calls the existing runner with `recover_only=True`
exactly once. Completion never starts a new attempt in that invocation. A
safety-paused pending attempt is not automatically recovered: use the existing
explicit `lab_experiment recover` after inspecting evidence. Its ambiguity,
auth/quota and disk gates remain unchanged. Ordinary `no_change`/`rejected`
outcomes allow later hours, subject to the existing repeated-strategy gate.

Tick also invokes [protected retention](autonomous-lab-retention.md) under the same
flock before admission and after completion. Manual pause still permits safe
retention; pending work defers disposal and safety-paused campaigns do not clean.
Low/unknown space pauses before disposal. A retention blocker prevents new work.
Exceptions persist structural-only `automation/failure.json` and block subsequent
admissions. Ordinary `resume` does not erase operational or safety evidence.

After repairing the cause and explicitly recovering any pending attempt, use:

```sh
python -m scripts.lab_automation resume --campaign /private/approved-campaign --acknowledge-blockers
```

This separately explicit acknowledgment requires no pending work, safe space and
valid pinned configuration/inputs. It archives the exact failure receipt before
clearing operational/safety pauses and the manual latch; it launches nothing.
Auth/quota repair is the operator's attestation, not a new inference test. If the
cause persists, the next action pauses again. Unknown pending inference can never
be bypassed by this control. Acquisition-time `BlockingIOError` alone means busy;
execution failures are not disguised as lock contention.

## Quiet local notification outbox

```sh
python -m scripts.lab_notifications scan --campaign /private/approved-campaign
```

The scan uses its own short lock, not the long campaign flock, so another process
can scan disk/recovery alerts while inference is blocked. Every initialized tick
scans on exit, including busy/paused returns and exceptions, but runs no alert daemon.
Independently invoking `scan`/`deliver` is necessary to observe alerts during a
blocked action. Neither initialization nor scanning sends anything.

Events are limited to authoritative accepted reports (promotions), new safety
blockers and operational failures. No ordinary no-gain, busy or skipped-hour
messages. Stable IDs bind campaign identity, attempt/report or blocker identity,
and kind; changing free-space samples do not create repeated blocker messages.
Each scan re-reads hash-bound manifest reports and current disk/recovery alerts,
so a crash after report publication but before outbox persistence does not lose
the event. Orphan reports absent from the authoritative manifest cannot announce
a promotion. The outbox is local operational evidence, not a provider transcript;
message text is fixed structural text plus an event hash, never model prose,
private paths, credentials, benchmark cases or exception strings.

Actual dispatch is a separate, **explicitly authorized** operator action:

```sh
python -m scripts.lab_notifications deliver \
  --campaign /private/approved-campaign \
  --executable /absolute/trusted/bin/hermes \
  --target 'telegram:CHAT_ID:TOPIC_ID' \
  --messaging-home /private/messaging-home \
  --messaging-hermes-home /private/messaging-profile
```

Replace both numeric target placeholders explicitly; platform-only/default-home
routing is rejected. No target is inferred from the invoking Hermes session.
The executable must be absolute, executable and not group/other writable. The
operator explicitly selects an already-configured messaging HOME/HERMES_HOME
(or provisions a messaging-only profile separately). Do not copy a production
profile or reuse the lab model's auth home. Existing messaging credentials are
resolved only inside the public CLI; the controller neither opens them nor
copies them into reports. It calls the public interface as an argv list, without
a shell: `hermes send --to TARGET --json SAFE_TEXT`.

Only the child receives the messaging homes. Its environment contains PATH,
HOME and HERMES_HOME only; it excludes all inherited HERMES_SESSION_* and
HERMES_CRON_* state, credentials and reply fields. The lab model environment is
unchanged. Public Hermes CLI delivery may perform its existing best-effort
**target-session** mirror; this is not a promise of zero transcript writes and
does not deliberately mirror into the invoking/origin session.

Before invoking the CLI, the outbox durably persists `claimed`, including target.
Only zero exit plus `success: true` and an actual numeric `message_id` counts as
`sent`. A success boolean alone, skipped response, malformed output, timeout,
exception or nonzero exit is `uncertain`. Process death after claim leaves
`claimed`, also shown as uncertain in status. Neither state is automatically
resent. The finite send timeout is a messaging transport timeout, **not** a
whole-experiment cutoff. This provides at-most-once attempts, not guaranteed
notification delivery. Inspect the target and reconcile uncertain receipts
manually; resetting claims is not a supported automatic retry policy.

## Disabled systemd user examples — documentation only

Do **not** install/enable these without separate operational authorization. Replace
all placeholders and verify the controller environment against the manual first.
`ExecStart` must use an absolute approved interpreter; an environment file must
contain only the approved lab-only variables, never a production profile.

`lab-hourly.service` example:

```ini
[Unit]
Description=One private lab admission tick

[Service]
Type=oneshot
WorkingDirectory=/absolute/trusted/controller-checkout
EnvironmentFile=/private/approved-lab-only-environment
ExecStart=/absolute/approved/python -m scripts.lab_automation tick --campaign /private/approved-campaign
TimeoutStartSec=infinity
Restart=no
UMask=0077
```

`lab-hourly.timer` example:

```ini
[Unit]
Description=Optional hourly private lab admission

[Timer]
OnCalendar=*-*-* *:00:00 UTC
Persistent=false
Unit=lab-hourly.service

[Install]
WantedBy=timers.target
```

There is no whole-experiment timeout, catch-up or automatic restart. These
examples do not schedule outbox scanning/delivery during a blocked experiment.
Retention runs inside ticks. Notifications require an independent no-agent
service so a blocked experiment cannot prevent delivery. After separately
approved configuration, an example `lab-notifications.service` is:

```ini
[Service]
Type=oneshot
WorkingDirectory=/absolute/trusted/controller-checkout
ExecStart=/absolute/approved/python -m scripts.lab_notifications deliver --campaign /private/approved-campaign --executable /absolute/trusted/bin/hermes --target telegram:CHAT_ID:TOPIC_ID --messaging-home /explicit/messaging-home --messaging-hermes-home /explicit/messaging-profile
TimeoutStartSec=180
Restart=no
UMask=0077
```

Its separate disabled timer can use `OnCalendar=*-*-* *:*:00 UTC`,
`Persistent=false`, and `Unit=lab-notifications.service`. This is a cheap local
outbox scan, not a model invocation or worker watchdog. The send's own 120-second
transport bound and per-event claims prevent simultaneous ticks duplicating an
event. No timer/service is installed by this repository. Verify the explicit
Cron topic and do not route stdout into the Installation session. Public CLI
availability is an operator-host prerequisite, separate from the pinned worker
image and official model-client environment.

## Verification boundary

Synthetic tests exercise the real controller with test-only session/evaluator
injection and real local flocks, plus a fake CLI boundary. They verify crash-slot
loss, backward-clock fencing, inherited lock descriptors, one recovery action,
manual controls during a blocked call, quiet events and uncertain delivery without
retry. They are not natural benchmark wins, live provider attempts or proof of
Telegram delivery. No real send or provider execution is required by these tests.
