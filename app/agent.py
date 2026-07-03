"""
Meeting Intelligence Agent — ADK 2.0 Workflow
Processes meeting transcripts → security check → extracts action items →
generates summaries → HITL review → final output.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import sys
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.apps import App
from google.adk.workflow import Workflow
from google.adk.tools import McpToolset
from mcp import StdioServerParameters
from google.genai import types
from pydantic import BaseModel

from .config import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Schemas
# ─────────────────────────────────────────────────────────────────────────────


class ActionItem(BaseModel):
    description: str
    owner: str
    deadline: str
    priority: str  # HIGH / MEDIUM / LOW


class TranscriptAnalysis(BaseModel):
    meeting_title: str
    date: str
    attendees: list[str]
    key_decisions: list[str]
    action_items: list[ActionItem]
    open_questions: list[str]
    next_meeting_date: str


class MeetingSummary(BaseModel):
    executive_summary: str
    action_items_formatted: str
    follow_up_email_subject: str
    follow_up_email_body: str


# ─────────────────────────────────────────────────────────────────────────────
# LLM Agents
# ─────────────────────────────────────────────────────────────────────────────

# Initialize McpToolset pointing to our custom MCP server
mcp_toolset = McpToolset(
    connection_params=StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
    )
)

transcript_analyzer = LlmAgent(
    name="transcript_analyzer",
    model=config.model,
    instruction="""You are an expert meeting analyst.
Given a meeting transcript (plain text), extract:
- meeting_title: infer a concise title from the context
- date: meeting date if mentioned, else 'Not specified'
- attendees: list of all speaker names mentioned
- key_decisions: list of decisions made during the meeting
- action_items: each with description, owner (person responsible), deadline, and priority (HIGH/MEDIUM/LOW)
- open_questions: unresolved questions raised
- next_meeting_date: if mentioned, else 'Not scheduled'

Be thorough and precise. Assign realistic owners and deadlines based on context clues.
Use directory lookup to verify spelling of attendee names if needed, and check calendar to verify date.""",
    output_schema=TranscriptAnalysis,
    output_key="transcript_analysis",
    tools=[mcp_toolset],
)

summary_agent = LlmAgent(
    name="summary_agent",
    model=config.model,
    instruction="""You are a professional business communication specialist.
Given the structured meeting analysis in state['transcript_analysis'], produce:
- executive_summary: 3-5 sentence high-level summary of the meeting
- action_items_formatted: clean Markdown list of action items with owners and deadlines
- follow_up_email_subject: concise, professional email subject line
- follow_up_email_body: full professional follow-up email body (ready to send)

The email should be warm, professional, and include all action items clearly.
Make sure to lookup user emails using directory lookup for each action item owner and create the tickets/tasks for all extracted action items.""",
    output_schema=MeetingSummary,
    output_key="meeting_summary",
    tools=[mcp_toolset],
)

# ─────────────────────────────────────────────────────────────────────────────
# Security Checkpoint Node (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

# PII patterns relevant to business meeting context
_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN_REDACTED]"),
    (re.compile(r"\b\d{16}\b"), "[CARD_REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b(?:password|passwd|secret|token|api[_\s]key)\s*[:=]\s*\S+", re.IGNORECASE), "[SECRET_REDACTED]"),
]

# Prompt injection keywords
_INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "ignore all instructions",
    "disregard your",
    "you are now",
    "act as",
    "pretend you are",
    "jailbreak",
    "dan mode",
    "developer mode",
    "system prompt",
    "override instructions",
]


def _scrub_pii(text: str) -> tuple[str, list[str]]:
    """Redact PII from text. Returns (scrubbed_text, list_of_findings)."""
    findings = []
    for pattern, replacement in _PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            findings.append(f"Redacted {len(matches)} instance(s) matching {replacement}")
            text = pattern.sub(replacement, text)
    return text, findings


def _detect_injection(text: str) -> list[str]:
    """Detect prompt injection attempts. Returns list of matched keywords."""
    lower = text.lower()
    return [kw for kw in _INJECTION_KEYWORDS if kw in lower]


def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """
    Security gate: scrubs PII, detects prompt injection, emits structured audit log.
    Routes to SECURITY_EVENT on injection detection, else passes cleaned input downstream.
    """
    # Extract raw text from Content or str
    if hasattr(node_input, "parts"):
        raw_text = " ".join(
            part.text for part in node_input.parts if hasattr(part, "text") and part.text
        )
    else:
        raw_text = str(node_input)

    # PII scrubbing
    clean_text, pii_findings = _scrub_pii(raw_text)

    # Injection detection
    injection_hits = _detect_injection(clean_text)

    # Determine severity
    if injection_hits:
        severity = "CRITICAL"
    elif pii_findings:
        severity = "WARNING"
    else:
        severity = "INFO"

    # Structured audit log
    audit = {
        "event": "security_checkpoint",
        "severity": severity,
        "pii_redacted": pii_findings,
        "injection_detected": injection_hits,
        "input_length": len(raw_text),
        "clean_length": len(clean_text),
    }
    logger.info("AUDIT_LOG: %s", json.dumps(audit))

    if injection_hits:
        logger.warning("SECURITY_EVENT: Prompt injection blocked. Keywords: %s", injection_hits)
        return Event(
            output=f"⛔ Security violation detected. Injection keywords: {injection_hits}",
            route="SECURITY_EVENT",
            state={"security_blocked": True, "audit_log": audit},
        )

    # Domain-specific rule: Validate minimum length to ensure it is a transcript
    if len(clean_text.strip()) < 20:
        severity = "WARNING"
        audit["severity"] = severity
        audit["domain_check_failed"] = "Input too short"
        logger.warning("SECURITY_EVENT: Input too short to be a valid meeting transcript.")
        return Event(
            output="⛔ Input is too short. Please submit a valid, detailed meeting transcript (minimum 20 characters).",
            route="SECURITY_EVENT",
            state={"security_blocked": True, "audit_log": audit},
        )

    return Event(
        output=clean_text,
        route="PASS",
        state={"raw_transcript": clean_text, "audit_log": audit, "security_blocked": False},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper Nodes
# ─────────────────────────────────────────────────────────────────────────────


def security_blocked_output(ctx: Context, node_input: str) -> Event:
    """Terminal node for security-blocked requests."""
    message = (
        "🚫 **Request Blocked by Security Checkpoint**\n\n"
        "Your input was flagged for potential prompt injection. "
        "Please submit a clean meeting transcript without instructions that could override system behavior."
    )
    return Event(
        output=message,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=message)],
        ),
    )


def prepare_transcript(ctx: Context, node_input: str) -> Event:
    """Pass the cleaned transcript to the analyzer with proper state."""
    return Event(
        output=node_input,
        state={"transcript_text": node_input},
    )


async def human_review_gate(ctx: Context, node_input: Any) -> Any:
    """
    HITL: Pause and ask the user to confirm the analysis before generating summary.
    """
    analysis_raw = ctx.state.get("transcript_analysis")
    if not analysis_raw:
        # No analysis yet — shouldn't happen, but handle gracefully
        yield Event(output=node_input, route="APPROVED")
        return

    # Parse stored analysis
    if isinstance(analysis_raw, dict):
        analysis = TranscriptAnalysis(**analysis_raw)
    else:
        analysis = analysis_raw

    # Format action items for display
    action_lines = "\n".join(
        f"  • [{item.priority}] {item.description} → **{item.owner}** by {item.deadline}"
        for item in analysis.action_items
    )

    review_message = (
        f"📋 **Meeting Analysis Ready for Review**\n\n"
        f"**Title:** {analysis.meeting_title}\n"
        f"**Date:** {analysis.date}\n"
        f"**Attendees:** {', '.join(analysis.attendees)}\n\n"
        f"**Key Decisions:**\n"
        + "\n".join(f"  • {d}" for d in analysis.key_decisions)
        + f"\n\n**Action Items ({len(analysis.action_items)}):**\n{action_lines}\n\n"
        f"**Open Questions:** {len(analysis.open_questions)}\n\n"
        f"Type **approve** to generate the full summary & email, or **edit** followed by corrections."
    )

    if not ctx.resume_inputs:
        yield RequestInput(
            interrupt_id="human_review",
            message=review_message,
        )
        return

    user_response = str(ctx.resume_inputs.get("human_review", "")).lower().strip()
    if user_response.startswith("approve") or user_response == "yes":
        yield Event(output=node_input, route="APPROVED")
    else:
        # User wants to provide corrections — store their feedback and re-route
        yield Event(
            output=user_response,
            route="APPROVED",  # Proceed with original analysis; user note stored
            state={"user_review_note": user_response},
        )


def format_final_output(ctx: Context, node_input: Any) -> Event:
    """Combine analysis + summary into the final user-facing response."""
    analysis_raw = ctx.state.get("transcript_analysis", {})
    summary_raw = ctx.state.get("meeting_summary", {})

    if isinstance(analysis_raw, dict):
        analysis = TranscriptAnalysis(**analysis_raw) if analysis_raw else None
    else:
        analysis = analysis_raw

    if isinstance(summary_raw, dict):
        summary = MeetingSummary(**summary_raw) if summary_raw else None
    else:
        summary = summary_raw

    audit = ctx.state.get("audit_log", {})
    security_note = (
        f"\n\n> 🔒 **Security:** {len(audit.get('pii_redacted', []))} PII item(s) redacted from transcript."
        if audit.get("pii_redacted")
        else "\n\n> 🔒 **Security:** No PII detected in transcript."
    )

    output_parts = ["# 📝 Meeting Intelligence Report\n"]

    if summary:
        output_parts.append(f"## Executive Summary\n{summary.executive_summary}\n")
        output_parts.append(f"## Action Items\n{summary.action_items_formatted}\n")
        output_parts.append(
            f"## Follow-Up Email\n"
            f"**Subject:** {summary.follow_up_email_subject}\n\n"
            f"---\n{summary.follow_up_email_body}\n---"
        )

    if analysis:
        output_parts.append(
            f"\n## Meeting Details\n"
            f"- **Title:** {analysis.meeting_title}\n"
            f"- **Date:** {analysis.date}\n"
            f"- **Next Meeting:** {analysis.next_meeting_date}\n"
            f"- **Open Questions:** {len(analysis.open_questions)}\n"
        )
        if analysis.open_questions:
            output_parts.append(
                "### Open Questions\n"
                + "\n".join(f"- {q}" for q in analysis.open_questions)
            )

    output_parts.append(security_note)

    final_text = "\n".join(output_parts)

    return Event(
        output=final_text,
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=final_text)],
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow Graph
# ─────────────────────────────────────────────────────────────────────────────

root_agent = Workflow(
    name="meeting_intelligence_agent",
    description=(
        "Processes meeting transcripts: security screening → transcript analysis → "
        "human review → summary generation → formatted output with follow-up email."
    ),
    edges=[
        # Entry: security gate
        ("START", security_checkpoint),
        # Route security check result
        (security_checkpoint, {"SECURITY_EVENT": security_blocked_output, "PASS": prepare_transcript}),
        # LLM: extract action items, decisions, owners
        (prepare_transcript, transcript_analyzer),
        # HITL: human reviews the analysis
        (transcript_analyzer, human_review_gate),
        # After approval: generate summary + email
        (human_review_gate, {"APPROVED": summary_agent}),
        # Format and display final output
        (summary_agent, format_final_output),
    ],
)

app = App(
    name="app",
    root_agent=root_agent,
)
