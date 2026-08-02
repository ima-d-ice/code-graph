"""
Tree-sitter based semantic code parser.

Extracts rich structural information from Python source files:
- Module, Class, Function, Variable nodes
- Call, Import, Inheritance, Read, Mutate, Return edges

Falls back to Python's ast module if tree-sitter grammar fails to load.
"""

import ast
import os
import logging
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────

class NodeType(str, Enum):
    MODULE = "Module"
    CLASS = "Class"
    FUNCTION = "Function"
    VARIABLE = "Variable"


class EdgeType(str, Enum):
    CALLS = "CALLS"
    RETURNS = "RETURNS"
    MUTATES = "MUTATES"
    READS = "READS"
    INHERITS_FROM = "INHERITS_FROM"
    IMPORTS = "IMPORTS"
    CONTAINS = "CONTAINS"
    DEFINED_IN = "DEFINED_IN"


@dataclass
class ParsedNode:
    """A semantic unit extracted from source code."""
    node_type: NodeType
    name: str
    file_path: str
    start_line: int = 0
    end_line: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Extra fields packed into metadata:
    #   Module:   imports, exports
    #   Class:    bases, methods, decorators, docstring
    #   Function: class_owner, parameters, return_type, decorators,
    #             complexity, is_async, docstring
    #   Variable: scope, type_annotation, is_mutable


@dataclass
class ParsedEdge:
    """A relationship between two semantic nodes."""
    edge_type: EdgeType
    source_name: str
    target_name: str
    source_file: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FileParseResult:
    """Complete parse output for a single file."""
    file_path: str
    nodes: List[ParsedNode] = field(default_factory=list)
    edges: List[ParsedEdge] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────
# Tree-sitter Parser (Primary)
# ─────────────────────────────────────────────

_TS_AVAILABLE = False
_TS_PYTHON_LANGUAGE = None

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser as TSParser

    _TS_PYTHON_LANGUAGE = Language(tspython.language())
    _TS_AVAILABLE = True
    logger.info("✅ Tree-sitter Python grammar loaded")
except ImportError:
    logger.warning("⚠️ tree-sitter not available, falling back to ast module")
except Exception as e:
    logger.warning(f"⚠️ tree-sitter init failed ({e}), falling back to ast module")


class TreeSitterPythonParser:
    """
    Extracts rich semantic structure from Python files using tree-sitter.
    
    Tree-sitter advantages over ast:
    - Error-tolerant parsing (doesn't fail on syntax errors)
    - Byte-level position tracking
    - Multi-language support (future: JS/TS/Rust/Go)
    - Faster for large files
    """

    def __init__(self):
        if not _TS_AVAILABLE:
            raise RuntimeError("tree-sitter not available")
        self.parser = TSParser(_TS_PYTHON_LANGUAGE)

    def parse_file(self, file_path: str, source: Optional[str] = None) -> FileParseResult:
        """Parse a Python file and extract all semantic nodes and edges."""
        result = FileParseResult(file_path=file_path)

        if source is None:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception as e:
                result.errors.append(f"Failed to read file: {e}")
                return result

        try:
            tree = self.parser.parse(bytes(source, "utf-8"))
        except Exception as e:
            result.errors.append(f"Tree-sitter parse failed: {e}")
            return result

        root = tree.root_node
        self._extract_module(root, file_path, source, result)
        self._walk_top_level(root, file_path, source, result)

        return result

    # ── Module-level extraction ──

    def _extract_module(self, root, file_path: str, source: str, result: FileParseResult):
        """Extract module-level metadata: imports and exports."""
        module_name = os.path.splitext(os.path.basename(file_path))[0]

        imports: List[str] = []
        exports: List[str] = []

        for child in root.children:
            # Import statements
            if child.type in ("import_statement", "import_from_statement"):
                import_text = source[child.start_byte:child.end_byte].strip()
                imports.append(import_text)

            # __all__ assignment
            if child.type == "expression_statement":
                expr_text = source[child.start_byte:child.end_byte]
                if "__all__" in expr_text:
                    # Extract list items
                    for sub in self._walk_all(child):
                        if sub.type == "string":
                            val = source[sub.start_byte:sub.end_byte].strip("\"'")
                            exports.append(val)

            # Top-level function/class names as implicit exports
            if child.type in ("function_definition", "class_definition"):
                name_node = child.child_by_field_name("name")
                if name_node:
                    name = source[name_node.start_byte:name_node.end_byte]
                    if not name.startswith("_"):
                        exports.append(name)

        result.nodes.append(ParsedNode(
            node_type=NodeType.MODULE,
            name=module_name,
            file_path=file_path,
            start_line=1,
            end_line=root.end_point[0] + 1,
            metadata={"imports": imports, "exports": exports}
        ))

        # Create IMPORTS edges
        for imp_text in imports:
            # Extract module name from "from X import Y" or "import X"
            target = self._parse_import_target(imp_text)
            if target:
                result.edges.append(ParsedEdge(
                    edge_type=EdgeType.IMPORTS,
                    source_name=module_name,
                    target_name=target,
                    source_file=file_path
                ))

    def _parse_import_target(self, import_text: str) -> Optional[str]:
        """Extract the module name from an import statement."""
        parts = import_text.split()
        if len(parts) >= 2:
            if parts[0] == "from":
                return parts[1]
            elif parts[0] == "import":
                return parts[1].split(",")[0].strip()
        return None

    # ── Top-level walking ──

    def _walk_top_level(self, root, file_path: str, source: str, result: FileParseResult):
        """Walk top-level nodes to extract classes, functions, variables."""
        module_name = os.path.splitext(os.path.basename(file_path))[0]

        for child in root.children:
            if child.type == "class_definition":
                self._extract_class(child, file_path, source, result, owner=None)
            elif child.type == "function_definition":
                self._extract_function(child, file_path, source, result, class_owner=None)
            elif child.type == "decorated_definition":
                # Handle @decorator\ndef/class ...
                inner = self._get_decorated_inner(child)
                if inner:
                    if inner.type == "class_definition":
                        self._extract_class(inner, file_path, source, result, owner=None,
                                            decorator_node=child)
                    elif inner.type == "function_definition":
                        self._extract_function(inner, file_path, source, result,
                                                class_owner=None, decorator_node=child)
            elif child.type in ("expression_statement", "assignment"):
                self._extract_variable(child, file_path, source, result,
                                       scope=module_name, class_owner=None)

    # ── Class extraction ──

    def _extract_class(self, node, file_path: str, source: str,
                       result: FileParseResult, owner: Optional[str],
                       decorator_node=None):
        """Extract class node with bases, methods, decorators, docstring."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        class_name = source[name_node.start_byte:name_node.end_byte]

        # Bases (inheritance)
        bases: List[str] = []
        arg_list = node.child_by_field_name("superclasses")
        if arg_list:
            for arg_child in arg_list.children:
                if arg_child.type == "identifier":
                    bases.append(source[arg_child.start_byte:arg_child.end_byte])
                elif arg_child.type == "attribute":
                    bases.append(source[arg_child.start_byte:arg_child.end_byte])

        # Decorators
        decorators = self._extract_decorators(decorator_node or node, source)

        # Docstring
        docstring = self._extract_docstring(node, source)

        # Methods
        methods: List[str] = []
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "function_definition":
                    fn_name = child.child_by_field_name("name")
                    if fn_name:
                        methods.append(source[fn_name.start_byte:fn_name.end_byte])
                    self._extract_function(child, file_path, source, result,
                                            class_owner=class_name)
                elif child.type == "decorated_definition":
                    inner = self._get_decorated_inner(child)
                    if inner and inner.type == "function_definition":
                        fn_name = inner.child_by_field_name("name")
                        if fn_name:
                            methods.append(source[fn_name.start_byte:fn_name.end_byte])
                        self._extract_function(inner, file_path, source, result,
                                                class_owner=class_name, decorator_node=child)
                elif child.type in ("expression_statement", "assignment"):
                    self._extract_variable(child, file_path, source, result,
                                           scope=class_name, class_owner=class_name)

        result.nodes.append(ParsedNode(
            node_type=NodeType.CLASS,
            name=class_name,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            metadata={
                "bases": bases,
                "methods": methods,
                "decorators": decorators,
                "docstring": docstring,
            }
        ))

        # INHERITS_FROM edges
        for base in bases:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.INHERITS_FROM,
                source_name=class_name,
                target_name=base,
                source_file=file_path
            ))

        # CONTAINS edge from module
        module_name = os.path.splitext(os.path.basename(file_path))[0]
        result.edges.append(ParsedEdge(
            edge_type=EdgeType.CONTAINS,
            source_name=module_name,
            target_name=class_name,
            source_file=file_path
        ))

    # ── Function extraction ──

    def _extract_function(self, node, file_path: str, source: str,
                          result: FileParseResult, class_owner: Optional[str],
                          decorator_node=None):
        """Extract function node with parameters, return type, calls, etc."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        func_name = source[name_node.start_byte:name_node.end_byte]
        qualified_name = f"{class_owner}.{func_name}" if class_owner else func_name

        # Parameters
        parameters: List[Dict[str, str]] = []
        params_node = node.child_by_field_name("parameters")
        if params_node:
            parameters = self._extract_parameters(params_node, source)

        # Return type annotation
        return_type = None
        ret_node = node.child_by_field_name("return_type")
        if ret_node:
            return_type = source[ret_node.start_byte:ret_node.end_byte].strip()

        # Decorators
        decorators = self._extract_decorators(decorator_node or node, source)

        # Docstring
        docstring = self._extract_docstring(node, source)

        # Is async
        is_async = False
        # Check if parent or self indicates async
        line_text = source[node.start_byte:node.end_byte]
        if line_text.strip().startswith("async"):
            is_async = True

        # Complexity (count branches)
        complexity = self._compute_complexity(node, source)

        # Calls made by this function
        calls: List[str] = []
        reads: Set[str] = set()
        mutates: Set[str] = set()
        body = node.child_by_field_name("body")
        if body:
            self._extract_calls_and_refs(body, source, calls, reads, mutates, func_name)

        result.nodes.append(ParsedNode(
            node_type=NodeType.FUNCTION,
            name=qualified_name,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            metadata={
                "class_owner": class_owner,
                "parameters": parameters,
                "return_type": return_type,
                "decorators": decorators,
                "complexity": complexity,
                "is_async": is_async,
                "docstring": docstring,
                "simple_name": func_name,
            }
        ))

        # DEFINED_IN edge (function → class or module)
        container = class_owner or os.path.splitext(os.path.basename(file_path))[0]
        result.edges.append(ParsedEdge(
            edge_type=EdgeType.DEFINED_IN,
            source_name=qualified_name,
            target_name=container,
            source_file=file_path
        ))

        # CALLS edges
        for call_target in calls:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.CALLS,
                source_name=qualified_name,
                target_name=call_target,
                source_file=file_path
            ))

        # READS edges
        for read_target in reads:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.READS,
                source_name=qualified_name,
                target_name=read_target,
                source_file=file_path
            ))

        # MUTATES edges
        for mut_target in mutates:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.MUTATES,
                source_name=qualified_name,
                target_name=mut_target,
                source_file=file_path
            ))

        # RETURNS edge
        if return_type:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.RETURNS,
                source_name=qualified_name,
                target_name=return_type,
                source_file=file_path
            ))

    # ── Variable extraction ──

    def _extract_variable(self, node, file_path: str, source: str,
                          result: FileParseResult, scope: str,
                          class_owner: Optional[str]):
        """Extract variable/attribute assignments."""
        text = source[node.start_byte:node.end_byte]

        # Handle assignment
        assign_node = node if node.type == "assignment" else None
        if node.type == "expression_statement":
            for child in node.children:
                if child.type == "assignment":
                    assign_node = child
                    break

        if not assign_node:
            return

        left = assign_node.child_by_field_name("left")
        if not left:
            return

        var_name = source[left.start_byte:left.end_byte]

        # Type annotation
        type_ann = None
        type_node = assign_node.child_by_field_name("type")
        if type_node:
            type_ann = source[type_node.start_byte:type_node.end_byte]

        # Check mutability (rough heuristic: ALL_CAPS = constant)
        is_mutable = not var_name.isupper()

        result.nodes.append(ParsedNode(
            node_type=NodeType.VARIABLE,
            name=var_name,
            file_path=file_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            metadata={
                "scope": scope,
                "type_annotation": type_ann,
                "is_mutable": is_mutable,
                "class_owner": class_owner,
            }
        ))

    # ── Helper methods ──

    def _extract_parameters(self, params_node, source: str) -> List[Dict[str, str]]:
        """Extract function parameters with type annotations."""
        params = []
        for child in params_node.children:
            if child.type in ("identifier", "typed_parameter",
                              "default_parameter", "typed_default_parameter"):
                param_info: Dict[str, str] = {}

                if child.type == "identifier":
                    param_info["name"] = source[child.start_byte:child.end_byte]
                elif child.type == "typed_parameter":
                    name_n = child.children[0] if child.children else None
                    type_n = child.child_by_field_name("type")
                    if name_n:
                        param_info["name"] = source[name_n.start_byte:name_n.end_byte]
                    if type_n:
                        param_info["type"] = source[type_n.start_byte:type_n.end_byte]
                elif child.type in ("default_parameter", "typed_default_parameter"):
                    name_n = child.child_by_field_name("name")
                    value_n = child.child_by_field_name("value")
                    type_n = child.child_by_field_name("type")
                    if name_n:
                        param_info["name"] = source[name_n.start_byte:name_n.end_byte]
                    if type_n:
                        param_info["type"] = source[type_n.start_byte:type_n.end_byte]
                    if value_n:
                        param_info["default"] = source[value_n.start_byte:value_n.end_byte]

                if param_info.get("name") and param_info["name"] not in ("(", ")", ","):
                    params.append(param_info)
        return params

    def _extract_decorators(self, node, source: str) -> List[str]:
        """Extract decorator strings from a decorated definition or function/class."""
        decorators = []
        for child in node.children:
            if child.type == "decorator":
                dec_text = source[child.start_byte:child.end_byte].strip()
                decorators.append(dec_text)
        return decorators

    def _extract_docstring(self, node, source: str) -> Optional[str]:
        """Extract docstring from function or class body."""
        body = node.child_by_field_name("body")
        if not body or not body.children:
            return None

        first = body.children[0]
        if first.type == "expression_statement":
            for child in first.children:
                if child.type == "string":
                    raw = source[child.start_byte:child.end_byte]
                    # Strip triple quotes
                    for q in ('"""', "'''"):
                        if raw.startswith(q) and raw.endswith(q):
                            return raw[3:-3].strip()
                    return raw.strip("\"'")
        return None

    def _compute_complexity(self, node, source: str) -> int:
        """Compute cyclomatic complexity (count decision points + 1)."""
        complexity = 1
        branch_types = {"if_statement", "elif_clause", "for_statement",
                        "while_statement", "except_clause", "with_statement",
                        "boolean_operator", "conditional_expression"}
        for child in self._walk_all(node):
            if child.type in branch_types:
                complexity += 1
        return complexity

    def _extract_calls_and_refs(self, body, source: str,
                                 calls: List[str], reads: Set[str],
                                 mutates: Set[str], current_func: str):
        """Walk function body to find calls, reads, and mutations."""
        for child in self._walk_all(body):
            # Function calls
            if child.type == "call":
                func_node = child.child_by_field_name("function")
                if func_node:
                    call_name = source[func_node.start_byte:func_node.end_byte]
                    # Skip built-in / common noise
                    if call_name not in ("print", "len", "str", "int", "float",
                                         "bool", "list", "dict", "set", "tuple",
                                         "type", "isinstance", "super", "range",
                                         "enumerate", "zip", "map", "filter"):
                        calls.append(call_name)

            # Attribute reads: self.x (in non-assignment context)
            if child.type == "attribute":
                attr_text = source[child.start_byte:child.end_byte]
                if attr_text.startswith("self."):
                    attr_name = attr_text[5:]  # strip "self."
                    reads.add(attr_name)

            # Assignments (mutations)
            if child.type == "assignment":
                left = child.child_by_field_name("left")
                if left:
                    left_text = source[left.start_byte:left.end_byte]
                    if left_text.startswith("self."):
                        mutates.add(left_text[5:])
                    elif "." in left_text:
                        mutates.add(left_text)

    def _get_decorated_inner(self, decorated_node):
        """Get the actual definition from a decorated_definition node."""
        for child in decorated_node.children:
            if child.type in ("function_definition", "class_definition"):
                return child
        return None

    def _walk_all(self, node):
        """Recursively yield all descendant nodes."""
        cursor = node.walk()
        visited = False

        while True:
            if not visited:
                yield cursor.node
                if cursor.goto_first_child():
                    continue
            if cursor.goto_next_sibling():
                visited = False
                continue
            if cursor.goto_parent():
                visited = True
                continue
            break


# ─────────────────────────────────────────────
# AST Fallback Parser
# ─────────────────────────────────────────────

class AstFallbackParser:
    """
    Fallback parser using Python's built-in ast module.
    Less capable than tree-sitter but always available.
    """

    def parse_file(self, file_path: str, source: Optional[str] = None) -> FileParseResult:
        result = FileParseResult(file_path=file_path)

        if source is None:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    source = f.read()
            except Exception as e:
                result.errors.append(f"Failed to read file: {e}")
                return result

        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as e:
            result.errors.append(f"SyntaxError: {e}")
            return result

        module_name = os.path.splitext(os.path.basename(file_path))[0]

        # Module node
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = ", ".join(a.name for a in node.names)
                imports.append(f"from {module} import {names}")

        result.nodes.append(ParsedNode(
            node_type=NodeType.MODULE,
            name=module_name,
            file_path=file_path,
            start_line=1,
            end_line=len(source.splitlines()),
            metadata={"imports": imports, "exports": []}
        ))

        # Walk top-level
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                self._extract_class_ast(node, file_path, source, result, module_name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._extract_function_ast(node, file_path, source, result,
                                            class_owner=None, module_name=module_name)

        return result

    def _extract_class_ast(self, node: ast.ClassDef, file_path: str,
                           source: str, result: FileParseResult, module_name: str):
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))

        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(item.name)
                self._extract_function_ast(item, file_path, source, result,
                                            class_owner=node.name, module_name=module_name)

        decorators = [ast.unparse(d) for d in node.decorator_list]
        docstring = ast.get_docstring(node)

        result.nodes.append(ParsedNode(
            node_type=NodeType.CLASS,
            name=node.name,
            file_path=file_path,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            metadata={
                "bases": bases,
                "methods": methods,
                "decorators": decorators,
                "docstring": docstring,
            }
        ))

        for base in bases:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.INHERITS_FROM,
                source_name=node.name,
                target_name=base,
                source_file=file_path
            ))

        result.edges.append(ParsedEdge(
            edge_type=EdgeType.CONTAINS,
            source_name=module_name,
            target_name=node.name,
            source_file=file_path
        ))

    def _extract_function_ast(self, node, file_path: str, source: str,
                               result: FileParseResult, class_owner: Optional[str],
                               module_name: str):
        func_name = node.name
        qualified = f"{class_owner}.{func_name}" if class_owner else func_name
        is_async = isinstance(node, ast.AsyncFunctionDef)

        # Parameters
        params = []
        for arg in node.args.args:
            p: Dict[str, str] = {"name": arg.arg}
            if arg.annotation:
                p["type"] = ast.unparse(arg.annotation)
            params.append(p)

        # Return type
        return_type = ast.unparse(node.returns) if node.returns else None

        # Decorators
        decorators = [ast.unparse(d) for d in node.decorator_list]

        # Docstring
        docstring = ast.get_docstring(node)

        # Calls
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    if child.func.id not in ("print", "len", "str", "int", "float",
                                              "bool", "list", "dict", "set", "tuple",
                                              "type", "isinstance", "super", "range"):
                        calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.append(ast.unparse(child.func))

        # Complexity
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                                  ast.With, ast.BoolOp, ast.IfExp)):
                complexity += 1

        result.nodes.append(ParsedNode(
            node_type=NodeType.FUNCTION,
            name=qualified,
            file_path=file_path,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            metadata={
                "class_owner": class_owner,
                "parameters": params,
                "return_type": return_type,
                "decorators": decorators,
                "complexity": complexity,
                "is_async": is_async,
                "docstring": docstring,
                "simple_name": func_name,
            }
        ))

        container = class_owner or module_name
        result.edges.append(ParsedEdge(
            edge_type=EdgeType.DEFINED_IN,
            source_name=qualified,
            target_name=container,
            source_file=file_path
        ))

        for call_target in calls:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.CALLS,
                source_name=qualified,
                target_name=call_target,
                source_file=file_path
            ))

        if return_type:
            result.edges.append(ParsedEdge(
                edge_type=EdgeType.RETURNS,
                source_name=qualified,
                target_name=return_type,
                source_file=file_path
            ))


# ─────────────────────────────────────────────
# Public API (auto-selects best parser)
# ─────────────────────────────────────────────

class SemanticParser:
    """
    Unified parser interface.
    Uses tree-sitter if available, falls back to ast.
    """

    def __init__(self):
        if _TS_AVAILABLE:
            self._parser = TreeSitterPythonParser()
            self._backend = "tree-sitter"
        else:
            self._parser = AstFallbackParser()
            self._backend = "ast"
        logger.info(f"🔬 SemanticParser initialized with {self._backend} backend")

    @property
    def backend(self) -> str:
        return self._backend

    def parse_file(self, file_path: str, source: Optional[str] = None) -> FileParseResult:
        """Parse a single file."""
        return self._parser.parse_file(file_path, source)

    def parse_directory(self, directory: str,
                        extensions: Optional[List[str]] = None) -> List[FileParseResult]:
        """Parse all matching files in a directory tree."""
        if extensions is None:
            extensions = [".py"]

        results = []
        for root, _, files in os.walk(directory):
            for fname in sorted(files):
                if any(fname.endswith(ext) for ext in extensions):
                    full_path = os.path.join(root, fname)
                    try:
                        result = self.parse_file(full_path)
                        results.append(result)
                        node_count = len(result.nodes)
                        edge_count = len(result.edges)
                        logger.debug(f"  Parsed {fname}: {node_count} nodes, {edge_count} edges")
                    except Exception as e:
                        logger.error(f"  Failed to parse {fname}: {e}")
                        results.append(FileParseResult(
                            file_path=full_path,
                            errors=[str(e)]
                        ))
        return results
