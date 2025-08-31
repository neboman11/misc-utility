#!/usr/bin/env python3
import paramiko
import re

INVENTORY_FILE = "inventory.txt"  # Format: IP role (control/worker)
K8S_LIST_FILE = "/etc/apt/sources.list.d/kubernetes.list"
SSH_USER = "your_ssh_user"
SSH_KEY_FILE = "/path/to/private/key"


def read_inventory():
    nodes = {"control": [], "worker": []}
    with open(INVENTORY_FILE) as f:
        for line in f:
            if not line.strip():
                continue
            ip, role = line.strip().split()
            if role not in nodes:
                raise ValueError(f"Unknown role '{role}' in inventory")
            nodes[role].append(ip)
    return nodes


def ssh_connect(host):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=SSH_USER, key_filename=SSH_KEY_FILE)
    return client


def run_cmd(client, cmd, sudo=False):
    if sudo:
        cmd = f"sudo -S -p '' {cmd}"
    stdin, stdout, stderr = client.exec_command(cmd)
    if sudo:
        stdin.write("\n")  # assumes passwordless sudo
        stdin.flush()
    output = stdout.read().decode()
    error = stderr.read().decode()
    if error:
        print(f"Error on {cmd}:\n{error}")
    return output.strip()


def get_current_k8s_version(client):
    content = run_cmd(client, f"cat {K8S_LIST_FILE}", sudo=True)
    match = re.search(r"v(\d+\.\d+)/deb", content)
    if match:
        return match.group(1)
    raise ValueError("Could not parse Kubernetes version from sources list")


def get_latest_k8s_version(client):
    output = run_cmd(client, "apt-cache madison kubeadm | head -1", sudo=True)
    match = re.search(r"\|\s*(\d+\.\d+\.\d+)-", output)
    if match:
        return match.group(1)
    raise ValueError("Could not find latest kubeadm version")


def update_k8s_sources(client, new_minor_version):
    content = run_cmd(client, f"cat {K8S_LIST_FILE}", sudo=True)
    new_content = re.sub(r"(v\d+\.\d+)/deb", f"v{new_minor_version}/deb", content)
    tmp_file = "/tmp/kubernetes.list"
    run_cmd(client, f'echo "{new_content}" | sudo tee {tmp_file}', sudo=False)
    run_cmd(client, f"sudo mv {tmp_file} {K8S_LIST_FILE}", sudo=True)
    print(f"Updated {K8S_LIST_FILE} to v{new_minor_version}")


def upgrade_k8s_node(client, kube_version, is_control=False):
    print("Updating apt cache...")
    run_cmd(client, "sudo apt-get update -y", sudo=True)

    print(f"Installing kubeadm={kube_version}-00...")
    run_cmd(client, f"sudo apt-get install -y kubeadm={kube_version}-00", sudo=True)

    if is_control:
        print("Draining control plane node...")
        run_cmd(
            client,
            "sudo kubectl drain $(hostname) --ignore-daemonsets --delete-local-data",
            sudo=True,
        )
        print(f"Applying kubeadm upgrade apply v{kube_version}...")
        run_cmd(client, f"sudo kubeadm upgrade apply -y v{kube_version}", sudo=True)

    print(f"Upgrading kubelet and kubectl to {kube_version}-00...")
    run_cmd(
        client,
        f"sudo apt-get install -y kubelet={kube_version}-00 kubectl={kube_version}-00",
        sudo=True,
    )
    run_cmd(client, "sudo systemctl daemon-reload", sudo=True)
    run_cmd(client, "sudo systemctl restart kubelet", sudo=True)

    if is_control:
        print("Uncordoning control plane node...")
        run_cmd(client, "sudo kubectl uncordon $(hostname)", sudo=True)


def main():
    nodes = read_inventory()
    if not nodes["control"]:
        print("No control plane node found in inventory.")
        return

    # Pick first control plane node for upgrade apply
    control_node = nodes["control"][0]
    client = ssh_connect(control_node)

    current_version = get_current_k8s_version(client)
    latest_version = get_latest_k8s_version(client)
    print(f"Current version: {current_version}, Latest available: {latest_version}")

    # Update sources on all nodes
    all_nodes = nodes["control"] + nodes["worker"]
    new_minor_version = ".".join(latest_version.split(".")[:2])
    for node in all_nodes:
        print(f"Updating sources on {node}...")
        c = ssh_connect(node)
        update_k8s_sources(c, new_minor_version)
        c.close()

    # Upgrade control plane node
    print(f"Upgrading control plane node: {control_node}")
    upgrade_k8s_node(client, latest_version, is_control=True)
    client.close()

    # Upgrade worker nodes
    for node in nodes["worker"]:
        print(f"Upgrading worker node: {node}")
        c = ssh_connect(node)
        upgrade_k8s_node(c, latest_version)
        c.close()

    print("Kubernetes upgrade completed!")


if __name__ == "__main__":
    main()
