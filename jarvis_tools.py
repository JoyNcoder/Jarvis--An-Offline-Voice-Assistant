"""
jarvis_tools.py - offline tools for Jarvis.
 
Steps covered here: time/date, unit conversion, calculator.
Pure Python (standard library only), no mic or models needed, so it can be
tested on its own:   python jarvis_tools.py
 
Main entry point:
    quick_answer(text) -> spoken answer string, or None if the sentence is not
                          a time/date, unit or math request.
 
Every answer is written to be read aloud by Kokoro (no symbols, no leading
zeros in times, numbers grouped only above 9,999).
 
Functions for the Qwen tool-calling path (used by the router in a later step):
    tool_get_datetime(args), tool_calculate(args), tool_convert_units(args)
"""
 
import ast
import calendar
import json
import math
import operator
import re
from datetime import datetime, timedelta
from decimal import Decimal
 
 
# ===============================================================
# 1. Spoken numbers and number formatting (shared by everything)
# ===============================================================
 
_UNITS_W = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS_W = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
            "nineteen": 19}
_TENS_W = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
           "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES_W = {"thousand": 10**3, "million": 10**6, "billion": 10**9}
 
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z]+(?:'[a-z]+)?|\S")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
 
 
def _read_number(tokens, i):
    """Read a spoken whole number starting at tokens[i] -> (value, next_i) or None."""
    n = len(tokens)
    j = i
    total = current = 0
    last = None            # unit / teen / tens / hundred / scale
    started = False
 
    if tokens[j] in ("a", "an") and j + 1 < n and tokens[j + 1] in (
            "hundred", "thousand", "million", "billion"):
        current, last, started = 1, "unit", True
        j += 1
 
    while j < n:
        t = tokens[j]
        if t in _UNITS_W and last in (None, "tens", "hundred", "scale"):
            current += _UNITS_W[t]
            last = "unit"
        elif t in _TEENS_W and last in (None, "hundred", "scale"):
            current += _TEENS_W[t]
            last = "teen"
        elif t in _TENS_W and last in (None, "hundred", "scale"):
            current += _TENS_W[t]
            last = "tens"
        elif t == "hundred" and last in ("unit", "teen", "tens") and 0 < current < 100:
            current *= 100
            last = "hundred"
        elif t in _SCALES_W and last in ("unit", "teen", "tens", "hundred"):
            total += current * _SCALES_W[t]
            current = 0
            last = "scale"
        elif (t == "and" and last in ("hundred", "scale") and j + 1 < n and
              (tokens[j + 1] in _UNITS_W or tokens[j + 1] in _TEENS_W
               or tokens[j + 1] in _TENS_W)):
            j += 1                  # "one hundred AND five"
            continue
        else:
            break
        started = True
        j += 1
 
    return (total + current, j) if started else None
 
 
def _read_decimal(tokens, j):
    """tokens[j] == 'point'. Read the digits after it -> (digits, next_i) or None."""
    n = len(tokens)
    k = j + 1
    digits = ""
    while k < n:
        t = tokens[k]
        if t in _UNITS_W:
            digits += str(_UNITS_W[t])
            k += 1
        elif re.fullmatch(r"\d+", t):
            digits += t
            k += 1
            break
        else:
            break
    return (digits, k) if digits else None
 
 
def words_to_digits(text):
    """'two hundred and five point five' -> '205.5'. Other words are left alone."""
    text = text.lower()
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)        # 1,000 -> 1000
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)           # twenty-five
    tokens = _TOKEN_RE.findall(text)
    out, i, n = [], 0, len(tokens)
 
    while i < n:
        t = tokens[i]
        if _NUM_RE.fullmatch(t):
            value = t
            i += 1
            if i < n and tokens[i] == "point" and "." not in value:
                dec = _read_decimal(tokens, i)
                if dec:
                    value, i = f"{value}.{dec[0]}", dec[1]
            while i < n and tokens[i] in ("hundred", *_SCALES_W):   # "1.5 million"
                scale = 100 if tokens[i] == "hundred" else _SCALES_W[tokens[i]]
                scaled = float(value) * scale
                value = str(int(scaled)) if scaled.is_integer() else repr(scaled)
                i += 1
            out.append(value)
            continue
 
        read = _read_number(tokens, i)
        if read:
            value, i = read
            value = str(value)
            if i < n and tokens[i] == "point":
                dec = _read_decimal(tokens, i)
                if dec:
                    value, i = f"{value}.{dec[0]}", dec[1]
            out.append(value)
            continue
 
        if t == "point":
            dec = _read_decimal(tokens, i)
            if dec:
                out.append("0." + dec[0])
                i = dec[1]
                continue
 
        out.append(t)
        i += 1
 
    return " ".join(out)
 
 
def _sci(a):
    """Spoken scientific notation for very large or very small positive numbers."""
    if isinstance(a, int):
        digits = str(a)
        exponent = len(digits) - 1
        mantissa = digits[0] + "." + digits[1:3]
    else:
        mantissa, exp = f"{a:.2e}".split("e")
        exponent = int(exp)
    mantissa = mantissa.rstrip("0").rstrip(".")
    sign = "minus " if exponent < 0 else ""
    return f"{mantissa} times ten to the power of {sign}{abs(exponent)}"
 
 
def fmt_num(x):
    """Format a number for speech: whole when whole, 2 decimals, 3 significant if tiny."""
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            return str(x)
        if abs(x) < 1e15 and abs(x - round(x)) < 1e-9 * max(1.0, abs(x)):
            x = int(round(x))
    sign = "minus " if x < 0 else ""
    a = abs(x)
 
    if isinstance(a, int):
        if a >= 10**15:
            return sign + _sci(a)
        return sign + (f"{a:,}" if a >= 10000 else str(a))
 
    if a >= 1e15 or a < 1e-6:
        return sign + _sci(a)
    if a >= 1:
        s = f"{a:,.2f}" if a >= 10000 else f"{a:.2f}"
        s = s.rstrip("0").rstrip(".")
    else:
        s = format(Decimal(f"{a:.3g}"), "f")
    return sign + s
 
 
# ===============================================================
# 2. Calculator
# ===============================================================
 
class _Unsupported(Exception):
    """The text is not a valid arithmetic expression."""
 
 
def _guard(v):
    if isinstance(v, complex):
        raise ValueError("not real")
    if isinstance(v, int) and v.bit_length() > 3300:
        raise OverflowError("too big")
    if isinstance(v, float) and not math.isfinite(v):
        raise OverflowError("too big")
    return v
 
 
def _pow(a, b):
    if isinstance(a, int) and isinstance(b, int) and b > 0:
        if b > 1000 or (a not in (0, 1, -1) and a.bit_length() * b > 3300):
            raise OverflowError("too big")
    return a ** b
 
 
def _factorial(n):
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if not isinstance(n, int) or n < 0 or n > 170:
        raise ValueError("factorial range")
    return math.factorial(n)
 
 
def _cbrt(x):
    return math.copysign(abs(x) ** (1 / 3), x)
 
 
_BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod}
_FUNCS = {"sqrt": math.sqrt, "cbrt": _cbrt, "abs": abs, "round": round,
          "floor": math.floor, "ceil": math.ceil, "factorial": _factorial,
          "log": math.log, "ln": math.log, "log10": math.log10, "log2": math.log2,
          "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan}
_CONSTS = {"pi": math.pi, "e": math.e}
 
 
def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            return _guard(_pow(left, right))
        if type(node.op) in _BIN_OPS:
            return _guard(_BIN_OPS[type(node.op)](left, right))
        raise _Unsupported
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS and not node.keywords and 1 <= len(node.args) <= 2):
        return _guard(_FUNCS[node.func.id](*[_eval(a) for a in node.args]))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise _Unsupported
 
 
def _evaluate(expr):
    """-> (value, None) on success, (None, spoken_error) for a math problem.
    Raises _Unsupported if `expr` is not arithmetic at all."""
    if not expr or len(expr) > 200:
        raise _Unsupported
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise _Unsupported
    try:
        return _eval(tree), None
    except ZeroDivisionError:
        return None, "You can't divide by zero."
    except OverflowError:
        return None, "That number is too big for me."
    except (ValueError, TypeError):
        return None, "That doesn't have a real answer."
    except RecursionError:
        raise _Unsupported
 
 
_NUM = r"(\d+(?:\.\d+)?)"
 
# Applied in this order to turn spoken math into a Python expression.
_MATH_SUBS = [
    (rf"{_NUM}\s*(?:%|percent|per cent)\s+off\s+(?:of\s+|on\s+|from\s+)?{_NUM}", r"(\2*(1-\1/100))"),
    (rf"{_NUM}\s*(?:%|percent|per cent)\s+of\s+", r"(\1/100)*"),
    (rf"\bsquare root of\s+(?:-|minus|negative)\s*{_NUM}", r"sqrt(-\1)"),
    (rf"\bsquare root of\s+{_NUM}", r"sqrt(\1)"),
    (rf"\bcube root of\s+{_NUM}", r"cbrt(\1)"),
    (r"\bfactorial of\s+(\d+)", r"factorial(\1)"),
    (r"(\d+)\s+factorial\b", r"factorial(\1)"),
    (rf"{_NUM}\s+squared\b", r"(\1)**2"),
    (rf"{_NUM}\s+cubed\b", r"(\1)**3"),
    (rf"\bsquare of\s+{_NUM}", r"(\1)**2"),
    (r"\bhalf of\b", "0.5 *"),
    (r"\bquarter of\b", "0.25 *"),
    (r"\b(?:double|twice)\s+(?=\d)", "2 * "),
    (r"\btriple\s+(?=\d)", "3 * "),
    (r"\bto the (\d+)\s*(?:st|nd|rd|th)?\s+power\b", r"** \1"),
    (r"\b(?:raised to the power of|raised to the|raised to|to the power of)\b", "**"),
    (rf"\bmultiply\s+{_NUM}\s+by\b", r"\1 *"),
    (rf"\bdivide\s+{_NUM}\s+by\b", r"\1 /"),
    (rf"\badd\s+{_NUM}\s+(?:and|to)\s+", r"\1 + "),
    (rf"\bsubtract\s+{_NUM}\s+from\s+{_NUM}", r"\2 - \1"),
    (r"\bremainder (?:of|when)\s+(\d+)\s+(?:is\s+)?divided by\s+(\d+)", r"\1 % \2"),
    (r"\b(?:multiplied by|multiply by|times)\b", "*"),
    (r"\b(?:divided by|divided into|over)\b", "/"),
    (r"\b(?:plus|added to)\b", "+"),
    (r"\b(?:minus|take away)\b", "-"),
    (r"\b(?:modulo|mod)\b", "%"),
    (r"\bnegative\s+(?=[\d(])", "-"),
]
_MATH_SUBS = [(re.compile(p), r) for p, r in _MATH_SUBS]
 
_MATH_FILLER = {
    "what", "is", "are", "whats", "how", "much", "calculate", "compute", "work",
    "out", "figure", "solve", "tell", "me", "please", "hey", "hi", "ok", "okay",
    "jarvis", "can", "you", "could", "would", "the", "result", "answer", "equals",
    "equal", "it", "give", "find", "evaluate", "simplify", "do", "sum", "total",
}
_MATH_WORDS = {"sqrt", "cbrt", "factorial", "pi"}
 
 
def _spoken_to_expression(text):
    """Turn a spoken sentence into a Python expression, or None if it isn't math."""
    q = text.lower().replace("’", "'").replace("×", "*").replace("÷", "/")
    q = q.replace("−", "-").replace("–", "-").replace("^", " ** ")
    q = re.sub(r"\bwhat'?s\b", "what is", q)
    q = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", q)               # 5 x 3
    q = words_to_digits(q)
    q = re.sub(r"[?!,=]", " ", q)
    q = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", q)
 
    for rx, repl in _MATH_SUBS:
        q = rx.sub(repl, q)
 
    tokens = re.findall(r"\d+(?:\.\d+)?|\*\*|[a-z]+|\S", q)
    kept = []
    for t in tokens:
        if t[0].isalpha():
            if t in _MATH_FILLER:
                continue
            if t not in _MATH_WORDS:
                return None                    # a real word: not a math sentence
        elif not (t[0].isdigit() or t in {"+", "-", "*", "/", "%", "(", ")", "**"}):
            return None
        kept.append(t)
 
    expr = " ".join(kept)
    if not re.search(r"\d|pi", expr):
        return None
    if not re.search(r"[+\-*/%]|sqrt|cbrt|factorial", expr):
        return None                            # a lone number is not a calculation
    if re.fullmatch(r"-\s*\d+(?:\.\d+)?", expr):
        return None
    return expr
 
 
def calc_answer(text):
    expr = _spoken_to_expression(text)
    if expr is None:
        return None
    try:
        value, error = _evaluate(expr)
    except _Unsupported:
        return None
    print(f"[Calc] {expr}")
    return error if error else f"The answer is {fmt_num(value)}."
 
 
def tool_calculate(args):
    """For Qwen tool calling: evaluate an expression string such as '15/100*80'."""
    expr = str(args.get("expression", "")).strip().lower()
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
    expr = expr.rstrip(" =?.")
    try:
        value, error = _evaluate(expr)
    except _Unsupported:
        # Qwen sometimes sends words ("15% of 80") instead of an expression.
        alt = _spoken_to_expression(expr)
        try:
            value, error = _evaluate(alt) if alt else (None, None)
        except _Unsupported:
            alt = None
        if not alt:
            return "I couldn't understand that expression."
        expr = alt
    return error if error else f"{expr} = {fmt_num(value)}"
 
 
# ===============================================================
# 3. Unit conversion
# ===============================================================
 
_UNIT_TABLE = {}      # alias -> (category, singular, plural, factor to base unit)
 
 
def _add_unit(category, singular, plural, factor, *aliases):
    entry = (category, singular, plural, factor)
    for alias in {singular, plural, *aliases}:
        _UNIT_TABLE[alias.lower()] = entry
 
 
# --- length (base: meter) ---
_add_unit("length", "millimeter", "millimeters", 0.001, "mm", "millimetre", "millimetres")
_add_unit("length", "centimeter", "centimeters", 0.01, "cm", "centimetre", "centimetres")
_add_unit("length", "meter", "meters", 1, "m", "metre", "metres")
_add_unit("length", "kilometer", "kilometers", 1000, "km", "kms", "kilometre", "kilometres")
_add_unit("length", "inch", "inches", 0.0254, "in")
_add_unit("length", "foot", "feet", 0.3048, "ft")
_add_unit("length", "yard", "yards", 0.9144, "yd", "yds")
_add_unit("length", "mile", "miles", 1609.344, "mi")
_add_unit("length", "nautical mile", "nautical miles", 1852, "nmi")
# --- mass (base: kilogram) ---
_add_unit("mass", "milligram", "milligrams", 1e-6, "mg")
_add_unit("mass", "gram", "grams", 0.001, "g", "gm", "gms")
_add_unit("mass", "kilogram", "kilograms", 1, "kg", "kgs", "kilo", "kilos")
_add_unit("mass", "metric ton", "metric tons", 1000, "tonne", "tonnes", "ton", "tons")
_add_unit("mass", "pound", "pounds", 0.45359237, "lb", "lbs")
_add_unit("mass", "ounce", "ounces", 0.028349523125, "oz")
_add_unit("mass", "stone", "stone", 6.35029318, "st")
# --- volume (base: liter; gallon, quart, pint, cup are US sizes) ---
_add_unit("volume", "milliliter", "milliliters", 0.001, "ml", "millilitre", "millilitres")
_add_unit("volume", "liter", "liters", 1, "l", "litre", "litres", "ltr")
_add_unit("volume", "cubic meter", "cubic meters", 1000, "m3", "cubic metre", "cubic metres")
_add_unit("volume", "teaspoon", "teaspoons", 0.00492892159375, "tsp")
_add_unit("volume", "tablespoon", "tablespoons", 0.01478676478125, "tbsp")
_add_unit("volume", "fluid ounce", "fluid ounces", 0.0295735295625, "fl oz", "floz")
_add_unit("volume", "cup", "cups", 0.2365882365)
_add_unit("volume", "pint", "pints", 0.473176473, "pt")
_add_unit("volume", "quart", "quarts", 0.946352946, "qt")
_add_unit("volume", "gallon", "gallons", 3.785411784, "gal")
# --- time (base: second; a year is 365 days, a month is a twelfth of that) ---
_add_unit("time", "millisecond", "milliseconds", 0.001, "ms")
_add_unit("time", "second", "seconds", 1, "s", "sec", "secs")
_add_unit("time", "minute", "minutes", 60, "min", "mins")
_add_unit("time", "hour", "hours", 3600, "h", "hr", "hrs")
_add_unit("time", "day", "days", 86400)
_add_unit("time", "week", "weeks", 604800)
_add_unit("time", "month", "months", 365 * 86400 / 12)
_add_unit("time", "year", "years", 365 * 86400, "yr", "yrs")
# --- speed (base: meter per second) ---
_add_unit("speed", "meter per second", "meters per second", 1, "m/s", "mps",
          "metre per second", "metres per second", "meters a second")
_add_unit("speed", "kilometer per hour", "kilometers per hour", 1 / 3.6, "km/h", "kph",
          "kmh", "kmph", "kilometre per hour", "kilometres per hour", "kilometers an hour")
_add_unit("speed", "mile per hour", "miles per hour", 0.44704, "mph", "miles an hour")
_add_unit("speed", "knot", "knots", 1852 / 3600, "kn", "kt", "kts")
_add_unit("speed", "foot per second", "feet per second", 0.3048, "ft/s", "fps")
# --- area (base: square meter) ---
_add_unit("area", "square meter", "square meters", 1, "sq m", "m2", "square metre", "square metres")
_add_unit("area", "square kilometer", "square kilometers", 1e6, "sq km", "km2",
          "square kilometre", "square kilometres")
_add_unit("area", "square centimeter", "square centimeters", 1e-4, "sq cm", "cm2",
          "square centimetre", "square centimetres")
_add_unit("area", "hectare", "hectares", 10000, "ha")
_add_unit("area", "acre", "acres", 4046.8564224)
_add_unit("area", "square foot", "square feet", 0.09290304, "sq ft", "ft2")
_add_unit("area", "square inch", "square inches", 0.00064516, "sq in")
_add_unit("area", "square yard", "square yards", 0.83612736, "sq yd")
_add_unit("area", "square mile", "square miles", 2589988.110336, "sq mi")
# --- digital data (base: byte; 1 kilobyte = 1024 bytes, like Windows) ---
_add_unit("data", "bit", "bits", 0.125)
_add_unit("data", "byte", "bytes", 1)
_add_unit("data", "kilobyte", "kilobytes", 1024, "kb")
_add_unit("data", "megabyte", "megabytes", 1024**2, "mb")
_add_unit("data", "gigabyte", "gigabytes", 1024**3, "gb")
_add_unit("data", "terabyte", "terabytes", 1024**4, "tb")
# --- temperature (special formulas, no factor) ---
_add_unit("temperature", "degree Celsius", "degrees Celsius", None, "celsius", "c", "centigrade")
_add_unit("temperature", "degree Fahrenheit", "degrees Fahrenheit", None, "fahrenheit", "f")
_add_unit("temperature", "kelvin", "kelvin", None, "k", "kelvins")
 
 
def _find_unit(phrase):
    p = re.sub(r"°|\b(?:degrees?|deg|us|the)\b", " ", phrase.lower())
    p = re.sub(r"\s+", " ", p).strip(" '\"")
    candidates = [p]
    if p.endswith("s"):
        candidates.append(p[:-1])
    if p.endswith("es"):
        candidates.append(p[:-2])
    for c in candidates:
        if c in _UNIT_TABLE:
            return _UNIT_TABLE[c]
    return None
 
 
def _to_celsius(v, name):
    return {"degree Celsius": v, "degree Fahrenheit": (v - 32) * 5 / 9,
            "kelvin": v - 273.15}[name]
 
 
def _from_celsius(c, name):
    return {"degree Celsius": c, "degree Fahrenheit": c * 9 / 5 + 32,
            "kelvin": c + 273.15}[name]
 
 
def _unit_label(unit, value):
    """Singular only when the spoken number is exactly 'one'."""
    return unit[1] if fmt_num(value) == "1" else unit[2]
 
 
def _convert(value, from_phrase, to_phrase, left_label=None):
    """-> (spoken_text, None) or (None, error). error is a spoken sentence."""
    u1, u2 = _find_unit(from_phrase), _find_unit(to_phrase)
    if u1 is None or u2 is None:
        bad = from_phrase if u1 is None else to_phrase
        return None, f"I don't know the unit {bad.strip()}."
    if u1[0] != u2[0]:
        return None, f"I can't convert {u1[2]} to {u2[2]}."
    if u1[0] == "temperature":
        result = _from_celsius(_to_celsius(value, u1[1]), u2[1])
    else:
        result = value * u1[3] / u2[3]
    about = ""
    names = {u1[1], u2[1]}
    if u1[0] == "time" and names & {"month", "year"} and names not in (
            {"year", "month"}, {"year", "day"}):
        about = "about "
    left = left_label or f"{fmt_num(value)} {_unit_label(u1, value)}"
    return f"{left} is {about}{fmt_num(result)} {_unit_label(u2, result)}.", None
 
 
def tool_convert_units(args):
    """For Qwen tool calling."""
    try:
        value = float(args["value"])
    except (KeyError, TypeError, ValueError):
        return "I need a number to convert."
    text, error = _convert(value, str(args.get("from_unit", "")), str(args.get("to_unit", "")))
    return error or text
 
 
_VAL = r"(-?\d+(?:\.\d+)?|a|an)"
_UNIT_PHRASE = r"([a-z][a-z0-9/° ]*?)"
# each entry: (regex, index of value, index of from-unit, index of to-unit)
_UNIT_PATTERNS = [
    (re.compile(rf"how many {_UNIT_PHRASE} (?:(?:is|are) (?:there )?in|in|is|are|per) "
                rf"(?:{_VAL} )?{_UNIT_PHRASE}"), 2, 3, 1),
    (re.compile(rf"{_VAL} ?{_UNIT_PHRASE} (?:is|are|equals?) how many {_UNIT_PHRASE}"), 1, 2, 3),
    (re.compile(rf"{_VAL} ?{_UNIT_PHRASE} (?:to|in|into|as) {_UNIT_PHRASE}"), 1, 2, 3),
]
_UNIT_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|jarvis|please|can you|could you|would you|tell me|"
    r"i need to know|i want to know|what is|what are|whats|how much is|how much are|"
    r"convert|change|turn|express|translate|give me|show me)\s+)+")
_UNIT_TAIL = re.compile(r"\s+(?:please|for me|approximately|roughly|exactly|again)$")
 
 
def unit_answer(text):
    q = text.lower().replace("’", "'")
    q = re.sub(r"\bwhat'?s\b", "what is", q)
    q = re.sub(r"(\d+)\s*'\s*(\d+)\s*(?:\"|'')?", r"\1 feet \2 inches", q)      # 5'10"
    q = words_to_digits(q)
    q = re.sub(r"\b(minus|negative)\s+(?=\d)", "-", q)
    q = re.sub(r"[?!,]", " ", q)
    q = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", q)
 
    left_label = None
    combo = re.search(r"(\d+(?:\.\d+)?)\s*(?:feet|foot|ft)\s*(?:and\s*)?"
                      r"(\d+(?:\.\d+)?)\s*(?:inches|inch)\b", q)
    if combo:                                         # "5 feet 10 inches" -> 70 inches
        feet, inches = float(combo.group(1)), float(combo.group(2))
        left_label = f"{fmt_num(feet)} feet {fmt_num(inches)} inches"
        q = q[:combo.start()] + f"{feet * 12 + inches:g} inches" + q[combo.end():]
 
    q = re.sub(r"\s+", " ", q).strip()
    q = _UNIT_LEAD.sub("", q)
    q = _UNIT_TAIL.sub("", q).strip()
 
    for rx, vi, fi, ti in _UNIT_PATTERNS:
        m = rx.fullmatch(q)
        if not m:
            continue
        raw = m.group(vi)
        value = 1.0 if raw in (None, "a", "an") else float(raw)
        text_out, error = _convert(value, m.group(fi), m.group(ti), left_label)
        if text_out:
            return text_out
        # Only complain about an unknown unit when the user clearly asked to convert.
        if error.startswith("I can't convert") or "convert" in text.lower():
            return error
        return None
    return None
 
 
# ===============================================================
# 4. Time and date
# ===============================================================
 
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
 
_DT_FILLER = {"hey", "hi", "hello", "ok", "okay", "jarvis", "please", "can", "could",
              "would", "you", "tell", "me", "do", "know", "i", "want", "to", "like",
              "say", "give", "got", "have", "just", "quickly", "for", "thanks"}
# After the question itself is understood, only these words may remain.
_DT_ALLOWED = {"what", "which", "when", "is", "was", "will", "be", "it", "the", "a", "an",
               "date", "day", "time", "of", "week", "month", "year", "on", "today",
               "current", "right", "now", "currently", "at", "moment", "and", "local",
               "clock", "are", "this", "that"}
 
# City -> IANA zone. Needs the `tzdata` package on Windows (pip install tzdata).
_PLACES = {
    "new york": "America/New_York", "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "chicago": "America/Chicago",
    "toronto": "America/Toronto", "mexico city": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo", "london": "Europe/London", "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "moscow": "Europe/Moscow", "istanbul": "Europe/Istanbul", "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi",
    "johannesburg": "Africa/Johannesburg", "dubai": "Asia/Dubai", "riyadh": "Asia/Riyadh",
    "tehran": "Asia/Tehran", "karachi": "Asia/Karachi", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "dhaka": "Asia/Dhaka", "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "seoul": "Asia/Seoul",
    "tokyo": "Asia/Tokyo", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
    "utc": "UTC", "gmt": "UTC",
}
 
 
def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
 
 
def _time_str(dt):
    h, m = dt.hour, dt.minute
    if m == 0 and h == 0:
        return "midnight"
    if m == 0 and h == 12:
        return "noon"
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"
 
 
def _date_str(d, weekday=True, year=False):
    s = f"{_MONTHS[d.month - 1]} {_ordinal(d.day)}"
    if weekday:
        s = f"{_WEEKDAYS[d.weekday()]}, {s}"
    return f"{s}, {d.year}" if year else s
 
 
def _add_months(d, n):
    year, month = divmod(d.year * 12 + d.month - 1 + n, 12)
    month += 1
    return d.replace(year=year, month=month,
                     day=min(d.day, calendar.monthrange(year, month)[1]))
 
 
def _dt_normalize(text):
    q = text.lower().replace("’", "'")
    q = re.sub(r"\bwhat'?s\b", "what is", q)
    q = re.sub(r"\bit's\b", "it is", q)
    q = re.sub(r"'s\b", "", q)                       # today's -> today
    q = q.replace("'", "")
    q = re.sub(r"[^a-z0-9\s:.]", " ", q)
    q = re.sub(r"(?<!\d)[.:]|[.:](?!\d)", " ", q)
    q = words_to_digits(q)
    return " ".join(w for w in q.split() if w not in _DT_FILLER)
 
 
_REL_PATTERNS = [
    ("fixed", re.compile(r"\b(day after tomorrow|day before yesterday|tomorrow|yesterday)\b")),
    ("offset", re.compile(r"\b(?:(in|after)\s+)?(\d+|a|an)\s+(minute|hour|day|week|month|year)s?"
                          r"(?:\s+(from now|from today|ago))?\b")),
    ("period", re.compile(r"\b(next|last)\s+(week|month|year)\b")),
    ("weekday", re.compile(r"\b(?:(next|this|last|on|coming)\s+)?"
                           r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")),
]
 
 
def _resolve_relative(q, now):
    """Find a relative expression ('tomorrow', 'in 3 days', 'next friday'...).
    -> (span, spoken_answer, is_clock_question) or None."""
    today = now.date()
 
    def full(d):
        return _date_str(d, weekday=True, year=(d.year != today.year))
 
    for kind, rx in _REL_PATTERNS:
        for m in rx.finditer(q):
            span = m.span()
 
            if kind == "fixed":
                d = {"day after tomorrow": 2, "day before yesterday": -2,
                     "tomorrow": 1, "yesterday": -1}[m.group(1)]
                lead = {2: "The day after tomorrow", -2: "The day before yesterday",
                        1: "Tomorrow", -1: "Yesterday"}[d]
                return span, f"{lead} {'is' if d > 0 else 'was'} {full(today + timedelta(days=d))}.", False
 
            if kind == "offset":
                lead_word, amount, unit, suffix = m.groups()
                if not lead_word and not suffix:
                    continue                             # "5 days" on its own is not relative
                n = 1 if amount in ("a", "an") else int(amount)
                past = suffix == "ago"
                sign = -1 if past else 1
                count = ("an" if unit == "hour" else "a") if n == 1 else str(n)
                plural = "" if n == 1 else "s"
                lead = f"{count} {unit}{plural} ago" if past else f"In {count} {unit}{plural}"
                lead = lead[0].upper() + lead[1:]
                verb = "it was" if past else "it will be"
                try:
                    if unit in ("minute", "hour"):
                        target = now + sign * timedelta(**{unit + "s": n})
                        when = ""
                        if target.date() != today:
                            when = (" tomorrow" if (target.date() - today).days == 1 else
                                    " yesterday" if (target.date() - today).days == -1 else
                                    f" on {_WEEKDAYS[target.weekday()]}")
                        return span, f"{lead}, {verb} {_time_str(target)}{when}.", True
                    if unit in ("day", "week"):
                        target = today + sign * timedelta(days=n * (7 if unit == "week" else 1))
                    else:
                        target = _add_months(today, sign * n * (12 if unit == "year" else 1))
                except (OverflowError, ValueError):
                    return span, "That date is out of range.", False
                return span, f"{lead}, {verb} {full(target)}.", False
 
            if kind == "period":
                which, unit = m.groups()
                sign = 1 if which == "next" else -1
                try:
                    if unit == "week":
                        target = today + timedelta(days=7 * sign)
                    else:
                        target = _add_months(today, sign * (12 if unit == "year" else 1))
                except (OverflowError, ValueError):
                    return span, "That date is out of range.", False
                lead = f"A {unit} from today is" if sign > 0 else f"A {unit} ago was"
                return span, f"{lead} {full(target)}.", False
 
            if kind == "weekday":
                prefix, name = m.groups()
                if not prefix and "when" not in q.split():
                    continue
                wd = _WEEKDAYS.index(name.capitalize())
                if prefix == "next":                   # the same weekday in next calendar week
                    target = today + timedelta(days=7 - today.weekday() + wd)
                    return span, f"Next {name.capitalize()} is {_date_str(target, False, target.year != today.year)}.", False
                if prefix == "last":
                    delta = (today.weekday() - wd) % 7 or 7
                    target = today - timedelta(days=delta)
                    return span, f"Last {name.capitalize()} was {_date_str(target, False, target.year != today.year)}.", False
                delta = (wd - today.weekday()) % 7
                target = today + timedelta(days=delta)
                if delta == 0:
                    return span, f"Today is {full(target)}.", False
                return span, f"{name.capitalize()} is {_date_str(target, False, target.year != today.year)}.", False
    return None
 
 
def _time_in_place(place, now):
    place = re.sub(r"\b(the|city)\b", " ", place)
    place = re.sub(r"\s+", " ", place).strip()
    zone = _PLACES.get(place)
    if zone is None:
        return f"I don't know the time zone for {place.title()}."
    try:
        from zoneinfo import ZoneInfo
        there = now.astimezone(ZoneInfo(zone))
    except Exception:
        return "I can't look up other time zones yet. Install the tzdata package to enable that."
    when = ""
    delta = (there.date() - now.date()).days
    if delta == 1:
        when = " tomorrow"
    elif delta == -1:
        when = " yesterday"
    return f"In {place.title()} it's {_time_str(there)}{when}."
 
 
def datetime_answer(text, now=None):
    now = now or datetime.now().astimezone()
    q = _dt_normalize(text)
    if not q:
        return None

    # Follow-ups ("and at what time?") refer to the conversation, not the clock.
    # Let Qwen answer those from the history.
    if re.match(r"(?:and|also|but|then|so)\b", q) and "is" not in q.split():
        return None
    if re.search(r"\bat what (?:time|day|date)\b", q):
        return None
 
    # Time in another city: "what time is it in tokyo"
    m = re.fullmatch(r"(?:what )?(?:is )?(?:the )?(?:current )?time(?: is it)?"
                     r"(?: right now| now)? in ([a-z ]+?)(?: right now| now)?", q)
    if m:
        return _time_in_place(m.group(1), now)
 
    # Relative: tomorrow, in 3 days, 2 hours from now, next friday, ...
    rel = _resolve_relative(q, now)
    if rel:
        (start, end), answer, is_clock = rel
        leftover = (q[:start] + " " + q[end:]).split()
        if (all(w in _DT_ALLOWED for w in leftover)
                and {"what", "which", "when", "date", "day", "time"} & set(leftover)
                and not ("time" in leftover and not is_clock)):
            return answer
        return None
 
    words = set(q.split())
    if not words <= _DT_ALLOWED:
        return None
    asks = {"what", "which"} & words
    want_time = "time" in words
    want_date = "date" in words
    want_day = "day" in words and bool(asks)
    want_month = "month" in words and bool(asks)
    want_year = "year" in words and bool(asks)
    if q == "what is today":
        want_date = True
 
    today = now.date()
    if want_time and (want_date or want_day):
        return f"It's {_time_str(now)} on {_date_str(today, True, False)}."
    if want_time:
        return f"It's {_time_str(now)}."
    if want_date:
        return f"Today is {_date_str(today, True, True)}."
    if want_day:
        if "week" in words:
            return f"It's {_WEEKDAYS[today.weekday()]}."
        return f"Today is {_date_str(today, True, False)}."
    if want_year and not want_month:
        return f"It's {today.year}."
    if want_month and not want_year:
        return f"It's {_MONTHS[today.month - 1]}."
    return None
 
 
def tool_get_datetime(_args=None):
    """For Qwen tool calling."""
    now = datetime.now().astimezone()
    return f"{_date_str(now.date(), True, True)}. The time is {_time_str(now)}."
 
 
# ===============================================================
# 5. One entry point for the router
# ===============================================================
 
def quick_answer(text, now=None):
    """Return a spoken answer for a unit, math, or time/date request, else None."""
    for name, handler in (("units", unit_answer), ("calc", calc_answer),
                          ("datetime", lambda t: datetime_answer(t, now))):
        try:
            answer = handler(text)
        except Exception as error:          # a tool bug must never crash the assistant
            print(f"[Tool error] {name}: {error}")
            continue
        if answer:
            print(f"[Tool:{name}] {text!r} -> {answer}")
            return answer
    return None
 
 
# ===============================================================
# 6. Router: rules first, Qwen tool calling only when it can help
#
#   route(text) -> {"answer": str | None, "tools": [schemas], "prompt": str}
#     answer  - ready spoken reply (no Qwen call needed), or None
#     tools   - tool schemas to send to Ollama ("tools" key); empty = send none
#     prompt  - text to append to the system prompt (empty when no tools)
#   execute_tool_call(tool_call) -> result string to give back to Qwen
# ===============================================================
 
TOOL_PROMPT = (
    " You have tools. Call one only when the request needs the exact current time "
    "or date, a calculation, or a unit conversion. Pass arguments exactly as the "
    "tool describes. After the tool answers, say the result in one short spoken "
    "sentence using its numbers. Never make up a tool result, and never say you "
    "did something a tool did not confirm."
)
 
_TOOLS = []     # registered tools: {"name", "schema", "fn", "hint"}
 
 
def register_tool(name, description, properties, required, fn, hint):
    """Add a tool Qwen may call. `hint` is a regex: the tool is only offered when
    the user's sentence matches it, which keeps prompts short and replies fast."""
    _TOOLS.append({
        "name": name,
        "schema": {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required}}},
        "fn": fn,
        "hint": re.compile(hint),
    })
 
 
register_tool(
    "get_datetime",
    "Get the current local date, weekday and time. Use it for any question that "
    "depends on today's date or the time now, such as how many days until an event.",
    {}, [], tool_get_datetime,
    r"\b(?:time|date|tomorrow|yesterday|weekday|what day|which day|"
    r"how many days|until|o'clock)\b")
 
register_tool(
    "calculate",
    "Evaluate an arithmetic expression. Use digits, + - * / % ** and parentheses, "
    "and the functions sqrt, abs, round, factorial, log10. Example: 80 * 0.15",
    {"expression": {"type": "string", "description": "The expression to evaluate"}},
    ["expression"], tool_calculate,
    r"\d\s*[-+*/x%^]\s*\d|%|\b(?:plus|minus|times|divided|multipl\w*|percent|"
    r"square root|squared|cubed|factorial|calculate|compute|sum|total|average|tip|"
    r"discount|half|double|triple|power|remainder)\b")
 
register_tool(
    "convert_units",
    "Convert a value between units of length, mass, volume, time, speed, area, "
    "digital data or temperature. Example: value 5, from_unit miles, to_unit kilometers.",
    {"value": {"type": "number"},
     "from_unit": {"type": "string", "description": "Unit to convert from"},
     "to_unit": {"type": "string", "description": "Unit to convert to"}},
    ["value", "from_unit", "to_unit"], tool_convert_units,
    r"\b(?:convert|conversion|how many|how much|how long|how far|how heavy|how tall|"
    r"celsius|fahrenheit|kelvin|miles?|kilometers?|kilometres?|km|meters?|metres?|"
    r"feet|foot|inch(?:es)?|pounds?|kilograms?|kg|grams?|ounces?|liters?|litres?|"
    r"gallons?|cups?|mph|acres?)\b")
 
 
def tool_schemas_for(text):
    t = text.lower()
    return [tool["schema"] for tool in _TOOLS if tool["hint"].search(t)]
 
 
def execute_tool_call(tool_call):
    """Run one tool call from Qwen safely and return the result text."""
    function = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    name = function.get("name", "")
    args = function.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    tool = next((t for t in _TOOLS if t["name"] == name), None)
    if tool is None:
        return f"There is no tool called {name}."
    try:
        result = str(tool["fn"](args))
    except Exception as error:
        print(f"[Tool error] {name}: {error}")
        result = "That tool failed."
    print(f"[Tool] {name}({args}) -> {result}")
    return result
 
 
def route(text, now=None):
    answer = quick_answer(text, now)
    if answer:
        return {"answer": answer, "tools": [], "prompt": ""}
    tools = tool_schemas_for(text)
    return {"answer": None, "tools": tools, "prompt": TOOL_PROMPT if tools else ""}
 
 
# ===============================================================
# Self-test:  python jarvis_tools.py
# ===============================================================
 
if __name__ == "__main__":
    fixed_now = datetime(2026, 11, 5, 12, 5).astimezone()      # a Thursday
    cases = [
        ("what time is it", "It's 12:05 PM."),
        ("what's the date today", "Today is Thursday, November 5th, 2026."),
        ("what day is it tomorrow", "Tomorrow is Friday, November 6th."),
        ("what is the date in seven days", "In 7 days, it will be Thursday, November 12th."),
        ("what time will it be in two hours", "In 2 hours, it will be 2:05 PM."),
        ("what's the date next friday", "Next Friday is November 13th."),
        ("how many hours in 3 days", "3 days is 72 hours."),
        ("what time should I set the alarm", None),
        ("will it rain tomorrow", None),
        ("convert 5 miles to kilometers", "5 miles is 8.05 kilometers."),
        ("how many minutes in two hours", "2 hours is 120 minutes."),
        ("what is 15 percent of 80", "The answer is 12."),
        ("what's two plus two", "The answer is 4."),
        ("what is ten divided by zero", "You can't divide by zero."),
        ("set a timer for 5 minutes", None),
        ("what is the capital of france", None),
    ]
    failed = 0
    for phrase, expected in cases:
        got = quick_answer(phrase, fixed_now)
        ok = got == expected
        failed += not ok
        print(("PASS" if ok else "FAIL"), "|", phrase, "->", got, "" if ok else f"(expected {expected})")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")