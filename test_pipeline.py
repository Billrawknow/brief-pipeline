"""
Tests for brief_pipeline.py. No API key or internet needed: a fake client replays
scripted Claude responses (web search results, citations, bad JSON, rate limits...).

    python -m unittest discover tests -v
"""

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import anthropic

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import brief_pipeline as bp  # noqa: E402

logging.getLogger("pipeline").propagate = False      # keep tests out of the real automation.log
logging.getLogger("pipeline").addHandler(logging.NullHandler())

RESEARCH_TEXT = ("M-Pesa processed KES 38.3 trillion in the year to March 2025, according to Safaricom's annual "
                 "report. Lipa na M-Pesa merchants passed 800,000. The CBK's 2024 FinAccess survey found 52% of "
                 "MSMEs take payments by mobile money as their main channel. Transaction charges for merchants "
                 "were cut in 2023. Fuliza disbursed over KES 800 billion in the year. ") * 2


def response(*content, stop="end_turn", searches=0, inp=1000, out=300):
    return NS(content=list(content), stop_reason=stop,
              usage=NS(input_tokens=inp, output_tokens=out, server_tool_use=NS(web_search_requests=searches)))


def text(t, cites=()):
    return NS(type="text", text=t, citations=[NS(type="web_search_result_location", url=u, title=title,
                                                 cited_text="...") for u, title in cites])


def search_result(ok=True):
    content = [NS(type="web_search_result", url="https://x.example", title="x")] if ok else \
        NS(type="web_search_tool_result_error", error_code="too_many_requests")
    return NS(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=content)


GOOD_RESEARCH = response(
    NS(type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "m-pesa sme kenya"}),
    search_result(),
    text(RESEARCH_TEXT[:400], [("https://safaricom.example/ar2025", "Safaricom Annual Report 2025")]),
    text(RESEARCH_TEXT[400:], [("https://cbk.example/finaccess", "CBK FinAccess 2024"),
                               ("https://safaricom.example/ar2025", "Safaricom Annual Report 2025")]),
    searches=3, inp=9000, out=700)

POINTS = {"points": [
    {"point": "M-Pesa moved KES 38.3 trillion in the year to March 2025.", "sources": [1]},
    {"point": "Lipa na M-Pesa has over 800,000 merchants.", "sources": [1]},
    {"point": "52% of MSMEs use mobile money as their main payment channel.", "sources": [2]},
    {"point": "Merchant charges were cut in 2023.", "sources": [1, 9]},     # 9 doesn't exist: dropped
    {"point": "Fuliza lent over KES 800 billion in a year.", "sources": [1]},
]}
GOOD_SUMMARY = response(text("Here you go:\n" + json.dumps(POINTS)))
BRIEF = ("M-Pesa is now the default way Nairobi SMEs get paid: KES 38.3 trillion moved in a year [1] and "
         "52% of MSMEs use mobile money as their main channel [2].\n\n"
         "For a small business, that means a Lipa na M-Pesa till is no longer optional [1]. Cheaper merchant "
         "charges since 2023 make it the lowest-cost channel too [1][7].")
GOOD_FORMAT = response(text(BRIEF))


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = NS(create=self.create)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def api_error(cls, status):
    """Build an SDK error without a real HTTP response (works across anthropic SDK versions)."""
    err = cls.__new__(cls)
    Exception.__init__(err, f"error {status}")
    err.message, err.status_code, err.body = f"error {status}", status, None
    return err


def connection_error():
    return api_error(anthropic.APIConnectionError, 0)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for name, value in {"RUNS_DIR": tmp / "runs", "BRIEFS_DIR": tmp / "briefs", "HERE": tmp}.items():
            patcher = mock.patch.object(bp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch("time.sleep"), mock.patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tmp

    def use(self, *replies):
        client = FakeClient(*replies)
        patcher = mock.patch.object(bp, "_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def run_logged(self, *args, **kwargs):
        with self.assertLogs("pipeline", level="INFO") as logs:
            result = bp.run_pipeline(*args, **kwargs)
        return result, logs.output


class TestHappyPath(PipelineTest):
    def test_three_steps_produce_a_sourced_brief(self):
        client = self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        md, logs = self.run_logged("M-Pesa payment trends among Nairobi SMEs")
        self.assertTrue(md and md.exists())
        self.assertEqual(len(client.requests), 3)
        text_ = md.read_text(encoding="utf-8")
        self.assertIn("# Business brief: M-Pesa payment trends", text_)
        self.assertIn("1. [Safaricom Annual Report 2025](https://safaricom.example/ar2025)", text_)
        self.assertIn("2. [CBK FinAccess 2024](https://cbk.example/finaccess)", text_)
        self.assertEqual(text_.count("safaricom.example/ar2025"), 1)        # duplicate citation kept once
        self.assertTrue(md.with_suffix(".docx").exists())
        self.assertIn("Pipeline completed OK", logs[-1])

    def test_each_step_feeds_the_next(self):
        client = self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        bp.run_pipeline("M-Pesa payment trends among Nairobi SMEs")
        step2_prompt = client.requests[1]["messages"][0]["content"]
        step3_prompt = client.requests[2]["messages"][0]["content"]
        self.assertIn("KES 38.3 trillion", step2_prompt)                    # research -> summarize
        self.assertIn("[2] CBK FinAccess 2024", step2_prompt)
        self.assertIn("52% of MSMEs use mobile money", step3_prompt)       # summary -> format
        self.assertNotIn("Fuliza disbursed", step3_prompt)                 # raw research is NOT sent to step 3

    def test_research_uses_web_search_without_a_location_by_default(self):
        # Real run, 19 Nov 2026: user_location country "KE" -> 400 "Country code KE is not supported".
        client = self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        bp.run_pipeline("topic")
        tool = client.requests[0]["tools"][0]
        self.assertEqual(tool["type"], "web_search_20250305")
        self.assertNotIn("user_location", tool)
        self.assertEqual(tool["max_uses"], bp.MAX_SEARCHES)
        self.assertIn("Prefer Kenyan", client.requests[0]["messages"][0]["content"])

    def test_search_country_can_be_set(self):
        client = self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        with mock.patch.object(bp, "SEARCH_COUNTRY", "GB"):
            bp.run_pipeline("topic")
        self.assertEqual(client.requests[0]["tools"][0]["user_location"], {"type": "approximate", "country": "GB"})

    def test_invented_citations_are_removed(self):
        self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        md, logs = self.run_logged("topic")
        self.assertNotIn("[7]", md.read_text(encoding="utf-8"))            # format step cited a source that doesn't exist
        self.assertTrue(any("removed citation(s) [7]" in line for line in logs))
        points = json.loads(next(self.tmp.glob("runs/*/2_summary.json")).read_text())
        self.assertEqual(points[3]["sources"], [1])                         # [1, 9] -> [1]

    def test_cost_counts_tokens_and_searches(self):
        self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        md = bp.run_pipeline("topic")
        tokens_in, tokens_out = 9000 + 1000 + 1000, 700 + 300 + 300
        expected = (tokens_in * 0.10 + tokens_out * 0.50) / 1e6 + 3 * 0.01
        self.assertIn(f"${expected:.4f}", md.read_text(encoding="utf-8"))

    def test_every_style_works(self):
        for style in bp.STYLES:
            self.use(GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
            self.assertTrue(bp.run_pipeline("topic", style=style), style)


class TestFailuresBetweenSteps(PipelineTest):
    def test_bad_json_is_retried_once_then_succeeds(self):
        client = self.use(GOOD_RESEARCH, response(text("Sure! Five points: ...")), GOOD_SUMMARY, GOOD_FORMAT)
        md, logs = self.run_logged("topic")
        self.assertTrue(md)
        self.assertIn("That didn't work", client.requests[2]["messages"][-1]["content"])
        self.assertTrue(any("attempt 1 unusable" in line for line in logs))

    def test_step_2_failure_is_logged_with_step_reason_and_input(self):
        four = {"points": POINTS["points"][:4]}
        self.use(GOOD_RESEARCH, response(text(json.dumps(four))), response(text(json.dumps(four))))
        result, logs = self.run_logged("M-Pesa payment trends")
        self.assertIsNone(result)
        error = logs[-1]
        self.assertIn("ERROR", error)
        self.assertIn("failed at step 2", error)
        self.assertIn("expected exactly 5 points, got 4", error)
        self.assertIn("input: M-Pesa processed KES 38.3 trillion", error)   # what step 2 was given

    def test_resume_skips_completed_steps(self):
        bad = response(text("not json"))
        self.use(GOOD_RESEARCH, bad, bad)
        self.assertIsNone(bp.run_pipeline("topic"))
        folder = next(self.tmp.glob("runs/*"))
        self.assertTrue((folder / "1_research.json").exists())
        self.assertFalse((folder / "2_summary.json").exists())

        client = self.use(GOOD_SUMMARY, GOOD_FORMAT)                      # no research reply needed
        self.assertTrue(bp.run_pipeline(resume=str(folder)))
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("tools", client.requests[0])

    def test_rate_limit_is_retried(self):
        self.use(api_error(anthropic.RateLimitError, 429), GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        md, logs = self.run_logged("topic")
        self.assertTrue(md)
        self.assertTrue(any("RateLimitError, retrying" in line for line in logs))

    def test_network_down_fails_after_retries_and_says_where(self):
        self.use(connection_error(), connection_error(), connection_error())
        result, logs = self.run_logged("topic")
        self.assertIsNone(result)
        self.assertIn("failed at step 1", logs[-1])
        self.assertIn("APIConnectionError after 3 attempts", logs[-1])

    def test_bad_api_key_stops_without_retrying(self):
        client = self.use(api_error(anthropic.AuthenticationError, 401))
        result, logs = self.run_logged("topic")
        self.assertIsNone(result)
        self.assertEqual(len(client.requests), 1)
        self.assertIn("API key was rejected", logs[-1])

    def test_research_without_sources_is_rejected(self):
        self.use(response(text(RESEARCH_TEXT)))                            # no citations at all
        result, logs = self.run_logged("topic")
        self.assertIsNone(result)
        self.assertIn("no cited web sources", logs[-1])

    def test_no_search_mode_accepts_unsourced_research(self):
        client = self.use(response(text(RESEARCH_TEXT)), GOOD_SUMMARY, GOOD_FORMAT)
        self.assertTrue(bp.run_pipeline("topic", search=False))
        self.assertNotIn("tools", client.requests[0])

    def test_short_research_fails_at_step_1(self):
        self.use(response(text("I couldn't find much.", [("https://a.example", "A")])))
        result, logs = self.run_logged("topic")
        self.assertIn("too short", logs[-1])

    def test_failed_searches_are_logged(self):
        research = response(search_result(ok=False), *GOOD_RESEARCH.content, searches=2)
        self.use(research, GOOD_SUMMARY, GOOD_FORMAT)
        md, logs = self.run_logged("topic")
        self.assertTrue(md)
        self.assertTrue(any("search(es) failed: too_many_requests" in line for line in logs))

    def test_pause_turn_is_continued(self):
        paused = response(NS(type="server_tool_use", id="s1", name="web_search", input={}), stop="pause_turn")
        client = self.use(paused, GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)
        self.assertTrue(bp.run_pipeline("topic"))
        self.assertEqual(client.requests[1]["messages"][-1]["role"], "assistant")

    def test_missing_key(self):
        with mock.patch.object(bp, "_client", None), mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            result, logs = self.run_logged("topic")
        self.assertIsNone(result)
        self.assertIn("ANTHROPIC_API_KEY is missing", logs[-1])


class TestBatch(PipelineTest):
    def test_batch_keeps_going_after_a_topic_fails(self):
        topics = self.tmp / "topics.txt"
        topics.write_text("# comment\nFirst topic\n\nSecond topic\n", encoding="utf-8")
        self.use(response(text(RESEARCH_TEXT)), GOOD_RESEARCH, GOOD_SUMMARY, GOOD_FORMAT)   # 1st: no sources
        with self.assertLogs("pipeline", level="INFO"):
            results = bp.run_batch(topics, "brief", True)
        self.assertEqual([t for t, _ in results], ["First topic", "Second topic"])
        self.assertIsNone(results[0][1])
        self.assertTrue(results[1][1])


    def test_batch_stops_on_an_error_that_would_repeat(self):
        topics = self.tmp / "topics.txt"
        topics.write_text("One\nTwo\nThree\n", encoding="utf-8")
        bad = api_error(anthropic.BadRequestError, 400)
        bad.body = {"type": "error", "error": {"type": "invalid_request_error",
                                              "message": "tools.0.web_search_20250305: Country code KE is not supported."}}
        client = self.use(bad)
        with self.assertLogs("pipeline", level="INFO") as logs:
            results = bp.run_batch(topics, "brief", True)
        self.assertEqual(len(client.requests), 1)                         # not repeated for topics 2 and 3
        self.assertEqual([r for _, r in results], [None, None, None])
        self.assertTrue(any("Country code KE is not supported." in line and "{'type'" not in line
                            for line in logs.output))                     # clean message, not the raw dict
        self.assertTrue(any("Batch stopped after topic 1/3" in line for line in logs.output))
        self.assertEqual(list(self.tmp.glob("runs/*")), [])               # no empty run folders left behind


if __name__ == "__main__":
    unittest.main()
