# JRWI archiver API (contract v0.19)

This is the same text as the integration contract in the user's "Multimedia Server" project. Everything in it is in the service since 0.10.0.

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
- **Compatibility:** apps treat a missing `sync` as an offset of 0, and a missing part `duration` as unknown. A v0.1 server still works, with positions shared at the same timestamp. A missing `season` means season 1. A missing `tabs` means just `tab`; a missing `image` means `banner`, then `poster`. A missing `seasons` means one season; a missing `places` means only `show` with `tabs`. A missing `handed_over` or `removed_from_site` means no. Before 0.9.0 the files endpoints answer `404`; apps then use the item's `images`, `attachments` and `documents`. A missing `step` or `progress.unit` means apps show `phase` and `done` of `total`, as before. A ping without `features` (before 0.9.7) means none of v0.17's endpoints are there, and `tag` would be ignored. A missing `author.id` means it isn't known. A missing `published_at` or `folder` means `null`.

**General rules**
- **Auth:** every request needs `Authorization: Token <key>`. The token is on the web app's Settings page. Players that can't set headers can add `?token=<key>` to the address instead.
- **Paths:** image, file, audio and video addresses in responses are paths such as `/files/...`, `/api/audio/...` or `/api/video/...`, relative to the server address. They take the same token.
- **Times** are ISO 8601 with a UTC offset. **Durations** are in seconds.
- **Errors** come back as `{"error": "message"}` with a 4xx or 5xx status.
- **Lists** are paged: `?page=1&page_size=50` returns `{"items": [...], "page": 1, "page_size": 50, "total": 123}`. `page_size` goes up to 200.

## Endpoints

| Method and path | What it does |
|---|---|
| `GET /api/ping` | `{"name": "jrwi-archiver", "version": "0.10.0", "contract": "0.19", "features": [...]}` (Ping, below). Needs no token. |
| `GET /api/status` | Archiver state (below) |
| `POST /api/check` | Starts a check. `?full=true` reads the whole feed. `202` when started, `409` if one is running. |
| `POST /api/stop` | (v0.17) Stops the check, or the re-read of saved pages, that's running, as the admin page's Stop does: `202` with `{"stopping": true}`, or `409` when nothing is running. The status shows it running until it has stopped, then `last_result` is `stopped`. |
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
| `PUT /api/progress/{postId}` | Body `{"position": 1234.5, "duration": 11712, "finished": false}`, plus `updated_at` for progress sent later (v0.11). Saves progress for any video or audio post, in that post's own timeline. Returns the same as `GET`: what the archive holds after the save. |
| `GET /api/events?since=<time>` | New items, hand-overs and failures since a time (default: the last 24 hours) |
| `GET /api/problems` | Posts that couldn't be fully saved |
| `GET /api/feed.xml?token=<key>` | Private podcast RSS of all audio |
| `GET /files/<path>` | Images, thumbnails, attachments, documents |

Item lists leave out posts above the user's tier. Add `include_locked=true` to include them; they'll have `"locked": true` and no media.

**Ping (v0.17):** what the archiver has, so apps can ask for only that, once per session:

```json
{"name": "jrwi-archiver", "version": "0.10.0", "contract": "0.19",
 "features": ["post_progress", "tags", "continue", "stop", "feed", "stats"]}
```

- **`contract`:** the version of this contract the archiver follows.
- **`features`:** a name for each optional part it has: `post_progress` (`progress` on posts), `tags` (`GET /api/tags` and `tag`), `continue` (`GET /api/continue`), `stop` (`POST /api/stop`), `feed` (`GET /api/feed`) and `stats` (`GET /api/stats`). Later optional parts get names of their own, and apps ignore names they don't know.

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

- **`checks`:** the last 15 runs, newest first, as the admin page lists them. A check that's running has `result` `running` and `finished` `null`; otherwise `result` is as `last_result` in the status. `new_items` counts the items archived for the first time, `archived` every post saved, new or changed.
- **`files`:** the archive's files by type (as File `type`) and extension, as Admin > Files counts them: how many and how big. YouTube videos and Google links that couldn't be saved are counted with `ext` `YouTube` or `link`, and `bytes` 0.
- **`comments`:** every comment and reply in the archive (`total`), how many of them are replies, how many members commented, and the ten who commented most, with how many comments and the likes on them, in the shape of a Comment's `author`.
  - **Members (v0.18):** told apart by `author.id`, so a member who changed their name counts once. `name`, `avatar`, `badges` and `color` are as on their newest comment, so the name is the one they use now. A comment without an `id` (the user's own) goes with the member last seen with that name, or else counts by name, with `id` `null`. Before v0.18, members were told apart by name.

**Finding an item by YouTube ID (v0.10):** `GET /api/items?youtube_id=CzZ13VPQOcw` lists the items with a post, of any version, that embeds that YouTube video: usually one item, or none. For a YouTube-only episode, that item's `video.youtube_id` is the ID asked for. An item whose members-only video is archived and that also embeds the YouTube version is listed too, with `video` being the archived file; apps that only want YouTube-only items can check `video.youtube_id`. `/api/posts` takes `youtube_id` the same way, listing the posts themselves.

**Progress (v0.3 and v0.9):** the archive keeps one position and one watched state per item (decisions 8 and 9).
- **Any video or audio post:** `/api/progress/{postId}` works for every post with a video (archived or YouTube-only) or audio, including items with only a video.
- **Each post's own timeline:** `position` is in the timeline of the post whose ID is in the address. Send the video position to `video.post_id` and the audio position to `audio.post_id`. The archiver converts between the two with `sync.offset` and keeps the result between 0 and that version's `duration`, so reading either post ID gives the same moment in that version's timeline.
- **The item's `id`** is its video post when it has one, so a position sent there is a video position.
- **Still compatible:** apps that send the audio position to `audio.post_id`, as v0.3 described, work unchanged.
- **`finished`** is shared by both versions. `finished: true` is also how the apps record that the episode was watched through the video, so the podcast feed and the web app show it as played.
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
- **Re-reading saved pages** (`step` `reparse`) runs on its own, not as part of a check: by itself after an update that reads pages differently, before the next check, or from the admin page.

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

**`video` takes one of two shapes, or is `null`:**
- **Archived file:** `{post_id, jellyfin_path, url, height, size, duration}`. `jellyfin_path` is the path as the archiver sees it. Match Jellyfin items on the `[jrwi-<post id>]` tag in the file name, not on the full path.
- **YouTube only:** `{post_id, youtube_id, youtube_url, duration}`. The video exists only as a YouTube embed. If TubeArchivist has that video ID, play it from there.
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
