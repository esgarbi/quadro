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

from pydantic import BaseModel, ConfigDict, Field


class Headline(BaseModel):
    """A single proposed article headline.

    Used by the ideation saga's first reason step. The model is
    intentionally minimal — the full ArticleBrief is produced by a
    separate reason step that takes this headline as input.
    """

    headline: str = Field(
        description="One concrete, specific, clickable article headline"
    )


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


class CoreConcept(BaseModel):
    """One decomposition of the article topic.

    Matches the ``core_concepts[]`` item shape that
    ``prompts/research.md`` asks the LLM to produce: a short label
    plus a pair of synonym lists (MeSH-style scientific terms and
    consumer-friendly terms).

    ``extra="forbid"`` plus explicitly-required fields are both
    required for OpenAI's strict structured-output mode. Without them
    the generated JSON schema omits ``additionalProperties: false`` on
    each list item, which trips the 400 Bad Request that the research
    saga's ``plan_strategy`` step previously hit on every task.
    """

    model_config = ConfigDict(extra="forbid")

    concept: str = Field(description="Short label for the concept")
    scientific_terms: list[str] = Field(
        description="MeSH-style scientific synonyms"
    )
    consumer_terms: list[str] = Field(
        description="Consumer-friendly synonyms"
    )


class SuggestedFilters(BaseModel):
    """PubMed search filters the research prompt asks for.

    All fields are required lists / strings (no defaults) so the
    generated schema is OpenAI-strict compatible. The LLM is free to
    return empty strings / empty lists where it has nothing useful to
    suggest; that preserves the fallback behaviour the research saga
    already handles downstream.
    """

    model_config = ConfigDict(extra="forbid")

    date_range: str = Field(description="e.g. 2020-2024")
    study_types: list[str] = Field(
        description="Study type filters — meta-analysis, RCT, systematic review, etc."
    )
    exclude_terms: list[str] = Field(
        description="Terms to exclude to reduce noise"
    )


class ResearchStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_concepts: list[CoreConcept] = Field(
        description="2-4 decomposed concepts with scientific + consumer synonyms"
    )
    pubmed_queries: list[str] = Field(description="3-5 PubMed query strings")
    gap_angle: str = Field(description="Non-obvious research thread")
    suggested_filters: SuggestedFilters = Field(
        description="Filters to narrow results to high-quality, recent studies"
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
