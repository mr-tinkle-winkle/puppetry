"""
The block editor's data model -- Qt-free, so it can be tested on its own.

Blocks are just another VIEW of a macro's `code` string (the one source
of truth). This module converts both ways:

    code_to_blocks(code)  -> Doc   (via Python's own `ast`)
    blocks_to_code(doc)   -> str

Fidelity rules (the part that matters most):
  * Nothing is ever dropped. Any statement no block understands becomes a
    "raw" block holding its exact source lines.
  * Comments survive (as comment blocks, or inside raw blocks), and so do
    blank lines (Block.blank_before) and trailing "# ..." comments.
  * A block that hasn't been edited since it was parsed re-emits its
    ORIGINAL source text (Block.src), so odd spacing like `tap( KEY_A )`
    comes back byte-for-byte even after the block is moved.
  * The editor additionally never regenerates code at all if the blocks
    weren't touched (see editor_page.py), so merely looking at a macro in
    Block view can't change it.

Tree, not a list: a loop's/if's children live in Block.bodies, one list
per "mouth". A Doc holds the main stack (what gets compiled) plus any
loose stacks lying around the canvas (never compiled -- the UI warns).
"""
from __future__ import annotations

import ast
import copy
import itertools
import re
from dataclasses import dataclass, field

from reference import PRIMITIVES_BY_NAME

# Blocks that only exist in Python -- the native fast path (python_on=False)
# rejects control flow and assignment at compile time.
PYTHON_ONLY_KINDS = frozenset({"repeat", "for", "while", "if", "assign", "change"})
COMPOUND_KINDS = frozenset({"repeat", "for", "while", "if"})
CHANGE_OPS = ("+", "-", "*", "/")
_AUG_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}

_ids = itertools.count(1)


@dataclass
class Block:
    kind: str                      # call | macro_call | arguments | repeat | for | while | if |
                                   # assign | change | comment | raw
    name: str = ""                 # primitive / macro name (call, macro_call)
    fields: dict = field(default_factory=dict)   # socket name -> SOURCE text
    kw: set = field(default_factory=set)         # call params that were passed by keyword
    bodies: list = field(default_factory=list)   # list[list[Block]] -- one per mouth
    has_else: bool = False         # if: the last body is the else-body
    blank_before: int = 0
    trailing: str = ""             # e.g. "  # note" after a simple statement
    header_trailing: list = field(default_factory=list)  # per header line of a compound block
    src: "list | None" = None      # [(line, is_absolute)] original text, reused until edited
    expanded: bool = False         # UI only: show every optional socket
    uid: int = field(default_factory=lambda: next(_ids))

    # ---- helpers -----------------------------------------------------------
    def branch_count(self) -> int:
        """if: number of condition branches (if + elifs)."""
        return len(self.bodies) - (1 if self.has_else else 0)

    def touch(self) -> None:
        """Mark edited: from now on it's regenerated from its fields."""
        self.src = None

    def copy_tree(self) -> "Block":
        """Fast deep copy that keeps uids (undo snapshots). Much cheaper
        than copy.deepcopy -- a 20k-block macro snapshots in ~0.1 s. `src`
        is shared: it's never mutated in place, only replaced."""
        c = Block.__new__(Block)
        c.__dict__.update(self.__dict__)
        c.fields = dict(self.fields)
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
class Doc:
    main: list = field(default_factory=list)      # the macro, top to bottom
    loose: list = field(default_factory=list)     # list[Stack], not compiled
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


def needs_python(blocks) -> bool:
    return any(b.kind in PYTHON_ONLY_KINDS for b in walk(blocks))


def variable_names(blocks) -> list[str]:
    """Names this script creates: arguments() params, assignments, loop vars."""
    names: list[str] = []

    def add(n):
        n = (n or "").strip()
        if n.isidentifier() and n != "_" and n not in names:
            names.append(n)

    for b in walk(blocks):
        if b.kind in ("assign", "change"):
            add(b.fields.get("name"))
        elif b.kind == "for":
            add(b.fields.get("var"))
        elif b.kind == "arguments":
            for part in _split_top_level(b.fields.get("params", "")):
                add(part.split("=", 1)[0])
    return names


# ---------------------------------------------------------------------------
# block factories (palette templates)
# ---------------------------------------------------------------------------

def new_call(name: str) -> Block:
    prim = PRIMITIVES_BY_NAME[name]
    fields_ = {}
    for p in prim.params:
        fields_[p.name] = ""
    b = Block("call", name=name, fields=fields_)
    # sensible starting values so a fresh block is valid right away
    starter = {"key": "KEY_A", "keys": "KEY_LEFTCTRL, KEY_C", "acting": "KEY_B", "text": '"hello"',
               "cmd": '"notify-send hi"', "x_pixels": "100", "y_pixels": "0", "amount": "1",
               "time_": "0.1" if name == "wait" else "", "multiplier": "1", "what": '"keyboard"',
               "ignore": "False"}
    for k in fields_:
        if k in starter and (prim_param(name, k).required or k in ("keys", "acting")):
            fields_[k] = starter[k]
    return b


def prim_param(prim_name: str, param_name: str):
    for p in PRIMITIVES_BY_NAME[prim_name].params:
        if p.name == param_name:
            return p
    return None


def new_block(kind: str, **kw) -> Block:
    if kind == "call":
        return new_call(kw["name"])
    if kind == "repeat":
        return Block("repeat", fields={"count": kw.get("count", "10")}, bodies=[[]], header_trailing=[""])
    if kind == "for":
        return Block("for", fields={"var": "i", "iter": "range(10)"}, bodies=[[]], header_trailing=[""])
    if kind == "while":
        return Block("while", fields={"cond": "True"}, bodies=[[]], header_trailing=[""])
    if kind == "if":
        has_else = kw.get("has_else", False)
        return Block("if", fields={"cond0": "True"}, bodies=[[], []] if has_else else [[]],
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
        return Block("arguments", fields={"params": kw.get("params", "count=3")})
    if kind == "macro_call":
        return Block("macro_call", name=kw.get("name", ""), fields={"args": ""})
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# blocks -> code
# ---------------------------------------------------------------------------

def render_call(b: Block) -> str:
    prim = PRIMITIVES_BY_NAME.get(b.name)
    if prim is None:
        return f"{b.name}()"
    has_vararg_value = any(p.vararg and b.fields.get(p.name, "").strip() for p in prim.params)
    pos, kws = [], []
    positional_open = True
    for p in prim.params:
        v = (b.fields.get(p.name) or "").strip()
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
        return f"for _ in range({f.get('count', '').strip() or '1'}):"
    if b.kind == "for":
        return f"for {f.get('var', '').strip() or '_'} in {f.get('iter', '').strip() or 'range(1)'}:"
    if b.kind == "while":
        return f"while {f.get('cond', '').strip() or 'True'}:"
    if b.kind == "if":
        if b.has_else and branch == b.branch_count():
            return "else:"
        word = "if" if branch == 0 else "elif"
        return f"{word} {f.get(f'cond{branch}', '').strip() or 'True'}:"
    raise ValueError(b.kind)


def simple_text(b: Block) -> str:
    f = b.fields
    if b.kind == "call":
        return render_call(b)
    if b.kind == "macro_call":
        return f"{b.name}({(f.get('args') or '').strip()})"
    if b.kind == "arguments":
        return f"arguments({(f.get('params') or '').strip()})"
    if b.kind == "assign":
        return f"{(f.get('name') or 'x').strip()} = {(f.get('value') or '0').strip()}"
    if b.kind == "change":
        op = f.get("op") if f.get("op") in CHANGE_OPS else "+"
        return f"{(f.get('name') or 'x').strip()} {op}= {(f.get('value') or '1').strip()}"
    if b.kind == "comment":
        text = f.get("text", "")
        return "#" + text
    if b.kind == "raw":
        return f.get("code", "")
    raise ValueError(b.kind)


def blocks_to_code(doc_or_blocks, indent: str | None = None) -> str:
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
                emit_lines([(l, False) for l in b.fields.get("code", "").split("\n")], depth)
            else:
                emit_lines([(simple_text(b), False)], depth, b.trailing)

    emit(doc.main, 0)
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


def _split_top_level(s: str) -> list[str]:
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


class _Parser:
    def __init__(self, code: str, macro_names):
        self.code = code
        self.lines = code.split("\n")
        self.macro_names = set(macro_names or ())
        self._bcache: dict = {}

    # -- text helpers (ast columns are UTF-8 byte offsets) ------------------
    def _line(self, lineno: int) -> str:
        return self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else ""

    def _after(self, lineno: int, col_bytes: int) -> str:
        return self._bline(lineno)[col_bytes:].decode("utf-8", "replace")

    def _bline(self, lineno: int) -> bytes:
        c = self._bcache.get(lineno)
        if c is None:
            c = self._bcache[lineno] = self._line(lineno).encode("utf-8")
        return c

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
        between them. first_gap_line: first line that could hold a gap line
        before stmts[0] (the line after the header)."""
        out: list[Block] = []
        col = stmts[0].col_offset if stmts else 0
        prev_end = first_gap_line - 1
        pending_blank = 0
        i = 0
        while i < len(stmts):
            st = stmts[i]
            pending_blank = self._gap(out, prev_end + 1, st.lineno - 1, col, pending_blank)
            # statements sharing lines (`a; b`, or a compound's body on its
            # header line) are one raw block
            group = [st]
            end = st.end_lineno
            while i + 1 < len(stmts) and stmts[i + 1].lineno <= end:
                i += 1
                group.append(stmts[i])
                end = max(end, stmts[i].end_lineno)
            if len(group) > 1 or self._trailing(st) is None and not isinstance(st, _COMPOUND_NODES):
                blk = self._raw(st.lineno, end, col)
            else:
                blk = self.stmt(st, col, is_first=(is_module and not out and i == 0))
            blk.blank_before = pending_blank
            pending_blank = 0
            out.append(blk)
            prev_end = self._block_end(st, end)
            i += 1
        self._pending_blank = pending_blank
        self._prev_end = prev_end
        return out

    def _block_end(self, st, end):
        return end

    def _gap(self, out, first: int, last: int, col: int, pending_blank: int) -> int:
        for n in range(first, last + 1):
            line = self._line(n)
            s = line.strip()
            if s == "":
                pending_blank += 1
                continue
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
        b = Block("raw", fields={"code": "\n".join(l for l, _ in rel)})
        b.src = rel
        return b

    def stmt(self, st, col: int, is_first: bool) -> Block:
        raw = lambda: self._raw(st.lineno, st.end_lineno, col)  # noqa: E731
        try:
            b = self._stmt(st, col, is_first)
        except _NotABlock:
            return raw()
        return b if b is not None else raw()

    def _simple(self, b: Block, st) -> Block:
        b.src = self._segment_lines(st)
        b.trailing = self._trailing(st) or ""
        return b

    def _stmt(self, st, col: int, is_first: bool):
        seg = self._seg
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name):
            call = st.value
            name = call.func.id
            if name == "arguments":
                if not is_first:
                    return None
                text = seg(call)
                inner = text[text.index("(") + 1: text.rindex(")")]
                return self._simple(Block("arguments", fields={"params": inner.strip()}), st)
            if name in PRIMITIVES_BY_NAME:
                b = self._call_block(name, call)
                return self._simple(b, st) if b else None
            if name in self.macro_names:
                text = seg(call)
                inner = text[text.index("(") + 1: text.rindex(")")]
                return self._simple(Block("macro_call", name=name, fields={"args": inner.strip()}), st)
            return None
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            return self._simple(Block("assign", fields={"name": st.targets[0].id, "value": seg(st.value)}), st)
        if isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name) and type(st.op) in _AUG_OPS:
            return self._simple(Block("change", fields={"name": st.target.id, "op": _AUG_OPS[type(st.op)],
                                                        "value": seg(st.value)}), st)
        if isinstance(st, ast.For) and not st.orelse and isinstance(st.target, ast.Name):
            trail = self._header_trailing(st, st.iter)
            it = st.iter
            if (st.target.id == "_" and isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                    and it.func.id == "range" and len(it.args) == 1 and not it.keywords
                    and not isinstance(it.args[0], ast.Starred)):
                b = Block("repeat", fields={"count": seg(it.args[0])})
            else:
                b = Block("for", fields={"var": st.target.id, "iter": seg(it)})
            return self._compound(b, st, [st.body], [trail])
        if isinstance(st, ast.While) and not st.orelse:
            trail = self._header_trailing(st, st.test)
            return self._compound(Block("while", fields={"cond": seg(st.test)}), st, [st.body], [trail])
        if isinstance(st, ast.If):
            b = Block("if")
            bodies, trails = [], []
            node, k = st, 0
            while True:
                b.fields[f"cond{k}"] = seg(node.test)
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
                # a real else: find its line (same indentation as the if)
                else_line = self._find_else(node, orelse[0].lineno)
                trails.append(else_line[1])
                bodies.append(orelse)
                b.has_else = True
                break
            return self._compound(b, st, bodies, trails)
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
        # every mouth's header must be on its own line, above its body
        for body in bodies:
            if not body:
                raise _NotABlock()
        parsed = []
        # header lines: body k starts after the line holding its header
        prev_header_line = st.lineno
        header_lines = [st.lineno]
        for k, body in enumerate(bodies):
            if k > 0:
                # elif/else header sits between the previous body and this one
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
            # comments between this body and the NEXT header belong to this body
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
        fields_ = {p.name: "" for p in prim.params}
        kw = set()
        positional = [p for p in prim.params if not p.kw_only and not p.vararg]
        vararg = next((p for p in prim.params if p.vararg), None)
        # params before the vararg take the first positionals
        if vararg is not None:
            idx = prim.params.index(vararg)
            positional = [p for p in prim.params[:idx] if not p.kw_only]
        args = list(call.args)
        for p, a in zip(positional, args):
            fields_[p.name] = self._seg(a)
        extra = args[len(positional):]
        if extra:
            if vararg is None:
                return None
            fields_[vararg.name] = ", ".join(self._seg(a) for a in extra)
        names = {p.name: p for p in prim.params}
        for k in call.keywords:
            p = names.get(k.arg)
            if p is None or p.vararg or fields_.get(k.arg):
                return None
            fields_[k.arg] = self._seg(k.value)
            if not p.kw_only:
                kw.add(k.arg)
        return Block("call", name=name, fields=fields_, kw=kw)


class _NotABlock(Exception):
    pass


_COMPOUND_NODES = (ast.For, ast.While, ast.If, ast.With, ast.Try, ast.FunctionDef, ast.ClassDef,
                   ast.AsyncFor, ast.AsyncWith, ast.AsyncFunctionDef) + ((ast.Match,) if hasattr(ast, "Match") else ())


def code_to_blocks(code: str, macro_names=()) -> Doc:
    """Parse macro code into a Doc. Raises BlockParseError on a syntax
    error (the editor stays in Text view and says where)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise BlockParseError(f"line {exc.lineno}: {exc.msg}", exc.lineno or 0) from None
    p = _Parser(code, macro_names)
    doc = Doc()
    if code.strip() == "" and "#" not in code:
        doc.final_newline = False
        doc.tail_blank = code.count("\n")
        return doc
    lines = code.split("\n")
    # text after the last statement: comments + blank lines
    doc.main = p.body(tree.body, 1, is_module=True)
    last_line = p._prev_end if tree.body else 0
    if not tree.body:
        p._pending_blank = 0
    # trailing region: everything after the last statement
    total = len(lines)
    ends_nl = code.endswith("\n")
    # lines list has a final "" if code ends with "\n"
    content_last = total - 1 if ends_nl else total
    # comments in the tail (blank lines between them become blank_before)
    tail_first = last_line + 1
    # the final run of blank lines stays as tail_blank
    last_nonblank = content_last
    while last_nonblank >= tail_first and lines[last_nonblank - 1].strip() == "":
        last_nonblank -= 1
    pend = getattr(p, "_pending_blank", 0) if tree.body else 0
    pend = p._gap(doc.main, tail_first, last_nonblank, 0, pend)
    doc.tail_blank = content_last - last_nonblank
    doc.final_newline = ends_nl
    # indent unit: the first indented body statement's indentation
    for b in doc.main:
        col = getattr(b, "_body_col", None)
        if col:
            doc.indent = "\t" * col if any(ln.startswith("\t") for ln in lines) else " " * col
            break
    return doc


def round_trips(code: str, macro_names=()) -> bool:
    try:
        return blocks_to_code(code_to_blocks(code, macro_names)) == code
    except BlockParseError:
        return False
