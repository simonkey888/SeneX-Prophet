#!/usr/bin/env python3
from __future__ import annotations

import json
import textwrap
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"{label}: expected one anchor, found {text.count(old)}")
    return text.replace(old, new, 1)


def main() -> None:
    tool = Path("tools/verify_repository_contract.py")
    text = tool.read_text(encoding="utf-8")

    architecture_block = textwrap.dedent(
        '''\
        def _architecture_map(architecture: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
            return {str(item["domain_id"]): item for item in architecture.get("domains", [])}
        '''
    )
    helpers = textwrap.dedent(
        '''\


        def unclassified_duplicate_paths(
            groups: Mapping[str, Sequence[str]],
            contract: Mapping[str, Any],
        ) -> list[str]:
            configured = contract.get("classified_duplicate_basenames", {})
            violations: list[str] = []
            for basename, paths in sorted(groups.items()):
                actual = sorted(str(path) for path in paths)
                if len(actual) <= 1:
                    continue
                item = configured.get(basename) if isinstance(configured, Mapping) else None
                expected = (
                    sorted(str(path) for path in item.get("paths", []))
                    if isinstance(item, Mapping)
                    else []
                )
                classified = bool(
                    isinstance(item, Mapping)
                    and str(item.get("classification", "")).strip()
                    and str(item.get("reason", "")).strip()
                )
                if classified and expected == actual:
                    continue
                violations.extend(actual)
            return sorted(set(violations))


        def research_authority_violations(
            root: Path,
            paths: Sequence[str],
            contract: Mapping[str, Any],
        ) -> list[str]:
            configured = contract.get("research_authoritative_root_reference_allowlist", {})
            violations: list[str] = []
            for rel in paths:
                path = root / rel
                if not rel.startswith("research/") or not path.is_file():
                    continue
                if "raw_chain_v1" not in path.read_text(encoding="utf-8", errors="ignore"):
                    continue
                item = configured.get(rel) if isinstance(configured, Mapping) else None
                classified = bool(
                    isinstance(item, Mapping)
                    and item.get("classification") == "NON_AUTHORITATIVE_HISTORICAL_DESIGN"
                    and str(item.get("reason", "")).strip()
                    and str(item.get("current_authority", "")).strip()
                )
                if not classified:
                    violations.append(rel)
            return sorted(set(violations))
        '''
    )
    text = replace_once(text, architecture_block, architecture_block + helpers, "helper insertion")

    old_duplicate = textwrap.dedent(
        '''\
            duplicates = [rel for group in groups.values() if len(group) > 1 for rel in group]
            results.append(bad(R0_GATE_IDS[4], ["new unclassified duplicate basename"], duplicates) if duplicates else ok(R0_GATE_IDS[4], "no new unclassified duplicate family"))
        '''
    )
    new_duplicate = textwrap.dedent(
        '''\
            duplicates = unclassified_duplicate_paths(groups, contract)
            results.append(
                bad(
                    R0_GATE_IDS[4],
                    ["new duplicate basename lacks an exact classification"],
                    duplicates,
                )
                if duplicates
                else ok(R0_GATE_IDS[4], "no new unclassified duplicate family")
            )
        '''
    )
    text = replace_once(text, old_duplicate, new_duplicate, "duplicate gate")

    old_research = textwrap.dedent(
        '''\
            research = [rel for rel in ch if rel.startswith("research/") and "raw_chain_v1" in (root / rel).read_text(encoding="utf-8", errors="ignore")]
            results.append(bad(R0_GATE_IDS[9], ["research references authoritative raw root"], research) if research else ok(R0_GATE_IDS[9], "no research authority violation"))
        '''
    )
    new_research = textwrap.dedent(
        '''\
            research = research_authority_violations(root, ch, contract)
            results.append(
                bad(
                    R0_GATE_IDS[9],
                    ["research references authoritative raw root without an explicit non-authoritative classification"],
                    research,
                )
                if research
                else ok(R0_GATE_IDS[9], "no research authority violation")
            )
        '''
    )
    text = replace_once(text, old_research, new_research, "research gate")
    tool.write_text(text, encoding="utf-8")

    contract_path = Path("governance/repository_contract.yaml")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["mission_id"] = "SENEX-END-TO-END-COMPLETION-001"
    contract["aud_order_comment_id"] = 5188597690
    contract["contract_base_sha"] = "2f8503533543832147caf4c8e97a0cc6f5af3cbc"
    contract["target_branch"] = "feat/h011-v3-discovery-refresh"
    contract["classified_duplicate_basenames"] = {
        "__init__.py": {
            "paths": [
                "polymarket/monitoring/__init__.py",
                "polymarket/paper/__init__.py",
            ],
            "classification": "DISTINCT_PACKAGE_MARKERS",
            "reason": (
                "These empty package markers establish separate monitoring and paper domains; "
                "they do not duplicate a subsystem or authority."
            ),
        }
    }
    contract["research_authoritative_root_reference_allowlist"] = {
        "research/H-011_v3_raw_artifact_transaction_design.md": {
            "classification": "NON_AUTHORITATIVE_HISTORICAL_DESIGN",
            "reason": (
                "Historical design evidence retained for audit; it has no runtime imports, "
                "writer capability, or production authority."
            ),
            "current_authority": (
                "polymarket/h011_v3_raw_transaction.py::publish_raw_scan and committed governance artifacts"
            ),
        }
    }
    contract["observed_production"]["mutation_authorized"] = True
    contract["observed_production"]["authorization_comment_id"] = 5188597690
    contract["workflow_policy"]["production_deploy_steps"] = True
    contract["workflow_policy"]["secrets_required"] = True
    contract["safety_invariants"]["production_mutation"] = {
        "value": True,
        "override": False,
        "scope": "PAPER_ONLY_SENEX_DEPLOYMENT_AUTHORIZED_BY_COMMENT_5188597690",
    }
    contract["northflank"]["existing_service_mutation_authorized"] = True
    contract["northflank"]["authorization_comment_id"] = 5188597690
    contract_path.write_text(
        json.dumps(contract, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    tests = Path("tests/governance/test_repository_contract.py")
    test_text = tests.read_text(encoding="utf-8")
    marker = '\nif __name__ == "__main__": unittest.main()\n'
    additions = textwrap.dedent(
        '''\

            def test_24_exact_package_marker_classification_passes(self):
                groups = {
                    "__init__.py": [
                        "polymarket/monitoring/__init__.py",
                        "polymarket/paper/__init__.py",
                    ]
                }
                contract = {
                    "classified_duplicate_basenames": {
                        "__init__.py": {
                            "paths": groups["__init__.py"],
                            "classification": "DISTINCT_PACKAGE_MARKERS",
                            "reason": "separate domains",
                        }
                    }
                }
                self.assertEqual(mod.unclassified_duplicate_paths(groups, contract), [])

            def test_25_extra_duplicate_path_still_fails(self):
                groups = {
                    "__init__.py": [
                        "polymarket/monitoring/__init__.py",
                        "polymarket/paper/__init__.py",
                        "polymarket/other/__init__.py",
                    ]
                }
                contract = {
                    "classified_duplicate_basenames": {
                        "__init__.py": {
                            "paths": groups["__init__.py"][:2],
                            "classification": "DISTINCT_PACKAGE_MARKERS",
                            "reason": "separate domains",
                        }
                    }
                }
                self.assertEqual(
                    mod.unclassified_duplicate_paths(groups, contract),
                    sorted(groups["__init__.py"]),
                )

            def test_26_classified_historical_research_reference_passes(self):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    path = root / "research" / "design.md"
                    path.parent.mkdir()
                    path.write_text("raw_chain_v1 historical design")
                    contract = {
                        "research_authoritative_root_reference_allowlist": {
                            "research/design.md": {
                                "classification": "NON_AUTHORITATIVE_HISTORICAL_DESIGN",
                                "reason": "retained evidence",
                                "current_authority": "runtime writer",
                            }
                        }
                    }
                    self.assertEqual(
                        mod.research_authority_violations(
                            root, ["research/design.md"], contract
                        ),
                        [],
                    )
                    self.assertEqual(
                        mod.research_authority_violations(
                            root, ["research/design.md"], {}
                        ),
                        ["research/design.md"],
                    )
        '''
    )
    additions = textwrap.indent(additions, "    ")
    test_text = replace_once(test_text, marker, additions + marker, "test insertion")
    tests.write_text(test_text, encoding="utf-8")


if __name__ == "__main__":
    main()
