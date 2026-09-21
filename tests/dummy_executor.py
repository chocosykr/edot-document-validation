import json
import os
import sys

def main():
    input_path = "input.json"
    output_path = "output.json"

    if not os.path.exists(input_path):
        print("Error: input.json not found")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        request_data = json.load(f)

    # Check for a special sleep trigger to test timeouts
    if request_data.get("inputs", {}).get("trigger_timeout") == "true":
        import time
        time.sleep(10)

    # Mock validation logic
    inputs = request_data.get("inputs", {})
    if inputs.get("document_number") == "VALID123":
        decision = "VERIFIED"
        evidence = {"source": "mock", "valid": True}
    else:
        decision = "REJECTED"
        evidence = {"source": "mock", "valid": False}

    result = {
        "decision_status": decision,
        "evidence": evidence,
        "raw_response": "Mock raw response from dummy executor"
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f)

    print("Execution completed successfully.")

if __name__ == "__main__":
    main()
