#!/usr/bin/env python3
"""Запускається НА тестовому сервері (його потім видаляють): перевіряє vm_guardian на цьому образі
і збирає факти про розкладку софту. Друкує один JSON. Аргумент: назва образу з каталогу."""
import glob, json, os, platform, shutil, subprocess, sys, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
image = sys.argv[1] if len(sys.argv) > 1 else ""
out = {"image": image, "errors": []}


def sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, text=True, errors="replace")
        return p.stdout.strip()
    except Exception as e:
        return f"ERR {e}"


def step(name, fn):
    try:
        out[name] = fn()
    except Exception:
        out["errors"].append(f"{name}: {traceback.format_exc()[-600:]}")


out["os"] = sh(". /etc/os-release; echo $PRETTY_NAME")
out["python"] = platform.python_version()
out["tools"] = {t: bool(shutil.which(t)) for t in ("logrotate", "systemctl", "sendmail", "docker", "journalctl", "growpart",
                                                    "resize2fs", "xfs_growfs", "ionice", "runuser", "ps", "findmnt", "dpkg", "rpm", "wp")}
out["guest_agent"] = sh("systemctl is-active qemu-guest-agent")
out["selinux"] = sh("getenforce 2>/dev/null || echo absent")
out["rootfs"] = sh("findmnt -no SOURCE,FSTYPE /")
out["cloud_init"] = sh("cloud-init status 2>/dev/null | head -1")
# факти про розкладку - щоб звірити профіль з реальністю
out["facts"] = {
    "services": sh("systemctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}' | tr '\\n' ' '"),
    "containers": sh("docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null"),
    "volumes": sh("docker volume ls -q 2>/dev/null | tr '\\n' ' '"),
    "dirs": {d: sorted(os.listdir(d))[:40] for d in ("/var/www", "/opt", "/srv", "/www", "/home", "/root", "/var/opt", "/var/lib", "/etc/logrotate.d") if os.path.isdir(d)},
    "du_top": sh("du -xb --max-depth=4 / 2>/dev/null | sort -rn | head -40 | awk '{printf \"%.0fM %s\\n\", $1/1048576, $2}'", 600),
    "big_files": sh("find / -xdev -type f -size +20M 2>/dev/null | grep -v -E '^/(usr|boot|proc|sys)/' | head -40", 300),
    "log_files": sh("find / -xdev -type f \\( -name '*.log' -o -name '*.log.*' \\) -size +200k 2>/dev/null | grep -v '^/usr/' | head -60", 300),
    "listening": sh("ss -ltnpH 2>/dev/null | awk '{print $4, $6}' | head -40"),
}

import vm_guardian as vg                                    # noqa: E402
vg.CONFIG_PATH = "/nonexistent"
vg.log = lambda *a: None
g = vg.Guardian()
g.cfg.update(state_file="/root/vmg-test-state.json", dry_run=True, mail_to="")
out["profiles_loaded"] = len(g.apps)
inv = {}


def detect():
    global inv
    inv = g.inventory()
    res = []
    for h in inv["apps"]:
        a = h["app"]
        res.append({"id": a["id"], "via": h["via"], "data": h["data"], "backups": h["backups"],
                    "cleanup": [" ".join(c) for cl in a.get("cleanup", []) for _, c in g.cleanup_cmds(cl, h)],
                    "logs_matched": sorted({p for gl in a.get("logs", []) for p in glob.glob(gl)})[:20],
                    "caches_matched": sorted({p for c in a.get("caches", []) for p in glob.glob(c["path"])})[:20],
                    "globs_unmatched": {k: [gl for gl in a.get(k, []) if not glob.glob(gl)] for k in ("data", "logs")}})
    return res


step("detected", detect)
expected = [a["id"] for a in g.apps if image in a.get("images", [])]
out["expected_profile"] = expected
out["expected_detected"] = bool(expected) and expected[0] in [d["id"] for d in out.get("detected", [])]
g.cfg["log_big_mb"] = 1
step("unrotated_logs_over_1mb", lambda: [(p, s) for p, s, _ in g.big_unrotated_logs(inv)][:30])
step("caches", lambda: {p: list(v) for p, v in g.find_caches(inv).items()})
step("sample", lambda: g.sample())
step("disk", lambda: g.disk())


def install_logrotate():
    """Те саме, що робить install.sh: на чистих образах logrotate немає."""
    if shutil.which("logrotate"):
        return "вже є"
    for cmd in ("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq logrotate",
                "dnf install -y -q logrotate", "yum install -y -q logrotate"):
        if shutil.which(cmd.split()[0]):
            res = sh(cmd, timeout=600)
            return "встановлено" if shutil.which("logrotate") else f"НЕ встановлено: {str(res)[-300:]}"
    return "немає пакетного менеджера"


step("install_logrotate", install_logrotate)


def real_logrotate():
    """Справжня ротація підробленого лога: чи працює наш конфіг logrotate на цьому дистрибутиві."""
    os.makedirs("/opt/vmg-fakeapp/logs", exist_ok=True)
    with open("/opt/vmg-fakeapp/logs/app.log", "w") as f:
        f.write("2026-10-13 12:00:00 INFO fake log line for vm-guardian test\n" * 60000)
    g.cfg.update(dry_run=False, log_roots=["/opt/vmg-fakeapp"], log_extra_globs=[])
    logs = [l for l in g.big_unrotated_logs({"mounts": [], "apps": []}) if l[0].startswith("/opt/vmg-fakeapp")]
    rep = g.add_logrotate(logs)
    return {"report": rep, "after": sorted(os.listdir("/opt/vmg-fakeapp/logs")), "size_after": os.path.getsize("/opt/vmg-fakeapp/logs/app.log")}


step("real_logrotate", real_logrotate)


def real_cleanup():
    """Справжнє чищення кешів на одноразовій машині: штатні команди інструментів і профілів."""
    g.cfg.update(dry_run=False)
    before = g.disk()["free"]
    rep = g.clean_caches(inv)
    return {"report": rep, "freed_mb": round((g.disk()["free"] - before) / 1048576)}


step("real_cleanup", real_cleanup)
step("diagnose_dry", lambda: (g.cfg.update(dry_run=True, disk_used_pct=0), g.diagnose_disk())[1][-3000:])
step("grow_fs", lambda: (g.grow_fs(), g.disk()["total"])[1])
print(json.dumps(out, ensure_ascii=False))
