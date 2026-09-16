import asyncio
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
                driver = root / "amdgpu"
                driver.mkdir()
                (device / "driver").symlink_to(driver)
                self.assertEqual(main.gpu_status()[0]["driver"], "amdgpu")
                (device / "device").write_text("0x9999")
                self.assertEqual(main.gpu_status(), [])

    def test_missing_sysfs_is_not_disconnected(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, "PCI_DEVICES", Path(folder) / "missing"):
                with self.assertRaises(FileNotFoundError):
                    main.gpu_status()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = main.Plugin()
        self.gpu = patch.object(main, "gpu_status", return_value=[
            {"address": "0000:03:00.0", "driver": "amdgpu"},
        ])
        self.gpu_mock = self.gpu.start()
        self.unit = patch.object(main, "unit_status", new=AsyncMock(
            return_value={"LoadState": "loaded", "ActiveState": "active"},
        ))
        self.unit_mock = self.unit.start()
        self.command = patch.object(main, "systemctl", new=AsyncMock(return_value=""))
        self.command_mock = self.command.start()
        self.addCleanup(patch.stopall)

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
                    {"id": "device-one", "name": "Razer Core X", "status": status},
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
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
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
