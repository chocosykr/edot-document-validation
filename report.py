import json
import os
from datetime import datetime

def generate_markdown_report(discovery_result: dict, document_path: str, output_dir: str = ".") -> str:
    """Generates a Markdown report from the discovery result."""
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(os.path.basename(document_path))[0]
    report_filename = os.path.join(output_dir, f"discovery_report_{base_name}_{timestamp}.md")
    
    final_result = discovery_result.get("result", {})
    searches = discovery_result.get("searches_performed", [])
    analyzed_results = discovery_result.get("analyzed_results", [])
    
    lines = []
    lines.append(f"# Document Validation Discovery Report")
    lines.append(f"**Document**: `{os.path.basename(document_path)}`")
    lines.append(f"**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    
    lines.append("## 1. Best Validation Sources")
    if not final_result:
        lines.append("No final results provided.")
    else:
        lines.append("```json")
        lines.append(json.dumps(final_result, indent=2, ensure_ascii=False))
        lines.append("```")
    lines.append("")
    
    lines.append("## 2. Search Queries Performed")
    for q in searches:
        lines.append(f"- `{q}`")
    lines.append("")
    
    lines.append("## 3. Analyzed Results Summary")
    for i, res in enumerate(analyzed_results, 1):
        query = res.get("query", "Unknown Query")
        source = res.get("source", {})
        title = source.get("title", "No Title")
        url = source.get("url", "No URL")
        error = res.get("error")
        
        lines.append(f"### 3.{i} {title}")
        lines.append(f"- **URL**: {url}")
        lines.append(f"- **Query**: `{query}`")
        if error:
            lines.append(f"- **Error**: {error}")
        else:
            analysis = res.get("analysis", {})
            lines.append(f"#### Analysis")
            lines.append("```json")
            lines.append(json.dumps(analysis, indent=2, ensure_ascii=False))
            lines.append("```")
        lines.append("")
        
    report_content = "\n".join(lines)
    
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    return report_filename
