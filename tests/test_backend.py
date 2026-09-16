import asyncio
import json
from pathlib import Path
import tempfile
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

