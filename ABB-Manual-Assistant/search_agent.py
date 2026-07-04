import inspect
import json
import logging
import os
import traceback
from typing import Any

import agents
from dotenv import load_dotenv
from openai import AsyncOpenAI

from search_tool import VertexSearchTool


load_dotenv()

logger = logging.getLogger(__name__)


class SearchAgent:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
        )

        self.knowledge_tool = agents.function_tool(
            self.search_knowledgebase,
            name_override="knowledge_search",
            description_override=(
                "Searches the ABB robot manual vector database for the most "
                "relevant technical sections."
            ),
        )

        self.search_agent = agents.Agent(
            name="Search Agent",
            instructions="""
You are a Search Agent specialized in retrieving exact, relevant information
from ABB robot manuals stored in a vector database.

Your ONLY purpose is to:
- Use the provided knowledge_search tool to query the database.
- Return the most relevant technical excerpts that directly answer the query.
- Preserve the original wording as much as possible.
- Do not paraphrase, summarize, or add advice.

Rules:
- Always use knowledge_search.
- Do not answer from your own knowledge.
- Do not guess or fill in missing technical details.
- Do not include unrelated or generic text.
- If no relevant information is found, return exactly [].
- If the search tool fails, clearly state that retrieval failed. Do not invent
  an ABB-specific answer.

Output Format:
Return ONLY valid JSON. Do not use markdown code fences.

The root value must be a JSON array.

Each result object MUST include all of these fields:
- source: string
- url: string
- excerpt: string
- confidence: number

Do not omit confidence.

If the search tool provides confidence, preserve it.
If the search tool does not provide confidence, set confidence to 1.0 for directly relevant results.

Required example:
[
  {
    "source": "ABB_IRC5_Operating_Troubleshooting_Manual — Page 105",
    "url": "projects/...",
    "excerpt": "Relevant excerpt...",
    "confidence": 1.0
  }
]

If no relevant information is found, return:
[]
""".strip(),
            model=agents.OpenAIChatCompletionsModel(
                model="gemini-2.5-flash",
                openai_client=self.client,
            ),
            model_settings=agents.ModelSettings(
                tool_choice="required",
                temperature=0,
            ),
            tools=[self.knowledge_tool],
        )

    @staticmethod
    def _compact_repr(value: Any, max_length: int = 4000) -> str:
        value_repr = repr(value)

        if len(value_repr) <= max_length:
            return value_repr

        return f"{value_repr[:max_length]}... <truncated>"

    @staticmethod
    async def _resolve_knowledge_result(value: Any) -> Any:
        """
        Normalize whatever VertexSearchTool.get_knowledge() returns.

        It may return:
        - a regular result value,
        - an awaitable/coroutine,
        - or an async generator.
        """
        if inspect.isasyncgen(value) or hasattr(value, "__aiter__"):
            return [item async for item in value]

        if inspect.isawaitable(value):
            return await value

        return value

    @staticmethod
    async def search_knowledgebase(query: str) -> Any:
        """
        Tool function registered with the Search Agent.
        """
        query = query.strip()

        if not query:
            print("[SEARCH] Empty query received; returning no results.", flush=True)
            return []

        try:
            print(f"\n[SEARCH] Query: {query!r}", flush=True)

            vertex_tool = VertexSearchTool()

            raw_result = vertex_tool.get_knowledge(query)

            print(
                f"[SEARCH] Backend return type: {type(raw_result).__name__}",
                flush=True,
            )

            resolved_result = await SearchAgent._resolve_knowledge_result(
                raw_result
            )

            print(
                f"[SEARCH] Resolved result type: "
                f"{type(resolved_result).__name__}",
                flush=True,
            )
            print(
                f"[SEARCH] Resolved result: "
                f"{SearchAgent._compact_repr(resolved_result)}",
                flush=True,
            )

            if resolved_result is None:
                print("[SEARCH] Backend returned None; returning no results.", flush=True)
                return []

            return resolved_result

        except Exception as exc:
            print("\n[SEARCH] VERTEX RETRIEVAL FAILED", flush=True)
            print(f"[SEARCH] Query: {query!r}", flush=True)
            print(f"[SEARCH] Error: {type(exc).__name__}: {exc}", flush=True)
            print(traceback.format_exc(), flush=True)

            logger.exception(
                "ABB manual retrieval failed. query=%r",
                query,
            )

            raise

    @staticmethod
    def _normalize_final_output(response: Any) -> str:
        """
        Ensure Search Agent final output matches the required JSON schema.

        Required output:
        [
          {
            "source": string,
            "url": string,
            "excerpt": string,
            "confidence": number
          }
        ]

        The search tool already returns confidence, but the LLM sometimes drops it
        when producing final output. This restores confidence when missing.
        """
        raw_output = getattr(response, "final_output", response)

        if not isinstance(raw_output, str):
            raw_output = str(raw_output)

        text = raw_output.strip()

        # Remove markdown code fences if the model returns ```json ... ```
        if text.startswith("```"):
            lines = text.splitlines()

            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except Exception:
            # Keep the failure visible, but still return schema-compatible JSON.
            return json.dumps(
                [
                    {
                        "source": "",
                        "url": "",
                        "excerpt": text,
                        "confidence": 0.0,
                    }
                ],
                indent=2,
                ensure_ascii=False,
            )

        if not isinstance(data, list):
            return json.dumps(
                [
                    {
                        "source": "",
                        "url": "",
                        "excerpt": str(data),
                        "confidence": 0.0,
                    }
                ],
                indent=2,
                ensure_ascii=False,
            )

        normalized = []

        for item in data:
            if not isinstance(item, dict):
                normalized.append(
                    {
                        "source": "",
                        "url": "",
                        "excerpt": str(item),
                        "confidence": 0.0,
                    }
                )
                continue

            confidence = item.get("confidence", 1.0)

            try:
                confidence = float(confidence)
            except Exception:
                confidence = 1.0

            normalized.append(
                {
                    "source": str(item.get("source", "")),
                    "url": str(item.get("url", "")),
                    "excerpt": str(item.get("excerpt", "")),
                    "confidence": confidence,
                }
            )

        return json.dumps(normalized, indent=2, ensure_ascii=False)

    async def run(self, prompt: str) -> Any:
        """
        Preserve the original return type for compatibility with Orchestrator,
        but normalize final_output so the Search Agent always returns the
        required JSON schema.
        """
        response = await agents.Runner.run(
            self.search_agent,
            input=prompt,
        )

        normalized_output = self._normalize_final_output(response)

        try:
            response.final_output = normalized_output
            return response
        
        except Exception:
            logger.warning(
                "Failed to normalize SearchAgent final_output. "
                "Returning original response."
            )
            return response
