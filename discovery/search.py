"""Discovery search stage: LLM-generated search queries and Tavily execution.

Split out of agent.py. The Tavily client is created at import time; a missing
key fails fast (tests set a dummy key before importing).
"""

import json

from tavily import TavilyClient

from config import TAVILY_API_KEY
from discovery.llm import call_llm, load_prompt, parse_json_array

MAX_SEARCH_QUERIES = 8
MAX_RESULTS_PER_QUERY = 5


if not TAVILY_API_KEY:
    raise ValueError(
        "TAVILY_API_KEY is not set in .env"
    )


tavily_client = TavilyClient(
    api_key=TAVILY_API_KEY
)


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
