#!/usr/bin/env python3
"""
vm-guardian - демон, що стежить за ресурсами цієї віртуалки (диск, пам'ять, CPU)
і за потреби розширює її через Fiberax API (https://api.fiberax.com/v1).

Логіка:
  диск  -> діагностика: логи без logrotate -> додати в logrotate; кеші -> підчистити старе;
           виросли дані з дозволеного списку (grow_paths) -> лист + +1 ГБ диска; інше -> лише лист.
           Софт не зашито: контейнери, томи, поди, процеси, кеші й логи виявляються наново щоразу.
  пам'ять / CPU -> +1 ГБ / +1 ядро.
  Перед кожним збільшенням: місячна вартість (VM працює весь місяць) + перевірка через API,
  що збільшення можливе. Ліміти залежать від ціни цієї VM і задаються
  користувачем у `vm-guardian setup` (показує поточну вартість, ціни апгрейдів і пропонує стелю):
  понад warn_eur -> попередження; понад stop_eur (стеля на загальну суму) -> стоп до `resume`.
  Поки setup не пройдено, демон лише моніторить і чистить, але ресурсів не додає.
  Саме збільшення (з перезавантаженням) виконується лише в годину найменшого навантаження.

Тільки стандартна бібліотека. Запуск: vm_guardian.py run | setup [--stop EUR] [--warn EUR] [--grow шляхи] [--mail адреса]
        | allow-growth <тека> | apps | status | diagnose | resume | test-mail
"""
import datetime as dt
import fnmatch
import glob
import json
import os
import pwd
import grp
import re
import shutil
import socket
import stat
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from email.message import EmailMessage
from zoneinfo import ZoneInfo

CONFIG_PATH = os.environ.get("VM_GUARDIAN_CONFIG", "/etc/vm-guardian/config.json")
GB = 1024 ** 3
MB = 1024 ** 2

DEFAULTS = {
    "api_base": "https://api.fiberax.com/v1",
    "token_file": "/etc/vm-guardian/fiberax.token",
    "server_id": None,                      # None -> знайти за IP цієї машини
    "state_file": "/var/lib/vm-guardian/state.json",
    "mail_to": "",                          # кому писати; задається в setup
    "mail_from": "vm-guardian@" + socket.getfqdn(),
    "mail_cooldown_h": 24,                  # однакові листи не частіше
    "interval_s": 60,
    "dry_run": False,                       # True -> нічого не міняти, лише логувати
    # пороги
    "disk_mount": "/",
    "disk_used_pct": 85,                    # "не вистачає диска": зайнято >= цього...
    "disk_min_free_gb": 6,                  # ...або вільно менше цього
    "disk_step_gb": 1,
    "disk_diag_cooldown_h": 6,
    "mem_avail_pct": 10,                    # середній MemAvailable за вікно нижче цього
    "mem_psi_pct": 10,                      # ...або тиск пам'яті (PSI some) у середньому вище цього:
                                            #    процеси стільки % часу чекали на пам'ять (своп, витіснення кешу)
    "mem_window_min": 15,
    "mem_step_mb": 1024,
    "cpu_busy_pct": 85,                     # середнє завантаження CPU за вікно вище цього
    "cpu_window_min": 30,
    "cpu_step": 1,
    # бюджет
    # бюджет - задається через `vm-guardian setup`, бо залежить від ціни конкретної VM
    "baseline_eur": None,                   # місячна вартість VM на момент setup
    "warn_eur": None,                       # вище цієї суми - попередження в листах
    "stop_eur": None,                       # стеля загальних місячних витрат; None -> ресурси не додаємо
    "suggest_cpu_steps": 5,                 # setup пропонує стелю: +N апгрейдів CPU...
    "suggest_pct": 30,                      # ...або +N% від поточної вартості, що більше
    "hours_per_month": 744,                 # 31 день без вимкнень - найгірший місяць
    # тиха година (UTC)
    "default_quiet_hour_utc": 3,
    "quiet_hour_utc": None,                 # число -> примусово, None -> з метрик API
    # логи
    "log_big_mb": 100,
    "log_roots": ["/var/log", "/root", "/home/*", "/opt", "/srv", "/var/www", "/usr/local", "/data"],
    "log_volume_depth": 3,                  # наскільки глибоко шукати *.log у томах контейнерів
    "log_extra_globs": ["/var/lib/docker/containers/*/*-json.log"],
    "logrotate_size": "50M",
    "logrotate_keep": 5,
    # кеші: чистимо файли старші за cache_max_age_days у теках, більших за cache_min_mb
    # (кеші шукаються самі за ознаками; тут - додаткові теки та винятки)
    "cache_dirs": ["/var/tmp", "/var/lib/systemd/coredump", "/var/crash"],
    "cache_exclude": [],
    "cache_search_depth": 5,
    "cache_max_age_days": 30,
    "cache_min_mb": 200,
    "journal_max_mb": 300,
    # дані, під які можна автоматично докуповувати диск (glob); задає власник у setup / allow-growth
    "grow_paths": [],
    "apps_file": None,                      # профілі відомого софту; None -> apps.json поруч зі скриптом
    "du_interval_h": 6,
    "du_depth": 6,
    "du_min_mb": 100,
    "growth_min_mb": 200,
}

# Теки з такими файлами - сховище СУБД: "логи" там є журналами транзакцій, їх не чіпаємо.
DB_MARKERS = {"ibdata1", "aria_log_control", "PG_VERSION", "mongod.lock", "WiredTiger", "raft_state.json",
              "node.lock", "appendonlydir", "pg_wal"}
CACHE_NAME = re.compile(r"^\.?(cache|caches|tmp|temp)$|^_cacache$|[-_.]cache$", re.I)
# Тут кеші за ознаками не шукаємо й нічого не видаляємо.
CACHE_PROTECTED = ("/proc", "/sys", "/dev", "/run", "/boot", "/etc", "/usr", "/bin", "/sbin", "/lib", "/snap", "/tmp",
                   "/lost+found", "/var/lib", "/var/cache/apt", "/var/cache/debconf", "/var/cache/ldconfig",
                   "/var/backups", "/var/spool", "/var/mail")

PRICE_KEY = {"cores": "maxcpu_on", "memory_mb": "maxmem_on", "disk_gb": "maxdisk"}
KIND_UA = {"cores": "CPU (ядра)", "memory_mb": "пам'ять", "disk_gb": "диск"}


def log(*a):
    print(*a, flush=True)


def run(cmd, timeout=600, **kw):
    """Запускає команду, повертає (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, text=True, errors="replace", **kw)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def fmt_size(b):
    return f"{b / GB:.2f} ГБ" if abs(b) >= GB else f"{b / MB:.0f} МБ"


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- API

class ApiError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body or {}
        self.code = self.body.get("code", "")
        super().__init__(f"HTTP {status} {self.code} {self.body.get('detail') or self.body.get('title') or ''}")


class Api:
    def __init__(self, base, token_file):
        self.base = base.rstrip("/")
        self.token_file = token_file

    def call(self, method, path, body=None, idem=None, raw_status=False):
        with open(self.token_file) as f:
            token = f.read().strip()
        url = (self.base if not path.startswith("/v1/") else self.base.rsplit("/v1", 1)[0]) + path
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
                   "User-Agent": "vm-guardian/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if idem:
            headers["Idempotency-Key"] = idem
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=75) as r:
                    status, payload, hdrs = r.status, r.read(), r.headers
            except urllib.error.HTTPError as e:
                status, payload, hdrs = e.code, e.read(), e.headers
            try:
                js = json.loads(payload or b"{}")
            except ValueError:
                js = {"title": payload[:200].decode(errors="replace")}
            if status in (429, 503) and attempt < 3 and hdrs.get("Retry-After"):
                time.sleep(min(int(hdrs["Retry-After"]) + 1, 65))
                continue
            if raw_status:
                return status, js
            if status >= 400:
                raise ApiError(status, js)
            return js
        raise ApiError(status, js)

    def get(self, path):
        return self.call("GET", path)["data"]

    def wait_task(self, task, timeout=900):
        if not task:
            return
        end = time.time() + timeout
        while not task.get("is_terminal"):
            if time.time() > end:
                raise ApiError(0, {"title": f"task {task.get('id')} не завершилась за {timeout}s"})
            time.sleep(5)
            task = self.call("GET", task["poll"])["data"]
        if task["status"] != "succeeded":
            raise ApiError(0, {"title": f"task {task['id']} {task['status']}: {task.get('error')}"})


# ---------------------------------------------------------------- guardian

class Guardian:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                self.cfg.update(json.load(f))
        self.api = Api(self.cfg["api_base"], self.cfg["token_file"])
        self.apps = []
        apps_file = self.cfg["apps_file"] or os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json")
        try:
            with open(apps_file) as f:
                self.apps = json.load(f)["apps"]
        except (OSError, ValueError, KeyError) as e:
            log(f"профілі софту не завантажено ({apps_file}): {e}")
        self.state = {"mail_sent": {}, "du": [], "pending": {}, "in_progress": None,
                      "added_eur": 0.0, "halted": False, "resizes": [], "last_diag": 0}
        try:
            with open(self.cfg["state_file"]) as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass
        n = max(self.cfg["mem_window_min"], self.cfg["cpu_window_min"]) * 60 // self.cfg["interval_s"] + 1
        self.samples = deque(maxlen=n)
        self.prev_cpu = None
        self.oom_seen = None

    # ---------- state / mail

    def save(self):
        path = self.cfg["state_file"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def mail(self, subject, body, key=None):
        """Лист на mail_to. key задає антиспам: той самий key не частіше mail_cooldown_h."""
        now = time.time()
        if not self.cfg["mail_to"]:
            log(f"mail_to не задано (vm-guardian setup), лист не надіслано: {subject}")
            return False
        if key and now - self.state["mail_sent"].get(key, 0) < self.cfg["mail_cooldown_h"] * 3600:
            log(f"mail suppressed (cooldown) [{key}] {subject}")
            return False
        host = socket.gethostname()
        m = EmailMessage()
        m["From"], m["To"] = self.cfg["mail_from"], self.cfg["mail_to"]
        m["Subject"] = f"[vm-guardian {host}] {subject}"
        m.set_content(body + f"\n\n-- \nvm-guardian на {host}, {utcnow():%Y-%m-%d %H:%M} UTC\n")
        log(f"MAIL -> {self.cfg['mail_to']}: {subject}\n" + "\n".join("  | " + l for l in body.splitlines()))
        try:
            p = subprocess.run(["/usr/sbin/sendmail", "-t", "-oi", "-f", self.cfg["mail_from"]],
                               input=m.as_bytes(), timeout=60)
            ok = p.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            log("sendmail failed:", e)
            ok = False
        if ok and key:
            self.state["mail_sent"][key] = now
            self.save()
        return ok

    # ---------- local metrics

    def disk(self):
        s = os.statvfs(self.cfg["disk_mount"])
        total, free = s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
        used = total - s.f_bfree * s.f_frsize
        return {"total": total, "free": free, "used_pct": 100.0 * used / (used + free)}

    def disk_low(self, d=None):
        d = d or self.disk()
        return d["used_pct"] >= self.cfg["disk_used_pct"] or d["free"] < self.cfg["disk_min_free_gb"] * GB

    @staticmethod
    def meminfo():
        out = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                out[k] = int(v.split()[0]) * 1024
        return out

    def sample(self):
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:9]]
        idle, total = v[3] + v[4], sum(v)
        cpu = None
        if self.prev_cpu and total > self.prev_cpu[1]:
            cpu = 100.0 * (1 - (idle - self.prev_cpu[0]) / (total - self.prev_cpu[1]))
        self.prev_cpu = (idle, total)
        mi = self.meminfo()
        oom = 0
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("oom_kill "):
                    oom = int(line.split()[1])
        # Лічильник OOM зберігаємо в стані, щоб не пропустити вбивства, які сталися, поки демон не працював.
        boot = open("/proc/sys/kernel/random/boot_id").read().strip()
        rec = self.state.get("oom") or {}
        if rec.get("boot") == boot:
            new_oom, since = oom > rec.get("n", 0), rec.get("ts", time.time() - 600)
        else:                                               # перший запуск або після перезавантаження
            new_oom, since = bool(rec) and oom > 0, time.time() - float(open("/proc/uptime").read().split()[0])
        if rec.get("boot") != boot or rec.get("n") != oom:
            self.state["oom"] = {"boot": boot, "n": oom, "ts": time.time()}
            self.save()
        elif self.oom_seen is not None:
            rec["ts"] = time.time()                         # у пам'яті; на диск - лише коли щось змінилось
        self.oom_seen = oom
        psi = None                                          # тиск пам'яті: % часу, коли процеси чекали на пам'ять
        try:
            with open("/proc/pressure/memory") as f:
                psi = float(re.search(r"some .*?avg60=([\d.]+)", f.read()).group(1))
        except (OSError, AttributeError):
            pass
        s = {"t": time.time(), "cpu": cpu, "mem_avail_pct": 100.0 * mi["MemAvailable"] / mi["MemTotal"],
             "oom": new_oom, "oom_since": since, "mem_psi": psi}
        self.samples.append(s)
        return s

    def classify_oom(self, since):
        """Розбирає журнал ядра після `since`: ([процеси, вбиті через брак пам'яті на всій машині],
        [(процес, чий ліміт) - вбиті через власний ліміт контейнера/сервісу]). Якщо журнал недоступний,
        вважаємо, що пам'яті бракує всій машині."""
        rc, out = run(["journalctl", "-k", "-o", "cat", "--since", f"@{int(since) - 30}"], timeout=30)
        if rc != 0 or not out.strip():
            rc, out = run(["dmesg"], timeout=30)
        whole, limited = [], []
        for line in out.splitlines():
            m = re.search(r"oom-kill:constraint=(\w+)", line)
            if not m:
                continue
            task = (re.search(r"[,\s]task=([^,]+)", line) or [None, "?"])[1]
            memcg = re.search(r"oom_memcg=([^,]+)", line)
            if m.group(1) == "CONSTRAINT_MEMCG" and memcg:
                who = memcg.group(1)
                cid = re.search(r"([0-9a-f]{64})", who)
                if cid and shutil.which("docker"):
                    rc2, name = run(["docker", "ps", "-a", "--no-trunc", "--filter", "id=" + cid.group(1),
                                     "--format", "{{.Names}} ({{.Image}})"], timeout=20)
                    who = (f"контейнер {name.strip()}" if rc2 == 0 and name.strip()
                           else f"контейнер {cid.group(1)[:12]} (уже видалений)")
                elif "kubepods" in who:
                    who = "под k8s, cgroup " + who
                limited.append((task, who))
            else:
                whole.append(task)
        if not whole and not limited:
            whole = ["(журнал ядра не показав, кого саме)"]
        return whole, limited

    def window(self, key, minutes):
        """Середнє за останні `minutes` хв, або None якщо вікно ще не набралось."""
        need = minutes * 60
        if not self.samples or self.samples[-1]["t"] - self.samples[0]["t"] < need * 0.9:
            return None
        vals = [s[key] for s in self.samples if s["t"] >= self.samples[-1]["t"] - need and s[key] is not None]
        return sum(vals) / len(vals) if vals else None

    @staticmethod
    def top_procs(sort):
        rc, out = run(["ps", "-eo", "pid,pcpu,rss,comm,args", "--sort=-" + sort, "--no-headers"])
        rows = []
        for line in out.splitlines()[:8]:
            p = line.split(None, 4)
            if len(p) >= 5:
                rows.append(f"  pid {p[0]:>7}  cpu {p[1]:>5}%  rss {int(p[2]) // 1024:>6} МБ  {p[4][:90]}")
        return "\n".join(rows)

    # ---------- server / cost / feasibility (п.4, п.5)

    def server_id(self):
        sid = self.cfg["server_id"] or self.state.get("server_id")
        if sid:
            return sid
        rc, out = run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
        mine = set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
        for s in self.api.get("/servers"):
            if s.get("ipv4") in mine:
                self.state["server_id"] = s["id"]
                self.save()
                return s["id"]
        raise ApiError(0, {"title": f"не знайшов цю VM серед серверів проєкту за IP {sorted(mine)}"})

    def monthly_cost(self):
        """Поточна місячна вартість VM, якщо вона не вимикається: остання повна доба з білінгу API
        (включно з IP, бекапами, додатковими дисками) * днів, плюс наші збільшення, яких у тій добі ще нема."""
        sid, H = self.server_id(), self.cfg["hours_per_month"]
        srv = self.api.get(f"/servers/{sid}")
        prices = self.api.get(f"/server-types/{srv['server_type_id']}/pricing")
        now = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        start = now - dt.timedelta(days=2)
        q = f"?server_id={sid}&granularity=daily&start={start:%Y-%m-%dT%H:%M:%SZ}&end={now:%Y-%m-%dT%H:%M:%SZ}"
        day, items = None, {}
        try:
            servers = self.api.get("/project/costs/servers" + q)["servers"]
            buckets = [b for b in (servers[0]["buckets"] if servers else []) if float(b["total"]) > 0]
            if buckets:
                day = buckets[-1]
                items = {i["sku"]: float(i["amount"]) / 24 * H for i in day["items"]}
        except (ApiError, KeyError, IndexError, ValueError) as e:
            log("costs ledger unavailable, using price list:", e)
        if day:
            end_ts = dt.datetime.fromisoformat(day["end_time"].replace("Z", "+00:00")).timestamp()
            cost = float(day["total"]) / 24 * H
            # збільшення, зроблені під час або після цієї доби, в ній ще не (повністю) враховані
            cost += sum(r["delta_eur"] for r in self.state["resizes"] if r["ts"] > end_ts - 86400)
        elif srv.get("plan_id") and any(p.get("id") == srv["plan_id"] for p in prices.get("plans") or []):
            cost = float(next(p["price"] for p in prices["plans"] if p["id"] == srv["plan_id"])) / 720 * H
        else:
            m = prices["metered"]
            cost = H * (srv["cores"] * m["maxcpu_on"]["hourly"] + srv["memory_mb"] / 1024 * m["maxmem_on"]["hourly"]
                        + srv["disk_gb"] * m["maxdisk"]["hourly"]
                        + len(srv.get("ips") or []) * m["ipv4_addresses"]["hourly"])
        return {"server": srv, "prices": prices, "cost": cost, "items": items}

    def delta_eur(self, prices, kind, amount):
        units = amount / 1024 if kind == "memory_mb" else amount
        return units * prices["metered"][PRICE_KEY[kind]]["hourly"] * self.cfg["hours_per_month"]

    def feasible(self, info, kind, new_value):
        """п.5: чи можливе збільшення взагалі. Повертає (ok, причина)."""
        srv, prices = info["server"], info["prices"]
        if prices.get("pricing_mode") != "sliders":
            return False, f"тип сервера «{srv.get('server_type')}» не дозволяє змінювати ресурси окремо (pricing_mode={prices.get('pricing_mode')})"
        lim = prices.get("limits", {})
        cap = {"cores": lim.get("cpu"), "memory_mb": (lim.get("ram_gb") or 0) * 1024 or None,
               "disk_gb": lim.get("disk_gb_ceph")}[kind]
        if cap and new_value > cap:
            return False, f"ліміт типу сервера: {kind} <= {cap}"
        if srv.get("status") != "running":
            return False, f"сервер у стані {srv.get('status')}"
        proj = self.api.get("/project")
        if proj.get("status") != "Active":
            return False, f"проєкт у стані {proj.get('status')}"
        # Порожній PATCH нічого не міняє: 422 invalid_request = токен має право запису і боргу нема;
        # 403 = токен лише для читання / бракує права "VM Scale"; 402 = борг або призупинення.
        status, js = self.api.call("PATCH", f"/servers/{srv['id']}", body={}, raw_status=True)
        if status != 422:
            return False, f"API не приймає зміну сервера: HTTP {status} {js.get('code')} {js.get('detail') or js.get('title') or ''}"
        return True, "ok"

    # ---------- quiet hour (п.10)

    def quiet_hour(self):
        if self.cfg["quiet_hour_utc"] is not None:
            return int(self.cfg["quiet_hour_utc"]), "задано в конфігу"
        c = self.state.get("quiet")
        if c and time.time() - c["ts"] < 86400:
            return c["hour"], c["why"]
        default = int(self.cfg["default_quiet_hour_utc"])
        hour, why = default, "типове значення (метрики недоступні)"
        try:
            sid = self.server_id()
            end = utcnow().replace(minute=0, second=0, microsecond=0)
            start = end - dt.timedelta(days=7)
            data = self.api.get(f"/servers/{sid}/metrics?type=cpu&step=3600&start={start:%Y-%m-%dT%H:%M:%SZ}"
                                f"&end={end:%Y-%m-%dT%H:%M:%SZ}")
            by_hour = {}
            for series in data["series"]:
                if series["name"] == "cpu_usage":
                    for p in series["points"]:
                        if p["value"] is not None:
                            by_hour.setdefault(int(p["time"][11:13]), []).append(p["value"])
            avg = {h: sum(v) / len(v) for h, v in by_hour.items()}
            # не перезавантажуватись під час щоденного бекапу і годину після нього
            banned = set()
            try:
                pol = self.api.get(f"/servers/{sid}/backup-policy")
                if pol.get("enabled"):
                    for t in pol.get("times") or []:
                        hh, mm = map(int, t.split(":"))
                        local = dt.datetime.now(ZoneInfo(pol.get("timezone") or "UTC")).replace(hour=hh, minute=mm)
                        for off in (-1, 0, 1):
                            banned.add((local.astimezone(dt.timezone.utc).hour + off) % 24)
            except Exception as e:
                log("backup policy unreadable:", e)
            ok = {h: v for h, v in avg.items() if h not in banned}
            if len(avg) == 24 and ok:
                best = min(ok, key=ok.get)
                if statistics.median(ok.values()) - ok[best] < 1.0 and default in ok:
                    hour, why = default, (f"навантаження за 7 днів рівне ({min(ok.values()):.1f}-{max(ok.values()):.1f}% CPU), "
                                          f"беру типову нічну годину; бекап о {sorted(banned)} UTC оминаю")
                else:
                    hour, why = best, f"мінімум CPU за 7 днів ({ok[best]:.1f}%), години бекапу {sorted(banned)} UTC оминаю"
        except Exception as e:
            log("quiet hour from metrics failed:", e)
        self.state["quiet"] = {"ts": time.time(), "hour": hour, "why": why}
        self.save()
        return hour, why

    # ---------- resize: plan (now) -> execute (quiet hour) -> verify (after reboot)

    def request_resize(self, kind, step, reason, details=""):
        """Перевіряє вартість/можливість/бюджет і ставить збільшення в чергу на тиху годину."""
        st = self.state
        if kind in st["pending"] or st["in_progress"]:
            return
        try:
            info = self.monthly_cost()
        except Exception as e:
            self.mail(f"Не можу перевірити збільшення: {KIND_UA[kind]}",
                      f"{reason}\n\nЗапит до Fiberax API не вдався: {e}\nСпробую ще раз пізніше.\n\n{details}",
                      key=f"apierr-{kind}")
            return
        if info["prices"].get("pricing_mode") != "sliders":
            self.mail(f"Не вистачає ресурсу: {KIND_UA[kind]} — потрібен ручний апгрейд",
                      f"{reason}\n\n{self.manual_upgrade_text(info)}\n{details}", key=f"manual-{kind}")
            return
        if st["halted"]:
            self.mail(f"Потрібно більше: {KIND_UA[kind]}, але автозбільшення ЗУПИНЕНО",
                      f"{reason}\n\nАвтоматичне додавання ресурсів зупинено через ліміт витрат "
                      f"({st.get('halt_reason', '')}).\nРозберись вручну, куди поділись ресурси, і після цього "
                      f"виконай:  vm-guardian resume\n\n{details}", key=f"halted-{kind}")
            return
        if self.cfg["stop_eur"] is None:
            self.mail(f"Потрібно більше: {KIND_UA[kind]}, але ліміт витрат не налаштовано",
                      f"{reason}\n\nЯ не додаю ресурси, доки не задано стелю витрат. Виконай на сервері:\n"
                      f"  vm-guardian setup\nВін покаже поточну вартість VM, ціни апгрейдів і спитає ліміт.\n\n{details}",
                      key=f"unconfigured-{kind}")
            return
        try:
            srv, cost = info["server"], info["cost"]
            new_value = srv[kind] + step
            delta = self.delta_eur(info["prices"], kind, step)
            ok, why = self.feasible(info, kind, new_value)
        except Exception as e:
            self.mail(f"Не можу перевірити збільшення: {KIND_UA[kind]}",
                      f"{reason}\n\nЗапит до Fiberax API не вдався: {e}\nСпробую ще раз пізніше.\n\n{details}",
                      key=f"apierr-{kind}")
            return
        ceiling = float(self.cfg["stop_eur"])
        warn = float(self.cfg["warn_eur"]) if self.cfg["warn_eur"] is not None else ceiling
        after = cost + delta
        cost_txt = (f"Місячні витрати на VM (якщо не вимикати, {self.cfg['hours_per_month']} год):\n"
                    f"  зараз:            {cost:.2f} EUR\n  після збільшення: {after:.2f} EUR (+{delta:.2f})\n"
                    + ("  складові зараз:   " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(info["items"].items())) + "\n"
                       if info["items"] else ""))
        change = f"{kind}: {srv[kind]} -> {new_value}"
        if not ok:
            self.mail(f"Збільшення неможливе: {KIND_UA[kind]}",
                      f"{reason}\n\nПотрібно {change}, але перевірка через API каже, що це неможливо:\n  {why}\n\n"
                      f"{cost_txt}\n{details}", key=f"infeasible-{kind}")
            return
        # п.8: жорстка стеля на загальні місячні витрати
        if after > ceiling + 1e-9:
            st["halted"] = True
            st["halt_reason"] = (f"витрати {cost:.2f} EUR/міс, після збільшення було б {after:.2f} - "
                                 f"це вище заданої стелі {ceiling:.2f} EUR")
            self.save()
            self.mail("СТОП: автоматичне додавання ресурсів припинено",
                      f"{reason}\n\nПотрібно було {change}, але я цього НЕ роблю.\n{st['halt_reason']}.\n\n{cost_txt}\n"
                      f"Автоматично ресурси більше не додаватимуться. Розберись руками, куди вони поділись.\n"
                      f"Зроблені мною збільшення:\n" + self.resize_history() +
                      f"\nЩоб знову дозволити автозбільшення, задай нову стелю:  vm-guardian setup\n\n{details}")
            return
        hour, why_hour = self.quiet_hour()
        st["pending"][kind] = {"step": step, "reason": reason, "ts": time.time(), "delta_eur": delta}
        self.save()
        warn_txt = (f"\n!!! УВАГА: після збільшення місячні витрати на VM перевищать {warn:.2f} EUR "
                    f"({after:.2f} EUR).\n" if after > warn else "")
        warn_txt += f"\nСтеля, після якої я зупинюсь: {ceiling:.2f} EUR/міс. {self.headroom(info['prices'], ceiling - after)}\n"
        self.mail(f"Заплановано збільшення: {change}" + (f" — витрати > {warn:.2f} EUR" if after > warn else ""),
                  f"{reason}\n\nЗбільшу {change} через Fiberax API. Перевірка API: збільшення можливе.\n"
                  f"Це перезавантаження VM, тому виконаю о {hour:02d}:00 UTC ({why_hour}).\n\n"
                  f"{cost_txt}{warn_txt}\n{details}")

    @staticmethod
    def manual_upgrade_text(info):
        """Текст для типів сервера, де ресурси не змінюються окремо (Solo - плани, GPU - пакети)."""
        srv, prices = info["server"], info["prices"]
        txt = (f"Сервер {srv['id']} {srv['name']} має тип «{srv.get('server_type')}» "
               f"(pricing_mode={prices.get('pricing_mode')}): ресурси тут не додаються окремо, тому автоматично "
               f"я нічого не збільшую. Апгрейд має виконати власник у кабінеті Fiberax.\n"
               f"Зараз: {srv['cores']} ядер, {srv['memory_mb'] // 1024} ГБ пам'яті, {srv['disk_gb']} ГБ диска, "
               f"~{info['cost']:.2f} EUR/міс.\n")
        plans = sorted(prices.get("plans") or [], key=lambda p: p.get("price", 0))
        if plans:
            txt += "Плани цього типу:\n" + "".join(
                f"  {'*' if p.get('id') == srv.get('plan_id') else ' '} {p.get('name')}: {p.get('cores')} ядер, "
                f"{p.get('memory_gb')} ГБ пам'яті, {p.get('disk_gb')} ГБ диска - {p.get('price')} EUR/міс\n"
                for p in plans) + "  (* - поточний план)\n"
        return txt

    def headroom(self, prices, room):
        """Скільки ще апгрейдів кожного виду влазить у `room` EUR."""
        c, m, d = (self.delta_eur(prices, k, v) for k, v in (("cores", 1), ("memory_mb", 1024), ("disk_gb", 1)))
        return (f"Запасу {max(room, 0):.2f} EUR - це ще {int(max(room, 0) // c)} ядер, "
                f"або {int(max(room, 0) // m)} ГБ пам'яті, або {int(max(room, 0) // d)} ГБ диска.")

    def setup(self, stop=None, warn=None, grow=None, mail_to=None):
        """Інтерактивно (або з --stop/--warn) задає стелю витрат, відштовхуючись від ціни цієї VM."""
        if mail_to is None and not self.cfg["mail_to"] and sys.stdin.isatty():
            mail_to = input("На яку адресу надсилати листи: ").strip()
        if mail_to:
            conf = json.load(open(CONFIG_PATH)) if os.path.exists(CONFIG_PATH) else {}
            conf["mail_to"] = self.cfg["mail_to"] = mail_to
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(conf, f, indent=2, ensure_ascii=False)
            os.chmod(CONFIG_PATH, 0o600)
        info = self.monthly_cost()
        srv, prices, cost = info["server"], info["prices"], info["cost"]
        if prices.get("pricing_mode") != "sliders":
            print(self.manual_upgrade_text(info))
            print("Стеля витрат тут не потрібна: коли ресурсів забракне, я надішлю листа на "
                  f"{self.cfg['mail_to']}, а апгрейд виконає власник. Логи й кеші чищу як завжди.")
            return
        steps = {k: self.delta_eur(prices, k, v) for k, v in (("cores", 1), ("memory_mb", 1024), ("disk_gb", 1))}
        print(f"Сервер {srv['id']} {srv['name']} ({srv.get('server_type')}): {srv['cores']} ядер, "
              f"{srv['memory_mb'] // 1024} ГБ пам'яті, {srv['disk_gb']} ГБ диска.")
        print(f"\nПоточна віртуалка коштує {cost:.2f} EUR/міс, якщо не вимикати ({self.cfg['hours_per_month']} год):")
        for k, v in sorted(info["items"].items()):
            print(f"    {k:<8} {v:6.2f}")
        print("\nЦіни наступних апгрейдів (за місяць):")
        for kind, label in (("cores", "+1 ядро CPU"), ("memory_mb", "+1 ГБ пам'яті"), ("disk_gb", "+1 ГБ диска")):
            ok, why = self.feasible(info, kind, srv[kind] + (1024 if kind == "memory_mb" else 1))
            print(f"    {label:<14} +{steps[kind]:.2f} EUR   {'можливо через API' if ok else 'НЕМОЖЛИВО: ' + why}")
        n, pct = self.cfg["suggest_cpu_steps"], self.cfg["suggest_pct"]
        by_cpu, by_pct = cost + n * steps["cores"], cost * (1 + pct / 100)
        suggested = float(-(-max(by_cpu, by_pct) // 1))     # вгору до цілого євро
        print(f"\nЯкий ліміт (стелю місячних витрат) ти хочеш? Після нього я припиню додавати ресурси.")
        print(f"    {n} апгрейдів CPU:           {by_cpu:.2f} EUR/міс")
        print(f"    +{pct}% від поточної ціни:   {by_pct:.2f} EUR/міс")
        print(f"    пропоную більше з двох:     {suggested:.0f} EUR/міс")
        if self.cfg["stop_eur"] is not None:
            print(f"    зараз налаштовано:          {float(self.cfg['stop_eur']):.2f} EUR/міс")

        def parse(txt, default):
            txt = txt.strip().replace(",", ".").replace("€", "").lower().replace("eur", "").strip()
            if not txt:
                return default
            if txt.endswith("%"):
                return cost * (1 + float(txt[:-1].lstrip("+")) / 100)
            return cost + float(txt[1:]) if txt.startswith("+") else float(txt)

        while stop is None:
            try:
                stop = parse(input(f"Стеля, EUR/міс (число, +N до поточної, або N%) [{suggested:.0f}]: "), suggested)
            except ValueError:
                print("  не зрозумів, введи число")
            except EOFError:
                sys.exit("\nСкасовано, нічого не змінено.")
        stop = parse(str(stop), suggested)
        if stop <= cost:
            print(f"  УВАГА: стеля {stop:.2f} не вища за поточну вартість {cost:.2f} - жодного апгрейду не буде, лише листи.")
        print("  " + self.headroom(prices, stop - cost))
        warn_default = round(min(stop, max(cost, (cost + stop) / 2)), 2)
        if warn is None and sys.stdin.isatty():
            try:
                warn = parse(input(f"З якої суми попереджати в листах, EUR/міс [{warn_default:.2f}]: "), warn_default)
            except (ValueError, EOFError):
                warn = warn_default
        warn = parse(str(warn), warn_default) if warn is not None else warn_default
        conf = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                conf = json.load(f)
        conf.pop("extra_eur", None)
        conf.update(baseline_eur=round(cost, 2), warn_eur=round(warn, 2), stop_eur=round(stop, 2))
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH + ".tmp", "w") as f:
            json.dump(conf, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(CONFIG_PATH + ".tmp", 0o600)
        os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)
        self.state.update(halted=False, halt_reason="")
        self.save()
        print(f"\nЗбережено в {CONFIG_PATH}: базова ціна {cost:.2f}, попередження від {warn:.2f}, стоп на {stop:.2f} EUR/міс.")
        self.setup_grow(grow)
        rc, out = run(["systemctl", "try-restart", "vm-guardian.service"])
        print("Демон перезапущено з новими лімітами." if rc == 0 else f"Перезапусти демон вручну: {out.strip()}")

    def resize_history(self):
        return "".join(f"  {dt.datetime.fromtimestamp(r['ts'], dt.timezone.utc):%Y-%m-%d %H:%M} UTC  "
                       f"{r['kind']} {r['old']} -> {r['new']}  (+{r['delta_eur']:.2f} EUR/міс)\n"
                       for r in self.state["resizes"][-20:]) or "  (ще не було)\n"

    def execute_pending(self):
        st = self.state
        if not st["pending"] or st["in_progress"] or st["halted"]:
            return
        hour, _ = self.quiet_hour()
        if utcnow().hour != hour:
            return
        if self.cfg["dry_run"]:
            log("dry_run: would apply", st["pending"])
            return
        done, sid = [], self.server_id()
        try:
            info = self.monthly_cost()
        except Exception as e:
            log("execute: API unavailable, retry next tick:", e)
            return
        srv = info["server"]
        for kind in ("disk_gb", "memory_mb", "cores"):      # один вид зміни на запит
            p = st["pending"].get(kind)
            if not p:
                continue
            new_value = srv[kind] + p["step"]
            try:
                ok, why = self.feasible(info, kind, new_value)
                if not ok:
                    raise ApiError(0, {"title": why})
                p.setdefault("idem", str(uuid.uuid4()))
                self.save()
                r = self.api.call("PATCH", f"/servers/{sid}", body={kind: new_value}, idem=p["idem"])
                self.api.wait_task(r.get("task"))
            except ApiError as e:
                if e.status in (409, 429, 500, 503) and time.time() - p["ts"] < 3 * 86400:
                    log(f"resize {kind} temporarily refused ({e}), will retry")
                    p.pop("idem", None)
                    continue
                del st["pending"][kind]
                self.save()
                self.mail(f"Збільшення НЕ вдалося: {kind} -> {new_value}", f"{p['reason']}\n\nFiberax API: {e}")
                continue
            del st["pending"][kind]
            st["added_eur"] += p["delta_eur"]
            rec = {"ts": time.time(), "kind": kind, "old": srv[kind], "new": new_value, "delta_eur": p["delta_eur"],
                   "reason": p["reason"]}
            st["resizes"].append(rec)
            done.append(rec)
            self.save()
            log(f"resized {kind}: {srv[kind]} -> {new_value}")
        if not done:
            return
        st["in_progress"] = {"changes": done, "reboots": 0, "ts": time.time()}
        self.save()
        self.finish_resize()

    def guest_sees(self, changes):
        """Чи бачить гостьова ОС нові ресурси (без/після перезавантаження)."""
        missing = []
        for c in changes:
            if c["kind"] == "cores" and os.cpu_count() < c["new"]:
                missing.append(f"ядер {os.cpu_count()} замість {c['new']}")
            if c["kind"] == "memory_mb" and self.meminfo()["MemTotal"] / MB < c["new"] * 0.92:
                missing.append(f"пам'яті {self.meminfo()['MemTotal'] // MB} МБ замість {c['new']}")
            if c["kind"] == "disk_gb":
                self.grow_fs()
                if self.disk()["total"] / GB < c["new"] * 0.95 - 1:
                    missing.append(f"ФС {self.disk()['total'] / GB:.1f} ГБ замість {c['new']}")
        return missing

    def grow_fs(self):
        rc, out = run(["findmnt", "-no", "SOURCE,FSTYPE", self.cfg["disk_mount"]])
        if rc or not out.split():
            return
        part, fstype = out.split()[:2]
        name = os.path.basename(part)
        try:
            with open(f"/sys/class/block/{name}/partition") as f:
                num = f.read().strip()
            parent = "/dev/" + os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
            run(["sh", "-c", f"echo 1 > /sys/class/block/{os.path.basename(parent)}/device/rescan"])
            log("growpart:", run(["growpart", parent, num])[1].strip())
        except OSError:
            pass                                            # ФС прямо на диску / LVM - growpart не потрібен
        cmd = ["xfs_growfs", self.cfg["disk_mount"]] if fstype == "xfs" else ["resize2fs", part]
        log(" ".join(cmd) + ":", run(cmd)[1].strip()[-200:])

    def finish_resize(self):
        """Після PATCH: перезавантажити, якщо гість ще не бачить ресурсів; після ребуту - перевірити й відзвітувати."""
        ip = self.state["in_progress"]
        missing = self.guest_sees(ip["changes"])
        pending_reboot = False
        try:
            pending_reboot = bool(self.api.get(f"/servers/{self.server_id()}").get("pending_reboot"))
        except Exception as e:
            log("cannot read pending_reboot:", e)
        if (missing or pending_reboot) and ip["reboots"] < 1:
            ip["reboots"] += 1
            self.save()
            log("rebooting to apply:", missing or "pending_reboot")
            try:
                self.api.call("POST", f"/servers/{self.server_id()}/actions/reboot", idem=str(uuid.uuid4()))
                time.sleep(180)                             # зазвичай нас уб'є shutdown раніше
            except ApiError as e:
                log("API reboot failed, rebooting locally:", e)
            if time.time() - float(open("/proc/uptime").read().split()[0]) < ip["ts"]:
                run(["systemctl", "reboot"])                # API не перезавантажив - робимо самі
                time.sleep(300)
            return
        lines = "".join(f"  {c['kind']}: {c['old']} -> {c['new']}  (+{c['delta_eur']:.2f} EUR/міс)\n    причина: {c['reason']}\n"
                        for c in ip["changes"])
        d, mi = self.disk(), self.meminfo()
        now_txt = (f"Зараз у системі: {os.cpu_count()} ядер, {mi['MemTotal'] // MB} МБ пам'яті, "
                   f"диск {d['total'] / GB:.1f} ГБ (зайнято {d['used_pct']:.0f}%).\n")
        try:
            cost = f"Місячні витрати тепер: {self.monthly_cost()['cost']:.2f} EUR.\n"
        except Exception:
            cost = ""
        if missing:
            self.mail("ПРОБЛЕМА: ресурси додано в Fiberax, але система їх не бачить",
                      f"Зміни:\n{lines}\nПісля перезавантаження не бачу: {'; '.join(missing)}.\n{now_txt}{cost}"
                      "Перевір VM у панелі Fiberax (можливо, потрібен повний stop/start).")
        else:
            self.mail("Ресурси збільшено" + (" (з перезавантаженням)" if ip["reboots"] else " (без перезавантаження)"),
                      f"Зміни:\n{lines}\n{now_txt}{cost}"
                      f"Стеля автозбільшення: {self.cfg['stop_eur']} EUR/міс; "
                      f"усього я додав {self.state['added_eur']:.2f} EUR/міс.")
        self.state["in_progress"] = None
        self.samples.clear()
        self.save()

    # ---------- disk diagnosis (п.2)

    def du_snapshot(self):
        """Розміри тек (du -x, без інших ФС) глибиною du_depth; зберігаємо історію для пошуку росту."""
        rc, out = run(["nice", "-n", "19", "ionice", "-c", "3", "du", "-xb", f"--max-depth={self.cfg['du_depth']}",
                       self.cfg["disk_mount"]], timeout=1800)
        sizes = {}
        for line in out.splitlines():
            p = line.split("\t", 1)
            if len(p) == 2 and p[0].isdigit() and int(p[0]) >= self.cfg["du_min_mb"] * MB:
                sizes[p[1]] = int(p[0])
        if sizes:
            self.state["du"] = [s for s in self.state["du"] if s["ts"] > time.time() - 8 * 86400][-40:]
            self.state["du"].append({"ts": time.time(), "sizes": sizes})
            self.save()
        return sizes

    def growers(self, sizes):
        """[(path, приріст, розмір)] - найглибші теки, де реально виріс обсяг відносно найстарішого знімка
        (до 8 днів). Без історії - найбільші теки за абсолютним розміром (приріст = None)."""
        old = next((s for s in self.state["du"] if s["ts"] < time.time() - 3600), None)
        root = self.cfg["disk_mount"]
        if old:
            base, floor = old["sizes"], self.cfg["growth_min_mb"] * MB
            maxd = max(p.count("/") for p in base)          # глибше старий знімок не міряв - там "росту" не видно
            grow = {p: s - base.get(p, 0) for p, s in sizes.items() if p.count("/") <= maxd}
            grow = {p: g for p, g in grow.items() if g >= floor and p != root}
            leaf = [p for p in grow if not any(c != p and c.startswith(p.rstrip("/") + "/") and grow[c] >= 0.7 * grow[p]
                                               for c in grow)]
            res = sorted(((p, grow[p], sizes[p]) for p in leaf), key=lambda x: -x[1])
            # прибрати батьків, чий приріст уже пояснено дітьми зі списку
            res = [r for r in res if not any(o[0].startswith(r[0].rstrip("/") + "/") for o in res)] or res
            return res[:10], old["ts"]
        leaf = [p for p in sizes if p != root and
                not any(c != p and c.startswith(p.rstrip("/") + "/") and sizes[c] >= 0.7 * sizes[p] for c in sizes)]
        res = sorted(((p, None, sizes[p]) for p in leaf), key=lambda x: -x[2])
        res = [r for r in res if not any(o[0].startswith(r[0].rstrip("/") + "/") for o in res)] or res
        return res[:10], None

    @staticmethod
    def path_size(path):
        rc, out = run(["du", "-sxb", path], timeout=600)
        return int(out.split()[0]) if rc == 0 and out.split() and out.split()[0].isdigit() else 0

    def is_growable(self, path):
        """Чи лежить тека в даних, під які власник дозволив автоматично докуповувати диск."""
        return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.rstrip("/") + "/*")
                   for g in self.cfg["grow_paths"])

    # --- хто що займає: софт виявляємо наново при кожній діагностиці, нічого не зашито

    def inventory(self):
        """Знімок того, що зараз стоїть на машині: контейнери з томами, PVC/поди k3s, процеси з відкритими файлами."""
        inv = {"mounts": [], "containers": {}, "pods": {}, "procs": [], "cmeta": [], "refs": [], "apps": []}
        for f in (glob.glob("/etc/systemd/system/*.service") + glob.glob("/etc/cron.d/*") + ["/etc/crontab"]
                  + glob.glob("/var/spool/cron/crontabs/*")):
            try:
                label = (f"systemd-сервіс {os.path.basename(f)}" if f.endswith(".service") else f"cron ({f})")
                inv["refs"].append((open(f, errors="replace").read(), label))
            except OSError:
                pass
        if shutil.which("docker"):
            rc, ids = run(["docker", "ps", "-aq"], timeout=30)
            if rc == 0 and ids.split():
                rc, out = run(["docker", "inspect"] + ids.split(), timeout=60)
                try:
                    for c in json.loads(out) if rc == 0 else []:
                        name, image = c["Name"].lstrip("/"), c["Config"]["Image"]
                        proj = (c["Config"].get("Labels") or {}).get("com.docker.compose.project")
                        desc = f"контейнер {name} ({image})" + (f", compose-проєкт {proj}" if proj else "")
                        inv["containers"][c["Id"]] = desc
                        meta = {"name": name, "image": image, "running": bool(c["State"].get("Running")), "mounts": []}
                        inv["cmeta"].append(meta)
                        for m in c.get("Mounts") or []:
                            if m.get("Source"):
                                inv["mounts"].append((m["Source"].rstrip("/"), f"{desc}, змонтовано в {m.get('Destination')}"))
                                meta["mounts"].append((m.get("Type"), m["Source"].rstrip("/")))
                except (ValueError, KeyError, TypeError) as e:
                    log("docker inspect parse failed:", e)
        if shutil.which("k3s") or shutil.which("kubectl"):
            kc = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
            rc, out = run(kc + ["get", "pods", "-A", "-o", "json"], timeout=30)
            try:
                for p in json.loads(out)["items"] if rc == 0 else []:
                    inv["pods"][p["metadata"]["uid"]] = f"под {p['metadata']['namespace']}/{p['metadata']['name']}"
            except (ValueError, KeyError):
                pass
        for pid in filter(str.isdigit, os.listdir("/proc")):    # лише процеси хоста: у контейнерів свої шляхи
            try:
                cg = open(f"/proc/{pid}/cgroup").read()
                if re.search(r"docker|kubepods|containerd|libpod|lxc", cg):
                    continue
                comm = open(f"/proc/{pid}/comm").read().strip()
                unit = re.search(r"/([^/]+\.service)", cg)
                desc = f"процес {comm}" + (f" ({unit.group(1)})" if unit else "")
                paths = {os.readlink(f"/proc/{pid}/cwd")}
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        paths.add(os.readlink(f"/proc/{pid}/fd/{fd}"))
                    except OSError:
                        pass
                inv["procs"] += [(p, desc) for p in paths if p.startswith("/") and not p.startswith(("/proc", "/dev", "/sys", "/run"))]
            except OSError:
                continue
        inv["apps"] = self.detect_apps(inv)
        return inv

    def detect_apps(self, inv):
        """Який відомий софт (профілі з apps.json) стоїть на машині. Розпізнаємо за ознаками на місці:
        файли, systemd-юніти, образи контейнерів, команди - а не за назвою образу, з якого зроблено сервер."""
        hits = []
        for a in self.apps:
            d, via = a.get("detect", {}), []
            for g in d.get("paths", []):
                found = glob.glob(g)
                if found:
                    via.append(found[0])
                    break
            for u in d.get("units", []):
                if any(os.path.exists(f"{b}/{u}") for b in ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system")):
                    via.append(u)
                    break
            conts = [c for c in inv["cmeta"] if any(re.search(rx, c["image"]) or re.search(rx, c["name"]) for rx in d.get("containers", []))]
            if conts:
                via.append("контейнер " + ", ".join(c["name"] for c in conts[:3]))
            for b in d.get("bins", []):
                if shutil.which(b):
                    via.append("команда " + b)
                    break
            if not via:
                continue
            data = {p.rstrip("/") for g in a.get("data", []) for p in glob.glob(g)}
            if a.get("container_data", True):
                for c in conts:
                    for mtype, src in c["mounts"]:
                        if mtype == "volume":
                            data.add(src[:-len("/_data")] if src.endswith("/_data") else src)
                        elif mtype == "bind" and os.path.isdir(src) and not src.startswith(
                                ("/etc", "/run", "/var/run", "/proc", "/sys", "/dev", "/usr", "/lib", "/boot", "/tmp")) and src != "/":
                            data.add(src)
            hits.append({"app": a, "via": via, "containers": conts, "data": sorted(data),
                         "backups": sorted({p.rstrip("/") for g in a.get("backups", []) for p in glob.glob(g)})})
        return hits

    @staticmethod
    def app_of(path, inv):
        """Профіль програми, до даних якої належить тека (або None)."""
        base = path.rstrip("/")
        for hit in inv["apps"]:
            if any(base == d or base.startswith(d + "/") or d.startswith(base + "/") for d in hit["data"] + hit["backups"]):
                return hit
        return None

    @staticmethod
    def cleanup_cmds(c, hit):
        """Розгортає запис cleanup із профілю в конкретні команди для цієї машини: [(назва, команда)]."""
        if c.get("bin") and not shutil.which(c["bin"]):
            return []
        if "sh" in c:
            return [(c["name"], ["sh", "-c", c["sh"]])]
        if "container" in c:
            return [(f"{c['name']} [{x['name']}]", [a.replace("{c}", x["name"]) for a in c["cmd"]])
                    for x in hit["containers"] if x["running"] and re.search(c["container"], x["image"])]
        if "foreach" in c:
            out, seen = [], set()
            for g in c["foreach"]:
                for p in glob.glob(g):
                    real = os.path.realpath(p)
                    if real in seen:
                        continue
                    seen.add(real)
                    cmd = [a.replace("{p}", real).replace("{dir}", os.path.dirname(real)) for a in c["cmd"]]
                    if c.get("as_owner"):
                        try:
                            cmd = ["runuser", "-u", pwd.getpwuid(os.stat(real).st_uid).pw_name, "--"] + cmd
                        except (KeyError, OSError):
                            continue
                    out.append((f"{c['name']} [{os.path.dirname(real)}]", cmd))
            return out
        return [(c["name"], c["cmd"])]

    @staticmethod
    def owner_of(path, inv):
        """Яка програма стоїть за текою - для листа."""
        base, who = path.rstrip("/"), []
        under = lambda p: p == base or p.startswith(base + "/")
        for src, desc in inv["mounts"]:
            if (under(src) or base.startswith(src + "/")) and desc not in who:
                who.append(desc)
        m = re.match(r"/var/lib/docker/containers/([0-9a-f]{64})", path)
        if m and m.group(1) in inv["containers"]:
            who.append(inv["containers"][m.group(1)] + " (його лог/службові файли)")
        m = re.search(r"/storage/pvc-[0-9a-f-]+_([^_/]+)_([^/]+)", path)
        if m:
            who.append(f"k8s том (PVC) {m.group(1)}/{m.group(2)}")
        m = re.match(r"/var/lib/kubelet/pods/([0-9a-f-]+)", path)
        if m:
            who.append(inv["pods"].get(m.group(1), f"k8s под {m.group(1)}"))
        procs = []
        for p, desc in inv["procs"]:
            if under(p) and desc not in procs:
                procs.append(desc)
        if procs:
            who.append("тримають тут відкриті файли: " + ", ".join(procs[:4]))
        if who:
            return "; ".join(who[:4])
        m = re.match(r"/var/lib/docker/volumes/([^/]+)", path)
        if m:
            return f"docker volume {m.group(1)}, жоден контейнер його зараз не використовує"
        for prefix, label in (("/var/lib/docker", "Docker (образи/шари контейнерів)"),
                              ("/var/lib/containerd", "containerd (образи/шари контейнерів)"),
                              ("/var/lib/rancher", "k3s (образи/дані кластера)"), ("/var/lib/kubelet", "k3s/kubelet"),
                              ("/var/log/journal", "systemd-journald"), ("/var/log", "системні логи"),
                              ("/var/cache", "системний кеш")):
            if path.startswith(prefix):
                return label
        if shutil.which("dpkg"):
            rc, out = run(["dpkg", "-S", path], timeout=20)
            pkgs = sorted({x.strip() for l in out.splitlines() if ": " in l and not l.startswith("dpkg")
                           for x in l.split(": ")[0].split(",")}) if rc == 0 else []
            if pkgs:
                return "пакет(и) " + ", ".join(pkgs[:5]) + (" ..." if len(pkgs) > 5 else "")
        m = re.match(r"/(root|home/[^/]+|opt|srv|var/www|usr/local|data)/([^/]+)", path)
        if not m:
            return "невідомо"
        refs = [label for text, label in inv["refs"] if re.search(re.escape(m.group(0)) + r"(/|\s|$)", text)]
        return f"тека {m.group(0)}; " + ("на неї посилаються: " + ", ".join(refs[:4]) if refs
                                           else "запущених програм чи сервісів, що нею користуються, не видно")

    # --- logs

    @staticmethod
    def logrotate_patterns():
        pats = []
        for f in ["/etc/logrotate.conf"] + sorted(glob.glob("/etc/logrotate.d/*")):
            try:
                text = open(f, errors="replace").read()
            except OSError:
                continue
            depth = 0
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if depth == 0 and line[:1] in ("/", '"'):
                    pats += [t.strip('"') for t in re.findall(r'"[^"]+"|[^\s{]+', line) if t.strip('"').startswith("/")]
                depth += line.count("{") - line.count("}")
        return pats

    @staticmethod
    def is_text_log(path):
        """Справжній текстовий лог, а не бінарний журнал транзакцій БД з розширенням .log."""
        try:
            with open(path, "rb") as f:
                chunk = f.read(8192)
        except OSError:
            return False
        if not chunk or b"\0" in chunk or b"\n" not in chunk:
            return False
        return sum(b >= 32 or b in (9, 10, 13) for b in chunk) / len(chunk) > 0.97

    def big_unrotated_logs(self, inv=None):
        """Великі текстові лог-файли, яких немає в жодному конфігу logrotate. Шукаємо в звичних місцях хоста,
        у логах контейнерів і неглибоко в томах контейнерів (теки з файлами БД оминаємо)."""
        big = self.cfg["log_big_mb"] * MB
        skip = {"node_modules", ".git", ".venv", "venv", "site-packages", "journal"}
        cand = set()

        def walk(root, max_depth, any_name):
            base_depth = root.rstrip("/").count("/")
            for dp, dns, fns in os.walk(root):
                if DB_MARKERS & set(fns) or DB_MARKERS & set(dns) or dp.count("/") - base_depth >= max_depth:
                    dns[:] = []
                    if DB_MARKERS & set(fns) or DB_MARKERS & set(dns):
                        continue
                dns[:] = [d for d in dns if d not in skip and not os.path.ismount(os.path.join(dp, d))]
                for fn in fns:
                    if re.search(r"\.(log|out)(\.\d+)?$", fn) or (
                            any_name and not re.search(r"\.(gz|xz|zst|bz2|journal)$|^(lastlog|wtmp|btmp|faillog)", fn)):
                        cand.add(os.path.join(dp, fn))

        for root in self.cfg["log_roots"]:
            for r in glob.glob(root):
                walk(r, 12, r.startswith("/var/log"))
        vols = set(glob.glob("/var/lib/docker/volumes/*/_data")) | set(glob.glob("/var/lib/rancher/k3s/storage/*"))
        vols |= {src for src, _ in (inv or {}).get("mounts", []) if os.path.isdir(src) and src.count("/") >= 2}
        for v in vols:
            walk(v, self.cfg["log_volume_depth"], False)
        for g in self.cfg["log_extra_globs"] + [g for hit in (inv or {}).get("apps", []) for g in hit["app"].get("logs", [])]:
            cand.update(glob.glob(g))
        found = []
        for p in cand:
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if os.path.isfile(p) and not os.path.islink(p) and st.st_blocks * 512 >= big and self.is_text_log(p):
                found.append((p, st.st_blocks * 512, st))
        if not found:
            return []
        covered = set()
        for pat in self.logrotate_patterns():
            covered.update(os.path.realpath(x) for x in glob.glob(pat))
        # "x.log.1" - це вже ротована копія "x.log": якщо сам лог у logrotate, копію не чіпаємо
        is_covered = lambda p: os.path.realpath(p) in covered or os.path.realpath(re.sub(r"\.\d+$", "", p)) in covered
        return sorted(((p, s, st) for p, s, st in found if not is_covered(p)), key=lambda x: -x[1])

    def add_logrotate(self, logs):
        """Додає знайдені логи в /etc/logrotate.d і одразу ротує. Повертає рядки звіту."""
        report, groups = [], {}
        for p, size, st in logs:
            if re.match(r"/var/lib/docker/containers/[^/]+/[^/]+-json\.log$", p):
                groups.setdefault(("docker-containers", "/var/lib/docker/containers/*/*-json.log"), []).append((p, size))
            else:
                slug = re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-")[-80:]
                groups[(slug, p)] = [(p, size)]
        for (slug, pattern), files in groups.items():
            conf = f"/etc/logrotate.d/vm-guardian-{slug}"
            listing = "; ".join(f"{p} ({fmt_size(s)})" for p, s in files)
            # su root root: шлях до тому контейнера доступний лише root, а без su logrotate відмовляється
            # працювати в теках, що належать не root. copytruncate зберігає власника самого лога.
            body = (f"# Додано автоматично vm-guardian {utcnow():%Y-%m-%d}: лог розрісся і не був у logrotate.\n"
                    f"# {listing}\n"
                    f'"{pattern}" {{\n    su root root\n    daily\n    size {self.cfg["logrotate_size"]}\n'
                    f'    rotate {self.cfg["logrotate_keep"]}\n    compress\n    missingok\n    notifempty\n    copytruncate\n}}\n')
            if self.cfg["dry_run"]:
                report.append(f"[dry-run] додав би в logrotate ({conf}): {listing}")
                continue
            with open(conf, "w") as f:
                f.write(body)
            os.chmod(conf, 0o644)
            rc, out = run(["logrotate", "-d", conf])
            if rc != 0 or "error:" in out:
                os.unlink(conf)
                err = " | ".join(l for l in out.splitlines() if "error" in l)[-300:] or out.strip()[-300:]
                report.append(f"НЕ вдалося додати в logrotate {pattern}: {err}")
                continue
            before = self.disk()["free"]
            rc, out = run(["logrotate", "-f", conf], timeout=1800)
            report.append(f"додано в logrotate ({conf}) і проротовано: {listing}; звільнено {fmt_size(self.disk()['free'] - before)}"
                          + (f"; logrotate: {out.strip()[-200:]}" if rc else ""))
        return report

    # --- caches

    @staticmethod
    def inside(path, parent):
        parent = parent.rstrip("/")
        return path == parent or path.startswith(parent + "/")

    def find_caches(self, inv=None):
        """Теки-кеші: з профілів відомого софту, за ознаками (назва cache/tmp/..., файл CACHEDIR.TAG) і з конфігу.
        Повертає {тека: (ознака, вік_у_днях)}. Сховища контейнерів, системні теки й дозволені дані за ознаками не чіпаємо."""
        found, known, age = {}, {}, self.cfg["cache_max_age_days"]
        for hit in (inv or {}).get("apps", []):
            for c in hit["app"].get("caches", []):
                for d in glob.glob(c["path"]):
                    if os.path.isdir(d) and not os.path.islink(d):
                        known[d.rstrip("/")] = (f"профіль {hit['app']['name']}", c.get("max_age_days", age))
        for g in self.cfg["cache_dirs"]:
            for d in glob.glob(g):
                if os.path.isdir(d) and not os.path.islink(d):
                    found[d] = ("з конфігу", age)
        root = self.cfg["disk_mount"]
        try:
            dev = os.stat(root).st_dev
        except OSError:
            return found
        protected = tuple(CACHE_PROTECTED) + tuple(self.cfg["cache_exclude"])

        def scan(d, depth):
            try:
                entries = [e for e in os.scandir(d) if e.is_dir(follow_symlinks=False)]
            except OSError:
                return
            for e in entries:
                p = e.path
                if p in found or p in known or p.startswith(protected) or self.is_growable(p):
                    continue
                try:
                    if e.stat(follow_symlinks=False).st_dev != dev:
                        continue
                except OSError:
                    continue
                if CACHE_NAME.search(e.name) and p != "/var/cache":     # /var/cache - по підтеках, там є винятки
                    found[p] = (f"назва «{e.name}»", age)
                elif os.path.exists(os.path.join(p, "CACHEDIR.TAG")):
                    found[p] = ("позначка CACHEDIR.TAG", age)
                elif depth < self.cfg["cache_search_depth"] and e.name not in ("node_modules", ".git", "site-packages"):
                    scan(p, depth + 1)

        scan(root, 1)
        # не чистити теку окремо, якщо вже чиститься її батьківська
        found = {p: v for p, v in found.items() if not any(p != o and p.startswith(o.rstrip("/") + "/") for o in found)}
        found.update(known)                                 # профільні теки чистимо за їхнім власним віком
        return found

    def clean_caches(self, inv):
        """Чистить старе з кешів. Повертає рядки звіту."""
        report, dry = [], self.cfg["dry_run"]

        def step(name, cmd=None, fn=None):
            if dry:
                report.append(f"[dry-run] {name}")
                return
            before = self.disk()["free"]
            out = fn() if fn else run(cmd, timeout=1800)[1]
            freed = self.disk()["free"] - before
            if freed > MB:
                report.append(f"{name}: звільнено {fmt_size(freed)}")
            log(f"cache cleanup '{name}': freed {fmt_size(freed)} {str(out or '').strip()[-150:]}")

        def dir_size(path):
            rc, out = run(["du", "-sxb", path])
            return int(out.split()[0]) if rc == 0 and out.split() and out.split()[0].isdigit() else 0

        # штатні команди очищення інструментів, які знайшлись на машині
        jmax = self.cfg["journal_max_mb"]
        if shutil.which("journalctl") and dir_size("/var/log/journal") > jmax * MB * 1.2:
            step(f"journald: обрізав до {jmax} МБ", ["journalctl", f"--vacuum-size={jmax}M"])
        if shutil.which("apt-get") and dir_size("/var/cache/apt/archives") > 100 * MB:
            step("apt: вичистив завантажені .deb", ["apt-get", "clean"])
        for tool in ("dnf", "yum"):
            if shutil.which(tool) and dir_size(f"/var/cache/{tool}") > 100 * MB:
                step(f"{tool}: вичистив завантажені пакети", [tool, "clean", "packages"])
                break
        if shutil.which("docker"):
            step("docker: build-кеш старший за 7 днів", ["docker", "builder", "prune", "-f", "--filter", "until=168h"])
            step("docker: образи без тегів (dangling)", ["docker", "image", "prune", "-f"])
        for hit in inv["apps"]:                             # штатні команди очищення відомого софту
            for c in hit["app"].get("cleanup", []):
                for name, cmd in self.cleanup_cmds(c, hit):
                    step(name, cmd)
        protected = tuple(CACHE_PROTECTED) + tuple(self.cfg["cache_exclude"])

        def purge(d, age):
            """Видаляє лише звичайні файли, яких давно не чіпали; теки, сокети, pid/lock-файли лишає."""
            n, cutoff = 0, time.time() - age * 86400
            for dp, dns, fns in os.walk(d):
                dns[:] = [x for x in dns if not os.path.ismount(os.path.join(dp, x))
                          and not any(self.inside(os.path.join(dp, x), pr) and not self.inside(d, pr) for pr in protected)]
                for fn in fns:
                    p = os.path.join(dp, fn)
                    try:
                        st = os.lstat(p)
                        if (stat.S_ISREG(st.st_mode) and max(st.st_mtime, st.st_atime) < cutoff
                                and not fn.endswith((".pid", ".lock", ".sock"))):
                            os.unlink(p)
                            n += 1
                    except OSError:
                        pass
            return f"{n} files"

        for d, (why, age) in sorted(self.find_caches(inv).items()):
            if dir_size(d) > self.cfg["cache_min_mb"] * MB:
                step(f"кеш {d} ({why}): видалив файли, яких не чіпали понад {age} днів", fn=lambda d=d, age=age: purge(d, age))
        return report

    def untouched_caches(self, sizes):
        """Великі теки, схожі на кеш, усередині сховищ контейнерів: сам не чищу, але показую власнику."""
        rows = [(p, s) for p, s in sizes.items()
                if p.startswith(("/var/lib/docker/volumes/", "/var/lib/rancher/k3s/storage/", "/var/lib/kubelet/pods/"))
                and CACHE_NAME.search(os.path.basename(p)) and s >= self.cfg["cache_min_mb"] * MB]
        return sorted(rows, key=lambda x: -x[1])[:5]

    def diagnose_disk(self):
        """п.2: чому не вистачає диска і що з цим робити."""
        cfg = self.cfg
        d0 = self.disk()
        head = (f"На {cfg['disk_mount']} зайнято {d0['used_pct']:.0f}% з {d0['total'] / GB:.1f} ГБ, вільно {fmt_size(d0['free'])} "
                f"(поріг: {cfg['disk_used_pct']}% або менше {cfg['disk_min_free_gb']} ГБ вільно).")
        inv = self.inventory()
        actions = []
        logs = self.big_unrotated_logs(inv)
        if logs:
            actions += self.add_logrotate(logs)
        if self.disk_low():
            actions += self.clean_caches(inv)
        sizes = self.du_snapshot()
        top, since = self.growers(sizes)
        d1 = self.disk()
        done = ("Що я зробив:\n" + "".join(f"  - {a}\n" for a in actions)) if actions else "Автоматично підчистити не було чого.\n"
        if since:
            where = (f"Де зросло споживання (з {dt.datetime.fromtimestamp(since, dt.timezone.utc):%Y-%m-%d %H:%M} UTC):\n" +
                     "".join(f"  +{fmt_size(g):>10}  (тепер {fmt_size(s)})  {p}\n      -> {self.owner_of(p, inv)}\n" for p, g, s in top))
        else:
            where = ("Історії розмірів ще немає, тому показую найбільших споживачів:\n" +
                     "".join(f"  {fmt_size(s):>10}  {p}\n      -> {self.owner_of(p, inv)}\n" for p, g, s in top))
        advised = []
        for p, g, s in top:
            hit = self.app_of(p, inv)
            if hit and hit not in advised and hit["app"].get("advice"):
                advised.append(hit)
        if advised:
            where += "\nЩо відомо про цей софт:\n" + "".join(f"  {h['app']['name']}: {h['app']['advice']}\n" for h in advised)
        baks = []
        for hit in inv["apps"]:
            for b in hit["backups"]:
                size = sizes.get(b) or self.path_size(b)
                if size >= cfg["cache_min_mb"] * MB:
                    baks.append((size, b, hit["app"]["name"]))
        if baks:
            where += ("\nВласні бекапи програм на цьому ж диску (сам не видаляю):\n" +
                      "".join(f"  {fmt_size(s):>10}  {b}  ({n})\n" for s, b, n in sorted(baks, reverse=True)))
        if inv["apps"]:
            where += "\nВідомий софт на машині: " + "; ".join(h["app"]["name"] for h in inv["apps"]) + ".\n"
        hints = self.untouched_caches(sizes)
        if hints:
            where += ("\nСхоже на кеш усередині томів контейнерів (сам не чіпаю, бо це дані програми):\n" +
                      "".join(f"  {fmt_size(s):>10}  {p}\n" for p, s in hints))
        after = f"Після цього: зайнято {d1['used_pct']:.0f}%, вільно {fmt_size(d1['free'])}.\n"
        details = f"{done}{after}\n{where or 'Помітного росту тек не знайшов.'}"
        if not self.disk_low(d1):
            if actions and not cfg["dry_run"]:
                self.mail("Диск закінчувався — підчистив сам", f"{head}\n\n{details}")   # звіт про зроблене не глушимо
            return details
        # місця все ще мало: чи це дані, під які власник дозволив докуповувати диск?
        allowed = [(p, g, s) for p, g, s in top if self.is_growable(p)]
        weight = lambda rows: sum((g if g is not None else s) for p, g, s in rows)
        if allowed and weight(allowed) >= 0.5 * weight(top):
            reason = (f"Диск: {head}\nВиросли дані, під які дозволено докуповувати диск: " +
                      ", ".join(f"{p} {'+' + fmt_size(g) if g is not None else fmt_size(s)}" for p, g, s in allowed))
            if cfg["dry_run"]:
                log("dry_run: would request +disk\n" + reason)
            else:
                self.request_resize("disk_gb", cfg["disk_step_gb"], reason, details)
        elif not cfg["dry_run"]:
            who = "\n".join(f"  {p}\n      -> {self.owner_of(p, inv)}" for p, g, s in top[:3]) or "  не визначив"
            hint = (f"\nЯкщо це нормальний ріст даних і під нього можна докуповувати диск автоматично:\n"
                    f"  vm-guardian allow-growth '{top[0][0]}'\n" if top else "")
            self.mail("Не вистачає диска: росте те, під що диск автоматично не докуповується",
                      f"{head}\n\nНайбільше росте:\n{who}\n\nДиск автоматично НЕ збільшую - цих даних немає в списку дозволених "
                      f"({', '.join(cfg['grow_paths']) or 'список порожній'}).{hint}\n{details}",
                      key="disk-other")
        return details

    def set_grow_paths(self, paths):
        conf = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                conf = json.load(f)
        conf["grow_paths"] = self.cfg["grow_paths"] = sorted(set(paths))
        with open(CONFIG_PATH + ".tmp", "w") as f:
            json.dump(conf, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(CONFIG_PATH + ".tmp", 0o600)
        os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)
        run(["systemctl", "try-restart", "vm-guardian.service"])

    def setup_grow(self, grow=None):
        """Питає власника, під які дані можна автоматично докуповувати диск. Кандидати - томи контейнерів."""
        inv = self.inventory()
        app_data = {d: hit["app"]["name"] for hit in inv["apps"] for d in hit["data"]}
        cands = set(glob.glob("/var/lib/docker/volumes/*/")) | set(glob.glob("/var/lib/rancher/k3s/storage/*/")) | set(app_data)
        rows = []
        for c in sorted({c.rstrip("/") for c in cands}):
            size = self.path_size(c)
            if size >= self.cfg["du_min_mb"] * MB or c in app_data:
                rows.append((size, c))
        rows.sort(key=lambda r: (r[1] not in app_data, -r[0]))
        if inv["apps"]:
            print("\nЗнайдений відомий софт: " + "; ".join(f"{h['app']['name']} ({h['via'][0]})" for h in inv["apps"]))
        cur = self.cfg["grow_paths"]
        print("\nПід які дані можна автоматично докуповувати диск, коли вони ростуть?")
        print("(Якщо росте щось інше, я лише надішлю листа з тим, що саме і де виросло.)")
        for i, (size, c) in enumerate(rows, 1):
            print(f"  {i:>2}. {fmt_size(size):>10}  {c}{'   [дозволено]' if self.is_growable(c) else ''}"
                  f"{'   <- дані ' + app_data[c] if c in app_data else ''}\n        {self.owner_of(c, inv)}")
        if not rows:
            print("  (томів контейнерів з даними не знайшов; шлях можна додати пізніше: vm-guardian allow-growth <тека>)")
        print(f"Зараз дозволено: {', '.join(cur) or 'нічого'}")
        if grow is None and sys.stdin.isatty():
            try:
                grow = input("Номери або шляхи через кому (Enter - лишити як є, 0 - нічого, a - усі дані відомого софту): ").strip()
            except EOFError:
                grow = ""
        if not grow:
            return
        paths = []
        if grow.lower() in ("a", "а"):
            grow = ",".join(app_data)
        for tok in [t.strip() for t in grow.split(",") if t.strip() and t.strip() != "0"]:
            paths.append(rows[int(tok) - 1][1] if tok.isdigit() and 0 < int(tok) <= len(rows) else tok.rstrip("/"))
        self.set_grow_paths(paths)
        print(f"Диск автоматично докуповується під: {', '.join(self.cfg['grow_paths']) or 'нічого'}")

    # ---------- checks / main loop

    def tick(self):
        cfg, st = self.cfg, self.state
        s = self.sample()
        now = time.time()
        if st["in_progress"]:
            self.finish_resize()
            return
        if now - (st["du"][-1]["ts"] if st["du"] else 0) > cfg["du_interval_h"] * 3600:
            self.du_snapshot()
        if self.disk_low() and now - st["last_diag"] > cfg["disk_diag_cooldown_h"] * 3600 and "disk_gb" not in st["pending"]:
            st["last_diag"] = now
            self.save()
            self.diagnose_disk()
        mem = self.window("mem_avail_pct", cfg["mem_window_min"])
        psi = self.window("mem_psi", cfg["mem_window_min"])
        why = None
        if s["oom"]:
            whole, limited = self.classify_oom(s["oom_since"])
            if limited and not cfg["dry_run"]:
                self.mail("Процес убито через ліміт пам'яті контейнера (пам'ять VM не додаю)",
                          "Ядро вбило процес, бо контейнер/сервіс уперся у ВЛАСНИЙ ліміт пам'яті, а не тому, що пам'ять "
                          "скінчилась на всій машині. Додавання пам'яті віртуалці тут не допоможе - треба підняти ліміт "
                          "самого контейнера або розібратись, чому він стільки їсть.\n\n" +
                          "".join(f"  процес {t} - {who}\n" for t, who in limited) +
                          f"\nНа машині зараз доступно {s['mem_avail_pct']:.0f}% пам'яті.", key="oom-memcg")
            if whole:
                why = "ядро вбило процес через брак пам'яті на всій машині (OOM kill): " + ", ".join(whole)
        if why is None and mem is not None and mem < cfg["mem_avail_pct"]:
            why = f"доступної пам'яті в середньому {mem:.1f}% за {cfg['mem_window_min']} хв (поріг {cfg['mem_avail_pct']}%)"
        if why is None and psi is not None and psi > cfg["mem_psi_pct"]:
            mi = self.meminfo()
            why = (f"процеси в середньому {psi:.1f}% часу за {cfg['mem_window_min']} хв чекали на пам'ять "
                   f"(тиск пам'яті PSI, поріг {cfg['mem_psi_pct']}%); доступно {s['mem_avail_pct']:.0f}%, "
                   f"своп зайнято {(mi['SwapTotal'] - mi['SwapFree']) // MB} з {mi['SwapTotal'] // MB} МБ")
        if why:
            self.request_resize("memory_mb", cfg["mem_step_mb"], "Пам'ять: " + why,
                                "Найбільші споживачі пам'яті:\n" + self.top_procs("rss"))
        cpu = self.window("cpu", cfg["cpu_window_min"])
        if cpu is not None and cpu > cfg["cpu_busy_pct"]:
            self.request_resize("cores", cfg["cpu_step"],
                                f"CPU: середнє завантаження {cpu:.0f}% за {cfg['cpu_window_min']} хв (поріг {cfg['cpu_busy_pct']}%), ядер {os.cpu_count()}",
                                "Найбільші споживачі CPU:\n" + self.top_procs("pcpu"))
        self.execute_pending()

    def loop(self):
        log(f"vm-guardian started; mail_to={self.cfg['mail_to']} dry_run={self.cfg['dry_run']}")
        while True:
            try:
                self.tick()
            except Exception as e:                          # демон не має падати через разову помилку
                log(f"tick error: {type(e).__name__}: {e}")
            time.sleep(self.cfg["interval_s"])

    def apps_report(self):
        """Який софт із маркетплейсу Fiberax покрито профілями і що з нього знайдено на цій машині."""
        covered = {n: a["name"] for a in self.apps for n in a.get("images", [])}
        try:
            srv = self.api.get(f"/servers/{self.server_id()}")
            imgs = self.api.get(f"/server-types/{srv['server_type_id']}/images")
            print(f"Образ цього сервера: {srv.get('image_name')}"
                  + (f" -> профіль «{covered[srv['image_name']]}»" if srv.get("image_name") in covered else " (чиста ОС або власний образ)"))
            market = sorted(i["name"] for i in imgs if i["type"] == "marketplace")
            missing = [n for n in market if n not in covered]
            print(f"Образів маркетплейсу в каталозі: {len(market)}, з профілем: {len(market) - len(missing)}"
                  + (f"; БЕЗ профілю: {', '.join(missing)}" if missing else ""))
        except Exception as e:
            print("Каталог образів з API недоступний:", e)
        hits = self.inventory()["apps"]
        print(f"\nПрофілів софту: {len(self.apps)}. Знайдено на цій машині: {len(hits)}")
        for h in hits:
            a = h["app"]
            print(f"  {a['name']}  (ознака: {'; '.join(h['via'])})")
            for d in h["data"]:
                print(f"      дані:   {d}{'   [диск докуповується]' if self.is_growable(d) else ''}")
            for b in h["backups"]:
                print(f"      бекапи: {b}")
            for c in a.get("cleanup", []):
                for name, cmd in self.cleanup_cmds(c, h):
                    print(f"      чищення: {' '.join(cmd)}")

    def status(self):
        d, mi, st = self.disk(), self.meminfo(), self.state
        print(f"диск {self.cfg['disk_mount']}: зайнято {d['used_pct']:.0f}% з {d['total'] / GB:.1f} ГБ, вільно {fmt_size(d['free'])}"
              f"  -> {'МАЛО' if self.disk_low(d) else 'ok'}")
        try:
            psi = open("/proc/pressure/memory").readline().strip()
        except OSError:
            psi = "PSI недоступний"
        print(f"тиск пам'яті: {psi}; своп зайнято {(mi['SwapTotal'] - mi['SwapFree']) // MB} з {mi['SwapTotal'] // MB} МБ")
        print(f"пам'ять: доступно {100 * mi['MemAvailable'] / mi['MemTotal']:.0f}% з {mi['MemTotal'] // MB} МБ; ядер {os.cpu_count()}, load {os.getloadavg()[0]:.2f}")
        try:
            info = self.monthly_cost()
            srv = info["server"]
            print(f"Fiberax: сервер {srv['id']} {srv['name']}: {srv['cores']} ядер, {srv['memory_mb']} МБ, {srv['disk_gb']} ГБ, {srv['status']}")
            print(f"місячні витрати (без вимкнень, {self.cfg['hours_per_month']} год): {info['cost']:.2f} EUR  "
                  + ", ".join(f"{k} {v:.2f}" for k, v in sorted(info["items"].items())))
            for kind, step in (("disk_gb", self.cfg["disk_step_gb"]), ("memory_mb", self.cfg["mem_step_mb"]), ("cores", self.cfg["cpu_step"])):
                ok, why = self.feasible(info, kind, srv[kind] + step)
                print(f"  +{step} {kind}: +{self.delta_eur(info['prices'], kind, step):.2f} EUR/міс, можливо через API: {'так' if ok else 'НІ - ' + why}")
            h, why = self.quiet_hour()
            print(f"тиха година для перезавантаження: {h:02d}:00 UTC ({why})")
        except Exception as e:
            print("Fiberax API недоступний:", e)
        if self.cfg["stop_eur"] is None:
            print("бюджет: стелю не задано - ресурси автоматично не додаються (для Cluster Server: vm-guardian setup)")
        else:
            print(f"бюджет: базова ціна {self.cfg['baseline_eur']} EUR, попередження від {self.cfg['warn_eur']} EUR, "
                  f"стеля {self.cfg['stop_eur']} EUR/міс (загальна сума), демон додав {st['added_eur']:.2f} EUR/міс; "
                  f"автозбільшення {'ЗУПИНЕНО: ' + st.get('halt_reason', '') if st['halted'] else 'дозволено'}")
        print("відомий софт:", "; ".join(h["app"]["name"] for h in self.inventory()["apps"]) or "не знайдено", "(докладніше: vm-guardian apps)")
        print("диск автоматично докуповується під:", ", ".join(self.cfg["grow_paths"]) or "нічого (vm-guardian allow-growth <тека>)")
        print("у черзі:", {k: v["reason"] for k, v in st["pending"].items()} or "нічого")
        print("історія збільшень:\n" + self.resize_history(), end="")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    g = Guardian()
    if cmd == "run":
        g.loop()
    elif cmd == "setup":
        a = sys.argv[2:]
        opt = lambda name: a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None
        g.setup(stop=opt("--stop"), warn=opt("--warn"), grow=opt("--grow"), mail_to=opt("--mail"))
    elif cmd == "allow-growth" and len(sys.argv) > 2:      # дозволити докуповувати диск під ці дані
        g.set_grow_paths(g.cfg["grow_paths"] + [a.rstrip("/") for a in sys.argv[2:]])
        print("Диск автоматично докуповується під:", ", ".join(g.cfg["grow_paths"]))
    elif cmd == "status":
        g.status()
    elif cmd == "apps":
        g.apps_report()
    elif cmd == "diagnose":                                 # показати діагностику диска, нічого не змінюючи
        g.cfg["dry_run"] = True
        logs = g.big_unrotated_logs(g.inventory())
        print("Великі логи поза logrotate:" if logs else "Великих логів поза logrotate немає.")
        for p, s, _ in logs:
            print(f"  {fmt_size(s):>10}  {p}")
        print("Теки-кеші:")
        for p, (why, age) in sorted(g.find_caches(g.inventory()).items()):
            print(f"  {p}  ({why}, файли старші за {age} днів)")
        g.cfg["disk_used_pct"] = 0                          # вдати, що диска мало
        print(g.diagnose_disk())
    elif cmd == "resume":
        g.state.update(halted=False, halt_reason="")
        g.save()
        print("Автозбільшення знову дозволено (якщо витрати все ще впираються в стелю - зупиниться знову).")
    elif cmd == "test-mail":
        ok = g.mail("тестовий лист", "Перевірка доставки пошти від vm-guardian.")
        print("sendmail прийняв лист" if ok else "sendmail НЕ прийняв лист")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
