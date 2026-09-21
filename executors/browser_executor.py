"""
Browser Executor (Playwright Stub)
====================================
Runs inside Docker. Handles method_type: BROWSER.

Full Playwright execution requires a Chromium-capable Docker image
(e.g. mcr.microsoft.com/playwright/python:v1.x-jammy) and Playwright
installed.  This stub returns VALIDATION_UNAVAILABLE so the system
degrades gracefully until a browser-capable image is configured.

To enable:
  1. Build/pull a playwright-capable image.
  2. Replace this file with the real Playwright implementation.
  3. Update the DockerMethodRunner to use that image for BROWSER methods.

The real implementation should follow this pattern:
  - Read input.json (same contract as other executors)
  - Use async_playwright() to launch Chromium
  - Execute each execution_step (navigate, fill, click, wait_for, etc.)
  - Screenshot on failure for evidence
  - Write output.json with decision + evidence
"""

import json
import os
import sys


INPUT_PATH = "input.json"
OUTPUT_PATH = "output.json"


def main():
    if not os.path.exists(INPUT_PATH):
        print("ERROR: input.json not found", file=sys.stderr)
        sys.exit(1)

    result = {
        "decision_status": "VALIDATION_UNAVAILABLE",
        "evidence": {
            "reason": "Browser execution is not yet configured. "
                      "A Playwright-capable Docker image is required.",
            "method_type": "BROWSER"
        },
        "raw_response": None
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print("Browser executor: returning VALIDATION_UNAVAILABLE (stub).")


if __name__ == "__main__":
    main()
