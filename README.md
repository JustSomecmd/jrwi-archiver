# JRWI archiver

A self-hosted service that keeps a private copy of the Just Roll With It members area (jrwishow.com/supporters). It checks the site on a schedule and saves everything your membership tier can see:

- members-only video, laid out as TV shows for Jellyfin
- audio episodes, which it plays itself and publishes as a private podcast feed
- the text of every post, with images, art, attachments and linked Google Docs (transcripts)
- comments and replies, like counts, tags and pinned posts
- the site's own pages (show pages, News, FAQs), plus its logo and show art

It comes with a web app for browsing the archive and running it, and an API for apps such as Tubedroid.

The archive is for personal use. Keep it private: don't put it on the open internet, since it republishes paid episodes and other members' comments.

## Setting it up

1. Make the folders, owned by the user the container runs as:

   ```sh
   mkdir -p jrwi/data jrwi/archive /srv/media/jrwi
   ```

2. Add the service from [`docker-compose.example.yml`](docker-compose.example.yml) to your compose file. Change `/srv/media/jrwi` to wherever your media lives, and `user:` to your own user and group IDs (run `id` to see them).
3. Start it with `docker compose up -d jrwi-archiver` and open `http://<server>:8090`.
4. Choose a password for the web app. You'll go straight to **Settings**.
5. Enter your jrwishow.com email and password, then save.
6. Go to **Admin** and press **Check now**.
   - The first check reads the whole members feed and downloads everything, which takes a long time.
   - After that, it checks every 4 hours by default and only fetches what's new.

### Jellyfin

Mount the same video folder into Jellyfin, for example `/srv/media/jrwi:/media/jrwi:ro`, and add it as a **Shows** library.

- **Episodes:** each show gets a folder, such as `Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-286486].mp4`.
- **Specials:** aftershows, extras and unnumbered videos go in `Season 00`.
- **Metadata:** every episode has a `.nfo` with the title, date and post text, plus a thumbnail. Every show has a poster.
- **Matching:** the `[jrwi-<post id>]` tag in each file name is how apps find the post that belongs to a Jellyfin episode.

### Tubedroid

In Tubedroid's JRWI settings, enter the server's address (`http://<server>:8090`) and the **API token** from Settings.

### Podcast apps and Audiobookshelf

To get the audio in a podcast app, first set **This server's address** in Settings. Then subscribe to:

```
http://<server>:8090/api/feed.xml?token=<API token>
```

### YouTube

Some episodes are posted on the members site only as YouTube embeds. The archiver records the YouTube link, and its API tells apps the YouTube ID, but by default it doesn't download those videos. TubeArchivist is the better place for them: subscribe to the Just Roll With It channel there. Tubedroid can then play the episode from TubeArchivist's copy. If you'd rather have the archiver download them, there's a setting for that.

## What you'll find in the folders

```
data/       jrwi.db (everything the web app shows), cookies.json (the jrwishow.com login session)
archive/
  posts/<post id>/
    page.html.gz          the post page exactly as downloaded
    comments_001.html.gz  comment threads exactly as downloaded
    images/  attachments/  thumbnail.webp  <audio>.m4a
  google-docs/            linked Google Docs, saved as PDF or XLSX
  pages/                  the site's own pages and their images
  shows/  site/  avatars/ show art, the site's logo and icons, commenters' pictures
media/jrwi/               video for Jellyfin
```

Every post page and comment thread is kept as it was downloaded. If a later version of this app reads pages better (for example, picking up a detail it missed), **Admin > Re-read saved pages** applies that to the whole archive without downloading anything again.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Check every | 4 hours | 0 means only when you press Check now |
| Seconds between page requests | 1.5 | Keeps the load on the site low |
| What to save | everything except YouTube embeds | Video, audio, images, attachments, comments, Google Docs, site pages |
| Video quality | Best available | Lower it to save disk space |
| Re-read comments on posts newer than | 30 days | Picks up comments that arrive after a post goes up |
| Notification URL | none | An ntfy topic URL or a Discord webhook. It gets new posts and problems such as a failed login. |

You can also set these environment variables:

- **`JRWI_EMAIL` and `JRWI_PASSWORD`:** the jrwishow.com login. They take priority over the Settings page.
- **`JRWI_WEB_PASSWORD`:** sets the web app's password on first start.
- **`TZ`:** the time zone.

## Disk space

Video takes up nearly all of the space. A three-hour episode at full quality is several gigabytes, so the whole library can pass a terabyte. If that's too much, lower **Video quality** or turn video off. Most episodes also have an audio-only version.

## Updating

```sh
docker compose pull jrwi-archiver && docker compose up -d jrwi-archiver
```

A new image is built for every change, and every week to pick up new yt-dlp releases.

If pulling the image fails with "denied", make the package public. On GitHub, go to your profile, then Packages, then jrwi-archiver, then Package settings. Alternatively, log in with `docker login ghcr.io`.

## API

The API is described in [`docs/api.md`](docs/api.md). Every request needs the header `Authorization: Token <API token>`, or `?token=<API token>` in the address.

## Development

```sh
pip install -r requirements.txt
sh tests/make_media.sh        # small test video, audio and images (needs ffmpeg)
python -m pytest tests        # runs a full check against a mock of the site
```

To run it locally against the mock site:

```sh
python tests/mock_site.py 8765 &
JRWI_DATA=./data JRWI_ARCHIVE=./archive JRWI_MEDIA=./media \
JRWI_SITE=http://127.0.0.1:8765 JRWI_MUX_BASE=http://127.0.0.1:8765/mux \
JRWI_EMAIL=fan@example.com JRWI_PASSWORD=hunter2 python -m jrwi serve
```

**Not yet checked against the real members-only pages:** the layout of comments and of the members-only video player. The tests use plausible markup, and everything downloaded is kept raw so it can be re-read once the parser is adjusted.

## Standalone script

[`standalone/`](standalone/) has the original one-off script for running on a desktop computer without Docker. Its `--dump-html` option saves raw member pages, which is the easiest way to show how logged-in pages are laid out.
