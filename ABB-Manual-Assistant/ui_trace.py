"""Gradio chat UI with per-turn Langfuse tracing."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import gradio as gr
from agent_utils.memory_store import MemoryStore
from conversation_manager import ConversationManagerAgent
from orchestrator_agent import Orchestrator
from utils import setup_langfuse_tracer
from utils.langfuse.shared_client import flush_langfuse, langfuse_client


store = MemoryStore()
conv_agent = ConversationManagerAgent(store)


def _label_for(id_: str, title: str) -> str:
    return f"{title} - {id_[:6]}"


def _choices_and_maps():
    rows = store.list_conversations()
    if not rows:
        store.create_conversation()
        rows = store.list_conversations()

    choices = [_label_for(r[0], r[1]) for r in rows]
    id_by_label = {_label_for(r[0], r[1]): r[0] for r in rows}
    label_by_id = {r[0]: _label_for(r[0], r[1]) for r in rows}
    return choices, id_by_label, label_by_id, choices[0]


def _history_messages(cid: str):
    rows = conv_agent.get_history_messages(cid, limit=1000)
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _get_current_trace_id() -> str | None:
    if langfuse_client is None:
        return None
    return langfuse_client.get_current_trace_id()


class GradioTraceApp:
    """Run the ABB chat UI and attach Langfuse spans per user turn."""

    def __init__(self):
        self.orchestrator = Orchestrator()

    async def run_search_stream(self, message: str, chat_history):
        """Stream orchestrator output chunks for one user message."""
        async for chunk in self.orchestrator.run(message, chat_history):
            yield chunk

    def launch(self):
        """Build and launch the traced Gradio UI."""
        with gr.Blocks(title="ABB Knowledgebase Search (Traced)") as demo:
            gr.Markdown("### ABB Knowledgebase Search - Multi-Chat (Langfuse traced)")

            state = gr.State({})

            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    gr.Markdown("**Chats**")
                    chat_dropdown = gr.Dropdown(
                        choices=[],
                        value=None,
                        label="Select chat",
                        interactive=True,
                    )
                    new_title = gr.Textbox(label="New chat title", placeholder="Optional (auto if blank)")
                    btn_new = gr.Button("New chat")

                    rename_to = gr.Textbox(label="Rename to")
                    btn_rename = gr.Button("Rename")

                    btn_delete = gr.Button("Delete chat")
                    btn_refresh = gr.Button("Refresh list")

                with gr.Column(scale=3):
                    chatbot = gr.Chatbot(label="Chat", height=520)
                    with gr.Row():
                        msg = gr.Textbox(
                            placeholder="Ask me something about ABB errors...",
                            scale=9,
                            label="Your Question",
                            lines=1,
                        )
                        send = gr.Button("Send", variant="primary", scale=1)

            examples = [
                "What is Error code 10039 and possible solution?",
                "What is a reference error?",
                "We are getting a Motor phase short circuit. Where should we look?",
                "How to set up an IRC5, and what is it?",
            ]
            gr.Examples(examples=examples, inputs=msg, label="Try one of these example questions:")

            def _init():
                choices, id_by_label, _, default_label = _choices_and_maps()
                cid = id_by_label[default_label]
                return (
                    gr.update(choices=choices, value=default_label),
                    {"conversation_id": cid},
                    _history_messages(cid),
                )

            demo.load(_init, inputs=None, outputs=[chat_dropdown, state, chatbot])

            def _select_chat(label, current_state):
                choices, id_by_label, _, default_label = _choices_and_maps()
                if not label or label not in id_by_label:
                    label = default_label
                cid = id_by_label[label]
                current_state = current_state or {}
                current_state["conversation_id"] = cid
                return current_state, _history_messages(cid)

            chat_dropdown.change(_select_chat, inputs=[chat_dropdown, state], outputs=[state, chatbot])

            async def _send(user_text, current_state, current_msgs):
                if not user_text or not user_text.strip():
                    yield current_msgs, "", (current_state or {})
                    return

                choices, id_by_label, _, default_label = _choices_and_maps()
                cid = (current_state or {}).get("conversation_id") or id_by_label[default_label]
                current_state = {"conversation_id": cid}

                conv_agent.save_user(cid, user_text)
                messages = _history_messages(cid)
                messages.append({"role": "user", "content": user_text})
                chat_history = messages

                messages.append({"role": "assistant", "content": "..."})
                yield messages, "", current_state

                partial = ""
                trace_context: Any = nullcontext(None)
                if langfuse_client is not None:
                    trace_context = langfuse_client.start_as_current_span(
                        name="abb_ui_chat_turn",
                        input={
                            "conversation_id": cid,
                            "user_message": user_text,
                            "history_message_count": len(chat_history),
                        },
                        metadata={
                            "surface": "gradio_ui",
                            "script": "ui_trace.py",
                        },
                    )

                trace_span = None
                try:
                    with trace_context as trace_span:
                        async for chunk in self.run_search_stream(user_text, chat_history):
                            partial += chunk
                            messages[-1]["content"] = partial
                            conv_agent.set_assistant_partial(cid, partial)
                            yield messages, "", current_state

                        trace_id = _get_current_trace_id()
                        if trace_span is not None:
                            trace_span.update(
                                output={
                                    "assistant_response": partial,
                                    "trace_id": trace_id,
                                }
                            )
                        if trace_id:
                            print(f"Langfuse trace id: {trace_id}", flush=True)

                except Exception as exc:
                    if trace_span is not None:
                        trace_span.update(
                            output={
                                "partial_response": partial,
                                "error": f"{type(exc).__name__}: {exc}",
                                "trace_id": _get_current_trace_id(),
                            },
                            level="ERROR",
                            status_message=f"{type(exc).__name__}: {exc}",
                        )
                    raise
                finally:
                    conv_agent.finalize_assistant(cid)
                    messages = _history_messages(cid)
                    yield messages, "", current_state

            send.click(_send, inputs=[msg, state, chatbot], outputs=[chatbot, msg, state])
            msg.submit(_send, inputs=[msg, state, chatbot], outputs=[chatbot, msg, state])

            def _new_chat(title):
                res = conv_agent.create(title or None)
                cid = res["conversation_id"]
                choices, _, label_by_id, _ = _choices_and_maps()
                label = label_by_id[cid]
                return (
                    gr.update(choices=choices, value=label),
                    {"conversation_id": cid},
                    _history_messages(cid),
                )

            btn_new.click(_new_chat, inputs=new_title, outputs=[chat_dropdown, state, chatbot])

            def _rename_chat(new_title, current_state):
                choices, id_by_label, _, default_label = _choices_and_maps()
                cid = (current_state or {}).get("conversation_id") or id_by_label[default_label]
                if new_title and new_title.strip():
                    conv_agent.rename(cid, new_title.strip())
                choices, _, label_by_id, _ = _choices_and_maps()
                label = label_by_id[cid]
                return gr.update(choices=choices, value=label)

            btn_rename.click(_rename_chat, inputs=rename_to, outputs=chat_dropdown)

            def _delete_chat(current_state):
                choices, id_by_label, _, default_label = _choices_and_maps()
                cid = (current_state or {}).get("conversation_id") or id_by_label[default_label]
                conv_agent.delete(cid)

                choices, id_by_label, _, default_label = _choices_and_maps()
                new_cid = id_by_label[default_label]
                return (
                    gr.update(choices=choices, value=default_label),
                    {"conversation_id": new_cid},
                    _history_messages(new_cid),
                )

            btn_delete.click(_delete_chat, inputs=state, outputs=[chat_dropdown, state, chatbot])

            def _refresh_list(current_state):
                choices, _, label_by_id, default_label = _choices_and_maps()
                cid = (current_state or {}).get("conversation_id")
                label = label_by_id[cid] if cid and cid in label_by_id else default_label
                return gr.update(choices=choices, value=label)

            btn_refresh.click(_refresh_list, inputs=state, outputs=chat_dropdown)

        demo.launch(server_name="0.0.0.0", share=True)


def main():
    """Start the traced UI and flush pending Langfuse events on shutdown."""
    setup_langfuse_tracer()
    app = GradioTraceApp()
    try:
        app.launch()
    finally:
        flush_langfuse()


if __name__ == "__main__":
    main()
