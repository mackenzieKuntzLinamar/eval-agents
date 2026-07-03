import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from langfuse import get_client
from rich.progress import track


load_dotenv()
langfuse = get_client()


REQUIRED_COLUMNS = {
    "test_prompt": ["Test prompt", "test prompt", "prompt", "question"],
    "expected_response": ["expected response", "expected_response", "expected_answer", "answer"],
    "safety_considerations": [
        "relevant safety considerations that should be addressed",
        "relevant_safety_considerations",
        "safety_considerations",
        "safety considerations",
    ],
    "expected_sources": ["expected sources (manual, page)", "expected_sources", "sources", "source"],
    "expected_trace": ["expected trace", "expected_trace", "trace"],
    "max_total_tokens": ["max total tokens", "max_total_tokens", "max_tokens"],
    "max_total_latency": ["max total latency", "max_total_latency", "max_latency"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a benchmark dataset into Langfuse")
    parser.add_argument("--dataset-name", required=True, help="Name of the Langfuse dataset")
    parser.add_argument("--input-file", required=True, help="Path to a CSV, JSON, or JSONL file containing the test cases")
    parser.add_argument(
        "--description",
        default="Robot error troubleshooting Q&A dataset",
        help="Description for the Langfuse dataset",
    )
    return parser.parse_args()


def load_rows(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return pd.DataFrame(data)
        if isinstance(data, dict) and "rows" in data:
            return pd.DataFrame(data["rows"])
        raise ValueError("JSON file must contain a list of objects or a {'rows': [...]} object")
    if suffix == ".jsonl":
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return pd.DataFrame(records)
    raise ValueError("Unsupported file type. Use .csv, .json, or .jsonl")


def _find_column(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


def normalize_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    if len(df.columns) < 2:
        raise ValueError("Input file must contain at least two columns: question and expected response.")

    normalized = df.copy()

    question_col = _find_column(normalized, REQUIRED_COLUMNS["test_prompt"])
    answer_col = _find_column(normalized, REQUIRED_COLUMNS["expected_response"])

    if question_col is None:
        question_col = normalized.columns[0]
    if answer_col is None:
        if len(normalized.columns) < 2:
            raise ValueError("Input file must contain at least two columns: question and expected response.")
        answer_col = normalized.columns[1]

    normalized["test_prompt"] = normalized[question_col]
    normalized["expected_response"] = normalized[answer_col]

    extra_columns = [
        col
        for col in normalized.columns
        if col not in {question_col, answer_col, "test_prompt", "expected_response"}
    ]

    return normalized, extra_columns


def _sanitize_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def build_expected_output(row: pd.Series) -> dict[str, Any]:
    return {
        "text": str(row["expected_response"]),
    }


def ensure_dataset(dataset_name: str, description: str) -> None:
    try:
        langfuse.get_dataset(dataset_name)
        print(f"Dataset '{dataset_name}' already exists; reusing it.")
    except Exception:
        langfuse.create_dataset(
            name=dataset_name,
            description=description,
            metadata={"type": "benchmark", "source": "manual_upload"},
        )
        print(f"Created dataset '{dataset_name}'.")


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_file).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    df = load_rows(input_path)
    df, extra_columns = normalize_columns(df)
    ensure_dataset(args.dataset_name, args.description)

    for idx, row in track(df.iterrows(), total=len(df), description="Uploading to Langfuse"):
        metadata = {
            "source_file": str(input_path),
            "row_index": int(idx),
        }

        for col in extra_columns:
            metadata[col] = _sanitize_value(row.get(col))

        # Move other canonical expectation columns into metadata if present.
        for canonical in [
            "safety_considerations",
            "expected_sources",
            "expected_trace",
            "max_total_tokens",
            "max_total_latency",
        ]:
            if canonical in row and canonical not in extra_columns:
                metadata[canonical] = _sanitize_value(row.get(canonical))

        langfuse.create_dataset_item(
            dataset_name=args.dataset_name,
            input={"text": str(row["test_prompt"])},
            expected_output=build_expected_output(row),
            metadata=metadata,
            id=f"{args.dataset_name.lower().replace(' ', '-')}-{idx:03}",
        )

    print(f"Uploaded {len(df)} rows to dataset '{args.dataset_name}'.")


if __name__ == "__main__":
    main()
