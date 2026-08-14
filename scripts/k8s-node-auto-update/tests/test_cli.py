"""Regression tests for the weekly node package updater."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, call, patch

from k8s_node_auto_update import cli


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.reboot_marker = Path(self.temp_dir.name) / "reboot-required"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @patch.object(cli, "notify")
    @patch.object(cli, "run_command")
    def test_existing_reboot_marker_skips_package_commands(
        self, run_command, notify
    ) -> None:
        self.reboot_marker.touch()

        result = cli.main(
            reboot_marker=self.reboot_marker,
            hostname="node-1",
            environment={"NTFY_AUTH_TOKEN": "token"},
            user_id=0,
        )

        self.assertEqual(result, 0)
        run_command.assert_not_called()
        notify.assert_called_once_with(
            "node-1: Node already scheduled for reboot. Skipping updates.",
            environment={"NTFY_AUTH_TOKEN": "token"},
        )

    @patch.object(cli, "notify")
    @patch.object(cli, "run_command", side_effect=["", ""])
    def test_no_updates_notifies_without_scheduling_reboot(
        self, run_command, notify
    ) -> None:
        result = cli.main(
            reboot_marker=self.reboot_marker,
            hostname="node-1",
            environment={"NTFY_AUTH_TOKEN": "token"},
            user_id=0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            run_command.call_args_list,
            [
                call(["apt-get", "update"]),
                call(
                    ["apt-get", "--simulate", "--quiet=2", "upgrade"]
                ),
            ],
        )
        notify.assert_called_once_with(
            "node-1: No updates available.",
            environment={"NTFY_AUTH_TOKEN": "token"},
        )
        self.assertFalse(self.reboot_marker.exists())

    @patch.object(cli, "notify")
    @patch.object(
        cli,
        "run_command",
        side_effect=["", "Inst openssl [3.0.11] (3.0.12 Debian:12/stable)\n", ""],
    )
    def test_available_updates_are_installed_and_mark_reboot_required(
        self, run_command, notify
    ) -> None:
        result = cli.main(
            reboot_marker=self.reboot_marker,
            hostname="node-1",
            environment={"NTFY_AUTH_TOKEN": "token"},
            user_id=0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            run_command.call_args_list,
            [
                call(["apt-get", "update"]),
                call(
                    ["apt-get", "--simulate", "--quiet=2", "upgrade"]
                ),
                call(cli.APT_UPGRADE_COMMAND),
            ],
        )
        self.assertTrue(self.reboot_marker.exists())
        self.assertEqual(
            notify.call_args_list,
            [
                call(
                    "node-1: Updates will be applied: openssl",
                    environment={"NTFY_AUTH_TOKEN": "token"},
                ),
                call(
                    "node-1: Updates applied; node is marked for reboot.",
                    environment={"NTFY_AUTH_TOKEN": "token"},
                ),
            ],
        )

    @patch.object(cli, "notify")
    def test_non_root_execution_is_rejected(self, notify) -> None:
        result = cli.main(
            reboot_marker=self.reboot_marker,
            hostname="node-1",
            environment={"NTFY_AUTH_TOKEN": "token"},
            user_id=1000,
        )

        self.assertEqual(result, 1)
        notify.assert_not_called()

    def test_missing_token_is_rejected_before_package_changes(self) -> None:
        with patch.object(cli, "run_command") as run_command:
            result = cli.main(
                reboot_marker=self.reboot_marker,
                hostname="node-1",
                environment={},
                user_id=0,
            )

        self.assertEqual(result, 2)
        run_command.assert_not_called()

    @patch.object(cli, "run_command")
    def test_package_command_failure_returns_its_exit_code(self, run_command) -> None:
        run_command.side_effect = cli.subprocess.CalledProcessError(
            100, ["apt-get", "update"]
        )

        result = cli.main(
            reboot_marker=self.reboot_marker,
            hostname="node-1",
            environment={"NTFY_AUTH_TOKEN": "token"},
            user_id=0,
        )

        self.assertEqual(result, 100)


class PackageParsingTests(unittest.TestCase):
    def test_extract_upgradable_packages_deduplicates_and_preserves_order(self) -> None:
        output = "\n".join(
            [
                "Inst openssl [3.0.11] (3.0.12 Debian:12/stable)",
                "Inst linux-image-amd64 [6.1.0] (6.1.1 Debian:12/stable)",
                "Inst openssl [3.0.11] (3.0.12 Debian:12/stable)",
            ]
        )

        self.assertEqual(
            cli.extract_upgradable_packages(output),
            ["openssl", "linux-image-amd64"],
        )


class CommandExecutionTests(unittest.TestCase):
    @patch.object(cli.subprocess, "run")
    def test_run_command_uses_non_interactive_c_locale(self, run) -> None:
        run.return_value = MagicMock(stdout="package output")

        self.assertEqual(cli.run_command(["apt-get", "update"]), "package output")

        self.assertEqual(run.call_args.args[0], ["apt-get", "update"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertTrue(run.call_args.kwargs["text"])
        self.assertEqual(
            run.call_args.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive"
        )
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")


class DeploymentConfigurationTests(unittest.TestCase):
    def test_ansible_runs_the_updater_from_its_repository_checkout(self) -> None:
        workspace_root = Path(__file__).resolve().parents[4]
        ansible_root = workspace_root / "home-ansible"
        playbook = ansible_root / "provision" / "provision-k8s-setup-node.yaml"
        launcher = (
            ansible_root
            / "provision"
            / "templates"
            / "run-k8s-node-auto-update.j2"
        )

        if not playbook.exists():
            self.skipTest("home-ansible checkout is not available")

        self.assertTrue(launcher.exists())
        self.assertFalse(
            (ansible_root / "provision" / "files" / "k8s-node-auto-update.py").exists()
        )
        self.assertIn("ansible.builtin.git:", playbook.read_text())
        self.assertIn("k8s_node_auto_update_repository_directory", playbook.read_text())
        self.assertIn("git -C \"$repository\" pull --ff-only", launcher.read_text())
        self.assertIn("k8s_node_auto_update_repository_directory", launcher.read_text())
        self.assertIn("k8s_node_auto_update_repository_revision", launcher.read_text())
        self.assertIn("k8s_node_auto_update_uv_binary", launcher.read_text())
        self.assertIn("refusing to run an unverified checkout", launcher.read_text())


class NotificationTests(unittest.TestCase):
    def test_notify_requires_a_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "NTFY_AUTH_TOKEN"):
            cli.notify("message", environment={})

    @patch.object(cli, "urlopen")
    def test_notify_posts_json_to_configured_topic(self, urlopen) -> None:
        response = MagicMock(status=200)
        urlopen.return_value.__enter__.return_value = response

        self.assertTrue(
            cli.notify(
                "message",
                environment={
                    "NTFY_AUTH_TOKEN": "token",
                    "NTFY_URL": "https://ntfy.example.test/topic",
                },
            )
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ntfy.example.test/topic")
        self.assertEqual(request.data, b'{"message": "message"}')
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10)

    @patch.object(cli, "urlopen", side_effect=cli.URLError("offline"))
    def test_notify_failure_does_not_raise(self, urlopen) -> None:
        self.assertFalse(cli.notify("message", environment={"NTFY_AUTH_TOKEN": "token"}))
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
