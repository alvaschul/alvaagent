"""A small, bounded interpreter for the run_python tool.

This module interprets an explicit AST subset. It never hands source text to a
Python source-execution primitive and never starts a child process.
"""
import ast
import math


MAX_CODE_CHARS = 10000
MAX_AST_NODES = 600
MAX_AST_DEPTH = 32
MAX_OPERATIONS = 5000
MAX_OUTPUT_CHARS = 5000
MAX_STRING_CHARS = 10000
MAX_CONTAINER_ITEMS = 1000
MAX_ITERATION_ITEMS = 500
MAX_VALUE_UNITS = 10000
MAX_INT_BITS = 512


class PythonSubsetError(ValueError):
    """Input or work exceeded the deliberately small interpreter subset."""


_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_PURE_CALLS = {
    "abs", "all", "any", "bool", "enumerate", "float", "int", "len",
    "max", "min", "range", "round", "sorted", "str", "sum", "tuple",
    "list", "zip",
}
_RESERVED_NAMES = _PURE_CALLS | {"print"}
_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.If, ast.Pass,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Pow, ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And,
    ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.IfExp, ast.Subscript, ast.Slice, ast.Call,
    ast.ListComp, ast.comprehension,
)


def _reject(message):
    raise PythonSubsetError(message)


def parse_python_subset(source):
    """Parse and validate source against the explicitly supported AST subset."""
    if not isinstance(source, str):
        _reject("code must be text")
    if not source.strip():
        _reject("empty code")
    if len(source) > MAX_CODE_CHARS:
        _reject("code too long (>%d chars)" % MAX_CODE_CHARS)
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        _reject("invalid Python syntax: %s" % exc)

    nodes = 0
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_AST_NODES:
            _reject("program is too large (>%d AST nodes)" % MAX_AST_NODES)
        if depth > MAX_AST_DEPTH:
            _reject("program is too deeply nested (>%d levels)" % MAX_AST_DEPTH)
        if not isinstance(node, _ALLOWED_NODES):
            _reject("unsupported syntax: %s" % type(node).__name__)
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            value = node.value
            if type(value) not in (str, int, float, bool, type(None)):
                _reject("only text, numeric, boolean, and None literals are supported")
            if type(value) is str and len(value) > MAX_STRING_CHARS:
                _reject("string literal is too large")
            if type(value) is int and value.bit_length() > MAX_INT_BITS:
                _reject("integer literal is too large")
            if type(value) is float and not math.isfinite(value):
                _reject("non-finite numeric literals are not supported")
        elif isinstance(node, ast.Name):
            if (not node.id or node.id.startswith("_") or len(node.id) > 64
                    or not isinstance(node.ctx, (ast.Load, ast.Store))):
                _reject("name is not allowed: %s" % node.id)
            if isinstance(node.ctx, ast.Store) and node.id in _RESERVED_NAMES:
                _reject("cannot assign to reserved name: %s" % node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                _reject("only direct calls to allowlisted functions are supported")
            if node.func.id != "print" and node.func.id not in _PURE_CALLS:
                _reject("function is not allowlisted: %s" % node.func.id)
            if node.keywords:
                _reject("keyword arguments are not supported")
        elif isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                _reject("assignment is limited to one simple variable")
        elif isinstance(node, (ast.AugAssign, ast.For)):
            if not isinstance(node.target, ast.Name):
                _reject("loop and augmented-assignment targets must be simple variables")
            if isinstance(node, ast.AugAssign) and not isinstance(node.op, _BINOPS):
                _reject("augmented operator is not supported")
            if isinstance(node, ast.For) and node.orelse:
                _reject("for-else is not supported")
        elif isinstance(node, ast.ListComp):
            if len(node.generators) != 1:
                _reject("comprehensions support exactly one bounded loop")
            generator = node.generators[0]
            if generator.is_async or not isinstance(generator.target, ast.Name):
                _reject("comprehension target must be one simple variable")
        elif isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                _reject("dictionary unpacking is not supported")

    return tree


def _is_number(value):
    return type(value) in (int, float)


def _check_value(value):
    """Reject oversized or unsupported runtime values before they accumulate."""
    stack = [(value, 0)]
    units = 0
    while stack:
        item, depth = stack.pop()
        if depth > MAX_AST_DEPTH:
            _reject("value is too deeply nested")
        item_type = type(item)
        units += 1
        if item_type is str:
            if len(item) > MAX_STRING_CHARS:
                _reject("string value is too large")
            units += len(item)
        elif item_type is int:
            if item.bit_length() > MAX_INT_BITS:
                _reject("integer result is too large")
        elif item_type is float:
            if not math.isfinite(item):
                _reject("non-finite numeric results are not supported")
        elif item_type in (bool, type(None)):
            pass
        elif item_type in (list, tuple):
            if len(item) > MAX_CONTAINER_ITEMS:
                _reject("collection is too large (>%d items)" % MAX_CONTAINER_ITEMS)
            stack.extend((child, depth + 1) for child in item)
        elif item_type is dict:
            if len(item) > MAX_CONTAINER_ITEMS:
                _reject("dictionary is too large (>%d items)" % MAX_CONTAINER_ITEMS)
            for key, child in item.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        else:
            _reject("runtime value type is not supported: %s" % item_type.__name__)
        if units > MAX_VALUE_UNITS:
            _reject("data exceeds the %d-unit memory budget" % MAX_VALUE_UNITS)
    return value


def _bounded_iterable(value):
    if type(value) not in (list, tuple, str, dict):
        _reject("expected a bounded list, tuple, dictionary, or string")
    if len(value) > MAX_ITERATION_ITEMS:
        _reject("iteration exceeds the %d-item limit" % MAX_ITERATION_ITEMS)
    if type(value) is dict:
        return tuple(value.keys())
    return tuple(value)


def _safe_int(value):
    if type(value) is int:
        return value
    if type(value) is bool:
        return int(value)
    if type(value) is float:
        if not math.isfinite(value):
            _reject("cannot convert a non-finite value to int")
        return int(value)
    if type(value) is str and len(value) <= 1000:
        return int(value)
    _reject("int() accepts only bounded numeric or text values")


def _safe_float(value):
    if type(value) in (int, float, bool):
        result = float(value)
    elif type(value) is str and len(value) <= 1000:
        result = float(value)
    else:
        _reject("float() accepts only bounded numeric or text values")
    if not math.isfinite(result):
        _reject("non-finite numeric results are not supported")
    return result


class _Interpreter:
    def __init__(self, cancel=None):
        self.cancel = cancel
        self.env = {}
        self.steps = 0
        self.output = []
        self.output_chars = 0
        self.output_truncated = False

    def _tick(self):
        self.steps += 1
        if self.steps > MAX_OPERATIONS:
            _reject("operation budget exceeded (%d steps)" % MAX_OPERATIONS)
        if self.cancel is not None and self.cancel.is_set():
            _reject("cancelled by user")

    def _check_environment(self):
        if len(self.env) > MAX_AST_NODES:
            _reject("too many variables")
        units = 0
        for value in self.env.values():
            units += self._value_units(value)
            if units > MAX_VALUE_UNITS:
                _reject("data exceeds the %d-unit memory budget" % MAX_VALUE_UNITS)

    @staticmethod
    def _value_units(value):
        stack = [(value, 0)]
        units = 0
        while stack:
            item, depth = stack.pop()
            if depth > MAX_AST_DEPTH:
                _reject("value is too deeply nested")
            item_type = type(item)
            units += 1
            if item_type is str:
                units += len(item)
            elif item_type in (list, tuple):
                stack.extend((child, depth + 1) for child in item)
            elif item_type is dict:
                for key, child in item.items():
                    stack.append((key, depth + 1))
                    stack.append((child, depth + 1))
            if units > MAX_VALUE_UNITS:
                return units
        return units

    def _emit(self, values):
        if self.output_truncated:
            return None
        for index, value in enumerate(values):
            if index:
                self._append_output(" ")
            if self.output_truncated:
                return None
            self._append_output(str(value))
            if self.output_truncated:
                return None
        self._append_output("\n")
        return None

    def _append_output(self, text):
        room = MAX_OUTPUT_CHARS - self.output_chars
        if len(text) <= room:
            self.output.append(text)
            self.output_chars += len(text)
            return
        marker = "\n... (truncated)"
        if room >= len(marker):
            self.output.append(text[:room - len(marker)] + marker)
        else:
            self.output.append(text[:room])
        self.output_chars += room
        self.output_truncated = True

    def _apply_binop(self, op, left, right):
        if op is ast.Add:
            if _is_number(left) and _is_number(right):
                result = left + right
            elif type(left) is type(right) and type(left) in (str, list, tuple):
                size = len(left) + len(right)
                limit = MAX_STRING_CHARS if type(left) is str else MAX_CONTAINER_ITEMS
                if size > limit:
                    _reject("combined value exceeds its size limit")
                result = left + right
            else:
                _reject("+ requires two numbers or matching strings/collections")
        elif op is ast.Mult:
            if _is_number(left) and _is_number(right):
                if type(left) is int and type(right) is int and left and right:
                    if abs(left).bit_length() + abs(right).bit_length() > MAX_INT_BITS + 1:
                        _reject("integer multiplication exceeds the numeric limit")
                result = left * right
            elif type(left) in (str, list, tuple) and type(right) is int:
                limit = MAX_STRING_CHARS if type(left) is str else MAX_CONTAINER_ITEMS
                if len(left) * max(0, right) > limit:
                    _reject("repeated value exceeds its size limit")
                result = left * right
            elif type(right) in (str, list, tuple) and type(left) is int:
                limit = MAX_STRING_CHARS if type(right) is str else MAX_CONTAINER_ITEMS
                if len(right) * max(0, left) > limit:
                    _reject("repeated value exceeds its size limit")
                result = right * left
            else:
                _reject("* requires numbers or a collection and an integer")
        elif op is ast.Pow:
            if not (_is_number(left) and _is_number(right)):
                _reject("** requires numeric operands")
            if abs(right) > 100:
                _reject("exponent magnitude exceeds 100")
            if (type(left) is int and type(right) is int and right > 0
                    and abs(left) not in (0, 1)
                    and abs(left).bit_length() * right > MAX_INT_BITS):
                _reject("power result exceeds the numeric limit")
            result = left ** right
        else:
            if not (_is_number(left) and _is_number(right)):
                _reject("arithmetic operators require numeric operands")
            if op in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
                _reject("division or modulo by zero")
            if op is ast.Sub:
                result = left - right
            elif op is ast.Div:
                result = left / right
            elif op is ast.FloorDiv:
                result = left // right
            elif op is ast.Mod:
                result = left % right
            else:
                _reject("operator is not supported")
        return _check_value(result)

    def _call(self, name, args):
        if name == "print":
            return self._emit(args)
        if name == "range":
            if not 1 <= len(args) <= 3 or any(type(arg) is not int for arg in args):
                _reject("range() expects one to three integer arguments")
            if any(abs(arg) > 10000 for arg in args):
                _reject("range() argument is too large")
            values_range = range(*args)
            if len(values_range) > MAX_ITERATION_ITEMS:
                _reject("range() exceeds the %d-item limit" % MAX_ITERATION_ITEMS)
            return tuple(values_range)
        if name in ("enumerate", "zip"):
            if name == "enumerate":
                if not 1 <= len(args) <= 2:
                    _reject("enumerate() expects one sequence and an optional start")
                values = _bounded_iterable(args[0])
                start = args[1] if len(args) == 2 else 0
                if type(start) is not int or abs(start) > 1000000:
                    _reject("enumerate() start must be a bounded integer")
                return tuple((start + index, value) for index, value in enumerate(values))
            if not 1 <= len(args) <= 4:
                _reject("zip() expects one to four bounded sequences")
            sequences = [_bounded_iterable(arg) for arg in args]
            return tuple(tuple(row) for row in zip(*sequences))
        if name in ("list", "tuple"):
            if len(args) > 1:
                _reject("%s() accepts at most one argument" % name)
            values = [] if not args else list(_bounded_iterable(args[0]))
            return tuple(values) if name == "tuple" else values
        if name == "len":
            if len(args) != 1 or type(args[0]) not in (str, list, tuple, dict):
                _reject("len() expects one bounded collection")
            return len(args[0])
        if name == "sum":
            if not 1 <= len(args) <= 2:
                _reject("sum() expects a sequence and optional numeric start")
            values = _bounded_iterable(args[0])
            result = args[1] if len(args) == 2 else 0
            if not _is_number(result):
                _reject("sum() start must be numeric")
            for value in values:
                if not _is_number(value):
                    _reject("sum() accepts only numeric items")
                result = self._apply_binop(ast.Add, result, value)
            return result
        if name in ("min", "max"):
            if not args:
                _reject("%s() expects one or more values" % name)
            values = _bounded_iterable(args[0]) if len(args) == 1 else args
            if not values:
                _reject("%s() cannot select from an empty sequence" % name)
            try:
                return min(values) if name == "min" else max(values)
            except (TypeError, ValueError) as exc:
                _reject("%s() failed: %s" % (name, exc))
        if name == "sorted":
            if len(args) != 1:
                _reject("sorted() expects one bounded sequence")
            values = _bounded_iterable(args[0])
            try:
                return sorted(values)
            except (TypeError, ValueError) as exc:
                _reject("sorted() failed: %s" % exc)
        if name == "all" or name == "any":
            if len(args) != 1:
                _reject("%s() expects one bounded sequence" % name)
            values = _bounded_iterable(args[0])
            return all(values) if name == "all" else any(values)
        if name == "abs":
            if len(args) != 1 or not _is_number(args[0]):
                _reject("abs() expects one number")
            return abs(args[0])
        if name == "round":
            if not 1 <= len(args) <= 2 or not _is_number(args[0]):
                _reject("round() expects a number and optional integer precision")
            if len(args) == 2 and (type(args[1]) is not int or abs(args[1]) > 100):
                _reject("round() precision must be an integer between -100 and 100")
            return round(*args)
        if name == "int":
            if len(args) > 1:
                _reject("int() accepts at most one argument")
            return 0 if not args else _safe_int(args[0])
        if name == "float":
            if len(args) > 1:
                _reject("float() accepts at most one argument")
            return 0.0 if not args else _safe_float(args[0])
        if name == "bool":
            if len(args) > 1:
                _reject("bool() accepts at most one argument")
            return bool(args[0]) if args else False
        if name == "str":
            if len(args) > 1:
                _reject("str() accepts at most one argument")
            result = "" if not args else str(args[0])
            return _check_value(result)
        _reject("function is not allowlisted: %s" % name)

    def _eval(self, node):
        self._tick()
        if isinstance(node, ast.Constant):
            return _check_value(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            if node.id == "None":
                return None
            _reject("name is not defined: %s" % node.id)
        if isinstance(node, ast.List):
            if len(node.elts) > MAX_CONTAINER_ITEMS:
                _reject("list literal is too large")
            return _check_value([self._eval(item) for item in node.elts])
        if isinstance(node, ast.Tuple):
            if len(node.elts) > MAX_CONTAINER_ITEMS:
                _reject("tuple literal is too large")
            return _check_value(tuple(self._eval(item) for item in node.elts))
        if isinstance(node, ast.Dict):
            if len(node.keys) > MAX_CONTAINER_ITEMS:
                _reject("dictionary literal is too large")
            result = {}
            for key_node, value_node in zip(node.keys, node.values):
                key = self._eval(key_node)
                value = self._eval(value_node)
                try:
                    result[key] = value
                except TypeError as exc:
                    _reject("dictionary keys must be immutable: %s" % exc)
            return _check_value(result)
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left), self._eval(node.right)
            return self._apply_binop(type(node.op), left, right)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if not _is_number(value):
                _reject("unary arithmetic requires a number")
            return _check_value(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = self._eval(node.values[0])
                for child in node.values[1:]:
                    if not result:
                        break
                    result = self._eval(child)
                return result
            result = self._eval(node.values[0])
            for child in node.values[1:]:
                if result:
                    break
                result = self._eval(child)
            return result
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op_node, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator)
                try:
                    if isinstance(op_node, ast.Eq):
                        matched = left == right
                    elif isinstance(op_node, ast.NotEq):
                        matched = left != right
                    elif isinstance(op_node, ast.Lt):
                        matched = left < right
                    elif isinstance(op_node, ast.LtE):
                        matched = left <= right
                    elif isinstance(op_node, ast.Gt):
                        matched = left > right
                    elif isinstance(op_node, ast.GtE):
                        matched = left >= right
                    elif isinstance(op_node, ast.In):
                        matched = left in right
                    elif isinstance(op_node, ast.NotIn):
                        matched = left not in right
                    else:
                        _reject("comparison operator is not supported")
                except (TypeError, ValueError) as exc:
                    _reject("comparison failed: %s" % exc)
                if not matched:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body if self._eval(node.test) else node.orelse)
        if isinstance(node, ast.Subscript):
            value = self._eval(node.value)
            if type(value) not in (list, tuple, dict, str):
                _reject("indexing is limited to bounded collections")
            key = node.slice
            if isinstance(key, ast.Slice):
                parts = []
                for part in (key.lower, key.upper, key.step):
                    item = None if part is None else self._eval(part)
                    if item is not None and type(item) is not int:
                        _reject("slice indexes must be integers")
                    parts.append(item)
                if parts[2] == 0:
                    _reject("slice step cannot be zero")
                key = slice(*parts)
            else:
                key = self._eval(key)
            try:
                return _check_value(value[key])
            except (IndexError, KeyError, TypeError) as exc:
                _reject("invalid index: %s" % exc)
        if isinstance(node, ast.Call):
            name = node.func.id
            args = [self._eval(arg) for arg in node.args]
            try:
                return _check_value(self._call(name, args))
            except PythonSubsetError:
                raise
            except (ArithmeticError, TypeError, ValueError, OverflowError) as exc:
                _reject("%s() failed: %s" % (name, exc))
        if isinstance(node, ast.ListComp):
            generator = node.generators[0]
            iterable = _bounded_iterable(self._eval(generator.iter))
            name = generator.target.id
            existed = name in self.env
            old_value = self.env.get(name)
            result = []
            try:
                for item in iterable:
                    self._tick()
                    self.env[name] = item
                    self._check_environment()
                    if all(self._eval(condition) for condition in generator.ifs):
                        result.append(self._eval(node.elt))
                        if len(result) > MAX_CONTAINER_ITEMS:
                            _reject("comprehension result is too large")
                        _check_value(result)
                return _check_value(result)
            finally:
                if existed:
                    self.env[name] = old_value
                else:
                    self.env.pop(name, None)
        _reject("unsupported expression: %s" % type(node).__name__)

    def _execute(self, statements):
        for node in statements:
            self._tick()
            if isinstance(node, ast.Expr):
                self._eval(node.value)
            elif isinstance(node, ast.Assign):
                value = self._eval(node.value)
                self.env[node.targets[0].id] = value
                self._check_environment()
            elif isinstance(node, ast.AugAssign):
                name = node.target.id
                if name not in self.env:
                    _reject("name is not defined: %s" % name)
                value = self._apply_binop(type(node.op), self.env[name], self._eval(node.value))
                self.env[name] = value
                self._check_environment()
            elif isinstance(node, ast.If):
                self._execute(node.body if self._eval(node.test) else node.orelse)
            elif isinstance(node, ast.For):
                values = _bounded_iterable(self._eval(node.iter))
                name = node.target.id
                for value in values:
                    self._tick()
                    self.env[name] = value
                    self._check_environment()
                    self._execute(node.body)
            elif isinstance(node, ast.Pass):
                continue
            else:
                _reject("unsupported statement: %s" % type(node).__name__)


def interpret_python(source, cancel=None):
    """Interpret source in the bounded subset and return captured print output."""
    tree = parse_python_subset(source)
    interpreter = _Interpreter(cancel=cancel)
    try:
        interpreter._execute(tree.body)
    except PythonSubsetError:
        raise
    except (ArithmeticError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        _reject("calculation failed: %s: %s" % (type(exc).__name__, exc))
    return "".join(interpreter.output)
