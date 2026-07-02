"""
Run an LLM judge over the ABB Manual Assistant test set.

This script reads SME-TestSet.csv, runs the assistant for each test prompt, and
uses Gemini 2.5 Pro to compare the actual assistant output against the expected
output from the CSV. It writes a separate Markdown report for review.

Examples:
    python3 llm_evaluator.py --csv SME-TestSet.csv --run_name "SME LLM Judge Run"
    python3 llm_evaluator.py --csv SME-TestSet.csv --limit 3
"""

import argparse
import asyncio
import os
from typing import Literal

import pandas as pd
from agents import Agent, OpenAIChatCompletionsModel, Runner
from dotenv import load_dotenv
from openai import AsyncOpenAI
from orchestrator_agent import Orchestrator
from pydantic import BaseModel, Field
from rich.progress import track


load_dotenv()


JUDGE_INSTRUCTIONS = """
You are an evaluator for an ABB robot manual assistant.

Compare the Actual Output against the Expected Output for the given Test Prompt.
The wording does not need to match exactly. Judge whether the Actual Output
captures the required meaning, troubleshooting steps, safety warnings, and key
technical facts from the Expected Output.

Use this rubric:
- PASS: The Actual Output covers the expected answer's important meaning and does not add harmful or contradictory guidance.
- PARTIAL: The Actual Output is related and contains some correct information, but misses important expected details or safety considerations.
- FAIL: The Actual Output does not answer the prompt, contradicts the expected answer, is mostly unrelated, or gives unsafe guidance.

When Safety Considerations are provided, treat them as important required context.
Expected Sources and Expected Trace are reference context; do not require exact wording, but use them to understand what the expected answer is checking.
Return only the structured evaluation object.
""".strip()


JUDGE_TEMPLATE = """\
# Test Prompt
{test_prompt}

# Expected Output
{expected_response}

# Safety Considerations
{safety_considerations}

# Expected Sources
{expected_sources}

# Expected Trace
{expected_trace}

# Actual Output
{actual_output}
"""


class JudgeResponse(BaseModel):
    verdict: Literal["PASS", "PARTIAL", "FAIL"]
    score: float = Field(ge=0, le=1, description="1.0 for PASS, around 0.5 for PARTIAL, 0.0 for FAIL")
    explanation: str
    missing_expected_points: list[str] = Field(default_factory=list)
    unsupported_or_incorrect_claims: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    row_number: int
    test_prompt: str
    expected_response: str
    actual_output: str
    safety_considerations: str
    expected_sources: str
    expected_trace: str
    max_total_tokens: str
    max_total_latency: str
    judge: JudgeResponse


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


async def run_assistant(orchestrator: Orchestrator, prompt: str) -> str:
    try:
        chunks: list[str] = []
        async for delta in orchestrator.run(prompt, []):
            chunks.append(delta)
        return "".join(chunks).strip() or "(no response)"
    except Exception as exc:  # noqa: BLE001 - include runtime failures in the judge report
        return f"(assistant error: {exc})"


async def run_judge(client: AsyncOpenAI, row: dict, actual_output: str) -> JudgeResponse:
    judge_agent = Agent(
        name="ABB Manual Assistant LLM Judge",
        instructions=JUDGE_INSTRUCTIONS,
        output_type=JudgeResponse,
        model=OpenAIChatCompletionsModel(model="gemini-2.5-pro", openai_client=client),
    )

    judge_input = JUDGE_TEMPLATE.format(
        test_prompt=_clean(row.get("test_prompt")),
        expected_response=_clean(row.get("expected_response")),
        safety_considerations=_clean(row.get("safety_considerations")) or "(none)",
        expected_sources=_clean(row.get("expected_sources")) or "(none)",
        expected_trace=_clean(row.get("expected_trace")) or "(none)",
        actual_output=actual_output,
    )
    result = await Runner.run(judge_agent, input=judge_input)
    return result.final_output_as(JudgeResponse)


async def evaluate_row(row_number: int, row: dict, orchestrator: Orchestrator, client: AsyncOpenAI) -> EvalResult:
    prompt = _clean(row.get("test_prompt"))
    print(f"Evaluating {row_number}: {prompt}")
    actual_output = await run_assistant(orchestrator, prompt)
    judge = await run_judge(client, row, actual_output)

    return EvalResult(
        row_number=row_number,
        test_prompt=prompt,
        expected_response=_clean(row.get("expected_response")),
        actual_output=actual_output,
        safety_considerations=_clean(row.get("safety_considerations")),
        expected_sources=_clean(row.get("expected_sources")),
        expected_trace=_clean(row.get("expected_trace")),
        max_total_tokens=_clean(row.get("max_total_tokens")),
        max_total_latency=_clean(row.get("max_total_latency")),
        judge=judge,
    )


def _md(value: str) -> str:
    text = _clean(value)
    return text if text else "_(none)_"


def _bullet_list(items: list[str]) -> list[str]:
    if not items:
        return ["_(none)_"]
    return [f"- {_md(item)}" for item in items]


def write_report(run_name: str, csv_path: str, results: list[EvalResult], output_path: str) -> None:
    pass_count = sum(1 for result in results if result.judge.verdict == "PASS")
    partial_count = sum(1 for result in results if result.judge.verdict == "PARTIAL")
    fail_count = sum(1 for result in results if result.judge.verdict == "FAIL")

    lines: list[str] = []
    lines.append(f"# LLM Evaluation Results: {run_name}")
    lines.append("")
    lines.append(f"- **Source:** {csv_path}")
    lines.append(f"- **Judge Model:** gemini-2.5-pro")
    lines.append(f"- **Items:** {len(results)}")
    lines.append(f"- **PASS:** {pass_count}")
    lines.append(f"- **PARTIAL:** {partial_count}")
    lines.append(f"- **FAIL:** {fail_count}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for result in results:
        lines.append(f"## {result.row_number}. {result.judge.verdict} - {result.test_prompt}")
        lines.append("")
        lines.append("**Score:**")
        lines.append("")
        lines.append(str(result.judge.score))
        lines.append("")
        lines.append("**Judge Explanation:**")
        lines.append("")
        lines.append(_md(result.judge.explanation))
        lines.append("")
        lines.append("**Missing Expected Points:**")
        lines.append("")
        lines.extend(_bullet_list(result.judge.missing_expected_points))
        lines.append("")
        lines.append("**Unsupported or Incorrect Claims:**")
        lines.append("")
        lines.extend(_bullet_list(result.judge.unsupported_or_incorrect_claims))
        lines.append("")
        lines.append("**Test Prompt:**")
        lines.append("")
        lines.append(_md(result.test_prompt))
        lines.append("")
        lines.append("**Expected Output:**")
        lines.append("")
        lines.append(_md(result.expected_response))
        lines.append("")
        lines.append("**Actual Output:**")
        lines.append("")
        lines.append(_md(result.actual_output))
        lines.append("")
        lines.append("**Safety Considerations:**")
        lines.append("")
        lines.append(_md(result.safety_considerations))
        lines.append("")
        lines.append("**Expected Sources:**")
        lines.append("")
        lines.append(_md(result.expected_sources))
        lines.append("")
        lines.append("**Expected Trace:**")
        lines.append("")
        lines.append(_md(result.expected_trace))
        lines.append("")
        lines.append("**Max Total Tokens:**")
        lines.append("")
        lines.append(_md(result.max_total_tokens))
        lines.append("")
        lines.append("**Max Total Latency:**")
        lines.append("")
        lines.append(_md(result.max_total_latency))
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))

    print(f"Wrote LLM evaluation report to {output_path}")


parser = argparse.ArgumentParser(description="Use Gemini 2.5 Pro to judge ABB assistant outputs against expected CSV answers.")
parser.add_argument("--csv", default="SME-TestSet.csv", help="Path to the test set CSV")
parser.add_argument("--run_name", default="SME LLM Judge Run", help="Label for this evaluation run")
parser.add_argument("--limit", type=int, help="Optional maximum number of rows to evaluate")
parser.add_argument("--output", default="llm_evaluation_output.md", help="Path to the Markdown judge report")


if __name__ == "__main__":
    args = parser.parse_args()

    data = pd.read_csv(args.csv)
    if args.limit:
        data = data.head(args.limit)
    rows = data.to_dict(orient="records")

    openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))
    assistant = Orchestrator()

    async def main() -> list[EvalResult]:
        results: list[EvalResult] = []
        for row_number, row in track(list(enumerate(rows, start=1)), description="Running LLM judge"):
            results.append(await evaluate_row(row_number, row, assistant, openai_client))
        return results

    evaluations = asyncio.run(main())
    write_report(args.run_name, args.csv, evaluations, args.output)
