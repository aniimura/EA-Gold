# -*- coding: utf-8 -*-
"""Single-source expression compiler.

An expression written once as a Python string is compiled into BOTH
  * a vectorised NumPy expression (for the Python backtester), and
  * an equivalent MQL5 expression (for the generated EA),
so the two can never drift apart.

Grammar (a strict subset of Python):
    name                 value of `name` on the signal bar
    name[k]              value of `name` k bars earlier
    open/high/low/close/volume   raw price series (same indexing rule)
    + - * /  unary -     arithmetic
    > < >= <= == !=      comparison (chains allowed: a < b < c)
    and / or / not       boolean
    abs() min() max() sqrt()
    numeric literals

Bar-index convention (identical on both sides):
    shift 0 == the most recently COMPLETED bar.
    In MQL5 that is CopyRates index 1; index 0 (the forming bar) is never read.
    An order therefore executes at the OPEN of the bar that follows the signal.
"""
from __future__ import annotations

import ast
from typing import Callable, Dict, List, Set, Tuple

PRICE_FIELDS = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "tick_volume",
}

# Calendar values of the bar itself.  Server-time ones are exact on both sides
# by construction; the UTC ones go through the broker offset + DST rule in
# core/timeutil.py, which is implemented identically in Python and MQL5.
TIME_FIELDS = {
    "hour": "SrvHour",                       # server hour, 0-23
    "minute_of_day": "SrvMinuteOfDay",       # server minutes since midnight
    "dow": "SrvDayOfWeek",                   # Monday = 0, like pandas
    "utc_hour": "UtcHour",
    "utc_minute_of_day": "UtcMinuteOfDay",
}

_BIN = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_CMP = {ast.Gt: ">", ast.Lt: "<", ast.GtE: ">=", ast.LtE: "<=",
        ast.Eq: "==", ast.NotEq: "!="}
_FUNCS = {
    "abs":  ("np.abs", "MathAbs", 1),
    "sqrt": ("np.sqrt", "MathSqrt", 1),
    "min":  ("np.minimum", "MathMin", 2),
    "max":  ("np.maximum", "MathMax", 2),
}


class ExprError(ValueError):
    pass


class CompiledExpr:
    """Result of compiling one expression string."""

    def __init__(self, source, py_code, mq5_code, refs):
        self.source = source
        self.py_code = py_code
        self.mq5_code = mq5_code
        self.refs = refs                       # [(name, shift), ...]

    @property
    def names(self):
        return {n for n, _ in self.refs}

    @property
    def max_shift(self):
        return max((s for _, s in self.refs), default=0)

    def __repr__(self):
        return "<CompiledExpr %r>" % (self.source,)


class _Compiler(ast.NodeVisitor):
    """Walks the AST once, emitting the NumPy and MQL5 forms together."""

    def __init__(self, mq5_ref):
        self.mq5_ref = mq5_ref
        self.refs = []

    # -- helpers ----------------------------------------------------------
    def _ref(self, name, shift):
        if shift < 0:
            raise ExprError(
                "negative shift not allowed (look-ahead): %s[%d]" % (name, shift))
        key = (name, shift)
        if key not in self.refs:
            self.refs.append(key)
        return ("E[(%r, %d)]" % (name, shift), self.mq5_ref(name, shift))

    def visit(self, node):
        meth = getattr(self, "v_" + type(node).__name__, None)
        if meth is None:
            raise ExprError("unsupported syntax: %s" % type(node).__name__)
        return meth(node)

    # -- literals / names -------------------------------------------------
    def v_Constant(self, node):
        if isinstance(node.value, bool):
            return ("True" if node.value else "False",
                    "true" if node.value else "false")
        if isinstance(node.value, (int, float)):
            return repr(float(node.value)), _mq_num(node.value)
        raise ExprError("unsupported literal: %r" % (node.value,))

    def v_Name(self, node):
        return self._ref(node.id, 0)

    def v_Subscript(self, node):
        if not isinstance(node.value, ast.Name):
            raise ExprError("only `name[k]` indexing is supported")
        idx = node.slice
        if isinstance(idx, ast.Index):            # Python < 3.9
            idx = idx.value
        if not (isinstance(idx, ast.Constant) and isinstance(idx.value, int)):
            raise ExprError("bar shift must be an integer literal")
        return self._ref(node.value.id, int(idx.value))

    # -- operators --------------------------------------------------------
    def v_UnaryOp(self, node):
        p, m = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return ("(-%s)" % p, "(-%s)" % m)
        if isinstance(node.op, ast.UAdd):
            return p, m
        if isinstance(node.op, ast.Not):
            return ("(~(%s))" % p, "(!(%s))" % m)
        raise ExprError("unsupported unary operator: %s" % type(node.op).__name__)

    def v_BinOp(self, node):
        op = _BIN.get(type(node.op))
        if op is None:
            raise ExprError("unsupported operator: %s" % type(node.op).__name__)
        lp, lm = self.visit(node.left)
        rp, rm = self.visit(node.right)
        return ("(%s %s %s)" % (lp, op, rp), "(%s %s %s)" % (lm, op, rm))

    def v_Compare(self, node):
        parts_py, parts_mq = [], []
        left = node.left
        for op, right in zip(node.ops, node.comparators):
            sym = _CMP.get(type(op))
            if sym is None:
                raise ExprError("unsupported comparison: %s" % type(op).__name__)
            lp, lm = self.visit(left)
            rp, rm = self.visit(right)
            parts_py.append("(%s %s %s)" % (lp, sym, rp))
            parts_mq.append("(%s %s %s)" % (lm, sym, rm))
            left = right
        if len(parts_py) == 1:
            return parts_py[0], parts_mq[0]
        return ("(" + " & ".join(parts_py) + ")",
                "(" + " && ".join(parts_mq) + ")")

    def v_BoolOp(self, node):
        py_op, mq_op = ("&", "&&") if isinstance(node.op, ast.And) else ("|", "||")
        vals = [self.visit(v) for v in node.values]
        return ("(" + (" %s " % py_op).join(v[0] for v in vals) + ")",
                "(" + (" %s " % mq_op).join(v[1] for v in vals) + ")")

    def v_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ExprError("only %s may be called" % sorted(_FUNCS))
        py_fn, mq_fn, arity = _FUNCS[node.func.id]
        if len(node.args) != arity:
            raise ExprError("%s() takes %d argument(s)" % (node.func.id, arity))
        args = [self.visit(a) for a in node.args]
        return (py_fn + "(" + ", ".join(a[0] for a in args) + ")",
                mq_fn + "(" + ", ".join(a[1] for a in args) + ")")


def _mq_num(v):
    """Emit a numeric literal MQL5 always treats as double."""
    s = repr(float(v))
    return s if ("." in s or "e" in s or "E" in s) else s + ".0"


def default_mq5_ref(name, shift, rates="g_rates"):
    """Map `name[shift]` onto generated MQL5.

    Price fields read the rates array directly; everything else is an
    indicator function emitted by the code generator.  The `+ 1` skips the
    still-forming bar, which is what makes look-ahead structurally impossible.
    """
    if name in PRICE_FIELDS:
        return "%s[%d + 1].%s" % (rates, shift, PRICE_FIELDS[name])
    if name in TIME_FIELDS:
        return "%s(%s[%d + 1].time)" % (TIME_FIELDS[name], rates, shift)
    return "Ind_%s(%d)" % (name, shift)


def compile_expr(source, mq5_ref=default_mq5_ref):
    """Compile one expression string into NumPy + MQL5 forms."""
    src = (source or "").strip()
    if not src:
        raise ExprError("empty expression")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ExprError("cannot parse %r: %s" % (src, exc))
    c = _Compiler(mq5_ref)
    py, mq = c.visit(tree.body)
    return CompiledExpr(src, py, mq, list(c.refs))


def compile_indicator_expr(source, shift_var="s", rates="g_rates", suffix=""):
    """Compile an expression used INSIDE an indicator body.

    Identical to compile_expr except that references are offset by the
    enclosing indicator's own shift parameter.  ``rates``/``suffix`` let the
    same expression be re-emitted against a higher-timeframe rates array.
    """
    def ref(name, shift):
        if name in PRICE_FIELDS:
            return "%s[(%s) + %d + 1].%s" % (rates, shift_var, shift, PRICE_FIELDS[name])
        if name in TIME_FIELDS:
            return "%s(%s[(%s) + %d + 1].time)" % (
                TIME_FIELDS[name], rates, shift_var, shift)
        return "Ind_%s%s((%s) + %d)" % (name, suffix, shift_var, shift)
    return compile_expr(source, ref)
