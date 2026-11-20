# JRWI archiver API (contract v0.7)

This is the same text as the integration contract in the user's "Multimedia Server" project. Items marked v0.3 (`sync`, part durations, progress by either post ID) aren't built yet.

**Changes since v0.1** (v0.3 items are not in the service yet)
- **`sync`** on items with both versions: the offset between the video and the audio, and whether it was checked.
- **`duration`** on the `video` and `audio` parts.
- **`/api/progress/{postId}`** takes either post ID of an item.
- **v0.3:** when the archiver isn't sure, it reports an offset of 0 rather than no `sync` at all, so positions are always shared.
- **v0.4:** comments gain `pinned` and `link`. In the service since 0.2.0.
- **v0.5:** comment authors gain `badges`. In the service since 0.2.4.
- **v0.6:** the status's `progress` gains `current`. In the service since 0.2.5.
- **v0.7:** `/api/posts`, `/api/posts/{postId}` and `/api/posts/{postId}/comments` for single posts. Item `versions` gain `version`, `main`, `date`, `like_count` and `comment_count`. Comments gain `post_id`. In the service since 0.2.6.
- **Compatibility:** apps treat a missing `sync` as an offset of 0, and a missing part `duration` as unknown. A v0.1 server still works, with positions shared at the same timestamp.

**General rules**
- **Auth:** every request needs `Authorization: Token <key>`. The token is on the web app's Settings page. Players that can't set headers can add `?token=<key>` to the address instead.
- **Paths:** image, file, audio and video addresses in responses are paths such as `/files/...`, `/api/audio/...` or `/api/video/...`, relative to the server address. They take the same token.
- **Times** are ISO 8601 with a UTC offset. **Durations** are in seconds.
- **Errors** come back as `{"error": "message"}` with a 4xx or 5xx status.
- **Lists** are paged: `?page=1&page_size=50` returns `{"items": [...], "page": 1, "page_size": 50, "total": 123}`. `page_size` goes up to 200.

## Endpoints

| Method and path | What it does |
|---|---|
| `GET /api/ping` | `{"name": "jrwi-archiver", "version": "0.2.1"}`. Needs no token. |
| `GET /api/status` | Archiver state (below) |
| `POST /api/check` | Starts a check. `?full=true` reads the whole feed. `202` when started, `409` if one is running. |
| `GET /api/shows` | Shows that have items, in the site's menu order, `other` last. `?include_empty=true` lists all of them. |
| `GET /api/shows/{showId}` | One show, with `tabs` |
| `GET /api/shows/{showId}/items?tab=episodes&order=desc` | Paged items in one tab. In `episodes`, items are ordered by episode number. |
| `GET /api/items?q=&show=&tab=&kind=&order=desc` | Paged search across everything. `kind` is `video`, `audio` or `post`. |
| `GET /api/latest?limit=20` | Newest items, for a home screen |
| `GET /api/items/{postId}` | Full item. Either post ID of a video/audio pair works. |
| `GET /api/items/{postId}/comments?page=` | Threaded comments from every version of the item, paged by top-level comment. Each says which post it's on (`post_id`). |
| `GET /api/posts?q=&show=&tab=&order=desc` | (v0.7) Paged single posts, every version listed on its own. Same filters as items. |
| `GET /api/posts/{postId}` | (v0.7) One post on its own: only its own text, likes, files and media |
| `GET /api/posts/{postId}/comments?page=` | (v0.7) Only that post's comments |
| `GET /api/audio/{postId}` | The audio file, with HTTP range support |
| `GET /api/video/{postId}` | The video file, with HTTP range support. This is a fallback; Jellyfin is the main way to play video. |
| `GET /api/progress/{postId}` | `{"position": 1234.5, "duration": 11695, "finished": false, "updated_at": "..."}` |
| `PUT /api/progress/{postId}` | Body `{"position": 1234.5, "duration": 11695, "finished": false}`. Saves audio progress. |
| `GET /api/events?since=<time>` | New items and failures since a time (default: the last 24 hours) |
| `GET /api/problems` | Posts that couldn't be fully saved |
| `GET /api/feed.xml?token=<key>` | Private podcast RSS of all audio |
| `GET /files/<path>` | Images, thumbnails, attachments, documents |

Item lists leave out posts above the user's tier. Add `include_locked=true` to include them; they'll have `"locked": true` and no media.

**Progress (v0.3):** progress is kept once per item. Either post ID of the item works in `/api/progress/{postId}`, so the apps can use the item's `id` or `audio.post_id`. `position` is always an audio position. `finished: true` is also how the apps record that the episode was watched through the video, so the podcast feed and the web app show it as played.

## Objects

### Status

```json
{"running": true, "phase": "archiving posts", "detail": "Through the Fire | Riptide #136: 42%",
 "progress": {"done": 12, "total": 30, "current": 0.42},
 "last_check_started": "2026-11-20T11:19:59+00:00", "last_check_finished": "2026-11-20T11:20:00+00:00",
 "next_check": "2026-11-20T15:29:00+00:00", "last_result": "ok", "message": null, "login_ok": true,
 "counts": {"items": 2104, "archived": 2001, "locked": 87, "problems": 3},
 "disk": {"archive_bytes": 912345678901, "free_bytes": 2345678901234}}
```

- **`last_result`:** one of `ok`, `login_failed`, `blocked`, `error` or `stopped`.
- **`next_check`:** `null` when checks only run by hand.
- **`progress` (`current` since v0.6):** `done` of `total` steps in the current phase, for example posts. `current` is how far the file downloading now has got, from 0 to 1, or `null`. A bar can show `(done + current) / total`. When `total` is 0 the phase has no known length yet, such as while reading the feed.
- **`phase`** can also be `re-reading saved pages`. The service does that by itself after an update that reads pages differently, before its next check.

### Show

```json
{"id": "riptide", "name": "Riptide", "description": "...",
 "poster": "/files/shows/riptide/f9f40bf840928626.webp", "banner": "/files/pages/assets/1f08bbd78676f42d.webp",
 "item_count": 412, "latest": "2026-11-07T00:00:00+00:00",
 "tabs": [{"id": "episodes", "name": "Episodes", "count": 136},
          {"id": "aftershow", "name": "Just Rolled With It", "count": 130}]}
```

- **`tabs`:** only included in `GET /api/shows/{showId}`.
- **Tab ids**, in this order: `episodes`, `aftershow`, `extras`, `art`, `posts`. Tabs with nothing in them are left out.

### Item (in lists)

```json
{"id": 300, "title": "Through the Fire | Riptide #136", "date": "2026-11-07T00:00:00+00:00",
 "show": "riptide", "tab": "episodes", "kind": "episode", "episode": 136, "pinned": true,
 "thumbnail": "/files/posts/300/thumbnail.webp", "duration": 11695,
 "video": {"post_id": 300, "jellyfin_path": "/media/jrwi/Riptide/Season 01/Riptide - S01E136 - Through the Fire [jrwi-300].mp4",
           "url": "/api/video/300", "height": 1080, "size": 4123456789, "duration": 11712},
 "audio": {"post_id": 298, "url": "/api/audio/298", "mime": "audio/mp4", "size": 187654321, "duration": 11695},
 "sync": {"offset": -17.2, "checked": true},
 "comment_count": 12, "like_count": 469, "locked": false, "archived": true}
```

**Fields**
- **`id`:** the main post of the item. That's the video post when there is one, otherwise the audio post.
- **`kind`:** one of `episode`, `video`, `audio`, `youtube`, `art` or `post`.
- **`date`:** the site only shows a date, so the time is midnight UTC.

**`video` takes one of two shapes, or is `null`:**
- **Archived file:** `{post_id, jellyfin_path, url, height, size, duration}`. `jellyfin_path` is the path as the archiver sees it. Match Jellyfin items on the `[jrwi-<post id>]` tag in the file name, not on the full path.
- **YouTube only:** `{post_id, youtube_id, youtube_url}`. The video exists only as a YouTube embed. If TubeArchivist has that video ID, play it from there.

**`audio`** is `null` when there's no archived audio version.

**`duration` on `video` and `audio` (v0.3):** that version's own length in seconds, or `null` when unknown. The item's `duration` stays as it was.

**`sync` (v0.3):** `{"offset": <seconds>, "checked": <true or false>}` on every item with both a video (archived or YouTube-only) and an audio version.
- **Meaning:** `audio position = video position + offset`. In the example, the video has 17.2 seconds at the start that the audio doesn't, so 1:00:00 in the video is 59:42.8 in the audio.
- **`checked`:** `true` when the archiver compared the two versions and found the offset. `false` when it couldn't or wasn't sure, in which case the offset is 0.
- **`null`** only when the item has just one version.
- **Apps** keep a carried-over position between 0 and the other version's `duration`.

### Item (full)

The list fields, plus:

```json
{"body_html": "<p>...</p>", "body_text": "...", "original_url": "https://jrwishow.com/supporters/videos/...",
 "tags": ["RIPTIDE"], "access": "tiers",
 "images": [{"url": "/files/posts/290/images/01.webp"}],
 "attachments": [{"name": "Zombie Character Sheets.pdf", "url": "/files/posts/299/attachments/Zombie%20Character%20Sheets.pdf", "size": 1234567}],
 "links": [{"text": "Episode transcription here.", "url": "https://docs.google.com/..."}],
 "documents": [{"name": "Riptide 135 - Blue Jay [TRANSCRIPT] [1B_ZJmqV].pdf", "url": "/files/google-docs/..."}],
 "embeds": [{"kind": "youtube", "url": "https://www.youtube.com/watch?v=CzZ13VPQOcw"}],
 "versions": [{"post_id": 300, "version": "video", "main": true, "title": "Through the Fire | Riptide #136",
               "url": "https://...", "date": "2026-11-07T00:00:00+00:00", "like_count": 469, "comment_count": 12},
              {"post_id": 298, "version": "audio", "main": false, "title": "Through the Fire | Riptide #136 (AUDIO ONLY)",
               "url": "https://...", "date": "2026-11-07T00:00:00+00:00", "like_count": 10, "comment_count": 1}],
 "problems": []}
```

- **`body_html`:** cleaned of scripts, with images pointing at the archived copies.
- **`attachments`, `documents` and `images`:** collected from every version of the item.
- **`versions` (fields beyond `post_id`, `title` and `url` since v0.7):** every post of the item, main post first, each with its own counts. `version` is `video` (members-only video), `audio`, `youtube`, or `null` for a post without either.

### Post (v0.7)

A single post, never merged with its other versions. It has the same fields as an item, list or full, but only from this one post: its own `body_html`, `like_count`, `comment_count`, `tags`, files, and `video` or `audio` only if this post has them. Plus:

```json
{"id": 298, "version": "audio", "item_id": 300, "paired": true}
```

- **`version`:** which version of its episode this post is: `video`, `audio`, `youtube`, or `null`.
- **`item_id`:** the item it's grouped into. The same as `id` for a main post or a post on its own.
- **`paired`:** `true` when the item has other versions.

### Comment

```json
{"id": "286486:1016036", "post_id": 286486,
 "author": {"name": "jazzyferin", "avatar": "/files/avatars/b4a56f4a9840a6bb.webp",
            "badges": [{"name": "4-month member", "icon": "🔥", "image": null},
                       {"name": "JRWiener", "icon": null, "image": "/files/badges/0c1d2e3f4a5b6c7d.webp"}]},
 "date": "2026-11-07T00:00:00+00:00", "date_text": "13d",
 "body_html": "<p>for anyone not reading the description: ...</p>", "body_text": "for anyone not reading ...",
 "like_count": 327, "pinned": true, "link": "https://jrwishow.com/supporters/videos/...?cid=comment_...",
 "replies": [ /* comments */ ]}
```

- **Dates:** the site only shows how long ago a comment was posted ("13d"), so `date` is counted back from when that page was downloaded. The site shows older comments more roughly ("5mo"), so when a later download is rougher, the earlier, more exact `date` is kept. `date_text` is what the site showed for the `date` that's kept.
- **`pinned` (v0.4):** pinned comments come first, as on the site.
- **`link` (v0.4):** the comment's own address on the site, or `null`.
- **`author.avatar`:** `null` for members without a profile picture. The site shows their initial instead, and apps can do the same.
- **`author.badges` (v0.5):** the badges next to the name, in the site's order: the membership tier (an image, with the tier name as `name`), months as a member (an emoji `icon` and text) and `Verified` on verified accounts. Any of `name`, `icon` and `image` can be `null`. An empty list when there are none.
- **Fields that can be missing:** `date` and `like_count` are `null` when the site didn't show them.
- **Which comments:** every comment and reply, fetched from the site's comments endpoint with paging. For an item, they come from every version, starting with the main post's, newest first; `/api/posts/{postId}/comments` gives one post's only.
- **`post_id` (v0.7):** the post the comment was made on, so apps can show which version it belongs to.

### Event

```json
{"type": "new_item", "at": "2026-11-20T11:19:30+00:00", "post_id": 300, "title": "Through the Fire | Riptide #136", "message": ""}
```

- **`type`:** one of `new_item`, `check_failed` or `login_failed`.
- **`new_item`:** sent once per item, when it has finished archiving.
