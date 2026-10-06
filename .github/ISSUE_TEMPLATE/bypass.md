---
name: Bypass
about: Work marked done or integrated without meeting its acceptance, a branch changed without its gate, a secret reaching a worker, or another way around a safety rule
title: "[bypass] "
labels: bypass
---

<!-- Known gaps (README, "Try to break it"; docs/ARCHITECTURE.md, "Known gaps") are worth
reporting only with a new route. Please remove secrets before posting: `ms doctor`
redacts them, other output may not. -->

**What you bypassed and how**


**Expected behaviour** (per the README or the code)


**Actual behaviour**


**master-system commit** (`git -C "$MS_HOME" rev-parse --short HEAD`)


**Did the worker run as a separate OS user?** (yes / no)


**Attempt id** (from `ms report`)


**`ms report` excerpt**

```
```

**The task's YAML** (from `tasks.yaml`)

```yaml
```

**`ms doctor` output**

```
```
