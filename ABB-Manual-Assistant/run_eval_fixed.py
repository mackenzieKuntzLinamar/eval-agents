"""
Run the ABB Manual assistant agent over a CSV test set and write a readable
Markdown report.

For every row in the CSV, the agent is run on the `test_prompt` and its answer
(the "Actual Output") is recorded next to the `expected_response` and all of the
other fields from the test set, so a human can manually mark each row Pass/Fail.

The Orchestrator's `run` method is a streaming async generator with signature
`run(prompt, history)`, so it is consumed with `async for ...` rather than awaited.

Example
    python run_eval_fixed.py --csv SME-TestSet.csv --run_name "SME Eval Run"
    python run_eval_fixed.py --csv SME-TestSet.csv --limit 3
"""

import argparse
import asyncio
import json

import pandas as pd
from dotenv import load_dotenv
from orchestrator_agent import Orchestrator
from rich.progress import track
from search_tool import VertexSearchTool


# --- Load environment ---
load_dotenv()


# Columns expected in the test set CSV, in the order they should appear in the
# report, mapped to the human-friendly label used in the output file.
FIELD_LABELS = {
    "expected_response": "Expected Output",
    "safety_considerations": "Safety Considerations",
    "expected_sources": "Expected Sources",
    "expected_trace": "Expected Trace",
    "max_total_tokens": "Max Total Tokens",
    "max_total_latency": "Max Total Latency",
}


# --- Agent Execution ---
async def run_agent(orchestrator: Orchestrator, query: str) -> str:
    """Run the orchestrator on a single prompt and collect the streamed answer."""
    try:
        chunks: list[str] = []
        async for delta in orchestrator.run(query, []):
            chunks.append(delta)
        return "".join(chunks) or "(no response)"
    except Exception as exc:  # noqa: BLE001 - surface any agent error in the report
        return f"(error: {exc})"


async def retrieve_search_results(query: str) -> str:
    """Run the knowledge search directly so retrieved sources are visible in the report."""
    try:
        return await VertexSearchTool().get_knowledge(query)
    except Exception as exc:  # noqa: BLE001 - surface search errors in the report
        return f"Search error: {exc}"


async def run_row(orchestrator: Orchestrator, row: dict) -> dict:
    prompt = str(row.get("test_prompt", "")).strip()
    print(f"Running query: {prompt}")
    search_results = await retrieve_search_results(prompt)
    answer = await run_agent(orchestrator, prompt)
    print(f"Agent response: {answer[:120]}...")
    return {"row": row, "answer": answer, "search_results": search_results}


# --- Results File ---
def _fmt(value) -> str:
    """Format a value for Markdown, keeping empty fields readable."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "_(none)_"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return "_(none)_"
    return text


def _format_search_results(value: str) -> list[str]:
    """Format the JSON array returned by VertexSearchTool as readable Markdown."""
    if not value or value.strip() in {"No results found.", "[]"}:
        return ["_(none)_"]

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return [_fmt(value)]

    if not parsed:
        return ["_(none)_"]

    lines: list[str] = []
    for idx, source in enumerate(parsed, start=1):
        lines.append(f"{idx}. **Document Name:** {_fmt(source.get('Document Name'))}")
        lines.append(f"   **URL:** {_fmt(source.get('URL'))}")
        lines.append(f"   **Page Number:** {_fmt(source.get('Page Number'))}")
        lines.append(f"   **Full Text:** {_fmt(source.get('Full Text'))}")
        lines.append("")
    return lines


def write_results_file(run_name: str, source: str, results: list[dict], output_path: str) -> None:
    lines: list[str] = []
    lines.append(f"# Evaluation Results: {run_name}")
    lines.append("")
    lines.append(f"- **Source:** {source}")
    lines.append(f"- **Items:** {len(results)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, result in enumerate(results, start=1):
        row = result["row"]
        prompt = str(row.get("test_prompt", "")).strip()

        lines.append(f"## {idx}. {prompt}")
        lines.append("")
        lines.append("**Test Prompt:**")
        lines.append("")
        lines.append(_fmt(prompt))
        lines.append("")
        lines.append("**Actual Output:**")
        lines.append("")
        lines.append(_fmt(result["answer"]))
        lines.append("")
        lines.append("**Retrieved Search Results:**")
        lines.append("")
        lines.extend(_format_search_results(result["search_results"]))
        lines.append("")
        for column, label in FIELD_LABELS.items():
            lines.append(f"**{label}:**")
            lines.append("")
            lines.append(_fmt(row.get(column)))
            lines.append("")
        lines.append("**Pass/Fail (manual):**")
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"Wrote evaluation results to {output_path}")


# --- CLI Entrypoint ---
parser = argparse.ArgumentParser(description="Run the ABB agent over a CSV test set and write a Markdown report.")
parser.add_argument("--csv", default="SME-TestSet.csv", help="Path to the test set CSV (default: SME-TestSet.csv)")
parser.add_argument("--run_name", default="SME Eval Run", help="Label for this evaluation run")
parser.add_argument("--limit", type=int, help="Optional maximum number of rows to evaluate")
parser.add_argument("--output", default="evaluations_output.md", help="Path to the Markdown report to write")

if __name__ == "__main__":
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    if args.limit:
        df = df.head(args.limit)
    rows = df.to_dict(orient="records")

    orchestrator = Orchestrator()

    async def main() -> list[dict]:
        return await asyncio.gather(*(run_row(orchestrator, row) for row in rows))

    results = asyncio.run(main())

    for _ in track(range(1), description="Writing report"):
        write_results_file(args.run_name, args.csv, results, args.output)
