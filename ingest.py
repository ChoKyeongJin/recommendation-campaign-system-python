from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


KOREAN_PARTICLES = (
    "으로부터",
    "에서부터",
    "에게서",
    "에서",
    "에게",
    "으로",
    "처럼",
    "부터",
    "까지",
    "보다",
    "께서",
    "와",
    "과",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "로",
    "도",
    "만",
    "께",
    "의",
)


@dataclass(frozen=True)
class NormalizationRule:
    rule_id: str
    canonical: str
    ko_label: str | None
    synonyms: tuple[str, ...]
    negative_synonyms: tuple[str, ...]


@dataclass(frozen=True)
class NormalizationResult:
    input: str
    normalized: str
    found: bool
    rule_id: str | None = None
    ko_label: str | None = None
    match_type: str | None = None
    matched_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "normalized": self.normalized,
            "found": self.found,
            "rule_id": self.rule_id,
            "ko_label": self.ko_label,
            "match_type": self.match_type,
            "matched_text": self.matched_text,
        }


class NormalizationIngester:
    def __init__(self, rules: Iterable[NormalizationRule]) -> None:
        self.rules = tuple(rules)
        self._lookup: dict[str, tuple[NormalizationRule, str, str]] = {}
        self._text_pattern: re.Pattern[str] | None = None
        self._build_indexes()

    @classmethod
    def from_file(cls, path: str | Path) -> "NormalizationIngester":
        with Path(path).open("r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NormalizationIngester":
        raw_rules = payload.get("normalization_rules")
        if not isinstance(raw_rules, list):
            raise ValueError("Input JSON must contain a normalization_rules list.")

        rules: list[NormalizationRule] = []
        for index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise ValueError(f"Rule at index {index} must be an object.")

            rule_id = _required_string(raw_rule, "rule_id", index)
            canonical = _required_string(raw_rule, "canonical", index)
            ko_label = _optional_string(raw_rule, "ko_label", index)
            synonyms = _string_tuple(raw_rule.get("synonyms", []), "synonyms", index)
            negative_synonyms = _string_tuple(
                raw_rule.get("negative_synonyms", []), "negative_synonyms", index
            )

            rules.append(
                NormalizationRule(
                    rule_id=rule_id,
                    canonical=canonical,
                    ko_label=ko_label,
                    synonyms=synonyms,
                    negative_synonyms=negative_synonyms,
                )
            )

        return cls(rules)

    def normalize_term(self, term: str) -> NormalizationResult:
        normalized_key = _normalize_key(term)
        lookup_value = self._lookup.get(normalized_key)
        if lookup_value is None:
            return NormalizationResult(input=term, normalized=term, found=False)

        rule, match_type, matched_text = lookup_value
        return NormalizationResult(
            input=term,
            normalized=rule.canonical,
            found=True,
            rule_id=rule.rule_id,
            ko_label=rule.ko_label,
            match_type=match_type,
            matched_text=matched_text,
        )

    def normalize_terms(self, terms: Iterable[str]) -> list[NormalizationResult]:
        return [self.normalize_term(term) for term in terms]

    def normalize_text(self, text: str) -> dict[str, Any]:
        matches: list[dict[str, str]] = []

        if self._text_pattern is None:
            return {"text": text, "matches": matches}

        def replace_match(match: re.Match[str]) -> str:
            lookup_value, particle = self._lookup_with_particle(match.group(0))
            if lookup_value is None:
                return match.group(0)

            rule, match_type, source_term = lookup_value
            matches.append(
                {
                    "matched_text": match.group(0),
                    "source_term": source_term,
                    "normalized": rule.canonical,
                    "rule_id": rule.rule_id,
                    "match_type": match_type,
                }
            )
            return rule.canonical + particle

        normalized_text = self._text_pattern.sub(replace_match, text)

        return {"text": normalized_text, "matches": matches}

    def _lookup_with_particle(
        self, matched_text: str
    ) -> tuple[tuple[NormalizationRule, str, str] | None, str]:
        lookup_value = self._lookup.get(_normalize_key(matched_text))
        if lookup_value is not None:
            return lookup_value, ""

        for particle in KOREAN_PARTICLES:
            if matched_text.endswith(particle):
                stem = matched_text[: -len(particle)]
                lookup_value = self._lookup.get(_normalize_key(stem))
                if lookup_value is not None:
                    return lookup_value, particle
        return None, ""

    def to_index(self) -> dict[str, Any]:
        terms = []
        for key, (rule, match_type, matched_text) in sorted(self._lookup.items()):
            terms.append(
                {
                    "term": matched_text,
                    "lookup_key": key,
                    "canonical": rule.canonical,
                    "rule_id": rule.rule_id,
                    "ko_label": rule.ko_label,
                    "match_type": match_type,
                }
            )
        return {"rule_count": len(self.rules), "term_count": len(terms), "terms": terms}

    def _build_indexes(self) -> None:
        indexed_terms: list[tuple[str, NormalizationRule, str]] = []

        for rule in self.rules:
            indexed_terms.extend(_rule_terms(rule))

        for term, rule, match_type in indexed_terms:
            normalized_key = _normalize_key(term)
            existing = self._lookup.get(normalized_key)
            if existing is not None:
                if existing[0].rule_id == rule.rule_id:
                    continue
                raise ValueError(
                    f"Term '{term}' is duplicated across rules "
                    f"'{existing[0].rule_id}' and '{rule.rule_id}'."
                )
            self._lookup[normalized_key] = (rule, match_type, term)

        sorted_terms = sorted(
            (lookup_value[2] for lookup_value in self._lookup.values()),
            key=len,
            reverse=True,
        )
        if sorted_terms:
            self._text_pattern = re.compile(
                "|".join(_term_regex(term) for term in sorted_terms),
                re.IGNORECASE,
            )


def _rule_terms(rule: NormalizationRule) -> list[tuple[str, NormalizationRule, str]]:
    terms = [(rule.canonical, rule, "canonical")]
    if rule.ko_label:
        terms.append((rule.ko_label, rule, "ko_label"))
    terms.extend((synonym, rule, "synonym") for synonym in rule.synonyms)
    terms.extend((synonym, rule, "negative_synonym") for synonym in rule.negative_synonyms)
    return [(term, source_rule, match_type) for term, source_rule, match_type in terms if term.strip()]


def _normalize_key(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip()).casefold()


def _term_regex(term: str) -> str:
    normalized_parts = [re.escape(part) for part in re.split(r"\s+", term.strip())]
    escaped_term = r"\s+".join(normalized_parts)
    boundary_chars = r"가-힣A-Za-z0-9_"
    if re.search(r"[가-힣]$", term):
        particles = "|".join(re.escape(particle) for particle in KOREAN_PARTICLES)
        return fr"(?<![{boundary_chars}]){escaped_term}(?:{particles})?(?![{boundary_chars}])"
    return fr"(?<![{boundary_chars}]){escaped_term}(?![{boundary_chars}])"


def _required_string(raw_rule: dict[str, Any], field_name: str, index: int) -> str:
    value = raw_rule.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Rule at index {index} must contain a non-empty {field_name} string.")
    return value.strip()


def _optional_string(raw_rule: dict[str, Any], field_name: str, index: int) -> str | None:
    value = raw_rule.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Rule at index {index} has an invalid {field_name}; expected string.")
    return value.strip() or None


def _string_tuple(value: Any, field_name: str, index: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Rule at index {index} has an invalid {field_name}; expected list.")

    terms: list[str] = []
    for term_index, term in enumerate(value):
        if not isinstance(term, str):
            raise ValueError(
                f"Rule at index {index} has an invalid {field_name}[{term_index}]; expected string."
            )
        stripped_term = term.strip()
        if stripped_term:
            terms.append(stripped_term)
    return tuple(terms)


def _load_terms_file(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as input_file:
        payload = json.load(input_file)

    if isinstance(payload, list):
        raw_terms = payload
    elif isinstance(payload, dict) and isinstance(payload.get("terms"), list):
        raw_terms = payload["terms"]
    else:
        raise ValueError("Terms JSON must be an array or an object with a terms array.")

    if not all(isinstance(term, str) for term in raw_terms):
        raise ValueError("Every term must be a string.")
    return raw_terms


def _write_json(payload: Any, output_path: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize campaign RAG/graph recommendation terms with a JSON dictionary."
    )
    parser.add_argument("rules", help="Path to the normalization rules JSON file.")
    parser.add_argument("--output", help="Optional path to write JSON output.")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--term", help="Single term to normalize.")
    mode.add_argument("--terms-file", help="JSON array or object with a terms array.")
    mode.add_argument("--text", help="Text to normalize by replacing known terms.")
    mode.add_argument("--text-file", help="UTF-8 text file to normalize.")
    mode.add_argument("--dump-index", action="store_true", help="Print the generated lookup index.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ingester = NormalizationIngester.from_file(args.rules)

    if args.term is not None:
        payload = ingester.normalize_term(args.term).to_dict()
    elif args.terms_file is not None:
        terms = _load_terms_file(args.terms_file)
        payload = {"results": [result.to_dict() for result in ingester.normalize_terms(terms)]}
    elif args.text is not None:
        payload = ingester.normalize_text(args.text)
    elif args.text_file is not None:
        text = Path(args.text_file).read_text(encoding="utf-8")
        payload = ingester.normalize_text(text)
    else:
        payload = ingester.to_index()

    _write_json(payload, args.output)


if __name__ == "__main__":
    main()