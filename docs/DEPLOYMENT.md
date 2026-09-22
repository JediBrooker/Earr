# Deployment

How this fork is built, shipped and configured. Written against a Proxmox host
running the media stack as LXC containers, but only the container layout is
specific to that.

## Pipeline

```
push to main  ->  GitHub Actions  ->  ghcr.io/<owner>/earr:latest  ->  watchtower  ->  running
```

`.github/workflows/build.yaml` runs the checks and, on `main`, builds and
pushes. It authenticates to GHCR with the `GITHUB_TOKEN` Actions provides, so
there are no secrets to configure on a fork.

Every build is tagged `latest` and `sha-<short>`, and the commit is baked in as
the version, which the UI shows at the bottom of any settings page. To roll
back, pin the `sha-` tag in the compose file.

The package must be **public** for watchtower to pull it anonymously. A private
package needs a registry login in the container's docker config instead.

## The container

`/opt/audiobookrequest/docker-compose.yml`:

```yaml
services:
  earr:
    image: ghcr.io/<owner>/earr:latest
    container_name: earr
    restart: unless-stopped
    # Docker's bridge NAT on these LXCs drops UDP/53, which breaks outbound DNS
    # for the Audible lookups. The LXC itself resolves fine, so use its stack.
    network_mode: host
    volumes:
      - ./config:/config
      - /data:/data
    environment:
      TZ: Australia/Sydney
      EARR_APP__PORT: "8000"

  watchtower:
    image: nickfedor/watchtower:latest
    restart: unless-stopped
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=86400
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

The environment prefix is `EARR_`, not `ABR_`. That changed with the rebrand and
existing deployments need their variables renamed.

To deploy immediately rather than waiting for watchtower's daily poll:

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  nickfedor/watchtower:latest --run-once --cleanup earr
```

## Storage

The library organizer hardlinks finished downloads into the library, which only
works when the downloads and the library are on the same filesystem **and** every
container sees them at the same paths.

In a Proxmox setup that means the same mount point on each container:

```
mp0: /data/subvol-100-disk-0,mp=/data
```

Add it to the Earr container the same way the download clients and the media
server have it:

```sh
pct set <ctid> -mp0 /data/subvol-100-disk-0,mp=/data
pct reboot <ctid>
```

Paths reported by a download client are used verbatim, so if a client sees
`/data/torrents/...` and Earr sees `/mnt/data/torrents/...` the import fails with
a "path the client reported does not exist here" warning. Mount them identically.

## Settings

Under `Settings > Library`:

| Setting | Notes |
| --- | --- |
| Mode | `hardlink` keeps torrents seeding and costs no extra space |
| Completed downloads folders | one per line; only a fallback when a download client is configured |
| Folder structure | see the README |
| Write metadata.json | on by default |

### Download clients

Optional but strongly preferred. Without one, a finished download is found by
matching the release name against folder names, which fails when the client
renames the folder: a release listed as `Silverthorn by Raymond E Feist [ENG /
M4B]` can arrive as `03 Silverthorn`.

With one, Earr asks the client where the files are. Torrents are keyed on the
info hash, so the lookup is exact.

- **qBittorrent**: URL, username, password. The WebUI port is not the torrent
  port; check `WebUI\Port`. Authentication is required unless the subnet
  whitelist is enabled, which is worth thinking about before turning on: an
  unauthenticated WebUI exposes the external-program hook to everything on that
  subnet.
- **SABnzbd**: URL and API key from `sabnzbd.ini`.

### Categories

Prowlarr decides the category when it hands a release to a download client and
its API has no field to override it, so grabs land under Prowlarr's own
category. Earr moves them afterwards if a category is set.

The category has to already exist in the client, because what makes it useful is
the save path attached to it. In qBittorrent, changing the category relocates the
files when Auto Torrent Management is on, which is the point.

## Conflicts with post-processing scripts

Earr organizing the library is redundant with, and will duplicate, any existing
post-processing script that does the same thing. Both hardlink, so no space is
wasted, but the library ends up with two entries per book under different names.

Check for and disable:

- qBittorrent: `Tools > Options > Downloads > Run external program`, or
  `[AutoRun] enabled` in `qBittorrent.conf`
- SABnzbd: the `script` on each category in `Settings > Categories`

Prefer changing these through each client's API or UI rather than editing config
files, since both rewrite their configs on shutdown and will discard a hand edit.

## Checks

```sh
just test              # pytest
just types             # basedpyright
just test_format       # djlint + ruff
just test_jinja        # jinjax template parameters
just check_migrations  # alembic upgrade + check
```

CI runs all of these. Migrations are verified against SQLite and Postgres,
including a downgrade to base and back.
