import json
import logging
import sys
import os
import asyncio

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from utils.llm_client import get_llm_config

# Langchain MCP integration
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp import StdioServerParameters
from langgraph.prebuilt import create_react_agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_fallback_agent():
    if not os.path.exists("agent_debug_log.json"):
        logger.error("No agent_debug_log.json found.")
        return

    with open("agent_debug_log.json", "r") as f:
        debug_info = json.load(f)

    config = get_llm_config()

    # Use the configured OpenAI-compatible endpoint. The LLM_MODEL toggle in
    # .env selects the model: "AI_Local" = local gateway, a frontier model
    # name = frontier escalation. The fallback agent runs whichever is set.
    llm = ChatOpenAI(
        model=config.get("model", "AI_Local"),
        api_key=config.get("api_key", "dummy"),
        base_url=config.get("url", "https://ai.edot-solutions.com/v1").replace("/chat/completions", ""),
    )

    config_mcp = {
        "dvs-mcp": {
            "command": sys.executable,
            "args": ["mcp_server/server.py"],
            "transport": "stdio"
        }
    }

    client = MultiServerMCPClient(config_mcp)
    
    tools = await client.get_tools()
    
    agent = create_react_agent(llm, tools)

    prompt = f"""
A technical failure occurred during document validation.
Here is the debug log:
{json.dumps(debug_info, indent=2)}

Your task is to diagnose the broken method and fix it, under these rules:

1. START by calling get_method for the method_id in the log. Read the actual
   failure_reason. If the failure says required values (e.g. passport_number,
   email) were not available from the document, classify each missing field
   before doing anything else (see the CONTACT-ONLY VS IDENTITY principle
   below).
2. Only attempt a fix if you can see, from the method's own page structure or
   the error, a concrete mechanical problem (wrong endpoint, wrong verb,
   wrong parameter names, missing anti-forgery token).
3. Rewrite execution_steps ONLY from evidence. Never guess field names or
   values. Never put redaction tokens ([...]) or placeholder values ("test",
   "xxx", "123456") in required_inputs or steps — the execution layer refuses
   to submit them to a live endpoint, and you must not try to sneak them
   through.
4. When you have a candidate fix, upsert it with upsert_method. The registry
   will store it with status TESTING — an LLM self-assessment is never trusted
   with ACTIVE.
5. LIVE-TEST it with validate_document using the exact required_inputs you
   declared. If it executes cleanly it is promoted to ACTIVE automatically.
   If it fails, read the returned error/raw_response and try again.
6. A method is done ONLY when validate_document returns a clean execution for
   it. Report honestly if you cannot fix it: say what you tried, what failed,
   and what manual input would be needed instead.

STANDING PRINCIPLE — CONTACT-ONLY VS IDENTITY FIELDS:
When a method fails because a required input was not available from the
document, classify the field first:

- CONTACT-ONLY fields (notification email, callback phone, requester
  reference) are used by the target site to SEND results or as metadata —
  they are never checked against the document holder or matched against a
  record. The method schema can declare them in expected_responses as
  "contact_only_inputs": ["field_name", ...]. The execution layer then
  synthesizes a plausible-format inert value (e.g. an address under the
  reserved .invalid TLD) and the missing-field refusal does not apply.
  If a method lacks this declaration for an obviously contact-only field,
  ADD the declaration in your upsert — that is the correct fix, not
  inventing extraction mappings. Evidence that a field is contact-only: its
  form label says "Requester's Email" / "notify" / similar, it is type=email
  with no record cross-check, or the site's own docs say results are mailed.
- IDENTITY fields (document numbers, serials, passport numbers, names,
  birth dates) are checked against records. NEVER declare them
  contact_only, NEVER fabricate values, and NEVER map extraction fields to
  them if the data does not exist on the document. If an identity field is
  genuinely absent from the document, the honest outcome is refusal with
  "requires manual input" — upsert_method will reject any attempt to mark
  an identity-shaped field as contact_only.
- If you are unsure whether a field is contact-only or identity, treat it
  as IDENTITY and say so in your report. Do not guess in the permissive
  direction.

Background you must not repeat: a previous run of this loop made three
consecutive untested guesses, upserted each directly as ACTIVE, and one of
them sent incomplete values to a live government server (real HTTP 500).
Every upsert now goes through a live test before promotion — cooperate with
that instead of working around it.
"""
    logger.info("Starting Agentic Fallback Loop...")
    async for chunk in agent.astream({"messages": [HumanMessage(content=prompt)]}):
        if "agent" in chunk:
            msg = chunk["agent"]["messages"][0].content
            if msg: print(f"Agent: {msg}")
        elif "tools" in chunk:
            print(f"Tool Result: {chunk['tools']['messages'][0].content}")

if __name__ == "__main__":
    asyncio.run(run_fallback_agent())
