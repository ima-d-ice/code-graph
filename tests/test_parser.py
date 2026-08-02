"""Unit tests for the semantic parser (tree-sitter or ast fallback)."""
import pytest

from app.services.parser_service import SemanticParser, NodeType, EdgeType

SRC = """class Calculator:
    def add(self, a, b):
        return self.subtract(a, b)

    def subtract(self, a, b):
        return a - b
"""


@pytest.fixture
def parser():
    return SemanticParser()


def _parse_source(parser, src):
    """Parse via source text with a temp file path (parser reads file if no source)."""
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        return parser.parse_file(path, src)
    finally:
        os.unlink(path)


def test_extracts_class(parser):
    result = _parse_source(parser, SRC)
    classes = [n for n in result.nodes if n.node_type == NodeType.CLASS]
    assert any(n.name == "Calculator" for n in classes)


def test_extracts_qualified_function(parser):
    result = _parse_source(parser, SRC)
    funcs = [n.name for n in result.nodes if n.node_type == NodeType.FUNCTION]
    assert "Calculator.add" in funcs
    assert "Calculator.subtract" in funcs


def test_extracts_call_edge(parser):
    result = _parse_source(parser, SRC)
    calls = [e for e in result.edges if e.edge_type == EdgeType.CALLS]
    assert any(e.source_name == "Calculator.add" and "subtract" in e.target_name for e in calls)


def test_methods_metadata(parser):
    result = _parse_source(parser, SRC)
    calc = next(n for n in result.nodes
                if n.node_type == NodeType.CLASS and n.name == "Calculator")
    assert "add" in calc.metadata.get("methods", [])
    assert "subtract" in calc.metadata.get("methods", [])


def test_complexity_metadata(parser):
    complex_src = "def f(x):\n    if x:\n        return 1\n    for i in range(x):\n        pass\n    return 0\n"
    result = _parse_source(parser, complex_src)
    fn = next(n for n in result.nodes if n.node_type == NodeType.FUNCTION and n.name == "f")
    assert fn.metadata.get("complexity", 0) >= 3


def test_no_errors_on_valid_source(parser):
    result = _parse_source(parser, SRC)
    assert result.errors == []


def test_syntax_error_handling(parser):
    """tree-sitter is error-tolerant by design (partial tree, no errors);
    the ast fallback reports the SyntaxError. Either way: no crash."""
    result = _parse_source(parser, "def broken(:\n    pass\n")
    if parser.backend == "ast":
        assert result.errors
    else:
        assert result.errors == []
