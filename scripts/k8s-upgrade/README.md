# k8s-upgrade

Rolling Kubernetes version upgrade script for kubeadm-managed clusters. Connects to each node over SSH and performs the full upgrade sequence: cordon → drain → upgrade packages → restart kubelet → uncordon.

## Requirements

- Python 3.12+
- `paramiko` (`pip install paramiko` or `uv sync`)
- SSH access to all cluster nodes (key-based auth, configured in `~/.ssh/config`)
- Sudo access on each node

## Setup

1. Copy `inventory.txt.example` to `inventory.txt` and fill in your nodes:

   ```
   cp-1   control
   cp-2   control
   worker-1  worker
   worker-2  worker
   ```

2. Install dependencies:

   ```
   uv sync
   ```

## Usage

```
python main.py [--target-version VERSION]
```

**`--target-version`** — Kubernetes version to upgrade to. Accepts:
- `X.Y` — upgrades to the latest available patch of that minor (e.g. `1.31`)
- `X.Y.Z` — pins to an exact patch version (e.g. `1.31.2`)

If omitted, the script auto-detects the current cluster version and upgrades to the next minor.

The script will pause and ask for confirmation before applying any changes.

## What it does

1. Reads `inventory.txt` to discover control-plane and worker nodes
2. SSHs into the first control-plane node and queries the live cluster version via `kubectl version`
3. Updates the Kubernetes apt source on every node to the target minor version
4. Resolves the exact package versions available for `kubeadm`, `kubelet`, and `kubectl`
5. Upgrades nodes in order: first control plane → additional control planes → workers
   - First control plane runs `kubeadm upgrade apply`
   - All other nodes run `kubeadm upgrade node`
   - Workers get a 10-second drain delay to allow in-flight requests to finish
6. kubectl commands are run remotely from the first control-plane node (falls back to `super-admin.conf` on auth errors)

## Running tests

```
python -m pytest
```
