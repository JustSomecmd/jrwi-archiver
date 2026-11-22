"""Timed transcripts: reading them by font, and placing their lines from words heard with mistakes."""

import random
import tempfile
from pathlib import Path

from jrwi.transcripts import find, match, read_pdf, tokens

from transcript_pdf import write

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
