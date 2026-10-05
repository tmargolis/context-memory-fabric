"""MS9 Phase 1 — duplicate-candidate rules (pure functions, no graph)."""

import numpy as np

from scripts import dedupe_entities_report as d

FAMILIES = [{d.normalize(x) for x in fam} for fam in d.NEVER_FAMILIES]
KW = dict(embed_threshold=0.92, contain_gate=0.80, families=FAMILIES, keep=set())


def E(name, wt=None, eps=0, notes=0, rel=0):
    return d.Entity(uuid=name, name=name, wiki_type=wt, episodes=eps, notes=notes, relates=rel)


def test_normalize_folds_case_punctuation_space_and_plural():
    assert d.normalize("GitHub") == d.normalize("Github")
    assert d.normalize("LM Studio") == d.normalize("LMStudio")
    assert d.normalize("Cityscapes") == d.normalize("cityscape")
    assert d.normalize("Batteries") == d.normalize("battery")
    assert d.normalize("Glass") != d.normalize("Gla")  # -ss is not a plural


def test_acronym():
    assert d.is_acronym("MCP", "Model Context Protocol")
    assert d.is_acronym("EVCS", "Electric Vehicle Charging Station")
    assert not d.is_acronym("MCP", "MCP server")
    assert not d.is_acronym("M", "Mac")


def test_containment():
    assert d.is_contained("Alex", "Alex Morgan")
    assert d.is_contained("Spark", "NVIDIA Spark")
    assert not d.is_contained("Alex Morgan", "Alex")
    assert not d.is_contained("the", "the big thing")


def test_exact_name_twins_are_auto_even_across_sides():
    got = d.classify(E("Graphiti", eps=7, rel=13), E("Graphiti", "tool", notes=6), 0.99, **KW)
    assert got == ("auto", "exact")


def test_normalized_with_conflicting_types_goes_to_review():
    got = d.classify(E("Cityscape", "domain"), E("Cityscapes", "project"), 0.95, **KW)
    assert got == ("review", "normalized+type-conflict")


def test_never_families_and_gold_keeps():
    assert d.classify(E("Claude"), E("Claude Code"), 0.95, **KW)[0] == "never-family"
    kw = dict(KW, keep={d.normalize("Gemini Flash"), d.normalize("Gemini Pro")})
    assert d.classify(E("Gemini Flash"), E("Gemini Pro"), 0.96, **kw)[0] == "never-gold-keep"
    # Gold keeps only block the embedding rule; "Spark" / "NVIDIA Spark" is the same thing.
    kw = dict(KW, keep={d.normalize("Spark"), d.normalize("NVIDIA Spark")})
    assert d.classify(E("Spark"), E("NVIDIA Spark"), 0.80, **kw) == ("review", "containment")


def test_version_families_are_never():
    assert d.classify(E("Claude Sonnet 4.5", "model"), E("Claude Sonnet 4.6", "model"), 0.97, **KW)[0] == "never-version"
    assert d.classify(E("Qwen3.5-27B", "model"), E("Qwen3.6-27B", "model"), 0.96, **KW)[0] == "never-version"


def test_generic_words_ambiguous_names_and_debris():
    assert d.classify(E("card"), E("Apple Card", "product"), 0.85, **KW) != ("review", "containment")
    assert d.classify(E("docs"), E("Google Docs"), 0.85, **KW) != ("review", "containment")
    kw = dict(KW, persons=frozenset({d.normalize("John Cramer")}), ambiguous_names=frozenset({"john"}))
    assert d.classify(E("John", "person"), E("John Cramer", "person"), 0.5, **kw) == ("weak", "containment")
    assert d.classify(E("CMF_MCP_AUTH_TOKEN"), E("CMF_MCP_AUTH_HEADER"), 0.95, **KW) == ("weak", "embedding")
    assert d.classify(E("CTM", "org"), E("cardboard template method"), 0.3, **KW) is None


def test_containment_needs_embedding_gate_and_embedding_needs_threshold():
    assert d.classify(E("MCP"), E("MCP server"), 0.70, **KW) is None
    assert d.classify(E("MCP"), E("MCP tool"), 0.87, **KW) == ("weak", "containment")
    kw = dict(KW, persons=frozenset({d.normalize("Alex Morgan")}))
    assert d.classify(E("Alex"), E("Alex Morgan"), 0.40, **kw) == ("review", "containment")
    assert d.classify(E("Morgan"), E("Alex Morgan"), 0.40, **kw) == ("review", "containment")
    assert d.classify(E("Charles"), E("Charles Howell", "person"), 0.40, **KW) == ("review", "containment")
    assert d.classify(E("Alex"), E("Alex Morgan"), 0.40, **KW) is None  # not known to be a person
    assert d.classify(E("Spark"), E("NVIDIA Spark"), 0.80, **KW) == ("review", "containment")
    assert d.classify(E("CTM"), E("CTM Legal Group", "org"), 0.83, **KW) == ("review", "containment")
    assert d.classify(E("Docker"), E("Docker Desktop"), 0.89, **KW) == ("weak", "containment")
    assert d.classify(E("Jacobian lens"), E("J-lens"), 0.93, **KW) == ("review", "embedding")
    assert d.classify(E("Obsidian"), E("Notion"), 0.80, **KW) is None


def test_types_block_fuzzy_rules_across_groups():
    assert d.classify(E("Chicago", "place"), E("Chicago Bears", "org"), 0.95, **KW) is None


def test_canonical_prefers_fact_bearing_then_connected_node():
    ep = E("Graphiti", eps=7, rel=13)
    wiki = E("Graphiti", "tool", notes=6)
    assert d.canonical(ep, wiki) is ep and d.canonical(wiki, ep) is ep
    assert d.canonical(E("MCP", "technique", eps=4, notes=21, rel=8), E("MCP", eps=3, rel=4)).notes == 21


def test_clusters_union_chains_but_not_never_pairs():
    pairs = [{"i": 0, "j": 1, "tier": "auto"}, {"i": 1, "j": 2, "tier": "review"},
             {"i": 3, "j": 4, "tier": "never-family"}]
    c = d.clusters(pairs, 5)
    assert c[0] == c[1] == c[2]
    assert 3 not in c and 4 not in c  # never pairs aren't clustered


def test_find_pairs_end_to_end_small():
    ents = [E("Graphiti", eps=7, rel=13), E("Graphiti", "tool", notes=6), E("Model Context Protocol", "tool"),
            E("MCP", "technique", notes=21), E("Obsidian"), E("Claude"), E("Claude Code")]
    rng = np.random.default_rng(0)
    mat = rng.normal(size=(len(ents), 16)).astype(np.float32)
    mat[1] = mat[0]
    mat[6] = mat[5] + 0.01
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    pairs = {(ents[p["i"]].name, ents[p["j"]].name): p["tier"] for p in d.find_pairs(ents, mat, **KW)}
    assert pairs[("Graphiti", "Graphiti")] == "auto"
    assert pairs[("Model Context Protocol", "MCP")] == "review"
    assert pairs[("Claude", "Claude Code")] == "never-family"


def test_short_or_ambiguous_acronyms_are_weak():
    assert d.classify(E("AI"), E("Associative Insights", "product"), 0.5, **KW) == ("weak", "acronym")
    kw = dict(KW, ambiguous=frozenset({d.normalize("CCC")}))
    assert d.classify(E("CCC"), E("Cisco Cloud Control", "product"), 0.5, **kw) == ("weak", "acronym")
    assert d.classify(E("MCP"), E("Model Context Protocol", "tool"), 0.42, **KW) == ("review", "acronym")


def test_auto_tier_guards():
    assert d.normalize("HTTPS") != d.normalize("HTTP")
    assert d.classify(E("levels"), E("Level", "product"), 0.9, **KW) == ("review", "normalized+case")
    assert d.classify(E("/token"), E("token", "domain"), 0.9, **KW) == ("review", "normalized+debris")
    assert d.classify(E("12B"), E("12B", "product"), 0.9, **KW) == ("review", "exact+debris")
    assert d.classify(E("Cat6a cables", "product"), E("Cat6a cable", "product"), 0.9, **KW) == ("auto", "normalized")
    assert d.classify(E("Condo 12A", "place"), E("Condo 12B", "place"), 0.95, **KW)[0] == "never-version"
