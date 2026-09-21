# Document Validation System - Architecture & Flow

This document explains the purpose of all folders and files in the codebase, ordered sequentially according to how a document flows through the system during validation.

---

## 1. Entry Point
* **`main.py`**
  The main executable script. It handles selecting a document (via a UI prompt), coordinates the OCR extraction, passes the data to the validation engine, and generates the final markdown report.
* **`config.py`** & **`.env`**
  Stores configuration settings and environment variables (like API keys, whether to use local LLMs, database paths, etc.).
* **`report.py`**
  Contains the logic to format the final validation decision into a readable Markdown report (like the `discovery_report_*.md` files).

---

## 2. Document Processing (OCR & Extraction)
* **`ocr/`**
  Handles reading the raw document and converting it into structured data.
  * **`client.py`**: Interacts with external/local OCR APIs to convert image/PDF pixels into raw text.
  * **`extractor.py`**: Takes the raw text from `client.py` and uses an LLM to extract specific facts (name, DOB, document number, country, etc.). It also redacts PII, creating a "redacted profile".

---

## 3. Database Lookup & Discovery
Once the document's facts are extracted, the system needs to know *where* to validate it.
* **`db/`**
  * **`lookup.py`**: Checks if the system already knows a confirmed validation source (website/API) for this specific country and document type (by querying `verification_sources.db`).
  * **`init_db.py`**: A utility script to initialize the databases and load static data from `sources_data.tsv`.
* **`sources_data.tsv`**: A spreadsheet of known government databases and verification URLs.
* **`discovery/`**
  If the database has no known source, the system attempts to find one dynamically on the web.
  * **`agent.py`**: Uses an LLM agent to search the internet (e.g., via Google Search) to find the official validation portal for the document.

---

## 4. Method Generation
Once a target website/API is found, the system must write a script (a "Method") to automate checking it.
* **`generation/`**
  * **`generator.py`**: Takes the target URL, visits it to extract the HTML structure (forms, inputs, and JavaScript logic), and asks the LLM to write a JSON script (`execution_steps`) on how to automate the validation.
* **`prompts/`**
  * **`method_generation.txt`**: The specific prompt instructions sent to the LLM detailing exactly how to format the JSON script and explaining the difference between `WEB_FORM` and `HTTP` methods.

---

## 5. Method Validation (Testing & Self-Healing)
Before a generated method is trusted, it is tested.
* **`validation/`**
  * **`validator.py`**: Orchestrates the testing of the newly generated method. It runs the method with fake data to ensure it correctly identifies an "invalid" document. If it fails, it runs a **Self-Healing Loop** (sending error logs back to the LLM to rewrite the script) for up to 5 attempts.

---

## 6. Execution (The Sandboxed "Robot")
This is where the actual validation happens. To keep the system secure, all validation methods run inside isolated Docker containers.
* **`execution/`**
  * **`docker_runner.py`**: The bridge between the main system and the secure Docker sandbox. It spins up a container, injects the executor script and the inputs, runs it, and reads the output.
  * **`models.py`**: Defines the data structures passed in and out of the Docker container.
* **`Dockerfile.executor`**: The recipe for building the secure, lightweight Docker container (`dvs-executor:latest`).
* **`executors/`**
  The actual scripts that run *inside* the Docker container to perform the check.
  * **`http_executor.py`**: Executes direct API calls (used for AJAX/REST endpoints).
  * **`form_executor.py`**: Executes standard HTML form submissions using BeautifulSoup.
  * **`browser_executor.py`**: (If implemented) Uses a headless browser for complex JavaScript-heavy sites.

---

## 7. Engine & Registry
The central brain that ties it all together and remembers what works.
* **`engine/`**
  * **`validation_engine.py`**: The central orchestrator. It manages the flow between checking the registry -> checking the DB -> running discovery -> generating a method -> validating the method -> executing it.
* **`registry/`**
  Stores successfully generated methods so we don't have to use the LLM again next time.
  * **`repository.py`**: Interacts with `method_registry.db` to save, load, and manage the lifecycle of validation methods (e.g., marking them ACTIVE if they pass testing).

---

## 8. Utilities & External Interfaces
* **`utils/`**
  * **`llm_client.py`**: A unified wrapper to interact with Large Language Models (Gemini, Groq, or Local LLMs via LM Studio).
* **`mcp_server/`**
  * **`server.py`**: The Model Context Protocol server. This allows AI assistants (like Claude/Gemini) to interact directly with the validation system's APIs and logic.
* **`tests/`**
  Contains automated unit tests for various system components.
