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

    async def test_unplug_or_unbound_blocks_restart(self):
        for devices in ([], [{"address": "gpu", "driver": None}]):
            self.gpu_mock.return_value = devices
            with self.assertRaises(RuntimeError):
                await self.plugin.restart_gamescope()
        self.command_mock.assert_not_awaited()

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
