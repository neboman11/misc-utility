import apt
import os
import requests
import socket


def main():
    hostname = socket.gethostname()
    # Check if the reboot file exists and skip the rest of the script if it does
    if os.path.exists("/var/run/reboot-required"):
        response = requests.post(
            "http://ponyboy.apartment/send_discord_message",
            json={
                "user_id": 178748204999901185,
                "message": f"{hostname}: Node is already scheduled for reboot. Skipping updates.",
            },
        )
        if not response.ok:
            print(response.text)
        return

    cache = apt.Cache()

    print("Updating package cache...")
    cache.update()
    cache.open(None)

    print("Determining packages to upgrade...")
    cache.upgrade()
    changes = sorted([package.name for package in cache.get_changes()])

    print("Sending package list to discord...")
    changes_notification_message = (
        f"{hostname}: The following updates will be applied:\n"
    )
    for package in changes:
        changes_notification_message += f"{package}, "

    # Remove the trailing comma and space
    changes_notification_message = changes_notification_message[:-2]

    response = requests.post(
        "http://ponyboy.apartment/send_discord_message",
        json={"user_id": 178748204999901185, "message": changes_notification_message},
    )
    if not response.ok:
        print(response.text)

    print("Performing package upgrade...")
    cache.commit()

    print("Notifying user of completion")
    response = requests.post(
        "http://ponyboy.apartment/send_discord_message",
        json={
            "user_id": 178748204999901185,
            "message": f"{hostname}: Updates have been applied, scheduling the node to be rebooted.",
        },
    )
    if not response.ok:
        print(response.text)

    # Creating this file will schedule the node for a reboot
    open("/var/run/reboot-required", "a").close()


if __name__ == "__main__":
    if os.geteuid() != 0:
        exit(
            "You need to have root privileges to run this script.\nPlease try again, this time using 'sudo'. Exiting"
        )
    main()
