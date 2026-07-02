import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run evaluations against a Langfuse dataset")
    parser.add_argument("--dataset-name", required=True, help="Name of the Langfuse dataset")
    parser.add_argument("--run-name", required=True, help="Name for this evaluation run")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of rows to evaluate")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    cmd = [
        sys.executable,
        str(base_dir / "run_eval.py"),
        "--langfuse_dataset_name",
        args.dataset_name,
        "--run_name",
        args.run_name,
    ]
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=base_dir, check=True)


if __name__ == "__main__":
    main()
