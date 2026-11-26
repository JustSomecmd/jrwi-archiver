"""The notice for a new version of the archiver (since 0.14.12): asked of the registry, with what's new from the
release commits, sent once to the notification address."""

from __future__ import annotations

import requests

from jrwi import VERSION, updates


class Answer:
    def __init__(self, data, status=200, links=None):
        self.data, self.status_code, self.links = data, status, links or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Db:
    def __init__(self, **settings):
        self.s = {"update_check": True, "notify_url": "", **settings}

    def setting(self, key):
        return self.s.get(key)

    def set_setting(self, key, value):
        self.s[key] = value


def bump(version: str, by: int = 1) -> str:
    a, b, c = updates.parse(version)
    return f"{a}.{b}.{c + by}"


def registry(tags_pages: list[list[str]], commits: list[str] | None = None, fail: bool = False):
    asked = []

    def get(url, params=None, headers=None, timeout=None):
        asked.append(url)
        if fail:
            raise requests.ConnectionError("no route to host")
        if url.endswith("/token"):
            assert params["scope"] == f"repository:{updates.IMAGE}:pull"
            return Answer({"token": "anon"})
        if "/tags/list" in url:
            assert headers["Authorization"] == "Bearer anon"
            page = 0 if "last=" not in url else int(url.rsplit("last=p", 1)[1])
            links = {"next": {"url": f"/v2/{updates.IMAGE}/tags/list?n=1000&last=p{page + 1}"}} \
                if page + 1 < len(tags_pages) else {}
            return Answer({"tags": tags_pages[page]}, links=links)
        if url.endswith("/commits"):
            return Answer([{"commit": {"message": m}} for m in commits or []])
        raise AssertionError(url)
    return get, asked


def test_versions_and_order():
    assert updates.newer("0.14.12", "0.14.11") and updates.newer("0.15.0", "0.14.99")
    assert not updates.newer("0.14.9", "0.14.10") and not updates.newer("latest", "0.1.0")
    get, asked = registry([["latest", "sha-abc1234", "0.14.9"], ["0.14.10", "0.14.11", "main"]])
    assert updates.published(get) == ["0.14.9", "0.14.10", "0.14.11"]
    assert sum("/tags/list" in u for u in asked) == 2   # both pages of the list


def test_a_new_version_shows_with_whats_new_and_is_sent_once():
    nxt, after = bump(VERSION), bump(VERSION, 2)
    commits = [f"{after}: something later\n\nmore words", "docs/api.md: not a release", f"{nxt}: the next one",
               f"{VERSION}: this version"]
    get, _ = registry([[VERSION, nxt, after]], commits)
    sent = []

    def post(url, data=None, json=None, headers=None, timeout=None):
        sent.append((url, data.decode() if data else json))
        return Answer({})

    db = Db(notify_url="https://ntfy.sh/my-topic")
    u = updates.check(db, get, post)
    assert u["available"] and u["latest"] == after and u["version"] == VERSION and not u["error"]
    assert u["notes"] == [{"version": nxt, "text": "the next one"}, {"version": after, "text": "something later"}]
    assert len(sent) == 1 and sent[0][0] == "https://ntfy.sh/my-topic"
    assert f"{after} is out" in sent[0][1] and updates.COMMAND in sent[0][1] and "the next one" in sent[0][1]
    updates.check(db, get, post)
    assert len(sent) == 1   # once per version


def test_up_to_date_offline_and_off():
    get, _ = registry([[VERSION]])
    db = Db()
    u = updates.check(db, get)
    assert u["latest"] == VERSION and not u["available"] and u["notes"] == []
    # The registry can't be reached: what was known stays, with the reason
    get, _ = registry([], fail=True)
    u = updates.check(db, get)
    assert u["latest"] == VERSION and "no route to host" in u["error"]
    db.set_setting("update_check", False)
    assert updates.status(db) is None
