"""Timed transcripts: reading them by font, and placing their lines from words heard with mistakes."""

import random
import tempfile
from pathlib import Path

from jrwi.transcripts import find, match, read_pdf, tokens

from transcript_pdf import write, write_early

TURNS = [
    ("Zach", "There's something wrong with this town and no one seems to notice. The people walk around smiling, "
             "but their faces are wrong. We are outnumbered by what lives in the dark."),
    ("Charlie", "What?"),
    ("Nate", "What the fuck?"),
    (None, "[laughter]"),
    ("Condi", "Okay."),
    ("Zach", "Lui, you lay in a cocoon, hiding from the cold around you. [sighs] Your breathing is heavy."),
    (None, "[all the players] Whoa!"),
    ("Mrs. Duponte", "Lui, you know you have to get up. It is time to go. It was time to go 20 minutes ago."),
]


def test_a_transcript_is_read_by_its_bold_names():
    pdf = Path(tempfile.mkdtemp()) / "t.pdf"
    write(pdf, TURNS * 30)   # several pages, so turns go over page breaks
    lines = read_pdf(pdf)
    # The standard font of this test file gives ’ for ', which matching treats the same
    assert [(l["speaker"], l["text"].replace("’", "'")) for l in lines[:8]] == TURNS
    assert len(lines) == len(TURNS) * 30


EARLY = [
    ("Grizzly", "[multiple voices layered] Ayooo! It's ya boy Grizzly! I control the whole show."),
    (None, "[Schmove by Shady Cicada plays]"),
    ("Condi", "I sure am, pal."),
    ("Charlie", "Yar har har!"),
    ("Condi", "Yar har har!"),
    ("Grizzly", "The air is fresh but salty, and the sky is painted with the orange and purple colours of sunset, "
                "as a large rosewood ship glides across these pink tinted waters."),
    (None, "(Laughter)"),
    ("Captain Rose", "(laughs) Welcome aboard the Midnight Rose."),
]


def test_an_early_transcript_is_read_by_name_and_colon():
    """The early layout (asked for on 2026-11-23): a bold title over a cast list, then "Name : what they said",
    sometimes two turns with no gap between. The bold title mustn't be taken for a speaker of everything below it."""
    pdf = Path(tempfile.mkdtemp()) / "early.pdf"
    head = ["Dungeon Master & NPCS: Grizzly", "Arlin James: Bizly", "Finn Tidestrider: Slimecicle (Charlie)",
            "Prologue - The Hole in the Sea"]
    turns = EARLY * 20
    write_early(pdf, "Just Roll With It: Riptide", head, turns,
                joined={k for k in range(len(turns)) if k % len(EARLY) in (4, 5)})
    lines = read_pdf(pdf)
    assert [(l["speaker"], l["text"]) for l in lines[:5]] == [(None, "Just Roll With It: Riptide")] + \
        [(None, h) for h in head]
    assert [(l["speaker"], l["text"].replace("’", "'")) for l in lines[5:5 + len(EARLY)]] == EARLY
    assert len(lines) == 5 + len(turns)
    assert tokens("(laughs) Welcome aboard. (Laughter)") == ["welcome", "aboard"]


def test_a_transcript_is_found_by_its_name():
    root = Path(tempfile.mkdtemp())
    (root / "google-docs").mkdir()
    (root / "google-docs" / "Episode transcription here. [1RkmhusZ].pdf").write_bytes(b"%PDF-1.4")
    (root / "google-docs" / "Character sheet [abc].pdf").write_bytes(b"%PDF-1.4")
    video = {"id": 300, "media": {"video": {"path": "x.mp4"}},
             "documents": [{"name": "Character sheet [abc].pdf", "url": "google-docs/Character sheet [abc].pdf"}]}
    audio = {"id": 298, "media": {"audio": {"path": "x.m4a"}},
             "documents": [{"name": "Episode transcription here. [1RkmhusZ].pdf",
                            "url": "google-docs/Episode transcription here. [1RkmhusZ].pdf"}]}
    found = find([video, audio], root)
    assert found["post_id"] == 298 and found["path"].endswith("[1RkmhusZ].pdf")
    assert find([video], root) is None


def heard_from(lines, misheard: float, seed: int = 3):
    """What a recogniser might hear: the transcript's words at a speaking pace, with some lost or wrong."""
    rnd = random.Random(seed)
    truth, words, t = [], [], 5.0
    for l in lines:
        t += rnd.uniform(0.3, 1.2)
        truth.append(t)
        for w in tokens(l["text"]):
            d = rnd.uniform(0.2, 0.4)
            r = rnd.random()
            if r >= misheard:
                words.append([round(t, 2), round(t + d, 2), w])
            elif r < misheard / 3:
                words.append([round(t, 2), round(t + d, 2), "mumble"])
            t += d
    return truth, words


def varied_lines(n: int, seed: int = 5) -> list[dict]:
    """Lines of made-up speech: short replies and long narration, words repeating as speech does."""
    rnd = random.Random(seed)
    common = "okay yeah what the a to and I you it is that no so oh".split()
    vocab = common * 6 + [f"word{k}" for k in range(900)]
    out = []
    for _ in range(n):
        size = rnd.choice([1, 1, 2, 3, 5, 8, 12, 20, 40])
        out.append({"speaker": rnd.choice(["Zach", "Charlie", "Nate", "Condi"]),
                    "text": " ".join(rnd.choice(vocab) for _ in range(size)).capitalize() + "."})
        if rnd.random() < 0.04:
            out.append({"speaker": None, "text": "[laughter]"})
    return out


def test_lines_are_placed_from_words_heard_with_mistakes():
    lines = varied_lines(1500)
    truth, words = heard_from(lines, 0.3)
    result = match(lines, words)
    assert result["found"] > 0.5
    errors = sorted(abs(l["start"] - truth[i]) for i, l in enumerate(result["lines"])
                    if l["start"] is not None and tokens(l["text"]))
    assert errors[len(errors) // 2] < 0.5 and errors[int(len(errors) * 0.9)] < 2.5
    starts = [l["start"] for l in result["lines"] if l["start"] is not None]
    assert starts == sorted(starts)
    # A note isn't spoken; it goes right after the line before it
    k = next(i for i, l in enumerate(result["lines"]) if l["speaker"] is None and l["start"] is not None)
    assert result["lines"][k]["start"] >= result["lines"][k - 1]["start"]


def test_another_episodes_transcript_doesnt_match():
    lines = varied_lines(800, seed=1)
    _, words = heard_from(varied_lines(800, seed=2), 0.2)
    assert match(lines, words)["found"] < 0.3


def test_a_line_by_a_long_gap_with_nothing_heard_gets_no_time():
    lines = [{"speaker": "A", "text": "one two three four five"}, {"speaker": "B", "text": "Okay."},
             {"speaker": "A", "text": "six seven eight nine ten"}]
    words = [[1.0, 1.2, "one"], [1.2, 1.4, "two"], [1.4, 1.6, "three"], [1.6, 1.8, "four"], [1.8, 2.0, "five"],
             [300.0, 300.2, "six"], [300.2, 300.4, "seven"], [300.4, 300.6, "eight"]]
    out = match(lines, words)["lines"]
    assert out[0]["start"] == 1.0 and out[2]["start"] == 300.0 and out[1]["start"] is None


def test_time_left_while_hearing():
    from jrwi.transcripts import Pace, left_text
    pace = Pace()
    assert pace.left(0.10, now=0) is None            # where it starts from (carried on from a saved piece)
    assert pace.left(0.12, now=30) is None           # too soon to tell
    assert pace.left(0.20, now=120) == 120 * 0.80 / 0.10   # 10% in 2 minutes: 80% more takes 16
    pace.reset()
    assert pace.left(0.5, now=500) is None
    assert [left_text(s) for s in (20, 95, 16 * 60, 3600, 3600 + 25 * 60)] == [
        "under a minute left", "about 2 min left", "about 16 min left", "about 1 h left", "about 1 h 25 min left"]


def test_hearings_with_hotwords_dont_count_as_heard():
    """Until 0.14.7 the speakers' names were given as hotwords, which the models wrote out in loops in place of what
    was said. Those hearings don't count, so the PC program's run --all hears those episodes once more."""
    from jrwi.transcripts import Transcripts
    t = Transcripts.__new__(Transcripts)
    runs = [{"model": "large-v3-turbo", "found": 0.33, "sound_size": 5},
            {"model": "medium.en", "found": 0.5, "sound_size": 5, "no_hotwords": True},
            {"model": "small.en", "found": 0.6, "sound_size": 4, "no_hotwords": True}]
    assert t.heard_with(1, 5, runs) == ["medium.en"]


def test_a_word_after_a_pause_starts_where_it_likely_begins():
    """The model stretches the first word after a pause back into the silence before it, so lines began early
    (0.14.11): such a word starts at its end less the usual length, in this episode's running speech, of words with
    as many letters, moved by half a second at most, and words not heard at a line's start take the episode's own
    pace rather than a fixed 0.3 s."""
    from jrwi.transcripts import settle
    vocab = ["the", "town", "is", "quiet", "and", "we", "walk", "home", "slowly", "tonight"]
    running = [vocab[k % len(vocab)] for k in range(400)]
    words, t = [], 0.0
    for w in running:   # running speech: a word every 0.2 s
        words.append([round(t, 2), round(t + 0.2, 2), w])
        t += 0.2
    b = t + 1.0          # after a 1 s pause "hello" begins, but is heard 0.45 s early
    words += [[b - 0.45, b + 0.2, "hello"], [b + 0.2, b + 0.4, "there"], [b + 0.4, b + 0.6, "friend"]]
    c = b + 3.0          # after another pause, "wait" is stretched 3 s: moved half a second at most
    words += [[c, c + 3.2, "wait"], [c + 3.2, c + 3.4, "for"], [c + 3.4, c + 3.6, "me"]]
    d = c + 3.6          # straight on: "okay see you", its two first words not heard at all
    words += [[d, d + 0.2, "okay"], [d + 0.2, d + 0.4, "see"], [d + 0.4, d + 0.6, "you"]]
    settled, pace = settle(words)
    assert abs(pace - 0.2) < 0.01
    assert abs(settled[400][0] - b) < 0.01 and settled[400][1] == words[400][1]   # ends stay
    assert abs(settled[403][0] - (c + 0.5)) < 0.01
    assert settled[:400] == [list(w) for w in words[:400]]   # running speech isn't touched
    lines = [{"speaker": "A", "text": " ".join(running)}, {"speaker": "B", "text": "Hello there friend."},
             {"speaker": "C", "text": "Wait for me!"}, {"speaker": "D", "text": "Really truly okay, see you."}]
    out = match(lines, words)["lines"]
    assert abs(out[1]["start"] - b) < 0.02, out[1]
    assert abs(out[2]["start"] - (c + 0.5)) < 0.02, out[2]
    assert abs(out[3]["start"] - (d - 2 * 0.2)) < 0.02, out[3]   # two words before it, at the episode's pace
