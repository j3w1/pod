"""Bounded, validated renewal identity shared by every obligation reader."""
from __future__ import annotations

from .errors import PodError
from .util import digest

FROZEN = ("withdrawn", "renewed")
DEFINITION = ("kind", "provenance", "source", "parent", "check", "resolves", "stop_condition",
              "scope", "question", "insufficiency", "existing_evidence", "uncovered_risk",
              "proof_scope", "boundary", "unit", "audit_role", "adopts", "finding")


def copied_identity(row):
    identity = {field: row[field] for field in DEFINITION if field in row}
    if row.get("provenance") == "project_policy" and isinstance(identity.get("source"), dict):
        identity["source"] = {key: identity["source"][key] for key in ("path", "quote_sha256") if key in identity["source"]}
    return digest(identity)


def _invalid(detail):
    raise PodError("obligation_invalid", "renewal_invalid: " + detail,
                   {"detail": "renewal_invalid", "next_action": "restore the recorded reciprocal renewal lineage"})


class Lineage:
    def __init__(self, rows):
        from .contracts import bound
        values = rows.values() if isinstance(rows, dict) else rows
        if not isinstance(rows, (dict, list)) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in values):
            _invalid("lineage retains complete named rows")
        self.rows = rows if isinstance(rows, dict) else {row["id"]: row for row in rows}
        if len(self.rows) > bound("map", "obligations", "max_items"):
            _invalid("lineage exceeds the total row bound")
        for key, row in self.rows.items():
            link = row.get("renewal")
            if row.get("state") == "renewed":
                if (not isinstance(link, dict) or set(link) != {"by", "seq", "definition", "was_satisfied"}
                        or type(link["was_satisfied"]) is not bool
                        or not isinstance(link["by"], str) or link["by"] not in self.rows):
                    _invalid("renewed rows retain their unique successor")
                successor = self.rows[link["by"]]
                if (successor.get("renews") != key or type(link["seq"]) is not int
                        or type(row.get("introduced_seq")) is not int
                        or successor.get("introduced_seq") != link["seq"]
                        or row["introduced_seq"] >= link["seq"]):
                    _invalid("links are reciprocal and introductions strictly increase")
            elif link is not None:
                _invalid("only a renewed row has a successor")
            if "renews" in row:
                old = self.rows.get(row["renews"]) if isinstance(row["renews"], str) else None
                if old is None or old.get("state") != "renewed" or old.get("renewal", {}).get("by") != key:
                    _invalid("a replacement retains its predecessor")
                if old["renewal"].get("definition") != copied_identity(old):
                    _invalid("the frozen predecessor retains the copied definition identity")
                if any(row.get(field) != old.get(field) for field in ("kind", "provenance", "parent", "unit", "audit_role", "adopts", "finding")):
                    _invalid("ordinary immutable identity fields survive renewal")
        self.tips = {}
        for key in self.rows:
            cursor, seen = key, set()
            while self.rows[cursor].get("state") == "renewed":
                if cursor in seen or len(seen) >= len(self.rows):
                    _invalid("renewal cycles are forbidden")
                seen.add(cursor)
                cursor = self.rows[cursor]["renewal"]["by"]
            self.tips[key] = cursor

    def effective(self, key):
        return self.tips.get(key, key)

    def family(self, key):
        tip = self.effective(key)
        return [row for identity, row in self.rows.items() if self.effective(identity) == tip]

    def pending_proof(self, key):
        """Only a proof current at renewal can hold a tick; reopening ends the hold."""
        row = self.rows[key]
        while True:
            if any("accepted_seq" in receipt for receipt in row.get("receipts", [])):
                return None
            if "renews" not in row:
                return None
            row = self.rows[row["renews"]]
            if row["renewal"]["was_satisfied"]:
                return row

    def findings(self, key):
        """Retained findings keep their bytes; resolution uses the current correction."""
        result = []
        for row in self.family(key):
            for finding in row.get("findings", []):
                item = dict(finding)
                if item.get("triage") == "required_correction":
                    child = self.rows.get(self.effective(item.get("correction")))
                    if child is not None and child.get("state") == "satisfied":
                        accepted = [receipt["accepted_seq"] for receipt in child.get("receipts", [])
                                    if receipt.get("evidence") in child.get("evidence", [])
                                    and type(receipt.get("accepted_seq")) is int]
                        if accepted:
                            item["resolved_seq"] = max(accepted)
                        else:
                            item.pop("resolved_seq", None)
                    elif (child is not None and child.get("state") == "withdrawn"
                          and child.get("withdrawal", {}).get("by") == "agent_decision"):
                        from .relay import validate_agent_withdrawal
                        withdrawal = child["withdrawal"]
                        item["resolved_seq"] = validate_agent_withdrawal(withdrawal, introduced_seq=child.get("introduced_seq", 1))
                    elif child is None or child.get("state") != "withdrawn" or child.get("withdrawal", {}).get("by") != "user_direct":
                        item.pop("resolved_seq", None)
                result.append(item)
        return result
