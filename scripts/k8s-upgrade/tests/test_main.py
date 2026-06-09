import importlib.util
import pathlib
import unittest
from unittest.mock import MagicMock, call, patch


MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "main.py"
SPEC = importlib.util.spec_from_file_location("k8s_upgrade_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


PACKAGE_VERSIONS = {
    "kubeadm": "1.30.4-1.1",
    "kubelet": "1.30.4-1.1",
    "kubectl": "1.30.4-1.1",
}


class VersionParsingTests(unittest.TestCase):
    def test_parse_k8s_version(self):
        self.assertEqual(main.parse_k8s_version("v1.31.2"), "1.31.2")

    def test_parse_minor_version(self):
        self.assertEqual(main.parse_minor_version("v1.31.2"), "1.31")

    def test_normalize_target_version_accepts_minor(self):
        self.assertEqual(main.normalize_target_version("v1.30"), "1.30")

    def test_normalize_target_version_accepts_patch(self):
        self.assertEqual(main.normalize_target_version("1.30.4"), "1.30.4")

    def test_normalize_target_version_rejects_embedded_patch_substring(self):
        with self.assertRaises(SystemExit):
            main.normalize_target_version("release-1.30.4-now")

    def test_normalize_target_version_rejects_invalid_value(self):
        with self.assertRaises(SystemExit):
            main.normalize_target_version("banana")


class KubectlExecutionTests(unittest.TestCase):
    @patch.object(main, "run_cmd", return_value=(0, '{"serverVersion": {"gitVersion": "v1.29.7"}}', ""))
    def test_get_current_cluster_version_runs_remotely(self, run_cmd):
        client = MagicMock()

        self.assertEqual(
            main.get_current_cluster_k8s_version(client, "secret", "cp-1"),
            "1.29.7",
        )
        run_cmd.assert_called_once_with(
            client,
            "kubectl --kubeconfig /etc/kubernetes/admin.conf version -o json",
            sudo_password="secret",
            sudo=True,
            host="cp-1",
            raise_on_error=False,
        )

    @patch.object(main, "run_cmd", return_value="Kubernetes v1.30.4")
    def test_get_node_installed_k8s_version(self, run_cmd):
        self.assertEqual(
            main.get_node_installed_k8s_version(MagicMock(), "secret", "worker-1"),
            "1.30.4",
        )

    @patch.object(main, "run_cmd", return_value=(0, "already cordoned", ""))
    def test_cordon_runs_from_control_plane(self, run_cmd):
        client = MagicMock()

        main.cordon_node(client, "cp-1", "secret", "worker-1")

        run_cmd.assert_called_once_with(
            client,
            "kubectl --kubeconfig /etc/kubernetes/admin.conf cordon worker-1",
            sudo_password="secret",
            sudo=True,
            host="cp-1",
            raise_on_error=False,
        )

    @patch.object(main, "run_cmd")
    def test_run_remote_kubectl_does_not_fallback_for_unrelated_error(
        self, run_cmd
    ):
        client = MagicMock()
        run_cmd.return_value = (1, "", "some unrelated failure")

        with self.assertRaises(SystemExit):
            main.run_remote_kubectl(client, "cp-1", "cordon cp-1", "secret")

        self.assertEqual(run_cmd.call_count, 1)

    @patch.object(main, "run_cmd", return_value=(0, "node/cp-1 cordoned", ""))
    def test_run_remote_kubectl_prefers_admin_conf(self, run_cmd):
        client = MagicMock()

        self.assertEqual(
            main.run_remote_kubectl(client, "cp-1", "cordon cp-1", "secret"),
            "node/cp-1 cordoned",
        )
        run_cmd.assert_called_once_with(
            client,
            "kubectl --kubeconfig /etc/kubernetes/admin.conf cordon cp-1",
            sudo_password="secret",
            sudo=True,
            host="cp-1",
            raise_on_error=False,
        )

    @patch.object(main, "run_cmd")
    def test_run_remote_kubectl_retries_with_super_admin_for_auth_failure(
        self, run_cmd
    ):
        client = MagicMock()
        run_cmd.side_effect = [
            (1, "", 'Error from server (Forbidden): nodes "cp-1" is forbidden: User "kubernetes-admin" cannot get resource "nodes"'),
            (0, "node/cp-1 cordoned", ""),
        ]

        self.assertEqual(
            main.run_remote_kubectl(client, "cp-1", "cordon cp-1", "secret"),
            "node/cp-1 cordoned",
        )
        self.assertEqual(
            run_cmd.call_args_list,
            [
                call(
                    client,
                    "kubectl --kubeconfig /etc/kubernetes/admin.conf cordon cp-1",
                    sudo_password="secret",
                    sudo=True,
                    host="cp-1",
                    raise_on_error=False,
                ),
                call(
                    client,
                    "kubectl --kubeconfig /etc/kubernetes/super-admin.conf cordon cp-1",
                    sudo_password="secret",
                    sudo=True,
                    host="cp-1",
                    raise_on_error=False,
                ),
            ],
        )


class TargetVersionResolutionTests(unittest.TestCase):
    @patch.object(main, "run_cmd")
    def test_selects_latest_common_patch_for_minor_target(self, run_cmd):
        run_cmd.side_effect = [
            "\n".join(
                [
                    " kubeadm | 1.30.3-1.1 | repo",
                    " kubeadm | 1.30.4-1.1 | repo",
                ]
            ),
            "\n".join(
                [
                    " kubelet | 1.30.4-1.1 | repo",
                    " kubelet | 1.30.3-1.1 | repo",
                ]
            ),
            "\n".join(
                [
                    " kubectl | 1.30.4-1.2 | repo",
                    " kubectl | 1.30.3-1.1 | repo",
                ]
            ),
        ]

        kube_version, package_versions = main.get_target_k8s_version(
            MagicMock(), "1.30", "secret", "cp-1"
        )

        self.assertEqual(kube_version, "1.30.4")
        self.assertEqual(
            package_versions,
            {
                "kubeadm": "1.30.4-1.1",
                "kubelet": "1.30.4-1.1",
                "kubectl": "1.30.4-1.2",
            },
        )

    @patch.object(main, "run_cmd")
    def test_uses_exact_patch_when_requested(self, run_cmd):
        run_cmd.side_effect = [
            "\n".join(
                [
                    " kubeadm | 1.30.4-1.1 | repo",
                    " kubeadm | 1.30.3-1.1 | repo",
                ]
            ),
            "\n".join(
                [
                    " kubelet | 1.30.4-1.1 | repo",
                    " kubelet | 1.30.3-1.1 | repo",
                ]
            ),
            "\n".join(
                [
                    " kubectl | 1.30.4-1.1 | repo",
                    " kubectl | 1.30.3-1.1 | repo",
                ]
            ),
        ]

        kube_version, package_versions = main.get_target_k8s_version(
            MagicMock(), "1.30.4", "secret", "cp-1"
        )

        self.assertEqual(kube_version, "1.30.4")
        self.assertEqual(package_versions, PACKAGE_VERSIONS)

    @patch.object(main, "run_cmd")
    def test_exits_when_exact_patch_is_unavailable(self, run_cmd):
        run_cmd.side_effect = [
            " kubeadm | 1.30.4-1.1 | repo",
            " kubelet | 1.30.4-1.1 | repo",
            " kubectl | 1.30.3-1.1 | repo",
        ]

        with self.assertRaises(SystemExit):
            main.get_target_k8s_version(MagicMock(), "1.30.4", "secret", "cp-1")


class K8sSourceFileTests(unittest.TestCase):
    @patch.object(main, "run_cmd")
    def test_discovers_kubernetes_source_files(self, run_cmd):
        run_cmd.return_value = "\n".join(
            [
                "/etc/apt/sources.list.d/kubernetes.sources",
                "/etc/apt/sources.list.d/kubernetes.sources",
                "/etc/apt/sources.list.d/kubernetes-extra.list",
            ]
        )

        self.assertEqual(
            main.get_k8s_source_files(MagicMock(), "secret", "cp-1"),
            [
                "/etc/apt/sources.list.d/kubernetes.sources",
                "/etc/apt/sources.list.d/kubernetes-extra.list",
            ],
        )

    @patch.object(main, "run_cmd")
    def test_exits_when_no_kubernetes_source_file_is_found(self, run_cmd):
        run_cmd.return_value = ""

        with self.assertRaises(SystemExit):
            main.get_k8s_source_files(MagicMock(), "secret", "cp-1")


class UpdateK8sSourcesTests(unittest.TestCase):
    @patch.object(main, "apt_update")
    @patch.object(main, "run_cmd")
    @patch.object(main, "get_k8s_source_files")
    def test_updates_all_discovered_kubernetes_source_files(
        self, get_k8s_source_files, run_cmd, apt_update
    ):
        client = MagicMock()
        get_k8s_source_files.return_value = [
            "/etc/apt/sources.list.d/kubernetes.sources",
            "/etc/apt/sources.list.d/kubernetes-extra.list",
        ]
        run_cmd.return_value = "kubeadm | 1.34.0-1.1 | https://pkgs.k8s.io"

        main.update_k8s_sources(client, "1.34", "secret", "cp-1")

        self.assertEqual(
            run_cmd.call_args_list,
            [
                call(
                    client,
                    "sed -i 's|/v[0-9]\\+\\.[0-9]\\+/deb/|/v1.34/deb/|g' \"/etc/apt/sources.list.d/kubernetes.sources\"",
                    sudo_password="secret",
                    sudo=True,
                    host="cp-1",
                ),
                call(
                    client,
                    "sed -i 's|/v[0-9]\\+\\.[0-9]\\+/deb/|/v1.34/deb/|g' \"/etc/apt/sources.list.d/kubernetes-extra.list\"",
                    sudo_password="secret",
                    sudo=True,
                    host="cp-1",
                ),
                call(client, "apt-cache madison kubeadm", host="cp-1"),
            ],
        )
        apt_update.assert_called_once_with(client, "secret", "cp-1")


class UpgradeK8sNodeTests(unittest.TestCase):
    @patch.object(main, "uncordon_node")
    @patch.object(main, "add_hold")
    @patch.object(main, "drain_node")
    @patch.object(main, "cordon_node")
    @patch.object(main, "remove_hold")
    @patch.object(main, "sleep")
    @patch.object(main, "apt_update")
    @patch.object(main, "run_cmd")
    def test_first_control_plane_uses_apply_and_remote_kubectl(
        self,
        run_cmd,
        apt_update,
        sleep_mock,
        remove_hold,
        cordon_node,
        drain_node,
        add_hold,
        uncordon_node,
    ):
        client = MagicMock()
        kubectl_client = MagicMock()

        main.upgrade_k8s_node(
            client,
            kubectl_client,
            "cp-1",
            "1.30.4",
            PACKAGE_VERSIONS,
            "secret",
            "cp-1",
            node_mode=main.NODE_MODE_FIRST_CONTROL,
        )

        sleep_mock.assert_not_called()
        cordon_node.assert_called_once_with(kubectl_client, "cp-1", "secret", "cp-1")
        drain_node.assert_called_once_with(kubectl_client, "cp-1", "secret", "cp-1")
        command_list = [args.args[1] for args in run_cmd.call_args_list]
        self.assertIn("kubeadm upgrade apply -y v1.30.4", command_list)
        self.assertNotIn("kubeadm upgrade node", command_list)
        add_hold.assert_called_once_with(client, "secret", "cp-1")
        uncordon_node.assert_called_once_with(kubectl_client, "cp-1", "secret", "cp-1")

    @patch.object(main, "uncordon_node")
    @patch.object(main, "add_hold")
    @patch.object(main, "drain_node")
    @patch.object(main, "cordon_node")
    @patch.object(main, "remove_hold")
    @patch.object(main, "sleep")
    @patch.object(main, "apt_update")
    @patch.object(main, "run_cmd")
    def test_worker_uses_node_upgrade_and_sleep(
        self,
        run_cmd,
        apt_update,
        sleep_mock,
        remove_hold,
        cordon_node,
        drain_node,
        add_hold,
        uncordon_node,
    ):
        main.upgrade_k8s_node(
            MagicMock(),
            MagicMock(),
            "cp-1",
            "1.30.4",
            PACKAGE_VERSIONS,
            "secret",
            "worker-1",
            node_mode=main.NODE_MODE_WORKER,
        )

        sleep_mock.assert_called_once_with(10)
        command_list = [args.args[1] for args in run_cmd.call_args_list]
        self.assertIn("kubeadm upgrade node", command_list)
        self.assertNotIn("kubeadm upgrade apply -y v1.30.4", command_list)


class MainOrchestrationTests(unittest.TestCase):
    @patch.object(main, "input", return_value="")
    @patch.object(main, "upgrade_k8s_node")
    @patch.object(main, "get_node_installed_k8s_version")
    @patch.object(main, "get_target_k8s_version", return_value=("1.30.4", PACKAGE_VERSIONS))
    @patch.object(main, "update_k8s_sources")
    @patch.object(main, "get_current_cluster_k8s_version", return_value="1.29.9")
    @patch.object(main, "ssh_connect")
    @patch.object(main, "getpass")
    @patch.object(main, "load_ssh_config", return_value=MagicMock())
    @patch.object(main, "read_inventory")
    def test_upgrades_remaining_nodes_when_first_control_already_matches_target(
        self,
        read_inventory,
        load_ssh_config,
        getpass_module,
        ssh_connect,
        get_current_cluster_k8s_version,
        update_k8s_sources,
        get_target_k8s_version,
        get_node_installed_k8s_version,
        upgrade_k8s_node,
        input_mock,
    ):
        read_inventory.return_value = {
            "control": ["cp-1", "cp-2"],
            "worker": ["worker-1", "worker-2"],
        }
        getpass_module.getpass.return_value = "secret"
        clients = [MagicMock(name=f"client-{index}") for index in range(8)]
        ssh_connect.side_effect = clients
        get_node_installed_k8s_version.return_value = "1.30.4"

        main.main(["--target-version", "1.30.4"])

        self.assertEqual(
            update_k8s_sources.call_args_list,
            [
                call(clients[1], "1.30", "secret", "cp-1"),
                call(clients[2], "1.30", "secret", "cp-2"),
                call(clients[3], "1.30", "secret", "worker-1"),
                call(clients[4], "1.30", "secret", "worker-2"),
            ],
        )
        self.assertEqual(
            upgrade_k8s_node.call_args_list,
            [
                call(
                    clients[5],
                    clients[0],
                    "cp-1",
                    "1.30.4",
                    PACKAGE_VERSIONS,
                    "secret",
                    "cp-2",
                    node_mode=main.NODE_MODE_CONTROL,
                ),
                call(
                    clients[6],
                    clients[0],
                    "cp-1",
                    "1.30.4",
                    PACKAGE_VERSIONS,
                    "secret",
                    "worker-1",
                    node_mode=main.NODE_MODE_WORKER,
                ),
                call(
                    clients[7],
                    clients[0],
                    "cp-1",
                    "1.30.4",
                    PACKAGE_VERSIONS,
                    "secret",
                    "worker-2",
                    node_mode=main.NODE_MODE_WORKER,
                ),
            ],
        )
        get_target_k8s_version.assert_called_once_with(clients[0], "1.30.4", "secret", "cp-1")
