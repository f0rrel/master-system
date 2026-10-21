# Security and reliability hardening

What was checked against the current repository, what changed in code, what did **not**
change and why, and the host-level steps only the owner can take. Written after the
architecture audit; nothing from that audit was assumed, each finding below was
re-checked on 2026-10-20 against the code and this machine.

**Read this first.** The code changes below reduce accidents and wasted attempts. They do
**not** make workers safe to run untrusted: a worker is still a process of your own user
account, and that account can become root through Docker. Until that is fixed on the host
(section 5) or the system moves into a VM (section 6), treat every worker as having your
full desktop access.

## 1. Verified findings

### Security

| Finding | Status in the current repository / host |
| --- | --- |
| Workers inherit the control plane's environment | **Not true.** `core/worker_env.py` already gives workers an allowlist (PATH, a separate HOME, locale, `CI`), refuses secret-named extras, and tests prove no key reaches a worker or verifier (`tests/test_worker_env.py`). |
| A separate worker HOME hides credentials | **Not true for SSH.** OpenSSH finds keys through the user database, not `$HOME`. Checked with `ssh -G` under a fake HOME: it still resolves `~/.ssh/id_*` of the real home. 2 private key files exist there. |
| Workers run as the desktop user, who is in the `docker` group | **Confirmed**, and the user is also in `sudo` (password required: `sudo -n true` fails). The Docker socket is writable by this user: a worker can start a privileged container and become root. |
| Credentials reachable by a worker | **Confirmed**, by file system access, not by environment: `github-app.pem` and `master.env` (mode 600, same user), `~/.config/gh`, browser profiles (Chrome, Chromium, Brave, snap Chromium). `ms doctor` now lists these (names and modes only). |
| Managed clone can push | The clone's remote is HTTPS and workers get no git credentials, so a worker push fails today. The clone's push URL is not disabled; that is configuration of the Match Legends clone and was left alone. |
| Host services listen on all interfaces | **Confirmed**: Portainer `:9000`, sshd `:22`, Ollama `:11434` (all `0.0.0.0` / `*`). Host configuration; not changed. |
| Worker output stored unredacted | **Confirmed.** Verifier excerpts (`output_excerpt`), worker `stderr_tail`, summaries and error messages went into the append-only history as printed. Fixed (section 2). |
| Service availability | Lingering is off (`Linger=no`): the service stops when you log out or the PC is off. Not changed (systemd is out of scope). |

### Retries

| Finding | Status |
| --- | --- |
| A retry gets the same prompt as the failed attempt | **Confirmed.** `build_prompt` had no failure input; only a cut-off attempt got a note (`core/splits.py`). |
| Retries are unbounded | **Not true.** `core/daemon.py` `work_for` stops a task after 3 failures (2 x tiers with tiers) since your last action on it, and 3 stalls. |
| Exhausted tasks are ambiguous | Partly. They were notified once, but only with batch notifications on (yours is off), and `ms status` said only "failed several attempts". They show in `ms status` and the morning summary; now with the reason. |
| `ml-30` and `ml-50` failed three times the same way | `ml-30` and `ml-15` did: three identical fingerprints. **`ml-50` did not**: its third failure differed. The audit overstated this. |
| The planner is re-invoked on retries | **Not true.** The planner is only asked for a split after two cut-offs, and never while a split waits for you (`core/splits.py` `SplitStep`). No change needed. |

### Spending

Master decisions in the history: 211. 75 were `run_task` (one per attempt); 130 were
`update_task` that only set a status: 63 `completed`, 61 `in_progress`, 6 `blocked`. The
`completed` and `in_progress` moves (124 calls, about 59 % of Master calls) carry no real
judgement: the completion gate already decides deterministically whether they may apply.

- Model id: `deepseek-v4-flash` (master and planner in your config) is, per DeepSeek's docs
  page, a retired alias that is still accepted and billed at the Flash price; the current
  id is `deepseek-flash`. The workers' `deepseek/deepseek-flash` profile is already current.
- Prices: the per-million-token figures are **not** on the pages that could be fetched, so
  they were **not verified** in this task. The repository prices the alias at $0.44/$1.32
  and `deepseek-flash` at $0.30/$1.20. Lowering a safety cap on an unverified number would
  be wrong, so the defaults were left alone.

## 2. Changes implemented

| Change | Files |
| --- | --- |
| Retry feedback: failed attempts since your last action, against the task's *current* spec, each with a stable fingerprint (findings, not timings; paths, numbers and hex ids normalised). Pure code, no model call. | `core/retry_feedback.py` (new) |
| The next attempt's prompt says what the verifier found (redacted, bounded to 2.5 kB), says so when the same failure repeated, and restates that the checks are fixed. Recorded in `attempt_started` (`previous_failures`, `previous_failure_fingerprint`, `identical_failure_streak`). | `core/task_orchestrator.py` `_briefing`, `core/opencode_backend.py` `build_prompt` |
| Stop on a repeat: `[run] max_identical_failures` (default 2, `0` = off). Same fingerprint on the same worker profile in a row makes the task exhausted (waits for you); a different profile (an escalation) starts a new streak, and with tiers the limit is at least 3 so a tier keeps its two tries. The in-session loop refuses the extra `run_task` (`repeated_failure`). | `core/daemon.py` `work_for`, `exhaustion_note`; `core/evidence.py`; `core/autonomous_loop.py`; `core/session_runner.py`; `core/run_cli.py`; `core/run_config.py` |
| Exhaustion is visible with its reason in `ms status`, the morning summary and the (batch) notification. `ms status` and the other overviews now use the same limits as the service, including tiers (they used the default cap before). | `core/ms.py`, `core/summary.py` |
| Redaction before storage: worker artifacts, reasons, error messages, stalls, worker-limit details, tamper names and verifier summaries, findings and evidence are passed through `redact_output` (secret-shaped strings, `NAME_KEY=value` assignments, URL passwords, `Authorization` headers; ordinary source lines stay readable). `ms.redact` keeps its behaviour. | `core/redaction.py` (new), `core/task_orchestrator.py`, `core/ms.py` |
| Worker-limit text, at its source and at its stores: `classify_failure` redacts the provider error `detail` it extracts, `LimitState.record_limit` redacts again before writing `limits/<project>.json`, and `probe_profile` redacts the `reason` (stderr tail, reply text) that `ms worker` prints and that is recorded as a `worker_probe` human action. `repository_tampered`, `restored_refs` and the tamper finding redact the ref and config-key names a worker chose. | `core/worker_limits.py`, `core/task_orchestrator.py` |
| Git guards for worker **and verification** processes (they share one environment): `GIT_SSH_COMMAND=/bin/false`, `GIT_TERMINAL_PROMPT=0`. Side effect: an acceptance command that fetches a dependency over `git+ssh` (for example an npm package from `git@github.com:`) now fails at once instead of authenticating with your key. HTTPS dependencies are unaffected. Not overridable through `extra` (the name looks like a secret). | `core/worker_env.py` |
| `ms doctor` "Worker exposure": Docker socket, sudo/wheel/admin, SSH private keys, credential files and modes, `gh` config, browser profiles. Read-only; contents are never read. | `core/worker_exposure.py` (new), `core/ms.py` |
| Spend is labelled an estimate; `spend_since` also returns `worker_reported_usd` (the worker tool's own figure) and `basis`. | `core/report.py`, `core/ms.py` |

Task states keep their meaning. A failed or timed-out attempt never changes a task's
status (checked by test); "exhausted" is derived from history, as before, not a new state.
The completion gate, spec hashing and verifier were reviewed and left as they are: they
already refuse a completion after a failed verification, after a changed spec, and for an
unverified, timed-out or errored latest attempt (`tests/test_evidence.py`). A retry runs
against the task's current spec, and failures recorded under an older spec are ignored.

### Expected effect on calls (honest numbers)

- Replaying the new fingerprint over a copy of the history (human resets ignored): a limit
  of 2 would have skipped 4 of 75 attempts, of which 2 were certain repeats (`ml-15`,
  `ml-30`) and 2 were attempts that, as it happened, failed differently. At about $0.01 per
  attempt (worker plus two Master calls) that is cents, not dollars. The value is time
  (a timed-out attempt takes up to 20 minutes) and a clearer signal to you.
- Whether the retry feedback raises the pass rate on the second attempt **cannot be
  quantified from repository evidence**: no attempt has run with it yet.
- Not done: replacing the 124 status-only Master calls with a transition table. It would
  have saved roughly $0.39 over the system's life (about 14 % of $2.77), but it changes the
  documented rule that the Master decides every step, touches the loop most tests are
  built around, and is not "clearly safe". Recorded as a follow-up. The planner accounts
  for most spend ($1.92) and is only called when you chat or a split is drafted.

## 3. Tests

- Baseline at the start (previous session, same machine): 1,593 passed, 10 skipped.
- After the changes: **1,681 passed, 10 skipped** (`.venv/bin/python -m pytest -q -p no:cacheprovider tests`, 108 s). No pre-existing failure, no regression. The 10 skips are the opt-in live and integration tests (they need a paid model or network) and were not run.
- New tests (88): `tests/test_retry_feedback.py` (fingerprints, repeated vs different failures, other worker profile, human reset, spec change, limits/stalls/passes not counted, missing and malformed details, note content and bounds, `work_for`, config), `tests/test_retry_orchestration.py` (real orchestrator and loop: prompt content, redaction in history, state unchanged, in-session refusal, human edit resumes), `tests/test_redaction.py`, `tests/test_limit_redaction.py` (limit detail at its source, in the state file on disk, in `describe` and in probe results), `tests/test_worker_exposure.py`, `tests/test_spend_basis.py`, two in `tests/test_worker_env.py` (git over SSH fails without starting `ssh`; the guard cannot be overridden), and one each in `tests/test_retry_orchestration.py` (secret in a worker limit) and `tests/test_tamper_check.py` (secret in a branch name). The expected key set in the existing allowlist test was updated for the two new variables.
- The new tests were mutation-checked (breaking the streak rule and the profile rule fails them). One real bug was caught while writing them: quoted values containing spaces escaped redaction; fixed.
- No paid model was called. Prices and model ids were read from DeepSeek's public docs page only.

## 4. Remaining risks (not fixed)

1. **Root through Docker** (and the `sudo` group). Any worker command can start a root container. Not fixed: host group membership is out of scope.
2. **Same-user file access.** A worker can read SSH keys, `master.env` (DeepSeek key and bot tokens), `github-app.pem` (can mint GitHub App tokens), `gh` login, browser sessions, and write anywhere the user can. The environment allowlist and `GIT_SSH_COMMAND` stop accidents, not a worker that does it on purpose (or a prompt-injected one).
3. **Branch protection depends on the owner's `gh` admin login** being unreachable. It is reachable.
4. **Host services on all interfaces**: Portainer (itself a Docker control panel), sshd, Ollama.
5. **No isolation between attempts**: shared git directory (gap V2), processes that call `setsid` outlive the attempt (N1).
6. **Redaction is best effort.** Patterns cannot recognise every secret; one printed in an unusual shape would be stored. Deliberately **not** changed:
   - **Raw attempt logs** (`<state dir>/logs/<project>/<attempt>/*.stdout|stderr`): they are the forensic record, and each file's SHA-256 is stored in the history, so editing a log would break that link. Checked on this machine: 1,929 files, none contains a token-, key-, URL-password- or header-shaped string. 29 stdout files match only the environment-style rule, on the game's own constants (`PROGRESS_KEY`, `SESSION_KEY`, `EFFECTS_KEY`), which are not secrets. The files are mode 644 in a 775 directory, so any local user can read them; this is a single-user machine, but it is the reason not to log secrets in the first place.
   - **History written before the change** (append-only by design, enforced by a database trigger). The existing limit state file was checked and holds nothing secret-shaped, so it needed no rewrite.
   - **A cost of the environment-style rule:** it also redacts harmless source constants whose names end in `KEY`, `TOKEN` and the like (as above), so a retry note or stored excerpt can show `<redacted>` where a game constant was. The diagnosis stays readable; the rule errs on the safe side.
   - **Names the worker chose** are redacted only where they match a secret pattern.
7. **Fingerprint noise falls back to the old cap.** Output with random names (for example temp directories) does not match, so such a task is stopped by the 3-failure cap, as before, not by the identical rule.
8. **Service restart pending.** The running service still has the old code. Restart it once `ms status` reports Idle: `systemctl --user restart master-system.service` (not done here).
9. Already-stored history is not rewritten (it is append-only): earlier excerpts may hold unredacted output.

## 5. Manual steps before running untrusted or unattended workers more widely

Each needs you; none was done. In order of value:

1. **Take workers off the Docker socket.** `sudo gpasswd -d $USER docker`, then log out and in. Risk: your own `docker` commands, Portainer-based workflows and anything relying on passwordless Docker stop working until you use `sudo docker` or rootless Podman/Docker. Running containers keep running. Alternative: do nothing here and do step 4 instead, which makes this moot for the system.
2. **Check `sudo`.** `sudo -n true` currently asks for a password (good). Make sure no `NOPASSWD` rule exists for your user in `/etc/sudoers` or `/etc/sudoers.d/`. Risk: none if you only read.
3. **Bind host services to localhost or firewall them.** Portainer (`:9000`), Ollama (`:11434`; `OLLAMA_HOST=127.0.0.1`), sshd (`:22`, or restrict with a firewall / key-only login). Risk: you could lock yourself out of remote access; test from a second session first.
4. **Run workers as a different identity** (the real fix): a dedicated unprivileged user without Docker/sudo, and ideally a VM (section 6). Move `github-app.pem` and `master.env` to that identity only, and keep your own SSH keys, `gh` login and browser profiles in the account workers cannot read.
5. **Credentials.** If you cannot rule out that a worker read your files, rotate the DeepSeek key, the Telegram/ntfy tokens and the GitHub App key, and review the app's installation. Prefer a spend-limited DeepSeek key for the worker profile.
6. **Branch protection.** Keep the rulesets without bypass actors and keep the owner's admin login out of workers' reach (item 4).
7. **Optional: disable pushes from the managed clone** (`git remote set-url --push origin no_push` in the Match Legends clone). The system's own pushes name the repository URL explicitly (`core/github.py`), so they do not use the remote's push URL; a human `git push` from that clone stops working, which is the point.
8. **Apply the code changes:** restart the service when idle. The only new setting is `[run] max_identical_failures` (default 2, so no config edit is needed; set `0` to turn it off).
9. **Optional model id:** change `model = "deepseek-v4-flash"` to `"deepseek-flash"` in the `[master]` and `[planner]` sections and add the matching entry under `[prices]` once you have confirmed the prices on DeepSeek's pricing page. That file is outside this repository and was not edited.
10. Housekeeping the audit found (5.4 GB of worktrees, 100+ attempt branches) was not touched.

## 6. Follow-up: VM and disposable execution

Not started here. Suggested shape, from the audit: a local KVM VM for the whole system, rootless Podman per attempt with no network except an allowlist (package registry, the model API), a read-only mount of the repository plus a scratch worktree, and credentials that exist only inside the VM. Then move the VM to a small cloud host if the Match Legends experiment shows the value. Do this as its own task, after items 1 to 4 above are decided. It also fixes the availability gap (PC off, no lingering) and gives gap N1 a real answer.

Other follow-ups, each its own task: the status-move transition table (about $0.39 of lifetime Master spend); redacting raw attempt logs on write; re-checking DeepSeek prices and updating the cap table; Claude Code as a capped escalation worker (not added: out of scope here, and its terms need care); a pre-flight that refuses to start workers while Docker-group membership persists, once you have decided you want that.
