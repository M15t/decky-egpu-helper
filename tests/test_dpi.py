import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main
from zapret2 import firewall, prepare

with patch.dict(sys.modules, {"prepare": prepare}):
    spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "zapret2/install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)


def fields(state="active", pid="123", loaded="loaded", sub="running"):
    return f"LoadState={loaded}\nActiveState={state}\nSubState={sub}\nMainPID={pid}\nResult=success\n"


class DpiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.marker = Path(self.directory.name) / "enabled"
        marker_patch = patch.object(main, "DPI_ENABLED_FILE", self.marker)
        marker_patch.start()
        self.addCleanup(marker_patch.stop)

    async def test_on_survives_new_plugin_and_runtime_stop(self):
        with patch.object(main, "dpi_systemctl", AsyncMock(side_effect=["", fields()])):
            status = await main.Plugin().set_dpi_enabled(True)
        self.assertTrue(status["requested"])
        self.assertTrue(self.marker.is_file())
        # Service stopping at shutdown does not change the saved selection.
        with patch.object(main, "dpi_systemctl", AsyncMock(return_value=fields(state="inactive", pid="0", sub="dead"))):
            restored = await main.Plugin().get_dpi_status()
        self.assertTrue(restored["requested"])
        self.assertFalse(restored["enabled"])
        self.assertIn("not running", restored["error"])

    async def test_off_removes_boot_preference_even_if_stop_fails(self):
        self.marker.touch()
        with patch.object(main, "dpi_systemctl", AsyncMock(side_effect=[RuntimeError("Denied"), fields()])):
            status = await main.Plugin().set_dpi_enabled(False)
        self.assertFalse(self.marker.exists())
        self.assertFalse(status["requested"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["error"], "Denied")

    async def test_start_failure_keeps_on_preference_and_reports_failure(self):
        with patch.object(main, "dpi_systemctl", AsyncMock(side_effect=[RuntimeError("Failed"), fields(state="failed", pid="0", sub="failed")])):
            status = await main.Plugin().set_dpi_enabled(True)
        self.assertTrue(self.marker.exists())
        self.assertTrue(status["requested"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["error"], "Failed")

    async def test_preference_write_failure_does_not_start_service(self):
        with patch.object(Path, "open", side_effect=PermissionError("Read only")), patch.object(main, "dpi_systemctl", AsyncMock(return_value=fields(state="inactive", pid="0", sub="dead"))) as control:
            status = await main.Plugin().set_dpi_enabled(True)
        control.assert_awaited_once_with("show")
        self.assertEqual(status["error"], "Read only")

    async def test_old_helper_requires_install_update(self):
        with patch.object(main, "DPI_ENABLED_FILE", self.marker / "missing"), patch.object(main, "dpi_systemctl", AsyncMock(return_value=fields())):
            status = await main.dpi_status()
        self.assertFalse(status["available"])
        self.assertIn("update", status["error"])

    async def test_status_requires_live_process(self):
        for output, enabled, available in ((fields(), True, True), (fields(pid="0"), False, True),
                                           (fields(state="inactive", sub="dead"), False, True),
                                           (fields(loaded="not-found"), False, False)):
            with self.subTest(output=output), patch.object(main, "dpi_systemctl", AsyncMock(return_value=output)):
                status = await main.dpi_status()
                self.assertEqual((status["enabled"], status["available"]), (enabled, available))

    async def test_unreadable_status_is_unknown(self):
        with patch.object(main, "dpi_systemctl", AsyncMock(side_effect=RuntimeError("No bus"))):
            status = await main.dpi_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "unknown")
        self.assertEqual(status["error"], "No bus")

    async def test_control_fixed_system_unit_and_clean_environment(self):
        process = Mock(returncode=0, communicate=AsyncMock(return_value=(b"ok", b"")))
        with patch.object(main.os, "geteuid", return_value=1000), patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            await main.dpi_systemctl("start")
        self.assertEqual(spawn.call_args.args, ("/usr/bin/systemctl", "--system", "--no-ask-password", "start", "decky-zapret2.service"))
        self.assertEqual(spawn.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})

    async def test_missing_unit_show_still_allows_install_status(self):
        process = Mock(returncode=1, communicate=AsyncMock(return_value=(fields(loaded="not-found", state="inactive", pid="0", sub="dead").encode(), b"Unit not found")))
        with patch.object(main.os, "geteuid", return_value=1000), patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            status = await main.dpi_status()
        self.assertFalse(status["installed"])
        self.assertEqual(status["state"], "inactive")

    async def test_reject_root_and_arbitrary_operations(self):
        with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaises(ValueError):
                await main.dpi_systemctl("restart something.service")
            with patch.object(main.os, "geteuid", return_value=0), self.assertRaises(RuntimeError):
                await main.dpi_systemctl("start")
            spawn.assert_not_called()

    async def test_timeout_kills_and_reaps_client(self):
        process = Mock(returncode=0, communicate=AsyncMock(side_effect=[asyncio.TimeoutError(), (b"", b"")]))
        with patch.object(main.os, "geteuid", return_value=1000), patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.TimeoutError):
                await main.dpi_systemctl("stop")
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    async def test_failed_stop_preserves_running_state(self):
        plugin = main.Plugin()
        with patch.object(main, "dpi_systemctl", AsyncMock(side_effect=[RuntimeError("Denied"), fields()])):
            status = await plugin.set_dpi_enabled(False)
        self.assertTrue(status["enabled"])
        self.assertEqual(status["error"], "Denied")

    async def test_reject_non_boolean(self):
        plugin = main.Plugin()
        with patch.object(main, "dpi_systemctl", AsyncMock()) as control:
            for value in ("false", 1, None):
                with self.assertRaises(ValueError):
                    await plugin.set_dpi_enabled(value)
            control.assert_not_called()

    async def test_toggles_and_status_are_serialized(self):
        plugin = main.Plugin()
        events = []
        async def control(operation):
            events.append(operation)
            await asyncio.sleep(0)
            return fields() if operation == "show" else ""
        with patch.object(main, "dpi_systemctl", side_effect=control):
            await asyncio.gather(plugin.set_dpi_enabled(True), plugin.set_dpi_enabled(False), plugin.get_dpi_status())
        self.assertEqual(events, ["start", "show", "stop", "show", "show"])


class InstallButtonTests(unittest.IsolatedAsyncioTestCase):
    def status(self, installed=False):
        return {"available": installed, "installed": installed, "enabled": False,
                "requested": False, "busy": False, "state": "inactive", "error": None}

    async def test_click_starts_only_one_install_without_blocking_status(self):
        gate = asyncio.Event()
        process = Mock(returncode=0)
        async def communicate():
            await gate.wait()
            return b"Installed", b""
        process.communicate = AsyncMock(side_effect=communicate)
        plugin = main.Plugin()
        with patch.object(main, "dpi_install_support", return_value=None), \
             patch.object(main, "dpi_status", AsyncMock(side_effect=lambda: self.status(gate.is_set()))), \
             patch.object(main.os, "geteuid", return_value=1000), \
             patch.object(main.pwd, "getpwuid", return_value=Mock(pw_name="deck")), \
             patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            first = await plugin.install_dpi_helper()
            self.assertTrue(first["installing"])
            await asyncio.sleep(0)
            again = await plugin.install_dpi_helper()
            self.assertTrue(again["installing"])
            current = await plugin.get_dpi_status()
            self.assertFalse(current["can_install"])
            self.assertFalse(current["available"])
            spawn.assert_awaited_once()
            self.assertEqual(spawn.call_args.args, (
                str(main.PKEXEC), "--disable-internal-agent", "/usr/bin/python3", "-E", "-s",
                str(main.DPI_INSTALLER), "--user", "deck"))
            self.assertEqual(spawn.call_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
            self.assertEqual(spawn.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            gate.set()
            await plugin._dpi_install_task
            self.assertTrue((await plugin.get_dpi_status())["available"])
            self.assertIsNone(plugin._dpi_install_error)

    async def test_existing_or_unsupported_installation_does_not_elevate(self):
        for installed, reason in ((True, None), (False, "No pkexec")):
            with patch.object(main, "dpi_install_support", return_value=reason), \
                 patch.object(main, "dpi_status", AsyncMock(return_value=self.status(installed))), \
                 patch.object(main.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
                result = await main.Plugin().install_dpi_helper()
                self.assertFalse(result["can_install"])
                spawn.assert_not_called()

    async def test_auth_cancel_and_missing_agent_are_actionable(self):
        for code, expected in ((126, "cancelled"), (127, "Desktop Mode"), (1, "Missing nftables")):
            plugin = main.Plugin()
            process = Mock(returncode=code, communicate=AsyncMock(return_value=(b"", b"Missing nftables")))
            with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                await plugin._install_dpi_helper("deck")
            self.assertIn(expected, plugin._dpi_install_error)

    async def test_exit_zero_requires_installed_service_verification(self):
        process = Mock(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
        plugin = main.Plugin()
        with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
             patch.object(main, "dpi_status", AsyncMock(return_value=self.status(False))):
            await plugin._install_dpi_helper("deck")
        self.assertIn("unavailable", plugin._dpi_install_error)

    async def test_slow_authorization_remains_in_progress_without_second_install(self):
        gate = asyncio.Event()
        async def communicate():
            await gate.wait()
            return b"", b""
        process = Mock(returncode=126, communicate=AsyncMock(side_effect=communicate))
        plugin = main.Plugin()
        with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
             patch.object(main.asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError())):
            task = asyncio.create_task(plugin._install_dpi_helper("deck"))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIn("Still waiting", plugin._dpi_install_error)
            process.kill.assert_not_called()
            gate.set()
            await task
        self.assertIn("cancelled", plugin._dpi_install_error)

    def test_root_and_unsupported_host_cannot_use_install_button(self):
        with patch.object(main.os, "geteuid", return_value=0):
            self.assertIn("non-root", main.dpi_install_support())
        with patch.object(main.os, "geteuid", return_value=1000), patch.object(main.platform, "system", return_value="Darwin"):
            self.assertIn("Linux", main.dpi_install_support())


class FirewallTests(unittest.TestCase):
    def test_missing_table_stop_is_idempotent(self):
        with patch.object(firewall, "nft", return_value='{"nftables": []}') as nft:
            firewall.remove_owned_table()
        nft.assert_called_once_with("-j", "list", "tables")

    def test_only_owned_table_is_deleted(self):
        table = {"table": {"family": "inet", "name": firewall.TABLE, "comment": firewall.OWNER}}
        output = json.dumps({"nftables": [table]})
        with patch.object(firewall, "nft", side_effect=[output, output, ""]) as nft:
            firewall.remove_owned_table()
        self.assertEqual(nft.call_args.args, ("delete", "table", "inet", firewall.TABLE))

    def test_foreign_table_is_preserved(self):
        output = json.dumps({"nftables": [{"table": {"family": "inet", "name": firewall.TABLE}}]})
        with patch.object(firewall, "nft", return_value=output) as nft:
            with self.assertRaises(RuntimeError):
                firewall.remove_owned_table()
        self.assertEqual(nft.call_count, 2)

    def test_listing_error_does_not_delete(self):
        with patch.object(firewall, "nft", side_effect=RuntimeError("permission denied")) as nft:
            with self.assertRaises(RuntimeError):
                firewall.remove_owned_table()
        self.assertEqual(nft.call_count, 1)

    def test_queue_collision_does_not_install_rules(self):
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value="28745 1 0\n"), patch.object(firewall, "nft") as nft:
            with self.assertRaises(RuntimeError):
                firewall.start()
        nft.assert_not_called()


class DistributionTests(unittest.TestCase):
    def test_install_enables_conditional_boot_without_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "runtime").mkdir(parents=True)
            (source / "runtime/engine").write_bytes(b"test")
            (source / "firewall.py").write_text("# test")
            unit_name = "decky-zapret2.service"
            (source / unit_name).write_text("# test")
            state = root / "state"
            with patch.object(installer, "__file__", str(source / "install.py")), \
                 patch.object(installer, "ROOT", root / "runtime"), \
                 patch.object(installer, "UNIT", root / unit_name), \
                 patch.object(installer, "RULE", root / "policy.rules"), \
                 patch.object(installer, "STATE", state), \
                 patch.object(installer, "FILES", {"engine": hashlib.sha256(b"test").hexdigest()}), \
                 patch.object(installer.os, "geteuid", return_value=0), \
                 patch.object(installer.os, "chown") as chown, \
                 patch.object(installer.platform, "system", return_value="Linux"), \
                 patch.object(installer.platform, "machine", return_value="x86_64"), \
                 patch.object(installer.pwd, "getpwnam", return_value=Mock(pw_uid=1000, pw_gid=1000)), \
                 patch.object(installer, "trusted_directory"), \
                 patch.object(Path, "is_file", return_value=True), \
                 patch.object(installer.subprocess, "run") as run:
                installer.install("deck")
            chown.assert_called_once_with(state, 1000, 1000)
            self.assertEqual(list(state.iterdir()), [])
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["/usr/bin/systemctl", "daemon-reload"],
                ["/usr/bin/systemctl", "enable", unit_name],
            ])

    def test_checksum_rejects_modified_runtime(self):
        with self.assertRaises(ValueError):
            prepare.checked(b"modified", next(iter(prepare.FILES.values())))

    def test_archive_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(f"zapret2-{prepare.VERSION}/engine")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc/passwd"
                tar.addfile(entry)
            with patch.object(prepare, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()), patch.object(prepare, "FILES", {"engine": "unused"}):
                with self.assertRaises(ValueError):
                    prepare.prepare(archive, Path(directory) / "output")
            self.assertFalse((Path(directory) / "output").exists())

    def test_policy_scopes_user_unit_and_verbs(self):
        rule = installer.polkit_rule('deck"user', 1000)
        self.assertIn('subject.user === "deck\\"user"', rule)
        self.assertIn('action.lookup("unit") === "decky-zapret2.service"', rule)
        self.assertIn('action.lookup("verb") === "start" || action.lookup("verb") === "stop"', rule)
        self.assertNotIn("manage-unit-files", rule)
        with self.assertRaises(ValueError):
            installer.polkit_rule("root", 0)

    def test_installer_refuses_non_linux_host(self):
        with patch.object(installer.platform, "system", return_value="Darwin"), patch.object(installer.os, "geteuid", return_value=0):
            with self.assertRaises(RuntimeError):
                installer.install("deck")

    def test_untrusted_install_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o777)
            with self.assertRaises(RuntimeError):
                installer.trusted_directory(parent)

    def test_existing_installation_is_never_overwritten(self):
        with patch.object(installer.os, "geteuid", return_value=0), \
             patch.object(installer.platform, "system", return_value="Linux"), \
             patch.object(installer.platform, "machine", return_value="x86_64"), \
             patch.object(installer.pwd, "getpwnam", return_value=Mock(pw_uid=1000)), \
             patch.object(installer, "trusted_directory"), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(installer.tempfile, "mkdtemp") as create:
            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                installer.install("deck")
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
