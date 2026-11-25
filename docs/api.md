# JRWI archiver API (contract v0.29)

This is the same text as the contract part of the integration docs in the user's "Multimedia Server" project, `claude/jrwi-archiver-integration-contract.md`; the index, `claude/jrwi-archiver-integration.md`, lists the other parts. Everything in it is in the service since 0.12.0.

**Changes since v0.1**
- **v0.3:** `sync` on items with both versions (the offset between the video and the audio, and whether it was checked), `duration` on the `video` and `audio` parts, and `/api/progress/{postId}` taking either post ID of an item. When the archiver isn't sure, it reports an offset of 0 rather than no `sync` at all, so positions are always shared. In the service since 0.4.0.
- **v0.4:** comments gain `pinned` and `link`. In the service since 0.2.0.
- **v0.5:** comment authors gain `badges`. In the service since 0.2.4.
- **v0.6:** the status's `progress` gains `current`. In the service since 0.2.5.
- **v0.7:** `/api/posts`, `/api/posts/{postId}` and `/api/posts/{postId}/comments` for single posts. Item `versions` gain `version`, `main`, `date`, `like_count` and `comment_count`. Comments gain `post_id`. In the service since 0.2.6.
- **v0.8:** comment authors gain `color`. In the service since 0.3.4.
- **v0.9:** progress for every post with a video or audio, with `position` in that post's own timeline (see Progress). Progress responses gain `post_id`. In the service since 0.4.0.
- **v0.10:** `youtube_id` on `GET /api/items` finds the items that embed a YouTube video, and items gain `progress` (see Item). In the service since 0.4.1.
- **v0.11:** items gain `season`, and the `episodes` tab is ordered by season, then episode. `PUT /api/progress` takes `updated_at`, for progress sent later (see Progress). A section on downloading audio for offline listening. In the service since 0.5.0.
- **v0.12:** items and posts gain `tabs`, every tab they're in, and `?tab=` lists everything in that tab, so one item can be in two tabs' lists; tab counts count it in each. Shows gain `image`, never empty; `poster` and `banner` are now the same picture, the show's own, or `null`. Shows can come from the site's tags, not only its menu. In the service since 0.6.0.
- **v0.13:** items and posts gain `places`, every show and tab they're in, and a show's lists and counts include the posts its tags place there from other shows. Shows gain `seasons` (with `GET /api/shows/{showId}`), and `GET /api/shows/{showId}/items` takes `?season=`. `season` is now also worked out when titles don't name it. In the service since 0.7.0.
- **v0.14:** items in a show's own Just Rolled With It have a `season` too, and the show's `episodes` and `aftershow` tabs each list their `seasons`, so `?tab=aftershow&season=2` gives a season's Just Rolled With It. In the service since 0.7.1.
- **v0.15:** `GET /api/items/{postId}/files`, `GET /api/posts/{postId}/files` and `GET /api/preview/<path>` (see Files and previews). A YouTube `video` gains `handed_over` when the item's archived video was deleted because TubeArchivist has it; posts gain `handed_over` and `removed_from_site`, and item `versions` gain `removed_from_site`. The event `video_handed_over`. `sync` on YouTube-only items can now be checked. A post whose video was handed over still takes video progress. Offline downloads keep the post's files. In the service since 0.9.0.
- **v0.16:** the status gains `step`, a stable name for what a check is doing, and `progress.unit`, what its count counts. `phase` and `detail` are now worded for people and can change between versions. In the service since 0.9.3.
- **v0.17:** the Tubedroid chat's suggestions 3 to 9. `GET /api/ping` gains `contract` and `features` (see Ping). Posts gain `progress` (see Post). `GET /api/tags`, and `tag` on `GET /api/posts` and `GET /api/items`, whose `q` now also finds the names of a post's tags. `GET /api/feed` (see Feed), `GET /api/continue` (see Continue watching), `POST /api/stop` and `GET /api/stats` (see Stats). In the service since 0.9.7.
- **v0.18:** comment authors gain `id`, the member's ID on the site (see Comment), and `GET /api/stats` counts commenters by it, with `id` on each of the top ten. In the service since 0.9.8.
- **v0.19:** items, posts and item `versions` gain `published_at`, the time of day they came out, from the site's private podcast feed (see Item). Files in a shared Google Drive folder a post links to are saved, and files and `documents` gain `folder`, where in that folder each is (see File). In the service since 0.10.0.
- **v0.20:** the Tubedroid chat's suggestions 10 and 11. `POST /api/reparse` starts a re-read of saved pages, and `GET /api/podcast` and `POST /api/podcast/read` give the podcast feed's state and read it now (see Podcast feed). Feature names `reparse` and `podcast`. In the service since 0.10.1.
- **v0.21:** a change (`POST`, `PUT`, `PATCH`, `DELETE`) that a browser sends from a page on another site, as its `Origin` or `Referer` says, is refused with `403` (see General rules). Apps that send neither header, as Tubedroid's do, aren't affected. A reply's `date_text` is no longer the site's text twice over ("2d 2d"), and its `date` is worked out from it. In the service since 0.10.3.
- **v0.22:** when to mark an episode watched is a setting in the archiver (asked for by the user on 2026-11-22): a progress save may leave out `finished`, and the archiver then marks the episode watched itself once the save's position is close enough to the end of that version, by the setting (see Progress). The ping gains `watched_left`, the setting in seconds, and lists `watched`. Saves with `finished` work as before. In the service since 0.11.0.
- **v0.23:** timed transcripts (the Tubedroid chat's suggestion 12): `GET /api/transcript/{postId}` gives the episode's transcript as lines, each with its speaker and, once the archiver has timed it, when it's said, in that post's timeline (see Transcripts). Items and posts gain `transcript`, and the ping lists `transcript`. The status gains `background`, for timing transcripts between checks. In the service since 0.12.0.
- **v0.24:** hearing on another computer (asked for by the user on 2026-11-23): `GET /api/transcripts/jobs`, `GET /api/transcripts/jobs/{postId}` and `POST /api/transcripts/jobs/{postId}` hand an episode's sound out to be heard elsewhere and take back the words heard (see Hearing on another computer). Feature name `transcript_jobs`. In the service since 0.14.0.
- **v0.25:** `GET /api/transcripts/jobs?all=true` hands out every episode whose transcript has been read and that has sound, timed or not (asked for by the user on 2026-11-23, to time them all again with a larger model), with `left_out` for those it can't and why; jobs gain `timed_with`, `found`, `source` and `heard_with`. A result now replaces the timing in use only when it found at least as many of the transcript's words, and the answer gains `replaced`, `before` and each try's `compare_in_use`. In the service since 0.14.1.
- **v0.26:** an episode with no archived audio or video whose video is on YouTube (only on YouTube, or handed over) is heard from TubeArchivist's copy, on the server and on another computer: its job's `sound` has `source` `tubearchivist` and the address `GET /api/transcripts/sound/{postId}`, the sound track copied out of that copy (asked about by the user on 2026-11-23, as no Wonderlust episode could be heard). A job's `sound` gains `source`, and `left_out` says when TubeArchivist isn't connected or doesn't have the video. In the service since 0.14.2.
- **v0.27:** the transcript's answer gains `timeline`, which timeline its times are in: `audio`, `video` (the item's video, archived or on YouTube) or `youtube`. The YouTube post of an episode whose archived video is kept (not handed over) gets its lines in its own timeline, `youtube`, once the archiver has lined the two videos up through TubeArchivist's copy (the Tubedroid chat's suggestion 13, 2026-11-23); until then, and when they can't be lined up, in the archived video's, `video`, as before. Lines heard from an archived video that has since been handed over are moved by the gap the hand-over measured, until the episode is heard again from the YouTube video. In the service since 0.14.8. Since 0.14.9 a YouTube `video` gains `size`, the size of TubeArchivist's copy, and its `duration` comes from TubeArchivist when the site gives none (it never seems to), once TubeArchivist is connected and has the video; both are asked at each check.
- **v0.28:** for computers hearing episodes (the Winchivist chat's asks, 2026-11-24). A job can be claimed before it's heard (`POST /api/transcripts/jobs/{postId}/claim`, for 30 minutes unless renewed), so the server's own timing and other computers leave it alone; jobs gain `claimed`, who is hearing them and until when, the server's own hearing included. `GET /api/transcripts/program` gives the PC program with the API token, and the ping gives its version (`pc_program`), as does each job (`program`). A job's `archiver` is the archiver's version again (from 0.14.5 to 0.14.9 it was `2`, the version of how transcripts are read). Feature names `transcript_claims` and `transcript_program`. The PC program (1.4) claims each episode it hears, prints its progress as JSON lines with `--json`, and replaces itself with the archiver's copy with `update`. In the service since 0.14.10.
- **v0.29:** the transcript's answer gains `shift`: how many seconds later the episode's lines are shown, set by hand on its admin page for an episode whose lines run a little early or late (0 when not set); the lines come already moved. Also since 0.14.11, a line that begins after a pause starts where its first word likely begins, as the speech model often starts such a word early (it's moved half a second at most), and words not heard at the start of a line count at the episode's own pace of speech; every transcript is matched again by itself to its words already heard. In the service since 0.14.11.
- **Compatibility:** apps treat a missing `sync` as an offset of 0, and a missing part `duration` as unknown. A v0.1 server still works, with positions shared at the same timestamp. A missing `season` means season 1. A missing `tabs` means just `tab`; a missing `image` means `banner`, then `poster`. A missing `seasons` means one season; a missing `places` means only `show` with `tabs`. A missing `handed_over` or `removed_from_site` means no. Before 0.9.0 the files endpoints answer `404`; apps then use the item's `images`, `attachments` and `documents`. A missing `step` or `progress.unit` means apps show `phase` and `done` of `total`, as before. A ping without `features` (before 0.9.7) means none of v0.17's endpoints are there, and `tag` would be ignored. A missing `author.id` means it isn't known. A missing `published_at` or `folder` means `null`. A ping without `reparse` or `podcast` (before 0.10.1) means those endpoints aren't there. A ping without `watched` (before 0.11.0) means a save without `finished` counts as `finished: false`, so apps keep their own rule there. A missing `transcript` means no transcript; a missing `background` means nothing runs in the background. A ping without `transcript_claims` (before 0.14.10) means jobs can't be claimed and have no `claimed`; without `transcript_program`, the PC program is only on Admin > Transcripts, behind the web app's password. A missing `shift` means 0.

**General rules**
- **Auth:** every request needs `Authorization: Token <key>`. The token is on the web app's Settings page. Players that can't set headers can add `?token=<key>` to the address instead.
- **Paths:** image, file, audio and video addresses in responses are paths such as `/files/...`, `/api/audio/...` or `/api/video/...`, relative to the server address. They take the same token.
- **Times** are ISO 8601 with a UTC offset. **Durations** are in seconds.
- **Errors** come back as `{"error": "message"}` with a 4xx or 5xx status.
- **Changes from other sites (v0.21):** a `POST`, `PUT`, `PATCH` or `DELETE` whose `Origin` (or, without one, `Referer`) is another address than the archiver's own, or `null`, gets `403`. Other services on the same server count as other sites. Apps don't send these headers, so they're not affected; a web page or WebView calling the API from its own address would be.
- **Lists** are paged: `?page=1&page_size=50` returns `{"items": [...], "page": 1, "page_size": 50, "total": 123}`. `page_size` goes up to 200.

## Endpoints

| Method and path | What it does |
|---|---|
| `GET /api/ping` | `{"name": "jrwi-archiver", "version": "0.14.11", "contract": "0.29", "features": [...]}` (Ping, below). Needs no token. |
| `GET /api/status` | Archiver state (below) |
| `POST /api/check` | Starts a check. `?full=true` reads the whole feed. `202` when started, `409` if one is running. |
| `POST /api/reparse` | (v0.20) Re-reads the saved pages with the current parsers, as the admin page's Re-read saved pages does: `202` with `{"started": true}`, or `409` when a check or a re-read is running. It runs as its own job, with `step` `reparse` in the status, and `POST /api/stop` stops it. |
| `POST /api/stop` | (v0.17) Stops the check, or the re-read of saved pages, that's running, as the admin page's Stop does: `202` with `{"stopping": true}`, or `409` when nothing is running. The status shows it running until it has stopped, then `last_result` is `stopped`. |
| `GET /api/podcast` | (v0.20) The site's podcast feed as the admin page's panel shows it, never with its address (Podcast feed, below) |
| `POST /api/podcast/read` | (v0.20) Reads the podcast feed now, as the panel's Read it now does: `202` with `{"reading": true}`, or `409` when no feed is set up. It reads in the background; `GET /api/podcast` shows the result a moment later. |
| `GET /api/stats` | (v0.17) The archive's own numbers: its last checks, its files by type, and its comments (Stats, below) |
| `GET /api/shows` | Shows that have items, in the site's menu order, then shows from the site's tags or added on the admin page, `other` last. `?include_empty=true` lists all of them. |
| `GET /api/shows/{showId}` | One show, with `tabs` and `seasons` |
| `GET /api/shows/{showId}/items?tab=episodes&season=2&order=desc` | Paged items in one tab: every item the show's tags place in it (v0.12, v0.13). `season` keeps one season of the show's own episodes (v0.13) or Just Rolled With It (v0.14). In `episodes`, items are ordered by season, then episode number, then upload date. Since 0.9.5 an item without a number sits after the numbered episode of its season that came out last before it. |
| `GET /api/items?q=&show=&tab=&kind=&tag=&youtube_id=&order=desc` | Paged search across everything. `q` looks in the title, the text and, since v0.17, the names of the tags. `kind` is `video`, `audio` or `post`. `tag` (v0.17) keeps one tag (Tags, below). `youtube_id` (v0.10) gives the items that embed that YouTube video (below). `show` includes items placed in that show from another. |
| `GET /api/latest?limit=20` | Newest items, for a home screen |
| `GET /api/continue?limit=12` | (v0.17) Episodes started and not finished, most recently played first (Continue watching, below). `limit` up to 50. |
| `GET /api/feed?q=&tag=&order=desc` | (v0.17) Paged posts as the web app's Home shows them, whole, with their first comments and progress (Feed, below). `page_size` 20 by default, up to 50. |
| `GET /api/tags` | (v0.17) `[{"name": "RIPTIDE", "count": 312}, ...]`: the site's tags on posts in the user's tier, by name (Tags, below) |
| `GET /api/items/{postId}` | Full item. Either post ID of a video/audio pair works. |
| `GET /api/items/{postId}/comments?page=` | Threaded comments from every version of the item, paged by top-level comment. Each says which post it's on (`post_id`). |
| `GET /api/items/{postId}/files` | (v0.15) `{"item_id": 300, "files": [...]}`: every file of all the item's versions (File, below) |
| `GET /api/posts?q=&show=&tab=&tag=&order=desc` | (v0.7) Paged single posts, every version listed on its own. Same filters as items. Since v0.17 each has its `progress`. |
| `GET /api/posts/{postId}` | (v0.7) One post on its own: only its own text, likes, files, media and, since v0.17, progress |
| `GET /api/posts/{postId}/comments?page=` | (v0.7) Only that post's comments |
| `GET /api/posts/{postId}/files` | (v0.15) `{"post_id": 299, "files": [...]}`: only that post's files |
| `GET /api/preview/<path>` | (v0.15) What's inside a file under `/files/<path>` (Preview, below). `?page=N` gives page N of a PDF as a PNG. |
| `GET /api/audio/{postId}` | The audio file, with HTTP range support |
| `GET /api/video/{postId}` | The video file, with HTTP range support. This is a fallback; Jellyfin is the main way to play video. |
| `GET /api/progress/{postId}` | `{"post_id": 300, "position": 1234.5, "duration": 11712, "finished": false, "updated_at": "..."}`, in the timeline of the post in the address (see Progress) |
| `PUT /api/progress/{postId}` | Body `{"position": 1234.5, "duration": 11712, "finished": false}`, plus `updated_at` for progress sent later (v0.11). Since v0.22 `finished` can be left out, for the archiver to decide (see Progress). Saves progress for any video or audio post, in that post's own timeline. Returns the same as `GET`: what the archive holds after the save. |
| `GET /api/transcript/{postId}` | (v0.23) The episode's transcript as lines, timed in this post's timeline once the archiver has timed it (Transcripts, below). `404` when it has none. |
| `GET /api/transcripts/jobs?all=` | (v0.24) Episodes whose transcript isn't timed yet, as jobs for another computer to hear (Hearing on another computer, below); since v0.25 `all=true` gives every episode, timed or not |
| `GET /api/transcripts/jobs/{postId}` | (v0.24) One episode's job, timed or not |
| `GET /api/transcripts/sound/{postId}` | (v0.26) The sound of an episode heard from TubeArchivist's copy, copied out of it (Hearing on another computer, below) |
| `POST /api/transcripts/jobs/{postId}` | (v0.24) The words another computer heard, one result per model: matched, recorded, the best kept |
| `POST /api/transcripts/jobs/{postId}/claim` | (v0.28) Claims an episode before hearing it, or renews a claim; `409` when someone else is hearing it (Claims, below) |
| `DELETE /api/transcripts/jobs/{postId}/claim?claim=` | (v0.28) Lets go of a claim |
| `GET /api/transcripts/program` | (v0.28) The PC program, `jrwi_hear.py`, as Admin > Transcripts hands it out (The PC program, below) |
| `GET /api/events?since=<time>` | New items, hand-overs and failures since a time (default: the last 24 hours) |
| `GET /api/problems` | Posts that couldn't be fully saved |
| `GET /api/feed.xml?token=<key>` | Private podcast RSS of all audio |
| `GET /files/<path>` | Images, thumbnails, attachments, documents |

Item lists leave out posts above the user's tier. Add `include_locked=true` to include them; they'll have `"locked": true` and no media.

**Ping (v0.17):** what the archiver has, so apps can ask for only that, once per session:

```json
{"name": "jrwi-archiver", "version": "0.14.11", "contract": "0.29",
 "features": ["post_progress", "tags", "continue", "stop", "feed", "stats", "reparse", "podcast", "watched",
              "transcript", "transcript_jobs", "transcript_claims", "transcript_program"],
 "watched_left": 120.0, "pc_program": "1.4"}
```

- **`contract`:** the version of this contract the archiver follows.
- **`features`:** a name for each optional part it has: `post_progress` (`progress` on posts), `tags` (`GET /api/tags` and `tag`), `continue` (`GET /api/continue`), `stop` (`POST /api/stop`), `feed` (`GET /api/feed`), `stats` (`GET /api/stats`), since v0.20 `reparse` (`POST /api/reparse`) and `podcast` (`GET /api/podcast` and `POST /api/podcast/read`), since v0.22 `watched` (the archiver marks episodes watched, below), since v0.23 `transcript` (`GET /api/transcript`), since v0.24 `transcript_jobs` (hearing on another computer), and since v0.28 `transcript_claims` (claims on jobs) and `transcript_program` (`GET /api/transcripts/program`). Later optional parts get names of their own, and apps ignore names they don't know.
- **`watched_left` (v0.22):** how many seconds before the end of a version an episode counts as watched, from the archiver's Settings (2 minutes unless changed; 0 means at the very end). See Progress.
- **`pc_program` (v0.28):** the version of the PC program this archiver hands out, such as `"1.4"`.

**Tags (v0.17):**
- **`GET /api/tags`:** the site's tags that archived posts in the user's tier carry, with how many such posts carry each, sorted by name whatever the capitals, as the web feed's tag picker lists them.
- **`tag`** on `GET /api/posts`, `GET /api/items` and `GET /api/feed`: one tag by its name, capitals not minding (`?tag=riptide` is `RIPTIDE`). Posts carry it themselves; items by their main post's tags, so an item is found by the tags of its `id` post. A tag no post carries gives no posts.
- **`q`** on the same three also finds posts by the names of their tags, as the web feed's search does, and can go with `tag`: `?tag=RIPTIDE&q=Blue` gives only Riptide posts with "Blue" somewhere.

**Continue watching (v0.17):** `GET /api/continue?limit=12` gives the web app's Continue watching row, in its order and by its rules: episodes 10 seconds or more in, not finished and not at their end, the one played most recently first, each item once, locked items left out.

```json
{"items": [{"item": { /* Item (in lists) */ },
            "progress": {"post_id": 288, "position": 1234.5, "duration": 11695, "finished": false,
                         "updated_at": "2026-11-21T00:12:00+00:00"}}]}
```

- **`item`:** the item as in lists, with its own `progress` in its main post's timeline.
- **`progress`:** in the timeline of the version played last; its `post_id` says which version that was. The web app's card opens that version, and resumes there.

**Feed (v0.17):** `GET /api/feed` pages through the archive's posts as the web app's Home does: every post on its own (as `/api/posts`), newest first, or oldest first with `order=asc`, with `q`, `tag` and `include_locked` as on `/api/posts`. Each entry is the post as `GET /api/posts/{postId}` gives it (with `progress`), plus:
- **`top_comments`:** its first two comments as the web feed shows them, pinned ones first, then the most liked, as Comment objects with their replies.
- **`more_comments`:** `true` when it has more than those two; `/api/posts/{postId}/comments` gives them all.

Locked posts are left out unless `include_locked=true`. A page holds up to 50 posts.

**Stats (v0.17):** `GET /api/stats` gives what apps can't work out from posts and items:

```json
{"checks": [{"started": "2026-11-21T20:00:00+00:00", "finished": "2026-11-21T20:04:12+00:00",
             "result": "ok", "message": null, "new_items": 3, "archived": 5}],
 "files": [{"type": "pdf", "ext": ".pdf", "count": 312, "bytes": 1234567890}],
 "comments": {"total": 18234, "replies": 4012, "commenters": 812,
              "top": [{"id": "1234567", "name": "jazzyferin", "avatar": "/files/avatars/b4a56f4a9840a6bb.webp",
                       "badges": [], "color": "rgb(247, 221, 210)", "count": 120, "likes": 3400}]}}
```

- **`checks`:** the last 15 runs, newest first, as the admin page lists them. A check that's running has `result` `running` and `finished` `null`; otherwise `result` is as `last_result` in the status. A check the archiver couldn't finish because it stopped while the check ran (an update, a restart or a crash) has `result` `stopped`, with a `message` saying so, and `finished` when the archiver shut down or, if it was killed, when it started again. Since 0.10.6; before, such a check stayed `running` with `finished` `null` for good. `new_items` counts the items archived for the first time, `archived` every post saved, new or changed.
- **`files`:** the archive's files by type (as File `type`) and extension, as Admin > Files counts them: how many and how big. YouTube videos and Google links that couldn't be saved are counted with `ext` `YouTube` or `link`, and `bytes` 0.
- **`comments`:** every comment and reply in the archive (`total`), how many of them are replies, how many members commented, and the ten who commented most, with how many comments and the likes on them, in the shape of a Comment's `author`.
  - **Members (v0.18):** told apart by `author.id`, so a member who changed their name counts once. `name`, `avatar`, `badges` and `color` are as on their newest comment, so the name is the one they use now. A comment without an `id` (the user's own) goes with the member last seen with that name, or else counts by name, with `id` `null`. Before v0.18, members were told apart by name.

**Podcast feed (v0.20):** `GET /api/podcast` gives the state of the site's private podcast feed (see "The site's podcast feed"), as the admin page's Podcast feed panel shows it. The feed's address is never given, nor anything that could hold its key.

```json
{"set_up": true, "last_read": "2026-11-22T13:45:00+00:00", "ok": true, "error": null, "title": "JRWI Members",
 "count": 412, "matched": 398, "with_time": 398, "times_used": 398, "gone": 0, "every_minutes": 15,
 "last_trigger": {"at": "2026-11-22T11:02:00+00:00", "titles": ["Through the Fire | Riptide #136 (AUDIO ONLY)"]},
 "latest": [{"title": "Through the Fire | Riptide #136 (AUDIO ONLY)", "published": "2026-11-22T11:00:00+00:00",
             "post_id": 298, "duration": 11695}],
 "unmatched": [{"title": "Bonus: Tavern Talk", "published": "2026-11-01T16:00:00+00:00"}],
 "fields": [{"name": "enclosure", "count": 412}, {"name": "itunes:duration", "count": 412}]}
```

- **`set_up`:** whether the feed's address is in the archiver's Settings. When it's `false`, nothing else is given: `{"set_up": false}`.
- **`last_read`, `ok`, `error` and `title`:** when it was last read, whether that worked, the error if not (naming only the host), and the feed's title. All `null` before the first read.
- **`count`, `matched` and `with_time`:** the episodes in the feed, those that are posts in the archive, and those that say their time of day. `gone` counts episodes the feed listed once and no longer does; `times_used` the posts in the archive that have a `published_at` from it.
- **`every_minutes`:** how often it's read between checks, or `null` when reading it between checks is off (the setting is 0).
- **`last_trigger`:** the last check a new episode in it started, with the episodes' titles, or `null`.
- **`latest`:** its newest episodes, up to 10, newest first: `post_id` when it's matched to a post in the archive, else `null`, and `duration` in seconds when the feed gives one.
- **`unmatched`:** up to 20 episodes that aren't posts in the archive, newest first.
- **`fields`:** the fields its episodes carry, such as `itunes:duration`, with how many carry each, most common first, so it shows what the real feed holds.
- **`POST /api/podcast/read`:** reads the feed at once, even when it said nothing changed last time, and starts a check when a new episode in it isn't in the archive, as Read it now does. It answers `202` straight away and reads in the background, so `GET /api/podcast` a few seconds later shows the result.

**Transcripts (v0.23):** many episodes link a transcript, a Google Doc saved as a PDF, with each speaker's name on a line of its own and no times. The archiver reads it into lines, and times them by hearing the episode once with a speech recognizer and matching the transcript's words to what it heard (see "Timed transcripts").

```json
{"post_id": 298, "aligned": true, "timeline": "audio", "shift": 0.0, "document": "/files/google-docs/Episode%20transcription%20here.%20%5B1RkmhusZ%5D.pdf",
 "lines": [{"start": 12.4, "end": 41.9, "speaker": "Zach", "text": "There's something wrong with this town and no one seems to notice. ..."},
           {"start": 42.1, "end": 42.6, "speaker": "Charlie", "text": "What?"},
           {"start": 43.0, "end": 43.0, "speaker": null, "text": "[laughter]"},
           {"start": null, "end": null, "speaker": "Condi", "text": "Okay."}]}
```

- **Which transcript:** a PDF, linked or attached, of any post of the item whose name says "transcript" or "transcription", the audio post's first. Any post of the item gives the same lines.
- **`lines`:** in the transcript's order, one per speaker's turn, as written, notes in brackets included. `speaker` is as the transcript writes it, player or character ("Lui", "Troy (Slimecicle)"), or `null` for a line that's a note of its own ("[laughter]") or a group's ("[all the players] Whoa!"). A turn of several paragraphs has them in `text` with a line break between.
- **`start` and `end`:** seconds in the timeline of the post in the address, as Progress is: the audio's as heard, and the video's moved by `sync.offset` (or the other way round when it was the video's sound that was heard). The line being said at a moment is the last one whose `start` has passed. A note gets the end of the line before it. A line none of whose words were heard sits right after the line before it, or, next to a gap longer than 10 seconds, where it can't be told which side it's on, gets `null` times; it's still shown, untimed.
- **`timeline` (v0.27):** which timeline `start` and `end` are in: `audio`, `video` (the item's video: the archived one, or the YouTube one when there's no archived video or it was handed over), or `youtube`, the YouTube post's own when the item's archived video is kept and the two have been lined up. A YouTube post whose gap to the archived video isn't known (yet) gets `video`: the archived video's timeline, which the YouTube upload can start a few seconds earlier or later than. A missing `timeline` (before v0.27) means the post's own, as the contract said.
- **`shift` (v0.29):** seconds the episode's lines are shown later than timed (earlier when below 0), set by hand on its admin page, from -30 to 30; `start` and `end` already include it. 0 for most episodes.
- **`aligned`:** `false` until the episode is timed, while timing is off in the archiver's Settings, or when the transcript turned out not to be this episode's (under 30% of its words heard); all times are `null` then, and the text can still be shown.
- **`document`:** the PDF it was read from.
- **`transcript`** on items, posts and feed entries: `true` when this endpoint has lines for the item, so a player can show a transcript button without asking.
- **Size:** a three-hour episode's lines are about 250 KB of JSON, and much less compressed. Apps keep them with an offline download as they keep the post's other files.

**Hearing on another computer (v0.24):** hearing an episode is the slow part of timing its transcript, so another computer can do it and send back the words it heard; the archiver matches them to the transcript as it matches its own, so the lines come out the same way. The archiver's own PC program (`jrwi_hear.py`, downloaded from Admin > Transcripts) uses this; apps don't need to.

```json
{"item_id": 300, "title": "Through the Fire | Riptide #136", "lines": 3629, "timed": true,
 "timed_with": "small.en", "found": 0.794, "source": "server", "heard_with": ["small.en"],
 "sound": {"post_id": 298, "timeline": "audio", "size": 187654321, "duration": 11695.0, "url": "/api/audio/298",
           "name": "Through the Fire.m4a", "source": "archive"},
 "hotwords": null, "archiver": "0.14.11", "program": "1.4", "claimed": null,
 "hearing": {"chunk": 600.0, "overlap": 10.0, "language": "en", "beam_size": 1, "word_timestamps": true,
             "vad_filter": true, "condition_on_previous_text": false}}
```

- **A job:** `GET /api/transcripts/jobs` lists the episodes whose transcript isn't timed yet, newest first (`limit`, 20 by default), as `{"jobs": [...], "left_out": [], "episodes": 142}`. With `all=true` (v0.25) it lists every episode whose transcript has been read and that has archived audio or video, timed or not (no limit unless one is given), and `left_out` lists the episodes with a transcript it can't hand out, each with `item_id`, `title` and `why` (no sound, or the transcript not read yet); `episodes` counts the episodes with a transcript. And `GET /api/transcripts/jobs/{postId}` gives one episode's, timed or not (any of its posts' IDs), or `404` when it has no read transcript or no archived audio or video. `sound` is what to hear, from its `url` with the token: the audio, or else the archived video, or else (v0.26) the sound of TubeArchivist's copy of the episode's YouTube video, for an episode only on YouTube or handed over, when TubeArchivist is connected and has it. That one has `source` `tubearchivist` (else `archive`), `timeline` `video` (the YouTube post's), and `url` `/api/transcripts/sound/{postId}`: the archiver copies the sound track out of TubeArchivist's file without converting it (Matroska), which takes a minute or two for a long episode before the download starts, and drops the copy once the episode is timed. Its `size` is TubeArchivist's file's, which says whether it's the same video, and the download's `Content-Length` the sound's. `hearing` says how the archiver hears (ten-minute pieces with ten seconds heard twice where they meet, keeping a piece's words from the middle of the overlap on), and `hotwords` words for the model to listen for, which since 0.14.7 is always `null`: the speakers' names given as hotwords made the models write the names out in a loop in place of what was said, losing up to 79% of an episode's speech. A program should hear with no hotwords when it's `null`.
- **What has heard it (v0.25):** `timed_with` is the model of the timing in use, `found` the share of the transcript's words it found and `source` where it was heard (`server` or `pc`), or all three `null` when it isn't timed from this sound. `heard_with` lists every model that has heard this sound and been matched, here or on another computer, so a program going through every episode can skip what a model has done. Since 0.14.7 only hearings made without hotwords count, so every episode heard before is heard once more.
- **The result:** `POST /api/transcripts/jobs/{postId}` with `{"item_id": 300, "sound_size": 187654321, "results": [{"model": "medium.en", "words": [[1.0, 1.3, "There"], ...], "until": 11695.0, "took": 410.2, "load": 3.1, "peak_mb": 2900, "device": "cuda", "batch": 16, "host": "...", "program": "1.0"}]}`: one result per model, up to six, each word as `[start, end, word]` in seconds of the sound. `sound_size` has to be the job's, or the answer is `409` (the sound changed since). The archiver matches each result, records every one (Admin > Transcripts, marked PC), keeps the one that found the most of the transcript's words (since v0.25 only when it found at least as many as the timing in use; otherwise that stays), and with two or more compares each with the first: of the lines both placed, how far apart their starts are (`median`, and `p90`, nine in ten within), the share `within_1s`, and how many lines each placed that the other didn't. It answers `{"item_id": 300, "used": "medium.en", "replaced": true, "before": {"model": "small.en", "source": "server", "found": 0.794, "at": "..."}, "tries": [{"model", "found", "placed", "unplaced", "took", "speed", "peak_mb", "error", "compare", "compare_in_use", "used"}, ...]}`: `used` is the model whose timing is in use afterwards, `replaced` whether it's a new one, `before` the timing that was in use (or `null`), and each try's `compare_in_use` how far its lines are from that timing's, as `compare` (v0.25), or `400` with an error for a result it can't use.
- **Claims (v0.28):** a computer about to hear an episode claims it, so the server's own timing and other computers don't hear it at the same time. `POST /api/transcripts/jobs/{postId}/claim` with `{"by": "LAPTOP-1", "minutes": 30}` (both can be left out: `by` names the computer for the admin page, `minutes` is 30 unless given, at most 240) answers `{"item_id": 300, "claim": "k3J9...", "by": "LAPTOP-1", "until": "2026-11-24T14:05:00+00:00"}`. Sending the same with `"claim": "k3J9..."` renews it from now, keeping the same ID; a claim that isn't renewed runs out at `until`. The answer is `409` with `{"error": "...", "claimed": {"by": "DESKTOP", "until": "..."}}` when another claim that hasn't run out has the episode, or `{"by": "server", "until": null}` while the archiver hears it itself; and `404` when the episode can't be heard. The words coming in (`POST /api/transcripts/jobs/{postId}`, from anyone) end every claim on the episode; `DELETE /api/transcripts/jobs/{postId}/claim?claim=k3J9...` lets go of one before that (`{"released": true}`, or `false` when it wasn't there). Every job, in the lists and on its own, has `claimed`: `{"by", "until"}` while someone is hearing it, else `null`; the claim's ID is only in the claim's own answer. The archiver's own timing skips claimed episodes, apart from one the user asks for on the admin page; Admin > Transcripts lists them under "Being heard now". A result is taken whoever claimed the episode, as before.
- **The PC program (v0.28):** `GET /api/transcripts/program` gives `jrwi_hear.py` as this archiver has it, with the token like any other request, so a program that runs it can fetch a new copy when the ping's `pc_program` (or a job's `program`) differs from its copy's. The answer has `X-Program-Version`, `ETag` and `Last-Modified`. The program's own `update` command uses it.
- **The program's output (program 1.4):** with `--json`, before or after the command, every line it prints is one JSON object, `{"event": "...", ...}`, and nothing else is printed on standard output; its other wording (without `--json`) can change between versions. Events, in the order they come: `start` (`program`, `command`); `message` (`text`: what it would have printed, for showing as it is); `newer_program` (`version`, `this`: the archiver has a newer program); `episode` (`item_id`, `title`, `duration`, `lines`, `n` and `of` when it knows how many, `models`); `claimed` (`item_id`, `by`, `until`: someone else is hearing it, so it's skipped); `claim` (`item_id`, `claim`, `until`: this computer has claimed it, so a program that stops this one by force can let go of it); `copying` (`item_id`: the archiver is copying TubeArchivist's sound first); `download` (`item_id`, `done` and `total` in bytes, at most twice a second); `downloaded` (`item_id`, `size`); `loading` (`item_id`, `model`); `hearing` (`item_id`, `model`, `done` and `total` pieces, `spent` and `left` in seconds); `heard` (`item_id`, `model`, `length`, `took`, `speed`, `words`); `sending` (`item_id`); `sent` (`item_id`, `used`, `replaced`, `before`, `tries`: the archiver's answer, each try with `model`, `found`, `placed`, `unplaced`, `took`, `speed`, `peak_mb`, `error`, `used`); `skipped` (`item_id`, `why`: the archiver couldn't take the words); `done` (`heard`, `skipped`, and for `run` `kept`, `took`); `updated` (`version`, `changed`); `error` (`message`), always the last line when it stops on an error. Each model is heard in a process of its own, whose events come on the same output. Programs ignore events and fields they don't know. Exit codes: 0 when done, 1 on an error, 2 for a command line it doesn't understand, and 3 when `compare` heard nothing because every episode it was given is being heard elsewhere. The archiver's address and token can come from `JRWI_SERVER` and `JRWI_TOKEN` in its environment; `JRWI_CLAIM` hands it a claim the program that starts it holds already, which it renews while hearing and leaves to its owner to let go of.
- **Feature names** `transcript_jobs` in the ping, and since v0.28 `transcript_claims` and `transcript_program`.

**Finding an item by YouTube ID (v0.10):** `GET /api/items?youtube_id=CzZ13VPQOcw` lists the items with a post, of any version, that embeds that YouTube video: usually one item, or none. For a YouTube-only episode, that item's `video.youtube_id` is the ID asked for. An item whose members-only video is archived and that also embeds the YouTube version is listed too, with `video` being the archived file; apps that only want YouTube-only items can check `video.youtube_id`. `/api/posts` takes `youtube_id` the same way, listing the posts themselves.

**Progress (v0.3 and v0.9):** the archive keeps one position and one watched state per item (decisions 8 and 9).
- **Any video or audio post:** `/api/progress/{postId}` works for every post with a video (archived or YouTube-only) or audio, including items with only a video.
- **Each post's own timeline:** `position` is in the timeline of the post whose ID is in the address. Send the video position to `video.post_id` and the audio position to `audio.post_id`. The archiver converts between the two with `sync.offset` and keeps the result between 0 and that version's `duration`, so reading either post ID gives the same moment in that version's timeline.
- **The item's `id`** is its video post when it has one, so a position sent there is a video position.
- **Still compatible:** apps that send the audio position to `audio.post_id`, as v0.3 described, work unchanged.
- **`finished`** is shared by both versions. `finished: true` is also how the apps record that the episode was watched through the video, so the podcast feed and the web app show it as played.
- **Watched near the end (v0.22):** a save that leaves out `finished` (or sends `null`) leaves it to the archiver: the episode is watched when the time left in that version, its `duration` less `position` (the save's `duration`, or the version's own), is at most `watched_left` seconds, or at most 5% of its length when that's shorter, so a ten-minute extra is marked in its last 30 seconds; and at most a second when `watched_left` is 0. Further back it's not watched, as with `finished: false`, so playing a watched episode again from earlier unmarks it, as it does now. Without a length to go by, the save keeps what the archive holds. The response's `finished` says what the archiver decided, so apps show that and pass it on (to Jellyfin, say), rather than marking it at a point of their own. `finished: true` or `false` in a save still decides it outright, for a watched button and for apps written before v0.22. Offline, apps can work it out the same way from the ping's `watched_left`, or leave `finished` out of the save sent later. The web app leaves it out since 0.11.0, and sends `true` when playback reaches the end.
- **Older saves:** progress saved before 0.4.0 (audio only, under the audio post) is read in the audio's timeline and moved to the item's record on the next save.
- **Responses** include `post_id`, the post whose timeline `position` is in.
- **Progress sent later (v0.11):** progress made offline, or held back for any reason, is sent with `updated_at`: when it was made, with a UTC offset. The archiver keeps whichever of the two is newer, so a late save can't undo listening or watching done since on another device or in the web app; when the archive's is newer, nothing changes and the response shows what it holds. A time later than the server's clock counts as now. Without `updated_at`, a save counts as made when it arrives, as before. A malformed time, or one without an offset, gets `400`.
- **Handed-over videos (v0.15):** after a hand-over, `video.post_id` is the YouTube post, and positions are in the YouTube video's timeline (the archiver moved the saved ones). The members-only post's ID, which apps may still hold, keeps working for video positions too, in the YouTube video's timeline.

**Downloading for offline listening (v0.11):** what an app needs to keep an episode's audio on the device (decision 11).
- **The file:** `GET /api/audio/{audio.post_id}` (the item's `audio.url`), with `?token=` or the header. Downloads can resume: the server takes `Range` requests and answers `206`.
- **Checking it:** a whole download's `Content-Length` matches `audio.size`, and the response has `ETag`, `Last-Modified` and `Content-Type` (`audio.mime`). A different `audio.size` or `ETag` later means the archive downloaded the episode again, and the copy on the device can be replaced.
- **Its name:** `Content-Disposition` gives the archived file's name, made from the post's title; apps can just as well name the file by post ID.
- **Picture and text:** the item's `thumbnail`, `images`, `attachments` and `documents` are plain files under `/files/` that take the token the same way, and the item and comments JSON can be kept as they are.
- **The post's files (v0.15, decision 17):** an offline download keeps the files too. `GET /api/items/{postId}/files` (or the shown version's `/api/posts/{postId}/files`) lists them; keep every entry with `saved: true` whose `role` isn't `video` or `audio` (those download on their own) by its `url`, and its Preview JSON from `/api/preview/<path>` for spreadsheets, documents, text and zips, so the inspector works offline. PDFs can be drawn on the device (Android's `PdfRenderer`) or kept as their page PNGs.
- **Progress:** saved on the device while offline, then sent with `updated_at` (above) once the archiver can be reached.

**Files and previews (v0.15, decision 16):** for a file inspector.
- **Lists:** `GET /api/posts/{postId}/files` gives one post's files, in the order the web app lists them: video, audio, documents, attachments, pictures, thumbnail, then Google links that couldn't be saved. `GET /api/items/{postId}/files` gives every version's, the main post's first, each file once.
- **Opening a file:** `url` is where it is, with the token like any address. `type` says how to show it: `image`, `audio` and `video` are shown or played from `url`; `pdf`, `table`, `document`, `text` and `archive` have a `preview` address; `youtube` plays through TubeArchivist; `link` was never saved and only has `source`; `other` can only be downloaded.
- **Drive folders (v0.19):** a shared Google Drive folder a post links to is saved whole: every file in it and in its folders (up to 300 files, 5 folders deep), Google Docs in it as PDFs (Sheets as `.xlsx`), each listed as a `document` with `folder`, such as `"Campaign Maps/Old maps"`. A folder that isn't shared with everyone who has the link stays a `link` entry, and so do Google Forms in a folder, which can't be files.

## Objects

### Status

```json
{"running": true, "step": "posts", "phase": "Saving new and changed posts",
 "detail": "Through the Fire | Riptide #136 · downloading the video, 42% of ~4.1 GB",
 "progress": {"done": 2, "total": 12, "current": 0.42, "unit": "post"},
 "last_check_started": "2026-11-20T11:19:59+00:00", "last_check_finished": "2026-11-20T11:20:00+00:00",
 "next_check": "2026-11-20T15:29:00+00:00", "last_result": "ok", "message": null, "login_ok": true,
 "counts": {"items": 2104, "archived": 2001, "locked": 87, "problems": 3},
 "disk": {"archive_bytes": 912345678901, "free_bytes": 2345678901234}}
```

- **`last_result`:** one of `ok`, `login_failed`, `blocked`, `error` or `stopped`.
- **`next_check`:** `null` when checks only run by hand.
- **`step` (v0.16):** what the check is doing, as a stable name for apps to act on: `starting`, `login`, `feed` (reading the members feed), `pages` (the site's own pages), `posts` (saving new and changed posts), `comments` (new comments on recent posts), `tubearchivist` (asking TubeArchivist about episodes now on YouTube), `line_up` (lining up video and audio), `finishing` (sorting posts and updating the Jellyfin folder), `reparse` (re-reading saved pages, its own job) or `idle`.
- **`phase` and `detail`:** the same in words, for showing as they are; the wording can change between versions. `phase` says what the check is doing, such as "Saving new and changed posts" or "Reading the whole members feed" on a full check. `detail` says which post or page, and what part of it: "Page 3: 60 posts read, 2 new", "Through the Fire | Riptide #136 · reading the post", "· downloading the video, 42% of ~4.1 GB", "· saving its comments". Since v0.16; before, they were shorter, such as "archiving posts".
- **`progress` (`current` since v0.6, `unit` since v0.16):** `done` of `total` in the current step. `unit` says what's counted: `post`, `episode` or `page`, or `null`. The one being worked on is number `done + 1`, so the web app shows "post 3 of 12". `current` is how far the file downloading now has got, from 0 to 1, or `null`. A bar can show `(done + current) / total`. When `total` is 0 the step has no known length yet, such as while reading the feed.
- **Re-reading saved pages** (`step` `reparse`) runs on its own, not as part of a check: by itself after an update that reads pages differently, before the next check, from the admin page, or since v0.20 with `POST /api/reparse`.
- **`background` (v0.23):** work the archiver does in the background, now timing transcripts: `{"step": "transcripts", "phase": "Timing transcripts", "detail": "Paid In Blood | Riptide #134 · hearing, 42%", "progress": {...}}`, or `null`. It isn't a check: it doesn't set `running`, and Check now and `POST /api/check` work as ever. Since service 0.13.0 it carries on while a check runs, so `background` and `running: true` can come together; before, a check took over from it, and it carried on afterwards from where it was. `POST /api/stop` doesn't stop it; the admin page's Pause transcripts does, until the next check. When it runs is set in the archiver's Settings (by hand only, between set hours, or as soon as there's something new); the status only reports it.

### Show

```json
{"id": "prime-defenders", "name": "Prime Defenders", "description": "...",
 "poster": null, "banner": null, "image": "/files/site/assets/0d4c1b2a3e4f5a6b.webp",
 "item_count": 157, "latest": "2023-10-21T00:00:00+00:00",
 "tabs": [{"id": "episodes", "name": "Episodes", "count": 64,
           "seasons": [{"season": 1, "name": null, "count": 24}, {"season": 2, "name": null, "count": 40}]},
          {"id": "aftershow", "name": "Just Rolled With It", "count": 56,
           "seasons": [{"season": 1, "name": null, "count": 22}, {"season": 2, "name": null, "count": 34}]},
          {"id": "extras", "name": "Extras", "count": 52}],
 "seasons": [{"season": 1, "name": null, "count": 24}, {"season": 2, "name": null, "count": 40}]}
```

- **`image` (v0.12):** the picture to show for the show: its own picture, or the JRWI logo when it has none. Never `null` once the site's logo has been saved.
- **`poster` and `banner`:** since v0.12 both are the show's own picture (see Show pictures), or `null` when it has none. Before v0.12 they could differ, and a show without either showed its newest thumbnail.
- **`tabs`:** only included in `GET /api/shows/{showId}`. A post in two tabs counts in both (v0.12), and posts from other shows that the site's tags place here count too (v0.13).
- **`seasons` on a tab (v0.14):** the `episodes` and `aftershow` tabs list the seasons of the show's own posts in them, with how many items each has and the season's `name` (`null` when its episodes don't share one). With more than one, apps can show a page per season (`?tab=aftershow&season=2`), as the web app does in place of one page for the tab, titled such as "Season 2: Empty Graves" and "Just Rolled With It: Season 2".
- **`seasons` on the show (v0.13):** only included in `GET /api/shows/{showId}`: the same list as the `episodes` tab's, kept for apps written for v0.13. One entry for a show with one season, none for a show without Episodes.
- **Tab ids**, in this order: `episodes`, `aftershow`, `extras`, `art`, `posts`. Tabs with nothing in them are left out.

### Item (in lists)

```json
{"id": 300, "title": "Through the Fire | Riptide #136", "date": "2026-11-07T00:00:00+00:00",
 "published_at": "2026-11-07T17:30:00+00:00",
 "show": "riptide", "tab": "episodes", "tabs": ["episodes"], "places": [{"show": "riptide", "tab": "episodes"}],
 "kind": "episode", "season": 1, "episode": 136, "pinned": true,
 "thumbnail": "/files/posts/300/thumbnail.webp", "duration": 11695,
 "video": {"post_id": 300, "jellyfin_path": "/media/jrwi/Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].mp4",
           "url": "/api/video/300", "height": 1080, "size": 4123456789, "duration": 11712},
 "audio": {"post_id": 298, "url": "/api/audio/298", "mime": "audio/mp4", "size": 187654321, "duration": 11695},
 "sync": {"offset": -17.2, "checked": true},
 "progress": {"post_id": 300, "position": 1234.5, "duration": 11712, "finished": false,
              "updated_at": "2026-11-21T00:12:00+00:00"},
 "comment_count": 12, "like_count": 469, "locked": false, "archived": true}
```

**Fields**
- **`id`:** the main post of the item. That's the video post when there is one, otherwise the audio post.
- **`show`:** the item's own show (see "In several shows").
- **`tabs` (v0.12):** every tab the item is in within its own show, in the tab order, from the main post's tags (see Tabs). `tab` is the main one of them, the one Jellyfin files it under.
- **`places` (v0.13):** every show and tab the item is in: its own show's `tabs` first, then other shows its tags name, such as `{"show": "prime-defenders", "tab": "extras"}` for UPP's first season.
- **`kind`:** one of `episode`, `video`, `audio`, `youtube`, `art` or `post`. `episode` is a video or audio whose main tab is `episodes`.
- **`season` (v0.11):** the season of an episode in its own show's `episodes` tab: named in the title or a tag, or since v0.13 worked out (see "Episode numbers and seasons"). Since v0.14 also the season of a post in its own show's `aftershow` tab. `null` for other posts, episodes without a number, and Just Rolled With It of a show without episodes. Seasons can each start again at episode 1, so apps show both, as the web app does with "S2 #1" for seasons after the first.
- **`date`:** the site only shows a date, so the time is midnight UTC.
- **`published_at` (v0.19):** when it came out, to the second, from the site's private podcast feed once it's set up in the archiver's Settings, or `null`. The feed only lists audio posts, so an item has its audio version's time when its main post has none of its own. `date` stays the site's date, and lists stay in the site's order; apps can show the time of day next to the date.
- **`transcript` (v0.23):** whether `GET /api/transcript/{id}` has lines for the item (see Transcripts).

**`video` takes one of two shapes, or is `null`:**
- **Archived file:** `{post_id, jellyfin_path, url, height, size, duration}`. `jellyfin_path` is the path as the archiver sees it. Match Jellyfin items on the `[jrwi-<post id>]` tag in the file name, not on the full path.
- **YouTube only:** `{post_id, youtube_id, youtube_url, duration, size}`. `duration` is the length the site shows, or else (since 0.14.9) TubeArchivist's; `size` (v0.27, since 0.14.9) is the size of TubeArchivist's copy, or missing when TubeArchivist isn't connected or doesn't have it. The video exists only as a YouTube embed. If TubeArchivist has that video ID, play it from there.
- **Handed over (v0.15):** the YouTube shape plus `"handed_over": {"at": "2026-11-28T10:02:11+00:00", "post_id": 286486}`, when the item had an archived video that was deleted because TubeArchivist has the same video (decision 15). `post_id` there is the members-only post the file belonged to. The Jellyfin episode is gone, so play it through TubeArchivist; a copy downloaded to the device through Jellyfin stays there until it's removed.

**`audio`** is `null` when there's no archived audio version.

**`duration` on `video` and `audio` (v0.3):** that version's own length in seconds, or `null` when unknown. Archived files are measured when they're downloaded; for YouTube-only videos it's the length the site shows, if it shows one. The item's `duration` stays as it was.

**`sync` (v0.3):** `{"offset": <seconds>, "checked": <true or false>}` on every item with both a video (archived or YouTube-only) and an audio version.
- **Meaning:** `audio position = video position + offset`. In the example, the video has 17.2 seconds at the start that the audio doesn't, so 1:00:00 in the video is 59:42.8 in the audio.
- **`checked`:** `true` when the archiver compared the two versions and found the offset. `false` when it couldn't or wasn't sure, in which case the offset is 0. Since v0.15 YouTube-only videos can be checked too, through TubeArchivist's copy.
- **`null`** only when the item has just one version, and on single posts (`/api/posts`).
- **Apps** keep a carried-over position between 0 and the other version's `duration`.

**`progress` (v0.10):** the same object `GET /api/progress/{id}` returns for the item's `id`, so `position` is in the main post's timeline (the video's when there is one), or `null` when the item was never played. On items in `/api/shows/{showId}/items`, `/api/items` and `/api/latest`, and on the full item. Since v0.17 single posts have their own (see Post).

### Item (full)

The list fields, plus:

```json
{"body_html": "<p>...</p>", "body_text": "...", "original_url": "https://jrwishow.com/supporters/videos/...",
 "tags": ["RIPTIDE"], "access": "tiers",
 "images": [{"url": "/files/posts/290/images/01.webp"}],
 "attachments": [{"name": "Zombie Character Sheets.pdf", "url": "/files/posts/299/attachments/Zombie%20Character%20Sheets.pdf", "size": 1234567}],
 "links": [{"text": "Episode transcription here.", "url": "https://docs.google.com/..."}],
 "documents": [{"name": "Riptide 135 - Blue Jay [TRANSCRIPT] [1B_ZJmqV].pdf", "url": "/files/google-docs/...", "folder": null}],
 "embeds": [{"kind": "youtube", "url": "https://www.youtube.com/watch?v=CzZ13VPQOcw"}],
 "versions": [{"post_id": 300, "version": "video", "main": true, "title": "Through the Fire | Riptide #136",
               "url": "https://...", "date": "2026-11-07T00:00:00+00:00", "published_at": null,
               "like_count": 469, "comment_count": 12,
               "removed_from_site": null},
              {"post_id": 298, "version": "audio", "main": false, "title": "Through the Fire | Riptide #136 (AUDIO ONLY)",
               "url": "https://...", "date": "2026-11-07T00:00:00+00:00", "published_at": "2026-11-07T17:30:00+00:00",
               "like_count": 10, "comment_count": 1,
               "removed_from_site": null}],
 "problems": []}
```

- **`body_html`:** cleaned of scripts, with images pointing at the archived copies.
- **`attachments`, `documents` and `images`:** collected from every version of the item.
- **`documents`:** linked Google Docs, saved as PDFs, and since 0.5.0 files shared from Google Drive, under the name Drive gives them. Since v0.19 also the files in a linked Drive folder, with `folder` saying where in it each is; `null` for files linked on their own.
- **`versions` (fields beyond `post_id`, `title` and `url` since v0.7):** every post of the item, main post first, each with its own counts. `version` is `video` (members-only video), `audio`, `youtube`, or `null` for a post without either. `removed_from_site` (v0.15): when the archiver noticed the site no longer has that post, or `null`. `published_at` (v0.19): that post's own time from the podcast feed, or `null`.

### Post (v0.7)

A single post, never merged with its other versions. It has the same fields as an item, list or full, but only from this one post: its own `body_html`, `like_count`, `comment_count`, `tags`, `tabs`, `places`, files, and `video` or `audio` only if this post has them. Plus:

```json
{"id": 298, "version": "audio", "item_id": 300, "paired": true,
 "removed_from_site": null, "handed_over": null,
 "progress": {"post_id": 298, "position": 1217.3, "duration": 11695, "finished": false,
              "updated_at": "2026-11-21T00:12:00+00:00"}}
```

- **`version`:** which version of its episode this post is: `video`, `audio`, `youtube`, or `null`.
- **`item_id`:** the item it's grouped into. The same as `id` for a main post or a post on its own.
- **`paired`:** `true` when the item has other versions.
- **`removed_from_site` (v0.15):** when the archiver noticed that the site's feed no longer shows the post, such as a members-only episode taken down once its YouTube version is up, or `null`. The archive keeps the post.
- **`handed_over` (v0.15):** for a members-only post whose video was deleted because TubeArchivist has it: `{"at": "...", "youtube_id": "...", "youtube_url": "...", "post_id": 286882}`, where `post_id` is the post with the YouTube version. Its `video` is then `null`. Otherwise `null`.
- **`progress` (v0.17):** on a post with a video or audio, and on a handed-over one: the same object `GET /api/progress/{postId}` gives for the post's own `id`, so `position` is in this post's timeline, or `null` when the episode was never played. The archive keeps one record per episode (decisions 8 and 9), so every version shows the same moment, each in its own timeline. `null` on posts without either. In `/api/posts`, `/api/posts/{postId}` and `/api/feed`.
- **`transcript` (v0.23):** as on items: whether the item this post is in has a transcript.

### File (v0.15)

```json
{"name": "Zombie Character Sheets.pdf", "role": "attachment", "type": "pdf", "mime": "application/pdf",
 "size": 1234567, "url": "/files/posts/299/attachments/Zombie%20Character%20Sheets.pdf",
 "path": "posts/299/attachments/Zombie Character Sheets.pdf", "post_id": 299, "source": null, "saved": true,
 "preview": "/api/preview/posts/299/attachments/Zombie%20Character%20Sheets.pdf", "folder": null}
```

- **`role`:** where the file comes from: `video`, `audio`, `document` (a linked Google Doc or Drive file), `attachment`, `image` (a picture in the post), `thumbnail`, or `link` (a Google link that couldn't be saved: not shared with everyone who has the link, say).
- **`type`:** how to show it: `image`, `audio`, `video`, `pdf`, `table`, `document`, `text`, `archive`, `youtube`, `link` or `other`. Worked out from the name, and for a file without a usable name (Drive files saved as `.bin`) from what's in it.
- **`url`:** the file's address, or `null` when it isn't saved (`youtube`, `link`). For the video and audio it's `/api/video/{postId}` and `/api/audio/{postId}`.
- **`path`:** the file's path under `/files/`, for `/api/preview/<path>`, or `null` for the video and for what isn't saved.
- **`source`:** the address it was saved from: the Google Docs or Drive link for a document, the site's address for a picture, the YouTube address for `youtube`, the link for `link`. Otherwise `null`.
- **`folder` (v0.19):** for a file from a shared Drive folder the post links to, where it is in that folder, starting with the folder's own name (`"Campaign Maps/Old maps"`); otherwise `null`.
- **`saved`:** `false` for `youtube` and `link` entries, which aren't files in the archive.
- **`preview`:** the Preview address, for `pdf`, `table`, `document`, `text` and `archive`; `null` for the rest.
- **Extra fields:** `duration` on the video and audio, `youtube_id` on `youtube` entries.

### Preview (v0.15)

`GET /api/preview/<path>` always gives `name`, `type`, `mime`, `size` and `url`, plus by `type`:

```json
{"type": "pdf", "pages": 12, "page_url": "/api/preview/google-docs/Riptide%20135.pdf?page={n}"}
{"type": "table", "sheets": [{"name": "Party", "rows": [["Name", "Class"], ["Ferin", "Rogue"]], "truncated": false}]}
{"type": "document", "blocks": [{"kind": "heading", "level": 1, "text": "Session notes"},
                                {"kind": "paragraph", "text": "..."}, {"kind": "item", "text": "..."},
                                {"kind": "row", "cells": ["...", "..."]}]}
{"type": "text", "text": "...", "format": "subtitles", "truncated": false}
{"type": "archive", "entries": [{"name": "maps/harbour.png", "size": 1234, "folder": false}], "truncated": false}
```

- **`pdf`:** `page_url` with `{n}` replaced by a page number (from 1) gives that page as a PNG 1400 pixels wide. `page_url` is `null` when the pages can't be counted; show the PDF itself then.
- **`table`:** every sheet, cells as text, up to 500 rows and 60 columns; `truncated` when there's more.
- **`document`:** `.docx` and `.pptx`. Each slide of a `.pptx` starts with a heading "Slide N".
- **`text`:** up to 400,000 characters; `format` is `plain`, `markdown`, `subtitles`, `json` or `html` (shown as text, never as a page).
- **`note`:** on a `table`, `document` or `archive` that can't be read, or is too big to show; the file can still be downloaded.
- Other types give only the common fields.

### Comment

```json
{"id": "286486:1016036", "post_id": 286486,
 "author": {"id": "1234567", "name": "jazzyferin", "avatar": "/files/avatars/b4a56f4a9840a6bb.webp",
            "badges": [{"name": "4-month member", "icon": "🔥", "image": null},
                       {"name": "JRWiener", "icon": null, "image": "/files/badges/0c1d2e3f4a5b6c7d.webp"}],
            "color": "rgb(247, 221, 210)"},
 "date": "2026-11-07T00:00:00+00:00", "date_text": "13d",
 "body_html": "<p>for anyone not reading the description: ...</p>", "body_text": "for anyone not reading ...",
 "like_count": 327, "pinned": true, "link": "https://jrwishow.com/supporters/videos/...?cid=comment_...",
 "replies": [ /* comments */ ]}
```

- **Dates:** the site only shows how long ago a comment was posted ("13d"), so `date` is counted back from when that page was downloaded. The site shows older comments more roughly ("5mo"), so when a later download is rougher, the earlier, more exact `date` is kept. `date_text` is what the site showed for the `date` that's kept.
- **`pinned` (v0.4):** pinned comments come first, as on the site.
- **`link` (v0.4):** the comment's own address on the site, or `null`.
- **`author.id` (v0.18):** the member's ID on the site, as text: the same on all their comments and replies, on every post, even after they change their name, so apps can tell members apart and group a member's comments. It's read from the comment's Report form, so it's `null` on the user's own comments, which the site doesn't let them report. Comments saved before 0.9.8 get it when the saved pages are re-read, which 0.9.8 does by itself after the update, before its first check.
- **`author.avatar`:** `null` for members without a profile picture. The site shows their initial instead, and apps can do the same.
- **`author.badges` (v0.5):** the badges next to the name, in the site's order: the membership tier (an image, with the tier name as `name`), months as a member (an emoji `icon` and text) and `Verified` on verified accounts. Any of `name`, `icon` and `image` can be `null`. An empty list when there are none.
- **`author.color` (v0.8):** the CSS colour the site shows the name in, which follows the member's tier, such as `rgb(247, 221, 210)`, or `null`.
- **Fields that can be missing:** `date` and `like_count` are `null` when the site didn't show them.
- **Which comments:** every comment and reply, fetched from the site's comments endpoint with paging. For an item, they come from every version, starting with the main post's, newest first; `/api/posts/{postId}/comments` gives one post's only.
- **`post_id` (v0.7):** the post the comment was made on, so apps can show which version it belongs to.

### Event

```json
{"type": "new_item", "at": "2026-11-20T11:19:30+00:00", "post_id": 300, "title": "Through the Fire | Riptide #136", "message": ""}
```

- **`type`:** one of `new_item`, `video_handed_over` (v0.15), `check_failed` or `login_failed`.
- **`new_item`:** sent once per item, when it has finished archiving.
- **`video_handed_over` (v0.15):** an item's archived video was deleted because TubeArchivist has it (decision 15). `post_id` is the item's `id`; `message` names the YouTube ID. Apps holding that item can load it again.
