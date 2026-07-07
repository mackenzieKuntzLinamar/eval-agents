import inspect
import json
import logging
import os
import traceback
from typing import Any

import agents
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel  # NEW — needed for the schema classes below

from search_tool import VertexSearchTool


load_dotenv()

logger = logging.getLogger(__name__)


# NEW — these two classes define the exact shape every search result must have.
# Once passed to output_type below, the SDK enforces this shape at decode time,
# so the model can no longer silently drop "confidence" the way it was doing.
class SearchResultItem(BaseModel):
    source: str
    url: str
    excerpt: str
    confidence: float


class SearchResults(BaseModel):
    results: list[SearchResultItem]


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
1. Use the provided `knowledge_search` tool to query the database.
2. Return the most relevant technical excerpts that directly answer the query.
3. Preserve the original wording. Do not paraphrase, summarize, or add advice.

Rules:
- Always use `knowledge_search`.
- Do not answer from your own knowledge.
- Do not guess or fill in missing technical details.
- Do not include unrelated or generic text.
- If no relevant information is found, return an empty results list.
- If the search tool fails, clearly state that retrieval failed. Do not invent
  an ABB-specific answer.
""".strip(),
            model=agents.OpenAIChatCompletionsModel(
                model="gemini-2.5-flash",
                openai_client=self.client,
            ),
            model_settings=agents.ModelSettings(
                tool_choice="auto",
                temperature=0,
            ),
            tools=[self.knowledge_tool],
            output_type=SearchResults,  # NEW — this is the actual fix
        )

    @staticmethod
    def _compact_repr(value: Any, max_length: int = 4000) -> str:
        value_repr = repr(value)

        if len(value_repr) <= max_length:
            return value_repr

        return f"{value_repr[:max_length]}... <truncated>"

    @staticmethod
    async def _resolve_knowledge_result(value: Any) -> Any:
        if inspect.isasyncgen(value) or hasattr(value, "__aiter__"):
            return [item async for item in value]

        if inspect.isawaitable(value):
            return await value

        return value

    @staticmethod
    async def search_knowledgebase(query: str) -> Any:
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

    async def run(self, prompt: str) -> Any:
        """
        Preserve the original return type for compatibility with Orchestrator.

        final_output is now a SearchResults pydantic object (because of
        output_type above), not a JSON string. We overwrite final_output with
        its JSON-string form so eval scripts / anything expecting a string
        keeps working unchanged.
        """
        response = await agents.Runner.run(
            self.search_agent,
            input=prompt,
        )

        final_output = getattr(response, "final_output", None)

        # NEW — serialize the structured object back to the JSON array string
        # your eval script and downstream consumers already expect.
        if isinstance(final_output, SearchResults):
            response.final_output = json.dumps(
                [item.model_dump() for item in final_output.results],
                indent=2,
                ensure_ascii=False,
            )

        return response