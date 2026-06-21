#!/usr/bin/env python3
# iCloud Shared Album Downloader - GUI
import os, sys, json, glob, threading, queue
import urllib.request, urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

THREADS = 8
DEFAULT_HOST = "p01-sharedstreams.icloud.com"


def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {}


def get_stream(token):
    base = f"https://{DEFAULT_HOST}/{token}/sharedstreams"
    _, data = post_json(f"{base}/webstream", {"streamCtag": None})
    host = data.get("X-Apple-MMe-Host")
    if host:
        base = f"https://{host}/{token}/sharedstreams"
        _, data = post_json(f"{base}/webstream", {"streamCtag": None})
    return base, data


def guess_ext(b):
    if len(b) < 12:
        return "bin"
    if b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF:
        return "jpg"
    if b[0] == 0x89 and b[1] == 0x50:
        return "png"
    if b[0] == 0x47 and b[1] == 0x49 and b[2] == 0x46:
        return "gif"
    if b[4:8] == b"ftyp":
        brand = b[8:12].decode("ascii", "replace")
        if brand[:2] == "qt":
            return "mov"
        if brand[:4] in ("heic", "heix", "hevc", "mif1", "heim", "heis"):
            return "heic"
        return "mp4"
    return "bin"


def sanitize(name):
    for c in '\\/:*?"<>|':
        name = name.replace(c, "_")
    name = name.strip().strip(".")
    return name or "icloud_album"


def download_one(idx, url, outdir):
    stem = f"{idx:04d}"
    for f in glob.glob(os.path.join(outdir, stem + ".*")):
        return ("skip", stem, None)
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
    except Exception as e:
        return ("err", stem, str(e))
    ext = guess_ext(data)
    with open(os.path.join(outdir, f"{stem}.{ext}"), "wb") as fh:
        fh.write(data)
    return ("get", f"{stem}.{ext}", None)


def run_download(url, folder, log, set_progress):
    token = url.split("#")[-1].strip()
    if not token:
        log("Неверная ссылка.")
        return
    log("Получаю данные альбома...")
    base, stream = get_stream(token)
    photos = stream.get("photos", [])
    if not photos:
        log("Альбом пуст или ссылка не публичная.")
        return
    album = (stream.get("streamName") or "").strip()
    if album:
        log(f"Альбом: «{album}»")
    folder = folder.strip()
    if not folder:
        folder = sanitize(album) if album else "icloud_album"
        log(f"Папка по названию альбома: {folder}")
    if not os.path.isabs(folder):
        folder = os.path.join(base_dir(), folder)
    os.makedirs(folder, exist_ok=True)

    sizemap = {}
    for p in photos:
        ders = list(p.get("derivatives", {}).values())
        ders = [d for d in ders if d.get("checksum")]
        if not ders:
            continue
        best = max(ders, key=lambda d: int(d.get("fileSize", 0) or 0))
        sizemap[best["checksum"]] = int(best.get("fileSize", 0) or 0)

    guids = [p["photoGuid"] for p in photos]
    _, assets = post_json(f"{base}/webasseturls", {"photoGuids": guids})
    items = assets.get("items", {})

    urls, sizes = [], []
    for checksum, info in items.items():
        if checksum not in sizemap:
            continue
        urls.append("https://" + info["url_location"] + info["url_path"])
        sizes.append(sizemap[checksum])

    total = len(urls)
    log(f"Файлов в альбоме: {total}. Качаю в {THREADS} потоков...")
    got = skip = err = 0
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futs = {ex.submit(download_one, i + 1, urls[i], folder): i + 1
                for i in range(total)}
        done = 0
        for fut in as_completed(futs):
            status, name, msg = fut.result()
            done += 1
            if status == "get":
                got += 1
            elif status == "skip":
                skip += 1
            else:
                err += 1
                log(f"  ошибка {name}: {msg}")
            set_progress(done, total)
    log(f"Скачано: {got}, пропущено: {skip}, ошибок: {err}")

    log("Проверка...")
    missing, corrupt, fixed = [], [], 0
    for i in range(1, total + 1):
        stem = f"{i:04d}"
        matches = glob.glob(os.path.join(folder, stem + ".*"))
        if not matches:
            missing.append(stem)
            continue
        fp = matches[0]
        exp = sizes[i - 1]
        if exp > 0 and os.path.getsize(fp) < exp:
            os.remove(fp)
            corrupt.append(stem)
            continue
        with open(fp, "rb") as fh:
            head = fh.read(16)
        ext = guess_ext(head)
        cur = os.path.splitext(fp)[1].lstrip(".").lower()
        if ext != "bin" and cur != ext:
            os.replace(fp, os.path.join(folder, stem + "." + ext))
            fixed += 1
    log(f"Ожидалось: {total} | целых: {total - len(missing) - len(corrupt)} | переименовано: {fixed}")
    if missing:
        log(f"Не хватает ({len(missing)}): {', '.join(missing)}")
    if corrupt:
        log(f"Битые, удалены ({len(corrupt)}): {', '.join(corrupt)}")
    if not missing and not corrupt:
        log(f"Готово, всё на месте и целое.\nПапка: {folder}")
    else:
        log("Нажми «Скачать» ещё раз — добьёт недостающее.")


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        root.title("iCloud Shared Album Downloader")
        root.geometry("580x440")
        root.minsize(480, 380)

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Ссылка на общий альбом iCloud:").pack(anchor="w")
        self.url = ttk.Entry(frm)
        self.url.pack(fill="x", pady=(2, 10))

        ttk.Label(frm, text="Папка (пусто = по названию альбома):").pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(2, 10))
        self.folder = ttk.Entry(row)
        self.folder.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор", width=8, command=self.pick).pack(side="left", padx=(6, 0))

        self.btn = ttk.Button(frm, text="Скачать", command=self.start)
        self.btn.pack(pady=4)

        self.pb = ttk.Progressbar(frm, mode="determinate")
        self.pb.pack(fill="x", pady=6)

        self.log = tk.Text(frm, height=12, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)

        self.poll()

    def pick(self):
        d = filedialog.askdirectory()
        if d:
            self.folder.delete(0, "end")
            self.folder.insert(0, d)

    def logmsg(self, m):
        self.q.put(("log", m))

    def setprog(self, d, t):
        self.q.put(("prog", (d, t)))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log["state"] = "normal"
                    self.log.insert("end", val + "\n")
                    self.log.see("end")
                    self.log["state"] = "disabled"
                elif kind == "prog":
                    d, t = val
                    self.pb["maximum"] = max(t, 1)
                    self.pb["value"] = d
                elif kind == "done":
                    self.btn["state"] = "normal"
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def start(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("iCloud", "Вставь ссылку на альбом")
            return
        self.btn["state"] = "disabled"
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"
        self.pb["value"] = 0
        folder = self.folder.get()
        threading.Thread(target=self._run, args=(url, folder), daemon=True).start()

    def _run(self, url, folder):
        try:
            run_download(url, folder, self.logmsg, self.setprog)
        except Exception as e:
            self.logmsg("Ошибка: " + str(e))
        self.q.put(("done", None))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()