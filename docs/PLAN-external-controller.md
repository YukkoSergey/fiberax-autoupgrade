# Plan: hybrid auto-upgrade through qemu-guest-agent

Status: draft for assigning the work. The code in this repository is the in-guest daemon `vm-guardian`; this
document describes how to move the same behaviour outside the VM, to the platform side.

## 1. Why

Today the daemon lives inside the VM and keeps an API token there with write access to the client's entire
project. This means: a compromised VM = control over every server in the project; it has to be installed on each
machine separately; and the daemon dies together with the machine when the machine runs out of memory.

The goal is an "auto-upgrade" platform service where:

- decisions, money, resource changes and e-mails live in an external controller;
- nothing runs or is stored permanently inside the guest; everything that needs to be seen or done in the guest
  is executed as one-off commands through `qemu-guest-agent` (QGA below);
- the behaviour for the client is the same as in `vm-guardian` (see the README);
- e-mails and dashboard texts are in the client's language, not Ukrainian (see §7).

Out of scope for this plan: Solo/GPU (an e-mail to the owner only, as now), shrinking resources back, an LLM
adviser for unrecognised software.

## 2. Architecture

```
                    ┌──────────────── controller (platform service) ───────────────┐
 client dashboard ─▶│ VM settings: consent level, spending ceiling, allowed data   │
 (opt-in, limits)   │ polling loop → rules → change queue → executor               │
                    │ state: metric and directory-size history, action log          │
                    └──────┬──────────────────────┬──────────────────┬─────────────┘
                           │ Proxmox API          │ billing/prices   │ mail
              ┌────────────▼───────────┐          ▼                  ▼
              │ Proxmox node           │
              │  VM metrics (RRD)      │
              │  resize / config       │
              │  QGA: fsinfo, exec ────┼──▶ guest: one-off probe / act, no daemon, no tokens
              └────────────────────────┘
```

The controller keeps no state in the guest: directory-size history, OOM counters, mail anti-spam — all of it is in
the controller's database.

## 3. Where each piece of data comes from

| What | Source | Note |
|---|---|---|
| CPU, network, disk I/O | Proxmox: `status/current`, `rrddata` | no agent needed |
| Quiet hour | Proxmox `rrddata` over 7 days | avoiding the VM backup window |
| Filesystem usage | QGA `get-fsinfo` | used/total per filesystem |
| Memory: MemAvailable, PSI, swap | QGA `exec` → probe | the hypervisor's memory metric is useless here: guest page cache looks "used" |
| OOM: whole machine or container limit | QGA `exec` → probe (guest kernel log) | |
| What ate the disk, logs, caches, directory owners | QGA `exec` → probe | |
| Cleanup, logrotate, `growpart`/`resize2fs` | QGA `exec` → act | only with the client's consent, see §6 |
| Prices, current VM cost | platform billing | directly, without the public API |

## 4. The in-guest part: `probe` and `act`

There is no permanent process in the guest. There is one script file that the controller delivers and runs
through QGA.

- **`probe`** — read-only, prints JSON: meminfo, PSI, the OOM counter and kernel-log analysis, top processes,
  directory sizes (`du`), unrotated logs found, cache directories found by signature, software inventory
  (containers, volumes, pods, processes with open files, systemd/cron).
- **`act`** — performs one action from a fixed set and prints JSON with the result:
  `add-logrotate <file>`, `clean-caches`, `grow-fs`. It does not accept arbitrary commands from the controller.

Where to take the code from: the functions `inventory`, `owner_of`, `big_unrotated_logs`, `is_text_log`,
`add_logrotate`, `find_caches`, `clean_caches`, `classify_oom`, `grow_fs`, `du_snapshot` in `vm_guardian.py`
already do all of this and depend only on the Python standard library. They need to be separated from the daemon
loop and given JSON output. The `growers` logic (comparing snapshots) moves to the controller, because the history
is kept there now. `tools/remote_probe.py` is a working example of running this code as a one-off probe on a
fresh server and returning JSON.

The known-software profiles (`apps.json`: 26 profiles for 27 marketplace images, see
`docs/marketplace-images.md`) are data, not code, so the controller uses the same file. The controller knows the
server's image name directly, but recognition by traces in the guest stays primary: the client may have installed
software by hand or changed the layout. The `advice` field is currently in Ukrainian — in the controller it has to
be replaced with message keys (§7).

Python in the guest: on 13 Oct 2026 Python 3.9–3.14 was present on all 36 Linux images in the catalogue
(see §13), so requiring Python 3 is realistic for standard images. A static binary (Go) is still worth
considering for client-installed systems; Windows needs a separate implementation, later.

## 5. Controller logic

The rules are the same as now; thresholds are per-VM parameters with defaults.

| Resource | Condition | Action |
|---|---|---|
| Disk | ≥ 85% used or < 6 GB free | probe → (with consent) logrotate, cache cleanup → if still short and allowed data grows → +disk step; otherwise an e-mail "what grew and where" |
| Memory | MemAvailable < 10% over 15 min, or PSI > 10% over 15 min, or whole-machine OOM | +1 GB |
| Memory | OOM because of a container limit | e-mail only |
| CPU | > 85% over 30 min | +1 core |

Money — as in `vm-guardian setup`: the VM's base price when the service is enabled, a warning threshold, a hard
ceiling on total monthly cost. Default ceiling proposal: the larger of "5 CPU upgrades" and "+30% of the current
price". Above the ceiling — stop, e-mail, manual release.

Differences from the in-guest version worth building in from the start, all learned on the production VM:

- **The disk step must depend on the growth rate**, not be a fixed +1 GB. On the production VM data grew by 9 GB a
  day with a step of 1 GB per night, and later the disk went from 93% to 100% within a day.
- **Explain the growth, not just its size.** On 14 Oct 2026 the disk of the production VM filled up because an
  integration re-uploaded unchanged files to Nextcloud and every upload created a version: 25 GB of byte-identical
  versions against 4.7 GB of files. Buying disk would not have helped. The probe should break known applications'
  data down (for Nextcloud: `files` / `files_versions` / `files_trashbin` / `appdata`) and flag duplicate versions.
- **Re-check before executing.** A change queued during an incident may no longer be needed by the quiet hour
  (disk freed by hand, memory pressure caused by services restart-looping on a full disk). The in-guest daemon now
  drops a queued disk increase when space is no longer short; memory and CPU are harder because night-time load is
  always lower than daytime.
- **Alerts must not depend on the guest.** On the full disk the guest's `sendmail` hung and the alert was never
  delivered; in the controller, mail is sent from the platform side.

## 6. Client consent levels

The client enables the service for a VM and picks a level. A higher level includes the lower ones.

1. **Observation** — metrics, `get-fsinfo`, `probe`; e-mails only.
2. **Auto-upgrade** — plus resource changes within the ceiling and `act grow-fs`.
3. **In-guest cleanup** — plus `act add-logrotate` and `act clean-caches`.

Without level 3 the platform changes nothing in the client's files except growing the filesystem after a disk
increase.

## 7. Languages

Requirement: the client receives every e-mail and every dashboard text in their own language.

- The language is a client property from the dashboard profile; for this service it can be overridden per VM
  (the recipient of e-mails about a particular machine may be a different person).
- If there is no translation for the client's language — English. Ukrainian is not the fallback language.
- No ready-made phrases from the guest: `probe` and `act` return only data and codes
  (`{"action": "add-logrotate", "path": ..., "freed_bytes": ...}`); the controller builds the text from templates.
  The current `vm_guardian.py` does not follow this — Ukrainian strings are hard-coded — so its messages must not
  be carried over, only its logic.
- E-mail templates are separate files keyed by message, one set per language; adding a language = adding a file,
  with no code changes. The e-mail subject also comes from the template.
- Numbers, amounts, dates and sizes are formatted according to the client's locale ("7,6 ГБ" / "7.6 GB"). The
  quiet hour and scheduled restarts are shown in the client's time zone, not UTC.
- Not translated: paths, names of containers, processes and packages, command output.
- The action log in the dashboard stores codes and parameters, not finished text, so it can be shown in the
  current language.
- The controller's own logs for engineers are in English.

## 8. Changing resources

What we know from the public API today: every change of disk, memory or cores through the Fiberax API restarts
the VM (a clean shutdown and start, 35–50 s), even for a disk increase; after that, the guest sees the new
resources and the root filesystem has already been grown at boot. No in-guest action was needed on any of the 36
images tested. A platform-side controller talking to Proxmox directly can do better:

1. Checks: the VM is not locked (backup, migration, snapshot), the agent responds, the budget allows it.
2. Disk: `resize` online → `act grow-fs` → check through `get-fsinfo`. Online disk resize avoids the restart the
   public API currently causes.
3. Cores and memory: if hotplug is enabled for the VM and the guest supports it — online; otherwise the change
   goes into pending and is applied by a restart in the quiet hour.
4. Never more than one restart per night per VM; group the queued changes into one restart.
5. Verify after the change through `probe` (the guest sees the new resources); e-mail the owner with the cost
   before and after.
6. The disk is never shrunk back — this must be stated explicitly to the client when enabling the service.

Proxmox API calls that will be needed (verify against the PVE version in production):
`GET /cluster/resources`, `GET .../qemu/{vmid}/status/current`, `GET .../rrddata`,
`POST .../agent/ping`, `GET .../agent/get-fsinfo`, `POST .../agent/exec`, `GET .../agent/exec-status`,
`POST .../agent/file-write`, `PUT .../resize`, `PUT|POST .../config`, `POST .../status/reboot`.

## 9. What can go wrong

| Situation | Behaviour |
|---|---|
| Agent does not respond (missing, hung, VM from the client's own ISO) | level drops to "CPU from the hypervisor only"; one e-mail to the owner; disk and memory are not touched blindly |
| Agent frozen during backup (`fsfreeze`) | do not poll during the backup window; a timeout ≠ an incident |
| `exec` hangs or returns garbage | timeout, one retry, then treat as "agent does not respond" |
| `du` on a large disk takes long | run with `nice`/`ionice`, rarely (every 6 h and on alert), with a depth limit |
| VM migrated to another node | address by `vmid` through `/cluster/resources`, not by node |
| Change accepted but the guest does not see it | do not repeat; e-mail a platform engineer and the owner |
| Controller crashed in the middle of a change | the change queue is idempotent; the step state is saved before the API call |
| Client changed the resources themselves | re-read the VM configuration before every action |
| Guest disk is completely full | the probe must still work (no temp files in the guest), and the alert goes out from the platform |

## 10. Security

- No tokens and no permanent code in the guest. The probe/act script is delivered every time or verified by hash.
- `guest-exec` is root in the guest. The controller calls only probe/act with fixed arguments; every call is logged
  in a log the client can see.
- The controller's Proxmox account is separate, with a minimal role (audit, monitor, agent, disk/CPU/memory
  changes, power management) and only on the pools of clients that enabled the service. Verify the exact privilege
  names against the PVE version.
- Only metrics, paths and process names leave the guest — never file contents. Paths may contain personal data
  (e.g. e-mail addresses in Nextcloud directory names): mask them in e-mails and logs.

## 11. Stages

| Stage | Deliverable | Done when |
|---|---|---|
| 0. Discovery | test bench: `get-fsinfo`, `exec` with JSON output, disk/CPU/memory hotplug, role privileges | a table "what works on which image and PVE version" |
| 1. Observer | the controller reads metrics and fsinfo and sends e-mails; changes nothing; e-mails already from templates, in at least two languages | a week on 5–10 of our own VMs without false alarms; the same e-mail arrives in the client's language |
| 2. probe | probe extracted from `vm_guardian.py`; size history in the controller; "what grew and where" e-mail, including duplicate-version detection for Nextcloud | on a test VM the report matches `vm-guardian diagnose` |
| 3. Auto-upgrade | budget, queue, quiet hour, resize + grow-fs, post-change check, one restart per night | disk, memory and CPU grow on the bench; the ceiling stops it; no token in the guest |
| 4. Cleanup | `act add-logrotate`, `act clean-caches` at consent level 3 | tests on fake software: a text log is rotated, a database journal is not touched |
| 5. Dashboard | enabling the service, consent level, ceiling with a hint, allowed data, e-mail language, action log | a client completes setup without support; all texts translated into every dashboard language |
| 6. Rollout | our own VMs → volunteer clients → all standard images | — |

Stages 1–2 change nothing on client machines and are useful on their own; it is fine to stop there and collect
threshold statistics before turning money on.

`tools/test_images.py` can serve as the basis for stage 0 and for regression tests: it deploys every catalogue
image, runs a probe and deletes the servers it created.

## 12. To decide before starting

1. Python in the guest or a static binary for probe/act (Python is present on all current Linux images).
2. Whether to enable CPU and memory hotplug for VMs by default (decides whether restarts are needed).
3. How to compute the disk step: fixed, from the growth rate, or "up to N days of headroom".
4. Whether backups are part of the spending ceiling (they are now: backups getting more expensive eats the
   headroom).
5. What to do with VMs without the agent: offer to install it, or not offer the service.
6. Windows: in the first version or later.
7. Which languages we launch with and who owns the template translations.

## 13. What we know from the in-guest version

Tested on all 36 Linux catalogue images (13 Oct 2026) and on one production VM (Debian 13, Cluster Server,
since 12 Oct 2026):

- `qemu-guest-agent` is installed and running on all 36 Linux images; on the production VM the host actively
  uses it (ping, fsfreeze during backup);
- Python 3.9–3.14 is present on all of them; root filesystems are ext4 (Debian/Ubuntu) and xfs (AlmaLinux/Rocky);
  SELinux is Enforcing on AlmaLinux/Rocky and does not get in the way;
- every resource change through the public API restarts the VM; the restart is clean, and new resources are
  visible right after it;
- real increases through the API were performed on test VMs (Debian 13, Ubuntu 24.04, AlmaLinux 9: disk, memory,
  cores) and by the daemon itself on the production VM (disk, twice);
- AlmaLinux/Rocky guests see about 84% of the assigned memory, so "the memory arrived" must be checked by the
  increase, not by the absolute value;
- disk diagnosis, logrotate, OOM classification and the budget check work;
- the 10% PSI threshold is a starting point, not a measured value.
