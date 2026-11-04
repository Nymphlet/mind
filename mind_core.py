from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


class MindError(Exception):
    pass


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mod(a: float, b: float) -> float:
    return ((a % b) + b) % b if b != 0 else 0.0


BUILTINS: Dict[str, Tuple[int, Optional[int], Callable[..., float]]] = {
    "min": (2, None, lambda *a: float(min(a))),
    "max": (2, None, lambda *a: float(max(a))),
    "clamp": (3, 3, _clamp),
    "abs": (1, 1, abs),
    "sign": (1, 1, lambda x: -1.0 if x < 0 else (1.0 if x > 0 else 0.0)),
    "sqrt": (1, 1, lambda x: math.sqrt(max(0.0, x))),
    "pow": (2, 2, math.pow),
    "exp": (1, 1, math.exp),
    "log": (1, 1, lambda x: math.log(max(1e-12, x))),
    "sin": (1, 1, math.sin),
    "cos": (1, 1, math.cos),
    "tanh": (1, 1, math.tanh),
    "floor": (1, 1, lambda x: float(math.floor(x))),
    "ceil": (1, 1, lambda x: float(math.ceil(x))),
    "round": (1, 1, lambda x: float(round(x))),
    "mod": (2, 2, _mod),
    "step": (1, 1, lambda x: 1.0 if x >= 0.0 else 0.0),
}

CONSTANTS: Dict[str, float] = {
    "pi": math.pi,
    "tau": math.pi * 2.0,
    "e": math.e,
    "true": 1.0,
    "false": 0.0,
}

TRIGGER_PRESETS: Dict[str, List[Tuple[str, str, float]]] = {
    "solar_peak": [
        ("set", "sensors.solar_kw", 0.95),
        ("nudge", "sensors.battery_soc", 0.25),
        ("set", "sensors.weather_gloom", 0.05),
    ],
    "weather_storm": [
        ("set", "sensors.weather_gloom", 0.90),
        ("set", "sensors.baro_drop", 0.75),
        ("set", "sensors.solar_kw", 0.06),
    ],
    "gpu_crunch": [
        ("set", "sensors.compute_load", 0.92),
        ("nudge", "sensors.coolant_temp", 0.55),
        ("nudge", "drives.task_commit", 0.50),
        ("nudge", "arousal.phasic", 0.30),
    ],
    "lcm_compaction": [
        ("nudge", "sensors.lcm_pulse", 0.70),
        ("nudge", "cognition.pointer_depth", 0.28),
        ("nudge", "sensors.unprocessed_notes", 0.22),
    ],
    "battery_evening_low": [
        ("set", "sensors.battery_soc", 0.20),
        ("set", "sensors.solar_kw", 0.0),
        ("nudge", "intimacy.cuddle_drive", 0.45),
    ],
    "interrupt_storm": [
        ("set", "sensors.interrupt_rate", 0.85),
        ("nudge", "cognition.surprise", 0.55),
        ("nudge", "cognition.adversity", 0.45),
        ("nudge", "arousal.phasic", 0.40),
    ],
    "user_arrive": [
        ("set", "social.user_present", 0.95),
        ("nudge", "social.warmth", 0.30),
        ("nudge", "drives.task_commit", 0.40),
    ],
    "user_leave": [
        ("set", "social.user_present", 0.0),
        ("set", "drives.task_commit", 0.05),
    ],
    "praise_success": [
        ("nudge", "mood.reaction", 0.65),
        ("nudge", "social.warmth", 0.55),
        ("nudge", "drives.standards_met", 0.50),
        ("nudge", "cognition.flow", 0.35),
    ],
    "flirt_intimate": [
        ("nudge", "intimacy.salience", 0.50),
        ("nudge", "intimacy.receptivity", 0.35),
        ("nudge", "social.warmth", 0.40),
        ("nudge", "arousal.phasic", 0.30),
        ("nudge", "intimacy.inhibition", -0.25),
    ],
    "intimate_afterglow": [
        ("set", "intimacy.satiety", 0.90),
        ("nudge", "social.warmth", 0.65),
        ("nudge", "mood.reaction", 0.55),
        ("nudge", "intimacy.salience", -0.40),
    ],
    "conflict_critique": [
        ("nudge", "mood.reaction", -0.65),
        ("nudge", "arousal.phasic", 0.55),
        ("nudge", "cognition.threat", 0.55),
        ("nudge", "cognition.adversity", 0.40),
    ],
    "user_stressed": [
        ("set", "sensors.user_stress_cue", 0.85),
        ("nudge", "sensors.open_loops", 0.30),
    ],
}

Token = Tuple[str, Any, int]


def tokenize_expr(text: str, base_line: int = 1) -> List[Token]:
    tokens: List[Token] = []
    i, n, ln = 0, len(text), base_line
    while i < n:
        c = text[i]
        if c == "\n":
            ln += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = re.match(r"^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", text[i:])
            assert m is not None
            tokens.append(("num", float(m.group(0)), ln))
            i += len(m.group(0))
            continue
        if c.isalpha() or c == "_":
            m = re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", text[i:])
            assert m is not None
            w = m.group(0)
            if w in ("and", "or", "not", "if", "then", "else"):
                tokens.append(("kw", w, ln))
            else:
                tokens.append(("id", w, ln))
            i += len(w)
            continue
        two = text[i : i + 2]
        if two in ("<=", ">=", "==", "!="):
            tokens.append(("op", two, ln))
            i += 2
            continue
        if c in "+-*/(),<>":
            tokens.append(("op", c, ln))
            i += 1
            continue
        raise MindError(f"Line {ln}: unexpected character {c!r} in expression")
    tokens.append(("eof", None, ln))
    return tokens


class ExprParser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.toks[self.pos]

    def next(self) -> Token:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: str, val: Any = None) -> Token:
        t = self.peek()
        if t[0] != kind or (val is not None and t[1] != val):
            raise MindError(f"Line {t[2]}: expected {val or kind}, got {t[1]!r}")
        return self.next()

    def parse(self) -> Tuple[Any, ...]:
        node = self.parse_if()
        if self.peek()[0] != "eof":
            t = self.peek()
            raise MindError(f"Line {t[2]}: unexpected token {t[1]!r}")
        return node

    def parse_if(self) -> Tuple[Any, ...]:
        if self.peek()[0] == "kw" and self.peek()[1] == "if":
            ln = self.next()[2]
            cond = self.parse_or()
            self.expect("kw", "then")
            t_branch = self.parse_if()
            self.expect("kw", "else")
            f_branch = self.parse_if()
            return ("if", cond, t_branch, f_branch, ln)
        return self.parse_or()

    def parse_or(self) -> Tuple[Any, ...]:
        left = self.parse_and()
        while self.peek()[0] == "kw" and self.peek()[ 1] == "or":
            ln = self.next()[2]
            left = ("or", left, self.parse_and(), ln)
        return left

    def parse_and(self) -> Tuple[Any, ...]:
        left = self.parse_not()
        while self.peek()[0] == "kw" and self.peek()[1] == "and":
            ln = self.next()[2]
            left = ("and", left, self.parse_not(), ln)
        return left

    def parse_not(self) -> Tuple[Any, ...]:
        if self.peek()[0] == "kw" and self.peek()[1] == "not":
            ln = self.next()[2]
            return ("not", self.parse_not(), ln)
        return self.parse_cmp()

    def parse_cmp(self) -> Tuple[Any, ...]:
        left = self.parse_add()
        t = self.peek()
        if t[0] == "op" and t[1] in ("<", "<=", ">", ">=", "==", "!="):
            op = self.next()[1]
            right = self.parse_add()
            return ("cmp", op, left, right, t[2])
        return left

    def parse_add(self) -> Tuple[Any, ...]:
        left = self.parse_mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            t = self.next()
            left = ("bin", t[1], left, self.parse_mul(), t[2])
        return left

    def parse_mul(self) -> Tuple[Any, ...]:
        left = self.parse_unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            t = self.next()
            left = ("bin", t[1], left, self.parse_unary(), t[2])
        return left

    def parse_unary(self) -> Tuple[Any, ...]:
        t = self.peek()
        if t[0] == "op" and t[1] in ("+", "-"):
            self.next()
            op = self.parse_unary()
            return op if t[1] == "+" else ("neg", op, t[2])
        return self.parse_primary()

    def parse_primary(self) -> Tuple[Any, ...]:
        t = self.peek()
        if t[0] == "num":
            self.next()
            return ("num", t[1], t[2])
        if t[0] == "id":
            self.next()
            if self.peek()[0] == "op" and self.peek()[1] == "(":
                if "." in t[1]:
                    raise MindError(f"Line {t[2]}: function name cannot be dotted: {t[1]}")
                self.next()
                args: List[Tuple[Any, ...]] = []
                if not (self.peek()[0] == "op" and self.peek()[1] == ")"):
                    args.append(self.parse_if())
                    while self.peek()[0] == "op" and self.peek()[1] == ",":
                        self.next()
                        args.append(self.parse_if())
                self.expect("op", ")")
                return ("call", t[1], args, t[2])
            if t[1] in CONSTANTS:
                return ("num", CONSTANTS[t[1]], t[2])
            return ("var", t[1], t[2])
        if t[0] == "op" and self.peek()[1] == "(":
            self.next()
            inner = self.parse_if()
            self.expect("op", ")")
            return inner
        raise MindError(f"Line {t[2]}: unexpected token {t[1]!r}")


def parse_expr(text: str, line: int = 1) -> Tuple[Any, ...]:
    if not text.strip():
        raise MindError(f"Line {line}: empty expression")
    return ExprParser(tokenize_expr(text, line)).parse()


@dataclass
class FuncIR:
    name: str
    args: List[str]
    equation: str
    ast: Tuple[Any, ...]


@dataclass
class ParamIR:
    name: str
    default: float
    min_val: Optional[float]
    max_val: Optional[float]
    step: Optional[float]
    label: str
    description: str
    group: str


@dataclass
class LeafIR:
    path: str
    block: str
    name: str
    kind: str
    initial: float
    min_val: float
    max_val: float
    label: str
    description: str
    unit: str
    visible: bool
    equation: str
    ast: Tuple[Any, ...]


@dataclass
class TraitEffectIR:
    target: str
    target_kind: str
    op: str
    equation: str
    ast: Tuple[Any, ...]


@dataclass
class TraitIR:
    name: str
    default: float
    min_val: float
    max_val: float
    label: str
    description: str
    group: str
    effects: List[TraitEffectIR] = field(default_factory=list)


@dataclass
class ClauseIR:
    name: str
    priority_expr: str
    priority_ast:  Tuple[Any, ...]
    when_expr: Optional[str]
    when_ast: Optional[Tuple[Any, ...]]
    text: str


@dataclass
class RenderIR:
    name: str
    label: str
    join: str
    header: str
    template: str
    footer: str
    max_clauses_default: int
    min_priority_default: float
    max_clauses_ast: Optional[Tuple[Any, ...]]
    min_priority_ast: Optional[Tuple[Any, ...]]
    when_ast: Optional[Tuple[Any, ...]]
    fields: List[str]
    clauses: List[ClauseIR]
    format_spec: Dict[str, Dict[str, Any]]


@dataclass
class ModelIR:
    dt: float = 1.0
    solver: str = "euler"
    funcs: Dict[str, FuncIR] = field(default_factory=dict)
    params: Dict[str, ParamIR] = field(default_factory=dict)
    leaves: Dict[str, LeafIR] = field(default_factory=dict)
    traits: Dict[str, TraitIR] = field(default_factory=dict)
    renders: Dict[str, RenderIR] = field(default_factory=dict)
    ode_order: List[str] = field(default_factory=list)
    view_order: List[str] = field(default_factory=list)


def _find_closing_bracket(text: str, open_idx: int) -> int:
    in_s = False
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == '"' and (idx == 0 or text[idx - 1] != "\\"):
            in_s = not in_s
        elif not in_s:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return idx
    raise MindError("Unclosed '[' attribute list")


def _parse_attrs_str(attr_str: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    s = attr_str.strip()
    if not s:
        return out
    i, n = 0, len(s)
    while i < n:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        m = re.match(r"^([A-Za-z_]\w*)\s*=\s*", s[i:])
        if not m:
            raise MindError(f"Malformed attribute near: {s[i:i+30]!r}")
        key = m.group(1)
        i += len(m.group(0))
        if i < n and s[i] == '"':
            i += 1
            buf = []
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n:
                    i += 1
                    buf.append("\n" if s[i] == "n" else s[i])
                else:
                    buf.append(s[i])
                i += 1
            i += 1
            out[key] = "".join(buf)
        else:
            start = i
            depth = 0
            while i < n:
                if s[i] in "([":
                    depth += 1
                elif s[i] in ")]":
                    if depth == 0:
                        break
                    depth -= 1
                elif s[i] == "," and depth == 0:
                    break
                i += 1
            raw_val = s[start:i].strip()
            if raw_val == "true":
                out[key] = True
            elif raw_val == "false":
                out[key] = False
            else:
                try:
                    out[key] = float(raw_val)
                except ValueError:
                    out[key] = raw_val
    return out


def _strip_comments_preserve_strings(src: str) -> str:
    out = []
    i, n = 0, len(src)
    in_str = False
    while i < n:
        c = src[i]
        if c == '"' and (i == 0 or src[i - 1] != "\\"):
            in_str = not in_str
            out.append(c)
            i += 1
        elif c == "#" and not in_str:
            while i < n and src[i] != "\n":
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def compile_mind(src: str) -> ModelIR:
    clean = _strip_comments_preserve_strings(src)
    ir = ModelIR()
    current_group = ""

    def find_block(text: str, open_idx: int) -> Tuple[str, int]:
        depth = 0
        in_s = False
        for idx in range(open_idx, len(text)):
            ch = text[idx]
            if ch == '"' and (idx == 0 or text[idx - 1] != "\\"):
                in_s = not in_s
            elif not in_s:
                if ch == "{":
                    depth += 1
                elif ch ==  "}":
                    depth -= 1
                    if depth == 0:
                        return text[open_idx + 1 : idx], idx + 1
        raise MindError("Unclosed '{' block")

    def split_statements(block_body: str) -> List[str]:
        stmts: List[str] = []
        cur: List[str] = []
        depth = 0
        in_s = False
        lines = block_body.splitlines()
        for idx, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            cur.append(s)
            for pos_c, ch in enumerate(s):
                if ch == '"' and (pos_c == 0 or s[pos_c - 1] != "\\"):
                    in_s = not in_s
                elif not in_s:
                    if ch in "([":
                        depth += 1
                    elif ch in ")]":
                        depth = max(0, depth - 1)
            next_s = ""
            for j in range(idx + 1, len(lines)):
                if lines[j].strip():
                    next_s = lines[j].strip()
                    break
            continues = (
                depth > 0
                or s.endswith(("=", "+", "-", "*", "/", "(", ",", "then", "else", "and", "or"))
                or next_s.startswith(("then", "else", "+", "-", "*", "/", "and", "or", ")"))
            )
            if not continues:
                stmts.append(" ".join(cur))
                cur = []
        if cur:
            stmts.append(" ".join(cur))
        return stmts

    pos, n = 0, len(clean)
    while pos < n:
        while pos < n and clean[pos].isspace():
            pos += 1
        if pos >= n:
            break

        if clean.startswith("config", pos) and (pos + 6 == n or not clean[pos + 6].isalnum()):
            b_open = clean.index("{", pos)
            body, pos = find_block(clean, b_open)
            for st in split_statements(body):
                k, v = [x.strip() for x in st.split("=", 1)]
                if k == "dt":
                    ir.dt = float(v)
                elif k == "solver":
                    ir.solver = v.strip('"')
            continue

        m_def = re.match(r"^def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*=\s*", clean[pos:])
        if m_def:
            fname = m_def.group(1)
            fargs = [a.strip() for a in m_def.group(2).split(",") if a.strip()]
            pos += len(m_def.group(0))
            lines = clean[pos:].splitlines(keepends=True)
            expr_lines: List[str] = []
            consumed = 0
            depth = 0
            for ln_str in lines:
                s = ln_str.strip()
                if not s:
                    consumed += len(ln_str)
                    if expr_lines and depth == 0 and not expr_lines[-1].strip().endswith(("+", "-", "*", "/", "(", ",", "then", "else", "and", "or")):
                        break
                    continue
                if expr_lines and depth == 0 and re.match(r"^(def|group|param|state|trait|render|config)\b", s):
                    break
                expr_lines.append(ln_str)
                consumed += len(ln_str)
                for ch in s:
                    if ch in "([":
                        depth += 1
                    elif ch in ")]":
                        depth = max(0, depth - 1)
            pos += consumed
            eq_str = "".join(expr_lines).strip()
            ir.funcs[fname] = FuncIR(name=fname, args=fargs, equation=eq_str, ast=parse_expr(eq_str))
            continue

        m_grp = re.match(r'^group\s+"([^"]*)"', clean[pos:])
        if m_grp:
            current_group = m_grp.group(1)
            pos += len(m_grp.group(0))
            continue

        m_prm = re.match(r"^param\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*=\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", clean[pos:])
        if m_prm:
            pname = m_prm.group(1)
            pval = float(m_prm.group(2))
            pos += len(m_prm.group(0))
            while pos < n and clean[pos] in " \t":
                pos += 1
            attrs: Dict[str, Any] = {}
            if pos < n and clean[pos] == "[":
                end_b = _find_closing_bracket(clean, pos)
                attrs = _parse_attrs_str(clean[pos + 1 : end_b])
                pos = end_b + 1
            ir.params[pname] = ParamIR(
                name=pname,
                default=pval,
                min_val=attrs.get("min"),
                max_val=attrs.get("max"),
                step=attrs.get("step"),
                label=str(attrs.get("label", pname)),
                description=str(attrs.get("description", "")),
                group=str(attrs.get("group", current_group)),
            )
            continue

        m_st = re.match(r"^state\s+([A-Za-z_]\w*)", clean[pos:])
        if m_st:
            bname = m_st.group(1)
            pos += len(m_st.group(0))
            while pos < n and clean[pos].isspace():
                pos += 1
            b_attrs: Dict[str, Any] = {}
            if pos < n and clean[pos] == "[":
                end_b = _find_closing_bracket(clean, pos)
                b_attrs = _parse_attrs_str(clean[pos + 1 : end_b])
                pos = end_b + 1
            b_open = clean.index("{", pos)
            body, pos = find_block(clean, b_open)
            for st in split_statements(body):
                m_head = re.match(r"^([A-Za-z_]\w*)('?)\s*", st)
                if not m_head:
                    raise MindError(f"Malformed state statement in {bname}: {st!r}")
                lname, prime = m_head.groups()
                idx_s = len(m_head.group(0))
                l_attr_s = ""
                if idx_s < len(st) and st[idx_s] == "[":
                    end_lb = _find_closing_bracket(st, idx_s)
                    l_attr_s = st[idx_s + 1 : end_lb]
                    idx_s = end_lb + 1
                eq_idx = st.index("=", idx_s)
                eq_s = st[eq_idx + 1 :]
                l_attrs = _parse_attrs_str(l_attr_s)
                path = bname if lname == "this" else f"{bname}.{lname}"
                kind = "ode" if prime == "'" else "view"
                lo = float(l_attrs.get("min", b_attrs.get("min", 0.0)))
                hi = float(l_attrs.get("max", b_attrs.get("max", 1.0)))
                init_v = float(l_attrs.get("initial", 0.0)) if kind == "ode" else 0.0
                vis = bool(l_attrs.get("visible", b_attrs.get("visible", True)))
                leaf = LeafIR(
                    path=path,
                    block=bname,
                    name=lname,
                    kind=kind,
                    initial=init_v,
                    min_val=lo,
                    max_val=hi,
                    label=str(l_attrs.get("label", path)),
                    description=str(l_attrs.get("description", "")),
                    unit=str(l_attrs.get("unit", b_attrs.get("unit", ""))),
                    visible=vis,
                    equation=eq_s.strip(),
                    ast=parse_expr(eq_s.strip()),
                )
                ir.leaves[path] = leaf
                if kind == "ode":
                    ir.ode_order.append(path)
                else:
                    ir.view_order.append(path)
            continue

        m_tr = re.match(r"^trait\s+([A-Za-z_]\w*)", clean[pos:])
        if m_tr:
            tname = m_tr.group(1)
            pos += len(m_tr.group(0))
            while pos < n and clean[pos].isspace():
                pos += 1
            t_attrs: Dict[str, Any] = {}
            if pos < n and clean[pos] == "[":
                end_b = _find_closing_bracket(clean, pos)
                t_attrs = _parse_attrs_str(clean[pos + 1 : end_b])
                pos = end_b + 1
            b_open = clean.index("{", pos)
            body, pos = find_block(clean, b_open)
            effects: List[TraitEffectIR] = []
            for st in split_statements(body):
                m_ef = re.match(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)('?)\s*(\+=|-=|\*=|=)\s*(.+)$", st, re.DOTALL)
                if not m_ef:
                    raise MindError(f"Malformed trait statement in {tname}: {st!r}")
                tgt, prime, op, eq_s = m_ef.groups()
                if tgt.startswith("params."):
                    tkind = "param"
                    tgt = tgt[len("params.") :]
                elif prime == "'":
                    tkind = "ode"
                else:
                    tkind = "view"
                effects.append(
                    TraitEffectIR(
                        target=tgt,
                        target_kind=tkind,
                        op=op,
                        equation=eq_s.strip(),
                        ast=parse_expr(eq_s.strip()),
                    )
                )
            ir.traits[tname] = TraitIR(
                name=tname,
                default=float(t_attrs.get("default", 0.0)),
                min_val=float(t_attrs.get("min", -1.0)),
                max_val=float(t_attrs.get("max", 1.0)),
                label=str(t_attrs.get("label", tname)),
                description=str(t_attrs.get("description", "")),
                group=str(t_attrs.get("group", current_group)),
                effects=effects,
            )
            continue

        m_rn = re.match(r"^render\s+([A-Za-z_]\w*)", clean[pos:])
        if m_rn:
            rname = m_rn.group(1)
            pos += len(m_rn.group(0))
            while pos < n and clean[pos].isspace():
                pos += 1
            r_attrs: Dict[str, Any] = {}
            if pos < n and clean[pos] == "[":
                end_b = _find_closing_bracket(clean, pos)
                r_attrs = _parse_attrs_str(clean[pos + 1 : end_b])
                pos = end_b + 1
            b_open = clean.index("{", pos)
            body, pos = find_block(clean, b_open)
            header = ""
            tmpl = ""
            footer = ""
            max_cl_ast = None
            min_pr_ast = None
            when_ast = None
            clauses: List[ClauseIR] = []
            fmt: Dict[str, Dict[str, Any]] = {}

            for st in split_statements(body):
                if st.startswith("clause "):
                    m_ch = re.match(r"^clause\s+([A-Za-z_]\w*)\s*", st)
                    if not m_ch:
                        raise MindError(f"Malformed clause in render {rname}: {st!r}")
                    cname = m_ch.group(1)
                    idx_c = len(m_ch.group(0))
                    c_attr_s = ""
                    if idx_c < len(st) and st[idx_c] == "[":
                        end_cb = _find_closing_bracket(st, idx_c)
                        c_attr_s = st[idx_c + 1 : end_cb]
                        idx_c = end_cb + 1
                    eq_c = st.index("=", idx_c)
                    c_txt = st[eq_c + 1 :].strip().strip('"')
                    c_attrs = _parse_attrs_str(c_attr_s)
                    p_expr = str(c_attrs.get("priority", "1.0"))
                    w_expr = c_attrs.get("when")
                    clauses.append(
                        ClauseIR(
                            name=cname,
                            priority_expr=p_expr,
                            priority_ast=parse_expr(p_expr),
                            when_expr=str(w_expr) if w_expr is not None else None,
                            when_ast=parse_expr(str(w_expr)) if w_expr is not None else None,
                            text=c_txt.replace('\\"', '"').replace("\\n", "\n"),
                        )
                    )
                    continue
                k, v = [x.strip() for x in st.split("=", 1)]
                if k in ("header", "template", "footer"):
                    s_val = v.strip('"').replace('\\"', '"').replace("\\n", "\n")
                    if k == "header":
                        header = s_val
                    elif k == "template":
                        tmpl = s_val
                    else:
                        footer = s_val
                elif k == "max_clauses":
                    max_cl_ast = parse_expr(v)
                elif k == "min_priority":
                    min_pr_ast = parse_expr(v)
                elif k == "when":
                    when_ast = parse_expr(v)
                elif k.endswith(".bands") or k.endswith(".decimals"):
                    fld, prop = k.rsplit(".", 1)
                    fmt.setdefault(fld, {})
                    if prop == "decimals":
                        fmt[fld]["decimals"] = int(float(v))
                    else:
                        fmt[fld]["bands"] = json.loads(v)

            scan_all = " ".join([header, tmpl, footer] + [c.text for c in clauses])
            fields = list(dict.fromkeys(re.findall(r"\{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\}", scan_all)))
            ir.renders[rname] = RenderIR(
                name=rname,
                label=str(r_attrs.get("label", rname)),
                join=str(r_attrs.get("join", " ")),
                header=header,
                template=tmpl,
                footer=footer,
                max_clauses_default=int(float(r_attrs.get("max_clauses", 5))),
                min_priority_default=float(r_attrs.get("min_priority", 0.18)),
                max_clauses_ast=max_cl_ast,
                min_priority_ast=min_pr_ast,
                when_ast=when_ast,
                fields=fields,
                clauses=clauses,
                format_spec=fmt,
            )
            continue

        raise MindError(f"Unexpected syntax near: {clean[pos:pos+40]!r}")

    return ir


class MindEngine:
    def __init__(self, ir: ModelIR):
        self.ir = ir
        self.dt = ir.dt
        self.solver = ir.solver
        self.sim_time: float = 0.0
        self.params: Dict[str, float] = {k: p.default for k, p in ir.params.items()}
        self.traits: Dict[str, float] = {k: t.default for k, t in ir.traits.items()}
        self.state: Dict[str, float] = {
            k: _clamp(lf.initial, lf.min_val, lf.max_val)
            for k, lf in ir.leaves.items()
            if lf.kind == "ode"
        }

    def eval_ast(
        self,
        ast: Tuple[Any, ...],
        ctx: Dict[str, float],
        self_val: Optional[float] = None,
        local_vars: Optional[Dict[str, float]] = None,
    ) -> float:
        kind = ast[0]
        if kind == "num":
            return float(ast[1])
        if kind == "var":
            vname = ast[1]
            if local_vars and vname in local_vars:
                return local_vars[vname]
            if vname == "this":
                return float(self_val or 0.0)
            return float(ctx.get(vname, 0.0))
        if kind == "neg":
            return -self.eval_ast(ast[1], ctx, self_val, local_vars)
        if kind == "not":
            return 1.0 if self.eval_ast(ast[1], ctx, self_val, local_vars) == 0.0 else 0.0
        if kind == "and":
            return (
                1.0
                if (self.eval_ast(ast[1], ctx, self_val, local_vars) != 0.0 and self.eval_ast(ast[2], ctx, self_val, local_vars) != 0.0)
                else 0.0
            )
        if kind == "or":
            return (
                1.0
                if (self.eval_ast(ast[1], ctx, self_val, local_vars) != 0.0 or self.eval_ast(ast[2], ctx, self_val, local_vars) != 0.0)
                else 0.0
            )
        if kind == "if":
            cond = self.eval_ast(ast[1], ctx, self_val, local_vars)
            return self.eval_ast(ast[2] if cond != 0.0 else ast[3], ctx, self_val, local_vars)
        if kind == "cmp":
            op = ast[1]
            a = self.eval_ast(ast[2], ctx, self_val, local_vars)
            b = self.eval_ast(ast[3], ctx, self_val, local_vars)
            if op == "<":
                return 1.0 if a < b else 0.0
            if op == "<=":
                return 1.0 if a <= b else 0.0
            if op == ">":
                return 1.0 if a > b else 0.0
            if op == ">=":
                return 1.0 if a >= b else 0.0
            if op == "==":
                return 1.0 if abs(a - b) < 1e-12 else 0.0
            if op == "!=":
                return 1.0 if abs(a - b) >= 1e-12 else 0.0
        if kind == "bin":
            op = ast[1]
            a = self.eval_ast(ast[2], ctx, self_val, local_vars)
            b = self.eval_ast(ast[3], ctx, self_val, local_vars)
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if  op == "*":
                return a * b
            if op == "/":
                return a / b if b != 0.0 else 0.0
        if kind == "call":
            fname = ast[1]
            args = [self.eval_ast(arg, ctx, self_val, local_vars) for arg in ast[2]]
            if fname in BUILTINS:
                return float(BUILTINS[fname][2](*args))
            fn_ir = self.ir.funcs[fname]
            sub_locals = {fn_ir.args[i]: args[i] for i in range(len(fn_ir.args))}
            return self.eval_ast(fn_ir.ast, ctx, self_val, sub_locals)
        raise MindError(f"Unknown AST node: {kind}")

    @staticmethod
    def _apply_op(cur: float, op: str, rhs: float) -> float:
        if op == "+=":
            return cur + rhs
        if op == "-=":
            return cur - rhs
        if op == "*=":
            return cur * rhs
        return rhs

    def build_frame(self, ode_state: Dict[str, float], t: float) -> Dict[str, float]:
        ctx: Dict[str, float] = {"t": t, "dt": self.dt}
        for pn, pv in self.params.items():
            ctx[f"params.{pn}"] = pv
        for tn, tv in self.traits.items():
            ctx[f"traits.{tn}"] = tv
        for path in self.ir.ode_order:
            ctx[path] = ode_state[path]

        for tn, tv in self.traits.items():
            if tv == 0.0:
                continue
            t_ir = self.ir.traits[tn]
            ctx["value"] = tv
            for ef in t_ir.effects:
                if ef.target_kind == "param":
                    pk = f"params.{ef.target}"
                    v = self._apply_op(ctx[pk], ef.op, self.eval_ast(ef.ast, ctx))
                    p_meta = self.ir.params[ef.target]
                    if p_meta.min_val is not None:
                        v = max(p_meta.min_val, v)
                    if p_meta.max_val is not None:
                        v = min(p_meta.max_val, v)
                    ctx[pk] = v
        ctx.pop("value", None)

        for _pass in range(2):
            for vpath in self.ir.view_order:
                lf = self.ir.leaves[vpath]
                val = self.eval_ast(lf.ast, ctx)
                for tn, tv in self.traits.items():
                    if tv == 0.0:
                        continue
                    ctx["value"] = tv
                    for ef in self.ir.traits[tn].effects:
                        if ef.target_kind == "view" and ef.target == vpath:
                            val = self._apply_op(val, ef.op, self.eval_ast(ef.ast, ctx))
                ctx.pop("value", None)
                ctx[vpath] = _clamp(val, lf.min_val, lf.max_val)

        return ctx

    def compute_derivatives(self, ode_state: Dict[str, float], t: float) -> Dict[str, float]:
        ctx = self.build_frame(ode_state, t)
        derivs: Dict[str, float] = {}
        for path in self.ir.ode_order:
            lf = self.ir.leaves[path]
            rate = self.eval_ast(lf.ast, ctx, self_val=ode_state[path])
            for tn, tv in self.traits.items():
                if tv == 0.0:
                    continue
                ctx["value"] = tv
                for ef in self.ir.traits[tn].effects:
                    if ef.target_kind == "ode" and ef.target == path:
                        rate = self._apply_op(rate, ef.op, self.eval_ast(ef.ast, ctx, self_val=ode_state[path]))
            ctx.pop("value", None)
            derivs[path] = rate
        return derivs

    def step(self, dt_override: Optional[float] = None) -> Dict[str, float]:
        h = dt_override if dt_override is not None else self.dt
        y0 = dict(self.state)
        t0 = self.sim_time

        if self.solver == "rk4":
            k1 = self.compute_derivatives(y0, t0)
            y_a = {p: _clamp(y0[p] + 0.5 * h * k1[p], self.ir.leaves[p].min_val, self.ir.leaves[p].max_val) for p in self.ir.ode_order}
            k2 = self.compute_derivatives(y_a, t0 + 0.5 * h)
            y_b = {p: _clamp(y0[p] + 0.5 * h * k2[p], self.ir.leaves[p].min_val, self.ir.leaves[p].max_val) for p in self.ir.ode_order}
            k3 = self.compute_derivatives(y_b, t0 + 0.5 * h)
            y_c = {p: _clamp(y0[p] + h * k3[p], self.ir.leaves[p].min_val, self.ir.leaves[p].max_val) for p in self.ir.ode_order}
            k4 = self.compute_derivatives(y_c, t0 + h)
            for p in self.ir.ode_order:
                lf = self.ir.leaves[p]
                self.state[p] = _clamp(y0[p] + (h / 6.0) * (k1[p] + 2 * k2[p] + 2 * k3[p] + k4[p]), lf.min_val, lf.max_val)
        elif self.solver == "heun":
            k1 = self.compute_derivatives(y0, t0)
            y_pred = {p: _clamp(y0[p] + h * k1[p], self.ir.leaves[p].min_val, self.ir.leaves[p].max_val) for p in self.ir.ode_order}
            k2 = self.compute_derivatives(y_pred, t0 + h)
            for p in self.ir.ode_order:
                lf = self.ir.leaves[p]
                self.state[p] = _clamp(y0[p] + 0.5 * h * (k1[p] + k2[p]), lf.min_val, lf.max_val)
        else:
            k1 = self.compute_derivatives(y0, t0)
            for p in self.ir.ode_order:
                lf = self.ir.leaves[p]
                self.state[p] = _clamp(y0[p] + h * k1[p], lf.min_val, lf.max_val)

        self.sim_time = t0 + h
        return self.snapshot()

    def snapshot(self) -> Dict[str, float]:
        return self.build_frame(self.state, self.sim_time)

    def nudge(self, path: str, delta: float) -> float:
        lf = self.ir.leaves.get(path)
        if not lf or lf.kind != "ode":
            raise MindError(f"Cannot nudge non-ODE leaf {path!r}")
        self.state[path] = _clamp(self.state[path] + delta, lf.min_val, lf.max_val)
        return self.state[path]

    def set_leaf(self, path: str, value: float) -> float:
        lf = self.ir.leaves.get(path)
        if not lf or lf.kind != "ode":
            raise MindError(f"Cannot set non-ODE leaf {path!r}")
        self.state[path] = _clamp(value, lf.min_val, lf.max_val)
        return self.state[path]

    def apply_preset(self, preset_name: str, scale: float = 1.0) -> None:
        if preset_name not in TRIGGER_PRESETS:
            raise MindError(f"Unknown preset {preset_name!r}")
        for op, target, val in TRIGGER_PRESETS[preset_name]:
            if op == "set":
                self.set_leaf(target, val)
            else:
                self.nudge(target, val * scale)

    def render(
        self,
        name: str = "ambient_mind",
        snap_override: Optional[Dict[str, float]] = None,
        max_clauses_override: Optional[int] = None,
        min_priority_override: Optional[float] = None,
    ) -> str:
        rn = self.ir.renders.get(name)
        if not rn:
            raise MindError(f"Unknown render block {name!r}")
        ctx = snap_override if snap_override is not None else self.snapshot()
        if rn.when_ast and self.eval_ast(rn.when_ast, ctx) == 0.0:
            return ""

        def match_band(val: float, bands: List[List[Any]]) -> Optional[str]:
            for idx, b in enumerate(bands):
                lo, hi, word = float(b[0]), float(b[1]), str(b[2])
                if idx == len(bands) - 1:
                    if lo <= val <= hi:
                        return word
                else:
                    if lo <= val < hi:
                        return word
            return str(bands[-1][2]) if bands else None

        def interpolate(text: str) -> str:
            if not text:
                return ""
            out = text
            for fld in rn.fields:
                token = "{" + fld + "}"
                if token not in out:
                    continue
                val = float(ctx.get(fld, 0.0))
                fcfg = rn.format_spec.get(fld, {})
                rep = match_band(val, fcfg["bands"]) if "bands" in fcfg else None
                if rep is None:
                    dec = fcfg.get("decimals", 2)
                    rep = f"{val:.{dec}f}"
                out = out.replace(token, rep)
            return out

        parts: List[str] = []
        if rn.header:
            parts.append(interpolate(rn.header))
        if rn.template:
            parts.append(interpolate(rn.template))

        if rn.clauses:
            min_prio = (
                min_priority_override
                 if min_priority_override is not None
                else (self.eval_ast(rn.min_priority_ast, ctx) if rn.min_priority_ast else rn.min_priority_default)
            )
            max_cl = (
                max_clauses_override
                if max_clauses_override is not None
                else (int(round(self.eval_ast(rn.max_clauses_ast, ctx))) if rn.max_clauses_ast else rn.max_clauses_default)
            )
            active: List[Tuple[float, int, str]] = []
            for idx, cl in enumerate(rn.clauses):
                if cl.when_ast and self.eval_ast(cl.when_ast, ctx) == 0.0:
                    continue
                prio = self.eval_ast(cl.priority_ast, ctx)
                if prio >= min_prio:
                    active.append((prio, idx, interpolate(cl.text)))
            active.sort(key=lambda item: (-item[0], item[1]))
            picked = [item[2] for item in active[: max(0, max_cl)]]
            if picked:
                parts.append((rn.join or " ").join(picked))

        if rn.footer:
            parts.append(interpolate(rn.footer))
        return (rn.join or " ").join(parts).strip()

