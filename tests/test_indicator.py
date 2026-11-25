"""Unit tests for text editor launching in the taskbar indicator."""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tspt.indicator import GUIDE_FILE, open_in_text_editor, save_setup_settings


class FakeAppInfo:
    def __init__(self, app_id: str, executable: str, launch_ok: bool = True):
        self._id = app_id
        self._exe = executable
        self._ok = launch_ok
        self.launched_files = []

    def get_id(self) -> str:
        return self._id

    def get_executable(self) -> str:
        return self._exe

    def launch(self, files, context) -> bool:
        self.launched_files.extend(files)
        return self._ok


class IndicatorEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg_file = Path(self.temp.name) / "config.json"

    @patch("tspt.indicator.subprocess.Popen")
    def test_configured_editor_takes_precedence(self, mock_popen):
        fake_gio = MagicMock()
        open_in_text_editor(self.cfg_file, configured_editor="subl -w", gio_module=fake_gio)
        self.assertTrue(self.cfg_file.exists())
        mock_popen.assert_called_once_with(
            ["subl", "-w", str(self.cfg_file)],
            start_new_session=True,
        )
        fake_gio.AppInfo.get_default_for_type.assert_not_called()

    @patch("tspt.indicator.subprocess.Popen")
    def test_uses_gio_text_plain_default_instead_of_browser_json_mime(self, mock_popen):
        app_info = FakeAppInfo("org.gnome.TextEditor.desktop", "gnome-text-editor")
        fake_gio = MagicMock()
        fake_gio.AppInfo.get_default_for_type.return_value = app_info
        fake_gio.File.new_for_path.side_effect = lambda p: f"GFile({p})"

        open_in_text_editor(self.cfg_file, gio_module=fake_gio)

        fake_gio.AppInfo.get_default_for_type.assert_called_once_with("text/plain", False)
        self.assertEqual(app_info.launched_files, [f"GFile({self.cfg_file})"])
        mock_popen.assert_not_called()

    @patch("tspt.indicator.shutil.which")
    @patch("tspt.indicator.subprocess.Popen")
    def test_skips_browser_appinfo_and_falls_back_to_gui_editor(self, mock_popen, mock_which):
        browser_app = FakeAppInfo("firefox_firefox.desktop", "firefox")
        fake_gio = MagicMock()
        fake_gio.AppInfo.get_default_for_type.return_value = browser_app
        mock_which.side_effect = lambda cmd: "/usr/bin/gnome-text-editor" if cmd == "gnome-text-editor" else None

        open_in_text_editor(self.cfg_file, gio_module=fake_gio)

        self.assertEqual(browser_app.launched_files, [])
        mock_popen.assert_called_once_with(
            ["/usr/bin/gnome-text-editor", str(self.cfg_file)],
            start_new_session=True,
        )

    def test_bundled_guide_exists_and_documents_config(self):
        self.assertTrue(GUIDE_FILE.exists())
        content = GUIDE_FILE.read_text(encoding="utf-8")
        self.assertIn("plug.ip", content)
        self.assertIn("plug.mac", content)
        self.assertIn("credentials.json", content)
        self.assertIn("Setup Plug & Credentials", content)

    def test_save_setup_settings_fresh_install_writes_chmod_600_and_probes_mac(self):
        cred_file = Path(self.temp.name) / "credentials.json"
        self.cfg_file.write_text(f'{{"credentials_file": "{cred_file}"}}\n', encoding="utf-8")

        cfg, probe = save_setup_settings(
            self.cfg_file,
            plug_ip="192.168.1.88",
            plug_model="P105",
            tapo_email="user@example.com",
            tapo_password="secret-password",
            charger_only_confirmed=True,
            enable_auto_charge=True,
            verify=True,
            probe_fn=lambda c: {"mac": "11:22:33:44:55:66", "model": "P105", "is_on": True},
        )

        self.assertEqual(stat.S_IMODE(cred_file.stat().st_mode), 0o600)
        self.assertEqual(cfg["plug"]["ip"], "192.168.1.88")
        self.assertEqual(cfg["plug"]["mac"], "11:22:33:44:55:66")
        self.assertTrue(cfg["auto_charge"]["enabled"])
        self.assertIsNotNone(probe)

    def test_save_setup_settings_preserves_saved_password_and_clears_stale_mac_on_ip_change(self):
        cred_file = Path(self.temp.name) / "credentials.json"
        cred_file.write_text('{"tapo_email": "old@example.com", "tapo_password": "keep-me"}\n', encoding="utf-8")
        cred_file.chmod(0o644)  # Simulate overly-permissive file edited manually
        self.cfg_file.write_text(
            f'{{"plug": {{"ip": "192.168.1.10", "mac": "AA:BB:CC:DD:EE:FF"}}, "credentials_file": "{cred_file}"}}\n',
            encoding="utf-8",
        )

        seen_mac_during_probe = []

        def _fake_probe(c):
            seen_mac_during_probe.append(c["plug"]["mac"])
            return {"mac": "99:88:77:66:55:44", "model": "P105", "is_on": False}

        cfg, _ = save_setup_settings(
            self.cfg_file,
            plug_ip="192.168.1.20",
            plug_model="P105",
            plug_mac="AA:BB:CC:DD:EE:FF",
            tapo_email="",
            tapo_password="",
            charger_only_confirmed=True,
            enable_auto_charge=False,
            verify=True,
            probe_fn=_fake_probe,
        )

        # Repaired permissions to 0600 and cleared stale MAC before probing replacement plug
        self.assertEqual(stat.S_IMODE(cred_file.stat().st_mode), 0o600)
        self.assertEqual(seen_mac_during_probe, [""])
        self.assertEqual(cfg["plug"]["mac"], "99:88:77:66:55:44")

    def test_save_setup_settings_rejects_missing_credentials_on_fresh_install(self):
        cred_file = Path(self.temp.name) / "missing_creds.json"
        self.cfg_file.write_text(f'{{"credentials_file": "{cred_file}"}}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            save_setup_settings(
                self.cfg_file,
                plug_ip="192.168.1.88",
                plug_model="P105",
                tapo_email="",
                tapo_password="",
                charger_only_confirmed=False,
                enable_auto_charge=False,
                verify=False,
            )


if __name__ == "__main__":
    unittest.main()
