"""Deterministic doc-proposal repairs (server.policies.doc_validate) and where extraction applies them.

Invented text only. Each case is a defect nightly review kept rejecting by hand: a page body stacked
several times, an addition that restates the live page, a frontmatter `project:` naming the wrong slug.
"""

from datetime import datetime, timezone

from server.consolidation.project_aliases import project_wiki_folders
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.policies.doc_validate import collapse_stacked_body, drop_restated_sections, fix_frontmatter_project
from server.policies.extract import ExtractPolicyV1, _canonicalize_doc_project_folder

FM = "---\ntitle: Widget Notes\ncreated: 2026-11-01\nproject: widgets\n---\n"
BODY_A = "# Widget Notes\n\n## Setup\n\nInstall the thing.\n\n## Related\n\n- [[Other]]\n"
BODY_B = "# Widget Notes\n\n## Setup\n\nInstall the thing.\n\n## Troubleshooting\n\nRestart it.\n\n## Related\n\n- [[Other]]\n"


# --- collapse_stacked_body ---------------------------------------------------------

def test_stacked_copies_keep_the_last_with_its_own_frontmatter():
    out = collapse_stacked_body(FM + BODY_A + "\n" + FM.replace("2026-11-01", "2026-11-05") + BODY_B)
    assert out.count("# Widget Notes") == 1
    assert "Troubleshooting" in out and "created: 2026-11-05" in out and out.count("---\ntitle") == 1


def test_stacked_copies_without_frontmatter_on_the_last_use_the_first_block():
    out = collapse_stacked_body(FM + BODY_A + "\n" + BODY_B)
    assert out.startswith("---\ntitle: Widget Notes") and out.count("# Widget Notes") == 1
    assert "Troubleshooting" in out


def test_three_copies_collapse_to_the_last():
    out = collapse_stacked_body(BODY_A + "\n" + BODY_A + "\n" + BODY_B)
    assert out.count("# Widget Notes") == 1 and "Troubleshooting" in out


def test_a_single_copy_or_different_titles_are_left_alone():
    assert collapse_stacked_body(FM + BODY_A) == FM + BODY_A
    mixed = "# One\n\ntext\n\n# Two\n\nmore\n"
    assert collapse_stacked_body(mixed) == mixed


def test_a_hash_line_inside_a_code_fence_is_not_a_title():
    text = "# Widget Notes\n\n```bash\n# Widget Notes\n```\n"
    assert collapse_stacked_body(text) == text


# --- drop_restated_sections --------------------------------------------------------

LIVE = "# Widget Notes\n\n## Setup\n\nInstall the thing.\n\n## Related\n\n- [[Other]]\n"


def test_a_restated_page_keeps_only_the_new_section():
    addition = LIVE.replace("## Related", "## Troubleshooting\n\nRestart it.\n\n## Related")
    out = drop_restated_sections(addition, LIVE)
    assert "Troubleshooting" in out and "Restart it." in out
    assert "Install the thing." not in out and "[[Other]]" not in out and "# Widget Notes" not in out


def test_a_section_with_changed_text_is_kept():
    out = drop_restated_sections("## Setup\n\nInstall it, then run the init script.\n", LIVE)
    assert "init script" in out


def test_a_repeated_section_inside_the_addition_keeps_the_later_copy():
    addition = "## Notes\n\nsame body here\n\n## Notes\n\nsame body here\n"
    assert drop_restated_sections(addition, LIVE).count("## Notes") == 1


def test_an_addition_with_nothing_restated_is_unchanged():
    addition = "## Troubleshooting\n\nRestart it.\n"
    assert drop_restated_sections(addition, LIVE) == addition


# --- fix_frontmatter_project -------------------------------------------------------

def test_a_wrong_project_line_is_corrected():
    assert "project: gadgets\n" in fix_frontmatter_project("---\ntitle: T\nproject: Old Name\n---\nbody\n", "gadgets")


def test_a_matching_project_line_is_left_alone_even_when_formatted_differently():
    text = "---\ntitle: T\nproject: J-Space\n---\nbody\n"
    assert fix_frontmatter_project(text, "jspace") == text


def test_a_missing_project_line_is_not_added_and_no_frontmatter_is_untouched():
    text = "---\ntitle: T\n---\nbody\n"
    assert fix_frontmatter_project(text, "gadgets") == text
    assert fix_frontmatter_project("no frontmatter\n", "gadgets") == "no frontmatter\n"
    assert fix_frontmatter_project(text, None) == text


# --- CMF_PROJECT_WIKI_FOLDERS -----------------------------------------------------------

def test_a_declared_folder_beats_the_slug_guess(monkeypatch):
    monkeypatch.setenv("CMF_PROJECT_WIKI_FOLDERS", "widgets-dev=Widgets, bad entry, x=a/b")
    assert project_wiki_folders() == {"widgets-dev": "Widgets"}
    out = _canonicalize_doc_project_folder("WIKI/projects/Widgets-Dev/Notes.md", "widgets-dev", ["Widgets", "Other"])
    assert out == "WIKI/projects/Widgets/Notes.md"


def test_without_a_declared_folder_the_slug_guess_stands(monkeypatch):
    monkeypatch.delenv("CMF_PROJECT_WIKI_FOLDERS", raising=False)
    out = _canonicalize_doc_project_folder("WIKI/projects/Whatever/Notes.md", "widgets-dev", ["Widgets"])
    assert out == "WIKI/projects/Widgets-Dev/Notes.md"


# --- where extraction applies them -------------------------------------------------

def _event():
    return SourceEvent(
        schema_version="1.0", event_id="e1", event_type="turn.completed",
        source=SourceProvenance(harness="antigravity", conversation_id="conv-1", turn_id="t1"),
        actor_type="user", observed_at=datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
        content={"text": "write it up"}, content_hash="h1", date_precision=DatePrecision.NONE,
        metadata={"project": "widgets"},
    )


def test_a_new_page_with_a_stacked_body_and_a_wrong_project_is_repaired(monkeypatch):
    monkeypatch.delenv("CMF_PROJECT_ALIASES", raising=False)
    spec = {"target_path": "WIKI/projects/Widgets/Notes.md", "rationale": "write up", "statement": "s",
            "proposed_content": FM.replace("project: widgets", "project: something-else") + BODY_A + "\n" + BODY_B}
    cand = ExtractPolicyV1(generate_fn=lambda *a, **k: "{}")._to_doc_candidate(
        spec, ordered=[_event()], existing_project_folders=["Widgets"])
    assert cand.proposed_content.count("# Widget Notes") == 1
    assert "Troubleshooting" in cand.proposed_content and "project: widgets\n" in cand.proposed_content


def test_an_addition_that_restates_the_live_page_keeps_only_the_new_section():
    addition = LIVE.replace("## Related", "## Troubleshooting\n\nRestart it.\n\n## Related")
    spec = {"target_path": "WIKI/projects/Widgets/Notes.md", "rationale": "extend", "statement": "s",
            "proposed_content": addition}
    cand = ExtractPolicyV1(generate_fn=lambda *a, **k: "{}")._to_doc_candidate(
        spec, ordered=[_event()], existing_project_folders=["Widgets"],
        relevant_wiki_docs=[{"target_path": "WIKI/projects/Widgets/Notes.md", "current_content": LIVE}])
    assert "Restart it." in cand.proposed_content
    assert "Install the thing." not in cand.proposed_content


def test_an_addition_that_only_restates_the_page_is_dropped():
    spec = {"target_path": "WIKI/projects/Widgets/Notes.md", "rationale": "extend", "statement": "s", "proposed_content": LIVE}
    cand = ExtractPolicyV1(generate_fn=lambda *a, **k: "{}")._to_doc_candidate(
        spec, ordered=[_event()], existing_project_folders=["Widgets"],
        relevant_wiki_docs=[{"target_path": "WIKI/projects/Widgets/Notes.md", "current_content": LIVE}])
    assert cand is None
