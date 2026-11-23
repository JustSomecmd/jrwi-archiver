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
   mkdir -p jrwi-data /srv/jrwi/archive /srv/jrwi/videos
   ```

2. Add the service from [`docker-compose.example.yml`](docker-compose.example.yml) to your compose file. Change `/srv/jrwi` to wherever your media lives, and `user:` to your own user and group IDs (run `id` to see them).
   - **One folder for both:** the archive and the Jellyfin videos sit in one mounted folder (`/jrwi/archive` and `/jrwi/videos` in the container). A finished video is then moved into place instead of copied, which matters for files of several GB.
   - **No VPN needed:** the service doesn't need to run behind a VPN container such as gluetun. The site sees your normal connection, as it does when you watch in a browser.
3. Start it with `docker compose up -d jrwi-archiver` and open `http://<server>:8090`.
4. Choose a password for the web app. You'll go straight to **Settings**.
5. Enter your jrwishow.com email and password, save, then press **Test login**.
   - If it fails, the message gives the site's reason, and **login details** shows what was sent and what came back (never the password).
   - If the site won't take a login from the server at all, use **Log in with your browser instead** on the same page. Export your browser's jrwishow.com cookies with the **Get cookies.txt LOCALLY** extension, then upload the file.
6. Go to **Admin** and press **Check now**.
   - The first check reads the whole members feed and downloads everything, which takes a long time.
   - After that, it checks every 4 hours by default and only fetches what's new.

### Jellyfin

Mount the video folder into Jellyfin, for example `/srv/jrwi/videos:/media/jrwi:ro`, and add it as a **Shows** library.

- **Turn off online metadata for this library:** in the library's settings, untick every metadata downloader and image fetcher. The shows aren't on TheTVDB or TMDB, and a show such as Riptide could otherwise be matched to an unrelated series. The `.nfo` files also carry `<lockdata>`, which tells Jellyfin to keep their metadata.

- **Episodes:** each show gets a folder, such as `Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-286486].mp4`.
- **Specials:** aftershows, extras and unnumbered videos go in `Season 00`.
- **Metadata:** every episode has a `.nfo` with the title, date and post text, plus a thumbnail. Every show has a poster.
- **Show pictures:** a show's picture is its page's own share picture on the site, or the JRWI logo when it has none. To set your own, open **Admin > Shows**, pick the show, and upload a picture or pick one from the show's page, its posts or anywhere in the archive. That override is used on Home, in the API and for the Jellyfin poster, and nothing changes it until you press **Turn off the override**.
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

**Episodes that go up on YouTube later.** Riptide, and other shows at times, post an episode as a members-only video first and on YouTube some weeks later. The site then adds a post with the YouTube video and takes the members-only post down. Enter TubeArchivist's address (such as `http://tubearchivist:8000`) and its API token under **Settings > TubeArchivist**, and the archiver asks TubeArchivist about each such episode at every check. Once TubeArchivist has downloaded the YouTube version, and it's the same length within 10 seconds, the archiver deletes its own copy and the episode plays from TubeArchivist. Saved positions move with any difference in where the two start. Episodes whose lengths differ are kept and listed on the Admin page, where you can delete them anyway or keep them. The archiver only reads from TubeArchivist.

### Files

Every post has a **Files** button that opens its files in place: PDFs page by page, spreadsheets as tables, Word and PowerPoint files as text, plain text and subtitles, the contents of zip files, pictures, audio and video. **Admin > Files** counts the archive's files by type.

A post's pictures, the pictures attached to it and its PDFs open in one picture viewer: tap any of them, then go through all of them with the arrows, the arrow keys or a swipe, PDFs page by page. The Files page has Previous and Next too, and the arrow keys go from file to file.

Google Docs and Drive files a post links to are saved with it, and so are shared Drive folders: every file in the folder and its subfolders (up to 300 files, 5 folders deep), with Google Docs in it saved as PDFs. The Files page says which folder each one is in. A folder that isn't shared with everyone who has the link is listed as a link that couldn't be saved.

### The site's podcast feed

The site gives each member a private podcast feed for podcast apps. Enter its address under **Settings > Podcast feed** and the archiver reads it every 15 minutes between checks. It lists the audio posts only, so it adds no new content, but:

- when a new episode shows up in it, a check of the site starts right away instead of at the next scheduled check (and again every 30 minutes, three times at most, if the site doesn't have the post yet);
- it gives the time of day each episode came out, which the site doesn't show; posts show it next to their date.

The address holds a key that's yours, so it's never shown again once saved, and it isn't logged. The Admin page shows what the feed holds and which of its episodes the archive doesn't have.

### Transcripts

Many episodes link a transcript: a Google Doc, saved as a PDF, with each speaker's name in bold on a line of its own and no times (the early ones have a title and cast list on top and "Name: what they said" in plain text, which is read too; notes in brackets or parentheses aren't looked for in what was heard). The archiver reads it into lines, and a **Transcript** button under the episode's player shows them, with the line being spoken lit up as it plays; clicking a line plays from there. That works on every version's page: the archived video, the audio, and the YouTube version, whose player the page asks where it is (through the messages YouTube's own player API uses, which YouTube doesn't document). The apps get the same lines from the API.

To time the lines, the archiver hears the episode once with a speech recognizer ([faster-whisper](https://github.com/SYSTRAN/faster-whisper), on the processor) and matches the transcript's words to the words it heard. Mishearing some words doesn't matter much, as a line only needs a few of its words found; a line none of whose words were heard goes right after the line before it, or is shown without a time next to a long pause. A transcript that turns out to be another episode's (under 30% of its words heard) isn't timed. It hears the archived audio, or else the archived video; an episode that's only on YouTube (or whose video was handed over) and has no audio version is heard from TubeArchivist's copy, when TubeArchivist is connected in Settings and has it: its sound track is copied out of it for the hearing, and deleted once the episode is timed.

Hearing an episode takes a while and a fair amount of memory: on one 4-thread server, an episode took about 27 minutes with the default model (small.en) and used at most about 1.7 GB. So it's off until you start it or choose when it runs:

1. On the **Admin** page, press **Time one episode now**. It times the newest episode with a transcript, in the background. The first time, the speech model is downloaded to the data folder's `models` folder.
2. **Admin > Transcripts** keeps a record of every episode: how many of the transcript's words were found in what was heard, how many lines got a time (also as a coloured figure before each timed episode, deep purple at 40% and under to yellow at 95% and over), the episode's length, how long hearing took and how fast that is, the memory used, the model and threads, and when. Every try is kept, also when an episode is timed again, and the page can download them all as a CSV file. Each episode shows its post number, the one the PC program takes to try it again. When a new version reads transcripts differently, every transcript is read again on its own and the words already heard are matched to the new lines, without hearing anything again.
3. While it runs, the top of the Admin page shows a bar for the episode being heard, which one of how many it is, and about how long that episode has left. When it's done, the Admin page says how long it took, the most memory it used, and how many lines it placed. Open the episode and try the Transcript button.
4. If that suits your server, either press **Time transcripts now** on the Admin page, which times every episode not timed yet and then stops, or choose when it runs in **Settings > Transcripts > Time them**:
   - **Only when started on the Admin page** (the default).
   - **Between set hours**, such as 01:00 to 07:00 in the server's local time (`TZ` in the compose file). The hours can go past midnight. An episode that's half heard when they end carries on the next day.
   - **As soon as there's something new**: after each check that finds new episodes, until every transcript is timed.

   Episodes are timed one at a time, newest first, at a low priority, using half the processor's threads (a setting).

Since 0.13.0 it runs in a thread of its own, alongside checks, the podcast feed and Re-read, so neither waits for the other; when both want the processor, the check gets it first, as timing runs at a low priority. The two together use more memory than either alone. If a check downloads, moves or hands over the file being heard, that episode is left for the next round, without an error. **Pause transcripts** on the Admin page (in place of Time transcripts now while it runs) stops it until the next check; what's been heard is saved every ten minutes of sound, and it carries on from there.

#### Hearing on your PC

Hearing is the slow part, so a faster computer can do it and send back what it heard. The archiver still matches the words to the transcript, so the lines come out the same way, and every try is in Admin > Transcripts, marked PC. An NVIDIA graphics card hears many times faster than a processor.

1. Install Python 3.9 or newer, then `pip install faster-whisper psutil`. For an NVIDIA card also `pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"`; the program finds those libraries itself. If the card still isn't used on Windows, unpack the NVIDIA libraries from Purfview's whisper-standalone-win into a folder named `nvidia` next to the program, or add `--device cpu`.
2. Download the program from **Admin > Transcripts** (`jrwi_hear.py`) into a folder of its own, and run `python jrwi_hear.py setup` there: the archiver's address and API token (Settings). They're kept in `jrwi_hear.json` next to it; keep that file to yourself. `python jrwi_hear.py check` then says whether it reaches the archiver, how many episodes it can hear, and whether the graphics card works (it hears a second of silence with the tiny model).
3. `python jrwi_hear.py compare 123 456` hears those episodes (the number at the end of an episode's address) once with each of medium.en, large-v3-turbo and large-v3, each in a process of its own, sends them, and prints how they compare: words found, lines placed, time, speed and memory, how far apart they put each line, and how far each is from the timing in use; with two episodes or more, a table for all of them together. `--models small.en,medium.en` takes any faster-whisper models, up to six. Without a number it takes the newest episode.
4. `python jrwi_hear.py run --all --models medium.en` hears every episode with a transcript, timed or not, newest first, sending each as it's done, and skips those that model has heard already, so running it again carries on where it stopped (`--again` hears those too; `--count 5` stops after five). Without `--all` it hears only those not timed yet. `list --all` shows every episode, the timing in use and which models have heard it.

The archiver keeps whichever timing found more of the transcript's words: a new one, or the one in use. Every try is in the record either way. On Windows the program keeps the computer from going to sleep while it runs.

Without the network: **Export for your PC** on an episode's admin page downloads its sound and job in one file; `python jrwi_hear.py file jrwi-job-123.zip` writes `jrwi-job-123-result.json` next to it, which **Import from your PC** on the same page takes.

When an episode times badly, **Debug data** on its admin page downloads what the archiver kept of it in one zip: the transcript PDF, its lines as read and as timed, the words heard for the timing in use with their times, and every try. It holds no settings, tokens or passwords, and not the sound.

On a graphics card it hears in batches of 16 (`--batch`); on a processor, several ten-minute pieces at once (`--workers`, with `--threads` each), which the pieces allow as each is heard on its own. The episode's sound is downloaded to `jrwi_hear_cache` and deleted once sent (`--keep` keeps it).

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

Every post page, comment page, feed card and site page is kept as it was downloaded. When an update changes how pages are read (for example, picking up a detail an older version missed or read wrong), the service re-reads everything it saved on its next start, before the next check. **Admin > Re-read saved pages** does the same by hand.

- **What it corrects:** post text, dates, tags, likes, comments, profile pictures, badges, the Shows menu and site pages.
- **Nothing big is downloaded again.** If the re-read finds files an older version never downloaded, the next check fetches just those posts again.
- **Clean-up:** profile pictures and badges that no comment uses any more are deleted.

When the saved copy itself is wrong or out of date, posts can be **downloaded again** from the site:

- **One post:** open **Admin** at the bottom of the post's page. Choose between its page and comments only, or everything including its video, audio, images and attachments.
- **Every post:** **Admin > Fetch all posts again** fetches every post's page and comments again. Downloaded files are kept.

### Versions of an episode

On the site, an episode's members-only video, its YouTube embed and its "(AUDIO ONLY)" post are separate posts, each with its own description, likes and comments. The archive keeps them separate: the web app shows each post on its own, labelled as the video, audio or YouTube version, with links to the others. The API groups them into one **item** for players such as Tubedroid, and also gives every **post** on its own (see [docs/api.md](docs/api.md)).

- **Where you are is shared:** an episode has one saved place and one watched state, whichever version you play. The web app's video and audio players save it and pick it up again, and so can the apps. An episode counts as watched with 2 minutes left (shorter videos in their last 5%), set under Settings > Apps and notifications; the web app follows it, and so do apps that leave it to the archive. Jellyfin has a rule of its own, under its Dashboard > Playback > Resume.
- **Lining up:** the video and the audio version don't always start at the same moment. At the end of each check, the archiver compares how loud the two are over the first 10 minutes to find the gap, and checks it against a stretch from the middle. When it isn't sure, it assumes there's no gap.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Check every | 4 hours | 0 means only when you press Check now |
| Seconds between page requests | 1.5 | Keeps the load on the site low |
| What to save | everything except YouTube embeds | Video, audio, images, attachments, comments, Google Docs, site pages |
| Video quality | Best available | Lower it to save disk space |
| Re-read comments on posts newer than | 30 days | Picks up comments that arrive after a post goes up |
| Notification URL | none | An ntfy topic URL or a Discord webhook. It gets new posts and problems such as a failed login. |
| Podcast feed | none | The site's private podcast feed, read every 15 minutes (a setting) to start checks for new episodes |
| Mark an episode watched with this many minutes left | 2 | For the web app, and apps that leave it to the archive. Shorter videos are marked in their last 5%. |
| Transcripts | read: on; time: only when started | Read linked transcripts into lines; when to time them with a speech model (off, between set hours, or as soon as there's something new), the model (small.en) and threads (half) |

You can also set these environment variables:

- **`JRWI_EMAIL` and `JRWI_PASSWORD`:** the jrwishow.com login. They take priority over the Settings page.
- **`JRWI_WEB_PASSWORD`:** sets the web app's password on first start.
- **`JRWI_TRUSTED_PROXIES`:** only needed behind a reverse proxy: its address (or a comma-separated list), so the archiver
  takes the visitor's address from it. Without it, the address the connection comes from is used.
- **`TZ`:** the time zone.

## Security

The archive holds your jrwishow.com login, so the web app protects itself:

- Its pages run only the archive's own scripts and can't be framed by other sites. Saved posts and comments are cleaned
  before they're shown: no scripts, event handlers or `javascript:` links.
- Files in the archive that a browser could run as a page (HTML, SVG, XML attachments) open in a sandbox.
- A change sent by a browser from a page on another site (even another service on the same server) is refused. The apps,
  which use the API token, aren't affected.
- After five wrong passwords in 15 minutes, logging in from that address waits until the oldest is 15 minutes old.
- The jrwishow.com password, TubeArchivist's token and the podcast feed's address are never shown again once saved.

It's meant for your own network or Tailscale. Run it as your own user (`user:` in the compose file), not as root.

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
