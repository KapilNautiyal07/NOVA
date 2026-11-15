from __future__ import annotations

import re
from urllib.parse import urlparse

STOPWORDS = set(
    """a an the this that these those i me my we our you your it its is are was were be been to of in on
    for with about from at by and or website websites site page pages web link that where which when
    find show open saved save visited visit reading read looking look last yesterday today week month
    ago one some any please can could would remind reminder all document documents file files pdf
    downloaded download""".split()
)


class NotFound(Exception):
    def __init__(self, what: str, ident: int | str | None = None):
        super().__init__(f"{what} not found" + (f": {ident}" if ident is not None else ""))
        self.what = what


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def search_tokens(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+#.\-]*", query.lower())
    return [w.strip(".-") for w in words if len(w) > 1 and w not in STOPWORDS][:8]


CATEGORY_RULES = [
    ("Video", ("youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "netflix.com")),
    ("Code", ("github.com", "gitlab.com", "bitbucket.org", "stackoverflow.com", "huggingface.co")),
    ("Jobs", ("linkedin.com/jobs", "indeed.", "glassdoor.", "greenhouse.io", "lever.co", "workday", "naukri.com", "wellfound.com")),
    ("Docs", ("docs.", "readthedocs", ".github.io", "developer.", "/docs", "/documentation", "learn.microsoft.com", "mdn")),
    ("Shopping", ("amazon.", "flipkart.com", "ebay.", "etsy.com")),
    ("News", ("news.", "nytimes.com", "bbc.", "theguardian.com", "reuters.com", "techcrunch.com")),
    ("Social", ("twitter.com", "x.com", "reddit.com", "facebook.com", "instagram.com")),
    ("Learning", ("coursera.org", "udemy.com", "khanacademy.org", "edx.org", "medium.com", "dev.to", "wikipedia.org")),
]


def categorize(url: str) -> str:
    low = url.lower()
    for name, needles in CATEGORY_RULES:
        if any(n in low for n in needles):
            return name
    return "Other"
