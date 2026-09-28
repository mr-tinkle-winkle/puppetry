"""
The block editor's data model -- Qt-free, so it can be tested on its own.

Blocks are just another VIEW of a macro's `code` string (the one source
of truth). This module converts both ways:

    code_to_blocks(code)  -> Doc   (via Python's own `ast`)
    blocks_to_code(doc)   -> str

Fidelity rules (the part that matters most):
  * Nothing is ever dropped. Any statement no block understands becomes a
    "raw" (custom code) block holding its exact source lines.
  * Comments survive (as note blocks, or inside raw blocks), and so do
    blank lines (Block.blank_before) and trailing "# ..." comments.
  * A block that hasn't been edited since it was parsed re-emits its
    ORIGINAL source text (Block.src), so odd spacing like `tap( KEY_A )`
    comes back byte-for-byte even after the block is moved.
  * The editor additionally never regenerates code at all if the blocks
    weren't touched (see editor_page.py), so merely looking at a macro in
    Block view can't change it.

Sockets hold either SOURCE TEXT (str) or a REPORTER (Rep: a variable,
true/false, a comparison, "key is held", the mouse position...), and a
list socket (combo keys, macro arguments, ...) holds a list of those.

Tree, not a list: a loop's/if's children live in Block.bodies, one list
per "mouth". A Doc holds the main stack (what gets compiled), this
macro's functions (`def` blocks, each its own stack), free-floating notes
(saved as `#@note x,y: text` comment lines), and any loose stacks lying
around the canvas (never compiled -- the UI warns).
"""
from __future__ import annotations

import ast
import copy
import itertools
import re
from dataclasses import dataclass, field

from reference import PRIMITIVES_BY_NAME

COMPOUND_KINDS = frozenset({"repeat", "for", "while", "if", "def"})
CHANGE_OPS = ("+", "-", "*", "/")
_AUG_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
COMPARE_OPS = (("==", "equal to"), ("!=", "not equal to"), (">", "greater than"), ("<", "less than"),
               (">=", "greater than or equal to"), ("<=", "less than or equal to"))
_CMP_NODES = {ast.Eq: "==", ast.NotEq: "!=", ast.Gt: ">", ast.Lt: "<", ast.GtE: ">=", ast.LtE: "<="}
NOTE_RE = re.compile(r"^\s*#@note\s+(-?\d+)\s*,\s*(-?\d+)\s*:\s?(.*)$")

_ids = itertools.count(1)


# ---------------------------------------------------------------------------
# reporters (values that live inside sockets)
# ---------------------------------------------------------------------------

@dataclass
class Rep:
    kind: str            # var | bool | compare | held | mouse | buttons
    name: str = ""       # var: its name; bool: "True"/"False"; compare: op; mouse: "" | "x" | "y"
    fields: dict = field(default_factory=dict)   # compare: left/right; held: key  (str | Rep)

    def copy(self) -> "Rep":
        return Rep(self.kind, self.name, {k: copy_value(v) for k, v in self.fields.items()})


def copy_value(v):
    if isinstance(v, Rep):
        return v.copy()
    if isinstance(v, list):
        return [copy_value(x) for x in v]
    return v


def is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, list):
        return all(is_empty(x) for x in v)
    return False


def expr_text(v) -> str:
    """Socket value -> Python source."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return ", ".join(t for t in (expr_text(x) for x in v) if t)
    k = v.kind
    if k == "var":
        return v.name
    if k == "bool":
        return "True" if v.name == "True" else "False"
    if k == "compare":
        return f"{_operand(v.fields.get('left'))} {v.name or '=='} {_operand(v.fields.get('right'))}"
    if k == "held":
        return f"{_operand(v.fields.get('key'), 'KEY_A')} in getButtonsHeld()"
    if k == "mouse":
        return "getMousePosition" + (f".{v.name}" if v.name in ("x", "y") else "()")
    if k == "buttons":
        return "getButtonsHeld()"
    if k == "press":
        parts = []
        btn = expr_text(v.fields.get("button"))
        if btn:
            parts.append(btn)
        rep = v.fields.get("repress")
        if isinstance(rep, Rep) and rep.kind == "bool" and rep.name == "True" or rep == "True":
            parts.append("repress=True")
        return f"waitForPress({', '.join(parts)})"
    return ""


def _operand(v, fallback: str = "None") -> str:
    t = expr_text(v) or fallback
    if isinstance(v, Rep) and v.kind in ("compare", "held"):
        return f"({t})"
    return t


def rep_label(r: Rep) -> str:
    if r.kind == "var":
        return r.name
    if r.kind == "bool":
        return "true" if r.name == "True" else "false"
    if r.kind == "mouse":
        return {"x": "mouse x", "y": "mouse y"}.get(r.name, "mouse position")
    if r.kind == "buttons":
        return "buttons held"
    if r.kind == "press":
        return "key pressed"
    return r.kind


def new_rep(kind: str, **kw) -> Rep:
    if kind == "var":
        return Rep("var", kw.get("name", "x"))
    if kind == "bool":
        return Rep("bool", "True" if kw.get("value", True) in (True, "True") else "False")
    if kind == "compare":
        return Rep("compare", kw.get("op", "=="), {"left": "", "right": ""})
    if kind == "held":
        return Rep("held", "", {"key": kw.get("key", "KEY_A")})
    if kind == "mouse":
        return Rep("mouse", kw.get("part", ""))
    if kind == "buttons":
        return Rep("buttons")
    if kind == "press":
        return Rep("press", "", {"button": "", "repress": Rep("bool", "False")})
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

@dataclass
class Block:
    kind: str                      # call | macro_call | func_call | custom | arguments | repeat | for | while |
                                   # if | def | return | assign | change | comment | raw
    name: str = ""                 # primitive / macro / function / custom-block name
    fields: dict = field(default_factory=dict)   # socket -> str | Rep | list
    kw: set = field(default_factory=set)         # call params that were passed by keyword
    bodies: list = field(default_factory=list)   # list[list[Block]] -- one per mouth
    has_else: bool = False         # if: the last body is the else-body
    blank_before: int = 0
    trailing: str = ""             # e.g. "  # note" after a simple statement
    header_trailing: list = field(default_factory=list)  # per header line of a compound block
    src: "list | None" = None      # [(line, is_absolute)] original text, reused until edited
    expanded: bool = False         # UI only: show every optional socket
    x: float = 0.0                 # def blocks: canvas position (UI only)
    y: float = 0.0
    uid: int = field(default_factory=lambda: next(_ids))

    def branch_count(self) -> int:
        """if: number of condition branches (if + elifs)."""
        return len(self.bodies) - (1 if self.has_else else 0)

    def touch(self) -> None:
        """Mark edited: from now on it's regenerated from its fields."""
        self.src = None

    def copy_tree(self) -> "Block":
        """Fast deep copy that keeps uids (undo snapshots). `src` is shared:
        it's never mutated in place, only replaced."""
        c = Block.__new__(Block)
        c.__dict__.update(self.__dict__)
        c.fields = {k: copy_value(v) for k, v in self.fields.items()}
        c.kw = set(self.kw)
        c.header_trailing = list(self.header_trailing)
        c.bodies = [[b.copy_tree() for b in body] for body in self.bodies]
        return c

    def clone(self) -> "Block":
        c = copy.deepcopy(self)
        for b in walk([c]):
            b.uid = next(_ids)
        return c


@dataclass
class Stack:
    blocks: list
    x: float = 0.0
    y: float = 0.0


@dataclass
class Note:
    """A free-floating note on the canvas (not attached to anything)."""
    text: str
    x: float = 0.0
    y: float = 0.0
    uid: int = field(default_factory=lambda: next(_ids))


@dataclass
class Doc:
    main: list = field(default_factory=list)       # the macro, top to bottom
    functions: list = field(default_factory=list)  # Block(kind="def"), each its own stack
    notes: list = field(default_factory=list)      # [Note]
    loose: list = field(default_factory=list)      # [Stack], not compiled
    indent: str = "    "
    final_newline: bool = True
    tail_blank: int = 0
    main_x: float = 40.0
    main_y: float = 50.0


def copy_blocks(blocks) -> list:
    return [b.copy_tree() for b in blocks]


def walk(blocks):
    for b in blocks:
        yield b
        for body in b.bodies:
            yield from walk(body)


def walk_doc(doc: Doc):
    yield from walk(doc.main)
    yield from walk(doc.functions)


def walk_values(v):
    """Every Rep/str inside a socket value (for renames etc.)."""
    if isinstance(v, list):
        for x in v:
            yield from walk_values(x)
    elif isinstance(v, Rep):
        yield v
        for x in v.fields.values():
            yield from walk_values(x)
    else:
        yield v


def _names_in_param_list(items) -> list[str]:
    out = []
    for part in items:
        n = (part if isinstance(part, str) else "").split("=", 1)[0].strip()
        if n:
            out.append(n)
    return out


def variable_names(blocks) -> list[str]:
    """Names this script creates: arguments() params, assignments, loop vars,
    function parameters."""
    names: list[str] = []

    def add(n):
        n = (n or "").strip()
        if n.isidentifier() and n != "_" and n not in names:
            names.append(n)

    for b in walk(blocks):
        if b.kind in ("assign", "change"):
            for n in str(b.fields.get("name") or "").split(","):
                add(n)
        elif b.kind == "for":
            add(b.fields.get("var"))
        elif b.kind in ("arguments", "def"):
            for n in _names_in_param_list(b.fields.get("params") or []):
                add(n)
    return names


def function_names(doc: Doc) -> list[str]:
    return [f.name for f in doc.functions if f.name]


# ---------------------------------------------------------------------------
# custom block library (global; see custom_blocks.py for storage)
# ---------------------------------------------------------------------------

@dataclass
class CustomArg:
    name: str
    default: str = "0"
    source: str = "any"     # any | keys | mouse | keys_mouse | list
    options: list = field(default_factory=list)


@dataclass
class CustomDef:
    name: str                          # label shown on the block, e.g. "click at"
    func: str = ""                     # the callable name (sanitized), e.g. "click_at"
    category: str = "My blocks"
    color: str = "#9b6ad6"
    args: list = field(default_factory=list)     # [CustomArg]
    template: str = ""                 # code with {arg} placeholders
    python_on: bool = False
    description: str = ""

    def arg_names(self) -> list[str]:
        return [a.name for a in self.args]


def custom_body_code(d: CustomDef) -> str:
    """What the daemon compiles for a custom block: an arguments() line
    from its args, then the template with {arg} -> arg."""
    params = ", ".join(f"{a.name}={a.default.strip() or '0'}" for a in d.args)
    body = d.template
    for a in d.args:
        body = body.replace("{" + a.name + "}", a.name)
    lines = []
    if d.args:
        lines.append(f"arguments({params})")
    lines.append(body.rstrip("\n") or "pass")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# block factories (palette templates)
# ---------------------------------------------------------------------------

def prim_param(prim_name: str, param_name: str):
    prim = PRIMITIVES_BY_NAME.get(prim_name)
    if prim is None:
        return None
    for p in prim.params:
        if p.name == param_name:
            return p
    return None


_STARTERS = {"key": "KEY_A", "keys": ["KEY_LEFTCTRL", "KEY_C"], "acting": ["KEY_B"], "text": '"hello"',
             "cmd": '"notify-send hi"', "x_pixels": "100", "y_pixels": "0", "amount": "1", "time_": "0.1",
             "multiplier": "1", "what": '"keyboard"', "button": "KEY_F8"}


def new_call(name: str) -> Block:
    prim = PRIMITIVES_BY_NAME[name]
    fields_ = {}
    for p in prim.params:
        fields_[p.name] = [] if p.vararg else ""
    for p in prim.params:
        if p.name in _STARTERS and (p.required or p.vararg):
            v = _STARTERS[p.name]
            fields_[p.name] = list(v) if isinstance(v, list) else v
        if p.required and p.kind == "bool":
            fields_[p.name] = Rep("bool", "False")
    return Block("call", name=name, fields=fields_)


def new_block(kind: str, **kw) -> Block:
    if kind == "call":
        return new_call(kw["name"])
    if kind == "repeat":
        return Block("repeat", fields={"count": kw.get("count", "10")}, bodies=[[]], header_trailing=[""])
    if kind == "for":
        return Block("for", fields={"var": "i", "iter": "range(10)"}, bodies=[[]], header_trailing=[""])
    if kind == "while":
        return Block("while", fields={"cond": Rep("bool", "True")}, bodies=[[]], header_trailing=[""])
    if kind == "if":
        has_else = kw.get("has_else", False)
        return Block("if", fields={"cond0": new_rep("compare")}, bodies=[[], []] if has_else else [[]],
                     has_else=has_else, header_trailing=["", ""] if has_else else [""])
    if kind == "assign":
        return Block("assign", fields={"name": kw.get("var", "x"), "value": "0"})
    if kind == "change":
        return Block("change", fields={"name": kw.get("var", "x"), "op": "+", "value": "1"})
    if kind == "comment":
        return Block("comment", fields={"text": " note"})
    if kind == "raw":
        return Block("raw", fields={"code": kw.get("code", "pass")})
    if kind == "arguments":
        return Block("arguments", fields={"params": list(kw.get("params", ["count=3"]))})
    if kind == "macro_call":
        return Block("macro_call", name=kw.get("name", ""), fields={"args": []})
    if kind == "func_call":
        return Block("func_call", name=kw.get("name", ""), fields={"args": []})
    if kind == "def":
        return Block("def", name=kw.get("name", "my_function"), fields={"params": []}, bodies=[[]],
                     header_trailing=[""])
    if kind == "return":
        return Block("return", fields={"value": ""})
    if kind == "custom":
        d: CustomDef = kw["defn"]
        return Block("custom", name=d.func, fields={a.name: a.default for a in d.args})
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# blocks -> code
# ---------------------------------------------------------------------------

def render_call(b: Block) -> str:
    prim = PRIMITIVES_BY_NAME.get(b.name)
    if prim is None:
        return f"{b.name}()"
    has_vararg_value = any(p.vararg and not is_empty(b.fields.get(p.name)) for p in prim.params)
    pos, kws = [], []
    positional_open = True
    for p in prim.params:
        val = b.fields.get(p.name)
        v = expr_text(val)
        if p.vararg:
            if v:
                pos.append(v)
            continue
        if not v:
            if not p.kw_only:
                positional_open = False
            continue
        as_kw = p.kw_only or not positional_open or (p.name in b.kw and not has_vararg_value)
        if as_kw:
            kws.append(f"{p.name}={v}")
            if not p.kw_only:
                positional_open = False
        else:
            pos.append(v)
    return f"{b.name}({', '.join(pos + kws)})"


def header_text(b: Block, branch: int = 0) -> str:
    """The `...:` line(s) of a compound block. For if: branch index, or
    branch == branch_count() for the else line."""
    f = b.fields
    if b.kind == "repeat":
        return f"for _ in range({expr_text(f.get('count')) or '1'}):"
    if b.kind == "for":
        return f"for {expr_text(f.get('var')) or '_'} in {expr_text(f.get('iter')) or 'range(1)'}:"
    if b.kind == "while":
        return f"while {expr_text(f.get('cond')) or 'True'}:"
    if b.kind == "if":
        if b.has_else and branch == b.branch_count():
            return "else:"
        word = "if" if branch == 0 else "elif"
        return f"{word} {expr_text(f.get(f'cond{branch}')) or 'True'}:"
    if b.kind == "def":
        return f"def {b.name or 'my_function'}({expr_text(f.get('params'))}):"
    raise ValueError(b.kind)


def simple_text(b: Block, customs: "dict | None" = None) -> str:
    f = b.fields
    if b.kind == "call":
        return render_call(b)
    if b.kind in ("macro_call", "func_call"):
        return f"{b.name}({expr_text(f.get('args'))})"
    if b.kind == "custom":
        d = (customs or {}).get(b.name)
        names = d.arg_names() if d else list(f.keys())
        vals = [expr_text(f.get(n)) or (next((a.default for a in d.args if a.name == n), "0") if d else "0")
                for n in names]
        return f"{b.name}({', '.join(vals)})"
    if b.kind == "arguments":
        return f"arguments({expr_text(f.get('params'))})"
    if b.kind == "assign":
        return f"{(f.get('name') or 'x').strip()} = {expr_text(f.get('value')) or '0'}"
    if b.kind == "change":
        op = f.get("op") if f.get("op") in CHANGE_OPS else "+"
        return f"{(f.get('name') or 'x').strip()} {op}= {expr_text(f.get('value')) or '1'}"
    if b.kind == "return":
        v = expr_text(f.get("value"))
        return f"return {v}" if v else "return"
    if b.kind == "comment":
        return "#" + f.get("text", "")
    if b.kind == "raw":
        return f.get("code", "")
    raise ValueError(b.kind)


def note_line(n: Note) -> str:
    text = n.text.replace("\n", " ")
    return f"#@note {int(round(n.x))},{int(round(n.y))}: {text}"


def blocks_to_code(doc_or_blocks, indent: str | None = None, customs: "dict | None" = None) -> str:
    if isinstance(doc_or_blocks, Doc):
        doc = doc_or_blocks
    else:
        doc = Doc(main=list(doc_or_blocks))
    unit = indent or doc.indent or "    "
    out: list[str] = []

    def emit_lines(text_lines, depth, trailing=""):
        ind = unit * depth
        for i, (line, absolute) in enumerate(text_lines):
            s = line if (absolute or line == "") else ind + line
            if i == len(text_lines) - 1:
                s += trailing
            out.append(s)

    def emit(blocks, depth):
        for b in blocks:
            out.extend([""] * b.blank_before)
            if b.kind in COMPOUND_KINDS:
                n_headers = len(b.bodies)
                trailers = list(b.header_trailing) + [""] * n_headers
                for k in range(n_headers):
                    out.append(unit * depth + header_text(b, k) + trailers[k])
                    if b.bodies[k]:
                        emit(b.bodies[k], depth + 1)
                    else:
                        out.append(unit * (depth + 1) + "pass")
                continue
            if b.src is not None:
                emit_lines(b.src, depth, b.trailing)
            elif b.kind == "raw":
                emit_lines([(ln, False) for ln in b.fields.get("code", "").split("\n")], depth)
            else:
                emit_lines([(simple_text(b, customs), False)], depth, b.trailing)

    main = list(doc.main)
    head = []
    if main and main[0].kind == "arguments":
        head = [main.pop(0)]
    emit(head, 0)
    emit(doc.functions, 0)
    emit(main, 0)
    for n in doc.notes:
        out.append(note_line(n))
    text = "\n".join(out)
    if out and doc.final_newline:
        text += "\n"
    text += "\n" * doc.tail_blank
    return text


# ---------------------------------------------------------------------------
# code -> blocks
# ---------------------------------------------------------------------------

class BlockParseError(Exception):
    def __init__(self, msg: str, line: int = 0):
        super().__init__(msg)
        self.line = line


_HEADER_TAIL = re.compile(r"^\s*:\s*(#.*)?$")
_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\((.*)\)\s*:\s*(#.*)?$")


def split_top_level(s: str) -> list[str]:
    parts, depth, cur, quote = [], 0, "", None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            cur += c
            if c == "\\" and i + 1 < len(s):
                cur += s[i + 1]
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur += c
        elif c in "([{":
            depth += 1
            cur += c
        elif c in ")]}":
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _collect_variables(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "arguments"):
            for k in node.keywords:
                if k.arg:
                    names.add(k.arg)
    names.discard("_")
    return names


class _Parser:
    def __init__(self, code: str, macro_names, variables=(), functions=(), customs=None):
        self.code = code
        self.lines = code.split("\n")
        self.macro_names = set(macro_names or ())
        self.variables = set(variables)
        self.functions = set(functions)
        self.customs = customs or {}
        self._bcache: dict = {}

    # -- text helpers (ast columns are UTF-8 byte offsets) ------------------
    def _line(self, lineno: int) -> str:
        return self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""

    def _bline(self, lineno: int) -> bytes:
        c = self._bcache.get(lineno)
        if c is None:
            c = self._bcache[lineno] = self._line(lineno).encode("utf-8")
        return c

    def _after(self, lineno: int, col_bytes: int) -> str:
        return self._bline(lineno)[col_bytes:].decode("utf-8", "replace")

    def _seg(self, node) -> str:
        # ast.get_source_segment() re-splits the WHOLE source on every call
        # (quadratic on a 20k-line transcription); slice our own lines.
        a, b = node.lineno, node.end_lineno
        if a is None or b is None:
            return ""
        if a == b:
            return self._bline(a)[node.col_offset:node.end_col_offset].decode("utf-8", "replace")
        parts = [self._bline(a)[node.col_offset:].decode("utf-8", "replace")]
        for n in range(a + 1, b):
            parts.append(self._line(n))
        parts.append(self._bline(b)[:node.end_col_offset].decode("utf-8", "replace"))
        return "\n".join(parts)

    def _is_note(self, lineno: int) -> bool:
        return bool(NOTE_RE.match(self._line(lineno)))

    # -- socket values --------------------------------------------------------
    def value(self, node):
        """AST expression -> socket value (a Rep where one fits, else its text)."""
        if isinstance(node, ast.Name) and node.id in self.variables:
            return Rep("var", node.id)
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return Rep("bool", "True" if node.value else "False")
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            right = node.comparators[0]
            if type(op) in _CMP_NODES:
                return Rep("compare", _CMP_NODES[type(op)], {"left": self.value(node.left), "right": self.value(right)})
            if (isinstance(op, ast.In) and isinstance(right, ast.Call) and isinstance(right.func, ast.Name)
                    and right.func.id == "getButtonsHeld" and not right.args and not right.keywords):
                return Rep("held", "", {"key": self.value(node.left)})
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args and not node.keywords):
            if node.func.id == "getMousePosition":
                return Rep("mouse", "")
            if node.func.id == "getButtonsHeld":
                return Rep("buttons")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "waitForPress"
                and len(node.args) <= 1 and not any(isinstance(a, ast.Starred) for a in node.args)
                and all(k.arg in ("button", "repress") for k in node.keywords)):
            f = {"button": self.value(node.args[0]) if node.args else "", "repress": Rep("bool", "False")}
            for k in node.keywords:
                f[k.arg] = self.value(k.value)
            if not (isinstance(f["repress"], Rep) and f["repress"].kind == "bool"):
                return self._seg(node)
            return Rep("press", "", f)
        if isinstance(node, ast.Attribute) and node.attr in ("x", "y"):
            base = node.value
            if isinstance(base, ast.Name) and base.id == "getMousePosition":
                return Rep("mouse", node.attr)
            if (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "getMousePosition"
                    and not base.args):
                return Rep("mouse", node.attr)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == "getMousePosition"
                and not node.value.args and isinstance(node.slice, ast.Constant) and node.slice.value in (0, 1)):
            return Rep("mouse", "x" if node.slice.value == 0 else "y")
        return self._seg(node)

    def _rel_lines(self, first: int, last: int, col: int) -> list:
        """Full lines first..last, made relative to indentation `col`. Lines
        that don't start with that indentation (continuations, string
        contents) are kept absolute so they come back unchanged."""
        base = self._line(first)[:col]
        out = []
        for n in range(first, last + 1):
            line = self._line(n)
            if n == first:
                out.append((line[col:], False))
            elif line.startswith(base) and line[:col].strip() == "":
                out.append((line[col:], False))
            else:
                out.append((line, True))
        return out

    def _segment_lines(self, node) -> list:
        """The node's own text (no trailing comment), relative to its column."""
        seg = self._seg(node)
        parts = seg.split("\n")
        out = [(parts[0], False)]
        base = self._line(node.lineno)[:node.col_offset]
        for p in parts[1:]:
            if p.startswith(base) and base.strip() == "":
                out.append((p[len(base):], False))
            else:
                out.append((p, True))
        return out

    def _trailing(self, node) -> "str | None":
        rest = self._after(node.end_lineno, node.end_col_offset)
        if rest.strip() == "":
            return ""
        if rest.lstrip().startswith("#"):
            return rest
        return None     # something else on the line (e.g. `; tap(B)`)

    # -- statements ---------------------------------------------------------
    def body(self, stmts, first_gap_line: int, is_module: bool = False) -> list:
        """Blocks for `stmts`, pulling in comment/blank lines from the gaps
        between them."""
        out: list[Block] = []
        col = stmts[0].col_offset if stmts else 0
        prev_end = first_gap_line - 1
        pending_blank = 0
        i = 0
        while i < len(stmts):
            st = stmts[i]
            pending_blank = self._gap(out, prev_end + 1, st.lineno - 1, col, pending_blank)
            group = [st]
            end = st.end_lineno
            while i + 1 < len(stmts) and stmts[i + 1].lineno <= end:
                i += 1
                group.append(stmts[i])
                end = max(end, stmts[i].end_lineno)
            if len(group) > 1 or self._trailing(st) is None and not isinstance(st, _COMPOUND_NODES):
                blk = self._raw(st.lineno, end, col)
            else:
                blk = self.stmt(st, col, is_first=(is_module and not out and i == 0), is_module=is_module)
            blk.blank_before = pending_blank
            pending_blank = 0
            out.append(blk)
            prev_end = end
            i += 1
        self._pending_blank = pending_blank
        self._prev_end = prev_end
        return out

    def _gap(self, out, first: int, last: int, col: int, pending_blank: int) -> int:
        for n in range(first, last + 1):
            line = self._line(n)
            s = line.strip()
            if s == "":
                pending_blank += 1
                continue
            if NOTE_RE.match(line):
                continue            # free-floating note: collected separately
            if s.startswith("#"):
                indent = len(line) - len(line.lstrip())
                target = self._comment_target(out, indent, col)
                cb = Block("comment", fields={"text": s[1:]})
                cb.blank_before = pending_blank
                pending_blank = 0
                target.append(cb)
        return pending_blank

    def _comment_target(self, out, indent: int, col: int):
        """A comment indented deeper than this level, right after a
        compound block, belongs at the end of that block's last body."""
        target, level = out, col
        while target and indent > level and target[-1].kind in COMPOUND_KINDS and target[-1].bodies:
            parent = target[-1]
            body_col = getattr(parent, "_body_col", level + 4)
            target = parent.bodies[-1]
            level = body_col
        return target

    def _raw(self, first: int, last: int, col: int) -> Block:
        rel = self._rel_lines(first, last, col)
        b = Block("raw", fields={"code": "\n".join(ln for ln, _ in rel)})
        b.src = rel
        return b

    def stmt(self, st, col: int, is_first: bool, is_module: bool = False) -> Block:
        try:
            b = self._stmt(st, col, is_first, is_module)
        except _NotABlock:
            b = None
        return b if b is not None else self._raw(st.lineno, st.end_lineno, col)

    def _simple(self, b: Block, st) -> Block:
        b.src = self._segment_lines(st)
        b.trailing = self._trailing(st) or ""
        return b

    def _call_args(self, call) -> "list | None":
        if call.keywords or any(isinstance(a, ast.Starred) for a in call.args):
            return None
        return [self.value(a) for a in call.args]

    def _stmt(self, st, col: int, is_first: bool, is_module: bool):
        seg = self._seg
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name):
            call = st.value
            name = call.func.id
            if name == "arguments":
                if not is_first:
                    return None
                if call.args or any(k.arg is None for k in call.keywords):
                    return None
                params = [f"{k.arg}={seg(k.value)}" for k in call.keywords]
                return self._simple(Block("arguments", fields={"params": params}), st)
            if name in self.functions:
                args = self._call_args(call)
                return None if args is None else self._simple(Block("func_call", name=name, fields={"args": args}), st)
            if name in self.customs:
                d = self.customs[name]
                if any(isinstance(a, ast.Starred) for a in call.args) or len(call.args) > len(d.args):
                    return None
                fields_ = {a.name: a.default for a in d.args}
                for a, node in zip(d.args, call.args):
                    fields_[a.name] = self.value(node)
                for k in call.keywords:
                    if k.arg not in fields_:
                        return None
                    fields_[k.arg] = self.value(k.value)
                return self._simple(Block("custom", name=name, fields=fields_), st)
            if name in PRIMITIVES_BY_NAME:
                b = self._call_block(name, call)
                return self._simple(b, st) if b else None
            if name in self.macro_names:
                args = self._call_args(call)
                return None if args is None else self._simple(Block("macro_call", name=name, fields={"args": args}), st)
            return None
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            t = st.targets[0]
            if isinstance(t, ast.Name):
                name = t.id
            elif isinstance(t, ast.Tuple) and t.elts and all(isinstance(e, ast.Name) for e in t.elts) \
                    and self._line(t.lineno)[t.col_offset:t.col_offset + 1] != "(":
                name = ", ".join(e.id for e in t.elts)
            else:
                return None
            return self._simple(Block("assign", fields={"name": name, "value": self.value(st.value)}), st)
        if isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) and type(st.op) in _AUG_OPS:
            return self._simple(Block("change", fields={"name": st.target.id, "op": _AUG_OPS[type(st.op)],
                                                        "value": self.value(st.value)}), st)
        if isinstance(st, ast.Return):
            return self._simple(Block("return", fields={"value": self.value(st.value) if st.value else ""}), st)
        if isinstance(st, ast.For) and not st.orelse and isinstance(st.target, ast.Name):
            trail = self._header_trailing(st, st.iter)
            it = st.iter
            if (st.target.id == "_" and isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                    and it.func.id == "range" and len(it.args) == 1 and not it.keywords
                    and not isinstance(it.args[0], ast.Starred)):
                b = Block("repeat", fields={"count": self.value(it.args[0])})
            else:
                b = Block("for", fields={"var": st.target.id, "iter": self.value(it)})
            return self._compound(b, st, [st.body], [trail])
        if isinstance(st, ast.While) and not st.orelse:
            trail = self._header_trailing(st, st.test)
            return self._compound(Block("while", fields={"cond": self.value(st.test)}), st, [st.body], [trail])
        if isinstance(st, ast.If):
            b = Block("if")
            bodies, trails = [], []
            node, k = st, 0
            while True:
                b.fields[f"cond{k}"] = self.value(node.test)
                trails.append(self._header_trailing(node, node.test))
                bodies.append(node.body)
                orelse = node.orelse
                if not orelse:
                    break
                nxt = orelse[0]
                if (len(orelse) == 1 and isinstance(nxt, ast.If)
                        and self._line(nxt.lineno).lstrip().startswith("elif")):
                    node, k = nxt, k + 1
                    continue
                else_line = self._find_else(node, orelse[0].lineno)
                trails.append(else_line[1])
                bodies.append(orelse)
                b.has_else = True
                break
            return self._compound(b, st, bodies, trails)
        if isinstance(st, ast.FunctionDef) and is_module:
            a = st.args
            if st.decorator_list or a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs or st.returns:
                return None
            m = _DEF_RE.match(self._line(st.lineno))
            if not m or st.body[0].lineno <= st.lineno:
                return None
            params = split_top_level(m.group(2))
            b = Block("def", name=st.name, fields={"params": params})
            tail = self._line(st.lineno)
            trail = tail[tail.rindex(":") + 1:] if m.group(3) else ""
            return self._compound(b, st, [st.body], [trail if trail.strip() else ""])
        return None

    def _find_else(self, node, before: int):
        for n in range(before - 1, node.body[-1].end_lineno, -1):
            line = self._line(n)
            if line.lstrip().startswith("else") and len(line) - len(line.lstrip()) == node.col_offset:
                m = re.match(r"^\s*else\s*:\s*(#.*)?$", line)
                if not m:
                    raise _NotABlock()
                rest = line[line.index(":") + 1:]
                return n, rest if rest.strip() else ""
        raise _NotABlock()

    def _header_trailing(self, st, expr) -> str:
        if expr.lineno != st.lineno or expr.end_lineno != st.lineno:
            raise _NotABlock()
        rest = self._after(st.lineno, expr.end_col_offset)
        if not _HEADER_TAIL.match(rest):
            raise _NotABlock()
        tail = rest[rest.index(":") + 1:]
        return tail if tail.strip() else ""

    def _compound(self, b: Block, st, bodies, trails) -> Block:
        b.header_trailing = trails
        for body in bodies:
            if not body:
                raise _NotABlock()
        parsed = []
        prev_header_line = st.lineno
        header_lines = [st.lineno]
        for k, body in enumerate(bodies):
            if k > 0:
                h = self._header_line_before(body[0].lineno, bodies[k - 1][-1].end_lineno, st.col_offset)
                if h is None:
                    raise _NotABlock()
                header_lines.append(h)
                prev_header_line = h
            if body[0].lineno <= prev_header_line:
                raise _NotABlock()          # body on the header's own line
        body_col = bodies[0][0].col_offset
        b._body_col = body_col
        for k, body in enumerate(bodies):
            blocks = self.body(body, header_lines[k] + 1)
            if k + 1 < len(bodies):
                self._gap(blocks, self._prev_end + 1, header_lines[k + 1] - 1, body_col, self._pending_blank)
            if len(blocks) == 1 and blocks[0].kind == "raw" and blocks[0].fields.get("code") == "pass" \
                    and blocks[0].blank_before == 0:
                blocks = []
            parsed.append(blocks)
        b.bodies = parsed
        return b

    def _header_line_before(self, body_first: int, prev_end: int, col: int):
        for n in range(body_first - 1, prev_end, -1):
            line = self._line(n)
            s = line.lstrip()
            if (s.startswith("elif") or s.startswith("else")) and len(line) - len(s) == col:
                return n
        return None

    def _call_block(self, name: str, call: ast.Call) -> "Block | None":
        prim = PRIMITIVES_BY_NAME[name]
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return None
        fields_ = {p.name: ([] if p.vararg else "") for p in prim.params}
        kw = set()
        positional = [p for p in prim.params if not p.kw_only and not p.vararg]
        vararg = next((p for p in prim.params if p.vararg), None)
        if vararg is not None:
            idx = prim.params.index(vararg)
            positional = [p for p in prim.params[:idx] if not p.kw_only]
        args = list(call.args)
        for p, a in zip(positional, args):
            fields_[p.name] = self.value(a)
        extra = args[len(positional):]
        if extra:
            if vararg is None:
                return None
            fields_[vararg.name] = [self.value(a) for a in extra]
        names = {p.name: p for p in prim.params}
        for k in call.keywords:
            p = names.get(k.arg)
            if p is None or p.vararg or not is_empty(fields_.get(k.arg)):
                return None
            fields_[k.arg] = self.value(k.value)
            if not p.kw_only:
                kw.add(k.arg)
        return Block("call", name=name, fields=fields_, kw=kw)


class _NotABlock(Exception):
    pass


_COMPOUND_NODES = (ast.For, ast.While, ast.If, ast.With, ast.Try, ast.FunctionDef, ast.ClassDef,
                   ast.AsyncFor, ast.AsyncWith, ast.AsyncFunctionDef) + ((ast.Match,) if hasattr(ast, "Match") else ())


def code_to_blocks(code: str, macro_names=(), customs: "dict | None" = None) -> Doc:
    """Parse macro code into a Doc. `customs`: {func_name: CustomDef}.
    Raises BlockParseError on a syntax error (the editor stays in Text view
    and says where)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise BlockParseError(f"line {exc.lineno}: {exc.msg}", exc.lineno or 0) from None
    lines = code.split("\n")
    doc = Doc()
    # free-floating notes, wherever they are
    for ln in lines:
        m = NOTE_RE.match(ln)
        if m:
            doc.notes.append(Note(m.group(3), float(m.group(1)), float(m.group(2))))
    if code.strip() == "" and "#" not in code:
        doc.final_newline = False
        doc.tail_blank = code.count("\n")
        return doc
    functions = [st.name for st in tree.body if isinstance(st, ast.FunctionDef)]
    p = _Parser(code, macro_names, _collect_variables(tree), functions, customs)
    main = p.body(tree.body, 1, is_module=True)
    last_line = p._prev_end if tree.body else 0
    pend = p._pending_blank if tree.body else 0
    total = len(lines)
    ends_nl = code.endswith("\n")
    content_last = total - 1 if ends_nl else total
    tail_first = last_line + 1
    last_nonblank = content_last
    while last_nonblank >= tail_first and lines[last_nonblank - 1].strip() == "":
        last_nonblank -= 1
    p._gap(main, tail_first, last_nonblank, 0, pend)
    doc.tail_blank = content_last - last_nonblank
    doc.final_newline = ends_nl
    # top-level defs become their own stacks
    doc.main = [b for b in main if b.kind != "def"]
    doc.functions = [b for b in main if b.kind == "def"]
    for b in main:
        col = getattr(b, "_body_col", None)
        if col:
            doc.indent = "\t" * col if any(ln.startswith("\t") for ln in lines) else " " * col
            break
    return doc


def round_trips(code: str, macro_names=(), customs=None) -> bool:
    try:
        return blocks_to_code(code_to_blocks(code, macro_names, customs), customs=customs) == code
    except BlockParseError:
        return False


# ---------------------------------------------------------------------------
# socket access by reference: (key, index, path) -- see block_render.py
# ---------------------------------------------------------------------------

def get_socket(b: Block, ref: tuple):
    key, idx, path = ref
    if key in ("@name", "@defname"):
        return b.name
    v = b.fields.get(key, "")
    if idx is not None:
        v = v[idx] if isinstance(v, list) and idx < len(v) else ""
    for p in path:
        if not isinstance(v, Rep):
            return ""
        v = v.fields.get(p, "")
    return v


def set_socket(b: Block, ref: tuple, value) -> bool:
    """Write a socket. In a list socket, writing the "+" slot appends and
    writing an empty value removes that element. Returns True if changed."""
    key, idx, path = ref
    if key in ("@name", "@defname"):
        value = value if isinstance(value, str) else expr_text(value)
        if b.name == value:
            return False
        b.name = value
        b.touch()
        return True
    if path:
        parent_ref = (key, idx, path[:-1])
        parent = get_socket(b, parent_ref)
        if not isinstance(parent, Rep):
            return False
        old = parent.fields.get(path[-1], "")
        if old == value:
            return False
        parent.fields[path[-1]] = value if not is_empty(value) else ""
        b.touch()
        return True
    if idx is not None:
        lst = b.fields.get(key)
        if not isinstance(lst, list):
            lst = [] if is_empty(lst) else [lst]
            b.fields[key] = lst
        if idx >= len(lst):
            if is_empty(value):
                return False
            lst.append(value)
        elif is_empty(value):
            del lst[idx]
        else:
            if lst[idx] == value:
                return False
            lst[idx] = value
        b.touch()
        return True
    if b.fields.get(key, "") == value:
        return False
    b.fields[key] = value
    b.kw.discard(key) if is_empty(value) else None
    b.touch()
    return True
