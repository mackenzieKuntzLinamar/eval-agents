import os

import agents
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent
from search_agent import SearchAgent
from workorder_agent import WorkorderAgent


load_dotenv()


class Orchestrator:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))

        search_agent_instance = SearchAgent()
        self.search_agent_tool = search_agent_instance.search_agent.as_tool(
            tool_name="search_knowledge_base",
            tool_description="Agent searches ABB robot manuals for repair, troubleshooting, maintenance instructions, etc.",
        )

        workorder_agent_instance = WorkorderAgent()
        self.workorder_agent_tool = workorder_agent_instance.workorder_agent.as_tool(
            tool_name="workorder_agent",
            tool_description="Given a conversation between the user and the orchestrator agent, the workorder agent will create a workorder.",
        )

        self.search_agent_tool = search_agent_instance.search_agent.as_tool(
            tool_name="search_knowledge_base",
            tool_description="Agent searches ABB robot manuals for repair, troubleshooting, maintenance instructions, etc.",
        )

        self.main_agent = agents.Agent(
            name="Orchestrator Agent",
            instructions="""
                            You are a helpful ABB support assistant.
                            First, decide whether the user's question could be related to ABB robots, ABB manuals, repair, troubleshooting, maintenance, or other ABB support topics.
                            If it is clearly out of scope, respond briefly with an out-of-scope message and do not use any tools.
                            If it is in scope, use the search tool exactly once to gather the best supporting information, then compose the final answer from those retrieved results.
                            Synthesize the evidence into a direct diagnosis or explanation, followed by 2–4 concise troubleshooting steps if helpful.
                            Do not simply repeat the retrieved excerpts or list raw search results as the answer.
                            Do not call the search tool repeatedly. Only use a second search if the first result is empty or clearly insufficient.
                            Do not ask clarifying questions unless the request is genuinely ambiguous.
                            Do not include long preambles, repeated restatements, or unnecessary internal reasoning.
                            When you used the search tool, present the most relevant findings at the bottom of your answer inside a short collapsible section.
                            If the user asks you to create a workorder, then call the workorder_agent.
                            """,
            model=agents.OpenAIChatCompletionsModel(model="gemini-2.5-pro", openai_client=self.client),
            model_settings=agents.ModelSettings(tool_choice="auto", temperature=0.5),
            tools=[self.search_agent_tool, self.workorder_agent_tool],
        )

    async def run(self, prompt: str, history) -> str:
        context = ""

        # reconstruct conversation history into a string
        if history:
            for i in range(0, len(history), 2):
                if i + 1 < len(history):
                    user_msg = history[i]["content"]
                    bot_msg = history[i + 1]["content"]
                    context += f"User: {user_msg}\nAssistant: {bot_msg}\n"

        # combine history with the new prompt
        full_prompt = f"{context} User: {prompt}"

        result_stream = agents.Runner.run_streamed(self.main_agent, input=full_prompt)
        async for event in result_stream.stream_events():
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                print(event.data.delta, end="", flush=True)
                yield event.data.delta
