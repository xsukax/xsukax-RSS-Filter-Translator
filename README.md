# xsukax RSS Filter & Translator

A lightweight, self-hosted web application for creating filtered RSS feeds with optional, fully local machine translation.

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

## Overview

xsukax RSS Filter & Translator turns an existing RSS or Atom feed into a new RSS 2.0 feed tailored to your interests. Through a password-protected web interface, you can filter entries by keywords, choose whether to search titles or titles and descriptions, limit the number of returned items, and optionally translate feed content into another language.

Translation is performed on the host machine with [Argos Translate](https://www.argosopentech.com/). No cloud translation API or API key is required. Installed translation models, application settings, generated-feed definitions, and translation results remain on your server in local storage.

The application is designed for personal servers and small VPS deployments. It uses Flask for the web application, Waitress as the production WSGI server, SQLite for persistence and caching, and `feedparser` for RSS/Atom parsing.

## Features

- Generate reusable RSS 2.0 feed URLs from existing RSS or Atom sources.
- Filter entries using case-insensitive keyword matching with OR logic.
- Search titles only or both titles and descriptions.
- Add up to 30 keywords, or leave them empty to include every entry.
- Return between 1 and 200 items per generated feed; the default is 20.
- Translate titles and descriptions locally with Argos Translate.
- Detect the source language when the upstream feed does not declare it.
- Install and remove translation models from the web interface.
- Cache translations in SQLite to avoid repeating expensive translation work.
- Cache rendered feeds for 15 minutes by default.
- Retry HTTP 403 responses with a feed-reader user agent.
- Reject source-feed downloads larger than 5 MiB by default.
- Protect administrative pages with password authentication and hashed password storage.
- Apply temporary login lockouts after repeated failed attempts.
- Expose a health endpoint with an optional translation self-test.
- Install as an unprivileged systemd service on supported Linux distributions.

> [!IMPORTANT]
> The administrative interface uses the default password `xsukax` on first launch. Change it immediately after signing in.

## Prerequisites

### Automated server installation

- A Linux system using `apt`, `dnf`, `yum`, or `apk`.
- `systemd` and the `systemctl` command.
- Root or `sudo` access.
- Python 3.9 or newer. The installer attempts to install Python if it is missing.
- At least 3 GB of free disk space during installation.
- At least 1 GB of RAM recommended for local translation.
- Internet access during dependency installation and the initial download of each translation model.

Translation models commonly consume approximately 100–300 MB of disk space and memory per loaded model. Actual requirements vary by language pair.

### Manual or development installation

- Python 3.9 or newer.
- Python virtual-environment support (`venv`).
- `pip`.
- Git, if cloning the repository instead of downloading an archive.

## Installation

### Option 1: Automated Linux installation

Clone the repository and run the installer from the project directory:

```bash
git clone https://github.com/xsukax/xsukax-RSS-Filter-Translator.git
cd xsukax-RSS-Filter-Translator
sudo bash install.sh
```

The installer performs the following actions:

1. Verifies that Python 3.9+ and virtual-environment support are available.
2. Creates an unprivileged `xsukax` system account.
3. Copies the application to `/opt/xsukax-rss-filter`.
4. Creates a Python virtual environment and installs the dependencies.
5. Stores persistent data under `/opt/xsukax-rss-filter/data`.
6. Registers and starts `xsukax-rss-filter.service`.
7. Opens TCP port `6985` when an active UFW or firewalld configuration is detected.

After installation, open:

```text
http://SERVER_IP:6985/
```

Sign in with `xsukax`, change the password, and then install the required translation models.

Check the service status or follow its logs with:

```bash
sudo systemctl status xsukax-rss-filter
sudo journalctl -u xsukax-rss-filter -f
```

### Option 2: Manual installation

```bash
git clone https://github.com/xsukax/xsukax-RSS-Filter-Translator.git
cd xsukax-RSS-Filter-Translator

python3 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python app.py
```

The application listens on `http://0.0.0.0:6985` by default. Stop it with `Ctrl+C`.

### Uninstallation

Remove the service and application code while preserving the database and downloaded models:

```bash
sudo bash install.sh uninstall
```

Remove the service, application code, database, caches, and translation models:

```bash
sudo bash install.sh uninstall --purge
```

> [!CAUTION]
> The `--purge` option permanently deletes `/opt/xsukax-rss-filter`, including generated-feed definitions, settings, caches, and installed translation models. Back up the data directory first if you may need it later.

## Configuration

Configuration is supplied through environment variables. Set them before starting `app.py`, or add/update the corresponding `Environment=` entries in `/etc/systemd/system/xsukax-rss-filter.service` and restart the service.

| Variable | Default | Description |
| --- | --- | --- |
| `XSUKAX_HOST` | `0.0.0.0` | Network address on which the application listens. |
| `XSUKAX_PORT` | `6985` | TCP port used by the web interface and generated feeds. |
| `XSUKAX_DB` | `./xsukax_rss.db` | Absolute or relative path to the SQLite database. |
| `XSUKAX_FEED_TTL` | `900` | Rendered-feed cache lifetime in seconds. |
| `XSUKAX_MAX_DL_BYTES` | `5242880` | Maximum accepted size of an upstream feed in bytes. |
| `XSUKAX_TR_BUDGET` | `120` | Translation time budget per feed build in seconds; use `0` for no time limit. |
| `XSUKAX_UA` | Browser-style user agent | User-Agent header used when retrieving upstream feeds. |
| `XDG_DATA_HOME` | `<database-directory>/xdg-data` | Storage location for Argos Translate models and application data. |
| `XDG_CACHE_HOME` | `<database-directory>/xdg-cache` | Cache location used by the sentence splitter and translation stack. |
| `XDG_CONFIG_HOME` | `<database-directory>/xdg-config` | Configuration location used by Argos Translate. |
| `ARGOS_CHUNK_TYPE` | `MINISBD` | Sentence-splitting backend. Keep this value unless you have tested another backend. |

The automated installer additionally assigns the application data directory as the service account's `HOME` because the `xsukax` system user has no normal home directory.

Example custom development configuration:

```bash
export XSUKAX_HOST=127.0.0.1
export XSUKAX_PORT=8080
export XSUKAX_DB="$PWD/data/xsukax_rss.db"
export XSUKAX_FEED_TTL=300
./venv/bin/python app.py
```

Standard `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` environment variables are honored by Requests when upstream feeds are downloaded.

### Reverse proxy

For an internet-facing deployment, place the application behind a reverse proxy such as Nginx, Caddy, or Apache and terminate HTTPS at the proxy. Forward traffic to `127.0.0.1:6985` and set `XSUKAX_HOST=127.0.0.1` when the application should not accept direct external connections.

## Usage

### 1. Sign in and secure the interface

Open the web interface and sign in with the initial password `xsukax`. Select **Change password** and replace it with a strong, unique password.

### 2. Install a translation model

Open **Translation models**, select the required source-to-target language pair, and start the installation. Model installation runs in the background. The first installation requires internet access to retrieve the Argos package index, the selected model, and the MiniSBD sentence-splitter data.

Skip this step if you only need filtering without translation.

### 3. Generate a filtered feed

On the generator page:

1. Enter a valid `http://` or `https://` RSS/Atom source URL.
2. Enter one or more keywords, or leave every keyword field empty.
3. Choose **Title only** or **Title + description** matching.
4. Select the maximum number of items.
5. Choose an available target language or keep the original language.
6. Generate the feed and copy the resulting URL into your RSS reader.

Keyword matching is case-insensitive. An item is included when any configured keyword appears in the selected fields. Keywords are evaluated against the original feed before translation, so enter them in the source feed's language.

Generated feed URLs use either of these forms:

```text
http://SERVER_IP:6985/feed/RANDOM_TOKEN
http://SERVER_IP:6985/feed/RANDOM_TOKEN.xml
```

The URLs are intentionally accessible without an authenticated browser session so RSS readers can retrieve them. Treat each URL as a secret; it remains valid for as long as its token is stored in the application database.

### Health checks

Basic service status:

```bash
curl --fail http://127.0.0.1:6985/health
```

List installed translation models and run a real translation self-test for a target language:

```bash
curl --fail 'http://127.0.0.1:6985/health?check=1&tl=en'
```

The translation check succeeds only when a compatible model translating into the requested target language is installed.

## Project Structure

```text
xsukax-rss-filter/
├── app.py
├── install.sh
├── requirements.txt
├── static/
│   └── style.css
├── templates/
│   ├── index.html
│   ├── login.html
│   ├── models.html
│   └── password.html
└── README.md
```

| Path | Purpose |
| --- | --- |
| `app.py` | Flask routes, authentication, SQLite schema, feed retrieval and filtering, RSS rendering, translation, model management, caching, and health checks. |
| `install.sh` | Automated installation, systemd service creation, firewall configuration, and uninstallation. |
| `requirements.txt` | Python runtime dependencies. |
| `templates/index.html` | Feed-generator interface. |
| `templates/login.html` | Authentication page. |
| `templates/models.html` | Translation-model installation and removal interface. |
| `templates/password.html` | Password-change interface. |
| `static/style.css` | Shared web-interface styling. |

Runtime data is not part of the repository. By default, a manual installation creates `xsukax_rss.db` and XDG data/cache/configuration directories beside it. The automated installation stores these files under `/opt/xsukax-rss-filter/data`.

## Troubleshooting

### The service does not start

Inspect the service status and recent logs:

```bash
sudo systemctl status xsukax-rss-filter
sudo journalctl -u xsukax-rss-filter -e
```

Confirm that port `6985` is not already in use and that the service account can write to `/opt/xsukax-rss-filter/data`.

### An upstream feed returns HTTP 403

The application automatically retries once with a feed-reader user agent. If the source blocks your VPS provider or IP range, configure a permitted outbound proxy with `HTTPS_PROXY` and confirm that accessing the feed complies with the source site's terms.

### Installation reports `No space left on device`

The translation stack includes large binary packages. The installer uses a disk-backed temporary directory under `/opt/xsukax-rss-filter` to avoid small `/tmp` RAM filesystems. For a manual installation, point `TMPDIR` to a disk-backed location and verify available disk space before retrying.

### A translation model cannot be installed

Ensure the server can reach the Argos package index and model-hosting services, then review the application logs. Model installation requires temporary internet access, even though translation is local after installation.

### The administrator password was lost

Reset the stored password hash so that the application recreates the default password on restart:

```bash
sudo sqlite3 /opt/xsukax-rss-filter/data/xsukax_rss.db \
  "DELETE FROM settings WHERE key='password_hash';"
sudo systemctl restart xsukax-rss-filter
```

Sign in with `xsukax` and change the password immediately.

## Contributing

Contributions are welcome. Before preparing a change:

1. Search the issue tracker for an existing report or proposal.
2. Open an issue for substantial changes so the approach can be discussed first.
3. Fork the repository and create a focused branch from the default branch.
4. Keep changes small, documented, and consistent with the existing code style.
5. Add or update tests when changing behavior.
6. Verify Python syntax and the installer before submitting:

   ```bash
   python3 -m compileall -q app.py
   bash -n install.sh
   ```

7. Test the relevant workflow manually, including feed generation and translation behavior when applicable.
8. Update this README when introducing new configuration, dependencies, endpoints, or user-visible behavior.
9. Submit a pull request with a clear summary, testing notes, compatibility impact, and links to related issues.

Please avoid committing databases, translation models, caches, virtual environments, credentials, generated feeds containing private tokens, or other runtime data.

## Security

Do not report suspected vulnerabilities in a public issue. Use the repository's private **Security** tab to submit a [GitHub Security Advisory](https://github.com/xsukax/xsukax-RSS-Filter-Translator/security/advisories/new). If private reporting is unavailable, contact a maintainer privately and include reproduction steps, affected versions, impact, and any suggested mitigation.

Deployment recommendations:

- Change the default password before exposing the service to any untrusted network.
- Use HTTPS through a trusted reverse proxy for internet-facing deployments.
- Bind to `127.0.0.1` when only the reverse proxy should reach the application.
- Restrict port `6985` with a host or network firewall when public access is unnecessary.
- Treat generated feed URLs as bearer secrets because they do not require interactive login.
- Do not allow untrusted users to access the generator. It retrieves user-supplied URLs from the server's network and is intended for a trusted administrator.
- Restrict outbound network access if the host can reach sensitive internal services.
- Keep the operating system, Python runtime, and dependencies updated.
- Back up the SQLite database and data directory with permissions that prevent unauthorized access.
- Review reverse-proxy request limits and timeouts to reduce abuse of CPU-intensive translation operations.

## License

This project is distributed under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html) (`GPL-3.0-only`). See the repository's `LICENSE` file for the complete license text.

## Author / Maintainers

- **xsukax** — project author and primary maintainer.

For general questions, bug reports, and feature requests, use the repository's [GitHub Issues](https://github.com/xsukax/xsukax-RSS-Filter-Translator/issues). For security-sensitive reports, follow the private process described in the [Security](#security) section.
