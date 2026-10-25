#!/usr/bin/env python3

import getpass
import hashlib
import imaplib
import subprocess
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from email.headerregistry import Address
from pathlib import Path


CONFIG = Path.home() / ".config" / "bootstraprc"


def load_config():
    config = {}

    if not CONFIG.is_file():
        raise SystemExit(f"Config file not found: {CONFIG}")

    for line in CONFIG.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")

    return config


def get_imap_password(config):
    """Get the IMAP password from KeePassXC if configured, otherwise prompt."""
    keepass_db = config.get("keepass")

    if not keepass_db:
        return getpass.getpass("IMAP password: ")

    keepass_entry = config.get("keepass_entry")
    if not keepass_entry:
        raise SystemExit(
            "Config has 'keepass' set but no 'keepass_entry' "
            "(the entry title/path inside the database)."
        )

    keepass_db = Path(keepass_db).expanduser()
    if not keepass_db.is_file():
        raise SystemExit(f"KeePassXC database not found: {keepass_db}")

    # Only capture stdout, so the master-password prompt (stderr) and
    # the actual prompt input (stdin) still go through the terminal
    # normally.
    result = subprocess.run(
        ["keepassxc-cli", "show", "-a", "Password", str(keepass_db), keepass_entry],
        stdout=subprocess.PIPE,
        text=True,
    )

    password = result.stdout.strip()

    if result.returncode != 0 or not password:
        raise SystemExit("Failed to retrieve password from KeePassXC.")

    return password


config = load_config()

user = config["user"]
imap_server = config["imap"]
folder = config["folder"]
attachment = Path(config["attachment"]).expanduser()

if not attachment.is_file():
    raise SystemExit(f"Attachment not found: {attachment}")


# Get the IMAP password, either from KeePassXC or by prompting directly.
password = get_imap_password(config)


# Calculate SHA-256.
sha256 = hashlib.sha256(attachment.read_bytes()).hexdigest()
filename = attachment.name


# Use a single "now" for the subject, the Date header, and INTERNALDATE,
# so they all agree.
now = datetime.now().astimezone()

# Create subject and body.
subject = f"{now:%Y-%m-%d} {filename}"
body = f"sha256sum {sha256} {filename}"


# Build the email.
message = EmailMessage()
message["From"] = Address(display_name=user.split('@')[0], addr_spec=user)
message["To"] = user
message["Subject"] = subject
message["Date"] = format_datetime(now)
message.set_content(body)


# Attach the actual file.
message.add_attachment(
    attachment.read_bytes(),
    maintype="application",
    subtype="octet-stream",
    filename=filename,
)


with imaplib.IMAP4_SSL(imap_server) as imap:
    print(f"Connecting to {imap_server}...")

    imap.login(user, password)

    print(f"Saving message to {folder}...")

    status, data = imap.append(
        folder,
        None,
        now,
        message.as_bytes(),
    )

    if status != "OK":
        raise SystemExit(f"Failed to save message to {folder}")

    imap.logout()


print(f"\nSaved to {folder}")
print(f"Subject: {subject}")
print(f"Attachment: {filename}")
print(f"SHA-256: {sha256}")
