# JRWI members archiver

This tool copies everything your Just Roll With It membership unlocks on jrwishow.com/supporters into a folder on your computer:

- the text of every post, saved as JSON and as a page you can open in your browser
- images and attached files (PDFs, character sheets, art, music)
- audio episodes
- members-only video
- Google Docs linked from posts, such as episode transcripts (only the ones that are shared publicly)
- links to YouTube videos embedded in posts (add `--embeds` to download the videos too)

It only saves what your tier can see. Posts above your tier still appear in the index, marked "not in your tier".

## One-time setup

1. Install Python 3.9 or newer from https://www.python.org/downloads/. On Windows, tick **"Add python.exe to PATH"** in the installer.
2. Recommended: install ffmpeg. yt-dlp uses it to join video segments into one file.
   - Windows: open Command Prompt and run `winget install Gyan.FFmpeg`
   - Mac: install Homebrew (https://brew.sh), then run `brew install ffmpeg`

   If you skip this, the script uses a bundled copy of ffmpeg and still works.

## Running it

Double-click **`Run on Windows.bat`** or **`Run on Mac.command`**.

The first run takes a minute to set itself up. Then it asks for your jrwishow.com email and password. The password is shown as you type, so you can check it (add `--hide-password` if you'd rather hide it). It isn't saved anywhere.

On a Mac, the first time you open the file, right-click it and choose **Open** (macOS blocks double-clicking scripts downloaded from the internet). Windows may show a SmartScreen warning: click **More info**, then **Run anyway**.

### Do a test run first

Before you download the whole library, archive the 5 newest posts and check the result. Open a terminal in this folder and run:

- Windows: `"Run on Windows.bat" --limit 5`
- Mac: `./"Run on Mac.command" --limit 5`

Then open `JRWI Archive/index.html` in your browser. If the posts, images and media look right, run the launcher again without `--limit`.

### Stopping and resuming

Press **Ctrl+C** to stop at any time. Run the launcher again and it picks up where it left off: finished posts are skipped, and half-downloaded files resume. Run it again weeks later and it only fetches the new posts.

## Disk space

Video takes up nearly all of the space. A 3-hour episode at full quality can be several gigabytes, and the feed has hundreds of episodes, so the full video library can pass a terabyte. Ways to keep it smaller:

- `--max-height 720` downloads video at 720p instead of the best quality
- `--no-video` skips video entirely. Most episodes also have an "AUDIO ONLY" post, which is far smaller.
- `--out "E:\JRWI Archive"` saves to an external drive (use your drive's path)

## Other ways to log in

If the login fails, the script tells you why, using the site's own message when there is one (for example "Invalid Email or password."). It also saves `login-debug.txt` in the archive folder, which describes the login form and the site's answer. Your password is never written to it, so you can send it to me if the reason isn't clear.

It then offers to use your browser's login instead: type `firefox` (or another browser), or the path to a cookies.txt file, and it carries on. You can also choose this from the start with the options below. Log in on jrwishow.com in that browser first.

- `--browser firefox` reads Firefox's login cookies. Also works with `safari`, `chrome`, `edge` and `brave`, except Chrome and Edge on Windows: since Chrome 127 they encrypt cookies so other programs can't read them. On Windows, use Firefox or the next option.
- `--cookies cookies.txt` uses a file exported with the **Get cookies.txt LOCALLY** browser extension (available for Chrome and Firefox). Open jrwishow.com while logged in, click the extension, export, and save the file in this folder. Treat that file like a password and delete it when you're done.

## All options

| Option | What it does |
|---|---|
| `--out FOLDER` | Where to save (default: `JRWI Archive` next to the script) |
| `--email ADDRESS` | Skip the email prompt |
| `--browser NAME` | Reuse your browser's login (see above) |
| `--cookies FILE` | Reuse a cookies.txt export (see above) |
| `--hide-password` | Hide the password while you type it |
| `--limit N` | Only archive N posts this run |
| `--no-video` | Skip members-only video |
| `--no-audio` | Skip audio episodes |
| `--no-images` | Skip images |
| `--no-attachments` | Skip PDFs and other attached files |
| `--no-google-docs` | Don't save linked Google Docs |
| `--embeds` | Also download YouTube videos embedded in posts |
| `--max-height 720` | Cap video quality |
| `--only videos,audio` | Only certain post types: `posts`, `videos`, `audio`, `video_embeds` |
| `--rss URL` | Also download every episode in your private podcast feed |
| `--refresh` | Re-read the whole feed. Use this after you change membership tier. |
| `--retry-failed` | Only retry posts that had errors |
| `--delay 3` | Wait longer between page requests (default 1.5 seconds) |
| `--dump-html` | Save the raw pages too, for troubleshooting |

Options you skipped on one run are picked up later. If you run with `--no-video` today and without it next month, the second run goes back and downloads the videos.

## What you get

```
JRWI Archive/
  index.html                 searchable list of every post; start here
  posts/
    2026-11-07_286486_Through the Fire Riptide #136/
      post.html              the post, readable offline, with the video player
      post.json              title, date, text, links and file list
      Through the Fire Riptide #136.mp4
      images/  attachments/
      card.html              the post's original HTML, for troubleshooting
  google-docs/               transcripts and other linked documents
  perks/                     a copy of your perks page and anything downloadable on it
  problems.txt               anything that couldn't be saved, and why
  archive.log                a log of every run
  _archive_state.json        progress tracker (keep it, or the next run starts over)
```

## If something goes wrong

- **Check `problems.txt`.** It lists every post with an issue. Failed downloads are retried automatically the next time you run the script.
- **"Login failed"**: the message says why. Check `login-debug.txt` in the archive folder, or use your browser's login when the script offers it.
- **"Login problem"**: your login expired part way through. Run it again. If you use `--browser` or `--cookies`, log in on the site in your browser first, and export a fresh cookies file.
- **"Blocked"**: the site answered with a bot check. Open jrwishow.com in your browser, then run with `--browser firefox` and `--delay 5`.
- **A post's attachments or video didn't download**: I built this tool from the public parts of the site, so members-only pages might be laid out a little differently. Send me the `card.html` file from that post's folder and I can adjust the script. That file only contains the one post.

This archive is for your own use. Please don't share the downloaded content.
