# fiberax-autoupgrade (vm-guardian)

A daemon for [Fiberax](https://fiberax.com) virtual machines. It watches disk, memory and CPU, cleans up
what can safely be cleaned, and when resources are genuinely short it buys more through the
[Fiberax API](https://docs.fiberax.com), within a spending ceiling set by the owner.

A single Python 3 file using only the standard library. Runs as a systemd service.

## What it does

**Disk** (≥ 85% used or < 6 GB free):

1. large text logs that are not covered by logrotate are added to `/etc/logrotate.d/vm-guardian-*` and rotated
   right away;
2. caches are cleaned: journald, apt/dnf, Docker build cache and untagged images, the Nextcloud trash bin and
   old versions (by Nextcloud's own retention policy), plus cache directories found by their signature
   (`cache`, `.cache`, `tmp`, `CACHEDIR.TAG`) — only files nobody has touched for more than 30 days;
3. if space is still short and data from the allowed list (`grow_paths`) has grown — an e-mail and +1 GB of disk;
4. if something else is growing — an e-mail only: which directory, who owns it (container, pod, service,
   process) and by how much it grew.

**Memory** → +1 GB if, over 15 minutes, less than 10% is available on average, or memory pressure (PSI) is above
10%, or the kernel killed a process because the whole machine ran out of memory. If a process was killed because
its own container limit was hit, only an e-mail is sent; VM memory is not increased.

**CPU** → +1 core if the average load over 30 minutes is above 85%.

**Money.** Before every increase the daemon calculates the monthly cost of the VM (assuming it is never powered
off) before and after the change, checks through the API that the increase is possible, and compares it with the
limits:

- above `warn_eur` — a warning in the e-mail;
- above `stop_eur` — nothing is added, a "STOP" e-mail is sent and the daemon waits for `vm-guardian resume`.

**Restarts.** On Fiberax every resource change restarts the VM (see [Test results](#test-results)), so changes
are applied only in the least-loaded hour (from 7 days of API metrics, avoiding the backup window), and at most
one batch per night: whatever was queued when the quiet hour started. Anything queued after that waits for the
next night. A queued disk increase is dropped if space has been freed in the meantime.

**Solo and GPU servers** (plans and fixed packages): nothing is increased; the owner gets an e-mail with the list
of plans and prices. Automatic increases work on Cluster Server.

## Known software

The daemon is not tied to any particular application: it discovers containers, volumes, processes, logs and
caches on its own. On top of that, everything Fiberax ships pre-installed (WordPress, WooCommerce, Nextcloud,
GitLab, Mailcow, Plesk, aaPanel, databases and more — 27 marketplace images) has a profile in
[`apps.json`](apps.json): where the application keeps its data, which of its logs must be rotated, which
directories may be cleaned by age, which built-in cleanup commands are safe to run, and what to advise the owner
when the application grows. Applications are recognised on the machine by their traces (files, services,
container images), not by the image name, so profiles also work for software installed by hand.

`vm-guardian apps` shows what was found on the machine and whether every catalogue image is covered by a profile.
The image table and the rules for adding a profile: [docs/marketplace-images.md](docs/marketplace-images.md).

## Installation

Requirements: root, Python 3.9+, systemd, a Fiberax API token with write access (dashboard → API tokens), and a
way to send mail — either a working `sendmail` or an SMTP server (see [Mail](#mail)).

```sh
git clone https://github.com/YukkoSergey/fiberax-autoupgrade.git
cd fiberax-autoupgrade
./install.sh
```

The installer installs `logrotate` if it is missing (it is not present on clean Debian images), asks for the token
and runs `vm-guardian setup`. Setup shows the current cost of the VM and the prices of the next upgrades,
proposes a spending ceiling (the larger of "5 CPU upgrades" and "+30% of the current price"), and then asks which
data the disk may be grown for automatically. Until setup is done the daemon only monitors and cleans; it does not
add resources. The installer also warns if there is neither `sendmail` nor SMTP configured.

## Commands

| Command | What it does |
|---|---|
| `vm-guardian status` | resources, cost, limits, queue, quiet hour |
| `vm-guardian setup [--stop EUR] [--warn EUR] [--grow paths] [--mail address]` | set limits and the allowed data |
| `vm-guardian allow-growth <dir>` | allow growing the disk for this data |
| `vm-guardian apps` | known software found and profile coverage of the image catalogue |
| `vm-guardian diagnose` | disk diagnosis without changing anything |
| `vm-guardian hold <disk_gb\|memory_mb\|cores>` | observe only: when short, send an e-mail but do not increase; a queued increase is cancelled |
| `vm-guardian release <kind>` | allow automatic increases of this resource again |
| `vm-guardian resume` | lift the stop after the ceiling was exceeded |
| `vm-guardian test-mail` | check mail delivery |

Commands that change the state (`hold`, `release`, `resume`) briefly stop the daemon, because it keeps its state
in memory and would otherwise overwrite the change.

Log: `journalctl -u vm-guardian`. Settings: `/etc/vm-guardian/config.json` (all keys and defaults are in the
`DEFAULTS` dictionary at the top of `vm_guardian.py`; example: `config.example.json`).
State: `/var/lib/vm-guardian/state.json`.

## Mail

- **A report of what was done is always sent.** If the daemon cleaned or changed something (rotated a log,
  cleaned a cache, scheduled or performed an increase), an e-mail goes out every time.
- **Repeated alerts at most once a day** (`mail_cooldown_h`). This applies to messages where the daemon did
  nothing: "something is growing that the disk is not bought for", "automatic increases stopped",
  "API unavailable", "process killed by its container limit".
- **How it is sent.** By default through the local `sendmail`. Clean Debian/Ubuntu images and most marketplace
  images do not have it, so there you need SMTP in `/etc/vm-guardian/config.json`:

  ```json
  { "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_user": "bot@example.com",
    "mail_from": "bot@example.com" }
  ```

  The password goes in a separate file, `/etc/vm-guardian/smtp.password` (chmod 600). Port 465 means TLS from
  the start; other ports use STARTTLS. Check with `vm-guardian test-mail`.
- **Language.** All e-mails are currently in Ukrainian and the texts are hard-coded. Sending them in the client's
  language is an open requirement (see [Open issues](#open-issues)).

## What gets cleaned and how to restrict it

The daemon finds cache directories on its own: by name (`cache`, `.cache`, `tmp`, `*-cache`), by a
`CACHEDIR.TAG` file, from the profiles of known software and from the `cache_dirs` list. Only regular files
untouched for more than `cache_max_age_days` (30) days are removed, and only from directories larger than
`cache_min_mb` (200 MB). To see what would be affected without changing anything: `vm-guardian diagnose`.

If a directory must not be cleaned — for example, it holds downloaded models or anything else that is expensive
to fetch again — add it to `cache_exclude`:

```json
{
  "cache_exclude": ["/root/.cache/huggingface", "/home/build/.cache/bazel"]
}
```

The exclusion covers the directory itself and everything inside it. After changing the config:
`systemctl restart vm-guardian`.

## What it does not do

- it does not delete anything inside container volumes, `/var/lib` or system directories;
- it does not touch database transaction logs, even if they are named `*.log`;
- it does not rotate logs that rotate themselves (runit/svlogd `current` files, e.g. in GitLab);
- it does not shrink resources back;
- it does not tell CPU load apart from I/O wait or steal time.

## Testing on images

`tools/test_images.py` deploys a server from every Linux image in the catalogue (clean OS and marketplace), runs
`tools/remote_probe.py` on it and deletes the server. It checks whether the script works on that distribution,
whether the image's software is recognised and whether the profile paths match the real ones, and performs a real
rotation of a fake log and a real cache cleanup. With `--resize` it also performs real disk, memory and core
increases through the API and checks what the guest sees after each one.

**It creates paid servers and IP addresses.** It deletes only what it created itself (listed in
`<out>/created.json`, and only servers whose name starts with `vmg-test-`). The API token is never copied to the
test servers; the resize test calls the API from the machine running the harness.

```sh
python3 tools/test_images.py --token /etc/vm-guardian/fiberax.token --out /root/vmg-test \
    --images "Debian 13" --resize "Debian 13"          # one image, to start with
python3 tools/test_images.py --token /etc/vm-guardian/fiberax.token --out /root/vmg-test --only os
python3 tools/test_images.py --token /etc/vm-guardian/fiberax.token --out /root/vmg-test --only marketplace
python3 tools/test_images.py --token /etc/vm-guardian/fiberax.token --out /root/vmg-test --cleanup   # if anything was left over
```

Results: one JSON per image plus `summary.json` in the `--out` directory.

## Test results

### Image run, 13 Oct 2026

All 36 Linux images in the catalogue were tested: 9 clean OS images (Debian 12/13, Ubuntu 22.04/24.04/26.04,
AlmaLinux 9/10, Rocky Linux 9/10) and 27 marketplace images. The script works on all of them, and the software of
all 27 marketplace images is recognised. All test servers were deleted afterwards.

| Fact | Detail |
|---|---|
| Python | present everywhere: 3.9 (AlmaLinux/Rocky 9) to 3.14 (Ubuntu 26.04) |
| qemu-guest-agent | installed and running on all 36 images |
| Root filesystem | ext4 on Debian/Ubuntu, xfs on AlmaLinux/Rocky; both grow correctly |
| SELinux | Enforcing on AlmaLinux/Rocky; does not get in the way |
| `logrotate` | missing on Debian 12/13 (installed by `install.sh`) |
| `sendmail` | missing on all clean Debian/Ubuntu images and on most marketplace images; present on AlmaLinux/Rocky, WordPress, WooCommerce, Plesk, GitLab |
| Marketplace base OS | Ubuntu 24.04 for 26 images, Ubuntu 22.04 for Plesk |

Findings that are already handled in the code:

- **Every resource change on Fiberax restarts the VM — even a disk increase.** The shutdown is clean; the server
  is back in 35–50 s, and the new cores, memory and grown filesystem are visible immediately, so no extra reboot is
  needed. Three kinds of change mean three restarts. The daemon records the target before each API call and, after
  the restart, completes the change instead of repeating it.
- **AlmaLinux/Rocky show the guest about 84% of the assigned memory** (kernel reservation). The "did the memory
  arrive" check therefore looks at the increase, not at the absolute value; otherwise it would report a false
  failure.
- On a fresh Rocky Linux 9 the logrotate state file does not exist yet; this is no longer treated as an error.
- AlmaLinux/Rocky were falsely detected as "Ubuntu Desktop" (empty session directories); detection now requires
  real session files.
- CS2 runs under PufferPanel (`/var/lib/pufferpanel/servers`, about 62 GB right after deployment); Minecraft runs
  under Crafty in `/opt/crafty/crafty-4`. Both profiles were corrected and re-tested.
- The Ghost and n8n images ship with more than 1 GB of old npm cache each; the daemon removes it the first time
  space runs short.

Not covered: behaviour under real load and with real data (images were tested right after deployment), actual mail
delivery, Windows and ISO images.

### Production VM `nc.u-ai.cloud`, 12–15 Oct 2026

The daemon has been running on a Nextcloud VM (Debian 13, Cluster Server, Docker + k3s) since 12 Oct.

- **First real increases through the API** (14 Oct, 03:00 UTC): disk 60 → 61 → 62 GB. The VM restarted on each
  change, the daemon came back up, recorded the change and the filesystem had already been grown at boot. The two
  increases happened five minutes apart in the same quiet hour, i.e. two restarts in one night; the daemon now
  does at most one batch per night.
- **Disk filled up to 100% on 14 Oct.** An external agent re-uploaded about 350 unchanged files to Nextcloud over
  WebDAV every 15 minutes; each upload created a new file version, and within two days `files_versions` grew to
  25 GB (203 identical copies of every file), against 4.7 GB of actual files. MariaDB and Elasticsearch went into a
  restart loop. The daemon only saw "the Nextcloud volume is growing" and kept adding 1 GB per night. Resolved by
  hand: 22.66 GB of versions byte-identical to the current files were deleted, and the agent was fixed to upload
  only new and changed files. The daemon still cannot recognise this situation by itself (see open issues).
- During the incident the daemon crashed on a `ps` timeout under memory pressure (fixed), and the alert e-mail
  was not delivered because the local `sendmail` hung on the full disk.
- Memory and swap ran out while the services were restart-looping, so the daemon queued +1 GB of memory. It was
  cancelled with `vm-guardian hold memory_mb`; memory is being observed on this VM for now.
- The night after the cleanup, the daemon cancelled its own queued disk increase because space had been freed.

## Further plans

Plan for moving this logic outside the VM, to the platform side (Proxmox + qemu-guest-agent):
[docs/PLAN-external-controller.md](docs/PLAN-external-controller.md).

## Open issues

For whoever continues this work. As of 15 Oct 2026.

- **Multilingual e-mails.** All texts are currently Ukrainian and hard-coded (including the `advice` fields in
  `apps.json`); the requirement is the client's language (see the plan, §7).
- **Duplicate Nextcloud versions are not recognised.** The daemon should break Nextcloud growth down into
  `files` / `files_versions` / `files_trashbin` / `appdata` and report separately when versions are identical to
  the current file — that usually means a client or integration is re-uploading unchanged files.
- **The disk step is fixed** (`disk_step_gb`, 1 GB per night) and cannot keep up with fast growth. The step should
  depend on the growth rate, and a critical fill (e.g. > 97%) should not wait for the quiet hour.
- **Memory and CPU are not re-checked before execution.** An increase queued during an incident (e.g. services
  restart-looping because the disk is full) is still applied at night even if the problem is gone. Re-checking at
  night is not straightforward: at 03:00 the load is always lower than when it was measured.
- **Mail during a full disk.** The local `sendmail` hangs when the disk is full, so the alert does not arrive
  exactly when it matters most. Direct SMTP (`smtp_host`) avoids this but has not been tested with a real message.
- **No command to cancel a single queued increase**; only `hold` / `release` for a whole resource kind.
- Tested only on Linux images right after deployment and on one production VM; Windows and ISO images are not
  covered.
