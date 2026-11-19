"""
Brief Pipeline: topic in, sourced professional brief out, in three AI steps.

    [1/3] research(topic)        Claude searches the web and writes cited findings
    [2/3] summarize(research)    condenses them into exactly 5 key points, each tied to its sources
    [3/3] format_output(points)  turns the points into a finished brief (Markdown + Word)

Every step saves its output to runs/<run>/ before the next step starts. If step 2 or 3
fails, the log says which step, why, and with what input, and you can pick up where it
stopped instead of paying for the research again:

    python brief_pipeline.py "M-Pesa payment trends among Nairobi SMEs"
    python brief_pipeline.py "Jumia vs Kilimall e-commerce Kenya" --style campaign
    python brief_pipeline.py "Agricultural financing in Rift Valley" --style thread
    python brief_pipeline.py --batch topics.txt          # one brief per line, keeps going if one fails
    python brief_pipeline.py --resume runs/2026-11-19_1435_m-pesa-payment-trends
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5").strip()
EXTRA_ARGS = {"thinking": {"type": "disabled"}} if "haiku" in MODEL else {}
PRICE = {"input": 0.10, "output": 0.50, "search": 0.01}   # USD: per MTok (Haiku 5.5, <100K prompt), per search
MAX_SEARCHES = int(os.getenv("MAX_SEARCHES", "5"))
SEARCH_COUNTRY = os.getenv("SEARCH_COUNTRY", "").strip().upper()   # optional 2-letter code, e.g. US, GB
RETRIES = 3                  # attempts per API call for temporary errors (rate limit, overload, network)
RUNS_DIR = HERE / "runs"
BRIEFS_DIR = HERE / "briefs"

log = logging.getLogger("pipeline")


class StepError(Exception):
    """A step failed. Carries which step and a preview of its input, for the log."""

    def __init__(self, step, reason, input_preview=""):
        super().__init__(f"step {step} failed: {reason}")
        self.step, self.reason, self.input_preview = step, reason, input_preview


class FatalError(Exception):
    """Retrying won't help: missing or rejected API key, bad request."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0

    def add(self, usage):
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        tool = getattr(usage, "server_tool_use", None)
        self.searches += (getattr(tool, "web_search_requests", 0) or 0) if tool else 0

    @property
    def cost(self):
        return (self.input_tokens * PRICE["input"] + self.output_tokens * PRICE["output"]) / 1e6 \
            + self.searches * PRICE["search"]


@dataclass
class Run:
    topic: str
    style: str
    folder: Path
    usage: dict = field(default_factory=dict)   # step name -> Usage

    def total(self):
        t = Usage()
        for u in self.usage.values():
            t.input_tokens += u.input_tokens
            t.output_tokens += u.output_tokens
            t.searches += u.searches
        return t


# ------------------------------------------------------------------ calling Claude

_client = None


def get_client():
    global _client
    if _client is None:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise FatalError("ANTHROPIC_API_KEY is missing. Copy .env.example to .env and paste your key.")
        _client = anthropic.Anthropic(api_key=key, max_retries=0)   # we do our own retries, with logging
    return _client


STEP_NO = {"research": 1, "summarize": 2, "format": 3}


def temporary_errors():
    import anthropic
    names = ("RateLimitError", "APIConnectionError", "InternalServerError", "OverloadedError")
    return tuple(getattr(anthropic, n) for n in names if hasattr(anthropic, n))


def call_claude(step, usage, **request):
    """One API call with retries for temporary errors. Handles web search 'pause_turn'."""
    import anthropic
    TEMPORARY_ERRORS = temporary_errors()
    messages = list(request.pop("messages"))
    for attempt in range(1, RETRIES + 1):
        try:
            blocks = []
            for _ in range(4):   # a long search turn can pause; send it back to continue
                response = get_client().messages.create(model=MODEL, messages=messages, **request, **EXTRA_ARGS)
                usage.add(response.usage)
                blocks += list(response.content)
                if response.stop_reason != "pause_turn":
                    break
                messages = messages + [{"role": "assistant", "content": response.content}]
            if response.stop_reason == "max_tokens":
                log.warning(f"{step}: answer hit max_tokens and may be cut short")
            return blocks
        except anthropic.AuthenticationError:
            raise FatalError("The API key was rejected. Check ANTHROPIC_API_KEY in .env.")
        except anthropic.BadRequestError as err:
            body = getattr(err, "body", None)
            reason = body.get("error", {}).get("message") if isinstance(body, dict) else None
            raise FatalError(f"Claude rejected the request: {reason or getattr(err, 'message', err)}")
        except TEMPORARY_ERRORS as err:
            wait = 2 ** attempt * 2
            if attempt == RETRIES:
                raise StepError(STEP_NO[step], f"{type(err).__name__} after {RETRIES} attempts")
            log.warning(f"{step}: {type(err).__name__}, retrying in {wait}s (attempt {attempt}/{RETRIES})")
            time.sleep(wait)
        except anthropic.APIStatusError as err:
            if err.status_code == 529 and attempt < RETRIES:          # overloaded
                log.warning(f"{step}: API overloaded, retrying in {2 ** attempt * 2}s")
                time.sleep(2 ** attempt * 2)
                continue
            raise StepError(STEP_NO[step], f"API error {err.status_code}")


def text_of(blocks):
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text").strip()


def preview(text, n=200):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[:n] + "..."


# ------------------------------------------------------------------ step 1: research

RESEARCH_PROMPT = """Today is {today}. Research this topic for a Kenyan business audience:

{topic}

Search the web for recent, specific facts: figures, dates, named companies, regulators and
programmes, prices, market shares, and what has changed in the last year or two. Prefer Kenyan
sources (CBK, KNBS, Business Daily, The Star, Nation, company reports) and international ones
such as GSMA or the World Bank.

Write your findings as plain paragraphs. Every fact must come from a search result. Say when a
figure is old or sources disagree. Don't pad: if something can't be found, say so."""


def research(topic, usage, search=True):
    """Step 1: web research with citations. Returns {'text': ..., 'sources': [{'url', 'title'}]}."""
    request = {"max_tokens": 3000,
               "messages": [{"role": "user", "content": RESEARCH_PROMPT.format(
                   today=f"{datetime.now():%d %B %Y}", topic=topic)}]}
    if search:
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}
        if SEARCH_COUNTRY:   # not every country is supported (KE isn't, as of Nov 2026); the prompt steers to Kenya
            tool["user_location"] = {"type": "approximate", "country": SEARCH_COUNTRY}
        request["tools"] = [tool]
    blocks = call_claude("research", usage, **request)

    sources, seen, failed = [], set(), []
    for b in blocks:
        btype = getattr(b, "type", "")
        if btype == "web_search_tool_result" and not isinstance(getattr(b, "content", []), list):
            failed.append(getattr(b.content, "error_code", "unknown"))
        for c in (getattr(b, "citations", None) or []) if btype == "text" else []:
            url = getattr(c, "url", "")
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "title": getattr(c, "title", "") or url})

    text = text_of(blocks)
    if failed:
        log.warning(f"research: {len(failed)} search(es) failed: {', '.join(failed)}")
    if len(text) < 300:
        raise StepError(1, f"research came back too short ({len(text)} characters)", topic)
    if search and not sources:
        raise StepError(1, "no cited web sources came back, so the facts can't be checked "
                           "(use --no-search to accept unsourced research)", topic)
    return {"text": text, "sources": sources}


# ------------------------------------------------------------------ step 2: summarize

SUMMARY_PROMPT = """Below is research on "{topic}", followed by its numbered sources.

Condense it into EXACTLY 5 key points for a business decision-maker. Each point:
- one or two sentences, with the most useful specific figure or name in it
- lists the numbers of the sources that support it

Reply with JSON only, no other text:
{{"points": [{{"point": "...", "sources": [1, 3]}}, ...]}}

RESEARCH:
{research}

SOURCES:
{sources}"""


def numbered(sources):
    return "\n".join(f"[{i}] {s['title']} - {s['url']}" for i, s in enumerate(sources, start=1)) or "(none)"


def parse_points(raw, n_sources):
    """Validate step 2's JSON. Returns the points or raises ValueError explaining what's wrong."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("no JSON object in the reply")
    data = json.loads(match.group(0))
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list) or len(points) != 5:
        raise ValueError(f"expected exactly 5 points, got {len(points) if isinstance(points, list) else 'none'}")
    clean = []
    for p in points:
        if not isinstance(p, dict) or not str(p.get("point", "")).strip():
            raise ValueError("a point is empty")
        refs = [r for r in p.get("sources", []) if isinstance(r, int) and 1 <= r <= n_sources]
        clean.append({"point": p["point"].strip(), "sources": sorted(set(refs))})
    return clean


def summarize(topic, findings, usage):
    """Step 2: exactly 5 key points as JSON. One retry, telling Claude what was wrong."""
    messages = [{"role": "user", "content": SUMMARY_PROMPT.format(
        topic=topic, research=findings["text"], sources=numbered(findings["sources"]))}]
    for attempt in (1, 2):
        raw = text_of(call_claude("summarize", usage, max_tokens=1200, messages=messages))
        try:
            return parse_points(raw, len(findings["sources"]))
        except (ValueError, json.JSONDecodeError) as err:
            log.warning(f"summarize: attempt {attempt} unusable ({err})")
            if attempt == 2:
                raise StepError(2, f"summary wasn't valid after 2 tries ({err})", findings["text"])
            messages += [{"role": "assistant", "content": raw or "(empty)"},
                         {"role": "user", "content": f"That didn't work: {err}. Reply with only the JSON, "
                                                     f"exactly 5 points."}]


# ------------------------------------------------------------------ step 3: format

STYLES = {
    "brief": ("Business brief",
              "Write a professional two-paragraph business brief. Paragraph 1: the situation and the key "
              "numbers. Paragraph 2: what it means for a Kenyan SME and what to do next."),
    "campaign": ("Campaign brief",
                 "Write a two-paragraph marketing campaign brief for a Nairobi agency. Paragraph 1: the "
                 "market insight and audience. Paragraph 2: the angle, key messages and suggested channels."),
    "thread": ("X thread",
               "Write an X (Twitter) thread of 5 to 7 posts, each under 270 characters, numbered 1/, 2/ ... "
               "Hook first, one fact per post, last post a takeaway. No hashtag spam (two at most)."),
}

FORMAT_PROMPT = """Topic: {topic}

{instruction}

Use ONLY these key points. Keep the figures exactly as written. Put the source numbers in
square brackets after the facts they support, like [2] or [1][4]. Plain, confident language
with no filler. Return only the {name}, without a heading.

KEY POINTS:
{points}"""


def format_output(topic, points, style, usage):
    """Step 3: the finished brief, checked for shape and for made-up source numbers."""
    name, instruction = STYLES[style]
    listed = "\n".join(f"- {p['point']} {''.join(f'[{s}]' for s in p['sources'])}" for p in points)
    allowed = {s for p in points for s in p["sources"]}
    raw = text_of(call_claude("format", usage, max_tokens=1200, messages=[{"role": "user", "content":
                  FORMAT_PROMPT.format(topic=topic, instruction=instruction, name=name.lower(), points=listed)}]))
    if len(raw) < 200:
        raise StepError(3, f"the {name.lower()} came back too short ({len(raw)} characters)", listed)
    if style != "thread" and len([p for p in raw.split("\n\n") if p.strip()]) < 2:
        log.warning("format: expected two paragraphs, got one")
    invented = {int(n) for n in re.findall(r"\[(\d+)\]", raw)} - allowed
    if invented:
        log.warning(f"format: removed citation(s) {sorted(invented)} that no key point uses")
        raw = re.sub(r"\[(\d+)\]", lambda m: "" if int(m.group(1)) in invented else m.group(0), raw)
    return raw.strip()


# ------------------------------------------------------------------ saving

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "brief"


def save(folder, name, data):
    path = folder / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) if not isinstance(data, str) else data,
                    encoding="utf-8")
    return path


def load(folder, name):
    path = folder / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")) if name.endswith(".json") else path.read_text(encoding="utf-8")


def write_brief_files(run, findings, points, body):
    name, _ = STYLES[run.style]
    BRIEFS_DIR.mkdir(exist_ok=True)
    stem = BRIEFS_DIR / f"{run.folder.name}"
    sources = findings["sources"]
    total = run.total()
    md = [f"# {name}: {run.topic}", "", f"*{datetime.now():%d %B %Y} | {len(sources)} sources | "
          f"{total.searches} web searches | cost ${total.cost:.4f}*", "", body, "", "## Key points", ""]
    md += [f"- {p['point']} {''.join(f'[{s}]' for s in p['sources'])}" for p in points]
    md += ["", "## Sources", ""] + [f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(sources, 1)]
    md_path = stem.with_suffix(".md")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    import docx
    from docx.shared import Pt, RGBColor
    d = docx.Document()
    d.add_heading(f"{name}: {run.topic}", 0)
    meta = d.add_paragraph().add_run(f"{datetime.now():%d %B %Y}  |  {len(sources)} sources")
    meta.italic = True
    for para in [p for p in body.split("\n\n") if p.strip()]:
        d.add_paragraph(para.strip())
    d.add_heading("Key points", level=1)
    for p in points:
        d.add_paragraph(f"{p['point']} {''.join(f'[{s}]' for s in p['sources'])}", style="List Bullet")
    d.add_heading("Sources", level=1)
    for i, s in enumerate(sources, 1):
        run_ = d.add_paragraph().add_run(f"[{i}] {s['title']}: {s['url']}")
        run_.font.size = Pt(9)
        run_.font.color.rgb = RGBColor(0x37, 0x41, 0x51)
    docx_path = stem.with_suffix(".docx")
    d.save(docx_path)
    return md_path, docx_path


# ------------------------------------------------------------------ the pipeline

def run_pipeline(topic=None, style="brief", search=True, resume=None, stop_on_fatal=False):
    """Run (or resume) the three steps. Returns the Markdown path, or None if a step failed."""
    if resume:
        folder = Path(resume)
        meta = load(folder, "run.json")
        if not meta:
            print(f"Can't resume: {folder} has no run.json")
            return None
        topic, style, search = meta["topic"], meta["style"], meta["search"]
    else:
        base = RUNS_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}_{slugify(topic)}"
        folder, n = base, 1
        while folder.exists():                  # same topic twice in one second (batch duplicates)
            n += 1
            folder = base.with_name(f"{base.name}-{n}")
        folder.mkdir(parents=True)
        save(folder, "run.json", {"topic": topic, "style": style, "search": search})
    run = Run(topic, style, folder)
    log.info(f"Pipeline started: '{topic}' (style={style}, run={folder.name})")

    step = 1
    try:
        findings = load(folder, "1_research.json")
        if findings:
            print(f"[1/3] Research: reusing saved results ({len(findings['sources'])} sources)")
        else:
            print(f"[1/3] Researching: {topic}")
            run.usage["research"] = u = Usage()
            findings = research(topic, u, search)
            save(folder, "1_research.json", findings)
            print(f"      {len(findings['sources'])} sources, {u.searches} searches, ${u.cost:.4f}")
            log.info(f"step 1 OK: {len(findings['sources'])} sources, {u.searches} searches, {len(findings['text'])} chars")

        step = 2
        points = load(folder, "2_summary.json")
        if points:
            print("[2/3] Summary: reusing saved key points")
        else:
            print("[2/3] Summarizing into 5 key points...")
            run.usage["summarize"] = u = Usage()
            points = summarize(topic, findings, u)
            save(folder, "2_summary.json", points)
            log.info(f"step 2 OK: {len(points)} points")

        step = 3
        print(f"[3/3] Formatting the {STYLES[style][0].lower()}...")
        run.usage["format"] = u = Usage()
        body = format_output(topic, points, style, u)
        save(folder, "3_output.md", body)
        md_path, docx_path = write_brief_files(run, findings, points, body)
        total = run.total()
        log.info(f"Pipeline completed OK: {md_path.name} | {total.input_tokens} in / {total.output_tokens} out "
                 f"tokens, {total.searches} searches, ${total.cost:.4f}")
        print(f"\n{body}\n")
        print(f"Saved {md_path.relative_to(HERE)} and {docx_path.name} | this run: ${total.cost:.4f}")
        return md_path

    except StepError as err:
        log.error(f"Pipeline failed at step {err.step} for topic='{topic}': {err.reason} | "
                  f"input: {preview(err.input_preview)}")
        print(f"\nFAILED at step {err.step}/3: {err.reason}")
    except FatalError as err:
        log.error(f"Pipeline stopped at step {step} for topic='{topic}': {err}")
        print(f"\nSTOPPED: {err}")
        if stop_on_fatal:
            if not (folder / "1_research.json").exists() and not resume:
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
            raise
    except Exception as err:   # anything unexpected: record it with a traceback, never lose the run
        log.exception(f"Pipeline crashed at step {step} for topic='{topic}': {type(err).__name__}: {err}")
        print(f"\nFAILED at step {step}/3: unexpected {type(err).__name__}: {err}")
    if (folder / "1_research.json").exists():
        print(f"Steps before the failure are saved, so they won't be paid for again. Fix the problem, then run:\n"
              f"  python brief_pipeline.py --resume {folder.relative_to(HERE).as_posix()}")
    elif not resume:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)      # nothing worth keeping
    return None


def run_batch(path, style, search):
    topics = [t.strip() for t in Path(path).read_text(encoding="utf-8").splitlines()
              if t.strip() and not t.startswith("#")]
    results = []
    for n, topic in enumerate(topics, start=1):
        print(f"\n{'=' * 70}\nTopic {n}/{len(topics)}: {topic}\n{'=' * 70}")
        try:
            results.append((topic, run_pipeline(topic, style, search, stop_on_fatal=True)))
        except FatalError:
            # A bad key or a rejected request will fail every topic the same way: don't repeat it.
            log.error(f"Batch stopped after topic {n}/{len(topics)}: the same error would hit every topic")
            print("\nBatch stopped: this error would repeat for every topic. Fix it and run the batch again.")
            results.append((topic, None))
            results += [(t, None) for t in topics[n:]]
            break
    print(f"\n{'=' * 70}\nBatch done: {sum(1 for _, r in results if r)}/{len(results)} briefs")
    for topic, result in results:
        print(f"  {'OK    ' if result else 'FAILED'} {topic}" + (f"  ->  {result.name}" if result else ""))
    return results


def setup_logging():
    logging.basicConfig(filename=HERE / "automation.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s | %(levelname)s | %(message)s")
    for noisy in ("httpx", "httpx2", "anthropic"):     # keep the log about OUR pipeline
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    p = argparse.ArgumentParser(description="Research a topic and write a sourced brief in 3 AI steps.")
    p.add_argument("topic", nargs="?", help="what to research, in quotes")
    p.add_argument("--style", choices=STYLES, default="brief", help="brief (default), campaign or thread")
    p.add_argument("--batch", metavar="FILE", help="text file with one topic per line")
    p.add_argument("--resume", metavar="RUN_FOLDER", help="continue a failed run from its last good step")
    p.add_argument("--no-search", action="store_true",
                   help="research from the model's own knowledge (cheaper, but unsourced and may be outdated)")
    args = p.parse_args()
    setup_logging()

    if args.batch:
        results = run_batch(args.batch, args.style, not args.no_search)
        sys.exit(0 if all(r for _, r in results) else 1)
    if not args.topic and not args.resume:
        p.error("give a topic in quotes, --batch FILE, or --resume RUN_FOLDER")
    ok = run_pipeline(args.topic, args.style, not args.no_search, args.resume)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
