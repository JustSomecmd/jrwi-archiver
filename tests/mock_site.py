"""A stand-in for jrwishow.com, built from markup seen on the live site's public pages.

Run: python tests/mock_site.py 8765   (needs tests/media, made by tests/make_media.sh)
Login: fan@example.com / hunter2
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MEDIA = Path(__file__).parent / "media"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
B = f"http://127.0.0.1:{PORT}"
SESSION, CSRF = "sess-ok", "csrf-123"
SHOWS = [("riptide", "Riptide"), ("unwitness-protection-program", "Unwitness Protection Program"),
         ("miller-troop-family-vacation", "Miller Troop Family Vacation")]


def header(logged_in):
    acct = "/supporters/account" if logged_in else "/supporters/sign_in"
    shows = "".join(f'<a href="/pages/{s}">{n}</a>' for s, n in SHOWS)
    side = "" if logged_in else '<aside><a href="/supporters/pricing">Join now</a> <a href="/supporters/sign_in">Login</a></aside>'
    return (f'<header><a href="/supporters"><img src="{B}/imgproxy/logo/logo.webp" alt="Just Roll With It"></a>'
            f'<a href="/">Home</a><div class="menu"><span>Shows</span><div>{shows}<a href="/supporters/videos">View All</a></div></div>'
            '<div class="menu"><span>Members only</span><a href="/supporters">Feed</a><a href="/supporters/perks">Perks</a>'
            '<a href="/pages/dm-notes">DM Notes</a></div><a href="/pages/news">News</a><a href="/pages/faqs">FAQs</a>'
            f'<a aria-label="Your account" href="{acct}"><svg></svg></a><a href="/cart">Cart</a></header>{side}')


def page(body, logged_in, head=""):
    return (f'<!DOCTYPE html><html><head><title>JRWI</title><link rel="icon" href="{B}/imgproxy/icon/fav.webp">'
            f'<meta name="csrf-param" content="authenticity_token"><meta name="csrf-token" content="{CSRF}">'
            f'{head}</head><body>{header(logged_in)}<main>{body}</main></body></html>')


# Like the real site may do: the form has no hidden token; the browser adds the page's token when it submits
LOGIN_FORM = ('<form action="/supporters/sign_in" method="post" data-turbo="true">'
              '<input type="email" name="supporter[email]" id="supporter_email" autocomplete="email">'
              '<input type="password" name="supporter[password]" id="supporter_password">'
              '<button type="submit">Login</button></form>')


def share(path):
    return (f'<div class="dropdown" data-controller="dropdown link-share" data-link-share-url-value="{B}{path}'
            f'?utm_medium=copy-share-link&amp;utm_source=share-link"></div>')


TAG_NAMES = {"101": "RIPTIDE", "102": "Riptide Rolled", "103": "Art"}


def comment_preview(pid):
    """Like the real feed: a comment with a profile pop-up that carries the commenter's join date."""
    return (f'<turbo-frame class="post__section"><div class="comments"><turbo-frame class="comments__container" id="post_{pid}_comments">'
            f'<turbo-frame class="comments__comment" id="comment_9{pid}"><div class="comment" data-testid="Post.Comment">'
            '<div class="comment__aside"><img class="avatar__image" alt="grizzly" src="' + B + '/imgproxy/av/g.webp"></div>'
            '<span class="comment__author"><div class="hidden"><div class="profile-popover"><time datetime="2025-12-31T10:00:00Z">Dec 31, 2025</time></div></div>'
            '<div data-testid="Post.CommentAuthor">grizzly</div></span><span class="comment__date">13d</span>'
            '<div class="comment__body"><p>Preview comment</p></div></div></turbo-frame></turbo-frame></div></turbo-frame>')


def frame(pid, path, title, date, media, body, extra="", access="tiers", locked=False, tags="", likes=10, comments=0):
    title_href = "/supporters/pricing" if locked else path
    lock = f'<a href="/supporters/payments/checkout/posts/{pid}/available_tiers">Join to access</a>' if locked else ""
    pills = "".join(f'<form action="/supporters" method="get"><input type="radio" name="tag_id" value="{t[4:]}" checked>'
                    f'<button class="pill" type="submit">{TAG_NAMES.get(t[4:], t)}</button></form>'
                    for t in tags.split() if t.startswith("tag_"))
    return (f'<turbo-frame class="main-list__list-item {tags}" data-testid="Post" id="post_{pid}">'
            f'<div class="post" access="{access}"><div class="post__inner"><div class="post__media">{media}</div>'
            f'<div class="post__main"><div class="post__content">'
            f'<a data-turbo-frame="_top" class="post__meta" href="{title_href}">{date}</a>'
            f'<div><a data-turbo-frame="_top" class="post__title" href="{title_href}">{title}</a></div>{lock}'
            f'<div class="post__body" data-controller="trim"><div class="rich-text" data-trim-target="content">{body}</div>'
            f'<button>...Continue reading</button></div>{extra}'
            f'<div class="post__section"><div class="post__tags">{pills}</div></div>'
            f'<div class="post__section"><div class="post-actions">{share(path)}'
            f'<turbo-frame id="post_{pid}_like"><button aria-label="Like"><span data-testid="Post.LikesCount"> {likes} </span></button></turbo-frame>'
            f'<label aria-label="Comment"><span id="post_{pid}_comments_count">{comments}</span></label></div></div>'
            f'{comment_preview(pid) if not locked else ""}</div></div></div></div></turbo-frame>')


POSTS = {}


def add(pid, path, *a, **kw):
    POSTS[path] = (pid, frame(pid, path, *a, **kw))
    return POSTS[path][1]


def mux_video(pid, token=None):
    tok = f' playback-token="{token}"' if token else ""
    return (f'<div class="media-player media-player--video"><mux-player data-controller="mux-player" playback-id="vid{pid}"{tok} '
            f'metadata-video-id="{pid}" poster="{B}/imgproxy/thumb{pid}/x.webp"></mux-player></div>')


def mux_audio(pid):
    return (f'<div class="audio-player"><div class="audio-player__media">'
            f'<img class="audio-player__media-background hidden@sm" src="{B}/imgproxy/abg{pid}/rs:fill:712:200:0/bl:20/bg.webp">'
            f'<img class="audio-player__media-image" alt="Audio thumbnail" src="{B}/imgproxy/acover{pid}/rs:fill:200:200:0/cover.webp"></div><mux-player audio data-controller="mux-player" playback-id="aud{pid}" '
            f'metadata-video-id="{pid}" default-duration="11695"></mux-player></div>')


def dl_audio(pid, name):
    return (f'<a class="text-button" href="{B}/mux/aud{pid}/audio.m4a?download={name}.m4a" download="true">'
            '<div class="post__download">Download audio</div></a>')


P1 = [
    add(300, "/supporters/videos/through-the-fire-riptide-136", "Through the Fire | Riptide #136", "Nov 7, 2026",
        mux_video(300, token="good") + "<span>Pinned</span>", "<p>Episode 136!</p>", tags="tag_101", likes=469, comments=3),
    add(299, "/supporters/posts/zombie-survival-guide", "ZOMBIE SURVIVAL GUIDE - Miller Troop Ruleset", "Nov 17, 2026",
        f'<div class="gallery"><a href="{B}/imgproxy/full1/img.webp"><img src="{B}/imgproxy/s1/a.webp"></a></div>',
        '<p>Hi, scouts!</p><p>Mechanics in <a href="https://docs.google.com/document/d/10vanqYbkaHg5j2lE7HmPq0mSA3Y88IXY8TqF6XfNmC4/edit">this doc</a>.</p>',
        extra=('<div class="post__section post__attachments"><button class="post__attachment" '
               'data-controller="post-attachment-download" '
               'data-post-attachment-download-path-value="/supporters/api/v1/posts/299/attachments/11/download_url" '
               'data-post-attachment-download-sign-in-path-value="/supporters/sign_in" type="button">'
               '<span class="post__attachment-name">Zombie Character Sheets.pdf</span></button></div>'), access="public", comments=2),
    add(298, "/supporters/audio/through-the-fire-riptide-136-audio-only", "Through the Fire | Riptide #136 (AUDIO ONLY)",
        "Nov 7, 2026", mux_audio(298), "<p>Audio version.</p>",
        extra=dl_audio(298, "Through%20the%20Fire%20%7C%20Riptide%20%23136%20%28AUDIO%20ONLY%29"), tags="tag_101"),
    add(297, "/supporters/videos/just-rolled-with-it-riptide-136", "Just Rolled With It | Riptide #136", "Nov 7, 2026",
        mux_video(297), "<p>The aftershow.</p><p>The cast talk through the episode: the fight in the harbour, the deal "
        "with the captain, and what comes next for the crew.</p><p>Questions from the members get answered at the end, "
        "so leave yours in the comments.</p><p>See you next time!</p>", tags="tag_102"),
    add(296, "/supporters/videos/high-roller-special", "High Roller Special", "Oct 10, 2026",
        '<a class="post__media" href="/supporters/payments/checkout/posts/296/available_tiers"><span>1:00:00</span></a>',
        "", locked=True),
]
P2 = [
    add(290, "/supporters/posts/riptide-135-art-scene", "Riptide #135 | Art Scene", "Oct 18, 2026",
        f'<div class="gallery"><a href="{B}/imgproxy/art1/a.webp"><img src="{B}/imgproxy/s2/b.webp"></a>'
        f'<a href="{B}/imgproxy/art2/b.webp"><img src="{B}/imgproxy/s3/c.webp"></a></div>',
        # Files shared from Google Drive: a small one, and one large enough that Drive asks to confirm
        '<p>Maps: <a href="https://drive.google.com/file/d/1SmallMapSmallMapSmallMap0001/view?usp=sharing">Map.pdf</a> '
        'and <a href="https://drive.google.com/open?id=1BigMapBigMapBigMapBigMap00002">the big one</a>.</p>',
        tags="tag_103 tag_101"),
    add(289, "/supporters/video_embeds/blue-jay-riptide-135", "Blue Jay | Riptide #135", "Oct 17, 2026",
        '<div class="embed-player" data-controller="youtube-player" data-youtube-player-video-id-value="CzZ13VPQOcw"></div>',
        "<p>On YouTube.</p>", access="public", tags="tag_101"),
    add(288, "/supporters/audio/blue-jay-riptide-135-audio-only", "Blue Jay | Riptide #135 (AUDIO ONLY)", "Oct 17, 2026",
        mux_audio(288), "<p>Audio.</p>", extra=dl_audio(288, "Blue%20Jay"), access="public", tags="tag_101"),
    add(287, "/supporters/videos/keeper-notes-unwitness", "Keeper Notes - Unwitness [Empty Graves]", "Oct 8, 2026",
        mux_video(287), "<p>Notes.</p>"),
]

TAGS = ('<div class="filters">'
        '<button value="101"><img alt="RIPTIDE\'s tag thumbnail" src="x"><span>RIPTIDE</span><span>277</span></button>'
        '<button value="102"><img alt="Riptide Rolled\'s tag thumbnail" src="x"><span>Riptide Rolled</span><span>125</span></button>'
        '<button value="103"><img alt="Art\'s tag thumbnail" src="x"><span>Art</span><span>136</span></button></div>')

BADGES = ('<div class="badge badge--small"><div class="badge__icon">🔥</div> 4-month member</div>'
          '<div class="badge badge--small badge--minimal" data-controller="tooltip" data-tippy-content="JRWiener">'
          f'<div class="badge__icon"><img alt="Badge icon" class="badge__image" src="{B}/imgproxy/badge-tier/b.webp"></div></div>')


# Members' IDs, as the site puts them in each comment's Report form. Alex is Kim under a new name; Lee's comment
# is the user's own, which the site doesn't let you report, so it has none.
MEMBER_IDS = {"Sam": "81000001", "Jo": "81000002", "Kim": "81000004", "Pat": "81000005", "Alex": "81000004"}


def real_comment(pid, cid, author, body, likes=0, replies=0, pinned=False, when="13d", picture=True, badges=False):
    """A comment as the real site renders it, including the parts that must be ignored. Members without a
    profile picture get their initial instead, and their badges are then the first images in the comment."""
    sid = MEMBER_IDS.get(author)
    report = ('<form action="/supporters/reported_supporters" method="post"><input type="hidden" name="authenticity_token" '
              f'value="tok"><input type="hidden" name="reported_supporter_id" value="{sid}"><button>Report</button></form>'
              if sid else "")
    badge = '<div class="comment__badge-container"><div class="pill">Pinned</div></div>' if pinned else ""
    avatar = (f'<div class="avatar"><img class="avatar__image" alt="{author}" src="{B}/imgproxy/av-{author}/a.webp"></div>'
              if picture else f'<div class="avatar avatar--medium">\n        {author[0].lower()}\n</div>')
    author_badges = f'<div class="comment__author-badges">{BADGES if badges else ""}</div>'
    popover = (f'<div class="hidden"><div class="profile-popover"><div class="profile-popover__user">{avatar}'
               f'<div class="profile-popover__badges">{BADGES}</div></div></div></div>')
    load = (f'<form action="/supporters/posts/{pid}/comments/{cid}/replies" data-testid="Post.LoadRepliesButton" '
            f'data-turbo-stream="true" method="get"><input type="hidden" name="limit" value="10"><button>{replies} '
            f'<turbo-frame id="comment_{cid}_reply_text">{"Load replies" if replies else "Reply"}</turbo-frame></button></form>')
    return (f'<turbo-frame class="comments__comment" id="comment_{cid}"><div class="comment" data-testid="Post.Comment">{badge}'
            f'<div class="comment__aside">{popover}<div>{avatar}</div></div>'
            f'<turbo-frame id="comment_{cid}_edit_form"></turbo-frame><div class="comment__main"><div class="comment__header">'
            f'<span class="comment__author"><div class="hidden"><div class="profile-popover"><div class="profile-popover__nickname">{author}</div>'
            '<time datetime="2025-07-16T22:34:22Z">Jul 16, 2025</time></div></div>'
            f'<div class="comment__author-name"><div data-testid="Post.CommentAuthor">{author}</div></div>{author_badges}</span>'
            f'<div class="comment__date-and-actions"><a href="/supporters/posts/x?cid=comment_h{cid}"><span class="comment__date">{when}</span></a>'
            f'<div class="comment__dropdown"><div class="dropdown"><div class="modal"><p>Block someone?</p>{report}</div></div></div></div></div>'
            f'<div class="comment__content"><div><div class="comment__body"><p>{body}</p></div><button>Read more</button></div></div>'
            f'<div class="comment__footer"><turbo-frame id="comment_{cid}_like"><form action="/supporters/posts/{pid}/comments/{cid}/like" method="post">'
            f'<button aria-label="Like"><span class="comment__footer-item-number">{likes}</span></button></form></turbo-frame>{load}</div>'
            f'<turbo-frame class="comments__list" id="comment_{cid}_replies"></turbo-frame></div></div></turbo-frame>')


def more_form(pid, marker, cid=None):
    action = f"/supporters/posts/{pid}/comments" + (f"/{cid}/replies" if cid else "")
    return (f'<form action="{action}" method="get" data-turbo-stream="true"><input type="hidden" name="limit" value="10">'
            f'<input type="hidden" name="order" value="newest"><input type="hidden" name="next_page_data" value="{marker}">'
            '<button>View more comments</button></form>')


COMMENT_PAGES = {
    (299, None): [real_comment(299, 501, "Sam", "Love these rules!", likes=4, replies=1),
                  real_comment(299, 503, "Jo", "Printing these now. I love rules so much, and this is a genuinely good ruleset: short, "
                               "simple to learn, with only a couple of caveats that make the game better defined overall. "
                               "If you're at all interested in survival games, give it a go with your own group.")], 
    (299, "p2"): [real_comment(299, 504, "Lee", "Great work, thanks!", when="2w", picture=False, badges=True)],
    (300, None): [real_comment(300, 601, "Kim", "Best episode yet.", likes=327, pinned=True)],
    (298, None): [real_comment(298, 701, "Pat", "Listening on my commute.", likes=2)],
}
REPLY_PAGES = {(299, 501): [real_comment(299, 502, "Alex", "Same here.", when="2d")]}


def comments_stream(pid, marker):
    frames = COMMENT_PAGES.get((pid, marker), [])
    more = more_form(pid, "p2") if (pid, "p2") in COMMENT_PAGES and marker is None else ""
    return (f'<turbo-stream action="append" target="post_{pid}_comments"><template>{"".join(frames)}{more}'
            '</template></turbo-stream>')


def watch_page(pid, fr):
    """Video and audio pages, shaped like the real one: a video_page block plus a column of other posts."""
    player = re.search(r"(<mux-player.*?</mux-player>|<div class=\"embed-player.*?</div>)", fr, re.S)
    title = re.search(r'class="post__title"[^>]*>(.*?)</a>', fr).group(1)
    date = re.search(r'class="post__meta"[^>]*>(.*?)</a>', fr).group(1)
    desc = re.search(r'data-trim-target="content">(.*?)</div>', fr, re.S).group(1)
    suggestion = ('<div class="next-video-suggestion hidden"><a class="video video--static" href="#"></a>'
                  '<a class="button" href="/supporters/videos/some-other-video?series_id=1">Play now</a></div>')
    media = player.group(1).replace("</mux-player>", suggestion + "</mux-player>") if player else ""
    sidebar = ('<div class="grid__column grid__column--7@md"><div>Recently uploaded</div>'
               f'<a data-testid="VideoCatalog.Video" href="/supporters/payments/checkout/posts/1/available_tiers">'
               f'<img alt="Thumbnail" src="{B}/imgproxy/side/s.webp"><span>3:14:55</span><span>Some other episode</span></a></div>')
    return (f'<div class="main"><turbo-frame class="video-page" id="video_page"><div class="video-page__video-container">{media}</div>'
            '<div class="container"><div class="grid"><div class="grid__column grid__column--16@md"><div class="video-page__details">'
            f'<div class="video-page__header"><div class="video-page__meta">{date}</div><div class="video-page__title">{title}</div></div>'
            f'<div class="video-page__description" data-controller="trim"><div class="rich-text">{desc}</div></div>'
            f'</div></div>{sidebar}</div></div></turbo-frame></div>')


# Stage 1 (set by GET /_test/stage?n=1): Riptide #136's aftershow and the Unwitness keeper notes go up on YouTube.
# The site adds a post embedding each YouTube video, dated like the members-only post, and takes the aftershow's
# members-only post (297) down.
STAGE = {"n": 0}
YT_POSTS = [
    (295, "/supporters/video_embeds/just-rolled-with-it-riptide-136-yt", "Just Rolled With It | Riptide #136", "Nov 7, 2026",
     '<div class="embed-player" data-controller="youtube-player" data-youtube-player-video-id-value="JRWI136ytAA"></div>',
     "<p>Now on YouTube.</p>", {"access": "public", "tags": "tag_102"}),
    (294, "/supporters/video_embeds/keeper-notes-unwitness-yt", "Keeper Notes - Unwitness [Empty Graves]", "Oct 8, 2026",
     '<div class="embed-player" data-controller="youtube-player" data-youtube-player-video-id-value="KEEPERytBB"></div>',
     "<p>Notes, on YouTube.</p>", {"access": "public"}),
]
for _pid, _path, _title, _date, _media, _body, _kw in YT_POSTS:
    add(_pid, _path, _title, _date, _media, _body, **_kw)
GONE = {"/supporters/videos/just-rolled-with-it-riptide-136"}
# TubeArchivist: (duration, file) for the YouTube videos it has downloaded
TA_TOKEN = "ta-test-token"
TA_VIDEOS = {"JRWI136ytAA": (23.0, "youtube.mp4"), "KEEPERytBB": (999.0, "youtube.mp4"), "CzZ13VPQOcw": (23.0, "youtube.mp4")}


def stage_posts(posts):
    if STAGE["n"] < 1:
        return posts
    added = [POSTS[path][1] for pid, path, *_ in YT_POSTS]
    gone = {POSTS[g][1] for g in GONE}
    return added[:1] + [f for f in posts if f not in gone] if posts is P1 else [f for f in posts if f not in gone] + added[1:]


def feed_page1(li):
    items = stage_posts(P1) if li else [P1[4]]
    more = ('<turbo-frame id="posts_load_more"><a data-turbo-stream="true" data-controller="infinite-scroll" '
            'href="/supporters/load_more?last_id=296&amp;last_live_at=x&amp;order=desc"></a></turbo-frame>')
    return page(f'{TAGS}<div id="posts_list">{"".join(items)}</div>{more}', li)


def stream(frames, nxt):
    out = "".join(f'<turbo-stream action="append" target="posts_list"><template>{f}</template></turbo-stream>' for f in frames)
    if nxt:
        out += ('<turbo-stream action="append" target="posts_list"><template><turbo-frame id="posts_load_more">'
                f'<a data-turbo-stream="true" data-controller="infinite-scroll" href="{nxt}"></a></turbo-frame></template></turbo-stream>')
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def li(self):
        return f"_jrwi_session={SESSION}" in (self.headers.get("Cookie") or "")

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, path, ctype):
        data = path.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            chunk = data[start:]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send(200, data, ctype)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        li = self.li()
        p = u.path
        if p == "/supporters/sign_in":
            return self.send(200, page(LOGIN_FORM, False))
        if p == "/supporters":
            return self.send(200, feed_page1(li))
        if p == "/supporters/load_more":
            if "turbo-stream" not in (self.headers.get("Accept") or ""):
                return self.send(406, "no")
            if q.get("last_id") == ["296"]:
                return self.send(200, stream(stage_posts(P2), "/supporters/load_more?last_id=287&last_live_at=y&order=desc"),
                                 "text/vnd.turbo-stream.html")
            return self.send(200, stream([], None), "text/vnd.turbo-stream.html")
        if p == "/_test/stage":
            STAGE["n"] = int(q.get("n", ["0"])[0])
            return self.send(200, "ok")
        if p.startswith("/ta/"):
            return self.tubearchivist(p[3:])
        if STAGE["n"] >= 1 and p in GONE:
            return self.send(404, "gone")
        if p in POSTS and p.split("/")[2] in ("videos", "audio", "video_embeds"):
            pid, fr = POSTS[p]
            if pid == 296:
                return self.send(302, "", headers={"Location": "/supporters/pricing"})
            return self.send(200, page(watch_page(pid, fr), li))
        if p in POSTS:
            pid, fr = POSTS[p]
            return self.send(200, page(f"<ul><li>{fr}</li></ul>", li))
        m = re.match(r"^/supporters/posts/(\d+)/comments(?:/(\d+)/replies)?$", p)
        if m:
            if not li:
                return self.send(302, "", headers={"Location": "/supporters/sign_in"})
            if "turbo-stream" not in (self.headers.get("Accept") or ""):
                return self.send(406, "no")
            pid, cid = int(m.group(1)), m.group(2)
            if cid:
                body = "".join(REPLY_PAGES.get((pid, int(cid)), []))
                return self.send(200, f'<turbo-stream action="append" target="comment_{cid}_replies"><template>{body}'
                                      '</template></turbo-stream>', "text/vnd.turbo-stream.html")
            return self.send(200, comments_stream(pid, q.get("next_page_data", [None])[0]), "text/vnd.turbo-stream.html")
        if p.startswith("/pages/"):
            slug = p.split("/")[2]
            name = dict(SHOWS).get(slug, slug.replace("-", " ").title())
            # Only Riptide's page has its own art; the others carry the site-wide share picture and logo
            own = slug == "riptide"
            poster = f"{B}/imgproxy/poster-{slug}/p.webp" if own else f"{B}/imgproxy/share/site.webp"
            banner = f"{B}/imgproxy/banner-{slug}/b.webp" if own else f"{B}/imgproxy/logo-big/logo.webp"
            head = (f'<meta property="og:title" content="{name}"><meta property="og:image" content="{poster}">'
                    f'<meta property="og:description" content="All about {name}.">')
            return self.send(200, page(f'<h1>{name}</h1><img src="{banner}"><p>Page text for {name}.</p>', li, head))
        if p == "/supporters/perks":
            return self.send(200, page('<h1>Perks</h1><a href="https://jrwishow.com/supporters/podcast/abc/rss">Private RSS</a>', li))
        if p == "/supporters/pricing":
            return self.send(200, page("<h1>Pricing</h1>", li))
        if p == "/supporters/api/v1/posts/299/attachments/11/download_url":
            if not li:
                return self.send(401, '{"error": "sign in"}', "application/json")
            return self.send(200, '{"url": "' + B + '/signed/Zombie%20Character%20Sheets.pdf?sig=abc"}', "application/json")
        if p.startswith("/gdocs/") and p.endswith("/export"):
            return self.send(200, (MEDIA / "sheet.pdf").read_bytes(), "application/pdf")
        if p == "/drive/download":
            fid = q.get("id", [""])[0]
            if fid.startswith("1Big") and q.get("confirm", [""])[0] != "t":
                return self.send(200, f'<html><body><p>Google Drive can\'t scan this file for viruses.</p>'
                                      f'<form id="download-form" action="{B}/drive/download" method="get">'
                                      f'<input type="submit" value="Download anyway"><input type="hidden" name="id" value="{fid}">'
                                      f'<input type="hidden" name="export" value="download"><input type="hidden" name="confirm" value="t">'
                                      f'<input type="hidden" name="uuid" value="u-123"></form></body></html>')
            name = {"1Small": "Map.pdf", "1BigMa": "Big Map.pdf"}.get(fid[:6])
            if not name:
                return self.send(404, "<html>Not found</html>")
            return self.send(200, (MEDIA / "sheet.pdf").read_bytes(), "application/pdf",
                             {"Content-Disposition": f'attachment; filename="{name}"'})
        if p.startswith("/signed/"):
            return self.send(200, (MEDIA / "sheet.pdf").read_bytes(), "application/pdf")
        if p.startswith("/imgproxy/"):
            return self.file(MEDIA / "img.webp", "image/webp")
        if p.startswith("/mux/aud") and p.endswith("/audio.m4a"):
            return self.file(MEDIA / "audio.m4a", "audio/mp4")
        if p.startswith("/mux/vid") and p.endswith(".m3u8"):
            if p == "/mux/vid300.m3u8" and q.get("token") != ["good"]:
                return self.send(403, "bad token")
            return self.file(MEDIA / "master.m3u8", "application/vnd.apple.mpegurl")
        if p.startswith("/mux/") and (MEDIA / p[5:]).exists():
            f = MEDIA / p[5:]
            return self.file(f, "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t")
        return self.send(404, "not found")

    def tubearchivist(self, p):
        if self.headers.get("Authorization") != f"Token {TA_TOKEN}":
            return self.send(403, '{"detail": "Invalid token."}', "application/json")
        if p == "/api/ping/":
            return self.send(200, '{"response": "pong", "user": 1, "version": "v0.5.4"}', "application/json")
        m = re.match(r"^/api/video/([\w-]+)/$", p)
        if m:
            vid = m.group(1)
            if vid not in TA_VIDEOS:
                return self.send(404, '{"detail": "video not found"}', "application/json")
            duration, name = TA_VIDEOS[vid]
            return self.send(200, json.dumps({"youtube_id": vid, "title": vid, "player": {"duration": duration},
                                              "media_url": f"/media/UCjrwi/{vid}.mp4", "active": True,
                                              "media_size": (MEDIA / name).stat().st_size}), "application/json")
        m = re.match(r"^/media/UCjrwi/([\w-]+)\.mp4$", p)
        if m and m.group(1) in TA_VIDEOS:
            return self.file(MEDIA / TA_VIDEOS[m.group(1)][1], "video/mp4")
        return self.send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        if u.path == "/supporters/sign_in":
            token_ok = form.get("authenticity_token") == [CSRF] or self.headers.get("X-CSRF-Token") == CSRF
            if not token_ok:
                return self.send(422, "<html><body><h1>The change you wanted was rejected.</h1></body></html>")
            if form.get("supporter[email]") == ["fan@example.com"] and form.get("supporter[password]") == ["hunter2"]:
                return self.send(303, "", headers={"Location": "/supporters",
                                                    "Set-Cookie": f"_jrwi_session={SESSION}; path=/; HttpOnly"})
            return self.send(422, page('<div class="flash flash--alert" role="alert">Invalid Email or password.</div>'
                                       + LOGIN_FORM, False))
        return self.send(404, "nope")


if __name__ == "__main__":
    print(f"mock site on {B}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
