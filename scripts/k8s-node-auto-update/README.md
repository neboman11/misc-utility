# Kubernetes node auto-update

This `uv` project installs available APT package updates on one Kubernetes
node, notifies the configured ntfy topic, and writes `/var/run/reboot-required`
when changes were installed. A later reboot mechanism can use that marker.

It intentionally updates one node at a time; the Ansible weekly cron schedule
is staggered per host to avoid every node updating simultaneously.

## Run locally

The program must run as root and requires an ntfy authentication token:

```sh
sudo env NTFY_AUTH_TOKEN="..." uv run k8s-node-auto-update
```

Optional configuration:

- `NTFY_URL` — ntfy topic URL. Defaults to `https://ntfy.nesbitt.rocks/servers`.
- `NTFY_AUTH_TOKEN` — required bearer token for the ntfy topic.

## Test

```sh
uv run python -m unittest discover -s tests -v
```
