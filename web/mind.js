/* Mind-Hermes MVP — Unified Compiler, IR v4 Builder, Fast ODE Engine & Handoff API.
 * Dual-format: Browser global (`window.MindCore`) and Node CommonJS (`module.exports`).
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MindCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var IR_VERSION = 4;

  function ModelError(msg, opts) {
    opts = opts || {};
    this.name = "ModelError";
    this.msg = msg;
    this.code = opts.code || "model_error";
    this.line = opts.line === undefined ? null : opts.line;
    this.origin = opts.origin === undefined ? null : opts.origin;
    this.message = this.render();
  }
  ModelError.prototype = Object.create(Error.prototype);
  ModelError.prototype.constructor = ModelError;
  ModelError.prototype.render = function () {
    var loc = "";
    if (this.origin) {
      loc = this.origin;
      if (this.line !== null) loc += ":" + this.line;
      loc += ": ";
    }
    return loc + this.msg;
  };
  ModelError.prototype.public = function () {
    return { code: this.code, message: this.render(), line: this.line, origin: this.origin };
  };

  var MATH_SIGNATURES = {
    sin: 1, cos: 1, tanh: 1, exp: 1, log: 1, sqrt: 1,
    abs: 1, floor: 1, sign: 1, sat: 1, step: 1,
    pow: 2, min: 2, max: 2, mod: 2, clamp: 3
  };
  // Stateful filter builtins: hidden buffers advanced once per step() with
  // alpha = 1 - exp(-dt/tau) (exact exponential, dt-aware; no overshoot when
  // dt > tau). ema(x, tau): exponential moving average tracking input x.
  // avg(x, window): boxcar mean of the last window/dt samples. Leaf equations only.
  // Optional 3rd argument: a stable string buffer NAME — ema(x, tau, "my_filter") —
  // so the hidden buffer keys by the name instead of the fragile path#uid call-site
  // position (reordering equations moves uid-keyed buffers; named buffers do not).
  var FILTER_SIGNATURES = { ema: 2, avg: 2 };   // min arity
  var FILTER_MAX_ARITY = { ema: 3, avg: 3 };    // 3rd arg = stable name string literal
  var CONSTANTS = { pi: Math.PI, tau: 2 * Math.PI, e: Math.E };
  var RESERVED = ["if", "then", "else", "and", "or", "not", "true", "false"];
  var BLOCK_KEYWORDS = ["import", "group", "config", "param", "state", "trait", "def", "render"];
  var ASSIGN_OPS = ["=", "+=", "-=", "*=", "/="];
  var SOLVER_NAMES = ["euler", "heun", "rk4"];

  var RESERVED_NAMES = Object.create(null);
  BLOCK_KEYWORDS.concat(RESERVED).concat(["params", "traits", "value", "dt", "t", "this"])
    .forEach(function (k) { RESERVED_NAMES[k] = true; });

  function checkFinite(v, label) {
    if (v !== v || !isFinite(v)) {
      throw new ModelError((label || "math operation") + " produced non-finite value", { code: "runtime_error" });
    }
    return v;
  }

  var MATH_IMPL = {
    sin: function (x) { return Math.sin(x); },
    cos: function (x) { return Math.cos(x); },
    tanh: function (x) { return Math.tanh(x); },
    sat: function (x) { return Math.tanh(x); },
    exp: function (x) { return checkFinite(Math.exp(x), "exp"); },
    log: function (x) {
      if (x <= 0) throw new ModelError("log() domain error: input <= 0", { code: "runtime_error" });
      return Math.log(x);
    },
    sqrt: function (x) {
      if (x < 0) throw new ModelError("sqrt() domain error: input < 0", { code: "runtime_error" });
      return Math.sqrt(x);
    },
    abs: function (x) { return Math.abs(x); },
    floor: function (x) { return Math.floor(x); },
    sign: function (x) { return (x > 0) - (x < 0); },
    step: function (x) { return x >= 0 ? 1 : 0; },
    pow: function (a, b) {
      if (a < 0 && Math.floor(b) !== b) {
        throw new ModelError("pow() domain error: negative base with fractional exponent", { code: "runtime_error" });
      }
      return checkFinite(Math.pow(a, b), "pow");
    },
    min: function (a, b) { return a < b ? a : b; },
    max: function (a, b) { return a > b ? a : b; },
    mod: function (a, b) {
      if (b === 0) throw new ModelError("mod() division by zero", { code: "runtime_error" });
      var r = a % b;
      return r < 0 ? r + Math.abs(b) : r;
    },
    clamp: function (x, lo, hi) { return x < lo ? lo : (x > hi ? hi : x); }
  };

  var TOKEN_RE = new RegExp(
    "[ \\t\\r]+|#[^\\n]*|\\n" + "|" +
    "(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][+-]?\\d+)?" + "|" +
    "[A-Za-z_][A-Za-z_0-9]*" + "|" +
    "\"[^\"\\n]*\"" + "|" +
    "\\+=|-=|\\*=|/=|==|!=|<=|>=|\\.|\\[|\\]|'|[-+*/=<>()?{},:]" + "|" +
    "[;&^!%~@|\\\\`]", "g"
  );

  function quote(s) { return "'" + s + "'"; }

  function tokenize(text, origin, startLine) {
    var toks = [], pos = 0, line = startLine || 1;
    TOKEN_RE.lastIndex = 0;
    while (pos < text.length) {
      TOKEN_RE.lastIndex = pos;
      var m = TOKEN_RE.exec(text);
      if (!m || m.index !== pos) {
        throw new ModelError("unexpected character " + quote(text[pos]), { line: line, origin: origin, code: "bad_character" });
      }
      var s = m[0];
      if (s === "\n") {
        toks.push(["nl", s, line, pos]);
        line += 1;
      } else if (s[0] === " " || s[0] === "\t" || s[0] === "\r" || s[0] === "#") {
        // skip whitespace & comments
      } else if (s[0] === '"' && s[s.length - 1] === '"') {
        toks.push(["str", s, line, pos]);
      } else if (/^[A-Za-z_]/.test(s)) {
        toks.push(["name", s, line, pos]);
      } else if (/^(\d|\.\d)/.test(s)) {
        toks.push(["num", s, line, pos]);
      } else if (/[;&^!%~@|\\`]/.test(s) && s.length === 1) {
        throw new ModelError("unexpected character " + quote(s), { line: line, origin: origin, code: "bad_character" });
      } else {
        toks.push(["op", s, line, pos]);
      }
      pos += s.length;
    }
    toks.push(["eof", "", line, text.length]);
    return toks;
  }

  function Parser(toks, origin, text, startIdx) {
    this.t = toks;
    this.origin = origin;
    this.text = text;
    this.i = startIdx || 0;
  }
  var P = Parser.prototype;

  P.peek = function (k) { return this.t[Math.min(this.i + (k || 0), this.t.length - 1)]; };
  P.next = function () { return this.t[this.i++]; };
  P.err = function (msg, tok, code) {
    tok = tok || this.peek();
    return new ModelError(msg, { line: tok[2], origin: this.origin, code: code || "syntax_error" });
  };
  P.accept = function (kind, val) {
    var tok = this.peek();
    if (tok[0] === kind && (val === undefined || val === null || tok[1] === val)) return this.next();
    return null;
  };
  P.expect = function (kind, val) {
    var tok = this.accept(kind, val);
    if (!tok) throw this.err("expected " + (val || kind) + ", got " + quote(this.peek()[1] || "end of file"));
    return tok;
  };
  P.skipNl = function () { while (this.peek()[0] === "nl") this.i += 1; };

  P.expr = function () {
    var p = this.peek();
    if (p[0] === "name" && p[1] === "if") {
      this.next();
      var c = this.expr();
      this.expect("name", "then");
      var a = this.expr();
      this.expect("name", "else");
      return ["if", c, a, this.expr()];
    }
    return this.orexpr();
  };
  P.orexpr = function () {
    var a = this.andexpr();
    while (this.accept("name", "or")) a = ["or", a, this.andexpr()];
    return a;
  };
  P.andexpr = function () {
    var a = this.notexpr();
    while (this.accept("name", "and")) a = ["and", a, this.notexpr()];
    return a;
  };
  P.notexpr = function () {
    if (this.accept("name", "not")) return ["not", this.notexpr()];
    return this.cmpexpr();
  };
  P.cmpexpr = function () {
    var a = this.addexpr();
    var p = this.peek();
    if (p[0] === "op" && ["<", ">", "<=", ">=", "==", "!="].indexOf(p[1]) >= 0) {
      var op = this.next()[1];
      return ["cmp", op, a, this.addexpr()];
    }
    return a;
  };
  P.addexpr = function () {
    var a = this.mulexpr();
    while (this.peek()[0] === "op" && (this.peek()[1] === "+" || this.peek()[1] === "-")) {
      var op = this.next()[1];
      a = ["bin", op, a, this.mulexpr()];
    }
    return a;
  };
  P.mulexpr = function () {
    var a = this.unary();
    while (this.peek()[0] === "op" && (this.peek()[1] === "*" || this.peek()[1] === "/")) {
      var op = this.next()[1];
      a = ["bin", op, a, this.unary()];
    }
    return a;
  };
  P.unary = function () {
    if (this.accept("op", "-")) return ["neg", this.unary()];
    this.accept("op", "+");
    return this.atom();
  };
  P.atom = function () {
    var tok = this.next(), kind = tok[0], s = tok[1], ln = tok[2];
    if (kind === "num") return ["num", parseFloat(s)];
    // String literals are valid ONLY as the stable-name argument of ema()/avg()
    // (validated in collectFilterSites); evalAst returns them as-is.
    if (kind === "str") return ["str", s.slice(1, -1), ln];
    if (kind === "name") {
      if (s === "true" || s === "false") return ["num", s === "true" ? 1 : 0];
      if (RESERVED.indexOf(s) >= 0) throw this.err("reserved word " + quote(s) + " used as value", tok);
      var segs = [s];
      while (this.accept("op", ".")) {
        var nxt = this.expect("name");
        segs.push(nxt[1]);
      }
      if (this.accept("op", "(")) {
        if (segs.length !== 1) throw this.err("dotted path " + quote(segs.join(".")) + " cannot be called as function", tok);
        var args = [];
        if (!this.accept("op", ")")) {
          args.push(this.expr());
          while (this.accept("op", ",")) args.push(this.expr());
          this.expect("op", ")");
        }
        return ["call", s, args, ln];
      }
      return ["var", segs, ln];
    }
    if (kind === "op" && s === "(") {
      var e = this.expr();
      this.expect("op", ")");
      return e;
    }
    throw this.err("expected expression value, got " + quote(s || "end of file"), tok);
  };

  function parseExpr(src, origin, baseLine) {
    var rawToks = tokenize(src, origin, baseLine || 1);
    var toks = [];
    for (var i = 0; i < rawToks.length; i++) {
      if (rawToks[i][0] !== "nl") toks.push(rawToks[i]);
    }
    var p = new Parser(toks, origin, src, 0);
    var ast = p.expr();
    if (p.peek()[0] !== "eof") {
      throw p.err("unexpected token " + quote(p.peek()[1]) + " after expression");
    }
    return ast;
  }

  var STATE_ATTRS = { label: 1, description: 1, initial: 1, min: 1, max: 1, unit: 1, group: 1, visible: 1 };
  var PARAM_ATTRS = { label: 1, description: 1, min: 1, max: 1, step: 1, group: 1, visible: 1, unit: 1 };
  var TRAIT_ATTRS = { label: 1, description: 1, default: 1, min: 1, max: 1, group: 1, visible: 1 };
  var RENDER_ATTRS = { label: 1, description: 1, visible: 1, max_clauses: 1, min_priority: 1, join: 1 };
  var CLAUSE_ATTRS = { priority: 1, when: 1 };

  P.metaValue = function () {
    this.skipNl();
    var pk = this.peek();
    if (pk[0] === "op" && (pk[1] === "-" || pk[1] === "+")) {
      var neg = this.accept("op", "-");
      this.accept("op", "+");
      var numTok = this.expect("num");
      return neg ? -Number(numTok[1]) : Number(numTok[1]);
    }
    if (pk[0] === "str") { this.next(); return pk[1].slice(1, -1); }
    if (pk[0] === "num") { this.next(); return Number(pk[1]); }
    if (pk[0] === "name" && (pk[1] === "true" || pk[1] === "false")) {
      this.next(); return pk[1] === "true";
    }
    if (pk[0] === "op" && pk[1] === "[") {
      this.next();
      var arr = [];
      this.skipNl();
      if (!this.accept("op", "]")) {
        arr.push(this.metaValue());
        this.skipNl();
        while (this.accept("op", ",")) {
          this.skipNl();
          arr.push(this.metaValue());
          this.skipNl();
        }
        this.expect("op", "]");
      }
      return arr;
    }
    throw this.err("expected attribute value");
  };

  P.readAttrs = function (allowed, kindName) {
    var out = Object.create(null);
    if (this.accept("op", "[")) {
      this.skipNl();
      while (!this.accept("op", "]")) {
        var k = this.expect("name")[1];
        if (!allowed[k]) throw this.err("unknown " + kindName + " attribute " + quote(k), null, "unknown_attr");
        this.expect("op", "=");
        out[k] = this.metaValue();
        this.skipNl();
        this.accept("op", ",");
        this.skipNl();
      }
    }
    while (this.peek()[0] === "name" && this.peek(1)[0] === "op" && this.peek(1)[1] === "=") {
      var k2 = this.next()[1];
      this.next();
      if (!allowed[k2]) throw this.err("unknown " + kindName + " attribute " + quote(k2), null, "unknown_attr");
      out[k2] = this.metaValue();
    }
    return out;
  };

  P.readBlockSpan = function () {
    this.skipNl();
    this.expect("op", "{");
    var start = this.i, depth = 1;
    while (true) {
      var pk = this.peek();
      if (pk[0] === "eof") throw this.err("unterminated '{' block", null, "unterminated_block");
      this.next();
      if (pk[0] === "op" && pk[1] === "{") depth += 1;
      else if (pk[0] === "op" && pk[1] === "}") {
        depth -= 1;
        if (depth === 0) return [start, this.i - 1];
      }
    }
  };

  P.stmtSpans = function (span) {
    var a0 = span[0], stop = span[1], out = [], i = a0;
    while (i < stop) {
      if (this.t[i][0] === "nl") { i += 1; continue; }
      var j = i, depth = 0;
      while (j < stop) {
        var tok = this.t[j];
        if (tok[0] === "op" && (tok[1] === "(" || tok[1] === "[" || tok[1] === "{")) depth += 1;
        else if (tok[0] === "op" && (tok[1] === ")" || tok[1] === "]" || tok[1] === "}")) depth -= 1;
        else if (tok[0] === "nl" && depth <= 0) {
          var k = j + 1;
          while (k < stop && this.t[k][0] === "nl") k += 1;
          if (k < stop) {
            var nxt = this.t[k], prev = this.t[j - 1];
            var prevCont = prev && ((prev[0] === "op" && ["+", "-", "*", "/", "=", ":", "<", ">", "<=", ">=", "==", "!=", ","].indexOf(prev[1]) >= 0) ||
                                    (prev[0] === "name" && ["if", "then", "else", "and", "or", "not"].indexOf(prev[1]) >= 0));
            var nextCont = (nxt[0] === "op" && ["+", "-", "*", "/", "<", ">", "<=", ">=", "==", "!="].indexOf(nxt[1]) >= 0) ||
                           (nxt[0] === "name" && ["then", "else", "and", "or"].indexOf(nxt[1]) >= 0);
            if (prevCont || nextCont) { j = k; continue; }
          }
          break;
        }
        j += 1;
      }
      out.push([i, Math.min(j, stop)]);
      i = j + 1;
    }
    return out;
  };

  P.sliceRaw = function (afterTok, stopIdx) {
    var j = stopIdx;
    while (j > this.i && this.t[j - 1][0] === "nl") j -= 1;
    if (j <= this.i) throw this.err("empty expression", afterTok, "empty_expr");
    var last = this.t[j - 1];
    return this.text.slice(afterTok[3] + afterTok[1].length, last[3] + last[1].length).trim();
  };

  P.dottedKey = function (disallowReserved) {
    var first = this.expect("name");
    if (disallowReserved && RESERVED_NAMES[first[1]]) {
      throw this.err(quote(first[1]) + " is a reserved word", first, "reserved_name");
    }
    var segs = [first[1]];
    while (this.accept("op", ".")) {
      var s = this.expect("name");
      if (disallowReserved && RESERVED_NAMES[s[1]]) {
        throw this.err(quote(s[1]) + " is a reserved word", s, "reserved_name");
      }
      segs.push(s[1]);
    }
    return segs;
  };

  P.parseDocument = function () {
    var doc = {
      config: { dt: 1.0, solver: "euler" },
      hasDt: false,
      params: Object.create(null),
      states: Object.create(null),
      leaves: Object.create(null),
      traits: Object.create(null),
      functions: Object.create(null),
      renders: Object.create(null),
      order: { params: [], states: [], leaves: [], traits: [], functions: [], renders: [] }
    };
    var activeGroup = null;
    this.skipNl();
    while (this.peek()[0] !== "eof") {
      var tk = this.next();
      if (tk[0] !== "name") throw this.err("expected block keyword, got " + quote(tk[1]), tk);
      var kw = tk[1], ln = tk[2];
      if (kw === "group") activeGroup = this.expect("str")[1].slice(1, -1);
      else if (kw === "config") this.readConfig(doc);
      else if (kw === "def") this.readDef(doc, ln);
      else if (kw === "param") this.readParam(doc, activeGroup, ln);
      else if (kw === "state") this.readState(doc, activeGroup, ln);
      else if (kw === "trait") this.readTrait(doc, activeGroup, ln);
      else if (kw === "render") this.readRender(doc, ln);
      else throw this.err("unknown block keyword " + quote(kw), tk, "unknown_block");
      this.skipNl();
    }
    return doc;
  };

  P.readConfig = function (doc) {
    var span = this.readBlockSpan();
    var after = this.i, seen = Object.create(null);
    this.i = span[0];
    this.skipNl();
    while (this.i < span[1] && this.peek()[0] !== "eof") {
      var kTok = this.expect("name"), k = kTok[1];
      if (seen[k]) throw this.err("duplicate config key " + quote(k), kTok, "duplicate_config");
      seen[k] = true;
      this.expect("op", "=");
      var v = this.metaValue();
      if (k === "dt") {
        if (typeof v !== "number" || !(v > 0)) throw this.err("config dt must be > 0", kTok, "bad_config");
        doc.config.dt = v;
        doc.hasDt = true;
      } else if (k === "solver") {
        if (SOLVER_NAMES.indexOf(v) < 0) throw this.err("unknown solver " + quote(v), kTok, "bad_config");
        doc.config.solver = v;
      } else {
        throw this.err("unknown config key " + quote(k), kTok, "unknown_config");
      }
      this.skipNl();
      this.accept("op", ",");
      this.skipNl();
    }
    this.i = after;
  };

  P.readDef = function (doc, ln) {
    var nameTok = this.expect("name"), name = nameTok[1];
    if (RESERVED_NAMES[name]) throw this.err(quote(name) + " is a reserved name", nameTok, "reserved_name");
    if (FILTER_SIGNATURES[name]) throw this.err("function name " + quote(name) + " collides with stateful builtin", nameTok, "reserved_name");
    if (doc.functions[name]) throw this.err("duplicate function " + quote(name), nameTok, "duplicate_function");
    this.expect("op", "(");
    var params = [];
    if (!this.accept("op", ")")) {
      while (true) {
        var pTok = this.expect("name");
        if (RESERVED_NAMES[pTok[1]]) throw this.err("reserved argument name " + quote(pTok[1]), pTok, "reserved_name");
        if (params.indexOf(pTok[1]) >= 0) throw this.err("duplicate argument " + quote(pTok[1]), pTok);
        params.push(pTok[1]);
        if (!this.accept("op", ",")) break;
      }
      this.expect("op", ")");
    }
    var eqTok = this.expect("op", "=");
    var span = this.stmtSpans([this.i, this.t.length - 1])[0];
    if (!span) throw this.err("empty function body", eqTok, "empty_body");
    var bodyLine = this.t[span[0]][2];
    var bodySrc = this.sliceRaw(eqTok, span[1]);
    this.i = span[1];
    doc.functions[name] = { name: name, params: params, body: bodySrc, line: bodyLine, origin: this.origin };
    doc.order.functions.push(name);
  };

  P.readParam = function (doc, activeGroup, ln) {
    var segs = this.dottedKey(true), name = segs.join(".");
    if (doc.params[name]) throw this.err("duplicate param " + quote(name), null, "duplicate_param");
    this.expect("op", "=");
    var val = this.metaValue();
    if (typeof val !== "number") throw this.err("param default must be a number", null, "bad_value");
    var attrs = this.readAttrs(PARAM_ATTRS, "param");
    var lo = attrs.min !== undefined ? Number(attrs.min) : null;
    var hi = attrs.max !== undefined ? Number(attrs.max) : null;
    if (lo !== null && hi !== null && lo > hi) throw this.err("param " + name + " has min > max", null, "bad_bounds");
    if (lo !== null && val < lo) throw this.err("param " + name + " default below min", null, "bad_bounds");
    if (hi !== null && val > hi) throw this.err("param " + name + " default above max", null, "bad_bounds");
    doc.params[name] = {
      name: name, default: val, min: lo, max: hi,
      step: attrs.step !== undefined ? Number(attrs.step) : 0.05,
      label: attrs.label || name, description: attrs.description || "",
      unit: attrs.unit || "", group: attrs.group || activeGroup || "Parameters",
      visible: attrs.visible !== undefined ? Boolean(attrs.visible) : true,
      line: ln, origin: this.origin
    };
    doc.order.params.push(name);
  };

  P.readState = function (doc, activeGroup, ln) {
    var nameTok = this.expect("name"), name = nameTok[1];
    if (RESERVED_NAMES[name]) throw this.err(quote(name) + " is a reserved name", nameTok, "reserved_name");
    if (doc.states[name]) throw this.err("duplicate state " + quote(name), nameTok, "duplicate_state");
    var attrs = this.readAttrs(STATE_ATTRS, "state");
    var lo = attrs.min !== undefined ? Number(attrs.min) : null;
    var hi = attrs.max !== undefined ? Number(attrs.max) : null;
    if (lo !== null && hi !== null && lo >= hi) throw this.err("state " + name + " has min >= max", nameTok, "bad_bounds");
    var span = this.readBlockSpan();
    var after = this.i;
    var stateEntry = {
      name: name, label: attrs.label || name, description: attrs.description || "",
      unit: attrs.unit || "", group: attrs.group || activeGroup || "States",
      visible: attrs.visible !== undefined ? Boolean(attrs.visible) : true,
      min: lo, max: hi, initial: attrs.initial !== undefined ? Number(attrs.initial) : 0.0,
      leaves: [], line: ln, origin: this.origin
    };
    doc.states[name] = stateEntry;
    doc.order.states.push(name);
    this.readStateEntries(doc, stateEntry, span, [name], attrs);
    this.i = after;
    if (!stateEntry.leaves.length) throw this.err("state " + quote(name) + " declares no leaves", nameTok, "no_leaves");
  };

  P.readStateEntries = function (doc, stateEntry, span, prefix, inherited) {
    var self = this;
    this.stmtSpans(span).forEach(function (sp) {
      var a = sp[0], b = sp[1];
      self.i = a;
      var headTok = self.expect("name"), head = headTok[1];
      var segs;
      if (head === "this") segs = prefix.slice();
      else {
        if (RESERVED_NAMES[head]) throw self.err("reserved leaf name " + quote(head), headTok, "reserved_name");
        segs = prefix.concat([head]);
        while (self.accept("op", ".")) {
          var sTok = self.expect("name");
          if (RESERVED_NAMES[sTok[1]]) throw self.err("reserved leaf name " + quote(sTok[1]), sTok, "reserved_name");
          segs.push(sTok[1]);
        }
      }
      var isOde = Boolean(self.accept("op", "'"));
      var attrs = self.readAttrs(STATE_ATTRS, "state");
      var merged = Object.create(null);
      Object.keys(inherited).forEach(function (k) { merged[k] = inherited[k]; });
      Object.keys(attrs).forEach(function (k) { merged[k] = attrs[k]; });

      if (self.peek()[0] === "op" && self.peek()[1] === "{") {
        if (isOde) throw self.err("prime (') cannot be placed on a namespace block", headTok);
        var innerSpan = self.readBlockSpan();
        self.readStateEntries(doc, stateEntry, innerSpan, segs, merged);
        return;
      }

      var opTok = self.next();
      if (!(opTok[0] === "op" && (opTok[1] === "=" || opTok[1] === ":"))) {
        throw self.err("expected '=' or ':' after leaf name " + quote(segs.join(".")), opTok, "bad_entry_op");
      }
      var path = segs.join(".");
      if (doc.leaves[path]) throw self.err("duplicate state leaf " + quote(path), headTok, "duplicate_leaf");
      var eqSrc = self.sliceRaw(opTok, b);
      var lo = merged.min !== undefined ? Number(merged.min) : null;
      var hi = merged.max !== undefined ? Number(merged.max) : null;
      var ini = merged.initial !== undefined ? Number(merged.initial) : 0.0;
      if (lo !== null && hi !== null) {
        if (lo >= hi) throw self.err("leaf " + quote(path) + " has min >= max", headTok, "bad_bounds");
        if (isOde && (ini < lo || ini > hi)) {
          throw self.err("leaf " + quote(path) + " initial value outside [min, max]", headTok, "bad_bounds");
        }
      }
      var defaultLabel = path === stateEntry.name ? stateEntry.label : segs.slice(1).join(".");
      doc.leaves[path] = {
        path: path, state: stateEntry.name, kind: isOde ? "ode" : "view",
        initial: isOde ? ini : null, min: lo, max: hi,
        label: attrs.label || defaultLabel, description: merged.description || "",
        unit: merged.unit || stateEntry.unit || "",
        visible: merged.visible !== undefined ? Boolean(merged.visible) : true,
        equation: eqSrc, line: opTok[2], origin: self.origin
      };
      stateEntry.leaves.push(path);
      doc.order.leaves.push(path);
    });
  };

  P.readTrait = function (doc, activeGroup, ln) {
    var self = this;
    var nameTok = this.expect("name"), name = nameTok[1];
    if (RESERVED_NAMES[name]) throw this.err(quote(name) + " is a reserved name", nameTok, "reserved_name");
    if (doc.traits[name]) throw this.err("duplicate trait " + quote(name), nameTok, "duplicate_trait");
    var attrs = this.readAttrs(TRAIT_ATTRS, "trait");
    var lo = attrs.min !== undefined ? Number(attrs.min) : -1.0;
    var hi = attrs.max !== undefined ? Number(attrs.max) : 1.0;
    var dflt = attrs.default !== undefined ? Number(attrs.default) : 0.0;
    if (lo >= hi) throw this.err("trait " + name + " has min >= max", nameTok, "bad_bounds");
    if (dflt < lo || dflt > hi) throw this.err("trait " + name + " default outside [min, max]", nameTok, "bad_bounds");

    var span = this.readBlockSpan();
    var after = this.i, stmts = [];
    this.stmtSpans(span).forEach(function (sp) {
      var a = sp[0], b = sp[1];
      self.i = a;
      var startTok = self.peek();
      var segs = self.dottedKey(false);
      var isDerivTarget = Boolean(self.accept("op", "'"));
      var opTok = self.next();
      if (opTok[0] !== "op" || ASSIGN_OPS.indexOf(opTok[1]) < 0) {
        throw self.err("trait statement must use =, +=, -=, *=, or /=", opTok);
      }
      if (segs.length === 1 && segs[0] === "value") {
        throw self.err("`value` is read-only inside trait blocks", startTok, "readonly_target");
      }
      var eqSrc = self.sliceRaw(opTok, b);
      stmts.push({ segs: segs, is_rate: isDerivTarget, op: opTok[1], equation: eqSrc, line: opTok[2] });
    });
    this.i = after;
    if (!stmts.length) throw this.err("trait " + quote(name) + " has an empty body", nameTok, "empty_trait");

    doc.traits[name] = {
      name: name, label: attrs.label || name, description: attrs.description || "",
      group: attrs.group || activeGroup || "Persona", min: lo, max: hi, default: dflt,
      visible: attrs.visible !== undefined ? Boolean(attrs.visible) : true,
      stmts: stmts, line: ln, origin: this.origin
    };
    doc.order.traits.push(name);
  };

  P.readRender = function (doc, ln) {
    var self = this;
    var nameTok = this.expect("name"), name = nameTok[1];
    if (RESERVED_NAMES[name]) throw this.err(quote(name) + " is a reserved name", nameTok, "reserved_name");
    if (doc.renders[name]) throw this.err("duplicate render block " + quote(name), nameTok, "duplicate_renderer");
    var attrs = this.readAttrs(RENDER_ATTRS, "render");
    var span = this.readBlockSpan();
    var after = this.i, tmpl = "", header = "", footer = "", fields = null, when = null, maxClausesExpr = null, minPriorityExpr = null, fmt = Object.create(null), clauses = [];

    this.stmtSpans(span).forEach(function (sp) {
      var a = sp[0], b = sp[1];
      self.i = a;
      if (self.peek()[0] === "name" && self.peek()[1] === "clause") {
        var cTok = self.next();
        var cName = self.expect("name")[1];
        var cAttrs = { priority: "1.0", when: null };
        if (self.accept("op", "[")) {
          self.skipNl();
          while (!self.accept("op", "]")) {
            var ck = self.expect("name")[1];
            if (!CLAUSE_ATTRS[ck]) throw self.err("unknown clause attribute " + quote(ck), cTok, "unknown_attr");
            var eqT = self.expect("op", "=");
            var startI = self.i, d = 0;
            while (self.i < b) {
              var tk = self.peek();
              if (tk[0] === "op" && (tk[1] === "(" || tk[1] === "[")) d += 1;
              else if (tk[0] === "op" && (tk[1] === ")" || tk[1] === "]")) {
                if (d === 0 && tk[1] === "]") break;
                d -= 1;
              } else if (tk[0] === "op" && tk[1] === "," && d === 0) break;
              self.next();
            }
            var endI = self.i;
            self.i = startI;
            cAttrs[ck] = self.sliceRaw(eqT, endI);
            self.i = endI;
            self.skipNl(); self.accept("op", ","); self.skipNl();
          }
        }
        self.expect("op", "=");
        var cText = self.metaValue();
        if (typeof cText !== "string") throw self.err("clause text must be a string", cTok);
        clauses.push({ name: cName, priority: cAttrs.priority, when: cAttrs.when, text: cText, line: cTok[2] });
        return;
      }
      var key = self.dottedKey(false);
      var opTok = self.expect("op", "=");
      if (key.length === 1 && key[0] === "when") {
        when = { equation: self.sliceRaw(opTok, b), line: opTok[2] };
      } else if (key.length === 1 && key[0] === "max_clauses") {
        maxClausesExpr = { equation: self.sliceRaw(opTok, b), line: opTok[2] };
      } else if (key.length === 1 && key[0] === "min_priority") {
        minPriorityExpr = { equation: self.sliceRaw(opTok, b), line: opTok[2] };
      } else if (key.length === 1) {
        var v = self.metaValue();
        if (key[0] === "template" || key[0] === "header" || key[0] === "footer") {
          if (typeof v !== "string") throw self.err(key[0] + " must be a string", opTok);
          if (key[0] === "template") tmpl = v;
          else if (key[0] === "header") header = v;
          else footer = v;
        } else if (key[0] === "fields") {
          if (!Array.isArray(v)) throw self.err("fields must be a list of strings", opTok);
          fields = v;
        } else {
          throw self.err("unknown render key " + quote(key[0]), opTok, "unknown_config");
        }
      } else {
        var fld = key.slice(0, -1).join("."), prop = key[key.length - 1];
        if (prop !== "decimals" && prop !== "label" && prop !== "bands") {
          throw self.err("unknown format property " + quote(prop), opTok, "unknown_format");
        }
        var fv = self.metaValue();
        if (!fmt[fld]) fmt[fld] = Object.create(null);
        fmt[fld][prop] = fv;
      }
    });
    this.i = after;
    if (!tmpl && !clauses.length) throw this.err("render " + quote(name) + " requires template or clauses", nameTok, "missing_template");

    var extracted = [];
    var scanStr = [tmpl, header, footer].concat(clauses.map(function (c) { return c.text; })).join(" ");
    var re = /\{([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)\}/g, m;
    while ((m = re.exec(scanStr)) !== null) {
      if (extracted.indexOf(m[1]) < 0) extracted.push(m[1]);
    }

    doc.renders[name] = {
      name: name, label: attrs.label || name, description: attrs.description || "",
      visible: attrs.visible !== undefined ? Boolean(attrs.visible) : true,
      max_clauses: attrs.max_clauses !== undefined ? Number(attrs.max_clauses) : 5,
      min_priority: attrs.min_priority !== undefined ? Number(attrs.min_priority) : 0.18,
      join: attrs.join !== undefined ? String(attrs.join) : " ",
      header: header, footer: footer,
      max_clauses_expr: maxClausesExpr, min_priority_expr: minPriorityExpr,
      template: tmpl, fields: fields !== null ? fields : extracted,
      template_fields: extracted, clauses: clauses, when: when, format: fmt, line: ln, origin: this.origin
    };
    doc.order.renders.push(name);
  };

  function rewriteThis(ast, leafPath) {
    if (!Array.isArray(ast) || !leafPath) return ast;
    var k = ast[0];
    if (k === "var") {
      if (ast[1][0] === "this") return ["var", leafPath.split(".").concat(ast[1].slice(1)), ast[2]];
      return ast;
    }
    if (k === "call") {
      return ["call", ast[1], ast[2].map(function (a) { return rewriteThis(a, leafPath); }), ast[3]];
    }
    return ast.map(function (c, idx) { return idx === 0 ? c : rewriteThis(c, leafPath); });
  }

  function astCalls(ast, out) {
    out = out || [];
    var k = ast[0];
    if (k === "call") {
      out.push([ast[1], ast[2].length, ast[3]]);
      ast[2].forEach(function (a) { astCalls(a, out); });
    } else if (k === "bin" || k === "cmp") {
      astCalls(ast[2], out); astCalls(ast[3], out);
    } else if (k === "and" || k === "or") {
      astCalls(ast[1], out); astCalls(ast[2], out);
    } else if (k === "neg" || k === "not") astCalls(ast[1], out);
    else if (k === "if") { astCalls(ast[1], out); astCalls(ast[2], out); astCalls(ast[3], out); }
    return out;
  }

  function astVars(ast, out) {
    out = out || [];
    var k = ast[0];
    if (k === "var") out.push([ast[1], ast[2]]);
    else if (k === "call") ast[2].forEach(function (a) { astVars(a, out); });
    else if (k === "bin" || k === "cmp") {
      astVars(ast[2], out); astVars(ast[3], out);
    } else if (k === "and" || k === "or") {
      astVars(ast[1], out); astVars(ast[2], out);
    } else if (k === "neg" || k === "not") astVars(ast[1], out);
    else if (k === "if") { astVars(ast[1], out); astVars(ast[2], out); astVars(ast[3], out); }
    return out;
  }

  function resolveSymbol(doc, segs, locals, allowValue, line, origin) {
    if (segs.length === 1) {
      var s = segs[0];
      if (locals && locals[s]) return ["local", s];
      if (allowValue && s === "value") return ["value", "value"];
      if (s === "dt") return ["dt", "dt"];
      if (s === "t") return ["t", "t"];
      if (Object.prototype.hasOwnProperty.call(CONSTANTS, s)) return ["const", s];
    }
    if (segs[0] === "params") {
      var pname = segs.slice(1).join(".");
      if (!pname || !doc.params[pname]) {
        throw new ModelError("unknown parameter " + quote(pname || "params"), { line: line, origin: origin, code: "unknown_ref" });
      }
      return ["param", "params." + pname];
    }
    if (segs[0] === "traits") {
      var tname = segs.slice(1).join(".");
      if (segs.length !== 2 || !doc.traits[tname]) {
        throw new ModelError("unknown trait " + quote(tname || "traits"), { line: line, origin: origin, code: "unknown_ref" });
      }
      return ["trait", "traits." + tname];
    }
    var lpath = segs.join(".");
    if (doc.leaves[lpath]) return ["leaf", lpath];
    if (doc.states[segs[0]]) {
      throw new ModelError("state " + quote(segs[0]) + " has no leaf " + quote(lpath), { line: line, origin: origin, code: "unknown_ref" });
    }
    throw new ModelError("unknown reference " + quote(lpath), { line: line, origin: origin, code: "unknown_ref" });
  }

  function validateCalls(ast, doc, origin, allowFilters) {
    astCalls(ast).forEach(function (c) {
      var fname = c[0], argc = c[1], ln = c[2];
      if (Object.prototype.hasOwnProperty.call(FILTER_SIGNATURES, fname)) {
        if (!allowFilters) {
          throw new ModelError(fname + "() is allowed only in leaf equations (not in def bodies, trait blocks, or render expressions)", { line: ln, origin: origin, code: "filter_placement" });
        }
        var lo = FILTER_SIGNATURES[fname], hi = FILTER_MAX_ARITY[fname];
        if (argc < lo || argc > hi) {
          throw new ModelError(fname + "() expects " + lo + (hi > lo ? "-" + hi : "") + " args (last = stable buffer name string), got " + argc, { line: ln, origin: origin, code: "arity_error" });
        }
        return;
      }
      if (Object.prototype.hasOwnProperty.call(MATH_SIGNATURES, fname)) {
        if (MATH_SIGNATURES[fname] !== argc) {
          throw new ModelError(fname + "() expects " + MATH_SIGNATURES[fname] + " args, got " + argc, { line: ln, origin: origin, code: "arity_error" });
        }
        return;
      }
      var fn = doc.functions[fname];
      if (!fn) throw new ModelError("undefined function " + quote(fname), { line: ln, origin: origin, code: "undefined_function" });
      if (fn.params.length !== argc) {
        throw new ModelError(fname + "() expects " + fn.params.length + " args, got " + argc, { line: ln, origin: origin, code: "arity_error" });
      }
    });
  }

  function collectFilterSites(ast, out) {
    (function walk(node) {
      if (!Array.isArray(node)) return;
      var k = node[0];
      if (k === "call") {
        var fmin = FILTER_SIGNATURES[node[1]];
        if (fmin && node[2].length >= fmin && node[2].length <= FILTER_MAX_ARITY[node[1]]) {
          var fname = null;
          if (node[2].length === 3) {
            var narg = node[2][2];
            if (!Array.isArray(narg) || narg[0] !== "str" || !narg[1]) {
              throw new ModelError(node[1] + "() stable name must be a non-empty string literal (\"my_filter\")", { line: node[3], code: "filter_name_type" });
            }
            fname = narg[1];
          }
          out.push({ kind: node[1], node: node, line: node[3], name: fname });
        }
        node[2].forEach(walk);
      } else if (k === "bin" || k === "cmp") { walk(node[2]); walk(node[3]); }
      else if (k === "and" || k === "or") { walk(node[1]); walk(node[2]); }
      else if (k === "neg" || k === "not") walk(node[1]);
      else if (k === "if") { walk(node[1]); walk(node[2]); walk(node[3]); }
    })(ast);
  }

  function buildIr(sourceInput) {
    var sources = typeof sourceInput === "string" ? [{ origin: "model/model.mind", text: sourceInput }] : sourceInput;
    var src = sources[0];
    var toks = tokenize(src.text, src.origin, 1);
    var parser = new Parser(toks, src.origin, src.text, 0);
    var doc = parser.parseDocument();

    if (!doc.hasDt) throw new ModelError("config { dt = ... } is required", { origin: src.origin, code: "missing_timing" });

    var fnGraph = Object.create(null);
    doc.order.functions.forEach(function (fname) {
      var fn = doc.functions[fname];
      var ast = parseExpr(fn.body, fn.origin, fn.line);
      validateCalls(ast, doc, fn.origin);
      var locals = Object.create(null);
      fn.params.forEach(function (p) { locals[p] = true; });
      astVars(ast).forEach(function (v) { resolveSymbol(doc, v[0], locals, false, v[1], fn.origin); });
      fn.ast = ast;
      var callees = [];
      astCalls(ast).forEach(function (c) {
        if (doc.functions[c[0]] && callees.indexOf(c[0]) < 0) callees.push(c[0]);
      });
      fnGraph[fname] = callees;
    });

    function checkFnCycle(name, stack) {
      fnGraph[name].forEach(function (callee) {
        if (stack.indexOf(callee) >= 0) {
          throw new ModelError("recursive function call: " + stack.concat([callee]).join(" -> "), {
            line: doc.functions[name].line, origin: doc.functions[name].origin, code: "recursion"
          });
        }
        checkFnCycle(callee, stack.concat([callee]));
      });
    }
    doc.order.functions.forEach(function (fname) { checkFnCycle(fname, [fname]); });

    var usedBy = Object.create(null);
    doc.order.params.forEach(function (p) { usedBy[p] = []; });
    function recordParamUse(ref, label) {
      if (ref.indexOf("params.") === 0) {
        var pn = ref.slice(7);
        if (usedBy[pn] && usedBy[pn].indexOf(label) < 0) usedBy[pn].push(label);
      }
    }

    var odeOrder = [], viewNodes = [];
    var filterUid = 0, filterSites = [], filterNames = Object.create(null);
    doc.order.leaves.forEach(function (path) {
      var lf = doc.leaves[path];
      var rawAst = parseExpr(lf.equation, lf.origin, lf.line);
      var ast = rewriteThis(rawAst, path);
      validateCalls(ast, doc, lf.origin, true);
      var refs = [];
      astVars(ast).forEach(function (v) {
        var res = resolveSymbol(doc, v[0], null, false, v[1], lf.origin);
        if (res[0] !== "const" && refs.indexOf(res[1]) < 0) refs.push(res[1]);
        recordParamUse(res[1], path);
      });
      lf.ast = ast;
      lf.refs = refs.sort();
      var sites = [];
      collectFilterSites(ast, sites);
      lf.filters = sites.map(function (s) {
        var uid = ++filterUid;
        s.uid = uid; s.leaf = path;
        var key = s.name || (path + "#" + uid);
        if (s.name) {
          if (filterNames[s.name]) {
            throw new ModelError("duplicate filter buffer name " + quote(s.name) + " (first used at " + filterNames[s.name].leaf + ")", { line: s.line, origin: lf.origin, code: "filter_name_dup" });
          }
          filterNames[s.name] = { leaf: path, uid: uid };
        }
        filterSites.push({ key: key, kind: s.kind, leaf: path, uid: uid, name: s.name || null, line: s.line });
        return s;
      });
      if (lf.kind === "ode") odeOrder.push(path);
      else viewNodes.push(path);
    });

    var viewSet = Object.create(null);
    viewNodes.forEach(function (p) { viewSet[p] = true; });
    var viewOrder = [], remaining = viewNodes.slice();
    while (remaining.length) {
      var ready = remaining.filter(function (p) {
        var deps = doc.leaves[p].refs;
        var hasFilter = (doc.leaves[p].filters || []).length > 0;
        for (var i = 0; i < deps.length; i++) {
          if (deps[i] === p && hasFilter) continue; // `this` in a filter leaf = step-start buffer, not a frame dep
          if (viewSet[deps[i]] && remaining.indexOf(deps[i]) >= 0) return false;
        }
        return true;
      });
      if (!ready.length) {
        var cycLeaf = doc.leaves[remaining[0]];
        throw new ModelError("circular dependency among algebraic views: " + remaining.join(", "), {
          line: cycLeaf.line, origin: cycLeaf.origin, code: "leaf_cycle"
        });
      }
      ready.forEach(function (p) {
        viewOrder.push(p);
        remaining.splice(remaining.indexOf(p), 1);
      });
    }

    doc.order.traits.forEach(function (tname) {
      var tr = doc.traits[tname], affects = [];
      tr.stmts.forEach(function (st) {
        var ast = parseExpr(st.equation, tr.origin, st.line);
        validateCalls(ast, doc, tr.origin);
        var refs = [];
        astVars(ast).forEach(function (v) {
          var r = resolveSymbol(doc, v[0], null, true, v[1], tr.origin);
          if (r[0] !== "const" && refs.indexOf(r[1]) < 0) refs.push(r[1]);
          recordParamUse(r[1], "trait " + tname);
        });
        var tRes = resolveSymbol(doc, st.segs, null, false, st.line, tr.origin);
        if (tRes[0] === "param") {
          if (st.is_rate) throw new ModelError("cannot apply prime (') to parameter " + quote(tRes[1]), { line: st.line, origin: tr.origin, code: "bad_target" });
          st.target_kind = "param";
          st.target = tRes[1];
          recordParamUse(tRes[1], "trait " + tname);
        } else if (tRes[0] === "leaf") {
          var targetLeaf = doc.leaves[tRes[1]];
          if (targetLeaf.kind === "ode") {
            st.target_kind = "ode_rate";
            st.target = tRes[1];
          } else {
            if (st.is_rate) throw new ModelError("cannot apply prime (') to view " + quote(tRes[1]), { line: st.line, origin: tr.origin, code: "bad_target" });
            st.target_kind = "view";
            st.target = tRes[1];
          }
        } else {
          throw new ModelError("traits can only target params.NAME, views, or ODE rates", { line: st.line, origin: tr.origin, code: "bad_target" });
        }
        st.ast = ast;
        st.refs = refs.sort();
        var dispTarget = st.target + (st.target_kind === "ode_rate" ? "'" : "");
        if (affects.indexOf(dispTarget) < 0) affects.push(dispTarget);
      });
      tr.affects = affects;
    });

    doc.order.params.forEach(function (pname) { doc.params[pname].used_by = usedBy[pname]; });

    doc.order.renders.forEach(function (rname) {
      var rn = doc.renders[rname];
      function checkField(f) { resolveSymbol(doc, f.split("."), null, false, rn.line, rn.origin); }
      rn.template_fields.forEach(function (tf) {
        checkField(tf);
        if (rn.fields.indexOf(tf) < 0) {
          throw new ModelError("render " + quote(rname) + " omits {" + tf + "} from fields", { line: rn.line, origin: rn.origin, code: "missing_field" });
        }
      });
      rn.fields.forEach(checkField);
      Object.keys(rn.format).forEach(function (fld) {
        checkField(fld);
        var b = rn.format[fld].bands;
        if (b) {
          b.forEach(function (band) {
            if (!Array.isArray(band) || band.length !== 3 || band[0] >= band[1]) {
              throw new ModelError("render " + quote(rname) + " has invalid band on " + quote(fld), { line: rn.line, origin: rn.origin, code: "bad_bands" });
            }
          });
        }
      });
      if (rn.when) {
        var wAst = parseExpr(rn.when.equation, rn.origin, rn.when.line);
        validateCalls(wAst, doc, rn.origin);
        astVars(wAst).forEach(function (v) { resolveSymbol(doc, v[0], null, false, v[1], rn.origin); });
        rn.when.ast = wAst;
      }
      if (rn.max_clauses_expr) {
        var mcAst = parseExpr(rn.max_clauses_expr.equation, rn.origin, rn.max_clauses_expr.line);
        validateCalls(mcAst, doc, rn.origin);
        astVars(mcAst).forEach(function (v) { resolveSymbol(doc, v[0], null, false, v[1], rn.origin); });
        rn.max_clauses_expr.ast = tagAstKeys(mcAst);
      }
      if (rn.min_priority_expr) {
        var mpAst = parseExpr(rn.min_priority_expr.equation, rn.origin, rn.min_priority_expr.line);
        validateCalls(mpAst, doc, rn.origin);
        astVars(mpAst).forEach(function (v) { resolveSymbol(doc, v[0], null, false, v[1], rn.origin); });
        rn.min_priority_expr.ast = tagAstKeys(mpAst);
      }
      (rn.clauses || []).forEach(function (cl) {
        var pAst = parseExpr(cl.priority, rn.origin, cl.line);
        validateCalls(pAst, doc, rn.origin);
        astVars(pAst).forEach(function (v) { resolveSymbol(doc, v[0], null, false, v[1], rn.origin); });
        cl.priority_ast = tagAstKeys(pAst);
        if (cl.when) {
          var cwAst = parseExpr(cl.when, rn.origin, cl.line);
          validateCalls(cwAst, doc, rn.origin);
          astVars(cwAst).forEach(function (v) { resolveSymbol(doc, v[0], null, false, v[1], rn.origin); });
          cl.when_ast = tagAstKeys(cwAst);
        }
      });
    });

    return {
      version: IR_VERSION, config: doc.config, constants: Object.keys(CONSTANTS).sort(),
      builtins: MATH_SIGNATURES, filterBuiltins: FILTER_SIGNATURES, filterMaxArity: FILTER_MAX_ARITY, functions: doc.functions, params: doc.params,
      states: doc.states, leaves: doc.leaves, filterSites: filterSites,
      schedule: { ode_order: odeOrder, view_order: viewOrder },
      traits: doc.traits, renders: doc.renders, order: doc.order
    };
  }

  function applyAssignOp(op, cur, val) {
    if (op === "=") return val;
    if (op === "+=") return cur + val;
    if (op === "-=") return cur - val;
    if (op === "*=") return cur * val;
    if (op === "/=") {
      if (val === 0) throw new ModelError("division by zero in trait operator", { code: "runtime_error" });
      return cur / val;
    }
    return val;
  }

  function clampLeaf(v, leaf) {
    if (leaf.min !== null && leaf.min !== undefined && v < leaf.min) v = leaf.min;
    if (leaf.max !== null && leaf.max !== undefined && v > leaf.max) v = leaf.max;
    return v;
  }

  function tagAstKeys(node) {
    if (!Array.isArray(node)) return node;
    var k = node[0];
    if (k === "var") {
      node.key = node[1].length === 1 ? node[1][0] : node[1].join(".");
      return node;
    }
    if (k === "call") {
      for (var i = 0; i < node[2].length; i++) tagAstKeys(node[2][i]);
      return node;
    }
    for (var j = 1; j < node.length; j++) tagAstKeys(node[j]);
    return node;
  }

  function Engine(ir) {
    var self = this;
    this.ir = ir;
    this.dt = Number(ir.config.dt);
    this.solver = ir.config.solver || "euler";
    this.odeOrder = ir.schedule.ode_order;
    this.viewOrder = ir.schedule.view_order;

    ir.order.functions.forEach(function (fn) { tagAstKeys(ir.functions[fn].ast); });
    ir.order.leaves.forEach(function (lp) { tagAstKeys(ir.leaves[lp].ast); });

    // Hidden filter buffers: one per call-site, keyed by the stable NAME when the
    // call supplies one (ema(x,tau,"name")), else by the legacy path#uid call-site
    // key. Advance order is reverse uid so nested filters update inner-first (outer
    // sees the freshly advanced inner buffer). Named defs also register a legacy
    // path#uid alias so pre-name blobs migrate their buffer values gracefully.
    this.filterDefs = [];
    this.filterByKey = Object.create(null);
    this.filterLegacyKeys = Object.create(null);
    ir.order.leaves.forEach(function (lp) {
      (ir.leaves[lp].filters || []).forEach(function (site) {
        var key = site.name || (lp + "#" + site.uid);
        site.node.fkey = key;
        var item = { key: key, leaf: lp, uid: site.uid, kind: site.kind, name: site.name || null, input: site.node[2][0], tau: site.node[2][1] };
        self.filterDefs.push(item);
        self.filterByKey[key] = item;
        if (site.name) self.filterLegacyKeys[lp + "#" + site.uid] = item;
      });
    });
    this.filterDefs.sort(function (a, b) { return b.uid - a.uid; });

    this.paramTraitStmts = [];
    this.viewTraitStmts = Object.create(null);
    this.rateTraitStmts = Object.create(null);

    ir.order.traits.forEach(function (tname) {
      ir.traits[tname].stmts.forEach(function (st) {
        tagAstKeys(st.ast);
        var item = { trait: tname, op: st.op, target: st.target, ast: st.ast };
        if (st.target_kind === "param") self.paramTraitStmts.push(item);
        else if (st.target_kind === "view") {
          if (!self.viewTraitStmts[st.target]) self.viewTraitStmts[st.target] = [];
          self.viewTraitStmts[st.target].push(item);
        } else if (st.target_kind === "ode_rate") {
          if (!self.rateTraitStmts[st.target]) self.rateTraitStmts[st.target] = [];
          self.rateTraitStmts[st.target].push(item);
        }
      });
    });

    this.reset();
  }
  var E = Engine.prototype;

  E.reset = function () {
    var self = this;
    this.simTime = 0.0;
    this.state = Object.create(null);
    this.filters = Object.create(null);   // hidden ema/avg buffers; boot-init on first read
    this.odeOrder.forEach(function (path) {
      self.state[path] = Number(self.ir.leaves[path].initial || 0.0);
    });
    this.traitValues = Object.create(null);
    this.ir.order.traits.forEach(function (tname) {
      self.traitValues[tname] = Number(self.ir.traits[tname].default || 0.0);
    });
    this.userParams = Object.create(null);
    this.lastRates = Object.create(null);
    this.odeOrder.forEach(function (path) { self.lastRates[path] = 0.0; });
  };

  E.evalAst = function (node, ctx, locals) {
    var k = node[0];
    switch (k) {
      case "num": return node[1];
      case "str": return node[1];
      case "var": {
        var key = node.key || (node.key = node[1].join("."));
        if (locals && locals[key] !== undefined) return locals[key];
        var v = ctx[key];
        if (v !== undefined) return v;
        if (CONSTANTS[key] !== undefined) return CONSTANTS[key];
        throw new ModelError("unknown runtime variable " + quote(key), { code: "unknown_ref" });
      }
      case "neg": return -this.evalAst(node[1], ctx, locals);
      case "not": return this.evalAst(node[1], ctx, locals) !== 0 ? 0 : 1;
      case "and": return (this.evalAst(node[1], ctx, locals) !== 0 && this.evalAst(node[2], ctx, locals) !== 0) ? 1 : 0;
      case "or": return (this.evalAst(node[1], ctx, locals) !== 0 || this.evalAst(node[2], ctx, locals) !== 0) ? 1 : 0;
      case "if": return this.evalAst(node[1], ctx, locals) !== 0 ? this.evalAst(node[2], ctx, locals) : this.evalAst(node[3], ctx, locals);
      case "cmp": {
        var a = this.evalAst(node[2], ctx, locals), b = this.evalAst(node[3], ctx, locals);
        switch (node[1]) {
          case "<": return a < b ? 1 : 0;
          case ">": return a > b ? 1 : 0;
          case "<=": return a <= b ? 1 : 0;
          case ">=": return a >= b ? 1 : 0;
          case "==": return a === b ? 1 : 0;
          default: return a !== b ? 1 : 0;
        }
      }
      case "bin": {
        var x = this.evalAst(node[2], ctx, locals), y = this.evalAst(node[3], ctx, locals);
        switch (node[1]) {
          case "+": return x + y;
          case "-": return x - y;
          case "*": return x * y;
          default:
            if (y === 0) throw new ModelError("division by zero", { code: "runtime_error" });
            return x / y;
        }
      }
      case "call": {
        var fname = node[1], argNodes = node[2], nArgs = argNodes.length;
        if (FILTER_SIGNATURES[fname]) return this.readFilter(node, ctx, locals);
        var userFn = this.ir.functions[fname];
        if (userFn) {
          var fnLocals = Object.create(null);
          for (var j = 0; j < nArgs; j++) fnLocals[userFn.params[j]] = this.evalAst(argNodes[j], ctx, locals);
          return this.evalAst(userFn.ast, ctx, fnLocals);
        }
        var fnImpl = MATH_IMPL[fname];
        if (nArgs === 1) return fnImpl(this.evalAst(argNodes[0], ctx, locals));
        if (nArgs === 2) return fnImpl(this.evalAst(argNodes[0], ctx, locals), this.evalAst(argNodes[1], ctx, locals));
        if (nArgs === 3) return fnImpl(this.evalAst(argNodes[0], ctx, locals), this.evalAst(argNodes[1], ctx, locals), this.evalAst(argNodes[2], ctx, locals));
        var args = new Array(nArgs);
        for (var i = 0; i < nArgs; i++) args[i] = this.evalAst(argNodes[i], ctx, locals);
        return fnImpl.apply(null, args);
      }
    }
    throw new ModelError("invalid AST node " + quote(k), { code: "runtime_error" });
  };

  // Read-only filter value: boot-init to the FIRST OBSERVED input (ema) or a ring full
  // of it (avg) so neither a cold start nor a truncated window drags the value toward 0.
  E.readFilter = function (node, ctx, locals) {
    var key = node.fkey;
    var e = this.filters[key];
    if (!e) {
      var x = checkFinite(this.evalAst(node[2][0], ctx, locals), node[1] + "() input");
      if (node[1] === "ema") {
        e = this.filters[key] = { kind: "ema", value: x };
      } else {
        var w = checkFinite(this.evalAst(node[2][1], ctx, locals), "avg() window");
        var n = Math.max(1, Math.round(w / this.dt));
        var s = new Array(n);
        for (var i = 0; i < n; i++) s[i] = x;
        e = this.filters[key] = { kind: "avg", samples: s, sum: x * n };
      }
    }
    if (e.kind === "ema") return e.value;
    return e.samples.length ? e.sum / e.samples.length : 0;
  };

  // Advance every hidden buffer once per step with the step's REAL dt:
  // alpha = 1 - exp(-dt/tau) — exact exponential relaxation, no overshoot when dt > tau.
  E.advanceFilters = function (ctx, dt) {
    for (var i = 0; i < this.filterDefs.length; i++) {
      var f = this.filterDefs[i];
      var e = this.filters[f.key];
      var x = checkFinite(this.evalAst(f.input, ctx, null), f.kind + "() input");
      var w = checkFinite(this.evalAst(f.tau, ctx, null), f.kind + "() time constant");
      if (!e) {
        if (f.kind === "ema") { e = this.filters[f.key] = { kind: "ema", value: x }; continue; }
        var n0 = Math.max(1, Math.round(w / this.dt));
        var s0 = new Array(n0);
        for (var q = 0; q < n0; q++) s0[q] = x;
        e = this.filters[f.key] = { kind: "avg", samples: s0, sum: x * n0 };
        continue;
      }
      if (f.kind === "ema") {
        var alpha = w > 0 ? 1 - Math.exp(-dt / w) : 1;   // tau<=0 => instant catch-up
        e.value = checkFinite(e.value + (x - e.value) * alpha, "ema() buffer");
      } else {
        var n = Math.max(1, Math.round(w / this.dt));
        e.samples.push(x); e.sum += x;
        while (e.samples.length > n) e.sum -= e.samples.shift();
        while (e.samples.length < n && e.samples.length) { /* window grew: repeat newest */
          e.samples.unshift(e.samples[0]); e.sum += e.samples[0];
          if (e.samples.length >= n) break;
        }
      }
    }
  };

  E.filtersSnapshot = function () {
    var out = Object.create(null), self = this;
    this.filterDefs.forEach(function (f) {
      var e = self.filters[f.key];
      var cur = !e ? null : (f.kind === "ema" ? e.value : (e.samples.length ? e.sum / e.samples.length : null));
      out[f.key] = {
        kind: f.kind, leaf: f.leaf, uid: f.uid, name: f.name, value: cur,
        samples: e && e.kind === "avg" ? e.samples.length : undefined
      };
    });
    return out;
  };

  E.buildFrame = function (t, yState) {
    var ctx = Object.create(null);
    ctx.t = t;
    ctx.dt = this.dt;

    var tnames = this.ir.order.traits;
    for (var i = 0; i < tnames.length; i++) {
      var tn = tnames[i];
      ctx["traits." + tn] = this.traitValues[tn];
    }
    for (var j = 0; j < this.odeOrder.length; j++) {
      var opath = this.odeOrder[j];
      ctx[opath] = yState[opath];
    }

    var pnames = this.ir.order.params;
    for (var k = 0; k < pnames.length; k++) {
      var pn = pnames[k];
      ctx["params." + pn] = this.userParams[pn] !== undefined ? this.userParams[pn] : this.ir.params[pn].default;
    }
    for (var m = 0; m < this.paramTraitStmts.length; m++) {
      var pst = this.paramTraitStmts[m];
      var tval = this.traitValues[pst.trait];
      if (tval === 0) continue;
      var delta = this.evalAst(pst.ast, ctx, { value: tval });
      ctx[pst.target] = applyAssignOp(pst.op, ctx[pst.target], delta);
    }
    for (var n = 0; n < pnames.length; n++) {
      var pn2 = pnames[n], pmeta = this.ir.params[pn2], pkey = "params." + pn2;
      ctx[pkey] = clampLeaf(ctx[pkey], pmeta);
    }

    for (var v = 0; v < this.viewOrder.length; v++) {
      var vpath = this.viewOrder[v], vleaf = this.ir.leaves[vpath];
      if (vleaf.filters && vleaf.filters.length) {
        // `this` inside a filter leaf's equation = its buffer value at step start
        var fb = this.filters[vleaf.filters[0].name || (vpath + "#" + vleaf.filters[0].uid)];
        ctx[vpath] = fb ? (fb.kind === "ema" ? fb.value : (fb.samples.length ? fb.sum / fb.samples.length : 0)) : 0;
      }
      var val = checkFinite(this.evalAst(vleaf.ast, ctx, null), "view " + vpath);
      var vMods = this.viewTraitStmts[vpath];
      if (vMods) {
        for (var vm = 0; vm < vMods.length; vm++) {
          var vst = vMods[vm], tv = this.traitValues[vst.trait];
          if (tv === 0) continue;
          var vinc = this.evalAst(vst.ast, ctx, { value: tv });
          val = applyAssignOp(vst.op, val, vinc);
        }
      }
      ctx[vpath] = clampLeaf(val, vleaf);
    }
    for (var f2 = 0; f2 < this.filterDefs.length; f2++) {
      var fd = this.filterDefs[f2], fe = this.filters[fd.key];
      ctx["filters#" + fd.key] = fe ? (fd.kind === "ema" ? fe.value : (fe.samples.length ? fe.sum / fe.samples.length : 0)) : 0;
    }
    return ctx;
  };

  E.evalDerivatives = function (t, yState, prebuiltCtx) {
    var ctx = prebuiltCtx || this.buildFrame(t, yState);
    var rates = Object.create(null);
    for (var i = 0; i < this.odeOrder.length; i++) {
      var path = this.odeOrder[i], leaf = this.ir.leaves[path];
      var rate = checkFinite(this.evalAst(leaf.ast, ctx, null), "ode " + path);
      var rMods = this.rateTraitStmts[path];
      if (rMods) {
        for (var rm = 0; rm < rMods.length; rm++) {
          var rst = rMods[rm], tv = this.traitValues[rst.trait];
          if (tv === 0) continue;
          var rinc = this.evalAst(rst.ast, ctx, { value: tv });
          rate = applyAssignOp(rst.op, rate, rinc);
        }
      }
      rates[path] = checkFinite(rate, "ode rate " + path);
    }
    return rates;
  };

  E.step = function (preCtx) {
    var t = this.simTime, dt = this.dt, y0 = this.state, odes = this.odeOrder, n = odes.length;
    var ctx = preCtx || this.buildFrame(t, y0);
    var k1 = this.evalDerivatives(t, y0, ctx);
    var nextY = Object.create(null);

    if (this.solver === "euler") {
      for (var i = 0; i < n; i++) {
        var p = odes[i];
        nextY[p] = clampLeaf(y0[p] + dt * k1[p], this.ir.leaves[p]);
      }
    } else if (this.solver === "heun") {
      var yPred = Object.create(null);
      for (var i2 = 0; i2 < n; i2++) {
        var p2 = odes[i2];
        yPred[p2] = clampLeaf(y0[p2] + dt * k1[p2], this.ir.leaves[p2]);
      }
      var k2 = this.evalDerivatives(t + dt, yPred);
      for (var j2 = 0; j2 < n; j2++) {
        var pk2 = odes[j2];
        nextY[pk2] = clampLeaf(y0[pk2] + 0.5 * dt * (k1[pk2] + k2[pk2]), this.ir.leaves[pk2]);
      }
    } else {
      var yHalf1 = Object.create(null);
      for (var a = 0; a < n; a++) {
        var pa = odes[a];
        yHalf1[pa] = clampLeaf(y0[pa] + 0.5 * dt * k1[pa], this.ir.leaves[pa]);
      }
      var k2r = this.evalDerivatives(t + 0.5 * dt, yHalf1);

      var yHalf2 = Object.create(null);
      for (var b = 0; b < n; b++) {
        var pb = odes[b];
        yHalf2[pb] = clampLeaf(y0[pb] + 0.5 * dt * k2r[pb], this.ir.leaves[pb]);
      }
      var k3r = this.evalDerivatives(t + 0.5 * dt, yHalf2);

      var yFull = Object.create(null);
      for (var c = 0; c < n; c++) {
        var pc = odes[c];
        yFull[pc] = clampLeaf(y0[pc] + dt * k3r[pc], this.ir.leaves[pc]);
      }
      var k4r = this.evalDerivatives(t + dt, yFull);

      for (var d = 0; d < n; d++) {
        var pd = odes[d];
        var rkRate = (k1[pd] + 2 * k2r[pd] + 2 * k3r[pd] + k4r[pd]) / 6;
        nextY[pd] = clampLeaf(y0[pd] + dt * rkRate, this.ir.leaves[pd]);
      }
    }

    this.state = nextY;
    this.simTime = Math.round((t + dt) * 1e9) / 1e9;
    this.lastRates = k1;
    if (this.filterDefs.length) this.advanceFilters(ctx, dt);
    return this.state;
  };

  E.advance = function (minutes) {
    var m = minutes === undefined ? this.dt : Number(minutes);
    if (!(m > 0) || !isFinite(m)) {
      throw new ModelError("advance(minutes) requires a positive finite number", { code: "bad_value" });
    }
    var nSteps = Math.max(1, Math.round(m / this.dt));
    if (nSteps > 200000) throw new ModelError("advance exceeded 200,000 steps", { code: "runtime_error" });
    for (var i = 0; i < nSteps; i++) this.step();
    return nSteps;
  };

  E.snapshot = function () {
    var ctx = this.buildFrame(this.simTime, this.state);
    this.lastRates = this.evalDerivatives(this.simTime, this.state, ctx);
    return ctx;
  };

  // Direct sensor / state setter for external telemetry feeds (weather, solar, CPU/GPU, presence)
  // setState/nudge on a filter view leaf writes its hidden buffer (leaf must own
  // exactly one filter site so the target buffer is unambiguous).
  E.filterLeafDef = function (target) {
    var lf = this.ir.leaves[target];
    if (!lf || lf.kind !== "view" || !(lf.filters || []).length) return null;
    if (lf.filters.length !== 1) return null;
    return this.filterByKey[lf.filters[0].name || (target + "#" + lf.filters[0].uid)] || null;
  };

  E.setState = function (target, value) {
    var lf = this.ir.leaves[target];
    if (!lf) throw new ModelError("unknown state leaf " + quote(target), { code: "bad_target" });
    if (lf.kind !== "ode") {
      var fdef = this.filterLeafDef(target);
      if (!fdef) throw new ModelError("cannot set algebraic view " + quote(target) + "; set an ODE/sensor leaf instead", { code: "bad_target" });
      var fv = checkFinite(Number(value), "setState " + target);
      var e = this.filters[fdef.key];
      if (!e) {
        if (fdef.kind === "ema") e = this.filters[fdef.key] = { kind: "ema", value: fv };
        else {
          var n0 = Math.max(1, Math.round(checkFinite(this.evalAst(fdef.tau, this.buildFrame(this.simTime, this.state)), "avg() window") / this.dt));
          var s0 = new Array(n0); for (var q = 0; q < n0; q++) s0[q] = fv;
          e = this.filters[fdef.key] = { kind: "avg", samples: s0, sum: fv * n0 };
        }
      }
      if (e.kind === "ema") e.value = clampLeaf(fv, lf);
      else { for (var m = 0; m < e.samples.length; m++) e.samples[m] = fv; e.sum = fv * e.samples.length; }
      return clampLeaf(fv, lf);
    }
    var v = checkFinite(Number(value), "setState " + target);
    this.state[target] = clampLeaf(v, lf);
    return this.state[target];
  };

  E.nudge = function (target, delta, expr) {
    var lf = this.ir.leaves[target];
    if (!lf) throw new ModelError("unknown state leaf " + quote(target), { code: "bad_target" });
    if (lf.kind !== "ode") {
      var fdef = this.filterLeafDef(target);
      if (!fdef) throw new ModelError("cannot nudge algebraic view " + quote(target) + "; nudge an ODE leaf instead", { code: "bad_target" });
      var e = this.filters[fdef.key];
      if (!e) { this.snapshot(); e = this.filters[fdef.key]; }  // boot from live signal first
      if (!e) throw new ModelError("filter buffer not initialized for " + quote(target), { code: "bad_target" });
      var cur = e.kind === "ema" ? e.value : (e.samples.length ? e.sum / e.samples.length : 0);
      var d = Number(delta), nv;
      if (lf.min !== null && lf.max !== null) {
        var span = lf.max - lf.min;
        var room = d >= 0 ? (lf.max - cur) / span : (cur - lf.min) / span;
        nv = cur + d * Math.max(0.15, room);
      } else nv = cur + d;
      checkFinite(nv, "nudge " + target);
      nv = clampLeaf(nv, lf);
      if (e.kind === "ema") e.value = nv;
      else { for (var m2 = 0; m2 < e.samples.length; m2++) e.samples[m2] = nv; e.sum = nv * e.samples.length; }
      return nv;
    }
    var nextVal;
    if (expr !== undefined && expr !== null) {
      var ctx = this.snapshot();
      var ast = tagAstKeys(parseExpr(String(expr), "<nudge>", 1));
      nextVal = this.evalAst(ast, ctx, { current: this.state[target] });
    } else {
      var cur = this.state[target], d = Number(delta);
      if (lf.min !== null && lf.max !== null) {
        var span = lf.max - lf.min;
        var room = d >= 0 ? (lf.max - cur) / span : (cur - lf.min) / span;
        nextVal = cur + d * Math.max(0.15, room);
      } else {
        nextVal = cur + d;
      }
    }
    checkFinite(nextVal, "nudge " + target);
    this.state[target] = clampLeaf(nextVal, lf);
    return this.state[target];
  };

  E.setTrait = function (name, value) {
    var tr = this.ir.traits[name];
    if (!tr) throw new ModelError("unknown trait " + quote(name), { code: "unknown_ref" });
    this.traitValues[name] = clampLeaf(checkFinite(Number(value), "trait " + name), tr);
  };

  E.setParam = function (name, value) {
    var pr = this.ir.params[name];
    if (!pr) throw new ModelError("unknown param " + quote(name), { code: "unknown_ref" });
    this.userParams[name] = clampLeaf(checkFinite(Number(value), "param " + name), pr);
  };

  E.exportPersona = function () {
    var tr = Object.create(null), up = Object.create(null), self = this;
    Object.keys(this.traitValues).forEach(function (k) { tr[k] = self.traitValues[k]; });
    Object.keys(this.userParams).forEach(function (k) { up[k] = self.userParams[k]; });
    return { schema: "mind-hermes-persona-v4", traits: tr, params: up };
  };

  E.importPersona = function (blob) {
    if (!blob || typeof blob !== "object") return;
    var self = this;
    var traitsObj = (blob.traits && typeof blob.traits === "object") ? blob.traits : (!blob.params && !blob.state ? blob : null);
    if (traitsObj) {
      Object.keys(traitsObj).forEach(function (k) {
        if (self.ir.traits[k]) self.setTrait(k, traitsObj[k]);
      });
    }
    if (blob.params && typeof blob.params === "object") {
      Object.keys(blob.params).forEach(function (k) {
        if (self.ir.params[k]) self.setParam(k, blob.params[k]);
      });
    }
  };

  E.exportState = function () {
    var st = Object.create(null), tr = Object.create(null), up = Object.create(null), fl = Object.create(null), self = this;
    this.odeOrder.forEach(function (k) { st[k] = self.state[k]; });
    Object.keys(this.traitValues).forEach(function (k) { tr[k] = self.traitValues[k]; });
    Object.keys(this.userParams).forEach(function (k) { up[k] = self.userParams[k]; });
    this.filterDefs.forEach(function (f) {
      var e = self.filters[f.key];
      if (!e) return;
      fl[f.key] = f.kind === "ema" ? e.value : { samples: e.samples.slice(), sum: e.sum };
    });
    return {
      schema: "mind-hermes-state-v4",
      sim_time: this.simTime,
      solver: this.solver,
      dt: this.dt,
      state: st,
      traits: tr,
      params: up,
      filters: fl
    };
  };

  E.importState = function (blob) {
    if (!blob || typeof blob !== "object") return;
    this.reset();
    var self = this;
    if (typeof blob.sim_time === "number" && isFinite(blob.sim_time)) this.simTime = blob.sim_time;
    if (blob.solver && SOLVER_NAMES.indexOf(blob.solver) >= 0) this.solver = blob.solver;
    var stateMap = (blob.state && typeof blob.state === "object") ? blob.state : (blob.initial && typeof blob.initial === "object" ? blob.initial : null);
    if (stateMap) {
      Object.keys(stateMap).forEach(function (k) {
        var lf = self.ir.leaves[k];
        if (!lf) throw new ModelError("import: unknown leaf " + quote(k), { code: "unknown_ref" });
        if (lf.kind !== "ode") throw new ModelError("import: " + quote(k) + " is a view", { code: "bad_value" });
        self.state[k] = clampLeaf(checkFinite(Number(stateMap[k]), k), lf);
      });
    }
    if (blob.traits) Object.keys(blob.traits).forEach(function (k) { if (self.ir.traits[k]) self.setTrait(k, blob.traits[k]); });
    if (blob.params) Object.keys(blob.params).forEach(function (k) { if (self.ir.params[k]) self.setParam(k, blob.params[k]); });
    // Hidden filter buffers: backward compatible (old blobs have no `filters` — buffers
    // then boot-init at the first step) and forward tolerant (unknown/stale keys ignored).
    // Pre-name blobs keyed by path#uid migrate gracefully into named buffers via the
    // legacy alias map (values land under the def's CURRENT key, name or uid).
    if (blob.filters && typeof blob.filters === "object") {
      Object.keys(blob.filters).forEach(function (k) {
        var def = self.filterByKey[k] || self.filterLegacyKeys[k];
        if (!def) return;
        var v = blob.filters[k];
        if (def.kind === "ema") {
          if (typeof v === "number" && isFinite(v)) self.filters[def.key] = { kind: "ema", value: v };
        } else if (v && typeof v === "object" && Array.isArray(v.samples)) {
          // explicit `sum` carried in the blob so round-trips are bit-exact
          var s = [];
          var sum = 0;
          for (var i = 0; i < v.samples.length; i++) {
            if (typeof v.samples[i] === "number" && isFinite(v.samples[i])) s.push(v.samples[i]);
          }
          if (!s.length) return;
          var rs = typeof v.sum === "number" && isFinite(v.sum) ? v.sum : s.reduce(function (a, b) { return a + b; }, 0);
          self.filters[def.key] = { kind: "avg", samples: s, sum: rs };
        } else if (Array.isArray(v)) {
          var s2 = [];
          var sum2 = 0;
          for (var j = 0; j < v.length; j++) {
            if (typeof v[j] === "number" && isFinite(v[j])) { s2.push(v[j]); sum2 += v[j]; }
          }
          if (s2.length) self.filters[def.key] = { kind: "avg", samples: s2, sum: sum2 };
        }
      });
    }
  };

  function fmtNumber(v, decimals) {
    var s = Number(v).toFixed(decimals === undefined ? 2 : decimals);
    return /^-0(\.0*)?$/.test(s) ? s.slice(1) : s;
  }

  function matchBand(v, bands) {
    if (!bands) return null;
    for (var i = 0; i < bands.length; i++) {
      var lo = bands[i][0], hi = bands[i][1], word = bands[i][2];
      if ((v >= lo && v < hi) || (i === bands.length - 1 && v === hi)) return word;
    }
    return null;
  }

  function Renderer(engine) { this.engine = engine; }
  Renderer.prototype.render = function (name, snapOverride, opts) {
    var rn = this.engine.ir.renders[name];
    if (!rn) throw new ModelError("unknown render block " + quote(name), { code: "unknown_ref" });
    var ctx = snapOverride || this.engine.snapshot();
    if (ctx.t === undefined) ctx.t = this.engine.simTime;
    if (ctx.dt === undefined) ctx.dt = this.engine.dt;
    if (rn.when && rn.when.ast) {
      if (this.engine.evalAst(rn.when.ast, ctx, null) === 0) return null;
    }
    var self = this;
    function interpolate(str) {
      if (!str) return "";
      var s = str;
      rn.fields.forEach(function (fld) {
        if (s.indexOf("{" + fld + "}") < 0) return;
        var v = ctx[fld];
        var fcfg = rn.format[fld] || {};
        var text = fcfg.bands ? matchBand(v, fcfg.bands) : null;
        if (text === null) text = fmtNumber(v, fcfg.decimals !== undefined ? fcfg.decimals : 2);
        s = s.split("{" + fld + "}").join(text);
      });
      return s;
    }
    var parts = [];
    if (rn.header) parts.push(interpolate(rn.header));
    if (rn.template) parts.push(interpolate(rn.template));
    if (rn.clauses && rn.clauses.length) {
      var minPrio = (opts && opts.min_priority !== undefined) ? Number(opts.min_priority)
        : (rn.min_priority_expr && rn.min_priority_expr.ast ? Number(self.engine.evalAst(rn.min_priority_expr.ast, ctx, null)) : rn.min_priority);
      var maxCl = (opts && opts.max_clauses !== undefined) ? Number(opts.max_clauses)
        : (rn.max_clauses_expr && rn.max_clauses_expr.ast ? Math.max(0, Math.round(self.engine.evalAst(rn.max_clauses_expr.ast, ctx, null))) : rn.max_clauses);
      var active = [];
      rn.clauses.forEach(function (cl, idx) {
        if (cl.when_ast && self.engine.evalAst(cl.when_ast, ctx, null) === 0) return;
        var prio = Number(self.engine.evalAst(cl.priority_ast, ctx, null));
        if (prio >= minPrio) active.push({ prio: prio, idx: idx, text: interpolate(cl.text) });
      });
      active.sort(function (a, b) { return b.prio - a.prio || a.idx - b.idx; });
      var picked = active.slice(0, maxCl).map(function (x) { return x.text; });
      if (picked.length) parts.push(picked.join(rn.join || " "));
    }
    if (rn.footer) parts.push(interpolate(rn.footer));
    return parts.join(rn.join || " ").trim();
  };

  Renderer.prototype.renderAll = function (snapOverride) {
    var ctx = snapOverride || this.engine.snapshot();
    var out = Object.create(null), self = this;
    this.engine.ir.order.renders.forEach(function (rname) {
      var rn = self.engine.ir.renders[rname];
      if (!rn.visible) return;
      var s = self.render(rname, ctx);
      if (s !== null) out[rname] = s;
    });
    return out;
  };

  function Mind(sourceOrIr) {
    this.ir = (typeof sourceOrIr === "string" || Array.isArray(sourceOrIr))
      ? buildIr(sourceOrIr)
      : sourceOrIr;
    this.engine = new Engine(this.ir);
    this.renderer = new Renderer(this.engine);
  }
  Mind.prototype.step = function (preCtx) { return this.engine.step(preCtx); };
  Mind.prototype.advance = function (m) { return this.engine.advance(m); };
  Mind.prototype.snapshot = function () { return this.engine.snapshot(); };
  Mind.prototype.setState = function (t, v) { return this.engine.setState(t, v); };
  Mind.prototype.nudge = function (t, d, e) { return this.engine.nudge(t, d, e); };
  Mind.prototype.setTrait = function (n, v) { return this.engine.setTrait(n, v); };
  Mind.prototype.setParam = function (n, v) { return this.engine.setParam(n, v); };
  Mind.prototype.render = function (n, c) { return this.renderer.render(n, c); };
  Mind.prototype.renderAll = function (c) { return this.renderer.renderAll(c); };
  Mind.prototype.reset = function () { return this.engine.reset(); };
  Mind.prototype.exportPersona = function () { return this.engine.exportPersona(); };
  Mind.prototype.importPersona = function (b) { return this.engine.importPersona(b); };
  Mind.prototype.exportState = function () { return this.engine.exportState(); };
  Mind.prototype.importState = function (b) { return this.engine.importState(b); };
  Mind.prototype.filtersSnapshot = function () { return this.engine.filtersSnapshot(); };

  return {
    IR_VERSION: IR_VERSION, ModelError: ModelError, MATH_SIGNATURES: MATH_SIGNATURES,
    FILTER_SIGNATURES: FILTER_SIGNATURES,
    MATH_IMPL: MATH_IMPL, CONSTANTS: CONSTANTS, SOLVER_NAMES: SOLVER_NAMES,
    tokenize: tokenize, parseExpr: parseExpr, buildIr: buildIr,
    Engine: Engine, Renderer: Renderer, Mind: Mind,
    fmtNumber: fmtNumber, matchBand: matchBand
  };
});
