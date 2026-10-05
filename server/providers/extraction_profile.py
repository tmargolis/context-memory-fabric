"""Entity-extraction profiles for Graphiti's add_episode() (MS4e).

Every CMF write into the episodic graph goes through Graphiti's
extract_nodes / extract_edges prompts, and the only levers CMF has over what
comes out are the kwargs it passes: `custom_extraction_instructions`,
`entity_types` and `excluded_entity_types`. This module owns all three, so
remember() and correct_memory() extract under the same rules (before MS4e,
correction.py passed none of them and corrected episodes quietly used
Graphiti's bare defaults).

Profiles, selected by CMF_EXTRACTION_PROFILE (server.core.config):

- "legacy": the pre-MS4e instructions, verbatim. Tuned in Spark Phase 7 for
  terse ~200-char episodes where qwen3.5-122b returned an empty entity list
  ~45% of the time, so it pushes hard the other way ("extract every specific
  named entity ... files and path patterns ... settings or parameters").
  On today's longer, thread-merged episodes that push is the main source of
  entity noise: 79% of jspace's 398 entities were mentioned by exactly one
  episode, most of them file names, identifiers and numbers (MS4e, measured
  2026-09-28).
- "selective": instructions that ask for durable, re-findable things only
  and list the debris to skip. No ontology.
- "typed": "selective" plus an ontology, with Graphiti's generic `Entity`
  type excluded, so anything the model can't place in a real type is
  dropped rather than saved.
- "typed-recall": the MS4e Phase 3 follow-up. The A/B showed the ontology
  is what filters debris, while "selective"'s cautious wording made qwen
  return nothing on long episodes (typed found 36 of 57 reference entities,
  legacy 50). So this keeps a wider ontology as the filter, adding Format and
  Topic per the user's review, and asks for coverage within it. It also names
  the episode's own project, which Graphiti's text prompt never shows the
  model (source_description carries it as `project=<bucket>`).

The ontology's type names become FalkorDB labels on the saved nodes
(alongside `Entity`), so none may collide with a label CMF already uses:
`Project` in particular is CMF's own `(:Project)` node that IN_PROJECT edges
point at (server.consolidation.graph_tagging), which is why a project-like
entity is a `Workstream` here. Types are docstring-only models: a field would
make Graphiti run an attribute-extraction LLM call per node, and a field
named like an EntityNode attribute (name, summary, ...) is rejected outright.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel

from server.core.config import (
    LEGACY_EXTRACTION,
    SELECTIVE_EXTRACTION,
    TYPED_EXTRACTION,
    TYPED_RECALL_EXTRACTION,
    VALID_EXTRACTION_PROFILES,
    extraction_profile_from_env,
)
from server.review.projects import project_display_name

# Verbatim pre-MS4e text (was memory_graphiti.EXTRACTION_INSTRUCTIONS) — see
# docs/spark-phase7-ab-log.md §4c for why it was written this way.
LEGACY_INSTRUCTIONS = (
    "The CURRENT MESSAGE is a concise, deliberately terse third-person summary of a single "
    "decision, plan, finding, or troubleshooting outcome. It still names concrete things. "
    "Extract every specific named entity it references — tools, software, files and path "
    "patterns, projects, people, places, hardware, product names, settings or parameters, "
    "artifacts, techniques, organizations — even when mentioned only briefly or in passing. "
    "Returning an empty entity list should be rare, only when the summary genuinely names "
    "nothing concrete. Do NOT extract the narrator (\"the user\", \"the assistant\") as an entity."
)

# The keep/drop examples are the user's own, from reviewing jspace's entities
# (2026-09-28); tests/fixtures/ms4e/entity_gold.json holds the same lists
# for scoring. `remember()` sends EpisodeType.text, whose prompt labels the
# input <TEXT> — the legacy text's "CURRENT MESSAGE" was the message prompt's
# wording.
SELECTIVE_INSTRUCTIONS = (
    "The TEXT is a third-person summary of work from a personal knowledge log: decisions, plans, "
    "findings and troubleshooting, sometimes followed by 'Driving question:' and 'Reasoning:' lines. "
    "It is later searched to answer questions like 'what did I decide about X' across months of "
    "sessions, so extract only entities someone would plausibly search for again in a DIFFERENT "
    "session or project: people; organizations and companies; products, software, apps, services "
    "and platforms; AI models and model families; hardware and devices; named projects; places; "
    "and named methods, techniques or research ideas that matter beyond this one task.\n"
    "Do NOT extract implementation debris, even when it is named precisely:\n"
    "- file names, scripts and paths (build_traces20.py, qa_dump.json, answers.json)\n"
    "- functions, variables, config keys, CLI flags, env vars, code identifiers "
    "(head_idx, defaultK, sys.path, place() function, run_qa_local)\n"
    "- exception and error names (ModuleNotFoundError)\n"
    "- numbers, measurements, sizes, durations, counts, limits, PIDs, IPs, ports, "
    "indices, layers and steps (layer 40, layer 29-40, step-0, 317MB, 3x call limit multiplier)\n"
    "- labels local to one discussion (Option C, Phase 7, scores, structure)\n"
    "- UI details and styling (arrowhead size, cloud token position)\n"
    "- data encodings and formats used in passing (base64 data URIs, UTF-8)\n"
    "- generic nouns, fragments, quoted words and punctuation (asterisk, 'Depends', renderer)\n"
    "Good entities look like: gemini-2.5-flash, Mac Pro, NAS, NVIDIA Spark, J-Space, Claude, "
    "Google Docs, Alex, labeling tool, open-source language model.\n"
    "A short list of durable entities is better than a long list padded with details; returning "
    "no entities is fine when the text names nothing durable. Do NOT extract the narrator "
    "(\"the user\", \"the assistant\") as an entity."
)


class Person(BaseModel):
    """A specific, named human being: a collaborator, contact, author or public figure. Not a role
    ("the reviewer"), a group of people, or the narrator ("the user", "the assistant")."""


class Organization(BaseModel):
    """A named company, institution, team, community or government body (e.g. Google, NVIDIA,
    Anthropic, a university lab)."""


class Software(BaseModel):
    """A named software product, application, library, framework, service, platform, protocol or
    online account (e.g. Google Docs, FalkorDB, Tailscale, Obsidian, Hugging Face, a Google
    account). Also a tool the user built, when it is referred to as a lasting thing (e.g. a
    labeling tool). Not a single file, script, function, variable or config key."""


class AIModel(BaseModel):
    """A named AI or machine-learning model, model family or model variant (e.g. Claude,
    gemini-2.5-flash, flash-lite variants, qwen3.5-122b, an open-source language model)."""


class Hardware(BaseModel):
    """A named physical device, computer, component or piece of equipment (e.g. Mac Pro, NAS,
    NVIDIA Spark, a camera or telescope model, a phone)."""


class Workstream(BaseModel):
    """A named project, codebase, product effort, study or ongoing initiative the user works on
    (e.g. J-Space, nanospark, Context Memory Fabric). Not a milestone, phase, step or option
    inside one ("Phase 7", "Option C")."""


class Place(BaseModel):
    """A named geographic location, venue, astronomical/celestial body or physical site (a city, gallery, observatory, home, celestial targets like Sun, Moon)."""


class Method(BaseModel):
    """A named technique, algorithm, research method or field that matters beyond a single task
    and could recur across projects (e.g. logit lens, sparse autoencoders, astrophotography
    stacking). Not a one-off implementation step, parameter or setting."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Organization": Organization,
    "Software": Software,
    "AIModel": AIModel,
    "Hardware": Hardware,
    "Workstream": Workstream,
    "Place": Place,
    "Method": Method,
}

# typed-recall (MS4e v2). the user's review of the Phase 3 replay (2026-09-28):
# techniques, formats and generic roles/organizations are entities; numbers,
# files, identifiers and local labels are not. Keep examples for the prompt
# and docstrings come from that review.
RECALL_INSTRUCTIONS = (
    "The TEXT is a third-person summary of work from a personal knowledge log: decisions, plans, "
    "findings and troubleshooting, often followed by 'Driving question:' and 'Reasoning:' lines. "
    "Extract EVERY entity in it that fits one of the entity types, including ones mentioned only in "
    "passing or only in the Reasoning line: people and roles, organizations and groups, software, "
    "libraries and services, AI models, hardware, projects and named works, places, techniques and "
    "methods, file and data formats, and the one or two topics the whole text is about. Look inside file names for "
    "real entities (uncertainty_fake_gemma-4-12b-it.jsonl names the model gemma-4-12b-it) but never "
    "extract the file name itself.\n"
    "Use the plain canonical name: no version numbers (Photoshop, not Photoshop v27) and no generic "
    "suffixes (J-Space, not J-Space project). Extract each thing once: GitHub, not also 'GitHub "
    "connector' or 'GitHub integration'.\n"
    "Never extract:\n"
    "- file names, scripts or paths (build_traces20.py, qa_dump.json, jspace20.js)\n"
    "- functions, variables, fields, config keys, CLI flags, env vars (head_idx, defaultK, top_probs, sys.path)\n"
    "- exception and error names (ModuleNotFoundError)\n"
    "- numbers, measurements, sizes, durations, counts, PIDs, IPs, ports, layers, steps "
    "(50 episodes, layer 40, 317MB, PID 12955, port 8000)\n"
    "- labels local to one discussion (Option C, Phase 6, Phase 6 step 1, step-0)\n"
    "- UI details and process narration (arrowhead size, progress bar, timed test)\n"
    "- quoted words and sentence fragments ('Depends', 'There')\n"
    "Do NOT extract the narrator (\"the user\", \"the assistant\") as an entity."
)


class PersonOrRole(BaseModel):
    """A specific person (Alex Finn) or a role a real person plays in the user's life (board
    director, property manager, staff, neighbor). Never the narrator ("the user", "the assistant"),
    and never a name that only appears as sample data inside the work."""


class OrganizationOrGroup(BaseModel):
    """A company, institution, team, community, board or committee (Google, NVIDIA, condo board,
    the board of an HOA)."""


class SoftwareOrService(BaseModel):
    """A software product, app, operating system, library, framework, service, platform, online
    account or web technology (Google Docs, BBEdit, Photoshop, macOS, FalkorDB, torch, transformers,
    uvicorn, node-cron, Hugging Face, a Google account, CSP, CORS, DOM, canvas), or a tool the user
    built and refers to as a lasting thing (labeling tool). A file, script, dataset id, field or
    variable name is NEVER Software."""


class Format(BaseModel):
    """A file or data format or encoding (JSON, Markdown, parquet, safetensors, GIF, HTML, CSV,
    base64). The format itself, never a particular file."""


class Topic(BaseModel):
    """The overall subject of the whole piece of work, in general terms (artwork, 3D, EV charging
    station, collaborative research studio, interpretability, star trails). At most TWO per text,
    and only what the text as a whole is about. NEVER a data field, metric, variable, internal
    mechanism, quantity, UI element, step, setting or implementation concept (embedding vectors,
    attention weights, entropy, per-layer opacity, ground truth answers)."""


class TechniqueOrMethod(BaseModel):
    """A technique, algorithm, research method or practice (occlusion, counterfactuals, k-means,
    PCA, MDS, teacher-forcing, logit lens). Not a one-off step, parameter or setting."""


class ProjectOrWork(BaseModel):
    """A project, codebase, product effort, study or named creative work the user works on
    (J-Space, Career Navigator, nanospark, Thought Trails, Unsaid). Not a milestone, phase, step or
    option inside one ("Phase 6", "Option C"), and not a document, README, test run, trace type or
    data type inside a project (README, Trace Schema, Key Questions, local model test)."""


class RecallAIModel(BaseModel):
    """An AI or machine-learning model, model family or variant, including one named only by a size
    or variant code (Claude, gemini-2.5-flash, qwen3.5-122b, Gemma, E4B, gemma-4-12b-it). Never
    Hardware."""


class RecallHardware(BaseModel):
    """A physical device, computer, component or piece of equipment (Mac Pro, NAS, NVIDIA Spark,
    Galaxy Tab, a camera or telescope). Never software, an operating system, a web technology, a
    model, or a form-factor word like 'mobile'."""


class RecallPlace(BaseModel):
    """A real geographic location, venue, astronomical/celestial body or physical site (a city,
    gallery, observatory, home, celestial targets like Sun, Moon). Never a word used to describe a
    style or design ('a gallery/lab treatment')."""


RECALL_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": PersonOrRole,
    "Organization": OrganizationOrGroup,
    "Software": SoftwareOrService,
    "AIModel": RecallAIModel,
    "Hardware": RecallHardware,
    "Workstream": ProjectOrWork,
    "Place": RecallPlace,
    "Method": TechniqueOrMethod,
    "Format": Format,
    "Topic": Topic,
}

_PROJECT_IN_SOURCE = re.compile(r"(?:^|\|)\s*project=([^|\s]+)")


def project_entity_name(source_description: Optional[str]) -> Optional[str]:
    """The display name of the project in an episode's `project=<bucket>`, or None."""
    m = _PROJECT_IN_SOURCE.search(source_description or "")
    return project_display_name(m.group(1)) if m else None


def _project_instruction(source_description: Optional[str]) -> str:
    name = project_entity_name(source_description)
    if not name:
        return ""
    return (
        f"\nThis TEXT comes from the user's '{name}' project. Always extract '{name}' as an entity "
        f"(a project, or the topic the work is about), even if the TEXT doesn't name it."
    )


# Graphiti always offers its generic type as id 0; excluding it is what makes
# the ontology a filter rather than a set of optional labels.
EXCLUDED_ENTITY_TYPES = ["Entity"]


def extraction_kwargs(profile: str | None = None, source_description: Optional[str] = None) -> dict[str, Any]:
    """The add_episode() kwargs for an extraction profile.

    `profile=None` reads CMF_EXTRACTION_PROFILE fresh (validated there), so
    a replay run can switch profiles through the environment without
    touching .env or a running MCP server. `source_description` is the
    episode's own; typed-recall reads its `project=` field.
    """
    if profile is None:
        profile = extraction_profile_from_env()
    if profile == LEGACY_EXTRACTION:
        return {"custom_extraction_instructions": LEGACY_INSTRUCTIONS}
    if profile == SELECTIVE_EXTRACTION:
        return {"custom_extraction_instructions": SELECTIVE_INSTRUCTIONS}
    if profile == TYPED_EXTRACTION:
        return {
            "custom_extraction_instructions": SELECTIVE_INSTRUCTIONS,
            "entity_types": ENTITY_TYPES,
            "excluded_entity_types": EXCLUDED_ENTITY_TYPES,
        }
    if profile == TYPED_RECALL_EXTRACTION:
        return {
            "custom_extraction_instructions": RECALL_INSTRUCTIONS + _project_instruction(source_description),
            "entity_types": RECALL_ENTITY_TYPES,
            "excluded_entity_types": EXCLUDED_ENTITY_TYPES,
        }
    raise ValueError(
        f"Unknown extraction profile {profile!r}. Valid values are {', '.join(VALID_EXTRACTION_PROFILES)}."
    )
