"""Test del parser/validatore tag_expr per /qualified filtri AND/OR/custom."""
from __future__ import annotations

import pytest

from app.agent.tag_expr import build_where_clause, parse_tag_expr, tokenize, validate


class TestTokenize:
    def test_simple(self):
        assert tokenize("F1", 1) == ["F1"]
        assert tokenize("F1 AND F2", 2) == ["F1", "AND", "F2"]
        assert tokenize("F1 OR F2", 2) == ["F1", "OR", "F2"]

    def test_case_insensitive(self):
        assert tokenize("f1 and f2", 2) == ["F1", "AND", "F2"]
        assert tokenize("F1 oR F2", 2) == ["F1", "OR", "F2"]

    def test_parens(self):
        assert tokenize("(F1 AND F2)", 2) == ["(", "F1", "AND", "F2", ")"]
        assert tokenize("((F1 OR F2) AND F3)", 3) == [
            "(", "(", "F1", "OR", "F2", ")", "AND", "F3", ")"
        ]

    def test_whitespace_tolerant(self):
        # Spazi multipli, leading/trailing
        assert tokenize("  F1   AND    F2  ", 2) == ["F1", "AND", "F2"]
        # No-space valido
        assert tokenize("(F1)", 1) == ["(", "F1", ")"]

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="vuota"):
            tokenize("", 1)
        with pytest.raises(ValueError, match="vuota"):
            tokenize("   ", 1)

    def test_out_of_range_filter(self):
        with pytest.raises(ValueError, match="fuori range"):
            tokenize("F5", 3)
        with pytest.raises(ValueError, match="fuori range"):
            tokenize("F0", 3)

    def test_unknown_token(self):
        with pytest.raises(ValueError, match="Caratteri non ammessi"):
            tokenize("F1; DROP TABLE assets", 1)
        with pytest.raises(ValueError, match="Caratteri non ammessi"):
            tokenize("F1 = 'x'", 1)

    def test_invalid_identifier(self):
        # "FOO" non e' un Fn
        with pytest.raises(ValueError, match="non riconosciuto"):
            tokenize("FOO", 1)


class TestValidate:
    def test_balanced_parens(self):
        validate(["F1"])
        validate(["(", "F1", ")"])
        validate(["(", "(", "F1", ")", ")"])

    def test_unbalanced_open(self):
        with pytest.raises(ValueError, match="manca chiusura"):
            validate(["(", "F1"])

    def test_unbalanced_close(self):
        with pytest.raises(ValueError, match="senza '\\('"):
            validate(["F1", ")"])

    def test_consecutive_operators(self):
        with pytest.raises(ValueError, match="non puo' essere seguito"):
            validate(["F1", "AND", "OR", "F2"])

    def test_missing_operator(self):
        with pytest.raises(ValueError, match="Manca un operatore"):
            validate(["F1", "F2"])

    def test_cant_start_with_operator(self):
        # AND iniziale: errore grammatica (controllato prima delle parentesi
        # se token "(" e ")" risultano bilanciati).
        with pytest.raises(ValueError, match="iniziare con"):
            validate(["AND", "F1"])
        # ")" iniziale: l'errore di parentesi sbilanciate viene prima, va bene
        # ugualmente (utente capisce comunque cosa c'e' che non va).
        with pytest.raises(ValueError):
            validate([")", "F1"])

    def test_cant_end_with_operator(self):
        with pytest.raises(ValueError, match="terminare con"):
            validate(["F1", "AND"])
        # "F1 AND (" — parentesi sbilanciate scattano prima
        with pytest.raises(ValueError):
            validate(["F1", "AND", "("])


class TestParse:
    def test_complex_valid(self):
        tokens = parse_tag_expr("((F1 AND F2) OR (F3 AND F4))", 4)
        assert tokens == [
            "(", "(", "F1", "AND", "F2", ")", "OR", "(", "F3", "AND", "F4", ")", ")"
        ]

    def test_single_filter(self):
        assert parse_tag_expr("F1", 1) == ["F1"]


class TestBuildWhereClause:
    def test_empty_filters_returns_empty(self):
        sql, params = build_where_clause([], "and")
        assert sql == ""
        assert params == []

    def test_and_mode(self):
        sql, params = build_where_clause(
            [("city", "Catania"), ("age", "25")], "and"
        )
        assert "EXISTS" in sql
        assert sql.count("EXISTS") == 2
        assert " AND " in sql
        assert params == ["city", "Catania", "age", "25"]

    def test_or_mode(self):
        sql, params = build_where_clause(
            [("city", "Catania"), ("age", "25")], "or"
        )
        assert sql.count("EXISTS") == 2
        # I 2 EXISTS sono uniti da OR. Notare che dentro ogni EXISTS c'e' un
        # AND letterale (tag_key AND tag_value), quindi non posso testare
        # "AND not in sql": verifico invece il pattern di join.
        # Se uniti da OR, il SQL contiene la sequenza ") OR EXISTS" (almeno una).
        assert ") OR EXISTS" in sql
        assert ") AND EXISTS" not in sql
        assert params == ["city", "Catania", "age", "25"]

    def test_custom_mode(self):
        sql, params = build_where_clause(
            [("city", "Catania"), ("age", "25"), ("int", "fitness")],
            "custom",
            expr="(F1 AND F2) OR F3",
        )
        # 3 EXISTS, e c'e' la struttura ( EXISTS AND EXISTS ) OR EXISTS
        assert sql.count("EXISTS") == 3
        # I params devono essere nell'ordine in cui appaiono nell'expression
        assert params == ["city", "Catania", "age", "25", "int", "fitness"]

    def test_custom_reorders_params(self):
        # Expression con Fn in ordine non sequenziale
        sql, params = build_where_clause(
            [("a", "1"), ("b", "2"), ("c", "3")],
            "custom",
            expr="F3 OR F1",
        )
        # Ordine params segue ordine token nell'expression
        assert params == ["c", "3", "a", "1"]
        # F2 non e' usato: e' lecito, l'utente puo' ignorare un filtro

    def test_custom_invalid_expr_raises(self):
        with pytest.raises(ValueError):
            build_where_clause(
                [("a", "1")], "custom", expr="F1; DROP"
            )

    def test_custom_missing_expr_raises(self):
        with pytest.raises(ValueError, match="richiede"):
            build_where_clause([("a", "1")], "custom", expr="")

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode invalido"):
            build_where_clause([("a", "1")], "xor")

    def test_sql_injection_attempt_blocked(self):
        # L'utente non puo' iniettare SQL: i tag_key/tag_value sono SOLO
        # parametri ($s), e l'expression non accetta caratteri ammessi.
        with pytest.raises(ValueError):
            build_where_clause(
                [("a", "1"), ("b", "2")],
                "custom",
                expr="F1 AND F2; DELETE FROM assets",
            )
