"""Fixture-backed repositories (stand-ins for the insurer's systems of record)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import FIXTURES_DIR


@dataclass(frozen=True)
class Policyholder:
    party_id: str
    name: str
    policy_number: str
    dob: str
    id_type: str
    id_last4: str
    phone: str
    email: str
    name_aliases: tuple[str, ...] = ()
    phone_aliases: tuple[str, ...] = ()
    email_aliases: tuple[str, ...] = ()

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else ""

    @property
    def id_label(self) -> str:
        return "SSN" if self.id_type == "ssn_last4" else "national ID"


@dataclass
class Repository:
    policyholders: list[Policyholder]
    claims: list[dict[str, Any]]
    representatives: list[dict[str, Any]]
    guideline: dict[str, Any]
    claim_schema: dict[str, Any]
    consent_scenarios: dict[str, Any]
    _by_party: dict[str, Policyholder] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, fixtures_dir: Path | None = None) -> "Repository":
        d = fixtures_dir or FIXTURES_DIR

        def read(name: str) -> Any:
            return json.loads((d / name).read_text(encoding="utf-8"))

        holders = [
            Policyholder(
                party_id=p["party_id"],
                name=p["name"],
                policy_number=p["policy_number"],
                dob=p["dob"],
                id_type=p.get("id_type", "ssn_last4"),
                id_last4=p["id_last4"],
                phone=p["phone"],
                email=p["email"],
                name_aliases=tuple(p.get("name_aliases", [])),
                phone_aliases=tuple(p.get("phone_aliases", [])),
                email_aliases=tuple(p.get("email_aliases", [])),
            )
            for p in read("policyholders.json")
        ]
        repo = cls(
            policyholders=holders,
            claims=read("claims.json"),
            representatives=read("representatives.json"),
            guideline=read("required_document_guideline.json"),
            claim_schema=read("claim_schema.json"),
            consent_scenarios=read("consent_scenarios.json"),
        )
        repo._by_party = {p.party_id: p for p in holders}
        return repo

    # -- lookups -----------------------------------------------------------
    def policyholder(self, party_id: str | None) -> Policyholder | None:
        return self._by_party.get(party_id or "")

    def claims_for(self, party_id: str | None) -> list[dict[str, Any]]:
        return [c for c in self.claims if c.get("party_id") == party_id]

    def claim(self, case_id: str | None) -> dict[str, Any] | None:
        wanted = (case_id or "").strip().upper()
        for c in self.claims:
            if c.get("case_id", "").upper() == wanted:
                return c
        return None

    def representatives_for(self, party_id: str | None) -> list[dict[str, Any]]:
        return [r for r in self.representatives if r.get("buyer_party_id") == party_id]

    @staticmethod
    def claim_index_entry(claim: dict[str, Any]) -> dict[str, Any]:
        """The 'index card' view of a claim used while resolving which claim is meant."""
        return {
            "case_id": claim["case_id"],
            "case_type": claim.get("case_type"),
            "created_at": claim.get("created_at"),
            "status": claim.get("status"),
            "summary": claim.get("summary"),
        }

    @staticmethod
    def claim_public_record(claim: dict[str, Any]) -> dict[str, Any]:
        """Full claim record as disclosed to the verified caller (internal ids removed)."""
        return {k: v for k, v in claim.items() if k != "party_id"}
