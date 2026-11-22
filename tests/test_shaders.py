import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main

CONFIG = """"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"ShaderCacheManager"
\t\t\t\t{
\t\t\t\t\t"CurrentBucketGPU"\t\t"NAVI23;NAVI23;AMD RADV NAVI23"
\t\t\t\t}
\t\t\t\t"Other"\t\t"kept"
\t\t\t}
\t\t}
\t}
}
"""


def load_script(home):
    script = next(content for path, content, _ in main.shader_files(home) if path.suffix == ".py")
    path = home / "egpu_shader_swap.py"
    path.write_text(script)
    spec = importlib.util.spec_from_file_location("egpu_shader_swap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShaderSwapTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.swap = load_script(self.home)
        steam = self.home / ".local/share/Steam"
        self.steamapps = steam / "steamapps"
        (self.steamapps / "shadercache/1243830").mkdir(parents=True)
        (self.steamapps / "shadercache/1243830/fozpipelinesv6").write_text("built on RX 6600")
        self.config = steam / "config/config.vdf"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(CONFIG)
        self.pci = self.home / "pci"
        self.pci.mkdir()
        pci_ids = self.home / "pci.ids"
        pci_ids.write_text(
            "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
            "# Used in the Steam Deck OLED\n"
            "\t150e  Strix [Radeon 880M / 890M]\n"
            "\t73ff  Navi 23 [Radeon RX 6600/6600 XT/6600M]\n"
            "\t\t1da2 e448  Radeon RX 6600\n"
            "\t7550  Navi 48 [Radeon RX 9070/9070 XT/9070 GRE]\n"
            "10de  NVIDIA Corporation\n"
            "\t73ff  Navi 99 not AMD\n"
        )
        for name, value in (("STEAM", steam), ("STATE", self.home / "state"), ("PCI_DEVICES", self.pci),
                            ("PCI_IDS", pci_ids)):
            patcher = patch.object(self.swap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        runtime = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.home)})
        runtime.start()
        self.addCleanup(runtime.stop)
        igpu = self.pci / "0000:64:00.0"
        igpu.mkdir()
        (igpu / "class").write_text("0x038000\n")
        (igpu / "vendor").write_text("0x1002\n")
        (igpu / "device").write_text("0x150e\n")
        self.running = patch.object(self.swap, "steam_running", return_value=False)
        self.running.start()
        self.addCleanup(self.running.stop)

    def set_egpu(self, ready):
        device = self.pci / "0000:05:00.0"
        device.mkdir(exist_ok=True)
        (device / "vendor").write_text("0x1002\n")
        (device / "device").write_text("0x73ff\n")
        (device / "class").write_text("0x030000\n")
        driver = device / "driver"
        if driver.is_symlink():
            driver.unlink()
        if ready:
            (self.pci / "amdgpu").mkdir(exist_ok=True)
            driver.symlink_to(self.pci / "amdgpu")

    def cache_link(self):
        return os.readlink(self.steamapps / "shadercache")

    def test_first_start_on_igpu_files_existing_cache_under_the_egpu_it_was_built_on(self):
        self.set_egpu(ready=False)
        self.swap.swap()
        self.assertEqual(self.cache_link(), "shadercache.igpu")
        self.assertEqual((self.steamapps / "shadercache.egpu-73ff/1243830/fozpipelinesv6").read_text(), "built on RX 6600")
        self.assertEqual(list((self.steamapps / "shadercache.igpu").iterdir()), [])
        # No saved iGPU bucket yet: config stays, and Steam processes shaders for the iGPU once.
        self.assertEqual(self.config.read_text(), CONFIG)

    def test_switching_back_and_forth_keeps_each_gpus_bucket(self):
        self.set_egpu(ready=True)
        self.swap.swap()
        self.assertEqual(self.cache_link(), "shadercache.egpu-73ff")
        self.set_egpu(ready=False)
        self.swap.swap()
        igpu_config = CONFIG.replace("NAVI23;NAVI23;AMD RADV NAVI23", "GFX1150")
        self.config.write_text(igpu_config)  # Steam rebuilt its bucket for the iGPU
        self.set_egpu(ready=True)
        self.swap.swap()
        self.assertEqual(self.cache_link(), "shadercache.egpu-73ff")
        self.assertEqual(self.config.read_text(), CONFIG)
        self.assertIn('"Other"\t\t"kept"', self.config.read_text())
        self.set_egpu(ready=False)
        self.swap.swap()
        self.assertEqual(self.config.read_text(), igpu_config)
        self.assertEqual(self.config.with_name("config.vdf.egpu-bak").read_text(), CONFIG)

    def test_pins_steam_to_the_session_gpu(self):
        environment = self.home / "egpu-helper-environment"
        self.set_egpu(ready=True)
        self.swap.swap()
        self.assertEqual(environment.read_text(), "DRI_PRIME=pci-0000_05_00_0\n")
        self.set_egpu(ready=False)
        self.swap.swap()
        self.assertEqual(environment.read_text(), "DRI_PRIME=pci-0000_64_00_0\n")

    def test_custom_symlink_is_left_alone(self):
        (self.steamapps / "shadercache/1243830/fozpipelinesv6").unlink()
        (self.steamapps / "shadercache/1243830").rmdir()
        (self.steamapps / "shadercache").rmdir()
        (self.steamapps / "shadercache").symlink_to("/mnt/fast/shadercache")
        self.swap.swap()
        self.assertEqual(self.cache_link(), "/mnt/fast/shadercache")

    def test_does_nothing_while_steam_runs(self):
        with patch.object(self.swap, "steam_running", return_value=True):
            self.swap.swap()
        self.assertFalse((self.steamapps / "shadercache").is_symlink())
        self.assertFalse((self.home / "state/active").exists())

    def test_restore_turns_active_cache_back_into_a_folder(self):
        self.set_egpu(ready=True)
        self.swap.swap()
        self.swap.restore()
        cache = self.steamapps / "shadercache"
        self.assertFalse(cache.is_symlink())
        self.assertEqual((cache / "1243830/fozpipelinesv6").read_text(), "built on RX 6600")


class ShaderTogglePluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plugin = main.Plugin()
        self.plugin._home = Path(folder.name)
        self.command = patch.object(main, "systemctl", new=AsyncMock(return_value=""))
        self.command_mock = self.command.start()
        self.addCleanup(patch.stopall)

    async def test_toggle_writes_and_removes_the_session_dropin_without_restarting(self):
        status = await self.plugin.set_shader_swap(True)
        self.assertEqual(status, {"state": "current"})
        dropin = self.plugin._home / ".config/systemd/user/gamescope-session.service.d" / main.SHADER_DROPIN
        self.assertIn("ExecStartPre=-/usr/bin/timeout 3 /usr/bin/python3", dropin.read_text())
        steam = self.plugin._home / ".config/systemd/user/steam-launcher.service.d" / main.STEAM_DROPIN
        self.assertIn("EnvironmentFile=-%t/egpu-helper-environment", steam.read_text())
        script = (self.plugin._home / ".local/bin/egpu-shader-swap.py").read_text()
        self.assertIn('"73ff,7550"', script)
        status = await self.plugin.set_shader_swap(False)
        self.assertEqual(status, {"state": "missing"})
        calls = [call.args for call in self.command_mock.await_args_list]
        self.assertEqual(calls, [("daemon-reload",), ("daemon-reload",)])

    async def test_load_adds_a_file_that_an_update_introduced(self):
        await self.plugin.set_shader_swap(True)
        steam = self.plugin._home / ".config/systemd/user/steam-launcher.service.d" / main.STEAM_DROPIN
        steam.unlink()
        await self.plugin._main()
        await self.plugin._unload()
        self.assertEqual(await self.plugin.get_shader_status(), {"state": "current"})

class ShaderManageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.plugin = main.Plugin()
        self.plugin._home = home = Path(folder.name)
        steamapps = home / ".local/share/Steam/steamapps"
        self.steamapps = steamapps
        for profile, appid, size in (("egpu-73ff", "1243830", 3000), ("igpu", "1243830", 1000), ("egpu-73ff", "589360", 500)):
            game = steamapps / f"shadercache.{profile}" / appid / "fozpipelinesv6"
            game.mkdir(parents=True)
            (game / "steamapps.foz").write_bytes(b"x" * size)
        (steamapps / "shadercache").symlink_to("shadercache.egpu-73ff")
        (steamapps / "appmanifest_1243830.acf").write_text('"AppState"\n{\n\t"appid"\t\t"1243830"\n\t"name"\t\t"Cyberpunk 2077"\n}\n')
        state = home / ".local/share/egpu-helper/shaders"
        state.mkdir(parents=True)
        (state / "active").write_text("egpu-73ff\n")

    async def test_info_lists_sizes_per_gpu_and_game(self):
        info = await self.plugin.get_shader_info()
        self.assertEqual(info["active"], "egpu-73ff")
        self.assertEqual([(p["id"], p["bytes"]) for p in info["profiles"]], [("egpu-73ff", 3500), ("igpu", 1000)])
        self.assertEqual(info["profiles"][1]["label"], "Built-in GPU")
        self.assertEqual(info["games"][0], {"appid": "1243830", "name": "Cyberpunk 2077", "sizes": {"egpu-73ff": 3000, "igpu": 1000}})
        self.assertEqual(info["games"][1]["name"], "App 589360")

    async def test_delete_removes_only_inactive_copies(self):
        result = await self.plugin.delete_inactive_shaders("1243830")
        self.assertEqual(result, {"freed": 1000})
        self.assertFalse((self.steamapps / "shadercache.igpu/1243830").exists())
        self.assertTrue((self.steamapps / "shadercache.egpu-73ff/1243830").is_dir())

    async def test_delete_rejects_paths(self):
        for appid in ("../igpu", "", "1243830/fozpipelinesv6"):
            with self.subTest(appid=appid), self.assertRaises(ValueError):
                await self.plugin.delete_inactive_shaders(appid)

    async def test_symlink_not_active_file_decides_what_is_in_use(self):
        (self.plugin._home / ".local/share/egpu-helper/shaders/active").write_text("igpu\n")
        info = await self.plugin.get_shader_info()
        self.assertEqual(info["active"], "egpu-73ff")
        await self.plugin.delete_inactive_shaders("1243830")
        self.assertTrue((self.steamapps / "shadercache.egpu-73ff/1243830").is_dir())
        self.assertFalse((self.steamapps / "shadercache.igpu/1243830").exists())

    async def test_delete_does_nothing_without_a_managed_symlink(self):
        (self.steamapps / "shadercache").unlink()
        self.assertEqual(await self.plugin.delete_inactive_shaders("1243830"), {"freed": 0})
        self.assertTrue((self.steamapps / "shadercache.igpu/1243830").is_dir())
