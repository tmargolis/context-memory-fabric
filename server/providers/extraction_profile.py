"""Entity-extraction profiles for Graphiti's add_episode() (MS4e).

Every CMF write into the episodic graph goes through Graphiti's
extract_nodes / extract_edges prompts, and the only levers CMF has over what
comes out are the kwargs it passes: `custom_extraction_instructions`,
`entity_types` and `excluded_entity_types`. This module owns all three, so
remember() and correct_memory() extract under the same rules (before MS4e,
correction.py passed none of them and corrected episodes quietly used
Graphiti's bare defaults).

Profiles, selected by CMF_EXTRACTION_PROFILE (server.core.config):

- "legacy": the pre-MS4e instructions, verbatim. Opt-in only, kept so
  pre-2026-09-28 extraction can be reproduced. Tuned in Spark Phase 7 for
  terse ~200-char episodes where qwen3.5-122b returned an empty entity list
  ~45% of the time, so it pushes hard the other way ("extract every specific
  named entity ... files and path patterns ... settings or parameters").
  On today's longer, thread-merged episodes that push is the main source of
  entity noise: 79% of jspace's 398 entities were mentioned by exactly one
  episode, most of them file names, identifiers and numbers (MS4e, measured
  2026-09-28).
- "typed-recall" (the default since MS10a, 2026-10-08): the MS4e Phase 3
  follow-up. The A/B showed the ontology is what filters debris, while the
  intermediate "selective" profile's cautious wording made qwen return
  nothing on long episodes ("typed", selective plus an ontology, found 36 of
  57 reference entities, legacy 50). So this keeps a wider ontology as the filter, adding Format and
  Topic per the user's review, and asks for coverage within it. It also names
  the episode's own project, which Graphiti's text prompt never shows the
  model (source_description carries it as `project=<bucket>`).

"selective" and "typed" were removed in MS10a (2026-10-08); their prompt and
ontology are in git history before that date.

The ontology's type names become FalkorDB labels on the saved nodes
(alongside `Entity`), so none may collide with a label CMF already uses:
`Project` in particular is CMF's own `(:Project)` node that IN_PROJECT edges
point at (server.consolidation.graph_tagging), which is why a project-like
entity is a `Workstream` here. Types are docstring-only models: a field would
make Graphiti run an attribute-extraction LLM call per node, and a field
named like an EntityNode attribute (name, summary, ...) is rejected outright.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from pydantic import BaseModel, create_model

from server.core.config import (
    LEGACY_EXTRACTION,
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

# typed-recall (MS4e v2). the user's review of the Phase 3 replay (2026-09-28):
# techniques, formats and generic roles/organizations are entities; numbers,
# files, identifiers and local labels are not. Keep examples for the prompt
# and docstrings come from that review.
#
# The examples that name the operator's own work -- projects, hardware,
# topics, a person, file names -- come from the gitignored .env (MS10a), so
# a new user's prompt never carries someone else's projects. Unset, each
# falls back to the generic built-in below. Each site takes the first few
# names in order; extra names are ignored.
EXAMPLE_SETTINGS: dict[str, tuple[str, int, tuple[str, ...]]] = {
    # placeholder: (env var, names used, built-in)
    "workstreams": (
        "CMF_EXTRACTION_WORKSTREAMS", 5, ("Atlas", "Acme Billing", "home-lab", "Field Notes", "Moonbeam"),
    ),
    "hardware": ("CMF_EXTRACTION_HARDWARE", 4, ("MacBook Pro", "NAS", "Raspberry Pi", "iPad")),
    "topics": (
        "CMF_EXTRACTION_TOPICS", 6,
        ("home renovation", "photography", "machine learning", "personal finance", "gardening", "travel planning"),
    ),
    "person": ("CMF_EXTRACTION_PERSON", 1, ("Jordan Lee",)),
    "debris_files": ("CMF_EXTRACTION_DEBRIS_FILES", 3, ("build_index.py", "results.json", "app.js")),
}


def extraction_examples() -> dict[str, list[str]]:
    """Each example list: the setting's comma-separated names, else the built-in."""
    out: dict[str, list[str]] = {}
    for key, (env_var, count, builtin) in EXAMPLE_SETTINGS.items():
        names = [n.strip() for n in (os.getenv(env_var) or "").split(",") if n.strip()]
        out[key] = (names or list(builtin))[:count]
    return out


_RECALL_INSTRUCTIONS_TEMPLATE = (
    "The TEXT is a third-person summary of work from a personal knowledge log: decisions, plans, "
    "findings and troubleshooting, often followed by 'Driving question:' and 'Reasoning:' lines. "
    "Extract EVERY entity in it that fits one of the entity types, including ones mentioned only in "
    "passing or only in the Reasoning line: people and roles, organizations and groups, software, "
    "libraries and services, AI models, hardware, projects and named works, places, techniques and "
    "methods, file and data formats, and the one or two topics the whole text is about. Look inside file names for "
    "real entities (uncertainty_fake_gemma-4-12b-it.jsonl names the model gemma-4-12b-it) but never "
    "extract the file name itself.\n"
    "Use the plain canonical name: no version numbers (Photoshop, not Photoshop v27) and no generic "
    "suffixes ({workstream}, not {workstream} project). Extract each thing once: GitHub, not also 'GitHub "
    "connector' or 'GitHub integration'.\n"
    "Never extract:\n"
    "- file names, scripts or paths ({debris_files})\n"
    "- functions, variables, fields, config keys, CLI flags, env vars (head_idx, defaultK, top_probs, sys.path)\n"
    "- exception and error names (ModuleNotFoundError)\n"
    "- numbers, measurements, sizes, durations, counts, PIDs, IPs, ports, layers, steps "
    "(50 episodes, layer 40, 317MB, PID 12955, port 8000)\n"
    "- labels local to one discussion (Option C, Phase 6, Phase 6 step 1, step-0)\n"
    "- UI details and process narration (arrowhead size, progress bar, timed test)\n"
    "- quoted words and sentence fragments ('Depends', 'There')\n"
    "Do NOT extract the narrator (\"the user\", \"the assistant\") as an entity."
)

# (label, model name, docstring template). A label is the dict key Graphiti
# saves as the node's FalkorDB label; the docstring is the type description
# Graphiti sends to the model, so it is part of the prompt.
_RECALL_TYPE_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("Person", "PersonOrRole",
     """A specific person ({person}) or a role a real person plays in the user's life (board
    director, property manager, staff, neighbor). Never the narrator ("the user", "the assistant"),
    and never a name that only appears as sample data inside the work."""),
    ("Organization", "OrganizationOrGroup",
     """A company, institution, team, community, board or committee (Google, NVIDIA, condo board,
    the board of an HOA)."""),
    ("Software", "SoftwareOrService",
     """A software product, app, operating system, library, framework, service, platform, online
    account or web technology (Google Docs, BBEdit, Photoshop, macOS, FalkorDB, torch, transformers,
    uvicorn, node-cron, Hugging Face, a Google account, CSP, CORS, DOM, canvas), or a tool the user
    built and refers to as a lasting thing (labeling tool). A file, script, dataset id, field or
    variable name is NEVER Software."""),
    ("AIModel", "RecallAIModel",
     """An AI or machine-learning model, model family or variant, including one named only by a size
    or variant code (Claude, gemini-2.5-flash, qwen3.5-122b, Gemma, E4B, gemma-4-12b-it). Never
    Hardware."""),
    ("Hardware", "RecallHardware",
     """A physical device, computer, component or piece of equipment ({hardware}, a camera or telescope). Never software, an operating system, a web technology, a
    model, or a form-factor word like 'mobile'."""),
    ("Workstream", "ProjectOrWork",
     """A project, codebase, product effort, study or named creative work the user works on
    ({workstreams}). Not a milestone, phase, step or
    option inside one ("Phase 6", "Option C"), and not a document, README, test run, trace type or
    data type inside a project (README, Trace Schema, Key Questions, local model test)."""),
    ("Place", "RecallPlace",
     """A real geographic location, venue, astronomical/celestial body or physical site (a city,
    gallery, observatory, home, celestial targets like Sun, Moon). Never a word used to describe a
    style or design ('a gallery/lab treatment')."""),
    ("Method", "TechniqueOrMethod",
     """A technique, algorithm, research method or practice (occlusion, counterfactuals, k-means,
    PCA, MDS, teacher-forcing, logit lens). Not a one-off step, parameter or setting."""),
    ("Format", "Format",
     """A file or data format or encoding (JSON, Markdown, parquet, safetensors, GIF, HTML, CSV,
    base64). The format itself, never a particular file."""),
    ("Topic", "Topic",
     """The overall subject of the whole piece of work, in general terms ({topics}). At most TWO per text,
    and only what the text as a whole is about. NEVER a data field, metric, variable, internal
    mechanism, quantity, UI element, step, setting or implementation concept (embedding vectors,
    attention weights, entropy, per-layer opacity, ground truth answers)."""),
)


def render_recall_profile(
    examples: Optional[dict[str, list[str]]] = None,
) -> tuple[str, dict[str, type[BaseModel]]]:
    """The typed-recall instructions and ontology with `examples` filled in
    (default: extraction_examples(), read from the environment now).

    Types are docstring-only models built fresh on each call, so rendering
    with other examples never touches the module-level ones.
    """
    ex = extraction_examples() if examples is None else examples
    fields = {
        "workstream": ex["workstreams"][0],
        "workstreams": ", ".join(ex["workstreams"]),
        "hardware": ", ".join(ex["hardware"]),
        "topics": ", ".join(ex["topics"]),
        "person": ex["person"][0],
        "debris_files": ", ".join(ex["debris_files"]),
    }
    instructions = _RECALL_INSTRUCTIONS_TEMPLATE.format(**fields)
    types = {
        label: create_model(model_name, __doc__=template.format(**fields), __module__=__name__)
        for label, model_name, template in _RECALL_TYPE_TEMPLATES
    }
    return instructions, types


# Rendered once at import: server/__init__.py has loaded .env by then, so a
# change to the example settings takes effect on the next restart.
RECALL_INSTRUCTIONS, RECALL_ENTITY_TYPES = render_recall_profile()

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
    if profile == TYPED_RECALL_EXTRACTION:
        return {
            "custom_extraction_instructions": RECALL_INSTRUCTIONS + _project_instruction(source_description),
            "entity_types": RECALL_ENTITY_TYPES,
            "excluded_entity_types": EXCLUDED_ENTITY_TYPES,
        }
    raise ValueError(
        f"Unknown extraction profile {profile!r}. Valid values are {', '.join(VALID_EXTRACTION_PROFILES)}."
    )
