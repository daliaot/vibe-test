# Substack agent

A small program that reads Substack for you once a week and gives you a short briefing:
the ten posts most worth your time on **historical facts and stories**, **self help**,
and **key business stories**.

Each item has:

- a one-line headline,
- two lines on why it matters to you,
- the link (with the newsletter's name and how long the post takes to read).

The briefing ends with one line saying how many sources were read and how many items
were dropped.

## What it does, step by step

1. **Searches Substack.** For each topic, Claude runs several web searches limited to
   substack.com and looks for posts from the last 7 days.
2. **Opens every post it found.** It reads each post's details from Substack and skips
   any post that is:
   - paid or for subscribers only,
   - a thread (a Substack Note) rather than a written post,
   - a podcast with no written post,
   - older than a week,
   - too short to be a real article (usually a paywall teaser).
3. **Picks the best.** Claude ranks the remaining posts for you: concrete, true and
   interesting comes first, while ads, link lists and repeats are pushed out. It tries
   to mix the three topics.
4. **Writes each item.** For the top 16 posts, Claude writes the headline and the two
   "why it matters" lines. For every fact it uses, it also copies a supporting quote
   from the post.
5. **Checks every claim.** Each item has to pass two checks:
   - The program checks that every quote really appears, word for word, in the post.
     (Capital letters and punctuation are ignored.)
   - A separate Claude call acts as a strict fact-checker. It reads the whole post and
     confirms that every fact in the item is supported by it.

   If either check fails, or the post can't be checked for any reason, the item is
   **dropped**. The first ten items that pass make up the briefing. The dropped items
   are listed at the bottom with the reason each one failed.
6. **Saves the briefing** to `briefings/YYYY-MM-DD.md` and prints it on screen.

## Setting it up (once)

You need Python 3.11 or newer and an Anthropic API key from
https://console.anthropic.com.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here     # on Windows: set ANTHROPIC_API_KEY=your-key-here
```

## Running it

```bash
python substack_agent.py
```

It takes a few minutes. Progress messages appear as it works, and the briefing prints at the end.

To run it automatically every week, use your computer's scheduler. On Mac or Linux,
`crontab -e` and add a line like this one (runs on Sundays at 8:00):

```
0 8 * * 0 cd /path/to/this/folder && /usr/bin/python3 substack_agent.py
```

## Changing what it looks for

Open `config.toml` in any text editor. You can change:

| Setting | What it does |
|---|---|
| `about_reader` | A few sentences about you. This shapes the "why it matters" lines. |
| `topics` | The subjects to search for. Add or remove lines. |
| `days_back` | How far back to look (7 = one week). |
| `max_items` | How many items go in the briefing. |
| `candidates_to_check` | How many posts get written up and checked. Raise this if too many items are being dropped. |
| `model` | Which Claude model does the work. |

## What the last line means

> Read 41 sources; dropped 3 items I could not check.

- **Sources read** is the number of Substack posts the program actually opened.
- **Items dropped** is the number of written-up items that failed the fact-check.
  Posts skipped for being paid, podcasts, threads or too old are not counted here.
  The progress messages show how many of those were skipped and why.

## Good to know

- **Only `name.substack.com` addresses are found.** Some newsletters run on their own
  web address, like `www.example.com`, and the search won't pick those up.
- **Some weeks you may get fewer than ten items.** That happens when not enough posts
  pass every check. The program never fills the gap with anything it couldn't check.
- **The checker confirms the item matches the post.** It doesn't confirm that the post
  itself is true. If a writer gets a historical fact wrong, the item will repeat it
  faithfully. The link is always there so you can read the original.
- **Cost:** each run makes about 40 Claude calls and some web searches, so expect a
  modest charge on your Anthropic account. If a request is declined, the program lets
  Claude retry on a fallback model automatically.
- **It relies on Substack's public post pages.** If Substack changes how those work,
  the reading step may need a small fix.

## Testing the program without a key

```bash
pip install pytest
python -m pytest -q
```

These tests use fake search results and fake posts, so they need no internet connection
and no API key. They check the filters, the quote check, and the format of the briefing.

## Files

- `substack_agent.py`: the program
- `config.toml`: your settings
- `test_substack_agent.py`: the offline tests
- `briefings/`: where each week's briefing is saved
