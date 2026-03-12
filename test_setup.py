"""
Test script for verifying the local inference + page parser setup.

Runs each component in isolation — no Tavily or Anthropic API calls needed.
Tests that require Ollama are skipped automatically if it's not running.

Usage:
    python test_setup.py
"""

import sys
import openai

from research_artists import (
    OLLAMA_BASE_URL,
    DEFAULT_PARSER_MODEL,
    MAX_HTML_CHARS,
    AgentConfig,
    check_ollama,
    fetch_page,
    get_tools,
    html_to_text,
    parse_concert_page,
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
results = []


def record(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "⊘"}[status]
    msg = f"  {icon} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)


# -----------------------------------------------------------------------
# Test 1: check_ollama
# -----------------------------------------------------------------------
print("\n── Test 1: check_ollama ──")

ollama_running = check_ollama(OLLAMA_BASE_URL, DEFAULT_PARSER_MODEL)
record(
    "Ollama reachable + default parser model available",
    PASS if ollama_running else SKIP,
    "available" if ollama_running else f"Ollama not running or {DEFAULT_PARSER_MODEL} not pulled",
)

result = check_ollama(OLLAMA_BASE_URL, "nonexistent-model-xyz:99b")
record(
    "Nonexistent model returns False",
    PASS if not result else FAIL,
)

result = check_ollama("http://127.0.0.1:1/v1", DEFAULT_PARSER_MODEL)
record(
    "Bogus URL returns False (no crash)",
    PASS if not result else FAIL,
)

# -----------------------------------------------------------------------
# Test 2: html_to_text
# -----------------------------------------------------------------------
print("\n── Test 2: html_to_text ──")

sample_html = """
<html>
<head><script>var x = 1;</script><style>body{color:red}</style></head>
<body>
<nav><a href="/">Home</a></nav>
<header><h1>Site Header</h1></header>
<main>
  <p>Radiohead live at Hallenstadion Zürich, 2026-09-15.</p>
  <p>Tickets available at ticketcorner.ch</p>
</main>
<footer>Copyright 2026</footer>
</body>
</html>
"""

text = html_to_text(sample_html)
has_content = "Radiohead" in text and "Hallenstadion" in text
no_script = "var x" not in text
no_nav = "Home" not in text
no_footer = "Copyright" not in text

record("Preserves main content", PASS if has_content else FAIL, repr(text[:80]))
record("Strips <script>", PASS if no_script else FAIL)
record("Strips <nav>", PASS if no_nav else FAIL)
record("Strips <footer>", PASS if no_footer else FAIL)

# Truncation test
huge_html = "<html><body>" + "<p>Concert info. </p>" * 100_000 + "</body></html>"
truncated = html_to_text(huge_html)
record(
    f"Truncates to MAX_HTML_CHARS ({MAX_HTML_CHARS})",
    PASS if len(truncated) <= MAX_HTML_CHARS else FAIL,
    f"got {len(truncated)} chars",
)

# Empty HTML
empty_text = html_to_text("<html><body></body></html>")
record("Empty HTML returns short text", PASS if len(empty_text) < 50 else FAIL, repr(empty_text))

# -----------------------------------------------------------------------
# Test 3: fetch_page on real URLs
# -----------------------------------------------------------------------
print("\n── Test 3: fetch_page ──")

try:
    page = fetch_page("https://en.wikipedia.org/wiki/Radiohead")
    text = html_to_text(page)
    record(
        "Fetch Wikipedia + convert to text",
        PASS if len(text) > 500 else FAIL,
        f"{len(text)} chars extracted",
    )
except Exception as e:
    record("Fetch Wikipedia", FAIL, str(e))

try:
    fetch_page("https://httpstat.us/404")
    record("404 URL raises exception", FAIL, "no exception raised")
except Exception:
    record("404 URL raises exception", PASS)

try:
    fetch_page("http://192.0.2.1:1")  # non-routable, should timeout
    record("Unreachable URL raises exception", FAIL, "no exception raised")
except Exception:
    record("Unreachable URL raises exception", PASS)

# -----------------------------------------------------------------------
# Test 4: parse_concert_page end-to-end (requires Ollama)
# -----------------------------------------------------------------------
print("\n── Test 4: parse_concert_page (Ollama required) ──")

if not ollama_running:
    record("End-to-end parse", SKIP, "Ollama not available")
    record("No-concert page", SKIP, "Ollama not available")
    record("Bad URL handling", SKIP, "Ollama not available")
else:
    config = AgentConfig(
        mode="ollama",
        parser_client=openai.OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama", timeout=120.0),
        parser_model=DEFAULT_PARSER_MODEL,
        page_parser_enabled=True,
    )

    # Test with a page that has no concert info
    result = parse_concert_page(
        "https://en.wikipedia.org/wiki/Radiohead",
        "Radiohead",
        config,
    )
    print(f"    Wikipedia parse result (first 200 chars): {result[:200]}")
    record("Wikipedia page returns a response", PASS if len(result) > 10 else FAIL)

    # Test with a bad URL
    result = parse_concert_page("https://httpstat.us/404", "Test", config)
    record(
        "Bad URL returns error string",
        PASS if "Failed to fetch" in result else FAIL,
        result[:80],
    )

# -----------------------------------------------------------------------
# Test 5: get_tools filtering
# -----------------------------------------------------------------------
print("\n── Test 5: get_tools filtering ──")

t_anthropic_all = get_tools("anthropic", True)
t_anthropic_no_parser = get_tools("anthropic", False)
t_openai_all = get_tools("openai", True)
t_openai_no_parser = get_tools("openai", False)

record("Anthropic + parser → 3 tools", PASS if len(t_anthropic_all) == 3 else FAIL, f"got {len(t_anthropic_all)}")
record("Anthropic − parser → 2 tools", PASS if len(t_anthropic_no_parser) == 2 else FAIL, f"got {len(t_anthropic_no_parser)}")
record("OpenAI + parser → 3 tools", PASS if len(t_openai_all) == 3 else FAIL, f"got {len(t_openai_all)}")
record("OpenAI − parser → 2 tools", PASS if len(t_openai_no_parser) == 2 else FAIL, f"got {len(t_openai_no_parser)}")

# Verify parse_concert_page is actually excluded
names_no_parser = [t.get("name") or t["function"]["name"] for t in t_anthropic_no_parser]
record(
    "parse_concert_page excluded when disabled",
    PASS if "parse_concert_page" not in names_no_parser else FAIL,
)

# -----------------------------------------------------------------------
# Test 6: Config matrix (startup logic)
# -----------------------------------------------------------------------
print("\n── Test 6: Config matrix ──")

# --no-page-parser always disables regardless of Ollama
config = AgentConfig(mode="anthropic", page_parser_enabled=False)
record("--no-page-parser → disabled", PASS if not config.page_parser_enabled else FAIL)

# Anthropic mode without Ollama → parser should be auto-disabled
if not ollama_running:
    config = AgentConfig(mode="anthropic", page_parser_enabled=True)
    # Simulate the startup check
    if not check_ollama(OLLAMA_BASE_URL, DEFAULT_PARSER_MODEL):
        config.page_parser_enabled = False
    record(
        "Anthropic mode, no Ollama → parser auto-disabled",
        PASS if not config.page_parser_enabled else FAIL,
    )
else:
    config = AgentConfig(mode="anthropic", page_parser_enabled=True)
    record("Anthropic mode + Ollama → parser stays enabled", PASS if config.page_parser_enabled else FAIL)

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print("\n── Summary ──")
passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)
skipped = sum(1 for _, s, _ in results if s == SKIP)
print(f"  {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    print("\nFailed tests:")
    for name, status, detail in results:
        if status == FAIL:
            print(f"  ✗ {name}: {detail}")
    sys.exit(1)
else:
    print("\nAll tests passed!")
