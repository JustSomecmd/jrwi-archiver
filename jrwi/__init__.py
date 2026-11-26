"""JRWI archiver: keeps a private copy of the Just Roll With It members area."""

VERSION = "0.14.12"
NAME = "jrwi-archiver"
# The version of the API contract (docs/api.md) this archiver follows, and its optional parts by name, for /api/ping
CONTRACT = "0.30"
FEATURES = ["post_progress", "tags", "continue", "stop", "feed", "stats", "reparse", "podcast", "watched", "transcript",
            "transcript_jobs", "transcript_claims", "transcript_program"]
