"""CSP Device Monitor - guided setup (5 screens). All logic lives in installer_core.py; this file is only the window.

    python wizard.py                    run the installer
    python wizard.py --selftest         build every screen with sample data, then quit (exit code 0 = fine)
    python wizard.py --screenshots DIR  save a picture of every screen (sample data, no network, nothing installed)
"""
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def fatal(message, detail=""):
    """Setup runs without a console window, so any startup failure must be shown to the person and saved to a file."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "csp_setup_error.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(message + "\n" + detail + "\n")
    except OSError:
        pass
    if not os.environ.get("CSP_SETUP_NO_DIALOG"):
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("CSP Device Monitor - Setup", "%s\n\nA technical note was saved to:\n%s" % (message, path))
            root.destroy()
        except Exception:
            pass
    return path


try:
    import installer_core as ic  # noqa: E402
except Exception as _e:                      # e.g. a missing Python package
    import traceback
    fatal("Setup could not start (%s). Please run SETUP.bat again; if it keeps happening, send the saved note to your administrator." % type(_e).__name__,
          traceback.format_exc())
    sys.exit(1)

try:                                   # sharp text on scaled displays, and correct window coordinates for screenshots
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

BG, FG, ACCENT, MUTED, WARN, GOOD, BAD = "#ffffff", "#1f2937", "#0b5cad", "#6b7280", "#b45309", "#15803d", "#b91c1c"
SCREENS = ["welcome", "register", "devices", "install", "done"]


class Wizard(tk.Tk):
    def __init__(self, sample=False):
        super().__init__()
        self.sample = sample
        self.title("CSP Device Monitor - Setup")
        self.geometry("720x560")
        self.minsize(680, 520)
        self.configure(bg=BG)
        try:
            self.iconbitmap(os.path.join(ic.SOURCE_DIR, "icon.ico"))
        except Exception:
            pass
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass
        self.style.configure("Big.TButton", font=("Segoe UI", 11), padding=(14, 6))
        self.cfg = None
        self.csp_id = self.csp_name = ""
        self.scan = {}
        self.selections = {}
        self.confirmed = {}
        self.result = None
        self.cancelled = False
        self.combos, self.warn_labels = {}, {}
        self.header = tk.Label(self, text="", font=("Segoe UI", 18, "bold"), bg=BG, fg=ACCENT, anchor="w")
        self.header.pack(fill="x", padx=28, pady=(22, 4))
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=6)
        self.footer = tk.Frame(self, bg=BG)
        self.footer.pack(fill="x", padx=28, pady=(0, 18))
        self.show("welcome")

    # ----------------------------------------------------------------------------- helpers
    def clear(self):
        for w in list(self.body.winfo_children()) + list(self.footer.winfo_children()):
            w.destroy()

    def label(self, text, size=11, color=FG, bold=False, wrap=640, pady=4, parent=None, anchor="w"):
        l = tk.Label(parent or self.body, text=text, font=("Segoe UI", size, "bold" if bold else "normal"), bg=BG, fg=color,
                     wraplength=wrap, justify="left", anchor=anchor)
        l.pack(fill="x", pady=pady)
        return l

    def button(self, text, command, side="right", state="normal"):
        b = ttk.Button(self.footer, text=text, command=command, style="Big.TButton", state=state)
        b.pack(side=side, padx=4)
        return b

    def show(self, name):
        self.clear()
        self.current = name
        getattr(self, "screen_" + name)()
        self.update_idletasks()

    # ----------------------------------------------------------------------------- 1. welcome
    def screen_welcome(self):
        self.header.config(text="Welcome")
        self.label("CSP Device Monitor", 22, ACCENT, True, pady=(30, 4))
        self.label("This short setup connects this computer to the monitoring service. It takes about five minutes. "
                   "It will check your printer, Micro-ATM, fingerprint scanner and GPS, and then keep an eye on them "
                   "so problems are noticed early.", 12, pady=(8, 8))
        self.label("You will not be asked for any technical information.", 12, MUTED)
        self.button("Continue", self.start_register)

    def start_register(self):
        try:
            self.cfg = ic.load_installer_config()
        except ic.InstallError as e:
            messagebox.showerror("Setup", str(e))
            return
        self.show("register")

    # ----------------------------------------------------------------------------- 2. registration
    def screen_register(self):
        self.header.config(text="Register your CSP")
        self.label("Please enter your CSP details exactly as given to you.", 12, pady=(10, 14))
        self.label("CSP Name", 11, bold=True, pady=(4, 0))
        self.name_var = tk.StringVar(value=self.csp_name)
        ttk.Entry(self.body, textvariable=self.name_var, font=("Segoe UI", 12)).pack(fill="x", pady=(0, 10))
        self.label("Unique CSP Code", 11, bold=True, pady=(4, 0))
        self.code_var = tk.StringVar(value=self.csp_id)
        ttk.Entry(self.body, textvariable=self.code_var, font=("Segoe UI", 12)).pack(fill="x", pady=(0, 10))
        self.reg_msg = self.label("", 11, BAD, pady=(6, 0))
        self.button("Verify and Continue", self.do_verify)

    def do_verify(self):
        name, code = self.name_var.get().strip(), self.code_var.get().strip()
        if not name or not code:
            self.reg_msg.config(text="Please enter both your CSP name and your CSP code.", fg=BAD)
            return
        self.reg_msg.config(text="Checking with the server...", fg=MUTED)
        self.update_idletasks()

        def work():
            res = ic.verify_registration(self.cfg["server_url"], code, name)
            self.after(0, lambda: self.verified(res, code, name))
        threading.Thread(target=work, daemon=True).start()

    def verified(self, res, code, name):
        if not res.get("ok"):
            self.reg_msg.config(text=res.get("message", "That could not be verified."), fg=BAD)
            return
        self.csp_id, self.csp_name = code, " ".join(name.split())
        self.show("devices")
        self.rescan()

    # ----------------------------------------------------------------------------- 3. devices
    def screen_devices(self):
        self.header.config(text="Select your devices")
        self.label("Choose the device you use for each job. If you do not use one, choose \"Skip\".", 11, MUTED, pady=(2, 8))
        self.grid_frame = tk.Frame(self.body, bg=BG)
        self.grid_frame.pack(fill="both", expand=True)
        self.scan_note = self.label("Looking for your devices...", 11, MUTED, pady=2)
        self.btn_next = self.button("Continue", self.devices_next, state="disabled")
        self.btn_rescan = self.button("Rescan devices", self.rescan, side="left")

    def rescan(self):
        try:
            self.btn_rescan.config(state="disabled")
            self.btn_next.config(state="disabled")
        except Exception:
            pass
        self.scan_note.config(text="Looking for your devices... please wait.", fg=MUTED)

        def work():
            res = self.sample_scan() if self.sample else ic.scan_devices(cfg=ic.read_existing_config(ic.find_existing_install() or ""))
            self.after(0, lambda: self.scanned(res))
        threading.Thread(target=work, daemon=True).start()

    def sample_scan(self):
        def c(cat, name, rec, gen=False):
            return {"category": cat, "name": name, "label": name, "recognised": rec, "generic": gen, "instance": "", "vid": "", "pid": "",
                    "manufacturer": "", "windows_ok": True, "status": "OK", "klass": ""}
        return {"printer": [c("printer", "Epson TM-T20 Receipt", True)], "microatm": [c("microatm", "USB Input Device", False, True)],
                "biometric": [c("biometric", "Mantra MFS100 Fingerprint Scanner", True)], "gps": [], "_scan_problem": ""}

    def scanned(self, scan):
        if self.current != "devices":
            return
        self.scan = scan
        existing = ic.read_existing_config(ic.find_existing_install() or "") if not self.sample else {}
        defaults = ic.suggest_defaults(scan, existing)
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.combos, self.warn_labels = {}, {}
        for row, (cat, title, desc) in enumerate(ic.CATEGORIES):
            tk.Label(self.grid_frame, text=title, font=("Segoe UI", 11, "bold"), bg=BG, fg=FG, anchor="w").grid(row=row * 3, column=0, sticky="w", pady=(8, 0))
            options = [c["label"] for c in scan.get(cat, [])] + ["Device Not Found", "Skip (I do not use this)"]
            cb = ttk.Combobox(self.grid_frame, values=options, state="readonly", width=44, font=("Segoe UI", 10))
            pick = defaults.get(cat)
            cb.set(next((c["label"] for c in scan.get(cat, []) if c["name"] == pick), "Skip (I do not use this)" if not scan.get(cat) else "Choose..."))
            cb.grid(row=row * 3, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
            cb.bind("<<ComboboxSelected>>", lambda e, k=cat: self.choice_changed(k))
            self.combos[cat] = cb
            w = tk.Label(self.grid_frame, text="", font=("Segoe UI", 9), bg=BG, fg=WARN, wraplength=560, justify="left", anchor="w")
            w.grid(row=row * 3 + 1, column=1, sticky="w", padx=(12, 0))
            self.warn_labels[cat] = w
            self.choice_changed(cat)
        problem = scan.get("_scan_problem")
        self.scan_note.config(text=("Windows could not list every device (%s). You can still continue." % problem) if problem
                              else "Tip: devices that were not detected can be skipped now and added later.", fg=MUTED)
        self.btn_next.config(state="normal")
        self.btn_rescan.config(state="normal")

    def selected(self, cat):
        text = self.combos[cat].get()
        for c in self.scan.get(cat, []):
            if c["label"] == text:
                return c
        return None

    def choice_changed(self, cat):
        text = self.combos[cat].get()
        w = self.warn_labels[cat]
        if text == "Device Not Found":
            w.config(text=ic.TROUBLESHOOTING[cat], fg=MUTED)
        elif text.startswith("Skip") or text == "Choose...":
            w.config(text="Skipped - the monitor will not check this device." if text.startswith("Skip") else "", fg=MUTED)
        else:
            w.config(text=ic.warning_for(self.selected(cat), cat), fg=WARN)

    def devices_next(self):
        self.selections = {cat: self.selected(cat) for cat, _t, _d in ic.CATEGORIES}
        unverified = [ic.TITLE[c] for c, v in self.selections.items() if v is not None and not v["recognised"]]
        unresolved = [ic.TITLE[c] for c in self.combos if self.combos[c].get() == "Choose..."]
        if unresolved:
            if not messagebox.askyesno("Setup", "You have not chosen anything for: %s.\n\nSkip these devices for now?" % ", ".join(unresolved)):
                return
        if unverified:
            if not messagebox.askyesno("Setup", "These could not be confirmed to be the right device: %s.\n\n"
                                       "You can continue - the monitor will show them as 'not verified'. Continue?" % ", ".join(unverified)):
                return
        self.show("install")
        self.begin_install()

    # ----------------------------------------------------------------------------- 4. installation
    def screen_install(self):
        self.header.config(text="Setting things up")
        self.label("Please wait. This can take a few minutes. You do not need to do anything.", 11, MUTED, pady=(6, 16))
        self.bar = ttk.Progressbar(self.body, maximum=100, length=620)
        self.bar.pack(fill="x", pady=6)
        self.status = self.label("Starting...", 12, pady=10)
        self.err = self.label("", 11, BAD)
        self.btn_cancel = self.button("Cancel", self.cancel_install)

    def cancel_install(self):
        self.cancelled = True
        self.btn_cancel.config(state="disabled")
        self.status.config(text="Stopping safely...")

    def progress(self, pct, msg):
        self.after(0, lambda: (self.bar.config(value=pct), self.status.config(text=msg)) if self.current == "install" else None)

    def begin_install(self):
        existing = ic.find_existing_install()
        params = dict(csp_id=self.csp_id, name=self.csp_name, server_url=self.cfg["server_url"], source_dir=ic.SOURCE_DIR,
                      install_dir=existing or ic.default_install_dir(), selections=self.selections, confirmed=self.confirmed,
                      agent_version=self.cfg.get("agent_version", ""))

        def work():
            try:
                res = ic.run_install(params, self.progress, cancel=lambda: self.cancelled)
                self.after(0, lambda: self.finished(res))
            except ic.InstallError as e:
                self.after(0, lambda: self.failed(str(e)))
            except Exception:
                ic.log.exception("install failed")
                self.after(0, lambda: self.failed("Something unexpected went wrong. Nothing was damaged. Please try again, "
                                                  "or ask your administrator for help."))
        threading.Thread(target=work, daemon=True).start()

    def failed(self, message):
        self.err.config(text=message)
        self.status.config(text="Setup could not finish.", fg=BAD)
        for w in self.footer.winfo_children():
            w.destroy()
        self.button("Close", self.destroy)
        self.button("Try again", lambda: self.show("install") or self.begin_install(), side="right")

    def finished(self, res):
        self.result = res
        self.show("done")

    # ----------------------------------------------------------------------------- 5. complete
    def screen_done(self):
        r = self.result
        self.header.config(text="Setup complete")
        self.label("%s  (code %s)" % (r["name"], r["csp_id"]), 14, bold=True, pady=(8, 2))
        status = {"approved": ("Connected to the monitoring service", GOOD), "existing": ("Connected to the monitoring service", GOOD),
                  "pending": ("Registered - waiting for your administrator to approve this computer", WARN)}.get(
            r["enrollment"], ("Registered", WARN))
        if r["enrollment"] in ("approved", "existing") and not r["connected"]:
            status = ("Installed - the first report to the server did not go through yet; it will keep trying", WARN)
        self.label(status[0], 12, status[1], True, pady=(0, 2))
        self.label("Monitor version %s" % (r["agent_version"] or "-"), 10, MUTED, pady=(0, 8))
        tbl = tk.Frame(self.body, bg=BG)
        tbl.pack(fill="x", pady=4)
        for i, (title, device, state) in enumerate(r["devices"]):
            tk.Label(tbl, text=title, font=("Segoe UI", 10, "bold"), bg=BG, fg=FG, anchor="w", width=18).grid(row=i, column=0, sticky="w", pady=2)
            tk.Label(tbl, text=device, font=("Segoe UI", 10), bg=BG, fg=FG, anchor="w", width=34).grid(row=i, column=1, sticky="w")
            colour = GOOD if state.startswith("Verified") else (MUTED if device == "Skipped" else WARN)
            tk.Label(tbl, text=state, font=("Segoe UI", 9), bg=BG, fg=colour, anchor="w", wraplength=260, justify="left").grid(row=i, column=2, sticky="w")
        self.label("The monitor now runs by itself in the background and starts with Windows.", 10, MUTED, pady=(14, 0))
        self.open_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self.body, text="Open the monitor's local page", variable=self.open_var, bg=BG, font=("Segoe UI", 10)).pack(anchor="w", pady=6)
        self.button("Finish", self.finish_clicked)

    def finish_clicked(self):
        if self.open_var.get():
            import webbrowser
            webbrowser.open(ic.LOCAL_UI)
        self.destroy()


# ------------------------------------------------------------------------------------------ self test / pictures
def _sample_result():
    return ic.InstallResult(csp_id="CSP-1001", name="Sample CSP", install_dir="", launcher="", enrollment="approved", connected=True,
                            connection_note="", agent_version="2.0.0", scan_summary="", local_url=ic.LOCAL_UI,
                            devices=[("Printer", "Epson TM-T20 Receipt", "Verified (USB hardware match)"),
                                     ("Micro-ATM", "USB Input Device", "Not verified - it could not be confirmed to be your micro-atm"),
                                     ("Biometric Scanner", "Mantra MFS100 Fingerprint Scanner", "Verified (device driver)"),
                                     ("GPS Device", "Skipped", "not used at this CSP")])


def _walk(app, shots=None):
    app.cfg = {"server_url": "https://example.invalid"}
    app.csp_id, app.csp_name = "CSP-1001", "Sample CSP"
    app.result = _sample_result()
    steps = [("welcome", None), ("register", None), ("devices", "scan"), ("install", "bar"), ("done", None)]
    for i, (name, kind) in enumerate(steps, 1):
        app.show(name)
        if kind == "scan":
            app.scanned(app.sample_scan())
        if kind == "bar":
            app.bar.config(value=62)
            app.status.config(text="Setting the monitor to start with Windows...")
        app.update()
        app.update_idletasks()
        if shots:
            from PIL import ImageGrab
            app.lift()
            app.attributes("-topmost", True)
            app.update()
            x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
            ImageGrab.grab(bbox=(x - 8, y - 38, x + w + 8, y + h + 8)).save(os.path.join(shots, "installer_%d_%s.png" % (i, name)))


def main(argv):
    if "--selftest" in argv or "--screenshots" in argv:
        app = Wizard(sample=True)
        shots = None
        if "--screenshots" in argv:
            shots = argv[argv.index("--screenshots") + 1]
            os.makedirs(shots, exist_ok=True)
        _walk(app, shots)
        app.destroy()
        print("wizard self-test OK (%d screens)" % len(SCREENS))
        return 0
    try:
        Wizard().mainloop()
    except Exception as e:
        import traceback
        fatal("Setup stopped unexpectedly (%s). Nothing was damaged; please run SETUP.bat again." % type(e).__name__, traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
