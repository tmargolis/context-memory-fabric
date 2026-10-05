"""Deterministic debris filter for extracted entities (MS4e).

Prompts steer qwen away from implementation debris but can't guarantee it:
in the MS4e A/B replay the typed profile still saved `qa_dump.json` and
`jspace20.js` as Software. `noise_category()` recognizes debris by shape
alone (files and paths, code identifiers, numbers and measures, labels local
to one discussion), and `prune_debris_entities()` runs it right after
add_episode(), deleting what the episode just created.

Only an entity no other node MENTIONS is deleted, so the filter never
reaches back into what earlier episodes (or the wiki layer) built: a debris
name that already existed stays, and existing nodes are out of scope for
MS4e. DETACH DELETE also removes the entity's RELATES_TO facts, which are
facts about the debris. Off by default; CMF_ENTITY_DEBRIS_FILTER=1 turns it on.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FILE_EXT = re.compile(
    r"\.(py|js|ts|json|jsonl|md|html?|parquet|csv|tsv|txt|sh|ya?ml|toml|db|sqlite|safetensors|gif|png|jpe?g|svg|plist|log)$",
    re.I,
)
_NUMERIC = re.compile(
    r"^[~≈<>]?\d"  # starts with a number: 317MB, 21 layers, 2 minutes 14 seconds, 0.0.0.0
    r"|^(pid|pids|port|index|layer|layers|step|l)[ -]?\d"  # PID 12955, layer 40, step-0, L17
    r"|\b(ip|lan ip)\s+\d"
    r"|:\d{2,5}$"  # localhost:8000
    r"|^[a-z_]\w*=[\w.]+$",  # p=36.5, topk=32, full_layer_steps=1
    re.I,
)
# "Phase 6", "Phase 6 step 1", "Option C", "tab-1" -- but not "Stage Manager".
_LOCAL_LABEL = re.compile(r"^(option|phase|part|tab|stage|step|milestone)[ -]?[a-z0-9]{1,3}\b", re.I)
_CONSTANT = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")  # CLOUD_PER_LAYER
_SNAKE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)+$")  # head_idx, run_qa_local
_CAMEL = re.compile(r"^[a-z]{4,}[A-Z]\w*$")  # defaultK, drawSegment -- but not macOS, iPhone
_DOTTED = re.compile(r"^[a-z_]+(\.[a-z_]+)+(\(\))?$")  # sys.path, json.dumps, lens.jacobians
_CALL = re.compile(r"\(\)|\bfunction$")  # place() function, read_layers()
_EXCEPTION = re.compile(r"^[A-Z]\w*(Error|Exception)$")
_QUOTED = re.compile(r"^['\"`].*['\"`]$")
_DIMENSION = re.compile(r"\d[DdKk]")  # 2D, 3D, 4K: names, not measurements


def _looks_like_path(name: str) -> bool:
    # A bare "A/B" is usually a pair of names (PyTorch/MPS, Thai/English);
    # a path has an extension, a root, a glob, or more than one separator.
    if " " in name or not ("/" in name or "\\" in name):
        return False
    return bool(
        _FILE_EXT.search(name)
        or name[0] in "/~.\\"
        or "*" in name
        or name.count("/") + name.count("\\") >= 2
    )


def noise_category(name: str) -> Optional[str]:
    """The implementation-debris pattern an entity name matches, or None.

    Deliberately conservative: it only flags shapes that are almost never a
    durable entity. Generic nouns ("scores", "renderer") can't be caught by
    shape; the prompt has to handle those.
    """
    n = name.strip()
    if not n:
        return "empty"
    if _DIMENSION.fullmatch(n):
        return None
    if _QUOTED.match(n):
        return "quoted"
    if _FILE_EXT.search(n) or _looks_like_path(n):
        return "file_or_path"
    if _EXCEPTION.match(n):
        return "exception"
    if _CALL.search(n):
        return "code_identifier"
    if " " not in n and (_CONSTANT.match(n) or _SNAKE.match(n) or _CAMEL.match(n) or _DOTTED.match(n)):
        return "code_identifier"
    if _LOCAL_LABEL.match(n):
        return "local_label"
    if _NUMERIC.search(n):
        return "number_or_measure"
    return None


def debris_filter_enabled() -> bool:
    raw = (os.getenv("CMF_ENTITY_DEBRIS_FILTER") or "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"CMF_ENTITY_DEBRIS_FILTER={raw!r} is not a boolean (use 1 or 0).")


_TRUSTED_IDENTIFIER_TYPES = {"Software", "AIModel"}


def _records(rows: Any) -> list[Any]:
    return rows[0] if rows and isinstance(rows[0], list) else (rows or [])


async def prune_debris_entities(driver: Any, episode_uuid: str, nodes: list[Any]) -> list[str]:
    """Delete debris-shaped entities that only `episode_uuid` mentions.

    `nodes` is AddEpisodeResults.nodes. Returns the deleted names.
    """
    removed: list[str] = []
    for node in nodes:
        category = noise_category(node.name)
        if not category:
            continue
        # A package name like huggingface_hub is identifier-shaped but real:
        # trust the model's Software/AIModel typing for identifiers, never for
        # files, numbers or labels (MS4e: the one false positive in 16).
        if category == "code_identifier" and set(getattr(node, "labels", []) or []) & _TRUSTED_IDENTIFIER_TYPES:
            continue
        rows = _records(await driver.execute_query(
            "MATCH (n:Entity {uuid: $uuid}) "
            "OPTIONAL MATCH (x)-[:MENTIONS]->(n) WHERE x.uuid <> $episode_uuid "
            "RETURN count(x) AS others",
            uuid=node.uuid, episode_uuid=episode_uuid,
        ))
        if not rows or rows[0]["others"] != 0:
            continue
        await driver.execute_query("MATCH (n:Entity {uuid: $uuid}) DETACH DELETE n", uuid=node.uuid)
        removed.append(node.name)
    return removed


async def filter_debris_after_add(graphiti: Any, result: Any, episode_name: str) -> list[str]:
    """Run prune_debris_entities on an add_episode() result when the filter is on.

    Logs what it removed, so a replay can reconstruct the pre-filter set.
    """
    if result is None or not debris_filter_enabled():
        return []
    removed = await prune_debris_entities(graphiti.driver, result.episode.uuid, result.nodes)
    if removed:
        logger.info("debris filter removed %d entit%s from %r: %s",
                    len(removed), "y" if len(removed) == 1 else "ies", episode_name, removed)
    return removed
