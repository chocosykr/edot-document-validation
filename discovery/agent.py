import json
import time
import requests

from tavily import TavilyClient

from config import (
    LLM_URL,
    LLM_MODEL,
    LLM_API_KEY,
    TAVILY_API_KEY
)


# --------------------------------------------------
# Configuration
# --------------------------------------------------

URL = LLM_URL

MODEL = LLM_MODEL
API_KEY = LLM_API_KEY

MAX_FINAL_SOURCE_CHARS = 30000
MAX_ANALYSIS_CHARS = 4000
MAX_SEARCH_QUERIES = 8
MAX_RESULTS_PER_QUERY = 2
LLM_MAX_RETRIES = 3
LLM_MIN_INTERVAL_SECONDS = 1.0

last_llm_request_at = 0.0


if not API_KEY:
    raise ValueError(
        "LLM_API_KEY is not set in .env"
    )

if not TAVILY_API_KEY:
    raise ValueError(
        "TAVILY_API_KEY is not set in .env"
    )


tavily_client = TavilyClient(
    api_key=TAVILY_API_KEY
)


# --------------------------------------------------
# Prompt loading
# --------------------------------------------------

def load_prompt(filename: str) -> str:
    with open(
        f"prompts/{filename}",
        "r",
        encoding="utf-8"
    ) as file:
        return file.read()


# --------------------------------------------------
# LLM API
# --------------------------------------------------

def call_llm(prompt: str) -> str:

    global last_llm_request_at

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0
    }

    for attempt in range(LLM_MAX_RETRIES + 1):

        elapsed = time.monotonic() - last_llm_request_at
        if elapsed < LLM_MIN_INTERVAL_SECONDS:
            time.sleep(
                LLM_MIN_INTERVAL_SECONDS - elapsed
            )

        response = requests.post(
            URL,
            headers=headers,
            json=payload,
            timeout=120
        )

        last_llm_request_at = time.monotonic()

        if response.status_code != 429:
            break

        if attempt == LLM_MAX_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get(
            "Retry-After"
        )

        try:
            delay = float(retry_after) if retry_after else 0
        except (TypeError, ValueError):
            delay = 2 ** (attempt + 1)

        print(
            f"LLM rate limit reached; retrying in "
            f"{delay:.0f}s..."
        )
        time.sleep(delay)

    response.raise_for_status()

    result = response.json()

    return result["choices"][0]["message"]["content"]


# --------------------------------------------------
# Generate search queries
# --------------------------------------------------

def generate_search_queries(
    redacted_profile: dict
) -> list[str]:

    prompt = load_prompt(
        "discovery_queries.txt"
    )

    prompt += "\n\n"

    prompt += json.dumps(
        redacted_profile,
        ensure_ascii=False,
        indent=2
    )

    print("\nGenerating search queries with LLM...")

    content = call_llm(prompt)

    return parse_json_array(content)[:MAX_SEARCH_QUERIES]


# --------------------------------------------------
# Tavily searches
# --------------------------------------------------

def perform_searches(
    queries: list[str]
) -> list[dict]:

    search_results = []

    for query in queries:

        print(f"\nSearching: {query}")

        try:

            response = tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=MAX_RESULTS_PER_QUERY,
                include_answer=False
            )

            results = []

            for result in response.get(
                "results",
                []
            ):

                results.append({
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "content": result.get(
                        "content",
                        ""
                    ),
                    "score": result.get("score")
                })

            search_results.append({
                "query": query,
                "results": results
            })

        except Exception as e:

            print(f"Search failed: {e}")

            search_results.append({
                "query": query,
                "results": [],
                "error": str(e)
            })

    return search_results


# --------------------------------------------------
# Analyze ONE Tavily result
# --------------------------------------------------

def analyze_single_result(
    redacted_profile: dict,
    query: str,
    search_result: dict
) -> dict:

    prompt = load_prompt(
        "discovery_analysis.txt"
    )

    prompt += "\n\nREDACTED DOCUMENT PROFILE:\n"

    prompt += json.dumps(
        redacted_profile,
        ensure_ascii=False,
        indent=2
    )

    prompt += "\n\nSEARCH QUERY:\n"

    prompt += query

    prompt += "\n\nTAVILY RESULT:\n"

    prompt += json.dumps(
        search_result,
        ensure_ascii=False,
        indent=2
    )

    print(
        f"  Analyzing: "
        f"{search_result.get('title', 'Unknown')}"
    )

    content = call_llm(prompt)

    return parse_json_response(content)


# --------------------------------------------------
# Analyze all Tavily results individually
# --------------------------------------------------

def analyze_search_results(
    redacted_profile: dict,
    search_results: list[dict]
) -> list[dict]:

    analyzed_results = []

    for search_group in search_results:

        query = search_group["query"]

        print(
            f"\nAnalyzing results for query: {query}"
        )

        for result in search_group.get(
            "results",
            []
        ):

            try:

                analysis = analyze_single_result(
                    redacted_profile,
                    query,
                    result
                )

                analyzed_results.append({
                    "query": query,
                    "source": result,
                    "analysis": analysis
                })

            except Exception as e:

                print(
                    f"  Analysis failed: {e}"
                )

                analyzed_results.append({
                    "query": query,
                    "source": result,
                    "analysis": None,
                    "error": str(e)
                })

    return analyzed_results


def build_source_selection_payload(
    analyzed_results: list[dict]
) -> list[dict]:

    selected_results = []
    total_chars = 0

    for result in analyzed_results:

        if result.get("analysis") is None:
            continue

        source = result.get("source", {})
        analysis = result["analysis"]
        analysis_text = json.dumps(
            analysis,
            ensure_ascii=False,
            separators=(",", ":")
        )

        if len(analysis_text) > MAX_ANALYSIS_CHARS:
            analysis = {
                "summary": analysis_text[:MAX_ANALYSIS_CHARS]
            }

        candidate = {
            "query": result.get("query"),
            "source": {
                "title": source.get("title"),
                "url": source.get("url"),
                "score": source.get("score")
            },
            "analysis": analysis
        }

        candidate_chars = len(json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":")
        ))

        if total_chars + candidate_chars > MAX_FINAL_SOURCE_CHARS:
            break

        selected_results.append(candidate)
        total_chars += candidate_chars

    return selected_results


# --------------------------------------------------
# Final source selection
# --------------------------------------------------

def select_best_sources(
    redacted_profile: dict,
    analyzed_results: list[dict]
) -> dict:

    prompt = load_prompt(
        "discovery_final.txt"
    )

    prompt += "\n\nREDACTED DOCUMENT PROFILE:\n"

    prompt += json.dumps(
        redacted_profile,
        ensure_ascii=False,
        indent=2
    )

    prompt += "\n\nINDIVIDUAL SOURCE ANALYSES:\n"

    prompt += json.dumps(
        build_source_selection_payload(analyzed_results),
        ensure_ascii=False,
        separators=(",", ":")
    )

    print(
        "\nSelecting the best verification sources with LLM..."
    )

    content = call_llm(prompt)

    return parse_json_response(content)


# --------------------------------------------------
# Main discovery flow
# --------------------------------------------------

def run_discovery(
    redacted_profile: dict
) -> dict:

    # ----------------------------------------------
    # Step 1:
    # LLM generates search queries
    # ----------------------------------------------

    queries = generate_search_queries(
        redacted_profile
    )

    # ----------------------------------------------
    # Step 2:
    # Tavily performs the searches
    # ----------------------------------------------

    search_results = perform_searches(
        queries
    )

    # ----------------------------------------------
    # Step 3:
    # Analyze each result separately with LLM
    # ----------------------------------------------

    analyzed_results = analyze_search_results(
        redacted_profile,
        search_results
    )

    # ----------------------------------------------
    # Step 4:
    # LLM selects the best sources
    # ----------------------------------------------

    final_result = select_best_sources(
        redacted_profile,
        analyzed_results
    )

    return {
        "result": final_result,
        "searches_performed": queries,
        "analyzed_results": analyzed_results,
        "raw_search_results": search_results
    }


# --------------------------------------------------
# JSON parsing
# --------------------------------------------------

def parse_json_response(
    content: str
) -> dict:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, dict):

        raise ValueError(
            "LLM did not return a JSON object."
        )

    return result


def parse_json_array(
    content: str
) -> list[str]:

    content = content.strip()

    if content.startswith("```"):

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        content = content.strip()

    result = json.loads(content)

    if not isinstance(result, list):

        raise ValueError(
            "LLM did not return a JSON array."
        )

    if not all(
        isinstance(query, str)
        for query in result
    ):

        raise ValueError(
            "All search queries must be strings."
        )

    return result

