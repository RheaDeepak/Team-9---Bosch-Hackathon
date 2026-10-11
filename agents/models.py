"""Typed inputs and outputs shared by the LangGraph workflows."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningInput(StrictModel):
    goal: str | None = None


class ToolBudgets(StrictModel):
    planning: int = Field(default=8, ge=1)
    analysis: int = Field(default=10, ge=1)
    recursion_limit: int = Field(default=34, ge=1)


class AgentRunMetadata(StrictModel):
    run_id: str
    mode: Literal["planning", "analysis"]
    prompt_version: str
    status: str
    model: str | None = None
    source: str | None = None
    started_at: str
    completed_at: str | None = None


class Prediction(StrictModel):
    svc: str
    metric: Literal["error_rate", "p95_ms"]
    direction: Literal["up", "down", "flat"]
    min_factor: float = Field(ge=0)


class EvidenceNote(StrictModel):
    tool: str
    finding: str


class CandidateAdjustment(StrictModel):
    candidate_id: str
    adjusted_risk: float = Field(ge=0, le=1)
    adjusted_priority: float
    evidence: list[EvidenceNote] = Field(default_factory=list)
    reason: str


class PlanningDecision(StrictModel):
    candidate_id: str
    hypothesis: str = Field(min_length=1)
    predictions: list[Prediction] = Field(min_length=1)
    adjusted_ranking: list[CandidateAdjustment] = Field(default_factory=list)


class FindingDecision(StrictModel):
    id: str
    title: str
    service: str
    edge: str | None = None
    pattern: str
    severity: Literal["Critical", "High", "Medium", "Low"]
    severity_score: float = Field(ge=0, le=100)
    root_cause: str
    evidence_ids: list[str] = Field(min_length=1)
    fix: str
    fix_location: str | None = None
    verify_by: str
    confidence: float = Field(ge=0, le=1)
    agent_adjustment: str | None = None


class AnalysisDecision(StrictModel):
    findings: list[FindingDecision] = Field(min_length=1)
    strengths: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class HypothesisClaim(StrictModel):
    claim: str
    status: Literal["supported", "contradicted", "no data"]
    evidence: str


class HypothesisVerdict(StrictModel):
    claims: list[HypothesisClaim] = Field(default_factory=list)
    verdict: Literal["held", "partially held", "not held", "inconclusive"]
    explanation: str
    recommendation: str


class AgentEvent(StrictModel):
    run_id: str
    mode: Literal["planning", "analysis"]
    sequence: int
    node: str | None = None
    type: str
    tool: str | None = None
    status: str | None = None
    model: str | None = None
    usage: dict | None = None
    summary: str
    detail: str | None = None
