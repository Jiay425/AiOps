import json

from scripts.audit_feature_parity import EVIDENCE, MAP, java_types, python_symbols
from scripts.audit_config_parity import audit as audit_config
from scripts.audit_http_parity import legacy_routes, python_routes
from ops_autoagent.config import Settings


def test_every_legacy_http_route_exists_in_fastapi():
    assert legacy_routes() <= python_routes()


def test_every_legacy_public_type_has_valid_target_and_evidence():
    mapping = json.loads(MAP.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    legacy = java_types()
    assert legacy <= mapping.keys()
    assert set(mapping.values()) <= python_symbols()
    assert legacy <= evidence.keys()
    for name in legacy:
        assert evidence[name]["target"] == mapping[name]


def test_every_legacy_full_profile_placeholder_is_supported():
    result = audit_config()
    assert result["missing"] == []
    assert result["invalidFields"] == []


def test_legacy_environment_aliases_resolve_to_python_settings():
    config = Settings(_env_file=None, OPENAI_CHAT_MODEL="legacy-chat", OPENAI_VECTOR_TABLE="legacy_vectors",
                      OPS_RUNBOOK_BASE_PATH="legacy-runbooks", OPS_RUNBOOK_EMBEDDING_DIMENSIONS=1536)
    assert config.openai_model == "legacy-chat"
    assert config.pgvector_table == "legacy_vectors"
    assert str(config.ops_runbook_path) == "legacy-runbooks"
    assert config.ops_embedding_dimensions == 1536
