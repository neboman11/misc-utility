#!/usr/bin/env bash
set -euo pipefail

### ============================
### Config
### ============================
INVENTORY_FILE="${INVENTORY_FILE:-./inventory.txt}"
SSH_USER="${SSH_USER:-$USER}"
SSH_PORT="${SSH_PORT:-22}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new}"
DRAIN_ARGS="${DRAIN_ARGS:---ignore-daemonsets --delete-emptydir-data --grace-period=60 --timeout=10m}"
RESTART_CONTAINERS="${RESTART_CONTAINERS:-false}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-$KUBECONFIG}"

### ======================
### Helpers
### ======================
die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo -e "\n==> $*\n"; }

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "Missing required command '$c'"
  done
}

parse_inventory() {
  awk 'NF>=2 { role=$1; host=$2; node=(NF>=3?$3:$2); print role,host,node }' "${INVENTORY_FILE}"
}

remote_sudo() {
  local host="$1"; shift
  ssh -t ${SSH_OPTS} -p "$SSH_PORT" "${SSH_USER}@${host}" "sudo bash -lc '$*'"
}

kubectl_cmd() { KUBECONFIG="${KUBECONFIG_PATH}" kubectl "$@"; }

drain_node() { kubectl_cmd drain "$1" ${DRAIN_ARGS}; }
uncordon_node() { kubectl_cmd uncordon "$1"; }
node_ready_wait() {
  local node="$1"
  info "Waiting for node ${node} to be Ready..."
  until kubectl_cmd get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; do
    sleep 5
  done
  echo "Node ${node} is Ready."
}

### ======================
### Version detection
### ======================
detect_next_version() {
  local leader_host="$1"

  # Current cluster version (from API server)
  local current_full current_minor next_minor
  current_full=$(kubectl_cmd version -o json | jq -r '.serverVersion.gitVersion' | sed 's/^v//')
  current_minor=$(echo "$current_full" | cut -d. -f2)
  next_minor=$(( current_minor + 1 ))

  info "Current cluster version: v${current_full}"
  info "Looking for latest patch in v1.${next_minor}.x (from ${leader_host})"

  # Update repo before checking apt-cache
  update_repo_for_minor "$leader_host" "$next_minor"

  # Ask the leader what versions are available
  local latest_patch
  latest_patch=$(remote_sudo "$leader_host" "
  apt-cache madison kubeadm \
      | awk '{print \\\$3}' \
      | grep -E '^1\\.${next_minor}\\.' \
      | sort -V \
      | tail -n1
  " | tr -d '\r' | head -n1)

  [ -n "$latest_patch" ] || die "No version found in apt-cache for v1.${next_minor}.x on leader node"

  echo "${latest_patch}"
}

### ==========================
### Remote upgrade functions
### ==========================
upgrade_cp_leader() {
  local host="$1" version="$2" short="$3"
  remote_sudo "$host" "
    set -e
    apt-mark unhold kubeadm kubelet kubectl || true
    apt-get update -y
    apt-get install -y kubeadm=${version}
    kubeadm version
    kubeadm upgrade plan
    kubeadm upgrade apply ${short} -y --etcd-upgrade=true
    apt-get install -y kubelet=${version} kubectl=${version}
    systemctl daemon-reload
    systemctl restart kubelet
    $( $RESTART_CONTAINERS && echo "systemctl restart containerd || true" )
    apt-mark hold kubeadm kubelet kubectl || true
  "
}

upgrade_node() {
  local host="$1" version="$2"
  remote_sudo "$host" "
    set -e
    apt-mark unhold kubeadm kubelet kubectl || true
    apt-get update -y
    apt-get install -y kubeadm=${version}
    kubeadm version
    kubeadm upgrade node
    apt-get install -y kubelet=${version} kubectl=${version}
    systemctl daemon-reload
    systemctl restart kubelet
    $( $RESTART_CONTAINERS && echo "systemctl restart containerd || true" )
    apt-mark hold kubeadm kubelet kubectl || true
  "
}

update_repo_for_minor() {
  local host="$1"
  local minor="$2"
  remote_sudo "$host" "sed -i 's|core:/stable:/v[0-9]*\.[0-9]*/deb/|core:/stable:/v1.${minor}/deb/|' /etc/apt/sources.list.d/kubernetes.list"
  remote_sudo "$host" "apt-get update -y"
}

### ==================
### Main flow
### ==================
require_cmd ssh awk kubectl jq
[ -f "${INVENTORY_FILE}" ] || die "Inventory file not found"

# Parse inventory to get leader first
mapfile -t CP_LEADER < <(parse_inventory | awk '$1=="cp-leader"{print $2" "$3}')
(( ${#CP_LEADER[@]} == 1 )) || die "Need exactly one cp-leader in inventory"
CP_LEADER_HOST=$(echo "${CP_LEADER[0]}" | awk '{print $1}')
CP_LEADER_NODE=$(echo "${CP_LEADER[0]}" | awk '{print $2}')

mapfile -t CP_FOLLOWERS < <(parse_inventory | awk '$1=="cp"{print $2" "$3}')
mapfile -t WORKERS < <(parse_inventory | awk '$1=="worker"{print $2" "$3}')

# Detect target version on leader
TARGET_DEB_VERSION=$(detect_next_version "${CP_LEADER_HOST}" | tr -d '\r')
if [ -z "$TARGET_DEB_VERSION" ]; then
  die "Failed to detect next upgrade version from leader node"
fi

# Extract x.y.z part before any dash
TARGET_SHORT_VERSION="v$(echo "$TARGET_DEB_VERSION" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
if [ "$TARGET_SHORT_VERSION" = "v" ]; then
  die "Failed to parse short version from $TARGET_DEB_VERSION"
fi
info "Target upgrade: Debian=${TARGET_DEB_VERSION}, kubeadm short=${TARGET_SHORT_VERSION}"

info "Cluster before upgrade:"
kubectl_cmd get nodes -o wide

# 1. Upgrade leader
drain_node "${CP_LEADER_NODE}"
upgrade_cp_leader "${CP_LEADER_HOST}" "${TARGET_DEB_VERSION}" "${TARGET_SHORT_VERSION}"
uncordon_node "${CP_LEADER_NODE}"
node_ready_wait "${CP_LEADER_NODE}"

# 2. Control-plane followers
for entry in "${CP_FOLLOWERS[@]}"; do
  host=$(echo "$entry" | awk '{print $1}')
  node=$(echo "$entry" | awk '{print $2}')
  # Update repo before upgrading
  update_repo_for_minor "$host" "$(echo "$TARGET_SHORT_VERSION" | cut -d. -f2)"
  drain_node "$node"
  upgrade_node "$host" "$TARGET_DEB_VERSION"
  uncordon_node "$node"
  node_ready_wait "$node"
  
done

# 3. Workers
for entry in "${WORKERS[@]}"; do
  host=$(echo "$entry" | awk '{print $1}')
  node=$(echo "$entry" | awk '{print $2}')
  # Update repo before upgrading
  update_repo_for_minor "$host" "$(echo "$TARGET_SHORT_VERSION" | cut -d. -f2)"
  drain_node "$node"
  upgrade_node "$host" "$TARGET_DEB_VERSION"
  uncordon_node "$node"
  node_ready_wait "$node"
done

info "Cluster after upgrade:"
kubectl_cmd get nodes -o wide
echo "Done."
