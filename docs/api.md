# JRWI archiver API (v0.2)

**General rules**
- **Auth:** every request needs `Authorization: Token <key>`. The token is on the web app's Settings page. Players that can't set headers can add `?token=<key>` to the address instead.
- **Paths:** image, file, audio and video addresses in responses are paths such as `/files/...`, `/api/audio/...` or `/api/video/...`, relative to the server address. They take the same token.
- **Times** are ISO 8601 with a UTC offset. **Durations** are in seconds.
- **Errors** come back as `{"error": "message"}` with a 4xx or 5xx status.
- **Lists** are paged: `?page=1&page_size=50` returns `{"items": [...], "page": 1, "page_size": 50, "total": 123}`. `page_size` goes up to 200.

## Endpoints

| Method and path | What it does |
|---|---|
| `GET /api/ping` | `{"name": "jrwi-archiver", "version": "0.2.0"}`. Needs no token. |
| `GET /api/status` | Archiver state (below) |
| `POST /api/check` | Starts a check. `?full=true` reads the whole feed. `202` when started, `409` if one is running. |
| `GET /api/shows` | Shows that have items, in the site's menu order, `other` last. `?include_empty=true` lists all of them. |
| `GET /api/shows/{showId}` | One show, with `tabs` |
| `GET /api/shows/{showId}/items?tab=episodes&order=desc` | Paged items in one tab. In `episodes`, items are ordered by episode number. |
| `GET /api/items?q=&show=&tab=&kind=&order=desc` | Paged search across everything. `kind` is `video`, `audio` or `post`. |
| `GET /api/latest?limit=20` | Newest items, for a home screen |
| `GET /api/items/{postId}` | Full item. Either post ID of a video/audio pair works. |
| `GET /api/items/{postId}/comments?page=` | Threaded comments, paged by top-level comment |
| `GET /api/audio/{postId}` | The audio file, with HTTP range support |
| `GET /api/video/{postId}` | The video file, with HTTP range support. This is a fallback; Jellyfin is the main way to play video. |
| `GET /api/progress/{postId}` | `{"position": 1234.5, "duration": 11695, "finished": false, "updated_at": "..."}` |
| `PUT /api/progress/{postId}` | Body `{"position": 1234.5, "duration": 11695, "finished": false}`. Saves audio progress. |
| `GET /api/events?since=<time>` | New items and failures since a time (default: the last 24 hours) |
| `GET /api/problems` | Posts that couldn't be fully saved |
| `GET /api/feed.xml?token=<key>` | Private podcast RSS of all audio |
| `GET /files/<path>` | Images, thumbnails, attachments, documents |

Item lists leave out posts above the user's tier. Add `include_locked=true` to include them; they'll have `"locked": true` and no media.

## Objects

### Status

```json
{"running": true, "phase": "archiving posts", "detail": "Through the Fire | Riptide #136: 42%",
 "progress": {"done": 12, "total": 30},
 "last_check_started": "2026-11-20T11:19:59+00:00", "last_check_finished": "2026-11-20T11:20:00+00:00",
 "next_check": "2026-11-20T15:29:00+00:00", "last_result": "ok", "message": null, "login_ok": true,
 "counts": {"items": 2104, "archived": 2001, "locked": 87, "problems": 3},
 "disk": {"archive_bytes": 912345678901, "free_bytes": 2345678901234}}
```

- **`last_result`:** one of `ok`, `login_failed`, `blocked`, `error` or `stopped`.
- **`next_check`:** `null` when checks only run by hand.

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
           "url": "/api/video/300", "height": 1080, "size": 4123456789},
 "audio": {"post_id": 298, "url": "/api/audio/298", "mime": "audio/mp4", "size": 187654321},
 "comment_count": 12, "like_count": 469, "locked": false, "archived": true}
```

**Fields**
- **`id`:** the main post of the item. That's the video post when there is one, otherwise the audio post.
- **`kind`:** one of `episode`, `video`, `audio`, `youtube`, `art` or `post`.
- **`date`:** the site only shows a date, so the time is midnight UTC.

**`video` takes one of two shapes, or is `null`:**
- **Archived file:** `{post_id, jellyfin_path, url, height, size}`. `jellyfin_path` is the path as the archiver sees it. Match Jellyfin items on the `[jrwi-<post id>]` tag in the file name, not on the full path.
- **YouTube only:** `{post_id, youtube_id, youtube_url}`. The video exists only as a YouTube embed. If TubeArchivist has that video ID, play it from there.

**`audio`** is `null` when there's no archived audio version.

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
 "versions": [{"post_id": 300, "title": "Through the Fire | Riptide #136", "url": "https://..."},
              {"post_id": 298, "title": "Through the Fire | Riptide #136 (AUDIO ONLY)", "url": "https://..."}],
 "problems": []}
```

- **`body_html`:** cleaned of scripts, with images pointing at the archived copies.
- **`attachments`, `documents` and `images`:** collected from every version of the item.

### Comment

```json
{"id": "286486:1016036", "author": {"name": "grizzly", "avatar": "/files/avatars/b4a56f4a9840a6bb.webp"},
 "date": "2026-11-07T00:00:00+00:00", "date_text": "13d",
 "body_html": "<p>for anyone not reading the description: ...</p>", "body_text": "for anyone not reading ...",
 "like_count": 327, "pinned": true, "link": "https://jrwishow.com/supporters/videos/...?cid=comment_aq6A...",
 "replies": [ /* comments */ ]}
```

- **Dates:** the site only shows how long ago a comment was posted ("13d"), so `date` is worked out from that when the comment was saved. `date_text` is what the site showed.
- **`pinned`:** pinned comments come first, like on the site.
- **`link`:** the comment's own address on the site.
- **Fields that can be missing:** `date`, `like_count` and `author.avatar` are `null` when the site didn't show them.
- **Which comments:** every comment and reply. They come from every version of the item, starting with the main post's, newest first.

### Event

```json
{"type": "new_item", "at": "2026-11-20T11:19:30+00:00", "post_id": 300, "title": "Through the Fire | Riptide #136", "message": ""}
```

- **`type`:** one of `new_item`, `check_failed` or `login_failed`.
- **`new_item`:** sent once per item, when it has finished archiving.
