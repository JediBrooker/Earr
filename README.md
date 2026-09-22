![GitHub Release](https://img.shields.io/github/v/release/markbeep/AudioBookRequest?style=for-the-badge)
![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fmarkbeep%2FAudioBookRequest%2Fmain%2Fpyproject.toml&style=for-the-badge&logo=python)
[![Discord](https://img.shields.io/discord/1350874252282171522?style=for-the-badge&logo=discord&link=https%3A%2F%2Fdiscord.gg%2FSsFRXWMg7s)](https://discord.gg/SsFRXWMg7s)

![Header](/media/AudioBookRequestIcon.png)

Your tool for handling audiobook requests on a Plex/Audiobookshelf/Jellyfin instance.

If you've heard of Overseer, Ombi, or Jellyseer; this is in the similar vein, <ins>but for audiobooks</ins>.

![Search Page](/media/search_page.png)

## Table of Contents

- [Motivation](#motivation)
  - [Features](#features)
  - [Out of Scope Features](#out-of-scope-features)
- [Getting Started](#getting-started)
  - [Quick Start](#quick-start)
  - [Basic Usage](#basic-usage)
    - [Auto download](#auto-download)
    - [Audiobookshelf Integration](#audiobookshelf-integration)
    - [Organizing Downloads](#organizing-downloads)
    - [OpenID Connect](#openid-connect)
      - [Getting locked out](#getting-locked-out)
    - [Environment Variables](#environment-variables)
- [Contributing](#contributing)
  - [Conventional Commits](#conventional-commits)
  - [Local Development](#local-development)
- [Tools](#tools)

# Motivation

AudioBookRequest aims to be a simple and lightweight tool for managing audiobook requests for your media server. It should be easy to set up and use, while integrating nicely with other common tools in the \*arr stack. AudioBookRequest serves as as the frontend for you and your friends to easily make audiobook wishlists or create requests in an organized fashion.

It is not intended as a full replacement for Readarr/Chaptarr, but instead intended to be used alongside them.

## Features

- Employs the Audible API to make it easy to search for and request audiobooks.
- Add manual audiobook requests for any books not available on Audible.
- Easy user management. Only three assignable groups, made to get out of your way.
- Automatic downloading of requests. Integrate Prowlarr to use all your existing indexer settings and download clients.
- Send notifications to your favorite notification service (apprise, gotify, discord, ntfy, etc.).
- Optionally copy, move or hardlink finished downloads into a folder structure of your choosing, e.g. `/Author/Series/Book`.
- Single image deployment. You can deploy and create your first requests in under 5 minutes.
- SQLite and Postgres support!
- Lightweight website. No bulky javascript files, allowing you to use the website even on low bandwidth.
- Mobile friendly. Search for books for accept requests on the go!

## Out of Scope Features

- AudioBookRequest does **not** rename files nor edit metadata after downloads. Placing finished downloads into a folder structure is supported and opt-in, see [Organizing Downloads](#organizing-downloads), but everything beyond that is left to other tools. ABR also supports multiple REST API endpoints that allow for easy interoptability with scripts and other apps.
  - Combinations:
    - _Know of or have an app or script that works with ABR? Open an issue and I'll add it here or to the docs._
  - Alternatives:
    - [Listenarr](https://github.com/Listenarrs/Listenarr): A more all-in-one approach with built-in file organization.
    - _I'd love to add more alternatives for ABR here. If you know of any good ones, open an issue!_

---

# Getting Started

AudioBookRequest is intended to be deployed using Docker or Kubernetes. For "bare-metal" deployments, read up on [local development](https://github.com/markbeep/AudioBookRequest/wiki/Local-Development) in the wiki.

## Quick Start

Run the image directly:

```bash
docker run -p 8000:8000 -v $(pwd)/config:/config markbeep/audiobookrequest:1
```

Then head to http://localhost:8000.

**NOTE:** AudioBookRequest uses the `/config` directory inside the container for storing configs and data. Mount that directory locally somewhere to ensure persistent data across restarts.

## Basic Usage

1. Logging in the first time the login-type and root admin user has to be configured.
2. Head to `Settings>Users` to create accounts for your friends.
3. Any user can search for books and request them by clicking the `+` button.
4. The admin can head to the wishlist to see all the books that have been requested.

### Auto download

Auto-downloading enables requests by `Trusted` and `Admin` users to directly start downloading once requested.

1. Ensure your Prowlarr instance is correctly set up with any indexers and download clients you want. [More info](https://prowlarr.com/).
2. On Prowlarr, head to `Settings>General` and copy the `API Key`.
3. On AudioBookRequest, head to `Settings>Prowlarr` and enter the API key as well as the base URL of your Prowlarr instance, i.e. `https://prowlarr.example.com`.
4. Head to `Settings>Download` to configure the automatic download settings:
   1. Enable `Auto Download` at the top.
   2. The remaining heuristics determine the ranking of any sources retrieved from Prowlarr.
   3. Indexer flags allow you to add priorities to certain sources like freeleeches.

### Audiobookshelf Integration

Audiobookshelf (ABS) integration lets ABR:

- Check if a book already exists in your ABS library and mark it as downloaded in search results to avoid duplicate requests.
- Trigger a library scan in ABS when a request is marked as downloaded in ABR (manual or automatic), so the new item appears quickly.

Setup steps:

1. In ABS, create an API token for an account with access to your audiobook library (Admin recommended).
2. In ABR, go to Settings > Audiobookshelf and enter:

- Base URL of your ABS server (e.g. https://abs.example.com or http://localhost:13378)
- API Token from step 1
- Select the target Library
- Enable “Use ABS to mark existing books as downloaded” if you want ABR to flag existing titles during search.

Notes:

- ABR searches ABS by ASIN and by “title + first author” to detect existing books; this is a best-effort match and may not catch every case depending on your metadata.
- ABS is automatically asked to scan after successful downloads are marked in ABR. ABS typically auto-detects updates, but this helps pick up changes sooner.

### Organizing Downloads

ABR can place finished downloads into a folder structure of your choosing. This is off by default.

Prowlarr hands grabs to your download client and never reports back, so ABR watches the folder your
download client writes finished downloads to and matches new items there against the releases it
grabbed. Both folders therefore have to be mounted into the ABR container.

```yaml
services:
  audiobookrequest:
    image: markbeep/audiobookrequest:1
    volumes:
      - ./config:/config
      # the same paths your download client uses, so hardlinks work
      - /mnt/data/downloads:/downloads
      - /mnt/data/audiobooks:/audiobooks
```

Head to `Settings>Library` and configure:

1. **Organize downloads** to turn the feature on.
2. **Mode**:
   - `hardlink` keeps a single copy on disk and lets torrents keep seeding. Both folders have to be on
     the same filesystem, which for Docker means the same volume mount, not two separate mounts.
   - `copy` uses twice the space but works everywhere.
   - `move` frees up the download folder but breaks seeding.
3. **Completed downloads folder** and **Library folder**, as seen from inside the container.
4. **Folder structure**, see below.

#### Folder structure

The folder structure is a `/` separated template. Placeholders are replaced with the metadata of
the book:

| Placeholder | Value |
| --- | --- |
| `{author}` | First author |
| `{authors}` | All authors, comma separated |
| `{title}` | Book title |
| `{subtitle}` | Book subtitle |
| `{series}` | Series name, empty if the book is standalone |
| `{series_position}` | Position within the series, e.g. `1` or `2.5` |
| `{narrator}` | First narrator |
| `{narrators}` | All narrators, comma separated |
| `{year}` | Release year |
| `{asin}` | Audible ASIN, or the request id for manual requests |

Two rules keep templates short:

- A folder that would end up empty is left out, so `{author}/{series}/{title}` becomes
  `Andy Weir/The Martian` for a book that is not part of a series.
- Text in square brackets disappears as a whole when a placeholder inside it is empty, which makes
  `[{series_position} - ]{title}` render as just the title for standalone books.

| Template | Series book | Standalone book |
| --- | --- | --- |
| `{author}/{title}` | `Brandon Sanderson/The Way of Kings` | `Andy Weir/The Martian` |
| `{author}/{series}/{title}` | `Brandon Sanderson/The Stormlight Archive/The Way of Kings` | `Andy Weir/The Martian` |
| `{author}/{series}/[{series_position} - ]{title}` | `Brandon Sanderson/The Stormlight Archive/1 - The Way of Kings` | `Andy Weir/The Martian` |
| `{author}/{title} ({year})` | `Brandon Sanderson/The Way of Kings (2010)` | `Andy Weir/The Martian (2013)` |

The settings page previews both cases while you type. File names inside the folder are left alone.

Notes:

- Audible does not always expose series information. The series position is read from the subtitle
  and may be missing for some books. Manual requests never have a series.
- A download is only picked up once it stopped changing and has no partial files left.
- Existing files are never replaced unless **Overwrite existing files** is turned on.
- If a download is not picked up because the download client renamed it, lower the **Match
  threshold**, or use `POST /api/library/import` with the ASIN and the path to organize it by hand.

### OpenID Connect

Head to the [OpenID Connect](https://github.com/markbeep/AudioBookRequest/wiki/OpenID-Connect) page in the wiki to learn how to set up OIDC authentication with your favorite auth provider.

### Environment Variables

Head to the [environment variables](https://github.com/markbeep/AudioBookRequest/wiki/Environment-Variables) page in the wiki.

---

# Contributing

Please read the [contribution guidelines](?tab=contributing-ov-file) before contributing.

## Conventional Commits

This project uses [Conventional Commits](https://www.conventionalcommits.org) to allow for a more organized commit history and support automated changelog generation. Pull requests will be squashed in most cases (with some exceptions).

## Local Development

Head to the [local development](https://github.com/markbeep/AudioBookRequest/wiki/Local-Development) page in the wiki.

# Tools

AudioBookRequest builds on top of a some other great open-source tools. A big thanks goes out to these developers.

- [Prowlarr](https://github.com/Prowlarr/Prowlarr) - Does a lot of the heavy lifting concerning searching through indexers and forwarding download requests to download clients. Saves me the ordeal of having to reimplement everything again.
- [External Audible API](https://audible.readthedocs.io/en/latest/misc/external_api.html) - Audible exposes key API endpoints which are used to, for example, search for books.
