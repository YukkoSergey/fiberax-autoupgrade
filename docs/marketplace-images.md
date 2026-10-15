# Fiberax images and software profiles

The catalogue was collected through the API (`GET /v1/server-types/{id}/images`, Cluster Server type; the Solo
Server catalogue is the same). Profiles live in [`apps.json`](../apps.json). To check coverage against the live
catalogue: `vm-guardian apps`.

The API returns only image names and minimum sizes, not how the software is laid out inside. The profiles were
first written from each application's standard layouts (native install and Docker, with several path variants),
and on 13 Oct 2026 they were checked on real servers deployed from every image with `tools/test_images.py`
(see the [README](../README.md#test-results)). The "Verified" column shows the result of that run.

## Marketplace

All marketplace images are based on Ubuntu 24.04, except Plesk (Ubuntu 22.04).

| Image | Category | Recommended | Profile | Data (grow disk) | Logs | Caches by age | Cleanup commands | Backups (shown only) | Verified |
|---|---|---|---|---|---|---|---|---|---|
| Ghost | cms | 1 core / 4 GB / 32 GB | `ghost` | yes | yes | — | — | — | detected; 1.26 GB of old cache in `/home/ghost/.cache` |
| PrestaShop | cms | 2 cores / 8 GB / 64 GB | `prestashop` | yes | yes | — | — | — | detected |
| WooCommerce | cms | 2 cores / 8 GB / 64 GB | `wordpress` | yes | yes | yes | yes | yes | detected; WooCommerce logs in `wp-content/uploads/wc-logs` |
| WordPress | cms | 1 core / 4 GB / 32 GB | `wordpress` | yes | yes | yes | yes | yes | detected |
| Jitsi | communication | 2 cores / 8 GB / 64 GB | `jitsi` | yes | yes | — | — | — | detected; no recordings directory until recording is used |
| Mailcow | communication | 2 cores / 16 GB / 128 GB | `mailcow` | yes | — | — | — | yes | detected; data in Docker volumes `mailcowdockerized_*` |
| MongoDB | databases | 2 cores / 8 GB / 64 GB | `mongodb` | yes | yes | — | — | — | detected |
| MySQL | databases | 1 core / 4 GB / 32 GB | `mysql` | yes | yes | — | — | — | detected |
| PostgreSQL | databases | 1 core / 4 GB / 32 GB | `postgresql` | yes | yes | — | — | — | detected; PostgreSQL 16 + pgAdmin |
| Valkey | databases | 1 core / 2 GB / 16 GB | `valkey` | yes | yes | — | — | — | detected |
| Ubuntu Desktop 24.04 | desktop | 2 cores / 8 GB / 64 GB | `ubuntu-desktop` | yes | — | — | yes | — | detected |
| Docker | devtools | 1 core / 4 GB / 32 GB | `docker` | — | — | — | — | — | detected (Portainer) |
| GitLab | devtools | 4 cores / 16 GB / 128 GB | `gitlab` | yes | yes | — | — | yes | detected; svlogd `current` logs left alone |
| Gitea | devtools | 1 core / 4 GB / 32 GB | `gitea` | yes | yes | — | — | yes | detected |
| Node.js | devtools | 1 core / 4 GB / 32 GB | `nodejs` | — | yes | — | — | — | detected (pm2) |
| n8n | devtools | 1 core / 4 GB / 32 GB | `n8n` | yes | yes | — | — | — | detected; 1.09 GB of old npm cache in `/root/.npm/_cacache` |
| CS2 | gaming | 2 cores / 8 GB / 128 GB | `cs2` | yes | yes | yes | — | yes | detected after fix; runs under PufferPanel, game ~62 GB |
| Minecraft | gaming | 1 core / 4 GB / 32 GB | `minecraft` | yes | yes | yes | — | yes | detected; Crafty in `/opt/crafty/crafty-4`, servers dir appears once a server is created |
| Matomo | monitoring | 2 cores / 8 GB / 64 GB | `matomo` | yes | yes | — | — | — | detected |
| Metabase | monitoring | 2 cores / 8 GB / 64 GB | `metabase` | yes | yes | — | — | — | detected; H2 database `/opt/metabase/metabase.db.*` |
| Uptime Kuma | monitoring | 1 core / 2 GB / 16 GB | `uptime-kuma` | yes | yes | — | — | — | detected (pm2) |
| Zabbix | monitoring | 2 cores / 8 GB / 64 GB | `zabbix` | yes | yes | — | — | — | detected (MySQL backend) |
| Plesk | panels | 2 cores / 8 GB / 64 GB | `plesk` | yes | yes | yes | — | yes | detected |
| aaPanel | panels | 1 core / 2 GB / 32 GB | `aapanel` | yes | yes | — | — | yes | detected |
| Immich | selfhosted | 2 cores / 16 GB / 128 GB | `immich` | yes | — | — | — | yes | detected; Docker, data in `/opt/immich/{upload,postgres}` |
| NextCloud | selfhosted | 2 cores / 8 GB / 64 GB | `nextcloud` | yes | yes | — | yes | — | detected; native install in `/var/www/nextcloud` with MySQL and Redis |
| Vaultwarden | selfhosted | 1 core / 2 GB / 16 GB | `vaultwarden` | yes | yes | — | — | — | detected |

## Clean OS images

No profile is needed: the generic logic applies (discovery of containers, volumes, processes, logs and caches by
their traces).

| Image | Supported | Verified |
|---|---|---|
| AlmaLinux 10 | yes | yes, incl. xfs and SELinux Enforcing |
| AlmaLinux 9 | yes | yes, incl. real disk/memory/core increase |
| Debian 12 | yes | yes; `logrotate` installed by `install.sh` |
| Debian 13 | yes | yes, incl. real disk/memory/core increase; production VM |
| Rocky Linux 10 | yes | yes |
| Rocky Linux 9 | yes | yes, after the logrotate state-file fix |
| Ubuntu 22.04 | yes | yes |
| Ubuntu 24.04 | yes | yes, incl. real disk/memory/core increase |
| Ubuntu 26.04 | yes | yes |
| VyOS Router | no — network appliance | — |
| Windows Server 2022 | no — the daemon is Linux-only | — |
| Windows Server 2025 | no — the daemon is Linux-only | — |

The ISO images (17) are installation media; what will be installed from them is not known in advance, so the
generic logic applies.

## Adding a profile

Add an object to `apps.json`:

- `images` — names of the catalogue images the profile corresponds to;
- `detect` — traces by which the application is recognised on the machine (any one is enough): `paths` (globs;
  point them at files rather than directories that may exist empty), `units`, `containers` (regular expressions
  for the container image or name), `bins`;
- `data` — data directories for which it makes sense to grow the disk (the owner confirms them in
  `vm-guardian setup`); for containers, volumes are added automatically;
- `logs` — logs that must be covered by logrotate;
- `caches` — directories where files older than `max_age_days` may be deleted;
- `backups` — the application's own backups: never deleted, only shown in the e-mail;
- `cleanup` — built-in safe cleanup commands (`bin` — required command, `container` — run inside the container,
  `foreach` — run for every path found);
- `advice` — a hint to the owner about what usually grows and how to limit it (currently in Ukrainian; see the
  multilingual requirement in the plan, §7).

After adding or changing a profile, check it on a real server:
`python3 tools/test_images.py --token ... --out ... --images "<image name>"`.
