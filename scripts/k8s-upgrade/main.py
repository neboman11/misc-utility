#!/usr/bin/env python3
import paramiko
import re
import os
import getpass
import logging
import sys

INVENTORY_FILE = "inventory.txt"  # Format: hostname role (control/worker)
K8S_LIST_FILE = "/etc/apt/sources.list.d/kubernetes.list"
SSH_CONFIG_FILE = os.path.expanduser("~/.ssh/config")

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
    return hostname, user, keyfile, port


def ssh_connect(hostname, user, keyfile, port=22):
    logging.info(f"Connecting to {hostname} as {user}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=hostname, username=user, key_filename=keyfile, port=port)
    return client


def run_cmd(client, cmd, sudo_password="", sudo=False, host=None):
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
        logging.error(f"[{host}] Command failed with exit code {exit_status}: {cmd}")
        logging.error(f"[{host}] Stderr: {error.strip()}")
        sys.exit(1)
    if error.strip():
        logging.warning(f"[{host}] Warning output: {error.strip()}")
    return output.strip()


def get_current_k8s_version(client, sudo_password, host):
    content = run_cmd(
        client,
        f"cat {K8S_LIST_FILE}",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )
    match = re.search(r"v(\d+\.\d+)/deb", content)
    if match:
        logging.info(
            f"[{host}] Current Kubernetes version in sources: {match.group(1)}"
        )
        return match.group(1)
    logging.error(f"[{host}] Could not parse Kubernetes version from sources list")
    sys.exit(1)


def get_latest_k8s_version(client, sudo_password, host):
    output = run_cmd(
        client,
        "apt-cache madison kubeadm | head -1",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )
    match = re.search(r"\|\s*(\d+\.\d+\.\d+)-", output)
    if match:
        logging.info(f"[{host}] Latest available kubeadm version: {match.group(1)}")
        return match.group(1)
    logging.error(f"[{host}] Could not find latest kubeadm version")
    sys.exit(1)


def update_k8s_sources(client, new_minor_version, sudo_password, host):
    logging.info(f"[{host}] Updating Kubernetes apt sources to v{new_minor_version}")

    # sed command to replace the version in-place
    sed_cmd = f"sed -i 's|/v[0-9]\\+\\.[0-9]\\+/deb/|/v{new_minor_version}/deb/|' {K8S_LIST_FILE}"

    # Run with sudo
    run_cmd(client, sed_cmd, sudo_password=sudo_password, sudo=True, host=host)

    logging.info(f"[{host}] Kubernetes sources updated successfully")


def upgrade_k8s_node(client, kube_version, sudo_password, host, is_control=False):
    logging.info(f"[{host}] Starting upgrade process")

    # Remove hold before upgrade
    remove_hold(client, sudo_password, host)

    # Update apt and upgrade kubeadm
    run_cmd(
        client, "apt-get update -y", sudo_password=sudo_password, sudo=True, host=host
    )
    run_cmd(
        client,
        f"apt-get install -y kubeadm={kube_version}-1.1",
        sudo_password=sudo_password,
        sudo=True,
        host=host,
    )

    if is_control:
        logging.info(f"[{host}] Draining control plane node")
        run_cmd(
            client,
            "kubectl drain $(hostname) --ignore-daemonsets --delete-local-data",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )
        logging.info(f"[{host}] Applying kubeadm upgrade")
        run_cmd(
            client,
            f"kubeadm upgrade apply -y v{kube_version}",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )

    # Upgrade kubelet and kubectl
    run_cmd(
        client,
        f"apt-get install -y kubelet={kube_version}-1.1 kubectl={kube_version}-1.1",
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

    if is_control:
        logging.info(f"[{host}] Uncordoning control plane node")
        run_cmd(
            client,
            "kubectl uncordon $(hostname)",
            sudo_password=sudo_password,
            sudo=True,
            host=host,
        )

    logging.info(f"[{host}] Upgrade process completed")


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


def main():
    nodes = read_inventory()
    if not nodes["control"]:
        logging.error("No control plane node found in inventory.")
        sys.exit(1)

    ssh_config = load_ssh_config()
    sudo_password = getpass.getpass("Enter sudo password for all nodes: ")

    # Pick first control plane node for upgrade apply
    control_host = nodes["control"][0]
    hostname, user, keyfile, port = get_ssh_params(ssh_config, control_host)
    client = ssh_connect(hostname, user, keyfile, port)

    current_version = get_current_k8s_version(client, sudo_password, control_host)
    latest_version = get_latest_k8s_version(client, sudo_password, control_host)
    logging.info(
        f"Current version: {current_version}, Latest available: {latest_version}"
    )

    all_hosts = nodes["control"] + nodes["worker"]
    new_minor_version = ".".join(latest_version.split(".")[:2])

    # Update sources on all nodes
    for host in all_hosts:
        hostname, user, keyfile, port = get_ssh_params(ssh_config, host)
        logging.info(f"[{host}] Connecting to update sources")
        c = ssh_connect(hostname, user, keyfile, port)
        update_k8s_sources(c, new_minor_version, sudo_password, host)
        c.close()

    # Upgrade control plane node
    logging.info(f"[{control_host}] Upgrading control plane node")
    upgrade_k8s_node(
        client, latest_version, sudo_password, control_host, is_control=True
    )
    client.close()

    # Upgrade worker nodes
    for host in nodes["worker"]:
        hostname, user, keyfile, port = get_ssh_params(ssh_config, host)
        logging.info(f"[{host}] Upgrading worker node")
        c = ssh_connect(hostname, user, keyfile, port)
        upgrade_k8s_node(c, latest_version, sudo_password, host)
        c.close()

    logging.info("Kubernetes upgrade completed successfully!")


if __name__ == "__main__":
    main()
