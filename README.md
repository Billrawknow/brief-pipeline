# Brief Pipeline

You give it a topic and it returns a **sourced, professional brief**, built in three AI steps where each step's output feeds the next:

```
"M-Pesa payment trends among Nairobi SMEs"
        |
[1/3] research()       Claude searches the web (up to 5 searches; the prompt steers it to Kenyan
        |              sources) and writes findings. Every source URL is kept.
        v              -> runs/<run>/1_research.json
[2/3] summarize()      Exactly 5 key points as JSON, each tied to its source numbers.
        |              The JSON is checked, and retried once if it's malformed.
        v              -> runs/<run>/2_summary.json
[3/3] format_output()  A two-paragraph brief, campaign brief or X thread, with [n] citations.
        |              Any citation to a source that doesn't exist is stripped.
        v
briefs/<run>.md  +  briefs/<run>.docx   (brief, key points, numbered source links)
```

Built for AI Masterclass 2, Week 6: Multi-Step AI Workflows.

## Why it's built this way

- **Research is real, not remembered.** The lesson's version asks the model to "research", which means it recalls training data that may be out of date and gives no sources. Here, step 1 uses Claude's web search tool, so every fact in the brief can be traced to a URL. If no cited sources come back, step 1 fails rather than passing unsourced claims down the chain. (`--no-search` turns this off deliberately.)
- **One function, one job.** Step 3 only sees the 5 key points, not the raw research. That keeps the brief focused and makes each step easy to debug or swap out.
- **Every step is checked before the next one runs:**
  - Research that's too short or has no sources fails at step 1.
  - A summary that isn't valid JSON or doesn't have exactly 5 points gets one retry, with Claude told what was wrong.
  - A brief that's too short fails, and citations to sources that don't exist are removed.
- **Failures say where, why and with what input,** in `automation.log`:
  ```
  ERROR | Pipeline failed at step 2 for topic='M-Pesa payment trends': summary wasn't valid after 2 tries
          (expected exactly 5 points, got 4) | input: M-Pesa processed KES 38.3 trillion in the year to ...
  ```
- **Resume instead of paying twice.** Each step saves its output before the next starts. If step 2 or 3 fails, run `--resume runs/<run>` and the research is reused.
- **A real-run lesson:** the first live batch failed with `Country code KE is not supported`, because web search's location setting doesn't accept Kenya. Now no location is sent by default, and the prompt asks for Kenyan sources (CBK, KNBS, Business Daily). The batch also stops at the first error that would repeat, instead of failing every topic the same way.
- **Temporary errors are retried:** rate limits, API overload and network drops get 3 attempts with backoff. A wrong API key stops at once, because retrying won't fix it.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # paste your ANTHROPIC_API_KEY
```

```bash
python brief_pipeline.py "M-Pesa payment trends among Nairobi SMEs"
python brief_pipeline.py "Jumia vs Kilimall e-commerce Kenya" --style campaign    # agency campaign brief
python brief_pipeline.py "Agricultural financing in Rift Valley" --style thread   # X thread, 5-7 posts
python brief_pipeline.py --batch topics.txt          # the lesson's 3 test topics; keeps going if one fails
python brief_pipeline.py --resume runs/2026-11-19_143725_m-pesa-payment-trends-among-nairobi-smes
```

**Cost:** a brief costs about **$0.04–0.06 (KES 5–8)**. Most of that is the web searches ($10 per 1,000 on the Claude API, up to 5 per brief) plus the tokens from reading the search results. Steps 2 and 3 cost a fraction of a cent. The exact cost is printed and written into each brief.

## Lesson test topics

Run on 19 Nov 2026 with `python brief_pipeline.py --batch topics.txt`. The batch result was 3/3 briefs, for **$0.134 in total**.

| Test | Topic | Sources / searches | Completed all 3 steps? | Coherent and usable? | Errors? | Cost |
|---|---|---|---|---|---|---|
| 1 | M-Pesa payment trends among Nairobi SMEs | 14 / 4 | Yes | Yes. FY2026 volumes, the Aug 2026 Lipa na M-Pesa tariff change and CBK cost targets, with concrete next steps. It notes where two sources disagree on a figure | None | $0.0491 |
| 2 | Agricultural financing options for smallholder farmers in Rift Valley | 17 / 3 | Yes | Yes. Credit sources by share, falling bank rates, the new AFC collateral-free loans and the pending AFC merger bill. It flags that AFC's rate is reported as both 8% and 10% | None | $0.0355 |
| 3 | Competitor analysis: Jumia vs Kilimall e-commerce Kenya | 20 / 4 | Yes | Yes. Jumia's Q2/Q3 2026 Kenya numbers and upcountry share. It says plainly that Kilimall has no reliable 2026 data, instead of inventing a comparison | None | $0.0497 |

**What the outputs show:**
- **The briefs say what they don't know.** Brief 3 refuses to invent Kilimall numbers. Brief 1 says no survey isolates Nairobi SMEs. That comes from step 1's instruction to note old or conflicting figures, which then carries through steps 2 and 3.
- **Every figure has a numbered source** that links to the page it came from (see `briefs/*.md`). Facts gathered by AI web search should still be spot-checked against those links before a client relies on them.

## Tests

```bash
python -m unittest discover tests -v     # 21 tests, no API key or internet needed
```

A fake Claude client replays scripted responses, including web search results with citations, so every path is tested:
- **Happy path:**
  - The 3 steps produce a sourced brief, in all three styles.
  - Each step feeds the next, and step 3 never sees the raw research.
  - Search sends no location by default, and `SEARCH_COUNTRY` can set one.
  - Duplicate sources are kept once, and invented citations are removed.
  - Cost includes searches.
- **Failures between steps:**
  - Bad JSON gets one retry.
  - A step 2 failure is logged with its input.
  - `--resume` skips completed steps.
  - Rate limits are retried, and a network outage fails cleanly after 3 tries.
  - A bad key stops without retrying.
  - Research with no sources is rejected.
  - Failed searches are logged, and a paused search turn is continued.
- **Batch:** keeps going when one topic fails, but stops at once on an error that would hit every topic (bad key, rejected request).

## Files

| File | Purpose |
|---|---|
| `brief_pipeline.py` | The three steps, the pipeline, resume, batch and the Markdown/Word output |
| `topics.txt` | The lesson's three test topics, for `--batch` |
| `tests/test_pipeline.py` | 21 tests |
| `briefs/` | Finished briefs (`.md` is committed as examples, `.docx` is ignored) |
| `runs/` | Per-run step outputs for resume (git-ignored) |

## Stack

Python, the Anthropic SDK (Claude Haiku 5.5 with the web search tool), python-docx, python-dotenv, logging and unittest.
