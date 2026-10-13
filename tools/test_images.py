#!/usr/bin/env python3
"""Розгортає сервери з образів каталогу Fiberax, перевіряє на кожному vm_guardian і видаляє їх.

УВАГА: створює ПЛАТНІ сервери й публічні адреси в проєкті токена. Усі сервери мають префікс vmg-test-
і видаляються наприкінці (разом з адресами). Скрипт видаляє ЛИШЕ те, що створив сам: ідентифікатори
створених серверів записуються в <out>/created.json, і видалення можливе тільки для сервера, який є в цьому
файлі та досі має назву з префіксом vmg-test-.

  test_images.py --token FILE --out DIR [--only os|marketplace] [--images "A,B"] [--parallel 5]
                 [--resize "Debian 13,AlmaLinux 9"] [--keep] [--cleanup]
"""
import argparse, json, os, re, secrets, string, subprocess, sys, threading, time, urllib.error, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.fiberax.com/v1"
PREFIX = "vmg-test-"
lock = threading.Lock()


def say(*a):
    with lock:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


class Api:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, ok=(200, 201, 202)):
        url = (API if not path.startswith("/v1/") else API[:-3]) + path
        data = json.dumps(body).encode() if body is not None else None
        hdr = {"Authorization": "Bearer " + self.token, "User-Agent": "vm-guardian-test/1.0", "Accept": "application/json"}
        if data:
            hdr["Content-Type"] = "application/json"
        if method != "GET":
            hdr["Idempotency-Key"] = str(uuid.uuid4())
        for attempt in range(6):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=hdr), timeout=120) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                payload = e.read()
                if e.code in (429, 503) and attempt < 5:
                    time.sleep(int(e.headers.get("Retry-After") or 10) + 1)
                    continue
                raise RuntimeError(f"{method} {path}: HTTP {e.code} {payload[:400].decode(errors='replace')}")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 5 and method == "GET":
                    time.sleep(10)
                    continue
                raise RuntimeError(f"{method} {path}: {e}")

    def wait(self, resp, timeout=3600):
        task = resp.get("task")
        end = time.time() + timeout
        while task and not task.get("is_terminal"):
            if time.time() > end:
                raise RuntimeError(f"task {task['id']} timeout")
            time.sleep(15)
            task = self.call("GET", task["poll"])["data"]
        if task and task["status"] != "succeeded":
            raise RuntimeError(f"task failed: {task.get('error')}")
        return resp


def password():
    alpha = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alpha) for _ in range(20))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def setup_values(img, slug, pw):
    vals = {}
    for f in img.get("setup_fields") or []:
        k = f["key"]
        if "domain" in k:
            vals[k] = f"{slug}.vmg-test.example.com"
        elif "email" in k:
            vals[k] = "vmg-test@example.com"
        elif f.get("type") == "password" or "password" in k:
            vals[k] = pw
        elif "login" in k:
            vals[k] = "vmgadmin"
        else:
            vals[k] = "vmg test"
    return vals


class Ssh:
    def __init__(self, ip, user, key, pw):
        self.ip, self.user, self.key, self.pw = ip, user, key, pw
        self.opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
                     "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=15"]
        self.mode = None

    def _base(self, prog):
        if self.mode == "key":
            return [prog, "-i", self.key, "-o", "BatchMode=yes"] + self.opts
        return ["sshpass", "-p", self.pw, prog, "-o", "PubkeyAuthentication=no"] + self.opts

    def run(self, cmd, timeout=900):
        p = subprocess.run(self._base("ssh") + [f"{self.user}@{self.ip}", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, text=True, errors="replace")
        return p.returncode, p.stdout

    def put(self, files, dest):
        p = subprocess.run(self._base("scp") + files + [f"{self.user}@{self.ip}:{dest}"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=300, text=True)
        if p.returncode:
            raise RuntimeError("scp: " + p.stdout[-300:])

    def wait(self, timeout=600):
        end, last = time.time() + timeout, ""
        while time.time() < end:
            for mode in ("key", "password"):
                self.mode = mode
                try:
                    rc, out = self.run("echo ok", timeout=30)
                except subprocess.TimeoutExpired:
                    rc, out = 1, "timeout"
                if rc == 0 and "ok" in out:
                    return mode
                last = out.strip()[-200:]
            time.sleep(10)
        raise RuntimeError(f"ssh недоступний: {last}")


def test_image(api, img, args, key_id, result):
    slug = re.sub(r"[^a-z0-9]+", "-", img["name"].lower()).strip("-")
    name, pw, sid = PREFIX + slug, password(), None
    rec = result[img["name"]] = {"image": img["name"], "type": img["type"], "name": name, "steps": {}, "ok": False}
    t0 = time.time()
    try:
        r = img["recommended"]
        body = {"name": name, "server_type_id": args.server_type, "image": img["id"], "cores": r["cores"],
                "memory_mb": r["memory_gb"] * 1024, "disk_gb": r["disk_gb"], "public_ipv4": {"new": True}, "password": pw}
        if key_id:
            body["ssh_keys"] = [key_id]
        setup = setup_values(img, slug, pw)
        if setup:
            body["setup"] = setup
        resp = api.call("POST", "/servers", body)
        sid = rec["server_id"] = resp["data"]["id"]
        remember(args, sid, name)
        say(f"[{img['name']}] створено сервер {sid}, чекаю розгортання")
        api.wait(resp)
        srv = api.call("GET", f"/servers/{sid}")["data"]
        if srv["status"] != "running":
            api.wait(api.call("POST", f"/servers/{sid}/actions/power-on"))
            srv = api.call("GET", f"/servers/{sid}")["data"]
        rec["steps"]["create_s"] = round(time.time() - t0)
        ip = rec["ip"] = srv["ipv4"]
        ssh = Ssh(ip, img.get("admin_user") or "root", args.key, pw)
        rec["ssh_mode"] = ssh.wait()
        rc, out = ssh.run("command -v cloud-init >/dev/null && timeout 900 cloud-init status --wait | tail -1; "
                          "command -v python3 || echo NO-PYTHON", timeout=960)
        rec["steps"]["boot"] = out.strip()[-200:]
        if "NO-PYTHON" in out:
            raise RuntimeError("на образі немає python3")
        time.sleep(args.settle)                             # дати маркетплейс-установнику закінчити
        ssh.run("mkdir -p /opt/vm-guardian-test")
        ssh.put([os.path.join(HERE, "..", "vm_guardian.py"), os.path.join(HERE, "..", "apps.json"),
                 os.path.join(HERE, "remote_probe.py")], "/opt/vm-guardian-test/")
        rc, out = ssh.run(f"cd /opt/vm-guardian-test && python3 remote_probe.py '{img['name']}'", timeout=1500)
        try:
            rec["probe"] = json.loads(out[out.index('{"image"'):])
        except ValueError:
            raise RuntimeError("probe не віддав JSON: " + out[-800:])
        if img["name"] in args.resize:
            rec["resize"] = resize_test(api, ssh, sid, args)
        rec["ok"] = not rec["probe"]["errors"]
        say(f"[{img['name']}] готово за {round(time.time() - t0)} c: ok={rec['ok']} "
            f"detected={[d['id'] for d in rec['probe'].get('detected', [])]} expected={rec['probe'].get('expected_profile')}")
    except Exception as e:
        rec["error"] = str(e)[-1500:]
        say(f"[{img['name']}] ПОМИЛКА: {rec['error'][:300]}")
    finally:
        rec["total_s"] = round(time.time() - t0)
        with open(os.path.join(args.out, slug + ".json"), "w") as f:
            json.dump(rec, f, indent=1, ensure_ascii=False)
        if sid and not args.keep:
            delete_server(api, sid, name, args)


def resize_test(api, ssh, sid, args):
    """Справжнє збільшення через API (токен лишається на машині, що тестує) і перевірка в гості:
    чи видно нові ресурси без перезавантаження, чи спрацьовує розширення ФС, чи допомагає перезавантаження через API."""
    guest = ("python3 -c \"import sys,os,json; sys.path.insert(0,'/opt/vm-guardian-test'); import vm_guardian as vg; "
             "vg.CONFIG_PATH='/nonexistent'; vg.log=lambda *a: None; g=vg.Guardian(); g.grow_fs(); "
             "print(json.dumps({'cores': os.cpu_count(), 'mem_mb': g.meminfo()['MemTotal']//1048576, "
             "'fs_gb': round(g.disk()['total']/2**30, 2), 'uptime_s': float(open('/proc/uptime').read().split()[0])}))\"")

    def view():
        rc, out = ssh.run(guest, timeout=300)
        try:
            return json.loads(out[out.rindex("{"):])
        except ValueError:
            return {"error": out[-300:]}

    def server():
        d = api.call("GET", f"/servers/{sid}")["data"]
        return {k: d[k] for k in ("cores", "memory_mb", "disk_gb", "pending_reboot", "status")}

    res = {"before_api": server(), "before_guest": view(), "steps": []}
    for kind, step in (("disk_gb", 1), ("memory_mb", 1024), ("cores", 1)):
        t = time.time()
        try:
            api.wait(api.call("PATCH", f"/servers/{sid}", {kind: res["before_api"][kind] + step}), timeout=900)
            res["steps"].append({"change": f"{kind} +{step}", "task_s": round(time.time() - t), "api": server(), "guest_without_reboot": view()})
        except RuntimeError as e:
            res["steps"].append({"change": f"{kind} +{step}", "error": str(e)[-400:]})
    t = time.time()
    try:
        api.wait(api.call("POST", f"/servers/{sid}/actions/reboot"), timeout=600)
        time.sleep(30)
        ssh.wait(900)
        res["reboot_s"] = round(time.time() - t)
    except RuntimeError as e:
        res["reboot_error"] = str(e)[-400:]
    res["after_reboot_api"], res["after_reboot_guest"] = server(), view()
    return res


def created_file(args):
    return os.path.join(args.out, "created.json")


def load_created(args):
    try:
        with open(created_file(args)) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def remember(args, sid, name):
    """Записує створений сервер на диск ДО будь-яких подальших дій - лише такі сервери можна видаляти."""
    with lock:
        args.created.add(sid)
        d = load_created(args)
        d[sid] = name
        with open(created_file(args), "w") as f:
            json.dump(d, f, indent=1)


def delete_server(api, sid, name, args):
    if sid not in load_created(args):
        say(f"НЕ видаляю {sid}: цей сервер створено не цим тестом (його немає в {created_file(args)})")
        return
    for attempt in range(40):
        try:
            srv = api.call("GET", f"/servers/{sid}")["data"]
            if not srv["name"].startswith(PREFIX):
                say(f"НЕ видаляю {sid}: назва {srv['name']} не з префіксом {PREFIX}")
                return
            api.wait(api.call("DELETE", f"/servers/{sid}?release_ips=true"), timeout=900)
            with lock:
                args.created.discard(sid)
            say(f"[{name}] сервер {sid} видалено")
            return
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                with lock:
                    args.created.discard(sid)
                return
            time.sleep(30)                                  # ще створюється / зайнятий
    say(f"УВАГА: сервер {sid} ({name}) НЕ видалено - видали вручну")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", dest="token_file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", help="приватний SSH-ключ; публічний (.pub) тимчасово додається в акаунт. Без нього створюється одноразовий")
    ap.add_argument("--only", choices=["os", "marketplace"])
    ap.add_argument("--images", default="")
    ap.add_argument("--resize", default="")
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--settle", type=int, default=60)
    ap.add_argument("--server-type", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--cleanup", action="store_true", help="лише видалити сервери, записані в <out>/created.json")
    args = ap.parse_args()
    args.created, args.resize = set(), [x.strip() for x in args.resize.split(",") if x.strip()]
    api = Api(open(args.token_file).read().strip())
    os.makedirs(args.out, exist_ok=True)
    if args.cleanup:
        mine = load_created(args)
        for s in api.call("GET", "/servers")["data"]:
            if s["id"] in mine:
                delete_server(api, s["id"], s["name"], args)
            elif s["name"].startswith(PREFIX):
                say(f"сервер {s['id']} {s['name']} має тестовий префікс, але створений не з цієї теки результатів - не чіпаю")
        for k in api.call("GET", "/ssh-keys")["data"]:
            if k["name"].startswith(PREFIX):
                api.call("DELETE", f"/ssh-keys/{k['id']}")
                say("видалено ключ", k["name"])
        return
    imgs = [i for i in api.call("GET", f"/server-types/{args.server_type}/images")["data"]
            if i["type"] in ("os", "marketplace") and i["category"] != "windows"]
    if args.only:
        imgs = [i for i in imgs if i["type"] == args.only]
    if args.images:
        want = [x.strip() for x in args.images.split(",")]
        imgs = [i for i in imgs if i["name"] in want]
    key_id = None
    if not args.key:                                        # одноразовий ключ для цього прогону
        args.key = os.path.join(args.out, "ssh_key")
        if not os.path.exists(args.key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vmg-test", "-f", args.key], check=True)
    if args.key:
        pub = open(args.key + ".pub").read().strip()
        key_id = api.call("POST", "/ssh-keys", {"name": PREFIX + time.strftime("%Y%m%d-%H%M%S"), "public_key": pub})["data"]["id"]
    say(f"образів до перевірки: {len(imgs)}; паралельно {args.parallel}")
    result, sem, threads = {}, threading.Semaphore(args.parallel), []

    def worker(img):
        with sem:
            test_image(api, img, args, key_id, result)

    try:
        for img in imgs:
            t = threading.Thread(target=worker, args=(img,))
            t.start()
            threads.append(t)
            time.sleep(4)                                   # не впиратись у ліміт записів API
        for t in threads:
            t.join()
    finally:
        if key_id:
            try:
                api.call("DELETE", f"/ssh-keys/{key_id}")
            except RuntimeError as e:
                say("ключ не видалено:", e)
        with open(os.path.join(args.out, "summary.json"), "w") as f:
            json.dump(result, f, indent=1, ensure_ascii=False)
        if args.created and not args.keep:
            say("УВАГА: лишились сервери", sorted(args.created))
    bad = [n for n, r in result.items() if not r["ok"]]
    say(f"перевірено {len(result)}, з помилками {len(bad)}: {bad}")


if __name__ == "__main__":
    main()
