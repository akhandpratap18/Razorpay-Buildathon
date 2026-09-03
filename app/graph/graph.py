"""Recoup — Full LangGraph StateGraph definition."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.diagnostic import diagnostic_agent
from app.graph.nodes.killswitch import killswitch_node
from app.graph.nodes.router import get_next_node, router_node
from app.graph.nodes.send_email import send_email_node
from app.graph.nodes.verification import get_final_route, verification_node
from app.graph.state import RecoveryState
from app.webhook.models import RecoveryStatus


def _escalate_node(state: RecoveryState) -> RecoveryState:
    """Human escalation terminal node."""
    return {**state, "status": RecoveryStatus.ESCALATED}


def _success_node(state: RecoveryState) -> RecoveryState:
    """Payment recovered terminal node."""
    return {**state, "status": RecoveryStatus.RECOVERED}


def build_graph() -> Any:
    """Construct and compile the Recoup recovery StateGraph."""
    graph = StateGraph(RecoveryState)

    graph.add_node("diagnostic", diagnostic_agent)
    graph.add_node("router", router_node)
    graph.add_node("send_email", send_email_node)
    graph.add_node("killswitch", killswitch_node)
    graph.add_node("escalate", _escalate_node)
    graph.add_node("verification", verification_node)
    graph.add_node("success", _success_node)

    graph.add_edge(START, "diagnostic")
    graph.add_edge("diagnostic", "router")

    graph.add_conditional_edges(
        "router",
        get_next_node,
        {
            "send_email": "send_email",
            "killswitch": "killswitch",
            "escalate": "escalate",
        },
    )

    graph.add_edge("send_email", "verification")
    graph.add_edge("killswitch", END)
    graph.add_edge("escalate", END)

    graph.add_conditional_edges(
        "verification",
        get_final_route,
        {
            "success": "success",
            "terminal": END,
            "retry": END,
        },
    )

    graph.add_edge("success", END)
    return graph


recovery_graph = build_graph().compile()
