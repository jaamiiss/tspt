"""Native Linux GTK3 + AyatanaAppIndicator3 taskbar indicator with built-in Auto-Charge Guard."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    default_config_path,
    ensure_config,
    load_json,
    resolve_kasa_python,
    save_json_atomic,
    state_dir,
)
from .guard import (
    manual_plug_action,
    read_battery,
    read_connection_cache,
    resolve_battery_path,
    run_guard_cycle,
    setup_logging,
)

PACKAGE_DIR = Path(__file__).resolve().parent
ICONS_DIR = PACKAGE_DIR / "icons"
GUIDE_FILE = PACKAGE_DIR / "GUIDE.md"
SYSTEM_ICONS_DIR = Path("/usr/share/tspt/icons")

PRESET_RANGES: list[tuple[int, int, str]] = [
    (20, 80, "20% – 80% (Balanced)"),
    (25, 80, "25% – 80% (Conservative Low)"),
    (20, 85, "20% – 85% (Extended Runtime)"),
    (30, 75, "30% – 75% (Storage / Desk)"),
]

BROWSER_KEYWORDS: tuple[str, ...] = (
    "firefox",
    "chrome",
    "chromium",
    "brave",
    "vivaldi",
    "opera",
    "epiphany",
    "midori",
    "waterfox",
    "librewolf",
    "zen-browser",
    "browser",
)

GUI_TEXT_EDITORS: tuple[str, ...] = (
    "gnome-text-editor",
    "gedit",
    "kate",
    "mousepad",
    "xed",
    "pluma",
    "featherpad",
    "leafpad",
    "subl",
    "code",
    "codium",
)


def _is_browser_app(app_info: Any) -> bool:
    ident = " ".join(
        filter(
            None,
            (
                getattr(app_info, "get_id", lambda: "")() or "",
                getattr(app_info, "get_executable", lambda: "")() or "",
            ),
        )
    ).lower()
    return any(keyword in ident for keyword in BROWSER_KEYWORDS)


def open_in_text_editor(
    target: Path,
    configured_editor: str = "",
    *,
    gio_module: Any = None,
) -> None:
    """Open a config or log file in a desktop text editor rather than a web browser."""
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()

    editor_cmd = configured_editor.strip()
    if editor_cmd:
        parts = shlex.split(editor_cmd)
        if parts:
            subprocess.Popen([*parts, str(target)], start_new_session=True)
            return

    gio = gio_module
    if gio is None:
        try:
            from gi.repository import Gio as gio  # type: ignore[no-redef]
        except Exception:
            gio = None

    if gio is not None:
        try:
            app_info = gio.AppInfo.get_default_for_type("text/plain", False)
            if app_info is not None and not _is_browser_app(app_info):
                gfile = gio.File.new_for_path(str(target))
                if app_info.launch([gfile], None):
                    return
        except Exception:
            pass

    for candidate in GUI_TEXT_EDITORS:
        resolved = shutil.which(candidate)
        if resolved:
            subprocess.Popen([resolved, str(target)], start_new_session=True)
            return

    term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
    cli_editor = (
        os.environ.get("VISUAL", "").strip()
        or os.environ.get("EDITOR", "").strip()
        or shutil.which("nano")
        or shutil.which("vi")
    )
    if term and cli_editor:
        if os.path.basename(term) == "gnome-terminal":
            subprocess.Popen([term, "--", *shlex.split(cli_editor), str(target)], start_new_session=True)
        else:
            subprocess.Popen([term, "-e", *shlex.split(cli_editor), str(target)], start_new_session=True)
        return

    subprocess.Popen(["xdg-open", str(target)], start_new_session=True)


def read_existing_credentials_soft(config: dict[str, Any]) -> tuple[str, str]:
    """Read existing email/password from credentials_file if present, ignoring permission errors."""
    cred_path = Path(config.get("credentials_file", "~/.config/tspt/credentials.json")).expanduser()
    data = load_json(cred_path, default={})
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("tapo_email", "")).strip(), str(data.get("tapo_password", ""))


def save_setup_settings(
    config_path: Path,
    *,
    plug_ip: str,
    plug_model: str,
    plug_mac: str = "",
    tapo_email: str,
    tapo_password: str,
    charger_only_confirmed: bool,
    enable_auto_charge: bool,
    verify: bool = True,
    probe_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Save private credentials (chmod 600) and config, optionally probing the plug to auto-fill MAC."""
    clean_ip = plug_ip.strip()
    if not clean_ip:
        raise ValueError("Smart Plug IP address is required.")

    cfg = ensure_config(config_path)
    existing_email, existing_password = read_existing_credentials_soft(cfg)

    final_email = tapo_email.strip() or existing_email
    final_password = tapo_password if tapo_password else existing_password
    if not final_email or not final_password:
        raise ValueError("Tapo account email and password are required.")

    cred_path = Path(cfg.get("credentials_file", "~/.config/tspt/credentials.json")).expanduser()
    save_json_atomic(
        cred_path,
        {"tapo_email": final_email, "tapo_password": final_password},
        mode=0o600,
    )

    old_ip = str(cfg.get("plug", {}).get("ip", "")).strip()
    clean_mac = plug_mac.strip()
    if verify and old_ip and old_ip != clean_ip and clean_mac == str(cfg.get("plug", {}).get("mac", "")).strip():
        # Clear stale MAC from previous IP before probing a new plug IP.
        clean_mac = ""

    plug_section = cfg.setdefault("plug", {})
    plug_section["ip"] = clean_ip
    plug_section["model"] = plug_model.strip()
    plug_section["mac"] = clean_mac

    auto_section = cfg.setdefault("auto_charge", {})
    auto_section["charger_only_confirmed"] = bool(charger_only_confirmed)
    if not charger_only_confirmed:
        auto_section["enabled"] = False
    elif not verify:
        auto_section["enabled"] = bool(enable_auto_charge and clean_mac)

    save_json_atomic(config_path, cfg, mode=0o644)

    probe_info: dict[str, Any] | None = None
    if verify:
        runner = probe_fn or (lambda c: asyncio.run(manual_plug_action("status", c)))
        probe_info = runner(cfg)
        if probe_info.get("mac"):
            plug_section["mac"] = str(probe_info["mac"])
        if probe_info.get("model"):
            plug_section["model"] = str(probe_info["model"])
        if charger_only_confirmed:
            auto_section["enabled"] = bool(enable_auto_charge)
        save_json_atomic(config_path, cfg, mode=0o644)

    return cfg, probe_info


def resolve_icon(icon_name: str, fallback_svg: Path, gtk_module: Any) -> str:
    """Resolve an icon name to a bundled SVG file, system path, or GTK icon theme name."""
    if os.path.isabs(icon_name) and os.path.exists(icon_name):
        return icon_name

    filename = icon_name if icon_name.endswith(".svg") else f"{icon_name}.svg"
    for folder in (ICONS_DIR, SYSTEM_ICONS_DIR):
        candidate = folder / filename
        if candidate.exists():
            return str(candidate)

    theme = gtk_module.IconTheme.get_default()
    clean_name = icon_name[:-4] if icon_name.endswith(".svg") else icon_name
    if theme and theme.has_icon(clean_name):
        return clean_name

    if fallback_svg.exists():
        return str(fallback_svg)
    return "ac-adapter-symbolic"


class TsptTrayApp:
    """Standalone Linux top-bar indicator with integrated Auto-Charge Guard background loop."""

    def __init__(self, config_path: Path | None = None) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, GLib, Gtk

        self.Gtk = Gtk
        self.GLib = GLib
        self.AppIndicator = AyatanaAppIndicator3

        self.config_path = config_path or default_config_path()
        self.config = ensure_config(self.config_path)
        self._busy_lock = threading.Lock()
        self._setup_window: Any = None
        setup_logging()

        icons_cfg = self.config.get("ui", {}).get("icons", {})
        initial_name = icons_cfg.get("plug_off", "tspt-off-symbolic")
        initial_icon = resolve_icon(initial_name, ICONS_DIR / "tspt-off-symbolic.svg", Gtk)

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "tspt",
            initial_icon,
            AyatanaAppIndicator3.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)

        self.refresh_ui()

        ui_interval = max(2, int(self.config.get("ui", {}).get("refresh_interval_sec", 3)))
        GLib.timeout_add_seconds(ui_interval, self.refresh_ui)

        guard_interval = max(10, int(self.config.get("auto_charge", {}).get("check_interval_sec", 30)))
        GLib.timeout_add_seconds(guard_interval, self._schedule_guard_tick)
        # Run an initial background check shortly after startup if configured.
        GLib.timeout_add_seconds(2, self._initial_tick_once)

    def _initial_tick_once(self) -> bool:
        self._schedule_guard_tick()
        return False

    def _build_menu(self) -> None:
        Gtk = self.Gtk
        for child in self.menu.get_children():
            self.menu.remove(child)

        labels = self.config.get("ui", {}).get("labels", {})

        self.item_battery = Gtk.MenuItem(label="Battery: --")
        self.item_battery.set_sensitive(False)
        self.menu.append(self.item_battery)

        self.item_plug = Gtk.MenuItem(label="Plug: --")
        self.item_plug.set_sensitive(False)
        self.menu.append(self.item_plug)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_on = Gtk.MenuItem(label=labels.get("turn_on", "Turn On"))
        item_on.connect("activate", lambda _: self.trigger_plug_action("on"))
        self.menu.append(item_on)

        item_off = Gtk.MenuItem(label=labels.get("turn_off", "Turn Off"))
        item_off.connect("activate", lambda _: self.trigger_plug_action("off"))
        self.menu.append(item_off)

        item_refresh = Gtk.MenuItem(label=labels.get("refresh", "Refresh Status"))
        item_refresh.connect("activate", lambda _: self.trigger_plug_action("status"))
        self.menu.append(item_refresh)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.item_auto = Gtk.MenuItem(label="Auto-Charge: --")
        self.item_auto.connect("activate", lambda _: self.toggle_auto_charge())
        self.menu.append(self.item_auto)

        presets_item = Gtk.MenuItem(label=labels.get("presets", "Threshold Presets"))
        presets_menu = Gtk.Menu()
        for low, high, title in PRESET_RANGES:
            p_item = Gtk.MenuItem(label=title)
            p_item.connect("activate", lambda _, lo=low, hi=high: self.set_thresholds(lo, hi))
            presets_menu.append(p_item)
        presets_item.set_submenu(presets_menu)
        self.menu.append(presets_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_setup = Gtk.MenuItem(label=labels.get("setup", "Setup Plug & Credentials…"))
        item_setup.connect("activate", lambda _: self.open_setup_dialog())
        self.menu.append(item_setup)

        item_cfg = Gtk.MenuItem(label=labels.get("settings", "Edit Config"))
        item_cfg.connect("activate", lambda _: self.open_path(self.config_path))
        self.menu.append(item_cfg)

        item_logs = Gtk.MenuItem(label=labels.get("logs", "View Activity Log"))
        item_logs.connect("activate", lambda _: self.open_path(state_dir() / "history.log"))
        self.menu.append(item_logs)

        item_help = Gtk.MenuItem(label=labels.get("help", "Usage & Config Guide"))
        item_help.connect("activate", lambda _: self.open_guide())
        self.menu.append(item_help)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label=labels.get("quit", "Quit"))
        item_quit.connect("activate", lambda _: self.Gtk.main_quit())
        self.menu.append(item_quit)

        self.menu.show_all()

    def refresh_ui(self) -> bool:
        self.config = ensure_config(self.config_path)
        ui_cfg = self.config.get("ui", {})
        labels = ui_cfg.get("labels", {})
        auto_cfg = self.config.get("auto_charge", {})
        bat_cfg = self.config.get("battery", {})

        try:
            bat_path = resolve_battery_path(str(bat_cfg.get("path", "auto")))
            bat = read_battery(bat_path, strict=False)
        except FileNotFoundError:
            bat = read_battery(Path("/nonexistent"), strict=False)

        cache = read_connection_cache()
        is_plug_on = cache.get("plug_on")

        icons_cfg = ui_cfg.get("icons", {})
        icon_name = (
            icons_cfg.get("plug_on", "tspt-on-symbolic")
            if is_plug_on
            else icons_cfg.get("plug_off", "tspt-off-symbolic")
        )
        fallback = (
            ICONS_DIR / "tspt-on-symbolic.svg"
            if is_plug_on
            else ICONS_DIR / "tspt-off-symbolic.svg"
        )
        icon_resolved = resolve_icon(icon_name, fallback, self.Gtk)
        self.indicator.set_icon_full(icon_resolved, "Plug ON" if is_plug_on else "Plug OFF")

        if ui_cfg.get("show_label", False):
            fmt = ui_cfg.get("label_format", "{battery}%")
            label_text = fmt.format(
                battery=bat.percent,
                status=bat.status,
                plug="ON" if is_plug_on else ("OFF" if is_plug_on is False else "--"),
            )
            self.indicator.set_label(label_text, "100%")
        else:
            self.indicator.set_label("", "")

        power_suffix = f" · {bat.power_w}W" if bat.power_w > 0 else ""
        bat_fmt = labels.get("battery_format", "Battery: {capacity}% ({status}){power_suffix}")
        self.item_battery.set_label(
            bat_fmt.format(capacity=bat.percent, status=bat.status, power_suffix=power_suffix)
        )

        if is_plug_on is True:
            plug_text = labels.get("plug_on", "Plug: On")
        elif is_plug_on is False:
            plug_text = labels.get("plug_off", "Plug: Off (15m failsafe)")
        else:
            plug_text = labels.get("plug_unknown", "Plug: Unknown")
        self.item_plug.set_label(plug_text)

        auto_enabled = bool(auto_cfg.get("enabled", False))
        low = int(auto_cfg.get("turn_on", 20))
        high = int(auto_cfg.get("turn_off", 80))
        auto_lbl = (
            labels.get("auto_on", "Auto-Charge: On ({low}%–{high}%)")
            if auto_enabled
            else labels.get("auto_off", "Auto-Charge: Off")
        )
        self.item_auto.set_label(auto_lbl.format(low=low, high=high))
        return True

    def _run_plug_command(self, subcommand: list[str]) -> dict[str, Any]:
        """Run plug I/O in-process if python-kasa is available, else via configured venv Python."""
        ext_python = resolve_kasa_python(self.config)
        if ext_python:
            env = {
                **os.environ,
                "TSPT_CONFIG": str(self.config_path),
                "PYTHONPATH": str(PACKAGE_DIR.parent),
            }
            cmd = [ext_python, "-m", "tspt.cli", *subcommand, "--json"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25, env=env, check=False)
            if res.returncode != 0:
                raise RuntimeError((res.stderr or res.stdout or "Plug command failed").strip())
            return json.loads(res.stdout)

        action = subcommand[0]
        if action in {"on", "off", "status"}:
            return asyncio.run(manual_plug_action(action, self.config))
        if action == "check":
            msg = run_guard_cycle(self.config, apply="--apply" in subcommand)
            return {"status": "ok", "message": msg}
        raise ValueError(f"Unknown command: {subcommand}")

    def _needs_initial_setup(self) -> bool:
        self.config = ensure_config(self.config_path)
        ip = str(self.config.get("plug", {}).get("ip", "")).strip()
        if not ip:
            return True
        if os.environ.get("TAPO_EMAIL") and os.environ.get("TAPO_PASSWORD"):
            return False
        email, password = read_existing_credentials_soft(self.config)
        return not (email and password)

    def trigger_plug_action(self, action: str) -> None:
        if self._needs_initial_setup():
            self.open_setup_dialog()
            return

        def worker() -> None:
            if not self._busy_lock.acquire(blocking=False):
                return
            try:
                data = self._run_plug_command([action])
                if action in ("on", "off"):
                    self.notify("TSPT", f"Plug turned {action.upper()}")
                else:
                    state_word = "ON" if data.get("is_on") else "OFF"
                    self.notify("TSPT", f"Plug status: {state_word}")
            except Exception as exc:
                self.notify("TSPT Error", str(exc))
            finally:
                self._busy_lock.release()
                self.GLib.idle_add(self.refresh_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_guard_tick(self) -> bool:
        self.config = ensure_config(self.config_path)
        auto_cfg = self.config.get("auto_charge", {})
        plug_ip = str(self.config.get("plug", {}).get("ip", "")).strip()
        if not plug_ip or not auto_cfg.get("enabled", False):
            return True

        def worker() -> None:
            if not self._busy_lock.acquire(blocking=False):
                return
            try:
                self._run_plug_command(["check", "--apply"])
            except Exception:
                pass
            finally:
                self._busy_lock.release()
                self.GLib.idle_add(self.refresh_ui)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def toggle_auto_charge(self) -> None:
        self.config = ensure_config(self.config_path)
        auto_cfg = self.config.setdefault("auto_charge", {})
        plug_cfg = self.config.get("plug", {})
        new_state = not bool(auto_cfg.get("enabled", False))
        if new_state and (
            self._needs_initial_setup()
            or not auto_cfg.get("charger_only_confirmed", False)
            or not str(plug_cfg.get("mac", "")).strip()
        ):
            self.open_setup_dialog()
            return
        auto_cfg["enabled"] = new_state
        save_json_atomic(self.config_path, self.config)
        self.notify("TSPT Auto-Charge", "Resumed" if new_state else "Paused")
        self.refresh_ui()
        if new_state:
            self._schedule_guard_tick()

    def open_setup_dialog(self) -> None:
        """Open a native GTK3 setup window for Plug IP, private credentials, and MAC verification."""
        if self._setup_window is not None:
            self._setup_window.present()
            return

        Gtk = self.Gtk
        self.config = ensure_config(self.config_path)
        plug_cfg = self.config.get("plug", {})
        auto_cfg = self.config.get("auto_charge", {})
        existing_email, existing_password = read_existing_credentials_soft(self.config)
        cred_display = str(
            Path(self.config.get("credentials_file", "~/.config/tspt/credentials.json")).expanduser()
        )

        win = Gtk.Window(title="TSPT - Setup Plug & Credentials")
        win.set_default_size(460, -1)
        win.set_border_width(18)
        win.set_resizable(False)
        win.set_position(Gtk.WindowPosition.CENTER)
        icon_svg = ICONS_DIR / "tspt-on-symbolic.svg"
        if icon_svg.exists():
            try:
                win.set_icon_from_file(str(icon_svg))
            except Exception:
                pass

        def _on_destroy(*_args: Any) -> None:
            self._setup_window = None

        win.connect("destroy", _on_destroy)
        self._setup_window = win

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        win.add(vbox)

        title = Gtk.Label()
        title.set_markup("<b>Tapo Smart Plug &amp; Credentials Setup</b>")
        title.set_xalign(0.0)
        vbox.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(
            label=f"Credentials are saved privately (chmod 600) to {cred_display}."
        )
        subtitle.set_line_wrap(True)
        subtitle.set_max_width_chars(56)
        subtitle.set_xalign(0.0)
        vbox.pack_start(subtitle, False, False, 0)

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)
        vbox.pack_start(grid, False, False, 4)

        def _add_row(row: int, label_text: str, widget: Any) -> None:
            lbl = Gtk.Label(label=label_text)
            lbl.set_xalign(0.0)
            widget.set_hexpand(True)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)

        entry_ip = Gtk.Entry()
        entry_ip.set_placeholder_text("e.g. 192.168.1.50")
        entry_ip.set_text(str(plug_cfg.get("ip", "")))
        _add_row(0, "Smart Plug IP:", entry_ip)

        entry_model = Gtk.Entry()
        entry_model.set_placeholder_text("P105")
        entry_model.set_text(str(plug_cfg.get("model", "P105")))
        _add_row(1, "Plug Model:", entry_model)

        entry_email = Gtk.Entry()
        entry_email.set_placeholder_text("you@example.com")
        entry_email.set_text(existing_email)
        _add_row(2, "Tapo Email:", entry_email)

        entry_pw = Gtk.Entry()
        entry_pw.set_visibility(False)
        entry_pw.set_placeholder_text(
            "Saved (leave blank to keep current)" if existing_password else "Tapo account password"
        )
        entry_pw.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
        entry_pw.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show / hide password")

        def _toggle_pw(entry: Any, _pos: Any, _event: Any) -> None:
            vis = not entry.get_visibility()
            entry.set_visibility(vis)
            entry.set_icon_from_icon_name(
                Gtk.EntryIconPosition.SECONDARY,
                "view-conceal-symbolic" if vis else "view-reveal-symbolic",
            )

        entry_pw.connect("icon-press", _toggle_pw)
        _add_row(3, "Tapo Password:", entry_pw)

        entry_mac = Gtk.Entry()
        entry_mac.set_placeholder_text("Auto-detected on Verify & Save")
        entry_mac.set_text(str(plug_cfg.get("mac", "")))
        _add_row(4, "Plug MAC:", entry_mac)

        chk_charger = Gtk.CheckButton(
            label="Only my laptop charger is plugged into this smart plug"
        )
        chk_charger.set_active(bool(auto_cfg.get("charger_only_confirmed", False)))
        vbox.pack_start(chk_charger, False, False, 0)

        low = int(auto_cfg.get("turn_on", 20))
        high = int(auto_cfg.get("turn_off", 80))
        chk_auto = Gtk.CheckButton(
            label=f"Enable Auto-Charge Guard ({low}%–{high}%)"
        )
        chk_auto.set_active(bool(auto_cfg.get("enabled", True if not existing_email else False)))
        chk_auto.set_sensitive(chk_charger.get_active())
        chk_charger.connect(
            "toggled", lambda btn: chk_auto.set_sensitive(btn.get_active())
        )
        vbox.pack_start(chk_auto, False, False, 0)

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spinner = Gtk.Spinner()
        status_lbl = Gtk.Label(label="")
        status_lbl.set_line_wrap(True)
        status_lbl.set_max_width_chars(52)
        status_lbl.set_xalign(0.0)
        status_box.pack_start(spinner, False, False, 0)
        status_box.pack_start(status_lbl, True, True, 0)
        vbox.pack_start(status_box, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        vbox.pack_start(btn_box, False, False, 4)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect("clicked", lambda _: win.destroy())
        btn_box.pack_start(btn_cancel, False, False, 0)

        btn_save_only = Gtk.Button(label="Save Only")
        btn_verify = Gtk.Button(label="Verify & Save")
        btn_verify.get_style_context().add_class("suggested-action")
        btn_box.pack_start(btn_save_only, False, False, 0)
        btn_box.pack_start(btn_verify, False, False, 0)

        def _set_busy(busy: bool, msg: str = "") -> None:
            for w in (entry_ip, entry_model, entry_email, entry_pw, entry_mac, chk_charger, chk_auto, btn_save_only, btn_verify):
                w.set_sensitive(not busy)
            if not busy:
                chk_auto.set_sensitive(chk_charger.get_active())
            if busy:
                spinner.start()
            else:
                spinner.stop()
            status_lbl.set_text(msg)

        def _submit(verify: bool) -> None:
            ip_val = entry_ip.get_text()
            model_val = entry_model.get_text()
            email_val = entry_email.get_text()
            pw_val = entry_pw.get_text()
            mac_val = entry_mac.get_text()
            charger_ok = chk_charger.get_active()
            auto_ok = chk_auto.get_active()

            _set_busy(True, "Connecting to smart plug over TPAP…" if verify else "Saving settings…")

            def worker() -> None:
                try:
                    def _probe(cfg_dict: dict[str, Any]) -> dict[str, Any]:
                        self.config = cfg_dict
                        return self._run_plug_command(["status"])

                    saved_cfg, probe_info = save_setup_settings(
                        self.config_path,
                        plug_ip=ip_val,
                        plug_model=model_val,
                        plug_mac=mac_val,
                        tapo_email=email_val,
                        tapo_password=pw_val,
                        charger_only_confirmed=charger_ok,
                        enable_auto_charge=auto_ok,
                        verify=verify,
                        probe_fn=_probe,
                    )
                    self.config = saved_cfg

                    def _on_ok() -> bool:
                        self.refresh_ui()
                        if verify and probe_info:
                            self.notify(
                                "TSPT Setup Verified",
                                f"Connected to {probe_info.get('model', 'Plug')} ({probe_info.get('mac', '')})",
                            )
                        else:
                            self.notify("TSPT Setup Saved", "Saved configuration and private credentials.")
                        if saved_cfg.get("auto_charge", {}).get("enabled"):
                            self._schedule_guard_tick()
                        if self._setup_window is not None:
                            self._setup_window.destroy()
                        return False

                    self.GLib.idle_add(_on_ok)
                except Exception as exc:
                    err_text = str(exc)

                    def _on_err() -> bool:
                        if self._setup_window is not None:
                            _set_busy(False, f"Error: {err_text}")
                        return False

                    self.GLib.idle_add(_on_err)

            threading.Thread(target=worker, daemon=True).start()

        btn_save_only.connect("clicked", lambda _: _submit(False))
        btn_verify.connect("clicked", lambda _: _submit(True))
        win.show_all()

    def set_thresholds(self, low: int, high: int) -> None:
        self.config = ensure_config(self.config_path)
        auto_cfg = self.config.setdefault("auto_charge", {})
        auto_cfg["turn_on"] = int(low)
        auto_cfg["turn_off"] = int(high)
        save_json_atomic(self.config_path, self.config)
        self.notify("TSPT Thresholds", f"Updated range to {low}% – {high}%")
        self.refresh_ui()
        if auto_cfg.get("enabled", False):
            self._schedule_guard_tick()

    def open_path(self, target: Path) -> None:
        self.config = ensure_config(self.config_path)
        configured_editor = str(self.config.get("ui", {}).get("editor", ""))
        try:
            open_in_text_editor(target, configured_editor=configured_editor)
        except Exception as exc:
            self.notify("TSPT Error", f"Could not open editor: {exc}")

    def open_guide(self) -> None:
        if not GUIDE_FILE.exists():
            self.notify("TSPT Error", f"Guide not found at {GUIDE_FILE}")
            return
        self.open_path(GUIDE_FILE)

    def notify(self, title: str, body: str) -> None:
        if not self.config.get("ui", {}).get("show_notifications", True):
            return
        try:
            subprocess.run(["notify-send", "-a", "TSPT", title, body], check=False, timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def run(self) -> None:
        self.Gtk.main()
