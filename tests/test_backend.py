import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main


class DetectionTests(unittest.TestCase):
    def test_connection_and_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            device = root / "0000:03:00.0"
            device.mkdir()
            (device / "vendor").write_text("0x1002\n")
            (device / "device").write_text("0x73ff\n")
            with patch.object(main, "PCI_DEVICES", root):
                self.assertIsNone(main.gpu_status()[0]["driver"])
                self.assertIsNone(main.gpu_status()[0]["name"])
                driver = root / "amdgpu"
                driver.mkdir()
                (device / "driver").symlink_to(driver)
                card = device / "drm" / "card1"
                card.mkdir(parents=True)
                (card / "device").mkdir()
                (card / "device" / "label").write_text("AMD Radeon RX 6950 XT\n")
                self.assertEqual(main.gpu_status()[0]["driver"], "amdgpu")
                self.assertEqual(main.gpu_status()[0]["name"], "RX 6950 XT")
                (device / "device").write_text("0x9999")
                self.assertEqual(main.gpu_status(), [])

    def test_lspci_name_for_matching_id(self):
        output = (
            "0000:05:00.0 VGA compatible controller [0300]: "
            "Advanced Micro Devices, Inc. [AMD/ATI] "
            "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT] [1002:73ff] (rev c1)\n"
            "0000:63:00.0 VGA compatible controller [0300]: "
            "Advanced Micro Devices, Inc. [AMD/ATI] Device [1002:15bf]\n"
        )
        names = main.parse_lspci_names(output)
        self.assertEqual(names["0000:05:00.0"], "RX 6800 XT")
        self.assertEqual(names["05:00.0"], names["0000:05:00.0"])
        self.assertNotIn("0000:63:00.0", names)
        devices = [{"address": "0000:05:00.0", "name": None, "driver": "amdgpu"}]
        main.apply_lspci_names(devices, output)
        self.assertEqual(devices[0]["name"], "RX 6800 XT")
        untitled = [{"address": "0000:05:00.0", "name": None}]
        main.apply_lspci_names(untitled, "00:00.0 VGA compatible controller [0300]: Other [10de:1234]")
        self.assertIsNone(untitled[0]["name"])

    def test_rx_9070_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            device = Path(folder) / "0000:64:00.0"
            device.mkdir()
            (device / "vendor").write_text("0x1002\n")
            (device / "device").write_text("0x7550\n")
            with patch.object(main, "PCI_DEVICES", Path(folder)):
                self.assertEqual(main.gpu_status()[0]["address"], "0000:64:00.0")
        names = main.parse_lspci_names(
            "0000:64:00.0 VGA compatible controller [0300]: Advanced Micro Devices, Inc. "
            "[AMD/ATI] Navi 48 [Radeon RX 9070/9070 XT/9070 GRE] [1002:7550] (rev c0)\n"
        )
        self.assertEqual(names["0000:64:00.0"], "RX 9070 XT")

    def test_missing_sysfs_is_not_disconnected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, "PCI_DEVICES", Path(folder) / "missing"):
                with self.assertRaises(FileNotFoundError):
                    main.gpu_status()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = main.Plugin()
        self.gpu = patch.object(main, "gpu_status", return_value=[
            {"address": "0000:03:00.0", "driver": "amdgpu", "name": "RX 6950 XT"},
        ])
        self.gpu_mock = self.gpu.start()
        self.unit = patch.object(main, "unit_status", new=AsyncMock(
            return_value={"LoadState": "loaded", "ActiveState": "active"},
        ))
        self.unit_mock = self.unit.start()
        self.command = patch.object(main, "systemctl", new=AsyncMock(return_value=""))
        self.command_mock = self.command.start()
        self.scan = patch.object(main, "pci_scan", new=AsyncMock(return_value={"ran": True, "error": None, "output": ""}))
        self.scan.start()
        self.addCleanup(patch.stopall)

    async def test_fills_gpu_name_from_lspci(self):
        self.gpu_mock.return_value = [{"address": "0000:05:00.0", "driver": "amdgpu", "name": None}]
        self.plugin._lspci_names = {}
        self.plugin._lspci_at = 0
        with patch.object(main, "pci_scan", new=AsyncMock(return_value={
            "ran": True,
            "error": None,
            "output": "0000:05:00.0 VGA compatible controller [0300]: Navi 21 [Radeon RX 6800 XT] [1002:73ff]\n",
        })):
            status = await self.plugin.get_status()
        self.assertEqual(status["devices"][0]["name"], "RX 6800 XT")

    async def test_queue_and_cooldown(self):
        self.assertTrue((await self.plugin.get_status())["can_restart"])
        result = await self.plugin.restart_gamescope()
        self.assertIn("queued", result["message"])
        self.command_mock.assert_awaited_once_with("--no-block", "restart", main.TARGET)
        self.assertFalse((await self.plugin.get_status())["can_restart"])
        with self.assertRaises(RuntimeError):
            await self.plugin.restart_gamescope()

    async def test_restart_does_not_require_gpu_detection(self):
        for devices in ([], [{"address": "gpu", "driver": None}]):
            self.gpu_mock.return_value = devices
            plugin = main.Plugin()
            self.assertTrue((await plugin.get_status())["can_restart"])
            result = await plugin.restart_gamescope()
            self.assertIn("queued", result["message"])
        self.assertEqual(self.command_mock.await_count, 2)

    async def test_restart_still_works_when_pci_status_fails(self):
        self.gpu_mock.side_effect = OSError("sysfs unavailable")
        with self.assertRaises(OSError):
            await self.plugin.get_status()
        result = await self.plugin.restart_gamescope()
        self.assertIn("queued", result["message"])
        self.command_mock.assert_awaited_once_with("--no-block", "restart", main.TARGET)

    async def test_inactive_session_blocks_restart(self):
        self.unit_mock.return_value = {"ActiveState": "inactive"}
        with self.assertRaises(RuntimeError):
            await self.plugin.restart_gamescope()
        self.command_mock.assert_not_awaited()

    async def test_automatic_service_busy(self):
        async def unit(name):
            return {"ActiveState": "activating" if name == main.SERVICE else "active"}
        self.unit_mock.side_effect = unit
        self.assertFalse((await self.plugin.get_status())["can_restart"])
        with self.assertRaises(RuntimeError):
            await self.plugin.restart_gamescope()
        self.command_mock.assert_not_awaited()

    async def test_command_failure_does_not_claim_success(self):
        self.command_mock.side_effect = RuntimeError("bus unavailable")
        with self.assertRaisesRegex(RuntimeError, "bus unavailable"):
            await self.plugin.restart_gamescope()
        self.assertEqual(self.plugin._cooldown_until, 0)
        self.assertFalse(self.plugin._restart_lock.locked())

    async def test_concurrent_requests_queue_only_once(self):
        results = await asyncio.gather(
            self.plugin.restart_gamescope(), self.plugin.restart_gamescope(),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, RuntimeError) for result in results), 1)
        self.command_mock.assert_awaited_once()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_is_rejected(self):
        with patch("main.os.geteuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "root"):
                await main.systemctl("show", main.TARGET)

    async def test_user_bus_and_clean_environment(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"ActiveState=active\n", b"")
        with patch("main.os.geteuid", return_value=1000), patch.dict(
            "main.os.environ", {"LD_PRELOAD": "bad", "LD_LIBRARY_PATH": "bad"}
        ), patch("main.asyncio.create_subprocess_exec", return_value=process) as spawn:
            await main.systemctl("show", main.TARGET)
            args, kwargs = spawn.call_args
            self.assertEqual(args[:2], ("/usr/bin/systemctl", "--user"))
            self.assertNotIn("LD_PRELOAD", kwargs["env"])
            self.assertNotIn("LD_LIBRARY_PATH", kwargs["env"])
            self.assertEqual(kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"],
                             "unix:path=/run/user/1000/bus")


BOLT_DEVICE = """ ● Razer Core X
   ├─ type:          peripheral
   ├─ name:          Core X
   ├─ vendor:        Razer
   ├─ uuid:          device-one
   ├─ status:        {status}
   │  ├─ domain:    domain-one
   │  └─ authflags: none
   └─ stored:        yes
      └─ policy:     auto
"""


class BoltParsingTests(unittest.TestCase):
    def test_statuses(self):
        for status in ("authorized", "connected", "disconnected", "connecting",
                       "authorizing", "auth-error", "unknown", "future-state"):
            with self.subTest(status=status):
                self.assertEqual(main.parse_bolt_devices(BOLT_DEVICE.format(status=status)), [
                    {"id": "device-one", "name": "Razer Core X", "status": status, "generation": None, "link": None, "power": None},
                ])

    def test_multiple_devices_and_color(self):
        output = BOLT_DEVICE.format(status="\x1b[32mauthorized\x1b[0m")
        output += BOLT_DEVICE.format(status="disconnected").replace("device-one", "device-two")
        devices = main.parse_bolt_devices(output)
        self.assertEqual([device["status"] for device in devices],
                         ["authorized", "disconnected"])
        self.assertEqual(devices[1]["id"], "device-two")

    def test_empty_and_host_only(self):
        self.assertEqual(main.parse_bolt_devices(""), [])
        self.assertEqual(main.parse_bolt_devices(
            BOLT_DEVICE.format(status="authorized").replace("peripheral", "host")
        ), [])

    def test_invalid_output_is_not_disconnected(self):
        for output in ("unexpected output", "type: peripheral\nname: incomplete"):
            with self.assertRaises(ValueError):
                main.parse_bolt_devices(output)

    def test_steamos_question_mark_tree_keeps_authorized_status(self):
        output = """ ? Intel TBT5 Dock
   ?? type:          peripheral
   ?? name:          TBT5 Dock
   ?? vendor:        Intel
   ?? uuid:          ac178780-002e-1ce9-ffff-ffffffffffff
   ?? generation:    USB4
   ?? status:        authorized
   ? ?? domain:    1a9b3804-b053-6472-ffff-ffffffffffff
   ? ?? rx speed:  40 Gb/s = 2 lanes * 20 Gb/s
   ? ?? tx speed:  40 Gb/s = 2 lanes * 20 Gb/s
   ? ?? authflags: none
   ?? authorized: Wed Sep 16 05:22:13 2026
   ?? connected: Wed Sep 16 05:22:11 2026
   ?? stored:        yes
      ?? policy:     auto
      ?? key:        no
"""
        self.assertEqual(main.parse_bolt_devices(output), [{
            "id": "ac178780-002e-1ce9-ffff-ffffffffffff",
            "name": "Intel TBT5 Dock",
            "status": "authorized",
            "generation": "USB4",
            "link": "40 Gb/s",
            "power": None,
        }])

    def test_asymmetric_link_and_power(self):
        output = BOLT_DEVICE.format(status="authorized")
        output += "   ├─ rx speed:  20 Gb/s = 2 lanes * 10 Gb/s\n"
        output += "   ├─ tx speed:  40 Gb/s = 2 lanes * 20 Gb/s\n"
        output += "   ├─ power:     15 W\n"
        device = main.parse_bolt_devices(output)[0]
        self.assertEqual(device["link"], "20 Gb/s RX · 40 Gb/s TX")
        self.assertEqual(device["power"], "15 W")

    def test_garbage_prefix_and_crlf_keep_authorized_status(self):
        output = (
            "\ufeff\x80 Intel TBT5 Dock\r\n"
            "xx type:          peripheral\r\n"
            "xx name:\tTBT5 Dock\r\n"
            "xx vendor:        Intel\r\n"
            "xx uuid:          ac178780-002e-1ce9-ffff-ffffffffffff\r\n"
            "xx status:        authorized\r\n"
            "xx authorized: Wed Sep 16 05:22:13 2026\r\n"
            "xx connected: Wed Sep 16 05:22:11 2026\r\n"
        )
        self.assertEqual(main.parse_bolt_devices(output), [{
            "id": "ac178780-002e-1ce9-ffff-ffffffffffff",
            "name": "Intel TBT5 Dock",
            "status": "authorized",
            "generation": None,
            "link": None,
            "power": None,
        }])

    def test_unrecognized_error_includes_output_preview(self):
        with self.assertRaisesRegex(ValueError, "NOPE_TOKEN"):
            main.parse_bolt_devices("NOPE_TOKEN garbage without fields")


class BoltCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_command_and_environment(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (
            BOLT_DEVICE.format(status="connected").encode(), b"",
        )
        with patch.dict("main.os.environ", {"LD_PRELOAD": "bad", "LD_LIBRARY_PATH": "bad"}), \
                patch("main.asyncio.create_subprocess_exec", return_value=process) as spawn:
            result = await main.Plugin().get_bolt_status()
            self.assertTrue(result["available"])
            self.assertEqual(result["devices"][0]["status"], "connected")
            args, kwargs = spawn.call_args
            self.assertEqual(args, ("/usr/bin/boltctl", "list"))
            self.assertEqual(kwargs["env"]["LC_ALL"], "C.UTF-8")
            self.assertNotIn("LD_PRELOAD", kwargs["env"])
            self.assertNotIn("LD_LIBRARY_PATH", kwargs["env"])

    async def test_missing_tool(self):
        with patch("main.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await main.bolt_status()
            self.assertFalse(result["available"])
            self.assertIn("not installed", result["error"])

    async def test_daemon_failure(self):
        process = AsyncMock()
        process.returncode = 1
        process.communicate.return_value = (b"", b"Could not connect to boltd")
        with patch("main.asyncio.create_subprocess_exec", return_value=process):
            result = await main.bolt_status()
            self.assertFalse(result["available"])
            self.assertIn("boltd", result["error"])

    async def test_timeout_kills_child(self):
        process = AsyncMock()
        process.returncode = None
        from unittest.mock import Mock
        process.kill = Mock()
        process.communicate.side_effect = [asyncio.TimeoutError(), (b"", b"")]
        with patch("main.asyncio.create_subprocess_exec", return_value=process):
            result = await main.bolt_status()
            self.assertFalse(result["available"])
            self.assertIn("timed out", result["error"])
            process.kill.assert_called_once()

    async def test_unrecognized_output_reports_error(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"unsupported output", b"")
        with patch("main.asyncio.create_subprocess_exec", return_value=process):
            result = await main.bolt_status()
            self.assertFalse(result["available"])
            self.assertIn("Unrecognized", result["error"])

    async def test_authorized_refresh_runs_lspci(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"00:00.0", b"")
        with patch("main.asyncio.create_subprocess_exec", return_value=process) as spawn:
            result = await main.pci_scan()
            self.assertTrue(result["ran"])
            self.assertIsNone(result["error"])
            self.assertIn("00:00.0", result["output"])
            args, kwargs = spawn.call_args
            self.assertEqual(args, ("/usr/bin/lspci", "-Dnn"))
            self.assertNotIn("LD_PRELOAD", kwargs["env"])

    async def test_authorized_devices_trigger_pci_scan(self):
        with patch.object(main, "bolt_status", new=AsyncMock(return_value={
            "available": True,
            "devices": [{"id": "one", "name": "Dock", "status": "authorized", "link": "40 Gb/s", "power": None}],
            "error": None,
        })), patch.object(main, "pci_scan", new=AsyncMock(return_value={"ran": True, "error": None})) as scan, \
                patch.object(main, "gpu_status", return_value=[{"address": "gpu", "driver": "amdgpu", "name": "GPU"}]):
            result = await main.Plugin().get_bolt_status()
            scan.assert_awaited_once()
            self.assertEqual(result["pci_scan"], {"ran": True, "error": None})
            self.assertTrue(result["gpu_ready"])
            self.assertTrue(result["gpu_on_pci"])

    async def test_unauthorised_devices_do_not_scan_pci(self):
        for status in ("connected", "disconnected", "authorizing"):
            with patch.object(main, "bolt_status", new=AsyncMock(return_value={
                "available": True,
                "devices": [{"id": "one", "name": "Dock", "status": status, "link": None, "power": None}],
                "error": None,
            })), patch.object(main, "pci_scan", new=AsyncMock()) as scan, \
                    patch.object(main, "gpu_status", return_value=[]):
                result = await main.Plugin().get_bolt_status()
                scan.assert_not_awaited()
                self.assertIsNone(result["pci_scan"])
                self.assertFalse(result["gpu_on_pci"])

    async def test_lspci_failure_does_not_drop_bolt_status(self):
        with patch.object(main, "bolt_status", new=AsyncMock(return_value={
            "available": True,
            "devices": [{"id": "one", "name": "Dock", "status": "authorized", "link": None, "power": None}],
            "error": None,
        })), patch.object(main, "pci_scan", new=AsyncMock(return_value={
            "ran": False, "error": "lspci is not installed.",
        })), patch.object(main, "gpu_status", return_value=[]):
            result = await main.Plugin().get_bolt_status()
            self.assertTrue(result["available"])
            self.assertEqual(result["devices"][0]["status"], "authorized")
            self.assertEqual(result["pci_scan"]["error"], "lspci is not installed.")
            self.assertFalse(result["gpu_ready"])

    async def test_missing_sysfs_leaves_gpu_ready_unknown(self):
        with patch.object(main, "bolt_status", new=AsyncMock(return_value={
            "available": True,
            "devices": [{"id": "one", "name": "Dock", "status": "authorized", "link": None, "power": None}],
            "error": None,
        })), patch.object(main, "pci_scan", new=AsyncMock(return_value={"ran": True, "error": None})), \
                patch.object(main, "gpu_status", side_effect=FileNotFoundError):
            result = await main.Plugin().get_bolt_status()
            self.assertIsNone(result["gpu_ready"])
            self.assertIsNone(result["gpu_on_pci"])

    async def test_lspci_timeout_kills_child(self):
        process = AsyncMock()
        process.returncode = None
        from unittest.mock import Mock
        process.kill = Mock()
        process.communicate.side_effect = [asyncio.TimeoutError(), (b"", b"")]
        with patch("main.asyncio.create_subprocess_exec", return_value=process):
            result = await main.pci_scan()
            self.assertFalse(result["ran"])
            self.assertIn("timed out", result["error"])
            process.kill.assert_called_once()


def make_backlight(root, name, brightness, maximum, vendor="0x1002", product="0x15bf"):
    node = Path(root) / "class" / name
    device = Path(root) / "pci" / name
    device.mkdir(parents=True)
    node.mkdir(parents=True)
    (device / "vendor").write_text(f"{vendor}\n")
    (device / "device").write_text(f"{product}\n")
    (node / "device").symlink_to(device)
    (node / "brightness").write_text(f"{brightness}\n")
    (node / "actual_brightness").write_text(f"{brightness}\n")
    (node / "max_brightness").write_text(f"{maximum}\n")
    return node


class ShortNameTests(unittest.TestCase):
    def test_sku_from_family_string(self):
        self.assertEqual(
            main.short_gpu_name(
                "Advanced Micro Devices, Inc. [AMD/ATI] Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"
            ),
            "RX 6800 XT",
        )
        self.assertEqual(main.short_gpu_name("AMD Radeon RX 6950 XT"), "RX 6950 XT")
        self.assertEqual(main.short_gpu_name("Navi 21 [Radeon RX 6800 XT]"), "RX 6800 XT")


class BacklightTests(unittest.TestCase):
    def test_skips_egpu_backlight(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            internal = make_backlight(root, "amdgpu_bl0", 180, 255)
            make_backlight(root, "amdgpu_bl1", 200, 255, product="0x73ff")
            with patch.object(main, "BACKLIGHT", root / "class"):
                self.assertEqual(main.internal_backlight_node(), internal)
                status = main.backlight_status({"backlight_off": False, "saved_brightness": None})
                self.assertTrue(status["available"])
                self.assertEqual(status["node"], "amdgpu_bl0")
                self.assertEqual(status["brightness"], 180)

    def test_missing_node(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, "BACKLIGHT", Path(folder) / "missing"):
                status = main.backlight_status()
                self.assertFalse(status["available"])
                self.assertEqual(status["error"], "No internal backlight node")

    def test_save_restore_and_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node = make_backlight(root, "amdgpu_bl0", 180, 255)
            settings = root / "settings"
            settings.mkdir()
            with patch.object(main, "BACKLIGHT", root / "class"), patch.dict(
                "os.environ", {"DECKY_PLUGIN_SETTINGS_DIR": str(settings)}
            ):
                off = main.apply_backlight(True)
                self.assertTrue(off["backlight_off"])
                self.assertEqual(off["saved_brightness"], 180)
                self.assertEqual((node / "brightness").read_text().strip(), "0")
                stored = json.loads((settings / "settings.json").read_text())
                self.assertTrue(stored["backlight_off"])
                self.assertEqual(stored["saved_brightness"], 180)
                on = main.apply_backlight(False)
                self.assertFalse(on["backlight_off"])
                self.assertEqual((node / "brightness").read_text().strip(), "180")


class BacklightPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_error_does_not_restart(self):
        plugin = main.Plugin()
        plugin._settings = {"backlight_off": False, "saved_brightness": 180}
        with patch.object(main, "apply_backlight", side_effect=PermissionError("denied")), patch.object(
            main, "backlight_status", return_value={
                "available": True, "off": False, "brightness": 180, "max": 255,
                "saved": 180, "node": "amdgpu_bl0", "error": None,
            }
        ), patch.object(main, "systemctl", new=AsyncMock()) as command:
            result = await plugin.set_backlight_off(True)
            self.assertFalse(result["available"])
            self.assertIn("denied", result["error"])
            command.assert_not_awaited()



class AutofixTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.files = main.autofix_files(self.home)
        self.script, self.unit = (path for path, _, _ in self.files)

    def test_script_matches_every_supported_gpu(self):
        script = self.files[0][1]
        self.assertIn('EGPU_ID="1002:(73ff|7550)"', script)
        self.assertNotIn("@EGPU_ID@", script)
        self.assertEqual(main.managed_state(self.files), "missing")
        main.write_managed(self.files)
        self.assertEqual(main.managed_state(self.files), "current")
        self.assertTrue(self.script.stat().st_mode & 0o100)

    def test_hand_installed_script_is_backed_up_once(self):
        self.script.parent.mkdir(parents=True)
        self.script.write_text('EGPU_ID="1002:73ff"\n')
        self.assertEqual(main.managed_state(self.files), "foreign")
        main.remove_managed(self.files)
        self.assertTrue(self.script.exists())
        main.write_managed(self.files)
        backup = self.script.with_name(self.script.name + ".bak")
        self.assertEqual(backup.read_text(), 'EGPU_ID="1002:73ff"\n')
        self.assertEqual(main.managed_state(self.files), "current")

    def test_managed_copy_is_outdated_then_removed(self):
        main.write_managed(self.files)
        self.script.write_text(main.MANAGED + "\nold ids\n")
        self.assertEqual(main.managed_state(self.files), "outdated")
        main.write_managed(self.files)
        self.assertFalse(self.script.with_name(self.script.name + ".bak").exists())
        main.remove_managed(self.files)
        self.assertFalse(self.script.exists() or self.unit.exists())
        self.assertEqual(main.managed_state(self.files), "missing")


class AutofixPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plugin = main.Plugin()
        self.plugin._home = Path(folder.name)
        self.files = main.autofix_files(self.plugin._home)
        self.command = patch.object(main, "systemctl", new=AsyncMock(return_value=""))
        self.command_mock = self.command.start()
        self.unit = patch.object(main, "unit_status", new=AsyncMock(
            return_value={"LoadState": "loaded", "UnitFileState": "enabled"},
        ))
        self.unit.start()
        self.addCleanup(patch.stopall)

    async def test_install_enables_without_starting(self):
        status = await self.plugin.install_autofix()
        self.assertEqual(status, {"state": "current", "enabled": True})
        calls = [call.args for call in self.command_mock.await_args_list]
        self.assertEqual(calls, [("daemon-reload",), ("enable", main.SERVICE)])

    async def test_remove_disables_and_deletes_managed_files(self):
        await self.plugin.install_autofix()
        self.command_mock.reset_mock()
        status = await self.plugin.remove_autofix()
        calls = [call.args for call in self.command_mock.await_args_list]
        self.assertEqual(calls, [("disable", main.SERVICE), ("daemon-reload",)])
        self.assertFalse(any(path.exists() for path, _, _ in self.files))
        self.assertEqual(status["state"], "missing")

    async def test_remove_refuses_hand_installed_script(self):
        script = self.files[0][0]
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\n")
        with self.assertRaises(RuntimeError):
            await self.plugin.remove_autofix()
        self.command_mock.assert_not_awaited()

    async def test_load_syncs_only_an_outdated_managed_copy(self):
        script = self.files[0][0]
        await self.plugin._main()
        self.assertFalse(script.exists())
        script.parent.mkdir(parents=True)
        script.write_text("hand-installed\n")
        await self.plugin._main()
        self.assertEqual(script.read_text(), "hand-installed\n")
        self.assertFalse(script.with_name(script.name + ".bak").exists())
        script.unlink()
        main.write_managed(self.files)
        script.write_text(main.MANAGED + "\n")
        await self.plugin._main()
        await self.plugin._unload()
        self.assertEqual(main.managed_state(self.files), "current")


class SleepHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.files = main.sleep_hook_files(self.root)

    def test_script_matches_supported_gpus_only(self):
        script = self.files[0][1]
        self.assertIn("EGPU_ID='^0x1002:0x(73ff|7550)$'", script)
        self.files[0][0].parent.mkdir(parents=True)
        main.write_managed(self.files)
        path = str(self.files[0][0])
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        pattern = script.split("EGPU_ID='", 1)[1].split("'", 1)[0]
        for gpu, matches in (("0x7550", True), ("0x73ff", True), ("0x150e", False)):
            probe = f'[[ "0x1002:{gpu}" =~ {pattern} ]]'
            self.assertEqual(subprocess.run(["bash", "-c", probe]).returncode == 0, matches, gpu)

    def load_installer(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sleep_install", main.SLEEP_HOOK_DIR / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        return installer

    def test_installer_installs_and_removes(self):
        installer = self.load_installer()
        calls = []
        with patch.object(installer, "sleep_hook_files", return_value=self.files), \
                patch.object(installer.subprocess, "run", side_effect=lambda args, **_: calls.append(args[1:])):
            hand = self.files[0][0]
            hand.parent.mkdir(parents=True)
            hand.write_text("#!/bin/bash\n")
            installer.install()
            self.assertEqual(main.managed_state(self.files), "current")
            self.assertTrue(hand.with_name(hand.name + ".bak").exists())
            installer.remove()
        self.assertEqual(calls, [["daemon-reload"], ["enable", main.SLEEP_SERVICE], ["pm", "on"],
                                 ["disable", main.SLEEP_SERVICE], ["pm", "auto"], ["daemon-reload"]])
        self.assertFalse(any(path.exists() for path, _, _ in self.files))

    def test_keep_dock_marker_follows_installer(self):
        installer = self.load_installer()
        script = self.files[0][0]
        marker = script.with_name(main.KEEP_DOCK_NAME)
        with patch.object(installer, "sleep_hook_files", return_value=self.files), \
                patch.object(installer.subprocess, "run"):
            with self.assertRaises(SystemExit):
                installer.keep_dock(True)
            installer.install()
            installer.keep_dock(True)
            self.assertTrue(marker.is_file())
            installer.keep_dock(False)
            self.assertFalse(marker.exists())
            installer.keep_dock(True)
            installer.remove()
        self.assertFalse(script.parent.exists())

    def test_hook_reads_marker_next_to_itself(self):
        self.assertIn(f'KEEP_DOCK="${{0%/*}}/{main.KEEP_DOCK_NAME}"', self.files[0][1])

    def test_udev_rule_runs_installed_script(self):
        script = "/" + str(self.files[0][0].relative_to(self.root))
        rule = next(content for path, content, _ in self.files if path.suffix == ".rules")
        self.assertIn(f'RUN+="{script} pm on"', rule)

    async def test_wrong_password_is_reported(self):
        plugin = main.Plugin()
        process = AsyncMock(returncode=1)
        process.communicate.return_value = (b"", b"sudo: 1 incorrect password attempt\n")
        with patch.object(main, "sleep_hook_files", return_value=self.files), \
                patch.object(main, "system_unit_enabled", new=AsyncMock(return_value=False)), \
                patch.object(main, "SUDO", Path("/bin/sh")), \
                patch.object(main.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            status = await plugin.set_sleep_hook(True, "hunter2")
            self.assertTrue(status["busy"])
            await plugin._sleep_task
            status = await plugin.get_sleep_hook_status()
        self.assertEqual(status["error"], "Wrong password. You can try again.")
        self.assertEqual(process.communicate.await_args.args, (b"hunter2\n",))
        self.assertEqual(spawn.await_args.args[:2], ("/bin/sh", "-S"))
        self.assertEqual(status["state"], "missing")
        self.assertNotIn("--remove", spawn.await_args.args)

    async def test_password_is_required(self):
        plugin = main.Plugin()
        for password in (None, "", "a\nb"):
            with self.subTest(password=password), self.assertRaises(ValueError):
                await plugin.set_sleep_hook(True, password)

    async def test_keep_dock_needs_current_hook(self):
        plugin = main.Plugin()
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b"", b"")
        with patch.object(main, "sleep_hook_files", return_value=self.files), \
                patch.object(main, "system_unit_enabled", new=AsyncMock(return_value=True)), \
                patch.object(main, "SUDO", Path("/bin/sh")), \
                patch.object(main.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            status = await plugin.set_keep_dock(True, "hunter2")
            self.assertFalse(status["busy"])
            spawn.assert_not_awaited()
            self.files[0][0].parent.mkdir(parents=True)
            main.write_managed(self.files)
            status = await plugin.set_keep_dock(True, "hunter2")
            self.assertTrue(status["busy"])
            await plugin._sleep_task
        self.assertEqual(spawn.await_args.args[-2:], ("--keep-dock", "on"))
        self.assertFalse(status["keep_dock"])


class AutoSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gpus = []
        self.game = False
        self.now = 100.0
        self.plugin = main.Plugin()
        self.plugin._settings = {"auto_switch": True}
        self.restart = AsyncMock()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.marker = Path(folder.name) / "egpu-gamescope-restarted"
        patches = [
            patch.object(main, "gpu_status", side_effect=lambda: list(self.gpus)),
            patch.object(main, "game_running", side_effect=lambda: self.game),
            patch.object(main, "time", SimpleNamespace(monotonic=lambda: self.now, time=time.time)),
            patch.object(main, "autofix_marker", return_value=self.marker),
            patch.object(main, "unit_status", new=AsyncMock(return_value={"ActiveState": "inactive"})),
            patch.object(self.plugin, "restart_gamescope", new=self.restart),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def tick(self, seconds=2):
        self.now += seconds
        await self.plugin._auto_switch_tick()

    def plug(self, node=1):
        self.gpus = [{"address": "0000:05:00.0", "node": node, "driver": "amdgpu", "name": "RX 6600"}]

    async def test_arrival_restarts_once_after_settling(self):
        await self.tick()
        self.plug()
        await self.tick()
        self.restart.assert_not_awaited()
        await self.tick(main.AUTO_SWITCH_SETTLE_SEC)
        await self.tick()
        self.restart.assert_awaited_once()

    async def test_readd_between_polls_is_an_arrival(self):
        # Across sleep the hook removes the eGPU and it returns before the next poll.
        self.plug(node=1)
        await self.tick()
        self.plug(node=2)
        await self.tick()
        await self.tick(main.AUTO_SWITCH_SETTLE_SEC)
        self.restart.assert_awaited_once()

    async def test_gpu_already_up_at_load_is_not_an_arrival(self):
        self.plug()
        for _ in range(5):
            await self.tick()
        self.restart.assert_not_awaited()

    async def test_waits_for_running_game_to_exit(self):
        await self.tick()
        self.plug()
        self.game = True
        await self.tick()
        await self.tick(10)
        self.restart.assert_not_awaited()
        self.assertTrue((await self.plugin.get_auto_switch())["waiting"])
        self.game = False
        await self.tick()
        self.restart.assert_awaited_once()

    async def test_arrival_while_disabled_is_dropped(self):
        self.plugin._settings = {"auto_switch": False}
        await self.tick()
        self.plug()
        await self.tick()
        await self.tick(10)
        self.plugin._settings = {"auto_switch": True}
        await self.tick()
        self.restart.assert_not_awaited()

    async def test_leaves_boot_arrival_to_running_autofix(self):
        main.unit_status.return_value = {"ActiveState": "activating"}
        await self.tick()
        self.plug()
        await self.tick()
        await self.tick(10)
        await self.tick()
        self.restart.assert_not_awaited()

    async def test_leaves_arrival_to_autofix_that_just_restarted(self):
        await self.tick()
        self.plug()
        await self.tick()
        self.marker.touch()
        await self.tick(10)
        self.restart.assert_not_awaited()

    async def test_old_autofix_marker_does_not_block_hotplug(self):
        self.marker.touch()
        stale = time.time() - main.AUTOFIX_GRACE_SEC - 60
        os.utime(self.marker, (stale, stale))
        await self.tick()
        self.plug()
        await self.tick()
        await self.tick(10)
        self.restart.assert_awaited_once()

    async def test_setting_persists(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict("os.environ", {"DECKY_PLUGIN_SETTINGS_DIR": folder}):
            self.assertEqual(await self.plugin.set_auto_switch(False), {"enabled": False, "waiting": False})
            self.assertFalse(main.load_settings()["auto_switch"])
            await self.plugin.set_auto_switch(True)
            self.assertTrue(main.load_settings()["auto_switch"])


class GameDetectionTests(unittest.TestCase):
    def test_game_detection_reads_process_command_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            (proc / "12").mkdir()
            (proc / "12" / "cmdline").write_bytes(b"steamwebhelper\0")
            self.assertFalse(main.game_running(proc))
            (proc / "34").mkdir()
            (proc / "34" / "cmdline").write_bytes(b"reaper\0SteamLaunch\0AppId=570\0")
            self.assertTrue(main.game_running(proc))


class HintTests(unittest.IsolatedAsyncioTestCase):
    def test_display_follows_enabled_connector(self):
        with tempfile.TemporaryDirectory() as folder:
            connector = Path(folder) / "0000:05:00.0/drm/card1/card1-DP-1"
            connector.mkdir(parents=True)
            (connector / "enabled").write_text("disabled\n")
            with patch.object(main, "PCI_DEVICES", Path(folder)):
                self.assertFalse(main.drives_display("0000:05:00.0"))
                (connector / "enabled").write_text("enabled\n")
                self.assertTrue(main.drives_display("0000:05:00.0"))
                self.assertFalse(main.drives_display("0000:64:00.0"))

    async def test_usb_fallback_only_without_authorized_dock(self):
        with tempfile.TemporaryDirectory() as folder:
            hub = Path(folder) / "3-1.4"
            hub.mkdir()
            (hub / "product").write_text("TBT5\n")
            bolt = {"available": True, "devices": [{"status": "disconnected"}], "error": None}
            plugin = main.Plugin()
            with patch.object(main, "USB_DEVICES", Path(folder)), \
                    patch.object(main, "bolt_status", new=AsyncMock(side_effect=lambda: dict(bolt))), \
                    patch.object(main, "gpu_status", return_value=[]):
                self.assertTrue((await plugin.get_bolt_status())["usb_fallback"])
                (hub / "product").write_text("USB2.0 Hub\n")
                self.assertFalse((await plugin.get_bolt_status())["usb_fallback"])
