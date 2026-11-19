"""Substack agent: finds the week's best free Substack posts on your topics,
writes a short briefing, and checks every claim against the post itself.

Run:  python substack_agent.py
See README.md for setup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
USER_AGENT = "substack-briefing-agent/1.0"
POST_URL = re.compile(r"^https?://([a-z0-9-]+)\.substack\.com/p/([a-z0-9-]+)", re.I)
MIN_WORDS = 150          # shorter than this usually means a paywall stub or a note
WORDS_PER_MINUTE = 230
FALLBACK_BETA = "server-side-fallback-2026-07-01"


# --------------------------------------------------------------------------- data

@dataclass
class Post:
    url: str
    subdomain: str
    title: str
    subtitle: str
    author: str
    publication: str
    published: datetime
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def minutes(self) -> int:
        return max(1, round(self.words / WORDS_PER_MINUTE))


@dataclass
class Item:
    post: Post
    headline: str = ""
    why: list[str] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    ok: bool = False
    problem: str = ""


@dataclass
class Stats:
    found: int = 0
    read: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def opened(self) -> None:
        with self._lock:
            self.read += 1

    def skip(self, reason: str) -> None:
        with self._lock:
            self.skipped[reason] = self.skipped.get(reason, 0) + 1


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- Claude

class Claude:
    def __init__(self, model: str):
        self.client = anthropic.Anthropic()
        self.model = model

    def _create(self, **kwargs):
        response = self.client.beta.messages.create(
            model=self.model,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        return response

    def json(self, prompt: str, schema: dict, effort: str = "medium") -> dict:
        response = self._create(
            max_tokens=16000,
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def search_urls(self, prompt: str, max_searches: int = 8) -> list[str]:
        """Let Claude run web searches on substack.com; return every result URL it saw."""
        tool = {"type": "web_search_20260209", "name": "web_search",
                "max_uses": max_searches, "allowed_domains": ["substack.com"]}
        messages = [{"role": "user", "content": prompt}]
        urls: list[str] = []
        for _ in range(4):  # a long search turn can pause; resume it a few times
            response = self._create(max_tokens=16000, tools=[tool],
                                    output_config={"effort": "low"}, messages=messages)
            for block in response.content:
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    urls += [r.url for r in block.content if getattr(r, "url", None)]
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
        return urls


# --------------------------------------------------------------------------- step 1: find

def find_candidate_urls(claude: Claude, topics: list[str], start: date, end: date) -> list[str]:
    seen: dict[str, str] = {}
    for topic in topics:
        log(f"Searching Substack for: {topic}")
        prompt = (
            f"Search Substack for free newsletter posts about \"{topic}\" published "
            f"between {start:%B %d, %Y} and {end:%B %d, %Y}. Run several different "
            "searches (vary the wording, include the month and year) so you find a wide "
            "range of individual posts, not publication home pages. Reply with just 'done'."
        )
        try:
            urls = claude.search_urls(prompt)
        except anthropic.APIError as e:
            log(f"  search failed: {e}")
            continue
        for url in urls:
            key = canonical_post_key(url)
            if key:
                seen.setdefault(key, url)
    return list(seen)


def canonical_post_key(url: str) -> str | None:
    """'https://x.substack.com/p/slug?utm=..' -> 'x/slug'. Notes, home pages, etc. -> None."""
    m = POST_URL.match(url.strip())
    if not m or m.group(1).lower() in {"www", "substack", "on"}:
        return None
    return f"{m.group(1).lower()}/{m.group(2).lower()}"


# --------------------------------------------------------------------------- step 2: read

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "figure", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def judge_post(data: dict, since: datetime) -> str | None:
    """Return why a post should be skipped, or None if it is fine."""
    if data.get("audience", "everyone") != "everyone":
        return "paid or subscriber-only"
    kind = data.get("type", "newsletter")
    words = len(html_to_text(data.get("body_html") or "").split())
    if kind == "thread":
        return "thread"
    if kind == "podcast" and words < MIN_WORDS:
        return "podcast only"
    if kind not in {"newsletter", "podcast"}:
        return f"{kind} post"
    posted = parse_date(data.get("post_date"))
    if posted is None or posted < since:
        return "older than the time window"
    if words < MIN_WORDS:
        return "too short or text not public"
    return None


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_post(key: str, since: datetime, stats: Stats) -> Post | None:
    sub, slug = key.split("/", 1)
    try:
        r = requests.get(f"https://{sub}.substack.com/api/v1/posts/{slug}",
                         headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        stats.skip("could not be opened")
        return None
    stats.opened()
    reason = judge_post(data, since)
    if reason:
        stats.skip(reason)
        return None
    bylines = data.get("publishedBylines") or []
    pub = (bylines[0].get("publicationUsers") or [{}])[0].get("publication", {}) if bylines else {}
    return Post(
        url=data.get("canonical_url") or f"https://{sub}.substack.com/p/{slug}",
        subdomain=sub,
        title=data.get("title", "").strip(),
        subtitle=(data.get("subtitle") or "").strip(),
        author=", ".join(b.get("name", "") for b in bylines if b.get("name")) or sub,
        publication=pub.get("name") or sub,
        published=parse_date(data["post_date"]),
        text=html_to_text(data.get("body_html") or ""),
    )


# --------------------------------------------------------------------------- step 3: pick

PICK_SCHEMA = {
    "type": "object",
    "properties": {"ranked_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ranked_ids"],
    "additionalProperties": False,
}


def pick(claude: Claude, posts: list[Post], cfg: dict) -> list[Post]:
    listing = "\n\n".join(
        f"[{i}] {p.title} — {p.subtitle}\nBy {p.author} ({p.publication}), "
        f"{p.published:%b %d}, {p.minutes} min read\nOpening: {' '.join(p.text.split()[:250])}"
        for i, p in enumerate(posts)
    )
    prompt = (
        f"You are choosing Substack posts for {cfg['reader_name']}.\n"
        f"About the reader: {cfg['about_reader'].strip()}\n"
        f"Topics: {', '.join(cfg['topics'])}.\n\n"
        f"Rank the {cfg['candidates_to_check']} posts below that are most worth her time. "
        "Favour posts that teach something concrete and true, tell a good story, or explain an "
        "important business event. Skip promotional posts, link round-ups, and near-duplicates. "
        "Keep a mix across the topics where the quality allows. Return post ids, best first.\n\n"
        f"{listing}"
    )
    ids = claude.json(prompt, PICK_SCHEMA, effort="high")["ranked_ids"]
    chosen, used = [], set()
    for i in ids:
        if 0 <= i < len(posts) and i not in used:
            chosen.append(posts[i])
            used.add(i)
    return chosen[: cfg["candidates_to_check"]]


# --------------------------------------------------------------------------- step 4: write

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "why_line_1": {"type": "string"},
        "why_line_2": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["claim", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "why_line_1", "why_line_2", "claims"],
    "additionalProperties": False,
}


def write_item(claude: Claude, post: Post, cfg: dict) -> Item:
    prompt = (
        f"Write a briefing entry about this Substack post for {cfg['reader_name']}.\n"
        f"About the reader: {cfg['about_reader'].strip()}\n\n"
        "- headline: one line, at most 15 words, saying what the post reveals or argues.\n"
        "- why_line_1 and why_line_2: one sentence each on why it matters to her. Be specific "
        "to the post; no filler like 'a must-read'.\n"
        "- claims: list EVERY factual statement your headline and lines make about the post's "
        "content (names, numbers, dates, events, what the author argues). For each, copy a "
        "short supporting quote word-for-word from the post text below (one sentence or less). "
        "If you cannot quote support for something, leave it out of the headline and lines.\n\n"
        f"Title: {post.title}\nSubtitle: {post.subtitle}\nBy: {post.author}\n\n"
        f"Post text:\n{post.text}"
    )
    out = claude.json(prompt, WRITE_SCHEMA)
    return Item(post=post, headline=out["headline"].strip(),
                why=[out["why_line_1"].strip(), out["why_line_2"].strip()],
                claims=out["claims"])


# --------------------------------------------------------------------------- step 5: check

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "every_claim_supported": {"type": "boolean"},
        "problems": {"type": "string"},
    },
    "required": ["every_claim_supported", "problems"],
    "additionalProperties": False,
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-", "…": "..."}))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def quotes_found(item: Item) -> str | None:
    """Every claim must have a quote that really appears in the post. Returns a problem or None."""
    if not item.claims:
        return "no claims could be tied to the post"
    body = normalise(item.post.text)
    for c in item.claims:
        q = normalise(c.get("quote", ""))
        if len(q.split()) < 3 or q not in body:
            return f"quote not found in post: \"{c.get('quote', '')[:80]}\""
    return None


def check_item(claude: Claude, item: Item) -> Item:
    problem = quotes_found(item)
    if problem:
        item.problem = problem
        return item
    prompt = (
        "You are a strict fact-checker. Below is a Substack post and a short summary written "
        "about it. Decide whether EVERY factual statement in the summary is directly supported "
        "by the post text. Statements about why it matters to the reader can be opinion, but "
        "any fact inside them (names, numbers, dates, events, what the author says) must be in "
        "the post. If anything is unsupported, exaggerated or wrong, answer false and say what.\n\n"
        f"SUMMARY\nHeadline: {item.headline}\n{item.why[0]}\n{item.why[1]}\n\n"
        f"POST: {item.post.title}\n{item.post.text}"
    )
    verdict = claude.json(prompt, CHECK_SCHEMA, effort="high")
    item.ok = bool(verdict["every_claim_supported"])
    item.problem = "" if item.ok else (verdict["problems"] or "checker found an unsupported claim")
    return item


def write_and_check(claude: Claude, post: Post, cfg: dict) -> Item:
    try:
        return check_item(claude, write_item(claude, post, cfg))
    except Exception as e:  # any failure means we could not check it -> drop
        return Item(post=post, problem=f"could not be checked ({e})")


# --------------------------------------------------------------------------- output

def render(items: list[Item], dropped: list[Item], stats: Stats, start: date, end: date,
           reader: str) -> str:
    lines = [f"# Substack briefing for {reader}",
             f"_{start:%B %d} – {end:%B %d, %Y}_", ""]
    if not items:
        lines += ["Nothing passed the checks this time.", ""]
    for n, it in enumerate(items, 1):
        p = it.post
        lines += [
            f"**{n}. {it.headline}**",
            f"{it.why[0]}",
            f"{it.why[1]}",
            f"{p.url}  ({p.publication}, {p.minutes} min read)",
            "",
        ]
    if dropped:
        lines += ["<details><summary>Dropped items</summary>", ""]
        lines += [f"- {d.post.title} ({d.post.url}): {d.problem}" for d in dropped]
        lines += ["", "</details>", ""]
    lines.append(f"Read {stats.read} sources; dropped {len(dropped)} "
                 f"item{'s' if len(dropped) != 1 else ''} I could not check.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main

def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def run(cfg: dict) -> str:
    end = datetime.now(timezone.utc)
    since = end - timedelta(days=cfg["days_back"])
    claude = Claude(cfg["model"])
    stats = Stats()

    keys = find_candidate_urls(claude, cfg["topics"], since.date(), end.date())
    stats.found = len(keys)
    log(f"Found {len(keys)} Substack posts. Opening them...")

    with ThreadPoolExecutor(max_workers=8) as pool:
        posts = [p for p in pool.map(lambda k: read_post(k, since, stats), keys) if p]
    log(f"{len(posts)} are free, written posts from the last {cfg['days_back']} days.")
    for reason, n in sorted(stats.skipped.items()):
        log(f"  skipped {n}: {reason}")

    items: list[Item] = []
    dropped: list[Item] = []
    if posts:
        chosen = pick(claude, posts, cfg)
        log(f"Writing up and checking the top {len(chosen)}...")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda p: write_and_check(claude, p, cfg), chosen))
        for it in results:  # keep ranking order
            if not it.ok:
                dropped.append(it)
            elif len(items) < cfg["max_items"]:
                items.append(it)

    report = render(items, dropped, stats, since.date(), end.date(), cfg["reader_name"])
    out_dir = HERE / cfg.get("output_folder", "briefings")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{end:%Y-%m-%d}.md"
    out_file.write_text(report, encoding="utf-8")
    log(f"Saved to {out_file}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly Substack briefing.")
    parser.add_argument("--config", default=str(HERE / "config.toml"))
    args = parser.parse_args()
    print(run(load_config(Path(args.config))))


if __name__ == "__main__":
    main()
