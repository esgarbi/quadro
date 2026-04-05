"""
Pydantic models for the LLM newsroom pipeline.

Each model represents one agent's structured output:
  ArticleBrief      — ideation agent output
  ResearchStrategy  — research strategy planner output
  Citation          — a single PubMed citation
  ResearchOutput    — full research agent output (strategy + citations)
  ReviewDecision    — review agent decision
"""

from __future__ import annotations

from typing import Annotated
from pydantic import BaseModel, Field


class ArticleBrief(BaseModel):
    title: str = Field(description="Specific, concrete, clickable headline")
    primary_category: str = Field(description="Primary health category")
    keywords: list[str] = Field(description="3-5 scientific keywords")
    lead: str = Field(description="1-2 sentence hook")
    thesis: str = Field(
        description="2-3 sentence summary of what the reader will learn"
    )
    sections: list[str] = Field(description="4-6 ordered section titles")
    writer: str = Field(description="Full name from the writer roster")
    writer_rationale: str = Field(description="One sentence explaining the assignment")
    abstract: str = Field(description="80-120 words in the writer's voice")
    research_keywords: list[str] = Field(
        description="3-5 PubMed-optimised search terms"
    )

    def slug(self) -> str:
        """URL-safe filename stem derived from the title."""
        import re

        s = self.title.lower()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_-]+", "-", s)
        return s[:80].strip("-")


class ResearchStrategy(BaseModel):
    core_concepts: list[dict] = Field(
        description="List of concepts with scientific_terms and consumer_terms"
    )
    pubmed_queries: list[str] = Field(description="3-5 PubMed query strings")
    gap_angle: str = Field(description="Non-obvious research thread")
    suggested_filters: dict = Field(
        description="date_range, study_types, exclude_terms"
    )


class Citation(BaseModel):
    pmid: str = Field(default="", description="PubMed ID")
    title: str = Field(description="Paper title")
    authors: str = Field(description="Author list (e.g. Smith J, Jones K)")
    year: int = Field(description="Publication year")
    journal: str = Field(default="", description="Journal name")
    abstract: str = Field(default="", description="Paper abstract or summary")


class ResearchOutput(BaseModel):
    strategy: ResearchStrategy
    citations: list[Citation] = Field(description="Curated list of relevant citations")
    summary: str = Field(description="1-2 sentence overview of the research findings")


class ReviewDecision(BaseModel):
    approved: bool
    reason: str = Field(default="", description="Reason for revision if not approved")


class ApprovedOutput(BaseModel):
    approved: bool
    reason: str = Field(default="", description="Reason for approval or revision")
