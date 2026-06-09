#!/usr/bin/env python3
from time import sleep
import argparse
import json
import paramiko
import re
import os
import getpass
import logging
import sys

INVENTORY_FILE = "inventory.txt"  # Format: hostname role (control/worker)
SSH_CONFIG_FILE = os.path.expanduser("~/.ssh/config")
NODE_MODE_FIRST_CONTROL = "first_control"
NODE_MODE_CONTROL = "control"
NODE_MODE_WORKER = "worker"
KUBECTL_BASE_CMD = "kubectl --kubeconfig /etc/kubernetes/admin.conf"
SUPER_ADMIN_KUBECTL_BASE_CMD = (
    "kubectl --kubeconfig /etc/kubernetes/super-admin.conf"
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("k8s_upgrade.log"), logging.StreamHandler()],
)


def read_inventory():
    nodes = {"control": [], "worker": []}
    with open(INVENTORY_FILE) as f:
        for line in f:
            if not line.strip():
                continue
            host, role = line.strip().split()
            if role not in nodes:
                raise ValueError(f"Unknown role '{role}' in inventory")
            nodes[role].append(host)
    return nodes


def load_ssh_config():
    ssh_config = paramiko.SSHConfig()
    with open(SSH_CONFIG_FILE) as f:
        ssh_config.parse(f)
    return ssh_config


def get_ssh_params(ssh_config, host):
    cfg = ssh_config.lookup(host)
    hostname = cfg.get("hostname", host)
    user = cfg.get("user", getpass.getuser())
    keyfile = cfg.get("identityfile", [os.path.expanduser("~/.ssh/id_rsa")])[0]
    port = int(cfg.get("port", 22))
    proxyjump = cfg.get("proxyjump")
    return hostname, user, keyfile, port, proxyjump


_jump_clients = []


def _open_jump_channel(ssh_config, jump_host, dest_hostname, dest_port):
    hostname, user, keyfile, port, proxyjump = get_ssh_params(ssh_config, jump_host)
    jump_client = paramiko.SSHClient()
    jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sock = None
    if proxyjump:
        sock = _open_jump_channel(ssh_config, proxyjump, hostname, port)
    jump_client.connect(hostname=hostname, username=user, key_filename=keyfile, port=port, sock=sock)
    _jump_clients.append(jump_client)
    return jump_client.get_transport().open_channel("direct-tcpip", (dest_hostname, dest_port), ("127.0.0.1", 0))


def ssh_connect(ssh_config, host):
    hostname, user, keyfile, port, proxyjump = get_ssh_params(ssh_config, host)
    logging.info(f"Connecting to {host} ({hostname}) as {user}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sock = None
    if proxyjump:
        sock = _open_jump_channel(ssh_config, proxyjump, hostname, port)
    client.connect(hostname=hostname, username=user, key_filename=keyfile, port=port, sock=sock)
    return client


def run_cmd(client, cmd, sudo_password="", sudo=False, host=None, raise_on_error=True):
    host = host or "unknown"
    logging.info(f"[{host}] Running command: {cmd}")
    if sudo:
        cmd = f"sudo -S -p '' {cmd}"
    stdin, stdout, stderr = client.exec_command(cmd)
    if sudo:
        stdin.write(sudo_password + "\n")
        stdin.flush()
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode()
    error = stderr.read().decode()
    if exit_status != 0:
        if raise_on_error:
            logging.error(f"[{host}] Command failed with exit code {exit_status}: {cmd}")
            logging.error(f"[{host}] Stderr: {error.strip()}")
            sys.exit(1)
        return exit_status, output.strip(), error.strip()
    if error.strip():
        logging.warning(f"[{host}] Warning output: {error.strip()}")
    if raise_on_error:
        return output.strip()
    return exit_status, output.strip(), error.strip()


def parse_k8s_version(version_text):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version_text)
    if match:
        return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
    return None


def parse_minor_version(version_text):
    match = re.search(r"v?(\d+)\.(\d+)", version_text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return None


def normalize_target_version(target_version):
    if target_version is None:
        return None

    target_version = target_version.strip()
    patch_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", target_version)
    if patch_match:
        return f"{patch_match.group(1)}.{patch_match.group(2)}.{patch_match.group(3)}"

    minor_match = re.fullmatch(r"v?(\d+)\.(\d+)", target_version)
    if minor_match:
        return f"{minor_match.group(1)}.{minor_match.group(2)}"

    logging.error(
        f"Invalid target version '{target_version}'. Expected X.Y or X.Y.Z"
    )
    sys.exit(1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Upgrade kubeadm-managed Kubernetes nodes")
    parser.add_argument(
        "--target-version",
        help="Target Kubernetes version to upgrade to (X.Y or X.Y.Z)",
    )
    return parser.parse_args(argv)


def run_remote_kubectl(
    client, kubectl_host, kubectl_args, sudo_password, target_host=None
):
    def log_cmd(cmd):
        if target_host:
            logging.info(f"[{target_host}] Running kubectl from {kubectl_host}: {cmd}")
        else:
            logging.info(f"[{kubectl_host}] Running kubectl: {cmd}")

    def should_retry_with_super_admin(error_text):
        return bool(
            re.search(
                r"(?i)(forbidden|unauthorized|cannot get resource|error loading config file|unable to load.*kubeconfig|no such file|permission denied|invalid (?:configuration|kubeconfig)|x509|context deadline exceeded)",
                error_text,
            )
        )

    for base_cmd, is_retry in (
        (KUBECTL_BASE_CMD, False),
        (SUPER_ADMIN_KUBECTL_BASE_CMD, True),
    ):
        full_cmd = f"{base_cmd} {kubectl_args}"
        log_cmd(full_cmd)
        exit_status, output, error = run_cmd(
            client,
            full_cmd,
            sudo_password=sudo_password,
            sudo=True,
            host=kubectl_host,
            raise_on_error=False,
        )
        if exit_status == 0:
            return output
        if not is_retry and should_retry_with_super_admin(error):
            logging.warning(
                f"[{kubectl_host}] kubectl failed with admin.conf; retrying with super-admin.conf"
            )
            continue

        logging.error(
            f"[{kubectl_host}] Command failed with exit code {exit_status}: {full_cmd}"
        )
        logging.error(f"[{kubectl_host}] Stderr: {error}")
        sys.exit(1)


def get_current_cluster_k8s_version(client, sudo_password, host):
    logging.info(f"[{host}] Discovering current Kubernetes cluster version via kubectl")
    output = run_remote_kubectl(client, host, "version -o json", sudo_password)

    try:
        version_info = json.loads(output)
    except json.JSONDecodeError:
        logging.error("Could not parse kubectl version output as JSON")
        sys.exit(1)

    server_version = version_info.get("serverVersion", {})
    git_version = server_version.get("gitVersion", "")
    current_version = parse_k8s_version(git_version)
    if current_version:
        logging.info(f"Current Kubernetes cluster version: {current_version}")
        return current_version

    major = server_version.get("major")
    minor = server_version.get("minor", "")
    if major and minor:
        cleaned_minor = re.sub(r"\D", "", minor)
        if cleaned_minor:
            current_version = f"{major}.{cleaned_minor}"
            logging.info(f"Current Kubernetes cluster version: {current_version}")
            return current_version

    logging.error("Could not determine current Kubernetes cluster version from kubectl")
    sys.exit(1)


def get_node_installed_k8s_version(client, sudo_password, host):
    output = run_cmd(
        client,
        "kubelet --version",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )
    installed_version = parse_k8s_version(output)
    if installed_version:
        logging.info(f"[{host}] Installed kubelet version: {installed_version}")
        return installed_version

    logging.error(f"[{host}] Could not determine installed kubelet version")
    sys.exit(1)


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def get_available_package_versions(client, package_name, new_minor_version, sudo_password, host):
    output = run_cmd(
        client,
        f"apt-cache madison {package_name}",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )

    matches = re.findall(rf"\|\s*({re.escape(new_minor_version)}\.\d+)(-[^\s|]+)", output)
    if not matches:
        logging.error(
            f"[{host}] Could not find an available {package_name} version for Kubernetes v{new_minor_version}"
        )
        sys.exit(1)

    return {
        kube_version: f"{kube_version}{package_suffix}"
        for kube_version, package_suffix in matches
    }


def get_target_k8s_version(client, target_version, sudo_password, host):
    normalized_target_version = normalize_target_version(target_version)
    new_minor_version = parse_minor_version(normalized_target_version)
    package_versions = {
        package_name: get_available_package_versions(
            client, package_name, new_minor_version, sudo_password, host
        )
        for package_name in ("kubeadm", "kubelet", "kubectl")
    }

    common_versions = set(package_versions["kubeadm"])
    for package_name in ("kubelet", "kubectl"):
        common_versions &= set(package_versions[package_name])

    if not common_versions:
        logging.error(
            f"[{host}] Could not find a common kubeadm/kubelet/kubectl version for Kubernetes v{new_minor_version}"
        )
        sys.exit(1)

    if parse_k8s_version(normalized_target_version):
        kube_version = normalized_target_version
        if kube_version not in common_versions:
            logging.error(
                f"[{host}] Could not find Kubernetes v{kube_version} across kubeadm, kubelet, and kubectl"
            )
            sys.exit(1)
    else:
        kube_version = max(common_versions, key=version_key)

    selected_packages = {
        package_name: versions[kube_version]
        for package_name, versions in package_versions.items()
    }
    logging.info(
        f"[{host}] Selected Kubernetes v{kube_version} with packages: {selected_packages}"
    )
    return kube_version, selected_packages


def get_k8s_source_files(client, sudo_password, host):
    output = run_cmd(
        client,
        "sh -c \"grep -R -lE '/v[0-9]+\\.[0-9]+/deb/' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true\"",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )

    source_files = list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))
    if source_files:
        logging.info(f"[{host}] Kubernetes apt source files: {', '.join(source_files)}")
        return source_files

    logging.error(
        f"[{host}] Could not find a Kubernetes apt source file with a versioned repository URL"
    )
    sys.exit(1)


def apt_update(client, sudo_password, host):
    exit_status, _, error = run_cmd(
        client, "apt-get update",
        sudo_password=sudo_password, sudo=True, host=host,
        raise_on_error=False,
    )
    if exit_status == 0:
        return
    if exit_status == 100:
        logging.warning(f"[{host}] apt-get update completed with some repo failures:\n{error.strip()}")
        return
    logging.error(f"[{host}] apt-get update failed with exit code {exit_status}: {error.strip()}")
    sys.exit(1)


def refresh_k8s_keyring(client, new_minor_version, sudo_password, host, source_files):
    seen = set()
    for source_file in source_files:
        content = run_cmd(client, f"cat \"{source_file}\"", host=host)
        match = re.search(r'signed-by=([^,\]\s]+)', content) or re.search(r'Signed-By:\s*(\S+)', content, re.IGNORECASE)
        if not match:
            continue
        keyring_path = match.group(1)
        if keyring_path in seen:
            continue
        seen.add(keyring_path)
        key_url = f"https://pkgs.k8s.io/core:/stable:/v{new_minor_version}/deb/Release.key"
        logging.info(f"[{host}] Refreshing Kubernetes apt signing key at {keyring_path}")
        run_cmd(
            client,
            f"sh -c \"curl -fsSL '{key_url}' | gpg --batch --yes --dearmor -o '{keyring_path}'\"",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )


def update_k8s_sources(client, new_minor_version, sudo_password, host):
    logging.info(f"[{host}] Updating Kubernetes apt sources to v{new_minor_version}")

    source_files = get_k8s_source_files(client, sudo_password, host)
    for source_file in source_files:
        sed_cmd = f"sed -i 's|/v[0-9]\\+\\.[0-9]\\+/deb/|/v{new_minor_version}/deb/|g' \"{source_file}\""
        run_cmd(client, sed_cmd, sudo_password=sudo_password, sudo=True, host=host)

    apt_update(client, sudo_password, host)

    output = run_cmd(client, "apt-cache madison kubeadm", host=host)
    if new_minor_version not in output:
        logging.info(f"[{host}] Kubernetes v{new_minor_version} not in apt cache — refreshing signing key and retrying")
        refresh_k8s_keyring(client, new_minor_version, sudo_password, host, source_files)
        apt_update(client, sudo_password, host)
        output = run_cmd(client, "apt-cache madison kubeadm", host=host)
        if new_minor_version not in output:
            logging.error(
                f"[{host}] No kubeadm packages found for v{new_minor_version} after refreshing signing key — "
                "check that the Kubernetes apt source configuration is correct"
            )
            sys.exit(1)

    logging.info(f"[{host}] Kubernetes sources updated successfully")


def remove_hold(client, sudo_password, host):
    logging.info(f"[{host}] Removing hold on kubeadm, kubelet, kubectl")
    run_cmd(
        client,
        "apt-mark unhold kubeadm kubelet kubectl",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )


def add_hold(client, sudo_password, host):
    logging.info(f"[{host}] Re-adding hold on kubeadm, kubelet, kubectl")
    run_cmd(
        client,
        "apt-mark hold kubeadm kubelet kubectl",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )


def cordon_node(kubectl_client, kubectl_host, sudo_password, host):
    run_remote_kubectl(
        kubectl_client, kubectl_host, f"cordon {host}", sudo_password, target_host=host
    )


def drain_node(kubectl_client, kubectl_host, sudo_password, host):
    run_remote_kubectl(
        kubectl_client,
        kubectl_host,
        f"drain --ignore-daemonsets --delete-emptydir-data {host}",
        sudo_password,
        target_host=host,
    )


def uncordon_node(kubectl_client, kubectl_host, sudo_password, host):
    run_remote_kubectl(
        kubectl_client,
        kubectl_host,
        f"uncordon {host}",
        sudo_password,
        target_host=host,
    )


def upgrade_k8s_node(
    client,
    kubectl_client,
    kubectl_host,
    kube_version,
    package_versions,
    sudo_password,
    host,
    node_mode=NODE_MODE_WORKER,
):
    logging.info(f"[{host}] Starting upgrade process")

    # Remove hold before upgrade
    remove_hold(client, sudo_password, host)

    # Cordon and drain the node
    cordon_node(kubectl_client, kubectl_host, sudo_password, host)

    if node_mode == NODE_MODE_WORKER:
        sleep(10)
    drain_node(kubectl_client, kubectl_host, sudo_password, host)

    # Update apt and upgrade kubeadm
    apt_update(client, sudo_password, host)
    run_cmd(
        client,
        f"apt-get install -y kubeadm={package_versions['kubeadm']}",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )

    if node_mode == NODE_MODE_FIRST_CONTROL:
        logging.info(f"[{host}] Applying kubeadm control plane upgrade")
        run_cmd(
            client,
            f"kubeadm upgrade apply -y v{kube_version}",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )
    else:
        logging.info(f"[{host}] Applying kubeadm node upgrade")
        run_cmd(
            client,
            "kubeadm upgrade node",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )

    # Upgrade kubelet and kubectl
    run_cmd(
        client,
        f"apt-get install -y kubelet={package_versions['kubelet']} kubectl={package_versions['kubectl']}",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )
    run_cmd(
        client,
        "systemctl daemon-reload",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )
    run_cmd(
        client,
        "systemctl restart kubelet",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )

    # Re-add hold after upgrade
    add_hold(client, sudo_password, host)

    # Uncordon node after upgrade
    uncordon_node(kubectl_client, kubectl_host, sudo_password, host)

    logging.info(f"[{host}] Upgrade process completed")


def main(argv=None):
    args = parse_args(argv)
    nodes = read_inventory()
    if not nodes["control"]:
        logging.error("No control plane node found in inventory.")
        sys.exit(1)

    ssh_config = load_ssh_config()
    sudo_password = getpass.getpass("Enter sudo password for all nodes: ")

    # Pick first control plane node for upgrade apply
    control_host = nodes["control"][0]
    client = ssh_connect(ssh_config, control_host)

    current_version = get_current_cluster_k8s_version(client, sudo_password, control_host)
    current_minor_version = parse_minor_version(current_version)
    if not current_minor_version:
        logging.error(
            f"Could not determine the current Kubernetes minor version from {current_version}"
        )
        sys.exit(1)

    requested_target_version = normalize_target_version(args.target_version)
    if requested_target_version:
        target_version_selector = requested_target_version
        new_minor_version = parse_minor_version(requested_target_version)
    else:
        current_major, current_minor = current_minor_version.split(".")
        new_minor_version = f"{current_major}.{int(current_minor) + 1}"
        target_version_selector = new_minor_version

    # Update sources on all nodes
    all_hosts = nodes["control"] + nodes["worker"]
    for host in all_hosts:
        logging.info(f"[{host}] Connecting to update sources")
        c = ssh_connect(ssh_config, host)
        update_k8s_sources(c, new_minor_version, sudo_password, host)
        c.close()

    latest_version, latest_package_versions = get_target_k8s_version(
        client, target_version_selector, sudo_password, control_host
    )
    if requested_target_version:
        logging.info(
            f"Current version: {current_version}, Requested target: {requested_target_version}, Resolved target: {latest_version} ({latest_package_versions})"
        )
    else:
        logging.info(
            f"Current version: {current_version}, Target version: {latest_version} ({latest_package_versions})"
        )

    input(f"New version will be {latest_version}. Continue?")

    # Upgrade control plane node
    first_control_version = get_node_installed_k8s_version(
        client, sudo_password, control_host
    )
    if first_control_version == latest_version:
        logging.info(
            f"[{control_host}] Control plane already at target version {latest_version}; skipping first control plane upgrade"
        )
    else:
        logging.info(f"[{control_host}] Upgrading control plane node")
        upgrade_k8s_node(
            client,
            client,
            control_host,
            latest_version,
            latest_package_versions,
            sudo_password,
            control_host,
            node_mode=NODE_MODE_FIRST_CONTROL,
        )

    # Upgrade remaining control plane nodes
    for host in nodes["control"][1:]:
        logging.info(f"[{host}] Upgrading additional control plane node")
        c = ssh_connect(ssh_config, host)
        upgrade_k8s_node(
            c,
            client,
            control_host,
            latest_version,
            latest_package_versions,
            sudo_password,
            host,
            node_mode=NODE_MODE_CONTROL,
        )
        c.close()

    # Upgrade worker nodes
    for host in nodes["worker"]:
        logging.info(f"[{host}] Upgrading worker node")
        c = ssh_connect(ssh_config, host)
        upgrade_k8s_node(
            c,
            client,
            control_host,
            latest_version,
            latest_package_versions,
            sudo_password,
            host,
            node_mode=NODE_MODE_WORKER,
        )
        c.close()

    client.close()
    logging.info("Kubernetes upgrade completed successfully!")


if __name__ == "__main__":
    main()
